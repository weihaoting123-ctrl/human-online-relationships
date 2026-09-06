[CmdletBinding()]
param(
    [ValidateRange(1, 720)]
    [int]$WaitHours = 168
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$pythonPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot ".venv\Scripts\python.exe"))
$watcherPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot "scripts\watch_wechat_key.py"))
$stateDir = [System.IO.Path]::GetFullPath((Join-Path $repoRoot "scripts\tmp\wechat-watcher"))
$pidFile = [System.IO.Path]::GetFullPath((Join-Path $stateDir "watcher-process.json"))

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "The project virtual environment is missing. Run start_dashboard.ps1 first."
}
if (-not (Test-Path -LiteralPath $watcherPath -PathType Leaf)) {
    throw "The local WeChat watcher entry point is missing."
}

New-Item -ItemType Directory -Path $stateDir -Force | Out-Null

function Get-ProcessDetails {
    param([Parameter(Mandatory = $true)][int]$ProcessId)
    try {
        return Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
    }
    catch {
        throw "Windows process state could not be queried safely."
    }
}

function Get-AllProcessDetails {
    try {
        return @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop)
    }
    catch {
        throw "Windows process inventory could not be queried safely."
    }
}

function Test-WatcherProcess {
    param(
        [Parameter(Mandatory = $true)]$Details,
        [Parameter(Mandatory = $true)][string[]]$AllowedPythons,
        [Parameter(Mandatory = $true)][string]$ExpectedScript
    )
    $executable = [string]$Details.ExecutablePath
    $commandLine = [string]$Details.CommandLine
    if ([string]::IsNullOrWhiteSpace($executable) -or [string]::IsNullOrWhiteSpace($commandLine)) {
        return $false
    }
    $actualPython = [System.IO.Path]::GetFullPath($executable)
    $pythonMatches = $false
    foreach ($candidate in $AllowedPythons) {
        if ($actualPython.Equals(
            ([System.IO.Path]::GetFullPath($candidate)),
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            $pythonMatches = $true
            break
        }
    }
    if (-not $pythonMatches) {
        return $false
    }
    $normalizedCommand = $commandLine.Replace("/", "\")
    $normalizedScript = ([System.IO.Path]::GetFullPath($ExpectedScript)).Replace("/", "\")
    return $normalizedCommand.IndexOf(
        $normalizedScript,
        [System.StringComparison]::OrdinalIgnoreCase
    ) -ge 0
}

function Get-WatcherProcesses {
    param(
        [Parameter(Mandatory = $true)][string[]]$AllowedPythons,
        [Parameter(Mandatory = $true)][string]$ExpectedScript
    )
    return @(
        Get-AllProcessDetails | Where-Object {
            Test-WatcherProcess -Details $_ -AllowedPythons $AllowedPythons -ExpectedScript $ExpectedScript
        }
    )
}

function Test-RecordedStartTime {
    param(
        [Parameter(Mandatory = $true)][int]$ProcessId,
        $RecordedStartTime,
        $RecordedStartTicks
    )
    try {
        $actual = (Get-Process -Id $ProcessId -ErrorAction Stop).StartTime.ToUniversalTime()
        if ($null -ne $RecordedStartTicks) {
            $recorded = [datetime]::new([long]$RecordedStartTicks, [System.DateTimeKind]::Utc)
        }
        elseif ($RecordedStartTime -is [datetime]) {
            $recorded = $RecordedStartTime.ToUniversalTime()
        }
        else {
            $recorded = [System.DateTimeOffset]::Parse([string]$RecordedStartTime).UtcDateTime
        }
        return [math]::Abs(($actual - $recorded).TotalSeconds) -lt 3
    }
    catch {
        return $false
    }
}

function Write-WatcherRecord {
    param(
        [Parameter(Mandatory = $true)]$Details,
        [Parameter(Mandatory = $true)][string]$ExpectedScript
    )
    $processId = [int]$Details.ProcessId
    $startedAt = (Get-Process -Id $processId -ErrorAction Stop).StartTime.ToUniversalTime()
    $state = [ordered]@{
        version = 1
        pid = $processId
        repo_root = $repoRoot
        python = [System.IO.Path]::GetFullPath([string]$Details.ExecutablePath)
        watcher = $ExpectedScript
        started_at_utc = $startedAt.ToString("o")
        started_at_utc_ticks = $startedAt.Ticks
    }
    $temporary = "$pidFile.$processId.tmp"
    try {
        $state | ConvertTo-Json | Set-Content -LiteralPath $temporary -Encoding UTF8
        Move-Item -LiteralPath $temporary -Destination $pidFile -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) {
            Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        }
    }
}

function Stop-NewWatcherProcesses {
    param(
        [Parameter(Mandatory = $true)][datetime]$LaunchedAfterUtc,
        [Parameter(Mandatory = $true)][string[]]$AllowedPythons,
        [Parameter(Mandatory = $true)][string]$ExpectedScript
    )
    try {
        $candidates = Get-WatcherProcesses -AllowedPythons $AllowedPythons -ExpectedScript $ExpectedScript
    }
    catch {
        return
    }
    foreach ($candidate in $candidates) {
        try {
            $started = (Get-Process -Id ([int]$candidate.ProcessId) -ErrorAction Stop).StartTime.ToUniversalTime()
            if ($started -ge $LaunchedAfterUtc.AddSeconds(-3)) {
                Stop-Process -Id ([int]$candidate.ProcessId) -Force -ErrorAction SilentlyContinue
            }
        }
        catch {
            # Cleanup is best effort and never targets an unvalidated process.
        }
    }
}

$basePythonText = [string](& $pythonPath -c "import sys; print(getattr(sys, '_base_executable', None) or sys.executable)")
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($basePythonText)) {
    throw "Unable to resolve the virtual environment Python executable."
}
$basePython = [System.IO.Path]::GetFullPath($basePythonText.Trim())
$allowedPythons = @($pythonPath, $basePython) | Select-Object -Unique

$sha = [System.Security.Cryptography.SHA256]::Create()
try {
    $digest = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($repoRoot.ToUpperInvariant()))
}
finally {
    $sha.Dispose()
}
$mutexSuffix = -join ($digest[0..11] | ForEach-Object { $_.ToString("x2") })
$lifecycleMutex = [System.Threading.Mutex]::new($false, "Local\CodexSheLoveMeWatcher_$mutexSuffix")
$mutexAcquired = $false

