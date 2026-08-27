<#
.SYNOPSIS
    引途医疗专利智能体 — 部署端一键同步更新（失败自动回滚）。

.DESCRIPTION
    维护端把代码推到 GitHub，部署端跑这个脚本把线上更新到最新：

        停看门狗 → 备份数据库 → 停应用 → git pull → 按需装依赖 → 构建前端
        → 起应用 → 健康检查 → 起看门狗

    任何一步失败，自动回滚到更新前的 commit、恢复数据库、重建并把服务拉回来，
    然后以非零退出码结束。也就是说：脚本要么让你用上新版本，要么让你回到
    动手之前的状态，不会停在中间。

    几个不显眼但要命的点，脚本已经处理：
      * 看门狗必须先停。它每 30 秒探一次健康，更新期间应用是停的，它会抢着
        用旧代码把进程拉起来，于是端口被占、新旧代码混跑。
      * 数据库不能直接拷。WAL 模式下 app.db 之外还有 -wal/-shm，冷拷会拿到
        不一致的快照，必须走 sqlite3 的 backup API。
      * git clean 绝不能带 -x。data/ 在 .gitignore 里，-x 会连用户的案件、
        上传的材料、API Key 一起删掉。
      * 依赖只在 pyproject.toml / package-lock.json 真的变了才重装，
        否则每次更新白等几分钟。

.PARAMETER Port
    应用端口，默认 8000。需与部署时一致。

.PARAMETER Branch
    跟踪的分支，默认 main。

.PARAMETER TunnelConfig
    cloudflared 配置文件路径。给了就一并把隧道纳入看门狗守护。

.PARAMETER Force
    工作区有未提交改动时也继续（改动会被 stash 保留，不会丢）。
    默认遇到本地改动直接停——部署端不该有本地改动，有就说明出了别的事。

.PARAMETER CheckOnly
    只检查有没有新提交并打印变更摘要，不做任何改动。

.PARAMETER SkipBackup
    跳过数据库备份。仅用于确认无数据的全新环境，日常绝不要用。

.EXAMPLE
    .\deploy\update.ps1
    检查并更新到最新，失败自动回滚。

.EXAMPLE
    .\deploy\update.ps1 -CheckOnly
    只看有什么更新，不动线上。

.NOTES
    首次部署见 docs\DEPLOYMENT.md 与 docs\DEPLOY_CLOUDFLARE.md。
    双机协作规则见 docs\SYNC_PROTOCOL.md。
#>
[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]    $Port          = 8000,
    [string] $Branch        = 'main',
    [string] $TunnelConfig  = '',
    [switch] $Force,
    [switch] $CheckOnly,
    [switch] $SkipBackup
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# --- 路径 -------------------------------------------------------------------
$Root       = Split-Path -Parent $PSScriptRoot
$Backend    = Join-Path $Root 'backend'
$Frontend   = Join-Path $Root 'frontend'
$DataDir    = Join-Path $Root 'data'
$BackupDir  = Join-Path $DataDir 'backups'
$LogFile    = Join-Path $DataDir 'update.log'
$Py         = Join-Path $Backend '.venv\Scripts\python.exe'
$HealthUrl  = "http://127.0.0.1:$Port/api/v1/system/health"
$WatchdogPs = Join-Path $Root 'watchdog.ps1'

$script:StashRef = $null   # 有本地改动且 -Force 时记下 stash，回滚时还原

# --- 日志 -------------------------------------------------------------------
function Write-Log {
    param([string] $Level, [string] $Message)
    $line = '{0} [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    $color = switch ($Level) {
        'OK'   { 'Green' }
        'WARN' { 'Yellow' }
        'ERR'  { 'Red' }
        'STEP' { 'Cyan' }
        default { 'Gray' }
    }
    Write-Host $line -ForegroundColor $color
    try {
        if (-not (Test-Path $DataDir)) { New-Item -ItemType Directory -Force $DataDir | Out-Null }
        Add-Content -Path $LogFile -Value $line -Encoding utf8
    } catch { }   # 日志写不了不该拖垮更新本身
}

function Invoke-Step {
    <# 跑一条外部命令，失败即抛出，输出并入日志 #>
    param([string] $What, [scriptblock] $Body)
    Write-Log 'STEP' $What
    $out = & $Body 2>&1
    if ($LASTEXITCODE -ne 0) {
        $tail = ($out | Select-Object -Last 12) -join "`n"
        throw "$What 失败（exit=$LASTEXITCODE）：`n$tail"
    }
    return $out
}

# --- 进程控制 ---------------------------------------------------------------
function Get-AppProcesses {
    <# 只认本仓库 .venv 起的 uvicorn，避免误杀同机其它 Python 服务 #>
    $venvPy = $Py -replace '\\', '\\'
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine -like "*$venvPy*" -and
            $_.CommandLine -like '*uvicorn*'
        }
}

function Get-WatchdogProcesses {
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue -Filter "Name='powershell.exe' OR Name='pwsh.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -like '*watchdog.ps1*' }
}

