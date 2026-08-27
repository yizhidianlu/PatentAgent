<#
.SYNOPSIS
    引途医疗专利智能体 — 数据备份（数据库热备 + 上传件与交付物）。

.DESCRIPTION
    把 DATA_DIR 下的全部业务数据备份到指定位置：

      * app.db  —— 走 sqlite 的 backup API 做热备，服务不必停
      * uploads/、outputs/ —— 增量复制

    数据目录不写死：直接问应用要（与 deploy\update.ps1 同一套判据），
    所以 .env 里把 DATA_DIR 指到数据盘之后，这个脚本自动跟着走。

    早先文档里的手工备份命令用 `os.environ['DATA_DIR']` 取路径，但 DATA_DIR 是
    pydantic-settings 从 backend\.env 读进配置**字段**的，并不会回写进程环境变量，
    照着执行只会得到 KeyError；robocopy 那两行同样会把 $env:DATA_DIR 展开成空串。
    这个脚本就是来取代那段命令的。

.PARAMETER Destination
    备份目标目录。必填。

.PARAMETER SkipMedia
    只备份数据库，跳过 uploads/ 与 outputs/（两者可能很大）。

.PARAMETER Keep
    目标目录下保留多少份数据库备份，默认 30。

.EXAMPLE
    .\deploy\backup.ps1 -Destination D:\backup\PatentAgent

.EXAMPLE
    .\deploy\backup.ps1 -Destination D:\backup\PatentAgent -SkipMedia
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $Destination,
    [switch] $SkipMedia,
    [int]    $Keep = 30
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root    = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $Root 'backend'
$Py      = Join-Path $Backend '.venv\Scripts\python.exe'

function Write-Line {
    param([string] $Level, [string] $Message)
    $color = switch ($Level) { 'OK' { 'Green' } 'WARN' { 'Yellow' } 'ERR' { 'Red' } default { 'Gray' } }
    Write-Host ('{0} [{1}] {2}' -f (Get-Date -Format 'HH:mm:ss'), $Level, $Message) -ForegroundColor $color
}

if (-not (Test-Path $Py)) {
    Write-Line 'ERR' "找不到虚拟环境 $Py，请先跑 .\start.ps1 -SetupOnly"
    exit 1
}

# 数据目录问应用要，不在这里复刻一份 .env 解析
$code = 'import sys; sys.path.insert(0, r"' + $Backend + '"); from app.config import get_config; print(get_config().data_dir)'
$prev = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    $global:LASTEXITCODE = 0
    $out = & $Py -c $code 2>$null
} finally {
    $ErrorActionPreference = $prev
}
if ($LASTEXITCODE -ne 0 -or -not $out) {
    Write-Line 'ERR' '读不到应用配置的 DATA_DIR，无法确定要备份哪个目录'
    exit 1
}
$DataDir = "$(@($out) | Select-Object -Last 1)".Trim()
Write-Line 'INFO' "数据目录：$DataDir"

if (-not (Test-Path $DataDir)) {
    Write-Line 'ERR' "数据目录不存在：$DataDir"
    exit 1
}
if (-not (Test-Path $Destination)) { New-Item -ItemType Directory -Force $Destination | Out-Null }

# --- 1. 数据库热备 ----------------------------------------------------------
$db = Join-Path $DataDir 'app.db'
if (Test-Path $db) {
    $stamp  = Get-Date -Format 'yyyyMMdd-HHmmss'
    $target = Join-Path $Destination "app-$stamp.db"
    # WAL 模式下 app.db 之外还有 -wal/-shm，冷拷会拿到不一致的快照
    $script = @"
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close(); src.close()
"@
    $tmp = Join-Path $env:TEMP "pa-backup-$stamp.py"
    Set-Content -Path $tmp -Value $script -Encoding utf8
    try {
        & $Py $tmp $db $target
        if ($LASTEXITCODE -ne 0) { throw "sqlite backup 退出码 $LASTEXITCODE" }
    } finally {
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    }
    $sizeMb = [math]::Round((Get-Item $target).Length / 1MB, 1)
    Write-Line 'OK' "数据库已备份：$([IO.Path]::GetFileName($target))（$sizeMb MB）"

    Get-ChildItem $Destination -Filter 'app-*.db' |
        Sort-Object LastWriteTime -Descending |
        Select-Object -Skip $Keep |
        Remove-Item -Force -ErrorAction SilentlyContinue
} else {
    Write-Line 'WARN' "$DataDir 下没有 app.db，跳过数据库备份"
}

# --- 2. 上传件与交付物 ------------------------------------------------------
if (-not $SkipMedia) {
    foreach ($name in @('uploads', 'outputs')) {
        $src = Join-Path $DataDir $name
        if (-not (Test-Path $src)) {
            Write-Line 'INFO' "$name\ 不存在，跳过"
            continue
        }
        $dst = Join-Path $Destination $name
        # /MIR 会让目标镜像源目录（含删除）。只在源确实存在时才走到这里，
        # 所以不会出现「源不存在导致目标被清空」的情况。
        & robocopy $src $dst /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
        # robocopy 的退出码 0-7 都算成功（8 及以上才是错误）
        if ($LASTEXITCODE -ge 8) {
            Write-Line 'ERR' "$name\ 复制失败（robocopy exit=$LASTEXITCODE）"
            exit 1
        }
        Write-Line 'OK' "$name\ 已同步到 $dst"
    }
}

Write-Line 'OK' "备份完成：$Destination"
exit 0