try {
    try {
        $mutexAcquired = $lifecycleMutex.WaitOne([TimeSpan]::FromSeconds(30))
    }
    catch [System.Threading.AbandonedMutexException] {
        $mutexAcquired = $true
    }
    if (-not $mutexAcquired) {
        throw "Another watcher lifecycle operation is still in progress."
    }

    $record = $null
    $recordInvalid = $false
    if (Test-Path -LiteralPath $pidFile -PathType Leaf) {
        try {
            $record = Get-Content -LiteralPath $pidFile -Raw -Encoding UTF8 | ConvertFrom-Json
        }
        catch {
            $recordInvalid = $true
        }
    }

    if ($null -ne $record) {
        $recordedPid = 0
        try {
            $recordedPid = [int]$record.pid
        }
        catch {
            $recordInvalid = $true
        }
        if (-not $recordInvalid -and $recordedPid -gt 0) {
            $details = Get-ProcessDetails -ProcessId $recordedPid
            $startProperty = $record.PSObject.Properties["started_at_utc"]
            $recordedStartTime = if ($null -ne $startProperty) { $startProperty.Value } else { $null }
            $ticksProperty = $record.PSObject.Properties["started_at_utc_ticks"]
            $recordedStartTicks = if ($null -ne $ticksProperty) { $ticksProperty.Value } else { $null }
            if ($null -ne $details -and
                (Test-WatcherProcess -Details $details -AllowedPythons $allowedPythons -ExpectedScript $watcherPath) -and
                (Test-RecordedStartTime -ProcessId $recordedPid -RecordedStartTime $recordedStartTime -RecordedStartTicks $recordedStartTicks)) {
                [pscustomobject]@{
                    status = "already_running"
                    pid = $recordedPid
                    local_only = $true
                } | ConvertTo-Json -Compress
                return
            }
        }
    }

    $existing = @(Get-WatcherProcesses -AllowedPythons $allowedPythons -ExpectedScript $watcherPath)
    $preferred = @($existing | Where-Object {
        ([System.IO.Path]::GetFullPath([string]$_.ExecutablePath)).Equals(
            $pythonPath,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    })
    if ($preferred.Count -eq 1) {
        Write-WatcherRecord -Details $preferred[0] -ExpectedScript $watcherPath
        [pscustomobject]@{
            status = "already_running"
            pid = [int]$preferred[0].ProcessId
            local_only = $true
        } | ConvertTo-Json -Compress
        return
    }
    if ($preferred.Count -gt 1 -or ($preferred.Count -eq 0 -and $existing.Count -gt 0)) {
        throw "An untracked local WeChat watcher is already running; refusing to start another."
    }

    if (Test-Path -LiteralPath $pidFile -PathType Leaf) {
        Remove-Item -LiteralPath $pidFile -Force
    }

    $runId = [guid]::NewGuid().ToString("N")
    $stdoutLog = Join-Path $stateDir "watcher-$runId.stdout.log"
    $stderrLog = Join-Path $stateDir "watcher-$runId.stderr.log"
    $argumentLine = '"{0}" --wait-hours {1}' -f $watcherPath, $WaitHours
    $launchTimeUtc = [DateTime]::UtcNow
    $process = $null
    try {
        $process = Start-Process `
            -FilePath $pythonPath `
            -ArgumentList $argumentLine `
            -WorkingDirectory $repoRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdoutLog `
            -RedirectStandardError $stderrLog `
            -PassThru

        Start-Sleep -Milliseconds 800
        $details = Get-ProcessDetails -ProcessId $process.Id
        if ($null -eq $details -or -not (Test-WatcherProcess -Details $details -AllowedPythons $allowedPythons -ExpectedScript $watcherPath)) {
            throw "The local WeChat watcher did not start safely."
        }
        Write-WatcherRecord -Details $details -ExpectedScript $watcherPath
    }
    catch {
        Stop-NewWatcherProcesses -LaunchedAfterUtc $launchTimeUtc -AllowedPythons $allowedPythons -ExpectedScript $watcherPath
        throw "The local WeChat watcher did not start safely."
    }

    [pscustomobject]@{
        status = "started"
        pid = $process.Id
        local_only = $true
    } | ConvertTo-Json -Compress
}
finally {
    if ($mutexAcquired) {
        try {
            $lifecycleMutex.ReleaseMutex()
        }
        catch {
            # Nothing else can be done safely during teardown.
        }
    }
    $lifecycleMutex.Dispose()
}
