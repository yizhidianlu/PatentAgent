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
# 更新日志记的是「这份代码副本的更新历史」，固定放仓库内，与业务数据无关。
# 数据库备份则必须跟数据库同源，位置由下面的 Resolve-DataDir 决定。
$LogDir     = Join-Path $Root 'data'
$LogFile    = Join-Path $LogDir 'update.log'
$DataDir    = $null   # 主流程开始时解析
$BackupDir  = $null
$Py         = Join-Path $Backend '.venv\Scripts\python.exe'
$HealthUrl  = "http://127.0.0.1:$Port/api/v1/system/health"
$WatchdogPs = Join-Path $Root 'watchdog.ps1'
$PidFile    = Join-Path $LogDir 'watchdog.pid'

$script:StashRef = $null   # 有本地改动且 -Force 时记下 stash，回滚时还原

# --- 日志 -------------------------------------------------------------------
function Write-Log {
    param([string] $Level, [string] $Message)
    # 错误摘要常是多行；逐行加前缀，否则日志里挤成超长的一行没法看
    if ($Message -match "[`r`n]") {
        foreach ($one in ($Message -split "`r?`n")) { Write-Log $Level $one }
        return
    }
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
        if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Force $LogDir | Out-Null }
        Add-Content -Path $LogFile -Value $line -Encoding utf8
    } catch { }   # 日志写不了不该拖垮更新本身
}

function Invoke-Step {
    <# 跑一条外部命令，只以退出码判定成败，输出并入日志。

       这里必须临时把 ErrorActionPreference 放回 Continue。PowerShell 5.1 下
       `2>&1` 会把原生命令写到 stderr 的每一行包装成 ErrorRecord，在 Stop 模式
       下当场变成终止错误——哪怕命令退出码是 0。npm/vite 把「chunk 体积偏大」
       这类警告写在 stderr，构建明明成功也会被判成失败，进而触发一次没必要的
       回滚。判定成败只看 $LASTEXITCODE。
    #>
    param([string] $What, [scriptblock] $Body)
    Write-Log 'STEP' $What
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $global:LASTEXITCODE = 0
        $out = & $Body 2>&1 | ForEach-Object { "$_" }
    } finally {
        $ErrorActionPreference = $prev
    }
    if ($LASTEXITCODE -ne 0) {
        throw "$What 失败（exit=$LASTEXITCODE）：`n$(Format-Tail $out)"
    }
    return $out
}

function Format-Tail {
    <# 取输出末尾若干行做错误摘要；剥掉 ANSI 色码，否则日志里全是乱码转义序列 #>
    param($Lines, [int] $Count = 12)
    $esc = [char] 27
    return (@($Lines) | Select-Object -Last $Count |
        ForEach-Object { $_ -replace "$esc\[[0-9;]*[a-zA-Z]", '' }) -join "`n"
}

# --- 进程控制 ---------------------------------------------------------------
function Get-AppProcesses {
    <# 找出「我们这一份部署」的进程，返回 PID 数组。

       主判据是谁在监听本部署的端口——这是唯一不会误伤的判据。同一台机器上
       很可能还跑着别的 uvicorn，而且它们的命令行同样是 `app.main:app`
       （不同项目重名很常见），只靠命令行匹配会连别人的服务一起杀掉。

       再补一条命令行判据兜底：进程已崩到不再监听、或刚起还没绑上端口时，
       仅凭端口就找不到它，会留下抢占端口的僵尸。这条限定在本仓库自己的
       .venv 解释器上，不会波及外部项目。
    #>
    $pids = [System.Collections.Generic.HashSet[int]]::new()
    try {
        Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            ForEach-Object { [void] $pids.Add([int] $_.OwningProcess) }
    } catch { }
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*$Py*" -and $_.CommandLine -like '*uvicorn*' } |
        ForEach-Object { [void] $pids.Add([int] $_.ProcessId) }
    return @($pids)
}

