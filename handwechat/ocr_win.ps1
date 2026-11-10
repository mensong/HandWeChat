param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Paths
)

$ErrorActionPreference = "Stop"

if (-not $Paths) {
    Write-Output "{}"
    exit 0
}

Add-Type -AssemblyName System.Runtime.WindowsRuntime

[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrResult, Windows.Media.Ocr, ContentType = WindowsRuntime] | Out-Null

$asTaskMethod = [System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object {
        $_.Name -eq "AsTask" -and
        $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq "IAsyncOperation``1"
    } |
    Select-Object -First 1

function Await-WinRt($operation, $resultType) {
    $asTask = $asTaskMethod.MakeGenericMethod($resultType)
    $task = $asTask.Invoke($null, @($operation))
    $task.Wait(-1) | Out-Null
    return $task.Result
}

$language = New-Object Windows.Globalization.Language("zh-Hans-CN")
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
if ($null -eq $engine) {
    Write-Output "{}"
    exit 2
}

$result = [ordered]@{}
foreach ($path in $Paths) {
    $result[$path] = ""
    try {
        $file = Await-WinRt (
            [Windows.Storage.StorageFile]::GetFileFromPathAsync($path)
        ) ([Windows.Storage.StorageFile])
        $stream = Await-WinRt (
            $file.OpenAsync([Windows.Storage.FileAccessMode]::Read)
        ) ([Windows.Storage.Streams.IRandomAccessStream])
        $decoder = Await-WinRt (
            [Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)
        ) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await-WinRt (
            $decoder.GetSoftwareBitmapAsync()
        ) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $ocr = Await-WinRt (
            $engine.RecognizeAsync($bitmap)
        ) ([Windows.Media.Ocr.OcrResult])
        $result[$path] = $ocr.Text
    } catch {
        $result[$path] = ""
    }
}

Write-Output (ConvertTo-Json -InputObject $result -Compress)
