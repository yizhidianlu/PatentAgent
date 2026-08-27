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

.PARAMETER NoRollback
    失败时不回滚，保持现场。用于「已经确认失败来自本脚本自身的误报」这类情况——
    自动回滚在那时只会把刚拉来的修复一起还原掉，反而挡住自愈。
    用完请自行确认服务状态。

.PARAMETER NoSelfHeal
    回滚时连 deploy\ 也一并回退（默认会把它留在新版，好让下次更新用上修好的脚本）。
    只在需要精确复现某个旧版本行为时使用。

.PARAMETER KeepBackupDays
    更新前数据库备份（pre-*.db）的保留天数，默认 30。
    按天而不是按份数：一天推五次的节奏下，按份数保留会在两天内把上周那个
    唯一正确的回退点挤掉，而那种更新恰恰最需要回退点。

.PARAMETER KeepBackupMin
    无论多旧都至少保留几份 pre-*.db，默认 10。磁盘再紧也不能连一步都退不回去。
    含数据库迁移的更新会另外固定一份 keep-*.db，永不自动清理。

.EXAMPLE
    .\deploy\update.ps1
    检查并更新到最新，失败自动回滚。

.EXAMPLE
    .\deploy\update.ps1 -CheckOnly
    只看有什么更新，不动线上。

.NOTES
    退出码：
      0  成功，或已是最新
      1  更新失败，已回滚到更新前的状态
      2  工作区有未提交改动，拒绝执行
      3  更新成功，但看门狗没能起回来（服务当前无人守护）
      4  连不上远端仓库，未做任何改动


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
    [switch] $SkipBackup,
    [switch] $NoRollback,
    [switch] $NoSelfHeal,
    [ValidateRange(1, 3650)]
    [int]    $KeepBackupDays = 30,
    [ValidateRange(1, 1000)]
    [int]    $KeepBackupMin  = 10
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
$script:SavedWatchdogArgs = @()   # 停看门狗前记下它的启动参数，重启时原样还原
$script:WatchdogFailed = $false   # 看门狗没能起回来 → 影响退出码，不能静默 exit 0

# PS 5.1 按控制台 OEM 代码页（简体中文机器上是 GBK）解码原生命令的输出，
# git log 里 UTF-8 的中文提交标题会就此损坏，写进更新日志就成了乱码——
# 而「这次更新改了什么」正是排障时最先要看的一行。
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }
$env:PYTHONIOENCODING = 'utf-8'

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

function Save-WatchdogArgs {
    <# 把 pid 文件里记的启动参数存下来，重启时原样还原 #>
    if (-not (Test-Path $PidFile)) { return }
    foreach ($line in (Get-Content $PidFile -ErrorAction SilentlyContinue)) {
        if ($line -match '^\s*args\s*=\s*(.+?)\s*$') {
            $script:SavedWatchdogArgs = @($Matches[1] -split '\|' | Where-Object { $_ -ne '' })
        }
    }
}

