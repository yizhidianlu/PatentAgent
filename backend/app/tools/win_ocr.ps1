# Windows 自带 OCR（Windows.Media.Ocr）命令行封装：识别一张图片，逐行输出文字。
#
# 用途：老专利的公开文本多是扫描件，PDF 里没有文字层；Google Patents 详情页又常常
# 拿不到。系统自带的 OCR 引擎（装了对应语言包即可用）能把扉页的摘要认出来，
# 不需要再装任何东西。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File win_ocr.ps1 -Path 图片路径 [-Lang zh-Hans-CN]
#   powershell -NoProfile -ExecutionPolicy Bypass -File win_ocr.ps1 -Probe        # 列出可用语言，一行一个
# 退出码：0 成功；2 没有该语言的 OCR 引擎；3 读图失败。输出为 UTF-8。
param(
    [string]$Path = "",
    [string]$Lang = "zh-Hans-CN",
    [switch]$Probe
)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = "Stop"

[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime] | Out-Null

if ($Probe) {
    foreach ($l in [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages) { Write-Output $l.LanguageTag }
    exit 0
}
if (-not $Path) { Write-Error "缺少 -Path"; exit 3 }

[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime] | Out-Null
Add-Type -AssemblyName System.Runtime.WindowsRuntime

# WinRT 的异步方法在 PowerShell 里要经 AsTask 同步等待
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]
function Await($op, $type) {
    $task = $asTaskGeneric.MakeGenericMethod($type).Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
    $task.Result
}

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new($Lang))
if ($null -eq $engine) { Write-Error "没有 $Lang 的 OCR 引擎（需安装对应语言包）"; exit 2 }

try {
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
} catch {
    Write-Error "读图失败：$($_.Exception.Message)"; exit 3
}

$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
foreach ($line in $result.Lines) { Write-Output $line.Text }
exit 0