function Get-WatchdogProcesses {
    <# 找出守护「本仓库」的看门狗，返回 PID 数组。

       以 watchdog.ps1 启动时写下的 data\watchdog.pid 为准。命令行匹配在这里
       两头不讨好：宽匹配（*watchdog.ps1*）会连同机另一份部署的看门狗一起停掉；
       严匹配（绝对路径）又认不出相对路径启动的实例——而文档里给的正是
       `watchdog.ps1 -Port 8000` 这种写法，Windows 保存的是原始命令行、不展开
       相对路径，于是必然失配。失配的后果不是「少停一个进程」这么轻：
       看门狗会在依赖安装/前端构建进行中把旧代码拉起来占住端口，
       之后的健康检查打在它身上依然返回 200，整个更新以假成功收场。

       pid 文件只是线索，不是凭据：进程被 Force kill 时 finally 不执行，
       文件会残留。所以逐条核对该 PID 是否还活着、且确实是个 watchdog 进程。
    #>
    $pids = [System.Collections.Generic.HashSet[int]]::new()

    if (Test-Path $PidFile) {
        $rec = @{}
        foreach ($line in (Get-Content $PidFile -ErrorAction SilentlyContinue)) {
            if ($line -match '^\s*([a-z]+)\s*=\s*(.+?)\s*$') { $rec[$Matches[1]] = $Matches[2] }
        }
        # pid 文件必须是本仓库写的；别人的仓库写的不算数
        if ($rec['root'] -eq $Root -and $rec['pid'] -match '^\d+$') {
            $candidate = [int] $rec['pid']
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$candidate" -ErrorAction SilentlyContinue
            if ($proc -and $proc.CommandLine -and $proc.CommandLine -like '*watchdog.ps1*') {
                $confirmed = $false
                if ($proc.CommandLine -like "*$WatchdogPs*") {
                    # 命令行里就是本仓库的脚本路径，直接坐实
                    $confirmed = $true
                } elseif ($rec['started']) {
                    # 相对路径启动时命令行看不出仓库，改用启动时间对齐。
                    # 这一步是防 PID 复用：本仓库的看门狗被 Force kill 后 pid 文件会残留
                    # （finally 不执行），那个 PID 可能已被别的进程占用。同机就有一个
                    # 真实例子——PB_System 的计划任务 PB_Watchdog 跑的是
                    # C:\PB_watchdog\watchdog.ps1，命令行同样含 "watchdog.ps1"。
                    # 认错人就会把别的项目的守护进程杀掉，而且两边日志都不会提这件事。
                    $started = [datetime]::MinValue
                    if ([datetime]::TryParse($rec['started'], [ref] $started)) {
                        $confirmed = ([math]::Abs((($proc.CreationDate) - $started).TotalSeconds) -le 120)
                    }
                }
                if ($confirmed) {
                    [void] $pids.Add($candidate)
                } else {
                    Write-Log 'WARN' "pid 文件记录的 $candidate 与实际进程对不上（可能是残留记录，PID 已被复用），已忽略"
                }
            }
        }
    }

    # 兜底：pid 文件不存在（老版本看门狗、或写文件失败）时退回命令行匹配，
    # 但**必须**限定在本仓库的绝对路径上。这里绝不能放宽成 *watchdog.ps1*：
    # 部署机上就有一个叫 C:\PB_watchdog\watchdog.ps1 的守护进程（另一个项目的
    # 计划任务），宽匹配会把它一起 Force kill，它要等下一个触发周期才回来，
    # 期间那个项目无人守护，且两边日志都不会提到这件事。
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue -Filter "Name='powershell.exe' OR Name='pwsh.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*$WatchdogPs*" } |
        ForEach-Object { [void] $pids.Add([int] $_.ProcessId) }

    return @($pids)
}

function Stop-Watchdog {
    $pids = @(Get-WatchdogProcesses)
    if ($pids.Count -eq 0) {
        # 更新期间应用会被停掉几分钟。若此刻其实有个看门狗在跑而我们没认出来，
        # 它会在构建过程中把旧代码拉起来占住端口——这正是本脚本要防的事，
        # 所以「没找到」值得一句 WARN，而不是淹没在 INFO 里。
        Write-Log 'WARN' '未发现本仓库的看门狗；若确有守护进程在跑，请先手动停止再更新'
        return $false
    }
    foreach ($processId in $pids) { Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 1
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    Write-Log 'OK' "已停看门狗（$($pids.Count) 个进程）"
    return $true
}

function Start-Watchdog {
    if (-not (Test-Path $WatchdogPs)) { Write-Log 'WARN' '找不到 watchdog.ps1，跳过'; return }
    # watchdog.ps1 只认 -IntervalSeconds / -Port / -Once。多传一个它不认识的参数，
    # PowerShell 会直接报错退出，服务就此失去守护且没有任何提示。
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $WatchdogPs, '-Port', $Port)
    Start-Process -FilePath 'powershell.exe' -ArgumentList $argList -WindowStyle Hidden
    Start-Sleep -Seconds 3
    if (@(Get-WatchdogProcesses).Count -eq 0) {
        Write-Log 'ERR' '看门狗启动后立即退出，服务当前无人守护——请手动检查 watchdog.ps1'
    } else {
        Write-Log 'OK' '看门狗已启动'
    }
}

function Stop-App {
    $pids = @(Get-AppProcesses)
    if ($pids.Count -eq 0) { Write-Log 'INFO' '应用未运行'; return }
    foreach ($processId in $pids) { Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue }
    # 等端口真正释放，否则新进程会撞上 address in use
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 500
        if (@(Get-AppProcesses).Count -eq 0) { break }
    }
    $left = @(Get-AppProcesses).Count
    if ($left -gt 0) { throw "停应用后仍有 $left 个进程占着端口 $Port" }
    Write-Log 'OK' "已停应用（$($pids.Count) 个进程）"
}

