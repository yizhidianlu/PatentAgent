<#
.SYNOPSIS
    引途医疗专利智能体 — 一键启动脚本。

.DESCRIPTION
    首次运行会自动完成：检查 Python / Node → 创建虚拟环境并安装后端依赖 →
    安装前端依赖并构建 → 建数据目录 → 启动服务 → 服务就绪后自动打开浏览器。
    再次运行时会跳过已完成的步骤，通常 3 秒内起服。

    按 Ctrl+C 停止服务。

.PARAMETER Port
    服务监听端口，默认 8000。端口被占用时脚本会提示占用进程并退出。

.PARAMETER NoBuild
    跳过前端依赖安装与构建（前端已构建过、只想快速起服时用）。

.PARAMETER Rebuild
    强制重新构建前端（改过前端代码后用）。

.PARAMETER NoBrowser
    不自动打开浏览器。

.PARAMETER SetupOnly
    只做环境准备与自检，不启动服务（用于验证安装是否完整）。

.EXAMPLE
    .\start.ps1
    首次安装并启动。

.EXAMPLE
    .\start.ps1 -Port 8080 -NoBrowser
    换端口启动且不自动开浏览器。

.EXAMPLE
    .\start.ps1 -SetupOnly
    只检查环境是否装好。

.NOTES
    若提示「禁止运行脚本」，请改用同目录的 start.bat 双击运行，
    或先执行：Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#>
[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int] $Port = 8000,
    [switch] $NoBuild,
    [switch] $Rebuild,
    [switch] $NoBrowser,
    [switch] $SetupOnly
)

$ErrorActionPreference = 'Stop'

# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------
$Root     = $PSScriptRoot
$Backend  = Join-Path $Root 'backend'
$Frontend = Join-Path $Root 'frontend'
$Venv     = Join-Path $Backend '.venv'
$Py       = Join-Path $Venv 'Scripts\python.exe'
$Dist     = Join-Path $Frontend 'dist\index.html'
$NodeMods = Join-Path $Frontend 'node_modules'
$DataDir  = Join-Path $Root 'data'

# --------------------------------------------------------------------------
# 控制台 UTF-8（保证中文与日志不乱码）
# 注意：本文件必须以 **UTF-8 with BOM** 保存，否则 Windows PowerShell 5.1
#       会按 GBK 解码脚本正文，下面所有中文提示都会变成乱码。
# --------------------------------------------------------------------------
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
try { chcp 65001 > $null } catch { }
try {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
    $OutputEncoding = [Console]::OutputEncoding
} catch { }

# --------------------------------------------------------------------------
# 输出助手
# --------------------------------------------------------------------------
$script:StepNo = 0
$script:StepTotal = 5

function Write-Step {
    param([string] $Message)
    $script:StepNo++
    Write-Host ""
    Write-Host "[$script:StepNo/$script:StepTotal] $Message" -ForegroundColor Cyan
}
function Write-Ok   { param([string] $m) Write-Host "      [OK] $m" -ForegroundColor Green }
function Write-Info { param([string] $m) Write-Host "      $m" -ForegroundColor Gray }
function Write-Note { param([string] $m) Write-Host "      [!]  $m" -ForegroundColor Yellow }

function Stop-WithHelp {
    param([string] $Message, [string[]] $Hints = @())
    Write-Host ""
    Write-Host "  启动失败：$Message" -ForegroundColor Red
    foreach ($h in $Hints) { Write-Host "    - $h" -ForegroundColor Yellow }
    Write-Host ""
    exit 1
}

# --------------------------------------------------------------------------
# 探测助手
# --------------------------------------------------------------------------
function Test-PortInUse {
    param([int] $TcpPort)
    $listener = $null
    try {
        $listener = New-Object System.Net.Sockets.TcpListener ([System.Net.IPAddress]::Loopback, $TcpPort)
        $listener.Start()
        return $false
    } catch {
        return $true
    } finally {
        if ($listener) { try { $listener.Stop() } catch { } }
    }
}

function Get-PortOwner {
    param([int] $TcpPort)
    try {
        $conn = Get-NetTCPConnection -LocalPort $TcpPort -State Listen -ErrorAction Stop |
                Select-Object -First 1
        if ($conn) {
            $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
            if ($proc) { return "$($proc.ProcessName)（PID $($proc.Id)）" }
            return "PID $($conn.OwningProcess)"
        }
    } catch { }
    return $null
}

