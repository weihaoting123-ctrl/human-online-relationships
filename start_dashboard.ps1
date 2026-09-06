[CmdletBinding()]
param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 8765
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$runnerPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot "scripts\run_dashboard.py"))
$requirementsPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot "requirements.txt"))
$venvDir = [System.IO.Path]::GetFullPath((Join-Path $repoRoot ".venv"))
$venvPython = [System.IO.Path]::GetFullPath((Join-Path $venvDir "Scripts\python.exe"))
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

function Test-DashboardProcess {
    param(
        [Parameter(Mandatory = $true)]$Details,
        [Parameter(Mandatory = $true)][string]$ExpectedRunner,
        [string[]]$AllowedPythons = @(),
        [int]$ExpectedPort = 0
    )
    $commandLine = [string]$Details.CommandLine
    $executablePath = [string]$Details.ExecutablePath
    if ([string]::IsNullOrWhiteSpace($commandLine) -or [string]::IsNullOrWhiteSpace($executablePath)) {
        return $false
    }
    if ([System.IO.Path]::GetFileName($executablePath) -notin @("python.exe", "python")) {
        return $false
    }
    if ($AllowedPythons.Count -gt 0) {
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
        if (-not $allowed) {
            return $false
        }
    }
    if (-not (Test-PathInCommandLine -CommandLine $commandLine -ExpectedPath $ExpectedRunner)) {
        return $false
    }
    $hostPattern = '(?i)(?:^|\s)--host(?:=|\s+)["'']?127\.0\.0\.1(?:["'']?)(?:\s|$)'
    if ($commandLine -notmatch $hostPattern) {
        return $false
    }
    $actualPort = Get-DashboardPort -CommandLine $commandLine
    if ($actualPort -eq 0) {
        return $false
    }
    return $ExpectedPort -eq 0 -or $actualPort -eq $ExpectedPort
}