function Stop-Watchdog {
    Save-WatchdogArgs
    $pids = @(Get-WatchdogProcesses)
    if ($pids.Count -eq 0) {
        # 更新期间应用会被停掉几分钟。若此刻其实有个看门狗在跑而我们没认出来，
        # 它会在构建过程中把旧代码拉起来占住端口——这正是本脚本要防的事，
        # 所以「没找到」值得一句 WARN，而不是淹没在 INFO 里。
        # 别再建议「先手动停止再更新」——照做会导致更新后无人重启它。
        # 更新结束时无论如何都会拉起看门狗，这里只是如实记录当前没找到。
        Write-Log 'INFO' '更新前未发现本仓库的看门狗（更新完成后会拉起一个）'
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
    # 原样还原停掉之前的启动参数。只传 -Port 是不够的：
    # 隧道相关的 -NoTunnel / -TunnelName / -TunnelConfig / -TunnelMetricsPort 丢掉后，
    # 看门狗会静默回落到默认隧道形态——本机没有那个配置文件时它撞上 exit 2 当场自杀，
    # 于是每更新一次就把唯一的自愈守护永久删掉一次；配置文件恰好存在时更隐蔽，
    # 它会开始盯着一条错的隧道。参数由 watchdog.ps1 写在 pid 文件的 args 行里。
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $WatchdogPs)
    if ($script:SavedWatchdogArgs -and $script:SavedWatchdogArgs.Count -gt 0) {
        $argList += $script:SavedWatchdogArgs
        Write-Log 'INFO' "还原看门狗参数：$($script:SavedWatchdogArgs -join ' ')"
    } else {
        $argList += @('-Port', $Port)
        Write-Log 'WARN' "未记录到原启动参数，按 -Port $Port 启动；若原先用了 -NoTunnel/-TunnelName，请手动重启看门狗"
    }
    Start-Process -FilePath 'powershell.exe' -ArgumentList $argList -WindowStyle Hidden
    Start-Sleep -Seconds 4
    if (@(Get-WatchdogProcesses).Count -eq 0) {
        Write-Log 'ERR' '看门狗启动后立即退出，服务当前无人守护——请手动检查 watchdog.ps1'
        $script:WatchdogFailed = $true
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
    # 连创建时间一起记下：单看 PID 无法区分「还是那个进程」和「PID 被复用了」
    $ci = Get-CimInstance Win32_Process -Filter "ProcessId=$($proc.Id)" -ErrorAction SilentlyContinue
    Write-Log 'INFO' "应用启动中（pid=$($proc.Id)）..."
    return [pscustomobject]@{
        Id      = [int] $proc.Id
        Started = $(if ($ci) { $ci.CreationDate } else { $null })
    }
}

function Test-DescendantOf {
    <# 端口属主是否就是 $Expected 本身，或它的后代进程。

       Windows 上 <venv>\Scripts\python.exe 是个 launcher：它把真正的解释器作为
       **子进程**拉起，socket 归子进程持有。所以 Start-Process 返回的 PID 与端口
       属主 PID 永远不相等——这不是 conda 的问题，用 uv 建的 venv 同样如此，
       只要按 DEPLOYMENT.md 用 backend\.venv 部署就一定会遇上。
       （顺带：属主的 ExecutablePath 指向 venv 的 base 解释器、并不在仓库目录下，
       所以也不能靠「路径是否落在 .venv 里」来判定。）

       因此改为沿父进程链回溯。限深 6 层足够——实测只差 1 层，多留些余量以防
       将来的 launcher 实现多套一层。
    #>
    param([int] $OwnerPid, $Expected)
    $cur = $OwnerPid
    for ($depth = 0; $depth -lt 6; $depth++) {
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$cur" -ErrorAction SilentlyContinue
        if (-not $p) { return $false }
        if ([int] $p.ProcessId -eq $Expected.Id) {
            # 防 PID 复用：命中的必须是同一次启动
            if ($Expected.Started -and $p.CreationDate -and $p.CreationDate -ne $Expected.Started) {
                return $false
            }
            return $true
        }
        $cur = [int] $p.ParentProcessId
        if ($cur -le 0) { return $false }
    }
    return $false
}

function Assert-PortOwnedBy {
    <# 确认端口上跑的就是我们刚起的进程；对不上只告警，不中断。

       这个检查的价值是**留下线索**，不是否决更新。判据一旦误报，代价是把一次
       本来成功的更新回滚掉、再对着一个健康的服务喊「请人工介入」——比漏报严重得多。
       而真正的成败判据是健康检查加 revision 比对，那两条已经过了。

       所以这里一律 WARN。真出现「端口被别人占着」的情况，日志里有属主 PID 与
       可执行文件路径可查。
    #>
    param($Expected)
    $owners = @()
    try {
        $owners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique)
    } catch { }
    if ($owners.Count -eq 0) {
        Write-Log 'WARN' "无法确认端口 $Port 的属主进程，跳过归属校验"
        return
    }
    foreach ($ownerPid in $owners) {
        if (Test-DescendantOf -OwnerPid ([int] $ownerPid) -Expected $Expected) { return }
    }
    Write-Log 'WARN' "端口 $Port 的监听者是 pid=$($owners -join ',')，未能回溯到本次启动的 pid=$($Expected.Id)"
    foreach ($ownerPid in $owners) {
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$ownerPid" -ErrorAction SilentlyContinue
        if ($p) { Write-Log 'WARN' "  pid=$ownerPid  $($p.ExecutablePath)" }
    }
    Write-Log 'WARN' '  健康检查与 revision 已通过，更新继续；若服务行为异常请核对上面的进程'
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
    # 代码放在 deploy\_datadir.py 里，这里只传文件名。
    # 别改回 `python -c "..."`：PS 5.1 会吞掉内嵌双引号，r"C:\path" 到 Python 手里
    # 变成 rC:\path 直接 SyntaxError；换单引号又会撞上路径里的 \U 转义。两条路都堵死，
    # 而 2>$null 会把这个 SyntaxError 一并吞掉，只剩一句笼统的 WARN——
    # 于是函数每次都静默走兜底，看日志还以为一切正常。
    $script = Join-Path $PSScriptRoot '_datadir.py'
    if (-not (Test-Path $script)) {
        Write-Log 'WARN' "缺少 $script，无法确定数据目录"
        return (Join-Path $Root 'data')
    }
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $errText = ''
    try {
        $global:LASTEXITCODE = 0
        $raw = & $Py $script 2>&1 | ForEach-Object { "$_" }
        $out = @($raw | Where-Object { $_ -and $_.Trim() })
        if ($LASTEXITCODE -ne 0) { $errText = ($out | Select-Object -Last 3) -join '; ' }
    } catch {
        $out = $null
        $errText = $_.Exception.Message
    } finally {
        $ErrorActionPreference = $prev
    }
    if ($errText) { Write-Log 'WARN' "定位数据目录失败：$errText" }
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
    param([string] $FromCommit = '', [string] $ToCommit = '')

    $db = Join-Path $DataDir 'app.db'
    if (-not (Test-Path $db)) {
        # 全新部署确实还没有库；但更常见的是 DATA_DIR 配错、指到了空目录。
        # 这时没有备份可回滚，必须让人看见，不能用 INFO 混在正常输出里。
        Write-Log 'WARN' "$DataDir 下没有 app.db，本次更新没有可回滚的数据库备份"
        return $null
    }
    if (-not (Test-Path $BackupDir)) { New-Item -ItemType Directory -Force $BackupDir | Out-Null }
    $stamp  = Get-Date -Format 'yyyyMMdd-HHmmss'
    # 名字里带「退回哪个 commit」。旧命名是纯时间戳，真要回滚时只能靠日志去对
    # 时间——而日志恰恰是出事时最不容易拿到的东西。
    $rev    = if ($FromCommit) { $FromCommit.Substring(0, 7) } else { 'unknown' }
    $target = Join-Path $BackupDir "pre-$rev-$stamp.db"

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

    # 改数据库结构的更新，回退点必须长期留着。
    # 迁移是**代码回滚救不回来**的那一类改动：退回旧代码，库已经是新结构了。
    if ($FromCommit -and $ToCommit) {
        $touched = @(& git -C $Root diff --name-only "$FromCommit..$ToCommit" 2>$null)
        if ($touched | Where-Object { $_ -like '*app/db/migrations/*' }) {
            $pinned = Join-Path $BackupDir "keep-$rev-$stamp.db"
            Copy-Item $target $pinned -Force -ErrorAction SilentlyContinue
            Write-Log 'WARN' "本次更新含数据库迁移，已固定回退点：$([IO.Path]::GetFileName($pinned))"
            Write-Log 'WARN' '  keep-*.db 永不自动清理；回退到该 commit 之前时必须连库一起回。'
        }
    }

    # 保留策略：按**天**而不是按份数。
    # 按份数（原来是 10 份）在快节奏更新下会失效——一天推五次，两天就把上周
    # 那个唯一正确的回退点挤掉了，而恰恰是那种更新最需要回退点。
    $cutoff = (Get-Date).AddDays(-$KeepBackupDays)
    # 同时清理旧命名 app-*.db：改名之后它们不再被任何规则扫到，会永远堆着。
    # keep-*.db 与人工固定的其它命名（如 PINNED-*.db）都不在此列，不会被碰。
    $all = @(Get-ChildItem $BackupDir -ErrorAction SilentlyContinue |
             Where-Object { $_.Name -like 'pre-*.db' -or $_.Name -like 'app-*.db' } |
             Sort-Object LastWriteTime -Descending)
    if ($all.Count -gt $KeepBackupMin) {
        # 无论多旧，最近 $KeepBackupMin 份一律保留：磁盘再紧也不能连一步都退不回去
        $all | Select-Object -Skip $KeepBackupMin |
            Where-Object { $_.LastWriteTime -lt $cutoff } |
            Remove-Item -Force -ErrorAction SilentlyContinue
    }
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
# 上一次失败回滚时特意把 deploy/ 留在了新版（见回滚段的说明），它会在这里
# 显示为「已修改」。那是本脚本自己干的、且正是自愈所依赖的状态，不能因此拒绝执行。
if ($dirty.Count -gt 0) {
    $nonDeploy = @($dirty | Where-Object { $_ -notmatch '^\s*\S+\s+deploy[/\\]' })
    if ($nonDeploy.Count -eq 0) {
        Write-Log 'INFO' "deploy\ 相对 HEAD 有改动（上次回滚保留的新版脚本），继续"
        $dirty = @()
    } else {
        $dirty = $nonDeploy
    }
}
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
#
# fetch 带重试：到 GitHub 的 TLS 握手会被本地代理/网络抖动打断（实测报
# 「schannel: failed to receive handshake」，重试一次即通）。这一步失败时
# 还没动过任何东西——服务照常在跑、看门狗也没停——所以直接干净退出，
# 不要抛未捕获异常吓人，也不要跟「更新失败已回滚」共用退出码。
$fetched = $false
for ($attempt = 1; $attempt -le 3; $attempt++) {
    Write-Log 'STEP' "拉取远端信息（第 $attempt 次）"
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $global:LASTEXITCODE = 0
        $fetchOut = & git -C $Root fetch origin $Branch --quiet 2>&1 | ForEach-Object { "$_" }
    } finally {
        $ErrorActionPreference = $prevEap
    }
    if ($LASTEXITCODE -eq 0) { $fetched = $true; break }
    Write-Log 'WARN' "拉取失败（exit=$LASTEXITCODE）：$(Format-Tail $fetchOut 4)"
    if ($attempt -lt 3) { Start-Sleep -Seconds (3 * $attempt) }
}
if (-not $fetched) {
    Write-Log 'ERR' '连不上远端仓库，本次未做任何改动；服务保持原状'
    Write-Log 'ERR' '请检查网络/代理后重试（若本机开着 TUN 模式的代理，可先关掉再试）'
    if ($script:StashRef) { & git -C $Root stash pop | Out-Null }
    exit 4
}

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
[void] (Stop-Watchdog)   # 返回值不再参与控制流：无论更新前有没有，结束时都会起一个
$backup = $null
$rolledBack = $false

