<#
.SYNOPSIS
    引途医疗专利智能体 — 服务自愈守护进程。

.DESCRIPTION
    每 30 秒体检一次，异常自动拉起，无需人工干预：
      * 应用（uvicorn:8000）—— 探 /api/v1/system/health；进程在但端口不响应（僵死）同样重启
      * 隧道（cloudflared）—— 进程存活 + 边缘连接数（metrics）
    连续 2 次失败才动手，避免瞬时抖动导致误重启。日志写 data\watchdog.log（超 5MB 轮转）。

.EXAMPLE
    .\watchdog.ps1          # 前台常驻，Ctrl+C 停止
.EXAMPLE
    .\watchdog.ps1 -Once    # 只体检一次（排障用）

.NOTES
    存在的原因：本机 Clash Verge 的 TUN 模式会干扰 cloudflared 与 Cloudflare 边缘之间的
    QUIC 连接，导致隧道间歇掉线。根治见 docs/DEPLOY_CLOUDFLARE.md，在那之前由本脚本兜底。
#>
[CmdletBinding()]
param(
    [int]    $IntervalSeconds = 30,
    [int]    $Port            = 8000,
    [switch] $Once
)

$ErrorActionPreference = 'Continue'
$env:PYTHONIOENCODING = 'utf-8'
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }

$Root       = $PSScriptRoot
$Backend    = Join-Path $Root 'backend'
$Py         = Join-Path $Backend '.venv\Scripts\python.exe'
$TunnelConf = Join-Path $env:USERPROFILE '.cloudflared\yintu-patent.yml'
$LogFile    = Join-Path $Root 'data\watchdog.log'
$HealthUrl  = "http://127.0.0.1:$Port/api/v1/system/health"

$script:AppFails    = 0
$script:TunnelFails = 0

function Write-Log {
    param([string] $Level, [string] $Message)
    $line = '{0} [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    $color = 'Gray'
    if ($Level -eq 'OK')   { $color = 'Green' }
    if ($Level -eq 'WARN') { $color = 'Yellow' }
    if ($Level -eq 'FIX')  { $color = 'Cyan' }
    if ($Level -eq 'ERR')  { $color = 'Red' }
    Write-Host $line -ForegroundColor $color
    try {
        $dir = Split-Path $LogFile -Parent
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
        if ((Test-Path $LogFile) -and ((Get-Item $LogFile).Length -gt 5MB)) {
            Move-Item $LogFile "$LogFile.1" -Force -ErrorAction SilentlyContinue
        }
        Add-Content -Path $LogFile -Value $line -Encoding utf8
    } catch { }
}

function Get-AppProcess {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*uvicorn app.main*' }
}

function Get-TunnelProcess {
    Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*yintu-patent*' }
}

function Test-AppHealthy {
    try {
        $r = Invoke-WebRequest -Uri $HealthUrl -TimeoutSec 8 -UseBasicParsing -ErrorAction Stop
        if ($r.StatusCode -eq 200) { return $true }
        return $false
    } catch {
        return $false
    }
}

function Test-TunnelHealthy {
    if (-not (Get-TunnelProcess)) { return $false }
    try {
        $m = Invoke-WebRequest -Uri 'http://127.0.0.1:20242/metrics' -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
        foreach ($l in ($m.Content -split "`n")) {
            if ($l -match '^cloudflared_tunnel_ha_connections\s+(\d+)') {
                if ([int]$Matches[1] -lt 1) { return $false }
            }
        }
    } catch { }
    return $true
}

function Restart-App {
    Write-Log 'FIX' '重启应用...'
    Get-AppProcess | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 3
    $argList = @('-m','uvicorn','app.main:app','--host','127.0.0.1','--port',"$Port",'--proxy-headers','--forwarded-allow-ips','127.0.0.1')
    Start-Process -FilePath $Py -ArgumentList $argList -WorkingDirectory $Backend -WindowStyle Hidden
    Start-Sleep -Seconds 10
    if (Test-AppHealthy) { Write-Log 'OK' '应用已恢复' } else { Write-Log 'ERR' '应用重启后仍不健康' }
    $script:AppFails = 0
}

function Restart-Tunnel {
    Write-Log 'FIX' '重启隧道...'
    Get-TunnelProcess | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 3
    $argList = @('tunnel','--config',$TunnelConf,'run')
    Start-Process -FilePath 'cloudflared' -ArgumentList $argList -WindowStyle Hidden
    Start-Sleep -Seconds 12
    if (Test-TunnelHealthy) { Write-Log 'OK' '隧道已恢复' } else { Write-Log 'ERR' '隧道重启后仍不健康' }
    $script:TunnelFails = 0
}

function Invoke-Check {
    $appOk = Test-AppHealthy
    if ($appOk) {
        if ($script:AppFails -gt 0) { Write-Log 'OK' '应用自行恢复' }
        $script:AppFails = 0
    } else {
        $script:AppFails = $script:AppFails + 1
        Write-Log 'WARN' "应用体检失败（连续 $script:AppFails 次）"
        if ($script:AppFails -ge 2) { Restart-App }
    }

    $tunOk = Test-TunnelHealthy
    if ($tunOk) {
        if ($script:TunnelFails -gt 0) { Write-Log 'OK' '隧道自行恢复' }
        $script:TunnelFails = 0
    } else {
        $script:TunnelFails = $script:TunnelFails + 1
        Write-Log 'WARN' "隧道体检失败（连续 $script:TunnelFails 次）"
        if ($script:TunnelFails -ge 2) { Restart-Tunnel }
    }

    if ($appOk -and $tunOk) { return $true }
    return $false
}

Write-Log 'INFO' "守护进程启动（每 $IntervalSeconds 秒体检，端口 $Port）"

if ($Once) {
    $ok = Invoke-Check
    if ($ok) {
        Write-Log 'INFO' '单次体检结果：全部正常'
        exit 0
    }
    Write-Log 'INFO' '单次体检结果：存在异常'
    exit 1
}

while ($true) {
    try {
        [void](Invoke-Check)
    } catch {
        Write-Log 'ERR' ('体检异常：' + $_.Exception.Message)
    }
    Start-Sleep -Seconds $IntervalSeconds
}