function Get-DashboardProcesses {
    param(
        [Parameter(Mandatory = $true)][string]$ExpectedRunner,
        [string[]]$AllowedPythons = @(),
        [int]$ExpectedPort = 0
    )
    return @(
        Get-AllProcessDetails | Where-Object {
            Test-DashboardProcess `
                -Details $_ `
                -ExpectedRunner $ExpectedRunner `
                -AllowedPythons $AllowedPythons `
                -ExpectedPort $ExpectedPort
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

function Resolve-PythonCandidates {
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        return @()
    }
    $baseText = [string](& $venvPython -c "import sys; print(getattr(sys, '_base_executable', None) or sys.executable)")
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($baseText)) {
        throw "Unable to resolve the virtual environment Python executable."
    }
    return @($venvPython, [System.IO.Path]::GetFullPath($baseText.Trim())) | Select-Object -Unique
}

function Stop-NewDashboardProcesses {
    param(
        [Parameter(Mandatory = $true)][datetime]$LaunchedAfterUtc,
        [Parameter(Mandatory = $true)][string]$ExpectedRunner,
        [Parameter(Mandatory = $true)][string[]]$AllowedPythons,
        [Parameter(Mandatory = $true)][int]$ExpectedPort
    )
    try {
        $candidates = Get-DashboardProcesses `
            -ExpectedRunner $ExpectedRunner `
            -AllowedPythons $AllowedPythons `
            -ExpectedPort $ExpectedPort
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
            # Cleanup is best effort and only targets a validated new process.
        }
    }
}

if (-not (Test-Path -LiteralPath $runnerPath -PathType Leaf)) {
    throw "Dashboard entry point not found."
}
if (-not (Test-Path -LiteralPath $requirementsPath -PathType Leaf)) {
    throw "Requirements file not found."
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

    if ($recordInvalid) {
        $untracked = @(Get-DashboardProcesses -ExpectedRunner $runnerPath)
        if ($untracked.Count -gt 0) {
            throw "Dashboard state is damaged while a matching process is running; refusing to start another."
        }
        $quarantine = Join-Path $stateDir ("dashboard-process.invalid-" + [guid]::NewGuid().ToString("N") + ".json")
        Move-Item -LiteralPath $pidFile -Destination $quarantine
        $record = $null
    }

    if ($null -ne $record) {
        $recordedPid = Get-RecordValue -Record $record -Name "pid"
        if ($null -eq $recordedPid -or [int]$recordedPid -le 0) {
            throw "Dashboard state has no valid process ID."
        }
        $details = Get-ProcessDetails -ProcessId ([int]$recordedPid)
        if ($null -eq $details) {
            Remove-Item -LiteralPath $pidFile -Force
            $record = $null
        }
        else {
            $allowedPythons = @(Resolve-PythonCandidates)
            if ($allowedPythons.Count -eq 0) {
                throw "Dashboard Python cannot be verified while a recorded process is running."
            }
            $recordedStartTime = Get-RecordValue -Record $record -Name "started_at_utc"
            $recordedStartTicks = Get-RecordValue -Record $record -Name "started_at_utc_ticks"
            $matches = (
                ($null -ne $recordedStartTicks -or -not [string]::IsNullOrWhiteSpace([string]$recordedStartTime)) -and
                (Test-DashboardProcess `
                    -Details $details `
                    -ExpectedRunner $runnerPath `
                    -AllowedPythons $allowedPythons) -and
                (Test-RecordedStartTime `
                    -ProcessId ([int]$recordedPid) `
                    -RecordedStartTime $recordedStartTime `
                    -RecordedStartTicks $recordedStartTicks)
            )
            if (-not $matches) {
                throw "The recorded running process failed Dashboard validation; refusing to start a second instance."
            }
            $actualPort = Get-DashboardPort -CommandLine ([string]$details.CommandLine)
            [pscustomobject]@{
                status = "already_running"
                url = "http://127.0.0.1:$actualPort/"
                pid = [int]$recordedPid
                local_only = $true
            } | ConvertTo-Json -Compress
            return
        }
    }

    $untracked = @(Get-DashboardProcesses -ExpectedRunner $runnerPath)
    if ($untracked.Count -gt 0) {
        throw "An untracked Dashboard for this repository is already running; refusing to start another."
    }

    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        $pythonLauncher = Get-Command "py" -ErrorAction SilentlyContinue
        if ($null -ne $pythonLauncher) {
            & $pythonLauncher.Source -3 -m venv $venvDir
        }
        else {
            $pythonCommand = Get-Command "python" -ErrorAction SilentlyContinue
            if ($null -eq $pythonCommand) {
                throw "Python 3 was not found. Install Python 3.9 or newer."
            }
            & $pythonCommand.Source -m venv $venvDir
        }
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to create the virtual environment."
        }
    }
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        throw "Python is missing from the virtual environment."
    }

    & $venvPython -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 2)"
    if ($LASTEXITCODE -ne 0) {
        throw "The local dashboard requires Python 3.9 or newer."
    }
    $allowedPythons = @(Resolve-PythonCandidates)

    $dependencyProbe = "import Crypto, zstandard, sqlcipher3, pymem"
    & $venvPython -c $dependencyProbe 2>$null
    if ($LASTEXITCODE -ne 0) {
        & $venvPython -m pip install --quiet --disable-pip-version-check -r $requirementsPath 2>$null
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to install project dependencies."
        }
        & $venvPython -c $dependencyProbe 2>$null
        if ($LASTEXITCODE -ne 0) {
            throw "Project dependencies are still unavailable after installation."
        }
    }

    $runId = [guid]::NewGuid().ToString("N")
    $stdoutLog = Join-Path $stateDir "dashboard-$runId.stdout.log"
    $stderrLog = Join-Path $stateDir "dashboard-$runId.stderr.log"
    $dashboardUrl = "http://127.0.0.1:$Port/"
    $argumentLine = '"{0}" --host 127.0.0.1 --port {1} --no-browser' -f $runnerPath, $Port
    $launchTimeUtc = [DateTime]::UtcNow
    $dashboardProcess = $null
    try {
        $dashboardProcess = Start-Process `
            -FilePath $venvPython `
            -ArgumentList $argumentLine `
            -WorkingDirectory $repoRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdoutLog `
            -RedirectStandardError $stderrLog `
            -PassThru

        $ready = $false
        $serverProcessId = 0
        $deadline = [DateTime]::UtcNow.AddSeconds(20)
        do {
            Start-Sleep -Milliseconds 200
            if (Test-Path -LiteralPath $stdoutLog -PathType Leaf) {
                try {
                    $readyRecord = Get-Content -LiteralPath $stdoutLog -Raw -Encoding UTF8 | ConvertFrom-Json
                    if ($readyRecord.status -eq "ready" -and
                        [int]$readyRecord.pid -gt 0 -and
                        [string]$readyRecord.url -eq $dashboardUrl -and
                        [bool]$readyRecord.local_only) {
                        $serverProcessId = [int]$readyRecord.pid
                        $ready = $true
                    }
                }
                catch {
                    # The redirected JSON line can be incomplete while Python flushes it.
                }
            }
        } while (-not $ready -and [DateTime]::UtcNow -lt $deadline)

        if (-not $ready -or $serverProcessId -le 0) {
            throw "Dashboard readiness timed out."
        }
        $details = Get-ProcessDetails -ProcessId $serverProcessId
        if ($null -eq $details -or -not (Test-DashboardProcess `
            -Details $details `
            -ExpectedRunner $runnerPath `
            -AllowedPythons $allowedPythons `
            -ExpectedPort $Port)) {
            throw "Dashboard process validation failed."
        }

        $serverStartTime = (Get-Process -Id $serverProcessId -ErrorAction Stop).StartTime.ToUniversalTime()
        $state = [ordered]@{
            version = 1
            pid = $serverProcessId
            url = $dashboardUrl
            host = "127.0.0.1"
            port = $Port
            local_only = $true
            repo_root = $repoRoot
            runner = $runnerPath
            python = [System.IO.Path]::GetFullPath([string]$details.ExecutablePath)
            venv_python = $venvPython
            started_at_utc = $serverStartTime.ToString("o")
            started_at_utc_ticks = $serverStartTime.Ticks
            stdout_log = $stdoutLog
            stderr_log = $stderrLog
        }
        $temporaryPidFile = "$pidFile.$serverProcessId.tmp"
        try {
            $state | ConvertTo-Json | Set-Content -LiteralPath $temporaryPidFile -Encoding UTF8
            Move-Item -LiteralPath $temporaryPidFile -Destination $pidFile -Force
        }
        finally {
            if (Test-Path -LiteralPath $temporaryPidFile -PathType Leaf) {
                Remove-Item -LiteralPath $temporaryPidFile -Force -ErrorAction SilentlyContinue
            }
        }
    }
    catch {
        Stop-NewDashboardProcesses `
            -LaunchedAfterUtc $launchTimeUtc `
            -ExpectedRunner $runnerPath `
            -AllowedPythons $allowedPythons `
            -ExpectedPort $Port
        throw "The local dashboard did not start safely within 20 seconds."
    }

    [pscustomobject]@{
        status = "started"
        url = $dashboardUrl
        pid = $serverProcessId
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
