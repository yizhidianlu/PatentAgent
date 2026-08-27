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
    # 隧道形态因机器而异，必须可配：
    #   -NoTunnel            只守护应用，完全不碰 cloudflared。
    #                        同机已有别的 cloudflared 服务（尤其是 token 托管模式，
    #                        本地没有 .cloudflared 目录）时必须用这个，否则本脚本
    #                        会认不出那条隧道、判定掉线，然后反复拿一个不存在的
    #                        配置文件去拉新进程，永久空转刷日志。
    #   -TunnelName          用于在 cloudflared 进程命令行里认出「自己那条」隧道。
    #   -TunnelConfig        隧道配置文件；留空则取 ~\.cloudflared\<TunnelName>.yml。
    #   -TunnelMetricsPort   cloudflared 的 metrics 端口，用来读边缘连接数。
    [switch] $NoTunnel,
    [string] $TunnelName        = 'yintu-patent',
    [string] $TunnelConfig      = '',
    [int]    $TunnelMetricsPort = 20242,
    [switch] $Once
)

$ErrorActionPreference = 'Continue'
$env:PYTHONIOENCODING = 'utf-8'
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }

$Root       = $PSScriptRoot
$Backend    = Join-Path $Root 'backend'
$Py         = Join-Path $Backend '.venv\Scripts\python.exe'
$TunnelConf = if ($TunnelConfig) { $TunnelConfig }
              else { Join-Path $env:USERPROFILE ".cloudflared\$TunnelName.yml" }
$LogFile    = Join-Path $Root 'data\watchdog.log'
# 自报身份：把 PID 与守护端口写在这里，供 deploy\update.ps1 准确找到「本仓库的看门狗」。
# 靠命令行匹配不可靠——相对路径启动（文档里给的就是 `watchdog.ps1 -Port 8000`）在命令行里
# 不会展开成绝对路径，任何按绝对路径的匹配都必然失配；于是更新时该停的看门狗没被停掉，
# 它会在依赖安装/前端构建进行中把旧代码拉起来，把整个更新带进假成功。
$PidFile    = Join-Path $Root 'data\watchdog.pid'
$script:PortFromEnv = $false

# --------------------------------------------------------------------------
# 端口来源：显式 -Port > backend\.env 的 PORT > 默认 8000
#
# 契约文档把「端口」列为部署端可通过改 .env 自主调整的本机配置，两份 .env 模板
# 也都列了 PORT。但脚本此前只认 -Port 参数，全文没有一处读 .env：改了 .env 再按
# 文档用不带 -Port 的计划任务重启，服务仍在 8000，而 .env 与对外回报的端口都是新值，
# 零告警。若据此改了隧道 ingress，对外立刻 502，排障时唯一的事实源还是错的。
# --------------------------------------------------------------------------
function Get-EnvFilePort {
    $envFile = Join-Path $Backend '.env'
    if (-not (Test-Path -LiteralPath $envFile)) { return 0 }
    try {
        foreach ($raw in (Get-Content -LiteralPath $envFile -ErrorAction Stop)) {
            $line = $raw.Trim()
            if (-not $line -or $line.StartsWith('#')) { continue }
            if ($line -match '^\s*PORT\s*=\s*"?''?(\d+)"?''?\s*$') {
                $v = [int] $Matches[1]
                if ($v -ge 1 -and $v -le 65535) { return $v }
            }
        }
    } catch { }
    return 0
}

if (-not $PSBoundParameters.ContainsKey('Port')) {
    $envPort = Get-EnvFilePort
    if ($envPort -gt 0 -and $envPort -ne $Port) {
        $Port = $envPort
        $script:PortFromEnv = $true
    }
}

