[CmdletBinding()]
param([ValidateRange(1024, 65535)][int]$Port = 8765)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$desktopRoot = Join-Path $projectRoot 'desktop\copilot'
$electronExe = Join-Path $desktopRoot 'node_modules\electron\dist\electron.exe'
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $electronExe) -or -not (Test-Path -LiteralPath $pythonExe)) {
    throw 'Copilot runtime is not installed. Follow docs/copilot.md. This launcher never downloads dependencies.'
}
$localUrl = "http://127.0.0.1:$Port/copilot/index.html"
try {
    $response = Invoke-WebRequest -UseBasicParsing -Uri $localUrl -TimeoutSec 4
    if ($response.StatusCode -ne 200 -or $response.Content -notmatch 'copilot') { throw 'Not ready' }
}
catch {
    throw 'Local dashboard with copilot is not ready. Start start_dashboard.ps1 first; no running service was stopped.'
}
$launchArguments = @(('"' + $desktopRoot + '"'), "--port=$Port")
$process = Start-Process -FilePath $electronExe -ArgumentList $launchArguments -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru
Start-Sleep -Milliseconds 500
if ($process.HasExited -and $process.ExitCode -ne 0) { throw 'Copilot did not start; existing WeChat and dashboard were not modified.' }
[pscustomobject]@{ status='launch_requested'; port=$Port; follows_window_only=$true; auto_send=$false } | ConvertTo-Json -Compress
