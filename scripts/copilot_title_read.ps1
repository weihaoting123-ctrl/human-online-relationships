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

function Read-HeaderBitmap([System.Drawing.Bitmap]$Bitmap) {
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
        if ($lines.Count -ne 1) { return $null }
        $words = @($lines[0].Words)
        if ($words.Count -eq 0) { return $null }
        $left = [double]::PositiveInfinity; $top = [double]::PositiveInfinity
        $right = 0.0; $bottom = 0.0
        foreach ($word in $words) {
            $rect = $word.BoundingRect
            if ($rect.X -lt 2 -or $rect.Y -lt 2 -or $rect.X+$rect.Width -gt $Bitmap.Width-2 -or $rect.Y+$rect.Height -gt $Bitmap.Height-2) { return $null }
            $left = [Math]::Min($left,$rect.X); $top = [Math]::Min($top,$rect.Y)
            $right = [Math]::Max($right,$rect.X+$rect.Width); $bottom = [Math]::Max($bottom,$rect.Y+$rect.Height)
        }
        $rawText = $lines[0].Text
        $text = $rawText.Trim().Normalize([Text.NormalizationForm]::FormC)
        # Windows OCR inserts spaces between adjacent CJK tokens. Do not fuzzy-match letters.
        $text = [regex]::Replace($text, '(?<=[\p{IsCJKUnifiedIdeographs}]) +(?=[\p{IsCJKUnifiedIdeographs}])', '')
        # OCR can collapse a displayed ellipsis into a single terminal period.
        # Reject that uncertain suffix too; a legitimate dotted name can use manual mode.
        if ([string]::IsNullOrWhiteSpace($text) -or $text.Length -gt 200 -or
            $rawText -match '[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069\u2026\u22ef]|\.{3}' -or
            $text -match '[.\uFF0E\u3002]\s*$') { return $null }
        return @{text=$text;rawText=$rawText;bounds=(New-Object System.Drawing.RectangleF($left,$top,($right-$left),($bottom-$top)))}
    } finally {
        if ($null -ne $software) { $software.Dispose() }
        if ($null -ne $random) { $random.Dispose() }
        $stream.Dispose()
    }
}

function Convert-HeaderBitmap([System.Drawing.Bitmap]$Bitmap) {
    $line = Read-HeaderBitmap $Bitmap
    if ($null -eq $line) { return '' }
    return $line.text
}

function Get-HeaderRequest([string]$Line) {
    if ($Line.Length -gt 512) { throw 'TITLE_REQUEST_INVALID' }
    # One explicit operation, no extra fields, duplicate keys, casing aliases or
    # caller-selected probe. The ordinary calibrated-ROI protocol stays separate.
    if ($Line -cmatch '^\s*\{\s*"operation"\s*:\s*"calibrate"\s*\}\s*$') { return @{operation='calibrate'} }
    try { $roi = $Line | ConvertFrom-Json } catch { throw 'TITLE_REQUEST_INVALID' }
    if ($null -eq $roi -or $roi -is [Array] -or ($roi.PSObject.Properties.Name | Sort-Object) -join ',' -cne 'height,width,x,y') { throw 'TITLE_REQUEST_INVALID' }
    foreach ($value in @($roi.x,$roi.y,$roi.width,$roi.height)) {
        if ($value -isnot [ValueType] -or $value -is [bool]) { throw 'TITLE_REQUEST_INVALID' }
    }
    if (-not [CopilotHeaderNative]::ValidRoi($roi.x,$roi.y,$roi.width,$roi.height)) { throw 'TITLE_REQUEST_INVALID' }
    return @{x=$roi.x;y=$roi.y;width=$roi.width;height=$roi.height}
}

function Get-HeaderTarget([int]$AssistantPid) {
    return [CopilotHeaderNative]::Find($AssistantPid)
}

function Read-CalibrationSample([int]$AssistantPid) {
    $target = Get-HeaderTarget $AssistantPid
    if ($null -eq $target) { throw 'TITLE_WINDOW_UNAVAILABLE' }
    $bitmap = $null
    try {
        $bitmap = [CopilotHeaderNative]::CaptureProbe($target)
        $line = Read-HeaderBitmap $bitmap
        if ($null -eq $line) { throw 'TITLE_AUTO_CALIBRATION_UNREADABLE' }
        $region = [CopilotHeaderNative]::CalibrationRegion($target,$line.bounds)
        if (-not [CopilotHeaderNative]::BlankCalibrationPadding($bitmap,$line.bounds,$target.Dpi)) { throw 'TITLE_AUTO_CALIBRATION_UNREADABLE' }
        if (-not [CopilotHeaderNative]::SameTarget($target,(Get-HeaderTarget $AssistantPid))) { throw 'TITLE_AUTO_CALIBRATION_UNSTABLE' }
        return @{target=$target;text=$line.text;rawText=$line.rawText;bounds=$line.bounds;region=$region}
    } finally { if ($null -ne $bitmap) { $bitmap.Dispose() }; $line = $null }
}

