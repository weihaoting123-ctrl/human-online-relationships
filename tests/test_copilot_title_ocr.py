"""Built-in OCR on invented pixels only; no desktop/window acquisition."""
import base64
import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'nt', 'Windows built-in OCR only')
class HeaderOcrScaleTests(unittest.TestCase):
    def run_ps(self, body):
        source = str(ROOT / 'scripts/copilot_title_read.ps1').replace("'", "''")
        native = str(ROOT / 'scripts/copilot_title_native.cs').replace("'", "''")
        script = ("$ErrorActionPreference='Stop'\n. '" + source + "'\n"
                  "Add-Type -Path '" + native + "' -ReferencedAssemblies System.Drawing\n" + body)
        result = subprocess.run(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand',
             base64.b64encode(script.encode('utf-16-le')).decode('ascii')],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=45)
        if result.returncode and 'TITLE_OCR_NOT_INSTALLED' in result.stderr:
            self.skipTest('Windows OCR language pack not installed')
        if result.returncode and 'SYNTHETIC_CJK_LANGUAGE_UNAVAILABLE' in result.stderr:
            self.skipTest('Chinese OCR profile required for small CJK fixture')
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])

    def test_small_cjk_title_is_read_and_bounds_remain_in_original_pixels(self):
        self.run_ps(r'''
$bitmap=[Drawing.Bitmap]::new(480,40)
$g=[Drawing.Graphics]::FromImage($bitmap)
$font=[Drawing.Font]::new('Microsoft YaHei UI',13,[Drawing.FontStyle]::Regular,[Drawing.GraphicsUnit]::Pixel)
try {
    $g.Clear([Drawing.Color]::White)
    Read-HeaderBitmap $bitmap | Out-Null
    if ([Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages().RecognizerLanguage.LanguageTag -notlike 'zh-*') { throw 'SYNTHETIC_CJK_LANGUAGE_UNAVAILABLE' }
    $g.DrawString('合成好友',$font,[Drawing.Brushes]::Black,10,8)
    $line=Read-HeaderBitmap $bitmap
    if ($null -eq $line -or $line.text -cne '合成好友') { throw 'SMALL_CJK_UNREADABLE' }
    if ($line.bounds.Left -lt 10 -or $line.bounds.Right -gt 70 -or
        $line.bounds.Top -lt 8 -or $line.bounds.Bottom -gt 30) { throw 'BOUNDS_NOT_ORIGINAL_PIXELS' }
    # The caller still owns an unchanged, usable original bitmap.
    if ($bitmap.Width -ne 480 -or $bitmap.Height -ne 40) { throw 'SOURCE_MUTATED' }
    $bitmap.GetPixel(0,0) | Out-Null
} finally { $font.Dispose();$g.Dispose();$bitmap.Dispose() }
''')

    def test_scaled_ocr_keeps_blank_multiline_and_clipped_suffix_rejected(self):
        self.run_ps(r'''
$bitmap=[Drawing.Bitmap]::new(480,40)
$g=[Drawing.Graphics]::FromImage($bitmap)
$font=[Drawing.Font]::new('Microsoft YaHei UI',13,[Drawing.FontStyle]::Regular,[Drawing.GraphicsUnit]::Pixel)
try {
    $g.Clear([Drawing.Color]::White)
    if ($null -ne (Read-HeaderBitmap $bitmap)) { throw 'BLANK_ACCEPTED' }
    $g.DrawString('测试好友',$font,[Drawing.Brushes]::Black,10,0)
    $g.DrawString('合成标题',$font,[Drawing.Brushes]::Black,10,21)
    if ($null -ne (Read-HeaderBitmap $bitmap)) { throw 'MULTILINE_ACCEPTED' }
    $g.Clear([Drawing.Color]::White)
    $g.DrawString('TEST FRIEND...',$font,[Drawing.Brushes]::Black,10,8)
    if ($null -ne (Read-HeaderBitmap $bitmap)) { throw 'ELLIPSIS_ACCEPTED' }
} finally { $font.Dispose();$g.Dispose();$bitmap.Dispose() }
''')

    def test_high_dpi_title_preserves_original_bounds_and_dpi_guards(self):
        self.run_ps(r'''
$bitmap=[Drawing.Bitmap]::new(960,80)
$g=[Drawing.Graphics]::FromImage($bitmap)
$font=[Drawing.Font]::new('Arial',28,[Drawing.FontStyle]::Regular,[Drawing.GraphicsUnit]::Pixel)
try {
    $g.Clear([Drawing.Color]::White)
    $g.DrawString('TEST FRIEND',$font,[Drawing.Brushes]::Black,30,16)
    $line=Read-HeaderBitmap $bitmap 192
    if ($null -eq $line -or $line.text -cne 'TEST FRIEND' -or
        $line.bounds.Right -gt 250 -or $line.bounds.Left -lt 30) { throw 'HIGH_DPI_BOUNDS_WRONG' }
    if ((Convert-HeaderBitmap $bitmap 192) -cne 'TEST FRIEND') { throw 'REGULAR_DPI_READ_FAILED' }
    # A distant omitted suffix must block ordinary reads, not only calibration.
    $bitmap.SetPixel(800,30,[Drawing.Color]::Black)
    if ((Convert-HeaderBitmap $bitmap 192) -cne '') { throw 'REGULAR_SUFFIX_ACCEPTED' }
} finally { $font.Dispose();$g.Dispose();$bitmap.Dispose() }
''')


if __name__ == '__main__':
    unittest.main()
