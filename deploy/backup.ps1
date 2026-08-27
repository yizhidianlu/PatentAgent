<#
.SYNOPSIS
    引途医疗专利智能体 — 数据备份（数据库热备 + 上传件与交付物）。

.DESCRIPTION
    把 DATA_DIR 下的全部业务数据备份到指定位置：

      * app.db  —— 走 sqlite 的 backup API 做热备，服务不必停
      * uploads/、outputs/ —— 镜像复制；源里已删除的文件先移入 _deleted\<时间戳>        保留一段时间，而不是跟着镜像一起消失

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

.PARAMETER KeepDeletedDays
    回收阁（_deleted\）里已删除文件的保留天数，默认 90。

    媒体侧用 /MIR 镜像，源里删掉的文件下一次备份会被一并删掉——那等于备份对删除
    毫无保护。现在这类文件先移入 _deleted\<时间戳>\，过了这个窗口才真正丢弃。

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
    [int]    $Keep = 30,
    [ValidateRange(1, 3650)]
    [int]    $KeepDeletedDays = 90
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

# 数据目录问应用要，不在这里复刻一份 .env 解析。
# 代码放在 _datadir.py 里，这里只传文件名——`python -c "..."` 在 PS 5.1 上必然失败：
# 内嵌双引号会被吞掉，r"C:\path" 变成 rC:\path 直接 SyntaxError；换单引号又撞上
# 路径里的 \U 转义。这个脚本早先正是栽在这里，每次都在这一步 exit 1，一个字节也备不出来。
$dataDirScript = Join-Path $PSScriptRoot '_datadir.py'
if (-not (Test-Path $dataDirScript)) {
    Write-Line 'ERR' "缺少 $dataDirScript，无法确定数据目录"
    exit 1
}
$prev = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    $global:LASTEXITCODE = 0
    $raw = & $Py $dataDirScript 2>&1 | ForEach-Object { "$_" }
    $out = @($raw | Where-Object { $_ -and $_.Trim() })
} finally {
    $ErrorActionPreference = $prev
}
if ($LASTEXITCODE -ne 0 -or -not $out) {
    Write-Line 'ERR' '读不到应用配置的 DATA_DIR，无法确定要备份哪个目录'
    foreach ($l in @($out | Select-Object -Last 5)) { Write-Line 'ERR' "  $l" }
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
    $backupPy = @"
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close(); src.close()
"@
    $tmp = Join-Path $env:TEMP "pa-backup-$stamp.py"
    Set-Content -Path $tmp -Value $backupPy -Encoding utf8
    try {
        & $Py $tmp $db $target
        if ($LASTEXITCODE -ne 0) { throw "sqlite backup 退出码 $LASTEXITCODE" }
        if (-not (Test-Path $target) -or (Get-Item $target).Length -eq 0) {
            throw 'sqlite backup 未产出有效文件'
        }
    } catch {
        # 半成品必须删掉。源库被并发写占住时会抛 database is locked，此时目标目录里
        # 已经留下一个 0 字节的 app-<时间戳>.db；它按时间戳是「最近一份」，
        # 恢复时照着取就会拿到空文件，同时还挤占 -Keep 的保留窗口。
        Remove-Item $target -Force -ErrorAction SilentlyContinue
        Write-Line 'ERR' "数据库备份失败：$($_.Exception.Message)"
        Write-Line 'ERR' '（若提示 database is locked，请稍后重试或先停服务）'
        exit 1
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
#
# /MIR 是镜像：源里删掉的文件，下一次备份就会把备份里的那一份也删掉。
# 于是「备份」对删除毫无保护——误删一个案件、或删掉一个账号连带清盘之后，
# 只要跑过一次备份，唯一的副本就没了。数据库那侧有多份历史快照，
# 媒体这侧却是零历史，这个不对称是灾备链上最容易被忽略的一环。
#
# 所以镜像之前先把「即将被删掉的文件」挪进回收阁 _deleted\<时间戳>\。
# 只处理删除，不处理覆盖：本平台的交付物是版本化只增不改的，
# 上传件也按唯一文件名落盘，同名覆盖在正常流程里不会发生。

function Move-ToAttic {
    <#
    .SYNOPSIS
        把 $dst 里存在、而 $src 里已经没有的文件挪进回收阁；返回挪走的份数。
    #>
    param([string] $Src, [string] $Dst, [string] $AtticRoot, [string] $Name)

    if (-not (Test-Path $Dst)) { return 0 }

    $srcFiles = @{}
    foreach ($f in Get-ChildItem $Src -Recurse -File -ErrorAction SilentlyContinue) {
        $srcFiles[$f.FullName.Substring($Src.Length).TrimStart('')] = $true
    }

    $moved = 0
    foreach ($f in Get-ChildItem $Dst -Recurse -File -ErrorAction SilentlyContinue) {
        $rel = $f.FullName.Substring($Dst.Length).TrimStart('')
        if ($srcFiles.ContainsKey($rel)) { continue }
        $target = Join-Path (Join-Path $AtticRoot $Name) $rel
        $dir = Split-Path -Parent $target
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
        try {
            Move-Item -LiteralPath $f.FullName -Destination $target -Force -ErrorAction Stop
            $moved++
        } catch {
            # 挪不动就别删：宁可让 /MIR 这次不清理它，也不能把它弄丢
            Write-Line 'WARN' "无法移入回收阁，保留原样：$rel（$($_.Exception.Message)）"
        }
    }
    return $moved
}

if (-not $SkipMedia) {
    $stampAttic = Get-Date -Format 'yyyyMMdd-HHmmss'
    $atticRoot  = Join-Path (Join-Path $Destination '_deleted') $stampAttic
    $atticTotal = 0

    foreach ($name in @('uploads', 'outputs')) {
        $src = Join-Path $DataDir $name
        if (-not (Test-Path $src)) {
            Write-Line 'INFO' "$name\ 不存在，跳过"
            continue
        }
        $dst = Join-Path $Destination $name

        $moved = Move-ToAttic -Src $src -Dst $dst -AtticRoot $atticRoot -Name $name
        if ($moved -gt 0) {
            $atticTotal += $moved
            Write-Line 'WARN' "$name\ 有 $moved 个文件在源目录已不存在，已移入回收阁而非直接丢弃"
        }

        # /R /W 必须显式给：默认是「重试 100 万次、每次等 30 秒」，
        # uploads 里只要有一个文件被杀软实时扫描或 Word COM 占住，
        # 备份就会挂死约 347 天，日志停在上一行、看着像还在跑。
        & robocopy $src $dst /MIR /R:2 /W:5 /NFL /NDL /NJH /NJS /NP | Out-Null
        # robocopy 的退出码 0-7 都算成功（8 及以上才是错误）
        if ($LASTEXITCODE -ge 8) {
            Write-Line 'ERR' "$name\ 复制失败（robocopy exit=$LASTEXITCODE）"
            exit 1
        }
        Write-Line 'OK' "$name\ 已同步到 $dst"
    }

    if ($atticTotal -gt 0) {
        Write-Line 'WARN' "共 $atticTotal 个已删除文件存入 $atticRoot"
        Write-Line 'WARN' "  保留 $KeepDeletedDays 天。若是误删，请在此期限内取回。"
    } else {
        # 空目录别留着，否则回收阁里全是空壳，真有东西时反而看不出来
        Remove-Item $atticRoot -Recurse -Force -ErrorAction SilentlyContinue
    }

    # 回收阁按天清理：过了窗口才真正丢弃
    $atticBase = Join-Path $Destination '_deleted'
    if (Test-Path $atticBase) {
        $cutoff = (Get-Date).AddDays(-$KeepDeletedDays)
        Get-ChildItem $atticBase -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.LastWriteTime -lt $cutoff } |
            ForEach-Object {
                Write-Line 'INFO' "回收阁过期，清理：$($_.Name)"
                Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
            }
    }
}

Write-Line 'OK' "备份完成：$Destination"
exit 0
