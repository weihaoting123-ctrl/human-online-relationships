[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectDirectory = [System.IO.Path]::GetFullPath($PSScriptRoot)
if ([System.IO.Path]::GetPathRoot($projectDirectory) -eq 'C:\') {
    throw 'Run the project from a non-C drive with sufficient free space.'
}
$pythonExecutable = Join-Path $projectDirectory '.venv\Scripts\python.exe'
& (Join-Path $projectDirectory 'start_dashboard.ps1')
& $pythonExecutable -X utf8 (Join-Path $projectDirectory 'scripts\run_local_pipeline.py')
exit $LASTEXITCODE