try {
    if (-not $SkipBackup) { $backup = Backup-Database -FromCommit $oldCommit -ToCommit $newCommit }

    Stop-App
    # --ff-only：部署端永远是远端的镜像，出现分叉说明有人在这台机器上提交过，
    # 那种情况必须人来看，不能让脚本 merge 出一个四不像。
    Invoke-Step "更新代码到 $($newCommit.Substring(0,7))" { & git -C $Root merge --ff-only $newCommit }

    Update-Dependencies -FromCommit $oldCommit -ToCommit $newCommit
    Build-Frontend

    $app = Start-App
    $rev = Test-Healthy -TimeoutSeconds 90
    if (-not $rev) { throw '应用启动后 90 秒内未通过健康检查' }
    # 健康检查过了不等于「跑的是我起的那个进程」；对不上只告警，不否决这次更新
    Assert-PortOwnedBy -Expected $app

    $expect = $newCommit.Substring(0, 7)
    if ($rev -ne $expect) {
        Write-Log 'WARN' "健康检查报告 revision=$rev，期望 $expect（可能存在残留旧进程）"
    }

    Write-Log 'OK' "=== 更新成功：$($oldCommit.Substring(0,7)) → $expect（revision=$rev）==="
}
catch {
    Write-Log 'ERR' "更新失败：$($_.Exception.Message)"
    if ($NoRollback) {
        Write-Log 'WARN' '已指定 -NoRollback：保持现场不回滚'
        Write-Log 'WARN' "  代码停在 $($newCommit.Substring(0,7))，数据库备份：$backup"
        Write-Log 'WARN' "  请自行确认服务状态；需要回退时用：git reset --hard $($oldCommit.Substring(0,7))"
        # 无条件起，不看更新前有没有。见下方 finally 处的说明。
        Start-Watchdog
        exit 1
    }
    Write-Log 'STEP' '开始回滚...'
    $rolledBack = $true
    try {
        Stop-App
        & git -C $Root reset --hard $oldCommit | Out-Null
        Write-Log 'OK' "代码已回退到 $($oldCommit.Substring(0,7))"

        # 代码回退，但 deploy/ 留在新版——这一条是为了打破一个死锁：
        #
        #   本脚本自身有缺陷 → 更新触发回滚 → 回滚把刚拉来的修复一起还原掉
        #   → 下次仍从旧脚本开始 → 又回滚 …… 于是「脚本自身的任何缺陷」
        #   都永久无法通过更新修复，只能人工介入。
        #
        # deploy/ 是元层：它管的是「怎么更新」，不是「跑什么代码」。让它保持最新，
        # 比让它与业务代码版本一致更重要。下次跑更新时用的就是修好的逻辑。
        if (-not $NoSelfHeal) {
            try {
                & git -C $Root checkout $newCommit -- 'deploy' 2>&1 | Out-Null
                if ($LASTEXITCODE -eq 0) {
                    Write-Log 'OK' "deploy\ 保留在新版 $($newCommit.Substring(0,7))（下次更新即用修好的脚本）"
                    Write-Log 'INFO' '  这会让工作区相对 HEAD 显示为「已修改」，属预期；下次更新会自动消化'
                } else {
                    Write-Log 'WARN' 'deploy\ 未能保留新版，若本次失败源于更新脚本自身，需人工处理'
                }
            } catch {
                Write-Log 'WARN' "deploy\ 保留新版失败：$($_.Exception.Message)"
            }
        }

        if ($backup) { Restore-Database -BackupPath $backup }

        # 回退后依赖与前端产物都得跟着回到旧版本
        Update-Dependencies -FromCommit $newCommit -ToCommit $oldCommit
        Build-Frontend
        $app = Start-App

        $rev = Test-Healthy -TimeoutSeconds 90
        if ($rev) {
            Assert-PortOwnedBy -Expected $app
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
    # 无条件把看门狗拉回来，而不是「更新前有才起」。
    #
    # 原先那句 WARN 建议操作员「先手动停止再更新」，照做的结果是：Stop-Watchdog
    # 返回 $false → 这里跳过 → 更新成功 exit 0 → 此后系统再无守护，而
    # WatchdogFailed 的唯一赋值点在 Start-Watchdog 里，为此准备的 exit 3 根本不可达。
    # 应用僵死没人拉起还是小事，要命的是隧道没人重启——看门狗存在的全部理由
    # 就是兜住代理干扰 QUIC 导致的隧道掉线。那时域名长期 502，
    # 而 update.log 最后一行写着「更新成功」。
    #
    # watchdog.ps1 的参数有默认值，pid 文件丢了也能起出一个可用的守护；
    # 真起不来会走 Start-Watchdog 里的 ERR 分支并把退出码抬到 3。
    Start-Watchdog
}

if ($rolledBack) { exit 1 }
if ($script:WatchdogFailed) {
    # 代码是新的、服务也在跑，但自愈守护没了——这不该报告成完全成功。
    Write-Log 'WARN' '更新已完成，但看门狗未能启动：服务当前无人守护，请手动处理'
    exit 3
}
exit 0