function Stop-Watchdog {
    $procs = @(Get-WatchdogProcesses)
    if ($procs.Count -eq 0) { Write-Log 'INFO' '看门狗未运行'; return $false }
    foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-Log 'OK' "已停看门狗（$($procs.Count) 个进程）"
    return $true
}

function Start-Watchdog {
    if (-not (Test-Path $WatchdogPs)) { Write-Log 'WARN' '找不到 watchdog.ps1，跳过'; return }
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $WatchdogPs, '-Port', $Port)
    if ($TunnelConfig) { $argList += @('-TunnelConfig', $TunnelConfig) }
    Start-Process -FilePath 'powershell.exe' -ArgumentList $argList -WindowStyle Hidden
    Write-Log 'OK' '看门狗已启动'
}

function Stop-App {
    $procs = @(Get-AppProcesses)
    if ($procs.Count -eq 0) { Write-Log 'INFO' '应用未运行'; return }
    foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    # 等端口真正释放，否则新进程会撞上 address in use
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 500
        if (@(Get-AppProcesses).Count -eq 0) { break }
    }
    Write-Log 'OK' "已停应用（$($procs.Count) 个进程）"
}

function Start-App {
    $argList = @(
        '-m', 'uvicorn', 'app.main:app',
        '--host', '127.0.0.1', '--port', $Port,
        '--proxy-headers', '--forwarded-allow-ips', '127.0.0.1'
    )
    Start-Process -FilePath $Py -ArgumentList $argList -WorkingDirectory $Backend -WindowStyle Hidden
    Write-Log 'INFO' '应用启动中...'
}

function Test-Healthy {
    <# 返回 revision 字符串代表健康；$null 代表不健康 #>
    param([int] $TimeoutSeconds = 90)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 5 -ErrorAction Stop
            if ($r.ok) { return [string] $r.revision }
        } catch { }
        Start-Sleep -Seconds 2
    }
    return $null
}

# --- 数据库备份 -------------------------------------------------------------
function Backup-Database {
    <# WAL 模式下必须走 sqlite backup API，冷拷 app.db 会得到不一致快照 #>
    $db = Join-Path $DataDir 'app.db'
    if (-not (Test-Path $db)) { Write-Log 'INFO' '尚无数据库，跳过备份'; return $null }
    if (-not (Test-Path $BackupDir)) { New-Item -ItemType Directory -Force $BackupDir | Out-Null }
    $stamp  = Get-Date -Format 'yyyyMMdd-HHmmss'
    $target = Join-Path $BackupDir "app-$stamp.db"

    $code = @"
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close(); src.close()
"@
    $tmp = Join-Path $env:TEMP "pa-backup-$stamp.py"
    Set-Content -Path $tmp -Value $code -Encoding utf8
    try {
        & $Py $tmp $db $target
        if ($LASTEXITCODE -ne 0) { throw "sqlite backup 退出码 $LASTEXITCODE" }
    } finally {
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    }
    $sizeMb = [math]::Round((Get-Item $target).Length / 1MB, 1)
    Write-Log 'OK' "数据库已备份：$([IO.Path]::GetFileName($target))（$sizeMb MB）"

    # 只留最近 10 份，避免长期占盘
    Get-ChildItem $BackupDir -Filter 'app-*.db' |
        Sort-Object LastWriteTime -Descending |
        Select-Object -Skip 10 |
        Remove-Item -Force -ErrorAction SilentlyContinue
    return $target
}

function Restore-Database {
    param([string] $BackupPath)
    if (-not $BackupPath -or -not (Test-Path $BackupPath)) {
        Write-Log 'WARN' '没有可用备份，数据库保持现状'
        return
    }
    $db = Join-Path $DataDir 'app.db'
    # 一并清掉 WAL 边车文件，否则旧 WAL 会盖回刚恢复的主库
    foreach ($sfx in @('', '-wal', '-shm')) {
        Remove-Item "$db$sfx" -Force -ErrorAction SilentlyContinue
    }
    Copy-Item $BackupPath $db -Force
    Write-Log 'OK' "已从备份恢复数据库：$([IO.Path]::GetFileName($BackupPath))"
}

# --- 构建 -------------------------------------------------------------------
function Update-Dependencies {
    <# 只在依赖清单真的变了才重装：整装一次要几分钟，每次更新都装纯属浪费 #>
    param([string] $FromCommit, [string] $ToCommit)
    $changed = @(& git -C $Root diff --name-only "$FromCommit..$ToCommit" 2>$null)

    if ($changed -contains 'backend/pyproject.toml') {
        Invoke-Step '后端依赖有变更，重新安装' { & $Py -m pip install -e $Backend --quiet }
    } else {
        Write-Log 'INFO' '后端依赖未变，跳过安装'
    }

    if ($changed -contains 'frontend/package-lock.json' -or $changed -contains 'frontend/package.json') {
        Invoke-Step '前端依赖有变更，npm ci' { & npm ci --prefix $Frontend --silent }
    } else {
        Write-Log 'INFO' '前端依赖未变，跳过 npm ci'
    }
}

function Build-Frontend {
    # dist/ 不入库，每次更新都必须重建，否则页面还是旧的
    Invoke-Step '构建前端' { & npm run build --prefix $Frontend }
}