function Get-PythonVersion {
    param([string] $Exe, [string[]] $Prefix = @())
    try {
        $argv = @()
        if ($Prefix) { $argv += $Prefix }
        # 注意：这段 Python 代码里**不能出现双引号**——Windows PowerShell 5.1 向原生
        # 程序传参时会吞掉内嵌的双引号，会让 python -c 收到语法错误的代码。
        $argv += @('-c', 'import sys;print(sys.version.split()[0])')
        $out = & $Exe @argv
        if ($LASTEXITCODE -eq 0 -and $out) { return ("$out").Trim() }
    } catch { }
    return $null
}

function Test-PythonVersionSupported {
    param([string] $Version)   # 形如 3.13.13
    if (-not $Version) { return $false }
    $parts = $Version.Split('.')
    if ($parts.Count -lt 2) { return $false }
    $major = [int] $parts[0]
    $minor = [int] $parts[1]
    return ($major -eq 3 -and $minor -ge 11 -and $minor -le 13)
}

function Resolve-SystemPython {
    # 优先 PATH 上的 python / python3，其次 py 启动器按 3.13 → 3.11 依次尝试
    foreach ($name in @('python', 'python3')) {
        $cmd = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue |
               Select-Object -First 1
        if ($cmd) {
            $v = Get-PythonVersion -Exe $cmd.Source
            if (Test-PythonVersionSupported $v) {
                return [pscustomobject]@{ Path = $cmd.Source; Prefix = @(); Version = $v }
            }
            if ($v) { Write-Info "跳过 $($cmd.Source)（Python $v，需 3.11 - 3.13）" }
        }
    }
    $launcher = Get-Command 'py' -CommandType Application -ErrorAction SilentlyContinue |
                Select-Object -First 1
    if ($launcher) {
        foreach ($tag in @('-3.13', '-3.12', '-3.11')) {
            $v = Get-PythonVersion -Exe $launcher.Source -Prefix @($tag)
            if (Test-PythonVersionSupported $v) {
                return [pscustomobject]@{ Path = $launcher.Source; Prefix = @($tag); Version = $v }
            }
        }
    }
    return $null
}

function Resolve-Npm {
    foreach ($name in @('npm.cmd', 'npm')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($cmd) { return $cmd.Source }
    }
    return $null
}

function Find-FirstExisting {
    param([string[]] $Paths)
    foreach ($p in $Paths) { if (Test-Path -LiteralPath $p -PathType Leaf) { return $p } }
    return $null
}

# --------------------------------------------------------------------------
# 开场
# --------------------------------------------------------------------------
Write-Host ""
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host "  引途医疗专利智能体 · 启动检查" -ForegroundColor Cyan
Write-Host "==============================================" -ForegroundColor Cyan

if (-not (Test-Path -LiteralPath $Backend -PathType Container)) {
    Stop-WithHelp "未找到 backend 目录（$Backend）" @(
        "请把 start.ps1 放在项目根目录（与 backend / frontend 同级）后再运行"
    )
}

# --------------------------------------------------------------------------
# 1. 端口
# --------------------------------------------------------------------------
Write-Step "检查端口 $Port"
if (Test-PortInUse $Port) {
    $owner = Get-PortOwner $Port
    $hints = @()
    if ($owner) {
        $hints += "端口 $Port 已被 $owner 占用"
        $hints += "若那是上一次没关干净的本程序，可在任务管理器结束该进程后重试"
    } else {
        $hints += "端口 $Port 已被其它程序占用"
    }
    $hints += "或换个端口启动：.\start.ps1 -Port 8080"
    Stop-WithHelp "端口 $Port 不可用" $hints
}
Write-Ok "端口 $Port 可用"

# --------------------------------------------------------------------------
# 2. Python 环境
# --------------------------------------------------------------------------
Write-Step "准备 Python 环境"
$venvReady = Test-Path -LiteralPath $Py -PathType Leaf