function Start-App {
    <# 起应用并记下 PID，供随后核对端口属主。

       Start-Process 是 fire-and-forget：端口已被别人占住时，新进程撞
       WinError 10048 立刻退出，而这里不会有任何异常。若此刻端口上蹲着的是
       看门狗用旧代码拉起来的进程，后续健康检查照样返回 200、revision 读的是
       .git（已 merge 完），比对也能过——更新于是以「成功」收场，实际跑的还是旧代码。
       所以必须回头确认：端口属主到底是不是我刚起的这个。
    #>
    $argList = @(
        '-m', 'uvicorn', 'app.main:app',
        '--host', '127.0.0.1', '--port', $Port,
        '--proxy-headers', '--forwarded-allow-ips', '127.0.0.1'
    )
    $proc = Start-Process -FilePath $Py -ArgumentList $argList -WorkingDirectory $Backend `
                          -WindowStyle Hidden -PassThru
    Write-Log 'INFO' "应用启动中（pid=$($proc.Id)）..."
    return $proc.Id
}

function Assert-PortOwnedBy {
    <# 端口的监听者必须是我们刚起的那个进程，否则说明端口被别人占着。 #>
    param([int] $ExpectedPid)
    $owners = @()
    try {
        $owners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique)
    } catch { }
    if ($owners.Count -eq 0) {
        # 拿不到属主信息（cmdlet 不可用等）时不阻断更新，健康检查仍是主判据
        Write-Log 'WARN' "无法确认端口 $Port 的属主进程，跳过归属校验"
        return
    }
    if ($owners -notcontains $ExpectedPid) {
        throw ("端口 $Port 的监听者是 pid=$($owners -join ',')，而本次启动的是 pid=$ExpectedPid。" +
               '通常意味着有看门狗或残留进程抢先占用了端口，当前服务跑的不是刚更新的代码。')
    }
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

# --- 数据目录与备份 ---------------------------------------------------------
function Resolve-DataDir {
    <# 问应用自己要数据目录，而不是在这里重新解析一遍 .env。

       DEPLOYMENT.md §2.2 建议把 DATA_DIR 指到数据盘（如 D:\PatentAgentData），
       照做之后仓库内的 data\ 就是个空壳。若这里仍按仓库内路径去备份，
       Backup-Database 会发现「库不存在」而跳过——于是「失败自动回滚会恢复数据库」
       这条保证在全新部署上静默失效，且看日志一切正常。

       DATA_DIR 可来自环境变量或 .env，还涉及引号、相对路径、大小写等细节。
       与其在 PowerShell 里复刻一份必然有偏差的解析，不如直接让应用告诉我们。
    #>
    $code = 'import sys; sys.path.insert(0, r"' + $Backend + '"); from app.config import get_config; print(get_config().data_dir)'
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $global:LASTEXITCODE = 0
        $out = & $Py -c $code 2>$null
    } catch {
        $out = $null
    } finally {
        $ErrorActionPreference = $prev
    }
    if ($LASTEXITCODE -eq 0 -and $out) {
        $p = "$(@($out) | Select-Object -Last 1)".Trim()
        if ($p -and (Test-Path $p)) { return $p }
        if ($p) {
            Write-Log 'WARN' "应用配置的 DATA_DIR 指向 $p，但该目录不存在"
            return $p
        }
    }
    Write-Log 'WARN' "读不到应用配置的 DATA_DIR，回退到仓库内 data\；若 .env 指定了别的位置，本次备份与回滚将不起作用"
    return (Join-Path $Root 'data')
}

function Backup-Database {
    <# WAL 模式下必须走 sqlite backup API，冷拷 app.db 会得到不一致快照 #>
    $db = Join-Path $DataDir 'app.db'
    if (-not (Test-Path $db)) {
        # 全新部署确实还没有库；但更常见的是 DATA_DIR 配错、指到了空目录。
        # 这时没有备份可回滚，必须让人看见，不能用 INFO 混在正常输出里。
        Write-Log 'WARN' "$DataDir 下没有 app.db，本次更新没有可回滚的数据库备份"
        return $null
    }
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

# 数据目录取自应用配置（可能被 .env 的 DATA_DIR 指到数据盘）。
# 明确打进日志：备份到底落在哪、回滚能不能恢复，看这一行就知道。
$DataDir   = Resolve-DataDir
$BackupDir = Join-Path $DataDir 'backups'
Write-Log 'INFO' "数据目录：$DataDir"

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

    $appPid = Start-App
    $rev = Test-Healthy -TimeoutSeconds 90
    if (-not $rev) { throw '应用启动后 90 秒内未通过健康检查' }
    # 健康检查过了不等于「跑的是我起的那个进程」——先确认端口归属再下结论
    Assert-PortOwnedBy -ExpectedPid $appPid

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
        $appPid = Start-App

        $rev = Test-Healthy -TimeoutSeconds 90
        if ($rev) {
            Assert-PortOwnedBy -ExpectedPid $appPid
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
