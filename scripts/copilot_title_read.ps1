[CmdletBinding()]
param([int]$ParentPid = 0)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

function Wait-HeaderOperation($Operation, [Type]$ResultType) {
    $method = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    } | Select-Object -First 1
    $task = $method.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    if (-not $task.Wait(1800)) { throw 'TITLE_OCR_TIMEOUT' }
    return $task.Result
}

function Convert-HeaderBitmap([System.Drawing.Bitmap]$Bitmap) {
    try {
        Add-Type -AssemblyName System.Runtime.WindowsRuntime
        [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
        [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
        [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
        [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
        [Windows.Globalization.Language, Windows.Globalization, ContentType=WindowsRuntime] | Out-Null
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
        if ($null -eq $engine) { throw 'missing' }
    } catch { throw 'TITLE_OCR_NOT_INSTALLED' }
    $stream = New-Object System.IO.MemoryStream
    $random = $null; $software = $null
    try {
        $Bitmap.Save($stream,[System.Drawing.Imaging.ImageFormat]::Png)
        $stream.Position = 0
        $random = [System.IO.WindowsRuntimeStreamExtensions]::AsRandomAccessStream($stream)
        $decoder = Wait-HeaderOperation ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($random)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $software = Wait-HeaderOperation ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $result = Wait-HeaderOperation ($engine.RecognizeAsync($software)) ([Windows.Media.Ocr.OcrResult])
        $lines = @($result.Lines)
        if ($lines.Count -ne 1) { return '' }
        $words = @($lines[0].Words)
        if ($words.Count -eq 0) { return '' }
        foreach ($word in $words) {
            $rect = $word.BoundingRect
            if ($rect.X -lt 2 -or $rect.Y -lt 2 -or $rect.X+$rect.Width -gt $Bitmap.Width-2 -or $rect.Y+$rect.Height -gt $Bitmap.Height-2) { return '' }
        }
        $text = $lines[0].Text.Trim().Normalize([Text.NormalizationForm]::FormC)
        # Windows OCR inserts spaces between adjacent CJK tokens. Do not fuzzy-match letters.
        $text = [regex]::Replace($text, '(?<=[\p{IsCJKUnifiedIdeographs}]) +(?=[\p{IsCJKUnifiedIdeographs}])', '')
        if ($text.Length -gt 200 -or $text -match '[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069\u2026]|\.{3}') { return '' }
        return $text
    } finally {
        if ($null -ne $software) { $software.Dispose() }
        if ($null -ne $random) { $random.Dispose() }
        $stream.Dispose()
    }
}

# Dot sourcing is a normal library import for synthetic bitmap tests, not capture.
if ($MyInvocation.InvocationName -eq '.') { return }
try {
    if ($ParentPid -le 0 -or [IntPtr]::Size -ne 8) { throw 'TITLE_PARENT_REQUIRED' }
    Add-Type -Path (Join-Path $PSScriptRoot 'copilot_title_native.cs') -ReferencedAssemblies System.Drawing
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    while ($null -ne ($line = [Console]::ReadLine())) {
        $bitmap = $null
        try {
            if ($null -eq (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue)) { break }
            if ($line.Length -gt 512) { throw 'TITLE_REQUEST_INVALID' }
            $roi = $line | ConvertFrom-Json
            if (($roi.PSObject.Properties.Name | Sort-Object) -join ',' -ne 'height,width,x,y') { throw 'TITLE_REQUEST_INVALID' }
            $target = [CopilotHeaderNative]::Find($ParentPid)
            if ($null -eq $target) { throw 'TITLE_WINDOW_UNAVAILABLE' }
            $bitmap = [CopilotHeaderNative]::Capture($target,[double]$roi.x,[double]$roi.y,[double]$roi.width,[double]$roi.height)
            $title = Convert-HeaderBitmap $bitmap
            if ([string]::IsNullOrWhiteSpace($title)) { throw 'TITLE_UNREADABLE' }
            # This bounded private pipe terminates in the local desktop process only.
            [Console]::WriteLine((@{state='observed';title=$title;target=$target.Key;source='local_ocr'} | ConvertTo-Json -Compress))
        } catch {
            [Console]::WriteLine('{"state":"unavailable","reason":"TITLE_UNAVAILABLE","source":"local_ocr"}')
        } finally { if ($null -ne $bitmap) { $bitmap.Dispose() }; $title = $null }
    }
} catch { [Console]::WriteLine('{"state":"unavailable","reason":"TITLE_READER_UNAVAILABLE","source":"local_ocr"}'); exit 2 }