if (-not $venvReady) {
    Write-Info "未发现虚拟环境，开始创建（首次约 2-5 分钟，需要联网）…"
    $sysPy = Resolve-SystemPython
    if (-not $sysPy) {
        Stop-WithHelp "没有找到可用的 Python（需要 3.11 - 3.13）" @(
            "请到 https://www.python.org/downloads/ 安装 Python 3.12 或 3.13",
            "安装时务必勾选 Add python.exe to PATH",
            "装好后关闭并重新打开终端，再运行 .\start.ps1"
        )
    }
    Write-Info "使用 Python $($sysPy.Version)：$($sysPy.Path)"

    $venvArgs = @()
    if ($sysPy.Prefix) { $venvArgs += $sysPy.Prefix }
    $venvArgs += @('-m', 'venv', $Venv)
    & $sysPy.Path @venvArgs
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $Py -PathType Leaf)) {
        Stop-WithHelp "创建虚拟环境失败（$Venv）" @(
            "磁盘空间不足或该目录被杀毒软件锁定时会失败",
            "可手动删除 backend\.venv 后重试",
            "也可手动执行：`"$($sysPy.Path)`" -m venv `"$Venv`""
        )
    }
    Write-Ok "虚拟环境已创建"
} else {
    Write-Ok "虚拟环境就绪"
}

# 依赖自检：venv 存在但依赖缺失（如中途装到一半）时自动补装。
# 用 find_spec 只查「有没有」而不真的 import —— 缺依赖时**不会吐 Traceback**，
# 免得正常的首次安装流程里先闪一屏吓人的红字。
# 外层双引号 + 内层单引号：PS 5.1 向原生程序传参会吞掉内嵌的双引号。
$ProbeCode = "import importlib.util as u,sys;sys.exit(0 if all(u.find_spec(m) for m in ['fastapi','uvicorn','pydantic','openai']) else 1)"
& $Py -c $ProbeCode
$depsOk = ($LASTEXITCODE -eq 0)

if (-not $depsOk) {
    if ($venvReady) { Write-Note "虚拟环境存在但后端依赖不完整，开始补装…" }
    Write-Info "安装后端依赖（首次约 2-5 分钟）…"
    & $Py -m pip install --upgrade pip --quiet --disable-pip-version-check
    & $Py -m pip install -e $Backend --quiet --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) {
        Stop-WithHelp "安装后端依赖失败" @(
            "请检查网络连接（需要访问 PyPI）",
            "国内网络可换镜像源后重试：",
            "  `"$Py`" -m pip install -e `"$Backend`" -i https://pypi.tuna.tsinghua.edu.cn/simple"
        )
    }
    & $Py -c "import fastapi, uvicorn, pydantic, openai"
    if ($LASTEXITCODE -ne 0) {
        Stop-WithHelp "后端依赖装完仍无法导入" @(
            "请手动执行下面这行看具体报错：",
            "  `"$Py`" -c `"import fastapi, uvicorn, pydantic, openai`""
        )
    }
    Write-Ok "后端依赖就绪"
} else {
    Write-Ok "后端依赖就绪"
}

# --------------------------------------------------------------------------
# 3. 前端构建产物
# --------------------------------------------------------------------------
Write-Step "准备前端界面"
$distReady = Test-Path -LiteralPath $Dist -PathType Leaf

if ($NoBuild) {
    if ($distReady) {
        Write-Ok "已跳过前端构建（-NoBuild），使用现有构建产物"
    } else {
        Write-Note "已跳过前端构建（-NoBuild），但 frontend\dist 不存在，浏览器打开将看不到界面"
    }
} elseif ($distReady -and -not $Rebuild) {
    Write-Ok "前端构建产物就绪"
    Write-Info "改过前端代码请用：.\start.ps1 -Rebuild"
} else {
    $npm = Resolve-Npm
    if (-not $npm) {
        Stop-WithHelp "没有找到 npm（构建前端界面需要 Node.js 18 及以上）" @(
            "请到 https://nodejs.org/ 安装 Node.js LTS，装好后重开终端再试",
            "若只想启动后端 API，可执行：.\start.ps1 -NoBuild"
        )
    }
    Push-Location $Frontend
    try {
        if (-not (Test-Path -LiteralPath $NodeMods -PathType Container)) {
            Write-Info "安装前端依赖（首次约 1-3 分钟）…"
            & $npm install --no-fund --no-audit
            if ($LASTEXITCODE -ne 0) {
                Stop-WithHelp "npm install 失败" @(
                    "请检查网络连接",
                    "国内网络可换镜像源后重试：npm config set registry https://registry.npmmirror.com"
                )
            }
        }
        Write-Info "构建前端（约 30 秒 - 2 分钟）…"
        & $npm run build
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $Dist -PathType Leaf)) {
            Stop-WithHelp "前端构建失败" @(
                "可进入 frontend 目录手动执行 npm run build 查看详细报错"
            )
        }
    } finally {
        Pop-Location
    }
    Write-Ok "前端构建完成"
}