function Invoke-HeaderCalibration([int]$AssistantPid) {
    $first = $null; $second = $null
    try {
        $first = Read-CalibrationSample $AssistantPid
        $second = Read-CalibrationSample $AssistantPid
        if (-not [CopilotHeaderNative]::SameTarget($first.target,$second.target) -or
            $first.text -cne $second.text -or $first.rawText -cne $second.rawText -or
            -not $first.bounds.Equals($second.bounds) -or -not $first.region.Equals($second.region) -or
            -not [CopilotHeaderNative]::SameTarget($second.target,(Get-HeaderTarget $AssistantPid))) { throw 'TITLE_AUTO_CALIBRATION_UNSTABLE' }
        $region = $second.region
        # Titles/bounds/bitmaps are volatile; only this numeric DTO crosses the pipe.
        return @{state='calibrated';region=@{x=$region.X;y=$region.Y;width=$region.Width;height=$region.Height};target=$second.target.Key;source='local_ocr'}
    } finally { $first = $null; $second = $null }
}

function Invoke-HeaderRequest([string]$Line,[int]$AssistantPid) {
    $bitmap = $null; $calibration = $false
    try {
        $request = Get-HeaderRequest $Line
        if ($request.ContainsKey('operation')) { $calibration = $true; return Invoke-HeaderCalibration $AssistantPid }
        $target = Get-HeaderTarget $AssistantPid
        if ($null -eq $target) { throw 'TITLE_WINDOW_UNAVAILABLE' }
        $bitmap = [CopilotHeaderNative]::Capture($target,$request.x,$request.y,$request.width,$request.height)
        $title = Convert-HeaderBitmap $bitmap
        if ([string]::IsNullOrWhiteSpace($title)) { throw 'TITLE_UNREADABLE' }
        if (-not [CopilotHeaderNative]::SameTarget($target,(Get-HeaderTarget $AssistantPid))) { throw 'TITLE_TARGET_CHANGED' }
        return @{state='observed';title=$title;target=$target.Key;source='local_ocr'}
    } catch {
        $reason = 'TITLE_UNAVAILABLE'
        if ($calibration) { $reason = 'TITLE_AUTO_CALIBRATION_UNAVAILABLE' }
        $errorCause = $_.Exception
        $allowed = @('TITLE_REQUEST_INVALID','TITLE_WINDOW_UNAVAILABLE','TITLE_AUTO_CALIBRATION_LAYOUT_UNSUPPORTED',
            'TITLE_AUTO_CALIBRATION_UNREADABLE','TITLE_AUTO_CALIBRATION_UNSTABLE','TITLE_OCCLUDED')
        while ($null -ne $errorCause) {
            if ($allowed -ccontains $errorCause.Message) { $reason = $errorCause.Message; break }
            if ($calibration -and $errorCause.Message -ceq 'TITLE_TARGET_CHANGED') { $reason = 'TITLE_AUTO_CALIBRATION_UNSTABLE'; break }
            $errorCause = $errorCause.InnerException
        }
        return @{state='unavailable';reason=$reason;source='local_ocr'}
    } finally { if ($null -ne $bitmap) { $bitmap.Dispose() }; $title = $null }
}

# Dot sourcing is a normal library import for synthetic bitmap tests, not capture.
if ($MyInvocation.InvocationName -eq '.') { return }
try {
    if ($ParentPid -le 0 -or [IntPtr]::Size -ne 8) { throw 'TITLE_PARENT_REQUIRED' }
    Add-Type -Path (Join-Path $PSScriptRoot 'copilot_title_native.cs') -ReferencedAssemblies System.Drawing
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    while ($null -ne ($line = [Console]::ReadLine())) {
        if ($null -eq (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue)) { break }
        # This bounded private pipe terminates in the local desktop process only.
        [Console]::WriteLine((Invoke-HeaderRequest $line $ParentPid | ConvertTo-Json -Compress -Depth 3))
    }
} catch { [Console]::WriteLine('{"state":"unavailable","reason":"TITLE_READER_UNAVAILABLE","source":"local_ocr"}'); exit 2 }
