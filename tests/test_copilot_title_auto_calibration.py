"""Pure geometry, in-memory OCR and mocked acquisition; never inspect real windows."""
import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'copilot_title_read.ps1'


@unittest.skipUnless(os.name == 'nt', 'Windows built-in OCR only')
class AutomaticTitleCalibrationTests(unittest.TestCase):
    def run_ps(self, body):
        setup = r'''
$ErrorActionPreference = 'Stop'
. $source
Add-Type -Path (Join-Path (Split-Path $source) 'copilot_title_native.cs') -ReferencedAssemblies System.Drawing
function New-SyntheticTarget {
    $t = New-Object CopilotHeaderNative+Target
    $t.Hwnd = [IntPtr]42; $t.Pid = 42; $t.Started = 1; $t.Dpi = 96; $t.Key = 'synthetic-target'
    $rect = New-Object CopilotHeaderNative+Rect
    $rect.Left = 100; $rect.Top = 100; $rect.Right = 1100; $rect.Bottom = 800
    $t.Bounds = $rect
    return $t
}
'''
        command = "& { param($source)\n" + setup + body + "\n} '" + str(SCRIPT).replace("'", "''") + "'"
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                                capture_output=True, text=True, timeout=45)
        if result.returncode and 'TITLE_OCR_NOT_INSTALLED' in result.stderr:
            self.skipTest('Windows OCR language pack not installed')
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])
        return result.stdout

    def test_probe_is_header_only_and_rejects_other_layouts(self):
        self.run_ps(r'''
$t = New-SyntheticTarget
$p = [CopilotHeaderNative]::CalibrationProbe($t)
if ($p.X -ne 320 -or $p.Y -ne 16 -or $p.Width -ne 600 -or $p.Height -ne 48) { throw 'PROBE_WRONG' }
foreach ($width in @(699,2001)) {
    $rect=$t.Bounds; $rect.Right = $rect.Left + $width; $t.Bounds=$rect
    $failed = $false
    try { [CopilotHeaderNative]::CalibrationProbe($t) | Out-Null } catch { $failed = $true }
    if (-not $failed) { throw 'UNSUPPORTED_LAYOUT_ACCEPTED' }
}
$rect=$t.Bounds; $rect.Right = $rect.Left + 2000; $t.Bounds=$rect
if ([CopilotHeaderNative]::CalibrationProbe($t).Width -ne 1200) { throw 'PROBE_UNBOUNDED' }
$t.Dpi = 192
if ([CopilotHeaderNative]::CalibrationProbe($t).Width -ne 600) { throw 'DPI_NOT_APPLIED' }
''')

    def test_region_leaves_space_for_later_longer_names_and_stays_in_probe(self):
        self.run_ps(r'''
$t = New-SyntheticTarget
$b = New-Object System.Drawing.RectangleF(20,10,100,22)
$r = [CopilotHeaderNative]::CalibrationRegion($t,$b)
if ($r.X -ne 334 -or $r.Y -ne 16 -or $r.Width -ne 480 -or $r.Height -ne 48) { throw 'REGION_TOO_TIGHT' }
if (-not [CopilotHeaderNative]::ValidRoi($r.X,$r.Y,$r.Width,$r.Height)) { throw 'REGION_INVALID' }
foreach ($box in @(
    (New-Object System.Drawing.RectangleF(1,10,100,22)),
    (New-Object System.Drawing.RectangleF(20,1,100,22)),
    (New-Object System.Drawing.RectangleF(20,10,475,22)),
    (New-Object System.Drawing.RectangleF(400,10,100,22)))) {
    $failed = $false
    try { [CopilotHeaderNative]::CalibrationRegion($t,$box) | Out-Null } catch { $failed = $true }
    if (-not $failed) { throw 'UNSAFE_LINE_ACCEPTED' }
}
''')

    def test_request_protocol_is_exact_and_preserves_regular_roi(self):
        self.run_ps(r'''
if ((Get-HeaderRequest ' { "operation" : "calibrate" } ').operation -cne 'calibrate') { throw 'CALIBRATE_NOT_PARSED' }
$r = Get-HeaderRequest '{"x":320,"y":16,"width":480,"height":48}'
if ($r.x -ne 320 -or $r.width -ne 480) { throw 'REGULAR_ROI_CHANGED' }
foreach ($line in @('{"operation":"Calibrate"}','{"operation":"calibrate","x":320}',
    '{"operation":"calibrate","operation":"calibrate"}','[{"operation":"calibrate"}]',
    '{"operation":"calibrate","title":"SYNTHETIC"}','{"x":320,"y":16,"width":2000,"height":900}')) {
    $failed=$false
    try { Get-HeaderRequest $line | Out-Null } catch { $failed=$true }
    if (-not $failed) { throw 'NONEXACT_REQUEST_ACCEPTED' }
}
''')

    def test_same_target_compares_geometry_dpi_pid_lifetime_and_opaque_key(self):
        self.run_ps(r'''
$a = New-SyntheticTarget; $b = New-SyntheticTarget
if (-not [CopilotHeaderNative]::SameTarget($a,$b)) { throw 'SAME_TARGET_REJECTED' }
foreach ($field in @('Dpi','Pid','Started','Key','Hwnd','Bounds')) {
    $b = New-SyntheticTarget
    switch ($field) {
        'Bounds' { $rect=$b.Bounds; $rect.Left += 1; $b.Bounds=$rect }
        'Key' { $b.Key = 'other-synthetic' }
        'Hwnd' { $b.Hwnd = [IntPtr]43 }
        default { $b.$field += 1 }
    }
    if ([CopilotHeaderNative]::SameTarget($a,$b)) { throw 'CHANGED_TARGET_ACCEPTED' }
}
''')

    def test_double_probe_returns_numeric_geometry_never_title(self):
        self.run_ps(r'''
$script:reads=0
function Read-CalibrationSample {
    $script:reads++
    return @{target=(New-SyntheticTarget);text='SYNTHETIC ONLY';rawText='SYNTHETIC ONLY';
        bounds=(New-Object System.Drawing.RectangleF(20,10,100,22));region=[CopilotHeaderNative]::CalibrationRegion((New-SyntheticTarget),(New-Object System.Drawing.RectangleF(20,10,100,22)))}
}
function Get-HeaderTarget { return New-SyntheticTarget }
$response = Invoke-HeaderCalibration 42
if ($script:reads -ne 2 -or $response.state -cne 'calibrated') { throw 'TWO_INDEPENDENT_PROBES_REQUIRED' }
$json = $response | ConvertTo-Json -Compress
if ($json -match 'SYNTHETIC ONLY|rawText|bounds|title|bitmap|image|path') { throw 'CALIBRATION_CONTENT_LEAK' }
if (($response.Keys | Sort-Object) -join ',' -cne 'region,source,state,target') { throw 'RESPONSE_KEYS' }
if ($response.region.width -ne 480 -or $response.region.x -ne 334) { throw 'REGION_MISSING' }
''')

    def test_double_probe_rejects_every_changed_observation_component(self):
        self.run_ps(r'''
function Get-HeaderTarget { return New-SyntheticTarget }
foreach ($change in @('text','rawText','bounds','region','target','finalTarget')) {
    $script:reads=0; $script:change=$change
    function Read-CalibrationSample {
        $script:reads++
        $sample=@{target=(New-SyntheticTarget);text='SYNTHETIC ONLY';rawText='SYNTHETIC ONLY';
          bounds=(New-Object System.Drawing.RectangleF(20,10,100,22));region=[CopilotHeaderNative]::CalibrationRegion((New-SyntheticTarget),(New-Object System.Drawing.RectangleF(20,10,100,22)))}
        if ($script:reads -eq 2) {
            switch ($script:change) {
                'text' { $sample.text='DIFFERENT SYNTHETIC' }
                'rawText' { $sample.rawText='DIFFERENT SYNTHETIC' }
                'bounds' { $sample.bounds.Width += 1 }
                'region' { $sample.region.Width -= 1 }
                'target' { $sample.target.Dpi = 120 }
            }
        }
        return $sample
    }
    function Get-HeaderTarget {
        $t=New-SyntheticTarget
        if ($script:change -eq 'finalTarget') { $rect=$t.Bounds; $rect.Left += 1; $t.Bounds=$rect }
        return $t
    }
    $failed=$false
    try { Invoke-HeaderCalibration 42 | Out-Null } catch {
        if ($_.Exception.Message -notmatch 'TITLE_AUTO_CALIBRATION_UNSTABLE') { throw }
        $failed=$true
    }
    if (-not $failed) { throw 'UNSTABLE_CALIBRATION_ACCEPTED' }
}
''')

    def test_synthetic_ocr_bounds_and_blank_multiline_or_clipped_rejection(self):
        self.run_ps(r'''
$bitmap=New-Object System.Drawing.Bitmap(600,48)
$graphics=[System.Drawing.Graphics]::FromImage($bitmap)
$font=New-Object System.Drawing.Font('Arial',14)
try {
    $graphics.Clear([System.Drawing.Color]::White)
    $graphics.DrawString('TEST FRIEND',$font,[System.Drawing.Brushes]::Black,20,10)
    $line=Read-HeaderBitmap $bitmap
    if ($null -eq $line -or $line.text -cne 'TEST FRIEND' -or $line.bounds.Width -le 20) { throw 'OCR_BOUNDS_MISSING' }
    $region=[CopilotHeaderNative]::CalibrationRegion((New-SyntheticTarget),$line.bounds)
    if ($region.Width -ne 480) { throw 'CALIBRATION_FROM_BITMAP_FAILED' }
    if (-not [CopilotHeaderNative]::BlankCalibrationPadding($bitmap,$line.bounds,96)) { throw 'OCR_PADDING_NOT_BLANK' }
    $graphics.Clear([System.Drawing.Color]::White)
    if ($null -ne (Read-HeaderBitmap $bitmap)) { throw 'EMPTY_ACCEPTED' }
    $graphics.DrawString('LINE ONE',$font,[System.Drawing.Brushes]::Black,20,0)
    $graphics.DrawString('LINE TWO',$font,[System.Drawing.Brushes]::Black,20,24)
    if ($null -ne (Read-HeaderBitmap $bitmap)) { throw 'MULTILINE_ACCEPTED' }
    $graphics.Clear([System.Drawing.Color]::White)
    $graphics.DrawString('TEST FRIEND...',$font,[System.Drawing.Brushes]::Black,20,10)
    if ($null -ne (Read-HeaderBitmap $bitmap)) { throw 'ELLIPSIS_ACCEPTED' }
} finally { $font.Dispose();$graphics.Dispose();$bitmap.Dispose() }
''')

    def test_padding_must_be_visibly_blank_not_merely_outside_ocr_bounds(self):
        self.run_ps(r'''
$bitmap=New-Object System.Drawing.Bitmap(600,48)
$graphics=[System.Drawing.Graphics]::FromImage($bitmap)
$bounds=New-Object System.Drawing.RectangleF(20,10,100,22)
try {
    $graphics.Clear([System.Drawing.Color]::White)
    $graphics.FillRectangle([System.Drawing.Brushes]::Black,20,10,100,22)
    if (-not [CopilotHeaderNative]::BlankCalibrationPadding($bitmap,$bounds,96)) { throw 'BLANK_PADDING_REJECTED' }
    # Unrecognized ink beside the OCR box must not be accepted as whitespace.
    $bitmap.SetPixel(124,20,[System.Drawing.Color]::Black)
    if ([CopilotHeaderNative]::BlankCalibrationPadding($bitmap,$bounds,96)) { throw 'NONBLANK_PADDING_ACCEPTED' }
} finally { $graphics.Dispose();$bitmap.Dispose() }
''')

    def test_protocol_failure_uses_only_fixed_safe_codes(self):
        self.run_ps(r'''
function Read-CalibrationSample { throw 'SYNTHETIC PRIVATE TEXT / path / provider detail' }
$response=Invoke-HeaderRequest '{"operation":"calibrate"}' 42
if ($response.reason -cne 'TITLE_AUTO_CALIBRATION_UNAVAILABLE') { throw 'FAILURE_NOT_SAFE' }
$json=$response | ConvertTo-Json -Compress
if ($json -match 'SYNTHETIC|"title"|path|provider') { throw 'FAILURE_LEAK' }
foreach ($reason in @('TITLE_OCCLUDED','TITLE_AUTO_CALIBRATION_LAYOUT_UNSUPPORTED','TITLE_AUTO_CALIBRATION_UNREADABLE')) {
    $script:failureCode=$reason
    function Read-CalibrationSample { throw $script:failureCode }
    $response=Invoke-HeaderRequest '{"operation":"calibrate"}' 42
    if ($response.reason -cne $reason) { throw 'SAFE_CODE_LOST' }
}
function Get-HeaderTarget { throw 'INVALID_REQUEST_MUST_NOT_FIND_WINDOW' }
$response=Invoke-HeaderRequest '{"operation":"calibrate","extra":true}' 42
if ($response.reason -cne 'TITLE_REQUEST_INVALID') { throw 'INVALID_REQUEST_NOT_REJECTED' }
''')

    def test_capture_implementation_keeps_both_guards_and_no_body_fallback(self):
        native = (ROOT / 'scripts' / 'copilot_title_native.cs').read_text(encoding='utf-8')
        script = SCRIPT.read_text(encoding='utf-8')
        self.assertIn('static Bitmap CaptureRectangle(', native)
        self.assertEqual(native.count('AssertUncovered(target,sx,sy,w,h);'), 2)
        self.assertIn('if(target==null||!ValidRoi(x,y,width,height))', native)
        self.assertIn('var probe=CalibrationProbe(target);', native)
        self.assertIn('version.StartsWith("4.1.13.",StringComparison.Ordinal)', native)
        self.assertIn('GetForegroundWindow()', native)
        self.assertIn('WindowFromPoint(point)', native)
        self.assertIn('patch.IntersectsWith(', native)
        self.assertNotIn('PrintWindow', native)
        self.assertNotIn('CopyFromScreen', native)
        self.assertNotIn('Save($stream,', script[script.index('function Read-CalibrationSample'):])


if __name__ == '__main__':
    unittest.main()