# --- 主流程 -----------------------------------------------------------------
Write-Log 'STEP' "=== 同步更新开始（分支 $Branch，端口 $Port）==="

# 0. 前置检查
if (-not (Test-Path (Join-Path $Root '.git'))) { throw "不是 git 仓库：$Root。请按 docs\DEPLOYMENT.md 用 git clone 部署。" }
if (-not (Test-Path $Py)) { throw "找不到虚拟环境 $Py。请先跑 .\start.ps1 -SetupOnly 完成安装。" }

$dirty = @(& git -C $Root status --porcelain)
if ($dirty.Count -gt 0) {
    if (-not $Force) {
        Write-Log 'ERR' "工作区有 $($dirty.Count) 处未提交改动，已中止："
        $dirty | Select-Object -First 10 | ForEach-Object { Write-Log 'ERR' "  $_" }
        Write-Log 'ERR' '部署端不应有本地改动。确认无误后加 -Force 继续（改动会被 stash 保留）。'
        exit 2
    }
    Write-Log 'WARN' "工作区有 $($dirty.Count) 处改动，先 stash 保留"
    & git -C $Root stash push -u -m "update.ps1 自动保留 $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" | Out-Null
    $script:StashRef = 'stash@{0}'
}

# 1. 看看有没有更新
Invoke-Step '拉取远端信息' { & git -C $Root fetch origin $Branch --quiet }
$oldCommit = (& git -C $Root rev-parse HEAD).Trim()
$newCommit = (& git -C $Root rev-parse "origin/$Branch").Trim()

if ($oldCommit -eq $newCommit) {
    Write-Log 'OK' "已是最新（$($oldCommit.Substring(0,7))），无需更新"
    if ($script:StashRef) { & git -C $Root stash pop | Out-Null }
    exit 0
}

$log = @(& git -C $Root log --oneline --no-decorate "$oldCommit..$newCommit")
Write-Log 'INFO' "发现 $($log.Count) 个新提交：$($oldCommit.Substring(0,7)) → $($newCommit.Substring(0,7))"
$log | ForEach-Object { Write-Log 'INFO' "  $_" }

if ($CheckOnly) {
    Write-Log 'OK' '仅检查模式，未做任何改动'
    if ($script:StashRef) { & git -C $Root stash pop | Out-Null }
    exit 0
}

# 2. 更新（失败即回滚）
$watchdogWasRunning = Stop-Watchdog
$backup = $null
$rolledBack = $false

try {
    if (-not $SkipBackup) { $backup = Backup-Database }

    Stop-App
    # --ff-only：部署端永远是远端的镜像，出现分叉说明有人在这台机器上提交过，
    # 那种情况必须人来看，不能让脚本 merge 出一个四不像。
    Invoke-Step "更新代码到 $($newCommit.Substring(0,7))" { & git -C $Root merge --ff-only $newCommit }

    Update-Dependencies -FromCommit $oldCommit -ToCommit $newCommit
    Build-Frontend

    Start-App
    $rev = Test-Healthy -TimeoutSeconds 90
    if (-not $rev) { throw '应用启动后 90 秒内未通过健康检查' }

    $expect = $newCommit.Substring(0, 7)
    if ($rev -ne $expect) {
        Write-Log 'WARN' "健康检查报告 revision=$rev，期望 $expect（可能存在残留旧进程）"
    }

    Write-Log 'OK' "=== 更新成功：$($oldCommit.Substring(0,7)) → $expect（revision=$rev）==="
}
catch {
    Write-Log 'ERR' "更新失败：$($_.Exception.Message)"
    Write-Log 'STEP' '开始回滚...'
    $rolledBack = $true
    try {
        Stop-App
        & git -C $Root reset --hard $oldCommit | Out-Null
        Write-Log 'OK' "代码已回退到 $($oldCommit.Substring(0,7))"

        if ($backup) { Restore-Database -BackupPath $backup }

        # 回退后依赖与前端产物都得跟着回到旧版本
        Update-Dependencies -FromCommit $newCommit -ToCommit $oldCommit
        Build-Frontend
        Start-App

        $rev = Test-Healthy -TimeoutSeconds 90
        if ($rev) {
            Write-Log 'OK' "=== 已回滚并恢复服务（revision=$rev）==="
        } else {
            Write-Log 'ERR' '=== 回滚后服务仍不健康，需要人工介入 ==='
            Write-Log 'ERR' "  代码在 $($oldCommit.Substring(0,7))；备份：$backup"
        }
    }
    catch {
        Write-Log 'ERR' "回滚过程也失败了：$($_.Exception.Message)"
        Write-Log 'ERR' "  请人工检查。更新前 commit=$oldCommit，数据库备份=$backup"
    }
}
finally {
    if ($script:StashRef) {
        try { & git -C $Root stash pop | Out-Null; Write-Log 'INFO' '已恢复 stash 的本地改动' }
        catch { Write-Log 'WARN' "stash 未能自动恢复，用 git stash list 查看" }
    }
    if ($watchdogWasRunning) { Start-Watchdog }
}

if ($rolledBack) { exit 1 }
exit 0
