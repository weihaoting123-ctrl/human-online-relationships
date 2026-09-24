"""Header-only OCR fixtures; never attach to or capture a real application."""
import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'copilot_title_read.ps1'

class TitleNativeTests(unittest.TestCase):
    def test_reader_source_exists(self):
        self.assertTrue(SCRIPT.is_file(), 'bounded local title reader is not implemented')

    @unittest.skipUnless(os.name == 'nt', 'Windows built-in OCR only')
    def test_synthetic_bitmap_and_empty_bitmap(self):
        if not SCRIPT.is_file():
            self.skipTest('source absence tested separately')
        code = r'''
$ErrorActionPreference='Stop'
. $source
Add-Type -Path (Join-Path (Split-Path $source) 'copilot_title_native.cs') -ReferencedAssemblies System.Drawing
$bitmap = New-Object System.Drawing.Bitmap(600,90)
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$font = New-Object System.Drawing.Font('Arial',26)
try {
  $graphics.Clear([System.Drawing.Color]::White)
  $graphics.DrawString('TEST FRIEND',$font,[System.Drawing.Brushes]::Black,16,16)
  $text = Convert-HeaderBitmap $bitmap
  if($text -ne 'TEST FRIEND'){throw ('SYNTHETIC_TEXT_MISMATCH: '+($text | ConvertTo-Json -Compress))}
  $graphics.Clear([System.Drawing.Color]::White)
  if((Convert-HeaderBitmap $bitmap) -ne ''){throw 'EMPTY_BITMAP_NOT_REJECTED'}
  if(-not [CopilotHeaderNative]::ValidRoi(300,25,250,40)){throw 'VALID_ROI_REJECTED'}
  if([CopilotHeaderNative]::ValidRoi(0,0,2000,1000)){throw 'UNBOUNDED_ROI_ACCEPTED'}
  [Console]::WriteLine('SYNTHETIC_OCR_OK')
} finally {$font.Dispose();$graphics.Dispose();$bitmap.Dispose()}
'''
        # Dot sourcing defines pure bitmap OCR helpers; it must not start native capture.
        command = "& { param($source)\n" + code + "\n} '" + str(SCRIPT).replace("'", "''") + "'"
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                                 command], capture_output=True, text=True, timeout=40)
        if result.returncode and 'TITLE_OCR_NOT_INSTALLED' in result.stderr:
            self.skipTest('Windows built-in OCR is not installed on this runner')
        self.assertEqual(result.returncode, 0, result.stderr[-1800:])
        self.assertIn('SYNTHETIC_OCR_OK', result.stdout)

if __name__ == '__main__':
    unittest.main()
