[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$expectedRunner = [System.IO.Path]::GetFullPath((Join-Path $repoRoot "scripts\run_dashboard.py"))
$scriptsRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot "scripts"))
$expectedPython = [System.IO.Path]::GetFullPath((Join-Path $repoRoot ".venv\Scripts\python.exe"))
$stateDir = [System.IO.Path]::GetFullPath((Join-Path $repoRoot "scripts\tmp\dashboard"))
$pidFile = [System.IO.Path]::GetFullPath((Join-Path $stateDir "dashboard-process.json"))

function Get-RecordValue {
    param(
        [Parameter(Mandatory = $true)]$Record,
        [Parameter(Mandatory = $true)][string]$Name
    )
    $property = $Record.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return $null
    }
    return $property.Value
}

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

function Test-PathInCommandLine {
    param(
        [Parameter(Mandatory = $true)][string]$CommandLine,
        [Parameter(Mandatory = $true)][string]$ExpectedPath
    )
    $normalizedCommand = $CommandLine.Replace("/", "\")
    $normalizedPath = ([System.IO.Path]::GetFullPath($ExpectedPath)).Replace("/", "\")
    return $normalizedCommand.IndexOf(
        $normalizedPath,
        [System.StringComparison]::OrdinalIgnoreCase
    ) -ge 0
}

function Get-DashboardPort {
    param([Parameter(Mandatory = $true)][string]$CommandLine)
    $match = [regex]::Match(
        $CommandLine,
        '(?i)(?:^|\s)--port(?:=|\s+)["'']?(?<port>\d{1,5})(?:["'']?)(?:\s|$)'
    )
    if (-not $match.Success) {
        return 0
    }
    $value = [int]$match.Groups["port"].Value
    if ($value -lt 1024 -or $value -gt 65535) {
        return 0
    }
    return $value
}

function Resolve-PythonCandidates {
    if (-not (Test-Path -LiteralPath $expectedPython -PathType Leaf)) {
        return @()
    }
    $baseText = [string](& $expectedPython -c "import sys; print(getattr(sys, '_base_executable', None) or sys.executable)")
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($baseText)) {
        throw "Unable to resolve the virtual environment Python executable."
    }
    return @($expectedPython, [System.IO.Path]::GetFullPath($baseText.Trim())) | Select-Object -Unique
}

function Test-DashboardProcess {
    param(
        [Parameter(Mandatory = $true)]$Details,
        [Parameter(Mandatory = $true)][string]$ExpectedRunner,
        [Parameter(Mandatory = $true)][string[]]$AllowedPythons
    )
    $commandLine = [string]$Details.CommandLine
    $executablePath = [string]$Details.ExecutablePath
    if ([string]::IsNullOrWhiteSpace($commandLine) -or [string]::IsNullOrWhiteSpace($executablePath)) {
        return $false
    }
    $actualExecutable = [System.IO.Path]::GetFullPath($executablePath)
    $allowed = $false
    foreach ($candidate in $AllowedPythons) {
        if ($actualExecutable.Equals(
            ([System.IO.Path]::GetFullPath($candidate)),
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            $allowed = $true
            break
        }
    }
    if (-not $allowed -or -not (Test-PathInCommandLine -CommandLine $commandLine -ExpectedPath $ExpectedRunner)) {
        return $false
    }
    $hostPattern = '(?i)(?:^|\s)--host(?:=|\s+)["'']?127\.0\.0\.1(?:["'']?)(?:\s|$)'
    return ($commandLine -match $hostPattern) -and (Get-DashboardPort -CommandLine $commandLine) -gt 0
}

function Get-ValidatedChildProcesses {
    param(
        [Parameter(Mandatory = $true)][int]$RootProcessId,
        [Parameter(Mandatory = $true)][string[]]$AllowedPythons
    )
    $all = Get-AllProcessDetails
    $knownParents = [System.Collections.Generic.HashSet[int]]::new()
    [void]$knownParents.Add($RootProcessId)
    $descendants = New-Object System.Collections.Generic.List[object]
    $changed = $true
    while ($changed) {
        $changed = $false
        foreach ($item in $all) {
            $candidateProcessId = [int]$item.ProcessId
            if ($knownParents.Contains($candidateProcessId) -or -not $knownParents.Contains([int]$item.ParentProcessId)) {
                continue
            }
            [void]$knownParents.Add($candidateProcessId)
            [void]$descendants.Add($item)
            $changed = $true
        }
    }
    return @(
        $descendants | Where-Object {
            $commandLine = [string]$_.CommandLine
            $executablePath = [string]$_.ExecutablePath
            if ([string]::IsNullOrWhiteSpace($commandLine) -or [string]::IsNullOrWhiteSpace($executablePath)) {
                return $false
            }
            $actual = [System.IO.Path]::GetFullPath($executablePath)
            $pythonAllowed = $false
            foreach ($candidate in $AllowedPythons) {
                if ($actual.Equals(
                    ([System.IO.Path]::GetFullPath($candidate)),
                    [System.StringComparison]::OrdinalIgnoreCase
                )) {
                    $pythonAllowed = $true
                    break
                }
            }
            return $pythonAllowed -and (Test-PathInCommandLine -CommandLine $commandLine -ExpectedPath $scriptsRoot)
        }
    )
}

New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
$sha = [System.Security.Cryptography.SHA256]::Create()
try {
    $digest = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($repoRoot.ToUpperInvariant()))
}
finally {
    $sha.Dispose()
}
$mutexSuffix = -join ($digest[0..11] | ForEach-Object { $_.ToString("x2") })
$lifecycleMutex = [System.Threading.Mutex]::new($false, "Local\CodexSheLoveMeDashboard_$mutexSuffix")
$mutexAcquired = $false

try {
    try {
        $mutexAcquired = $lifecycleMutex.WaitOne([TimeSpan]::FromSeconds(30))
    }
    catch [System.Threading.AbandonedMutexException] {
        $mutexAcquired = $true
    }
    if (-not $mutexAcquired) {
        throw "Another Dashboard lifecycle operation is still in progress."
    }

    if (-not (Test-Path -LiteralPath $pidFile -PathType Leaf)) {
        [pscustomobject]@{
            status = "not_running"
            message = "No Dashboard state was found for this repository."
        } | ConvertTo-Json -Compress
        return
    }

    try {
        $record = Get-Content -LiteralPath $pidFile -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw "Dashboard state cannot be parsed; refusing to stop an unverified process."
    }

    $recordedPid = Get-RecordValue -Record $record -Name "pid"
    $recordedRepo = [string](Get-RecordValue -Record $record -Name "repo_root")
    $recordedStartTime = Get-RecordValue -Record $record -Name "started_at_utc"
    $recordedStartTicks = Get-RecordValue -Record $record -Name "started_at_utc_ticks"
    if ($null -eq $recordedPid -or [int]$recordedPid -le 0) {
        throw "Dashboard state has no valid process ID."
    }
    if ([string]::IsNullOrWhiteSpace($recordedRepo) -or
        -not ([System.IO.Path]::GetFullPath($recordedRepo)).Equals(
            $repoRoot,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
        throw "Dashboard state does not belong to this repository."
    }
    if ($null -eq $recordedStartTicks -and [string]::IsNullOrWhiteSpace([string]$recordedStartTime)) {
        throw "Dashboard state has no start time; refusing to risk PID reuse."
    }

    $processId = [int]$recordedPid
    $details = Get-ProcessDetails -ProcessId $processId
    if ($null -eq $details) {
        Remove-Item -LiteralPath $pidFile -Force
        [pscustomobject]@{
            status = "stale_state_removed"
            pid = $processId
            message = "The recorded Dashboard process no longer exists."
        } | ConvertTo-Json -Compress
        return
    }

    $allowedPythons = @(Resolve-PythonCandidates)
    if ($allowedPythons.Count -eq 0 -or -not (Test-DashboardProcess `
        -Details $details `
        -ExpectedRunner $expectedRunner `
        -AllowedPythons $allowedPythons)) {
        throw "The recorded running process failed Dashboard validation; refusing to stop it."
    }

    try {
        $actualStartTime = (Get-Process -Id $processId -ErrorAction Stop).StartTime.ToUniversalTime()
        if ($null -ne $recordedStartTicks) {
            $expectedStartTime = [datetime]::new([long]$recordedStartTicks, [System.DateTimeKind]::Utc)
        }
        elseif ($recordedStartTime -is [datetime]) {
            $expectedStartTime = $recordedStartTime.ToUniversalTime()
        }
        else {
            $expectedStartTime = [System.DateTimeOffset]::Parse([string]$recordedStartTime).UtcDateTime
        }
    }
    catch {
        throw "Unable to verify the Dashboard start time; refusing to risk PID reuse."
    }
    if ([math]::Abs(($actualStartTime - $expectedStartTime).TotalSeconds) -ge 3) {
        throw "The Dashboard PID start time does not match; refusing to stop it."
    }

    $children = @(Get-ValidatedChildProcesses -RootProcessId $processId -AllowedPythons $allowedPythons)
    foreach ($child in ($children | Sort-Object ProcessId -Descending)) {
        Stop-Process -Id ([int]$child.ProcessId) -Force -ErrorAction SilentlyContinue
    }
    Stop-Process -Id $processId -Force -ErrorAction Stop
    try {
        Wait-Process -Id $processId -Timeout 10 -ErrorAction SilentlyContinue
    }
    catch {
        # Windows PowerShell 5.1 does not expose Wait-Process -Timeout consistently.
    }
    if (Get-Process -Id $processId -ErrorAction SilentlyContinue) {
        throw "The Dashboard process did not exit in time; state was retained."
    }

    Remove-Item -LiteralPath $pidFile -Force
    [pscustomobject]@{
        status = "stopped"
        pid = $processId
        message = "The local Dashboard for this repository was stopped."
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