# HealthUrl 依赖 $Port，必须在端口定下来之后再拼
$HealthUrl  = "http://127.0.0.1:$Port/api/v1/system/health"
$script:AppFails    = 0
$script:TunnelFails = 0
# $null = 尚未探测；探测一次后固定为 $true/$false，决定隧道健康的判据
$script:MetricsUsable = $null

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
    <# 找出「本仓库、本端口」的应用进程，返回 PID 数组。

       只按 `*uvicorn app.main*` 认人有两个真实后果：
         * 同机另一个端口上跑着的本项目实例（比如按提示改用 -Port 8080 起的那个）
           会被这个看门狗当成自己的，健康检查一失败就把人家 Force kill 掉；
         * 操作员在前台跑 start.ps1 排障时，进程会被无声杀掉、端口被隐藏的替身占住，
           再起就报端口占用，如此循环。
       所以判据必须和 deploy\update.ps1 一致：先看谁在监听本端口，
       再以本仓库 .venv 的解释器兜底（进程已崩到不监听、或刚起还没绑上端口）。
    #>
    # 端口监听者
    $byPort = [System.Collections.Generic.HashSet[int]]::new()
    try {
        Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            ForEach-Object { [void] $byPort.Add([int] $_.OwningProcess) }
    } catch { }

    # 本仓库 .venv 解释器跑的 uvicorn
    $byVenv = @(
        Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine -like "*$Py*" -and $_.CommandLine -like '*uvicorn*' } |
            ForEach-Object { [int] $_.ProcessId }
    )

    # 两条判据取**交集**，不是并集。并集会误伤：本看门狗守 8000，而同机可能还跑着
    # `.\start.ps1 -Port 8080` 起的另一个健康实例（端口占用时的官方建议就是换端口），
    # 它同样是本仓库 .venv 的 uvicorn——并集会在 8000 不健康时把 8080 那个一起 Force kill。
    $both = @($byVenv | Where-Object { $byPort.Contains($_) })
    if ($both.Count -gt 0) { return $both }

    # 交集为空的两种情形，都只该动本端口上的那个：
    #   * 进程崩到不再监听（byPort 空）→ 用 byVenv 里的？不行，那可能是别的端口上的实例。
    #     只有当 byVenv 只有一个、且本端口无人监听时，才能确定它就是我们的。
    #   * 端口被非本仓库进程占着（byVenv 空）→ 交给上层报错，不在这里杀。
    if ($byPort.Count -eq 0 -and $byVenv.Count -eq 1) { return $byVenv }
    return @()
}

function Get-TunnelProcess {
    Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*$TunnelName*" }
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
    <# 进程在 + 边缘连接数 >= 1 才算健康。

       原先 metrics 抓不到时直接 return $true（fail-open），与 Test-AppHealthy 的
       fail-closed 方向相反：「进程还在、边缘连接掉到 0」正是本脚本要兜的那种故障，
       却会被判成健康。但一律 fail-closed 也不行——cloudflared 没带 --metrics 时
       端口本就不存在，那会变成每 30 秒重启一次隧道。

       折中：首次探测决定后续判据。metrics 可用则严格按连接数判；
       不可用则说明这台机器的隧道没开 metrics，退化为只看进程存活，并说明一次。
    #>
    if (-not (Get-TunnelProcess)) { return $false }
    try {
        $m = Invoke-WebRequest -Uri "http://127.0.0.1:$TunnelMetricsPort/metrics" -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
        if ($null -eq $script:MetricsUsable) {
            $script:MetricsUsable = $true
            Write-Log 'INFO' "隧道 metrics 可用（$TunnelMetricsPort），按边缘连接数判定健康"
        }
        foreach ($l in ($m.Content -split "`n")) {
            if ($l -match '^cloudflared_tunnel_ha_connections\s+(\d+)') {
                if ([int]$Matches[1] -lt 1) { return $false }
            }
        }
        return $true
    } catch {
        if ($null -eq $script:MetricsUsable) {
            $script:MetricsUsable = $false
            Write-Log 'WARN' ("隧道 metrics 不可达（$TunnelMetricsPort）：隧道健康将只按进程存活判定，" +
                              '「进程在但边缘连接掉到 0」这类故障探不出来。' +
                              '若隧道确实开了 metrics，请用 -TunnelMetricsPort 指明端口。')
        }
        # metrics 从可用变为不可达，通常意味着隧道进程出了问题——按不健康处理
        if ($script:MetricsUsable) { return $false }
        return $true
    }
}

