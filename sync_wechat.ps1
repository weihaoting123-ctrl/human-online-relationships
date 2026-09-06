[CmdletBinding()]
param(
    [switch]$Scheduled,
    [switch]$RescanKeys,
    [switch]$AllowProcessHook
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$pythonPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot ".venv\Scripts\python.exe"))
$syncPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot "scripts\sync_all_wechat.py"))

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "The project virtual environment is missing. Run start_dashboard.ps1 first."
}
if (-not (Test-Path -LiteralPath $syncPath -PathType Leaf)) {
    throw "The local WeChat sync entry point is missing."
}

$arguments = @($syncPath, "--sync")
if ($Scheduled) {
    $arguments += "--scheduled"
}
if ($RescanKeys) {
    $arguments += "--rescan-keys"
}
if ($AllowProcessHook -and -not $Scheduled) {
    $arguments += "--allow-process-hook"
}

$syncOutput = @(& $pythonPath @arguments)
$syncExitCode = $LASTEXITCODE
$syncOutput | ForEach-Object { Write-Output $_ }

# Start the login-time observer only after a successful sync has explicitly
# reported that its local key cache is missing.  A completed sync must never
# re-arm the observer, so future WeChat launches are left entirely alone.
if ($Scheduled -and $syncExitCode -eq 0) {
    $publicStatus = $null
    try {
        $lastOutputLine = $syncOutput | Select-Object -Last 1
        if (-not [string]::IsNullOrWhiteSpace([string]$lastOutputLine)) {
            $publicStatus = $lastOutputLine | ConvertFrom-Json -ErrorAction Stop
        }
    }
    catch {
        $publicStatus = $null
    }

    if ($null -ne $publicStatus -and $publicStatus.state -eq "awaiting_wechat_restart") {
        $watcherLauncher = Join-Path $repoRoot "start_wechat_watcher.ps1"
        if (-not (Test-Path -LiteralPath $watcherLauncher -PathType Leaf)) {
            Write-Error "The local WeChat watcher launcher is missing."
            exit 1
        }
        try {
            & $watcherLauncher | Out-Null
        }
        catch {
            Write-Error "The local WeChat watcher could not be started safely."
            exit 1
        }
    }
}

exit $syncExitCode