# --------------------------------------------------------------------------
# 4. 数据目录与本机能力探测
# --------------------------------------------------------------------------
Write-Step "检查数据目录与本机能力"
if (-not (Test-Path -LiteralPath $DataDir -PathType Container)) {
    New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
}
Write-Ok "数据目录：$DataDir"

$word = Find-FirstExisting @(
    'C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE',
    'C:\Program Files (x86)\Microsoft Office\root\Office16\WINWORD.EXE'
)
if ($word) { Write-Ok "检测到 Microsoft Word，PDF 导出可用" }
else       { Write-Note "未检测到 Microsoft Word，导出将只提供 Word 文档（.docx），可自行另存为 PDF" }

$chrome = Find-FirstExisting @(
    'C:\Program Files\Google\Chrome\Application\chrome.exe',
    'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe'
)
$edge = Find-FirstExisting @(
    'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    'C:\Program Files\Microsoft\Edge\Application\msedge.exe'
)
if ($chrome)   { Write-Ok "检测到 Chrome，国知局联网查新与图表渲染可用" }
elseif ($edge) { Write-Ok "检测到 Edge，国知局联网查新与图表渲染可用（建议装 Chrome 以获得更好成功率）" }
else           { Write-Note "未检测到 Chrome / Edge，联网查新会失败并转人工录入；建议安装 Chrome" }

if ($SetupOnly) {
    Write-Host ""
    Write-Host "==============================================" -ForegroundColor Green
    Write-Host "  环境自检通过（-SetupOnly，未启动服务）" -ForegroundColor Green
    Write-Host "  直接运行 .\start.ps1 即可启动" -ForegroundColor Green
    Write-Host "==============================================" -ForegroundColor Green
    Write-Host ""
    exit 0
}

# --------------------------------------------------------------------------
# 5. 启动服务
# --------------------------------------------------------------------------
$Url = "http://127.0.0.1:$Port"
Write-Step "启动服务"
Write-Host ""
Write-Host "      地址：$Url" -ForegroundColor White
Write-Host "      首次使用请打开「设置 → 模型服务」填写大模型接口并点击测试连接" -ForegroundColor Gray
Write-Host "      按 Ctrl+C 停止服务" -ForegroundColor Gray
Write-Host ""

$env:PORT = "$Port"

# 服务就绪后再开浏览器（轮询健康检查，避免「拒绝连接」白页）
$browserJob = $null
if (-not $NoBrowser) {
    try {
        $browserJob = Start-Job -ScriptBlock {
            param([string] $BaseUrl)
            $deadline = (Get-Date).AddSeconds(120)
            while ((Get-Date) -lt $deadline) {
                try {
                    $resp = Invoke-WebRequest -Uri "$BaseUrl/api/v1/system/health" `
                                              -UseBasicParsing -TimeoutSec 3
                    if ($resp.StatusCode -eq 200) {
                        Start-Process $BaseUrl
                        return 'opened'
                    }
                } catch { }
                Start-Sleep -Milliseconds 700
            }
            return 'timeout'
        } -ArgumentList $Url
    } catch {
        $browserJob = $null
        Write-Note "无法自动打开浏览器，请手动访问：$Url"
    }
}

$exitCode = 0
Push-Location $Backend
try {
    # --proxy-headers：信任反向代理（Cloudflare Tunnel / Nginx）传来的
    #   X-Forwarded-Proto 与 X-Forwarded-For，否则审计日志与登录限流拿到的
    #   会是隧道的本地地址而不是用户真实 IP。
    # --forwarded-allow-ips 限定只信任本机来源，防止伪造。
    & $Py -m uvicorn app.main:app --host 127.0.0.1 --port $Port `
        --proxy-headers --forwarded-allow-ips 127.0.0.1
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
    if ($browserJob) { Remove-Job $browserJob -Force -ErrorAction SilentlyContinue }
}

Write-Host ""
if ($exitCode -eq 0) {
    Write-Host "  服务已停止。数据保存在 $DataDir" -ForegroundColor Cyan
} else {
    Write-Host "  服务异常退出（代码 $exitCode）。" -ForegroundColor Red
    Write-Host "  可执行 .\start.ps1 -SetupOnly 复查环境，或查看上方报错。" -ForegroundColor Yellow
}
Write-Host ""
exit $exitCode