function Restart-App {
    Write-Log 'FIX' '重启应用...'
    Get-AppProcess | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
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
    # --metrics 必须带上：不带的话 cloudflared 会退回默认行为，metrics 不在我们探的
    # 端口上；而 Test-TunnelHealthy 首次探测成功后已切到 fail-closed，于是探不到就判
    # 不健康 → 再重启 → 还是不带 --metrics……每 60 秒重启一次本来健康的隧道，永不收敛。
    $argList = @('tunnel','--config',$TunnelConf,'--metrics',"127.0.0.1:$TunnelMetricsPort",'run')
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

    # -NoTunnel：这台机器的隧道不归本脚本管（比如是 token 托管模式的服务，
    # 或者压根没对外），只守护应用。
    if ($NoTunnel) { return $appOk }

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

if ($NoTunnel) {
    Write-Log 'INFO' "守护进程启动（每 $IntervalSeconds 秒体检，端口 $Port；不守护隧道）"
} else {
    # 配置文件不存在还硬跑，只会每 30 秒拿一个不存在的路径去拉进程，永久空转。
    # 同机已有 token 托管模式的 cloudflared 服务时，本地不会有 .cloudflared 目录，
    # 正是这种情况——那时应当用 -NoTunnel。
    if (-not (Test-Path -LiteralPath $TunnelConf)) {
        Write-Log 'ERR' "隧道配置不存在：$TunnelConf"
        Write-Log 'ERR' '若本机隧道由 cloudflared 服务（token 托管模式）管理，请改用 -NoTunnel 只守护应用；'
        Write-Log 'ERR' '若用独立进程 + 独立配置，请用 -TunnelName / -TunnelConfig / -TunnelMetricsPort 指明。'
        exit 2
    }
    Write-Log 'INFO' "守护进程启动（每 $IntervalSeconds 秒体检，端口 $Port；隧道 $TunnelName，metrics $TunnelMetricsPort）"
}

if ($Once) {
    $ok = Invoke-Check
    if ($ok) {
        Write-Log 'INFO' '单次体检结果：全部正常'
        exit 0
    }
    Write-Log 'INFO' '单次体检结果：存在异常'
    exit 1
}

# 只有常驻模式才登记身份；-Once 是一次性体检，不该被当成正在守护的实例。
try {
    $pidDir = Split-Path -Parent $PidFile
    if (-not (Test-Path $pidDir)) { New-Item -ItemType Directory -Force $pidDir | Out-Null }
    # args 行记下完整启动参数：update.ps1 停掉看门狗后要原样把它拉回来。
    # 只记 port 是不够的——隧道形态的几个开关丢掉后，重启的看门狗会静默回落到
    # 默认形态，轻则盯错隧道，重则撞上配置缺失的闸门当场退出，服务从此无人守护。
    $savedArgs = @('-Port', "$Port", '-IntervalSeconds', "$IntervalSeconds")
    if ($NoTunnel) {
        $savedArgs += '-NoTunnel'
    } else {
        $savedArgs += @('-TunnelName', $TunnelName, '-TunnelMetricsPort', "$TunnelMetricsPort")
        if ($TunnelConfig) { $savedArgs += @('-TunnelConfig', $TunnelConfig) }
    }
    Set-Content -Path $PidFile -Encoding utf8 -Value @(
        "pid=$PID"
        "port=$Port"
        "root=$Root"
        "started=$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
        "args=$($savedArgs -join '|')"
    )
} catch {
    Write-Log 'WARN' ('无法写入 ' + $PidFile + '：' + $_.Exception.Message + '；update.ps1 可能认不出本进程')
}

try {
    while ($true) {
        try {
            [void](Invoke-Check)
        } catch {
            Write-Log 'ERR' ('体检异常：' + $_.Exception.Message)
        }
        Start-Sleep -Seconds $IntervalSeconds
    }
} finally {
    # 被 Stop-Process -Force 杀掉时 finally 不执行，会留下过期的 pid 文件；
    # 所以读取方必须核对该 PID 是否还活着，不能只看文件在不在。
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}
