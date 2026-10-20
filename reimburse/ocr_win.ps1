param(
    [string[]]$Paths,
    [string]$ListFile,
    [string]$OutFile
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

if ($ListFile) {
    $Paths = @(Get-Content -LiteralPath $ListFile -Encoding UTF8 | Where-Object { $_.Trim() -ne '' })
}
if (-not $Paths -or $Paths.Count -eq 0) {
    throw 'NO_INPUT_PATHS'
}

try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime

    $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and
        $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    } | Select-Object -First 1)

    function Await-WinRT {
        param($Operation, [Type]$ResultType)
        $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
        $netTask = $asTask.Invoke($null, @($Operation))
        $netTask.Wait(-1) | Out-Null
        return $netTask.Result
    }

    [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
    [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
    [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
    [Windows.Globalization.Language, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null

    $language = New-Object Windows.Globalization.Language 'zh-Hans-CN'
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
    if ($null -eq $engine) {
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    }
    if ($null -eq $engine) {
        throw 'OCR_ENGINE_UNAVAILABLE'
    }

    $results = @()
    foreach ($path in $Paths) {
        $item = [ordered]@{
            path  = $path
            text  = ''
            error = $null
        }
        try {
            if (-not [System.IO.File]::Exists($path)) {
                throw 'FILE_NOT_FOUND'
            }
            $file = Await-WinRT ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
            $stream = Await-WinRT ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
            try {
                $decoder = Await-WinRT ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
                $bitmap = Await-WinRT ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
                $ocr = Await-WinRT ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
                $lines = @()
                foreach ($line in $ocr.Lines) {
                    $lines += $line.Text
                }
                $item.text = ($lines -join "`n")
            }
            finally {
                if ($null -ne $stream) { $stream.Dispose() }
            }
        }
        catch {
            $item.error = $_.Exception.Message
        }
        $results += [pscustomobject]$item
    }

    $json = ConvertTo-Json -InputObject @($results) -Depth 4 -Compress
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    if ($OutFile) {
        [System.IO.File]::WriteAllText($OutFile, $json, (New-Object System.Text.UTF8Encoding($false)))
    }
    else {
        [Console]::Out.Write($json)
    }
    exit 0
}
catch {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $payload = @([pscustomobject]@{ path = ''; text = ''; error = $_.Exception.Message })
    [Console]::Out.Write((ConvertTo-Json -InputObject $payload -Depth 4 -Compress))
    exit 1
}
