<#
.SYNOPSIS
    停掉本机的对外服务（看门狗 → 隧道 → 应用），代码仓库原样保留。

.DESCRIPTION
    把线上服务迁到另一台机器时，源码机必须先停干净再让新机器接管。

    同一条隧道在两台机器同时运行不会报错，Cloudflare 会把请求**在两边负载均衡**——
    而两边的数据库各自独立，用户会看到「数据时有时无」，且很难联想到是双跑导致的。
    所以迁移的第一步永远是「停旧的」，不是「起新的」。

    只停服务，不动 .git / .venv / node_modules / data，本机继续用来开发。

.PARAMETER Port
    应用端口，默认取 backend\.env 的 PORT，再默认 8000。

.PARAMETER TunnelName
    隧道名，用于在 cloudflared 进程里认出自己那条。默认 yintu-patent。

.PARAMETER KeepTunnel
    只停应用与看门狗，保留隧道进程。

.PARAMETER Domain
    停完之后探测这个域名，确认已不再由本机响应。默认 yintuai.com。

.EXAMPLE
    .\deploy\stop-serving.ps1
#>
[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]    $Port       = 0,
    [string] $TunnelName = 'yintu-patent',
    [switch] $KeepTunnel,
    [string] $Domain     = 'yintuai.com'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }

$Root       = Split-Path -Parent $PSScriptRoot
$Backend    = Join-Path $Root 'backend'
$Py         = Join-Path $Backend '.venv\Scripts\python.exe'
$PidFile    = Join-Path $Root 'data\watchdog.pid'
$WatchdogPs = Join-Path $Root 'watchdog.ps1'

function Write-Line {
    param([string] $Level, [string] $Message)
    $color = switch ($Level) { 'OK' { 'Green' } 'WARN' { 'Yellow' } 'ERR' { 'Red' } 'STEP' { 'Cyan' } default { 'Gray' } }
    Write-Host ('{0} [{1}] {2}' -f (Get-Date -Format 'HH:mm:ss'), $Level, $Message) -ForegroundColor $color
}

# 端口：-Port > .env 的 PORT > 8000
if ($Port -eq 0) {
    $Port = 8000
    $envFile = Join-Path $Backend '.env'
    if (Test-Path -LiteralPath $envFile) {
        foreach ($raw in (Get-Content -LiteralPath $envFile -ErrorAction SilentlyContinue)) {
            $line = $raw.Trim()
            if (-not $line -or $line.StartsWith('#')) { continue }
            if ($line -match '^\s*PORT\s*=\s*"?''?(\d+)"?''?\s*$') { $Port = [int] $Matches[1] }
        }
    }
}
Write-Line 'STEP' "停止对外服务（端口 $Port，隧道 $TunnelName）"

# --- 1. 看门狗（必须最先停，否则它会把应用重新拉起来）----------------------
$wdPids = [System.Collections.Generic.HashSet[int]]::new()
if (Test-Path $PidFile) {
    $rec = @{}
    foreach ($line in (Get-Content $PidFile -ErrorAction SilentlyContinue)) {
        if ($line -match '^\s*([a-z]+)\s*=\s*(.+?)\s*$') { $rec[$Matches[1]] = $Matches[2] }
    }
    if ($rec['root'] -eq $Root -and $rec['pid'] -match '^\d+$') {
        [void] $wdPids.Add([int] $rec['pid'])
    }
}
# 兜底只认本仓库的绝对路径：同机可能还有别的项目的 watchdog.ps1，不能误伤
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue -Filter "Name='powershell.exe' OR Name='pwsh.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*$WatchdogPs*" } |
    ForEach-Object { [void] $wdPids.Add([int] $_.ProcessId) }

if ($wdPids.Count -gt 0) {
    foreach ($processId in @($wdPids)) { Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 1
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    Write-Line 'OK' "已停看门狗（$($wdPids.Count) 个）"
} else {
    Write-Line 'INFO' '看门狗未运行'
}

# --- 2. 隧道 ----------------------------------------------------------------
if ($KeepTunnel) {
    Write-Line 'INFO' '按要求保留隧道进程'
} else {
    $tun = @(Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*$TunnelName*" })
    if ($tun.Count -gt 0) {
        foreach ($p in $tun) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
        Write-Line 'OK' "已停隧道（$($tun.Count) 个进程）"
    } else {
        Write-Line 'INFO' "未找到名为 $TunnelName 的隧道进程"
    }
    # 同机可能还有别的项目的 cloudflared（服务形态），必须原样留着
    $others = @(Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -notlike "*$TunnelName*" })
    if ($others.Count -gt 0) {
        Write-Line 'INFO' "另有 $($others.Count) 个 cloudflared 进程（其它项目的），未触碰"
    }
}

# --- 3. 应用 ----------------------------------------------------------------
$appPids = [System.Collections.Generic.HashSet[int]]::new()
try {
    Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        ForEach-Object { [void] $appPids.Add([int] $_.OwningProcess) }
} catch { }
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*$Py*" -and $_.CommandLine -like '*uvicorn*' } |
    ForEach-Object { [void] $appPids.Add([int] $_.ProcessId) }

if ($appPids.Count -gt 0) {
    foreach ($processId in @($appPids)) { Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
    Write-Line 'OK' "已停应用（$($appPids.Count) 个进程）"
} else {
    Write-Line 'INFO' '应用未运行'
}

# --- 4. 确认 ----------------------------------------------------------------
$localUp = $false
try {
    $null = Invoke-RestMethod "http://127.0.0.1:$Port/api/v1/system/health" -TimeoutSec 4 -ErrorAction Stop
    $localUp = $true
} catch { }
if ($localUp) {
    Write-Line 'ERR' "端口 $Port 仍在响应，本机没停干净——别急着在新机器上启动，会变成两边分流"
    exit 1
}
Write-Line 'OK' "本机 $Port 已停止响应"

if ($Domain) {
    Write-Line 'STEP' "探测 $Domain（此时应当打不开；等新机器接管后再变成 200）"
    try {
        $r = Invoke-WebRequest "https://$Domain/api/v1/system/health" -TimeoutSec 8 -UseBasicParsing -ErrorAction Stop
        Write-Line 'WARN' "$Domain 仍返回 $($r.StatusCode)——可能是 Cloudflare 缓存，或别处还跑着同一条隧道"
    } catch {
        Write-Line 'OK' "$Domain 已无后端响应（预期）"
    }
}

Write-Host ''
Write-Line 'OK' '本机已停止对外服务。代码仓库、虚拟环境、data 目录均原样保留。'
Write-Line 'INFO' '接下来在部署机上按迁移说明的第 2–6 步操作。'
exit 0
