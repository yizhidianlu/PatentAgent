<#
.SYNOPSIS
    引途医疗专利智能体 — 从备份恢复（数据库 + 上传件与交付物）。

.DESCRIPTION
    backup.ps1 的反向操作：把一份备份恢复到指定的数据目录。

    **为什么必须有这个脚本，而不是「照着文档手工拷贝」。**
    恢复是整条灾备链上唯一一个「只在出事时才执行」的环节——也就是唯一一个
    平时不会被验证、真用时又不容出错的环节。手工步骤在那个时刻最不可靠：
    人在慌，命令记不全，WAL 边车文件忘了删，恢复出来的库看着能开、数据却是旧的。

    恢复后的数据目录**可以与备份来源不同**：库里存的是相对路径，
    应用启动时会把遗留的绝对路径一并归一。所以「恢复到另一台机器/另一个盘」
    是受支持的，不需要额外修补。

    默认是**演练模式**（-WhatIf 语义）：只报告将要做什么，不动任何文件。
    真要写入必须显式加 -Apply —— 恢复会覆盖目标数据目录，
    一个手滑的回车不该有这么大的后果。

.PARAMETER Source
    备份目录（backup.ps1 的 -Destination）。必填。

.PARAMETER DataDir
    恢复目标数据目录。缺省时问应用要（与 backup.ps1 同一套判据）。
    **演练恢复时请显式指定一个新目录**，不要覆盖生产目录。

.PARAMETER Database
    指定要恢复的数据库快照文件名。缺省取 $Source 下最新的一份
    （app-*.db / pre-*.db / keep-*.db 一并参与挑选）。

.PARAMETER SkipMedia
    只恢复数据库，不恢复 uploads/ 与 outputs/。

.PARAMETER Apply
    真正执行写入。不给这个开关时只演练。

.EXAMPLE
    .\deploy\restore.ps1 -Source D:\backup\PatentAgent
    演练：列出将恢复哪一份库、多少个文件，不做任何改动。

.EXAMPLE
    .\deploy\restore.ps1 -Source D:\backup\PatentAgent -DataDir D:\PatentAgentRestoreTest -Apply
    恢复到一个**新目录**做验证，不碰生产。

.NOTES
    退出码：0 成功 / 1 失败 / 2 目标数据目录正被占用（应用还在跑）
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $Source,
    [string] $DataDir,
    [string] $Database,
    [switch] $SkipMedia,
    [switch] $Apply
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root    = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $Root 'backend'
$Py      = Join-Path $Backend '.venv\Scripts\python.exe'

function Write-Line {
    param([string] $Level, [string] $Message)
    $color = switch ($Level) { 'OK' { 'Green' } 'WARN' { 'Yellow' } 'ERR' { 'Red' } 'PLAN' { 'Cyan' } default { 'Gray' } }
    Write-Host ('{0} [{1}] {2}' -f (Get-Date -Format 'HH:mm:ss'), $Level, $Message) -ForegroundColor $color
}

function Do-Or-Plan {
    <# -Apply 时执行，否则只打印计划。恢复是破坏性的，默认必须是只读的。 #>
    param([string] $What, [scriptblock] $Action)
    if ($Apply) {
        Write-Line 'INFO' $What
        & $Action
    } else {
        Write-Line 'PLAN' "将执行：$What"
    }
}

if (-not (Test-Path $Source)) {
    Write-Line 'ERR' "备份目录不存在：$Source"
    exit 1
}

# --- 1. 确定目标数据目录 ----------------------------------------------------
if (-not $DataDir) {
    $dataDirScript = Join-Path $PSScriptRoot '_datadir.py'
    if (-not (Test-Path $Py) -or -not (Test-Path $dataDirScript)) {
        Write-Line 'ERR' '未指定 -DataDir，且无法从应用配置读取（缺 venv 或 _datadir.py）'
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
        Write-Line 'ERR' '读不到应用配置的 DATA_DIR，请显式指定 -DataDir'
        exit 1
    }
    $DataDir = "$(@($out) | Select-Object -Last 1)".Trim()
    Write-Line 'WARN' "未指定 -DataDir，将恢复到应用当前使用的目录：$DataDir"
    Write-Line 'WARN' '  这会覆盖生产数据。演练请改用 -DataDir 指向一个新目录。'
}
Write-Line 'INFO' "备份来源：$Source"
Write-Line 'INFO' "恢复目标：$DataDir"

# --- 2. 目标不能正被应用占用 ------------------------------------------------
#
# 应用跑着的时候恢复数据库，等于把库从它脚下抽走：连接还开着、WAL 还在写，
# 恢复出来的东西既不是旧的也不是新的。这一条必须挡在前面。
$liveDb = Join-Path $DataDir 'app.db'
if (Test-Path $liveDb) {
    try {
        $fs = [System.IO.File]::Open($liveDb, 'Open', 'ReadWrite', 'None')
        $fs.Close()
    } catch {
        Write-Line 'ERR' "目标数据库正被占用（应用可能还在运行）：$liveDb"
        Write-Line 'ERR' '  请先停掉应用与看门狗（deploy\stop-serving.ps1）再恢复。'
        exit 2
    }
}

