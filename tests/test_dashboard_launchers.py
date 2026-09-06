import shutil
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
START_SCRIPT = REPO_ROOT / "start_dashboard.ps1"
STOP_SCRIPT = REPO_ROOT / "stop_dashboard.ps1"
WATCHER_SCRIPT = REPO_ROOT / "start_wechat_watcher.ps1"


class DashboardLauncherTests(unittest.TestCase):
    def test_launchers_have_local_only_and_safe_process_guards(self):
        start = START_SCRIPT.read_text(encoding="utf-8")
        stop = STOP_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("127.0.0.1", start)
        self.assertNotIn("0.0.0.0", start)
        self.assertIn("-WindowStyle Hidden", start)
        self.assertIn("requirements.txt", start)
        self.assertIn(".venv", start)
        self.assertIn("dashboard-process.json", start)

        self.assertIn("dashboard-process.json", stop)
        self.assertIn("Get-CimInstance", stop)
        self.assertIn("CommandLine", stop)
        self.assertIn("ExecutablePath", stop)
        self.assertIn("StartTime", stop)
        self.assertIn("Stop-Process -Id", stop)
        self.assertNotIn("Get-Process -Name", stop)

    def test_dashboard_start_and_stop_share_a_lifecycle_mutex(self):
        start = START_SCRIPT.read_text(encoding="utf-8")
        stop = STOP_SCRIPT.read_text(encoding="utf-8")
        mutex_name = "Local\\CodexSheLoveMeDashboard_$mutexSuffix"
        self.assertIn(mutex_name, start)
        self.assertIn(mutex_name, stop)
        self.assertIn("WaitOne", start)
        self.assertIn("WaitOne", stop)
        self.assertIn("ReleaseMutex", start)
        self.assertIn("ReleaseMutex", stop)

    def test_process_inventory_failures_are_fail_closed(self):
        for script in (START_SCRIPT, STOP_SCRIPT, WATCHER_SCRIPT):
            source = script.read_text(encoding="utf-8")
            self.assertIn("Windows process state could not be queried safely", source)
            self.assertNotIn("catch {\n        return $null\n    }", source)

    def test_dashboard_start_rolls_back_without_exposing_logs_or_state_paths(self):
        start = START_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("Stop-NewDashboardProcesses", start)
        self.assertNotIn("Get-Content -LiteralPath $stderrLog -Raw", start)
        self.assertNotIn("pid_file = $pidFile", start)

    def test_dependency_install_runs_only_after_local_probe_fails(self):
        start = START_SCRIPT.read_text(encoding="utf-8")
        probe = start.index("& $venvPython -c $dependencyProbe")
        install = start.index("-m pip install")
        self.assertLess(probe, install)
        self.assertIn("if ($LASTEXITCODE -ne 0)", start[probe:install])

    def test_stop_handles_validated_dashboard_children(self):
        stop = STOP_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("Get-ValidatedChildProcesses", stop)
        self.assertIn("ParentProcessId", stop)
        self.assertIn("Test-PathInCommandLine -CommandLine $commandLine -ExpectedPath $scriptsRoot", stop)

    def test_watcher_launcher_has_mutex_recovery_and_owned_rollback(self):
        watcher = WATCHER_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("Local\\CodexSheLoveMeWatcher_$mutexSuffix", watcher)
        self.assertIn("Get-WatcherProcesses", watcher)
        self.assertIn("Write-WatcherRecord", watcher)
        self.assertIn("Stop-NewWatcherProcesses", watcher)

    def test_launchers_parse_as_powershell_when_available(self):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            self.skipTest("PowerShell is not installed on this test host")

        for script in (START_SCRIPT, STOP_SCRIPT):
            escaped = str(script).replace("'", "''")
            command = (
                "$ErrorActionPreference='Stop'; "
                f"[scriptblock]::Create((Get-Content -LiteralPath '{escaped}' -Raw)) | Out-Null"
            )
            result = subprocess.run(
                [shell, "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_launchers_are_ascii_for_legacy_windows_powershell(self):
        # Windows PowerShell 5.1 treats UTF-8 files without a BOM as ANSI; a
        # multibyte sequence can consume a quote byte and turn valid code into
        # a parser error. Keep executable launcher text portable and ASCII-only.
        for script in (START_SCRIPT, STOP_SCRIPT):
            script.read_bytes().decode("ascii")


if __name__ == "__main__":
    unittest.main()