# --- 3. 挑数据库快照 --------------------------------------------------------
if ($Database) {
    $dbFile = if (Test-Path $Database) { Get-Item $Database } else { Get-Item (Join-Path $Source $Database) }
} else {
    $candidates = @(Get-ChildItem $Source -Filter '*.db' -File -ErrorAction SilentlyContinue |
                    Where-Object { $_.Length -gt 0 } |
                    Sort-Object LastWriteTime -Descending)
    if (-not $candidates) {
        Write-Line 'ERR' "$Source 下没有可用的数据库快照（*.db）"
        exit 1
    }
    $dbFile = $candidates[0]
    if ($candidates.Count -gt 1) {
        Write-Line 'INFO' "备份目录内共 $($candidates.Count) 份快照，取最新一份；可用 -Database 指定其它："
        $candidates | Select-Object -First 5 | ForEach-Object {
            Write-Line 'INFO' ('  {0}  {1:yyyy-MM-dd HH:mm}  {2:N1} MB' -f $_.Name, $_.LastWriteTime, ($_.Length / 1MB))
        }
    }
}
Write-Line 'OK' "选定快照：$($dbFile.Name)（$([math]::Round($dbFile.Length / 1MB, 1)) MB）"

# --- 4. 快照完整性先验 ------------------------------------------------------
#
# 恢复一份坏库比不恢复更糟：应用能起来、页面能开，坏在哪要等用到才知道。
# 所以在动目标目录之前先验，验不过就一个字节都不写。
if (Test-Path $Py) {
    $check = Join-Path $env:TEMP ("pa-restore-check-{0}.py" -f (Get-Date -Format 'yyyyMMddHHmmss'))
    $code = @"
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
ok = con.execute('PRAGMA integrity_check').fetchone()[0]
if ok != 'ok':
    print('integrity_check: ' + str(ok)); sys.exit(1)
tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
need = {'cases', 'artifacts', 'files', 'users'}
missing = need - tables
if missing:
    print('缺少核心表: ' + ', '.join(sorted(missing))); sys.exit(1)
n = con.execute('SELECT COUNT(*) FROM cases').fetchone()[0]
a = con.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0]
print(f'ok cases={n} artifacts={a}')
con.close()
"@
    Set-Content -Path $check -Value $code -Encoding utf8
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $global:LASTEXITCODE = 0
        $res = & $Py $check $dbFile.FullName 2>&1 | ForEach-Object { "$_" }
    } finally {
        $ErrorActionPreference = $prev
        Remove-Item $check -Force -ErrorAction SilentlyContinue
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Line 'ERR' "快照未通过完整性检查，已中止（未写入任何文件）：$($res -join ' ')"
        exit 1
    }
    Write-Line 'OK' "快照校验通过：$($res -join ' ')"
} else {
    Write-Line 'WARN' '找不到 venv，跳过快照完整性检查'
}

# --- 5. 恢复数据库 ----------------------------------------------------------
Do-Or-Plan "创建数据目录 $DataDir" {
    if (-not (Test-Path $DataDir)) { New-Item -ItemType Directory -Force $DataDir | Out-Null }
}

# 目标已有库时先挪开而不是直接覆盖：万一挑错了快照，还退得回去
if (Test-Path $liveDb) {
    $aside = Join-Path $DataDir ("app.db.replaced-{0}" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
    Do-Or-Plan "把现有 app.db 改名留档为 $([IO.Path]::GetFileName($aside))" {
        Move-Item $liveDb $aside -Force
    }
}

Do-Or-Plan "写入数据库快照 → $liveDb" {
    Copy-Item $dbFile.FullName $liveDb -Force
    # WAL 边车必须清掉：留着旧的 -wal/-shm 会把刚恢复的主库又盖回去，
    # 而且是静默的——库能开，数据是错的。
    foreach ($sfx in @('-wal', '-shm')) {
        Remove-Item "$liveDb$sfx" -Force -ErrorAction SilentlyContinue
    }
}

# --- 6. 恢复上传件与交付物 --------------------------------------------------
if (-not $SkipMedia) {
    foreach ($name in @('uploads', 'outputs')) {
        $src = Join-Path $Source $name
        if (-not (Test-Path $src)) {
            Write-Line 'WARN' "备份里没有 $name\，跳过（正文插图与下载会缺失）"
            continue
        }
        $count = @(Get-ChildItem $src -Recurse -File -ErrorAction SilentlyContinue).Count
        $dst = Join-Path $DataDir $name
        Do-Or-Plan "恢复 $name\（$count 个文件）→ $dst" {
            # /E 而不是 /MIR：恢复只做「补齐」，绝不删除目标里已有的东西。
            # 恢复动作本身不该有删除语义——真要一个干净的目标，请自己先清空。
            & robocopy $src $dst /E /R:2 /W:5 /NFL /NDL /NJH /NJS /NP | Out-Null
            if ($LASTEXITCODE -ge 8) { throw "$name 恢复失败（robocopy exit=$LASTEXITCODE）" }
        }
    }

    $attic = Join-Path $Source '_deleted'
    if (Test-Path $attic) {
        $sets = @(Get-ChildItem $attic -Directory -ErrorAction SilentlyContinue)
        if ($sets) {
            Write-Line 'INFO' "备份里另有 $($sets.Count) 批已删除文件（$attic），本次**不**恢复。"
            Write-Line 'INFO' '  它们是此前从源目录删掉的；如需取回，请手工挑选后拷入对应目录。'
        }
    }
}

# --- 7. 收尾 ----------------------------------------------------------------
if (-not $Apply) {
    Write-Line 'OK' '演练完成，未做任何改动。确认无误后加 -Apply 真正执行。'
    exit 0
}

Write-Line 'OK' "恢复完成：$DataDir"
Write-Line 'INFO' '接下来：'
Write-Line 'INFO' "  1) 让应用指向该目录（backend\.env 里的 DATA_DIR），再启动；"
Write-Line 'INFO' '  2) 首次启动会把库里遗留的绝对路径归一为相对路径（幂等，日志里有条数）；'
Write-Line 'INFO' '  3) 逐项验证：下载 / 文本预览 / 正文插图 / 说明书附图。'
Write-Line 'WARN' '  第 3 步不能省。路径类问题恢复后不会报错，只会静默地打不开。'
exit 0
