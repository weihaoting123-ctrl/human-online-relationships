"""Desktop launch boundaries; all installation paths and processes are synthetic."""
import importlib
import io
import json
import os
import stat
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from dashboard import app, analysis as ai


class DesktopFixture(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('dashboard.copilot.desktop'),
                             'The fixed-target desktop launch service must exist')
        self.desktop = importlib.import_module('dashboard.copilot.desktop')
        scratch = Path(__file__).resolve().parents[1] / 'scripts' / 'tmp'
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic-desktop-', dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.main = self.root / 'desktop' / 'copilot'
        self.exe = self.main / 'node_modules' / 'electron' / 'dist' / 'electron.exe'
        self.exe.parent.mkdir(parents=True)
        self.exe.write_bytes(b'SYNTHETIC_NOT_EXECUTABLE')
        (self.main / 'main.cjs').write_text('// synthetic', encoding='utf-8')
        (self.main / 'package.json').write_text('{"main":"main.cjs"}', encoding='utf-8')
        self.clock = [100.0]
        self.child = mock.Mock()
        self.child.wait.side_effect = subprocess.TimeoutExpired('synthetic', 0.3)
        patches = [
            mock.patch.object(self.desktop.sys, 'platform', 'win32'),
            mock.patch.object(self.desktop.time, 'monotonic', side_effect=lambda: self.clock[0]),
            mock.patch.object(self.desktop.subprocess, 'Popen', return_value=self.child),
            mock.patch.object(self.desktop.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'stopped\n', '')),
        ]
        _, _, self.spawn, self.probe = [patch.start() for patch in patches]
        for patch in patches:
            self.addCleanup(patch.stop)
        self.desktop._ATTEMPTS.clear()
        self.desktop._CHILDREN.clear()
        self.addCleanup(self.desktop._CHILDREN.clear)
        self.addCleanup(self.desktop._ATTEMPTS.clear)

    def assert_dto(self, result, *, state, code, supported=True, installed=True):
        self.assertEqual(result, {'status': 'ok', 'desktop': {
            'supported': supported, 'installed': installed, 'state': state, 'code': code}})
        serialized = json.dumps(result)
        for private in ('SYNTHETIC_PRIVATE', str(self.root), 'pid', 'command', 'stdout', 'stderr'):
            self.assertNotIn(private, serialized)


class DesktopServiceTests(DesktopFixture):
    def test_status_reports_only_fixed_metadata_without_launching(self):
        before = set(self.root.rglob('*'))
        for state in ('running', 'stopped', 'unknown'):
            with self.subTest(state=state):
                self.probe.return_value.stdout = state + '\n'
                code = 'DESKTOP_STATUS_UNKNOWN' if state == 'unknown' else 'DESKTOP_' + state.upper()
                self.assert_dto(self.desktop.status(self.root, 8765), state=state, code=code)
        self.spawn.assert_not_called()
        self.assertEqual(before, set(self.root.rglob('*')))
        args, options = self.probe.call_args
        self.assertTrue(Path(args[0][0]).is_absolute())
        self.assertIn('-NoProfile', args[0])
        self.assertIn('-NonInteractive', args[0])
        self.assertIn(str(self.exe), args[0])
        self.assertIn(str(self.main), args[0])
        self.assertIn('8765', args[0])
        self.assertFalse(options['shell'])
        self.assertLessEqual(options['timeout'], 5)
        self.assertEqual(options['stdin'], subprocess.DEVNULL)
        self.assertEqual(options['stderr'], subprocess.DEVNULL)
        self.assertTrue(options['creationflags'] & 0x08000000)

    def test_unsupported_or_missing_installation_never_starts_any_process(self):
        with mock.patch.object(self.desktop.sys, 'platform', 'linux'):
            for operation in (self.desktop.status, self.desktop.launch):
                self.assert_dto(operation(self.root, 8765), state='unavailable',
                                code='DESKTOP_UNSUPPORTED', supported=False, installed=False)
        self.exe.unlink()
        for operation in (self.desktop.status, self.desktop.launch):
            self.assert_dto(operation(self.root, 8765), state='unavailable',
                            code='DESKTOP_NOT_INSTALLED', installed=False)
        self.spawn.assert_not_called()
        self.probe.assert_not_called()

    def test_missing_main_or_manifest_is_not_an_installed_app(self):
        for name in ('main.cjs', 'package.json'):
            target = self.main / name
            saved = target.read_bytes()
            target.unlink()
            try:
                self.assert_dto(self.desktop.launch(self.root, 8765), state='unavailable',
                                code='DESKTOP_NOT_INSTALLED', installed=False)
            finally:
                target.write_bytes(saved)
        self.spawn.assert_not_called()

    def test_probe_failure_timeout_and_untrusted_output_are_private_unknown_states(self):
        for failure, code in (
            (OSError('SYNTHETIC_PRIVATE path'), 'DESKTOP_PROBE_FAILED'),
            (subprocess.TimeoutExpired('SYNTHETIC_PRIVATE', 3, output='private'), 'DESKTOP_PROBE_TIMEOUT'),
        ):
            with self.subTest(code=code):
                self.probe.side_effect = failure
                self.assert_dto(self.desktop.status(self.root, 8765), state='unknown', code=code)
        self.probe.side_effect = None
        for output, returncode in [('running\nSYNTHETIC_PRIVATE', 0), ('running', 1), ('SYNTHETIC_PRIVATE', 0), ('x' * 4096, 0)]:
            self.probe.return_value = subprocess.CompletedProcess([], returncode, output, 'SYNTHETIC_PRIVATE')
            self.assert_dto(self.desktop.status(self.root, 8765), state='unknown', code='DESKTOP_PROBE_FAILED')
        self.spawn.assert_not_called()

    def test_launch_uses_only_fixed_target_server_port_and_hidden_private_stdio(self):
        with mock.patch.dict(os.environ, {'ELECTRON_RUN_AS_NODE': '1', 'NODE_OPTIONS': 'SYNTHETIC_PRIVATE'}):
            self.assert_dto(self.desktop.launch(self.root, 56789), state='launch_requested', code='DESKTOP_LAUNCH_REQUESTED')
        args, options = self.spawn.call_args
        self.assertEqual(args[0], [str(self.exe), str(self.main), '--port=56789', '--show'])
        self.assertEqual(options['cwd'], str(self.root))
        self.assertFalse(options['shell'])
        self.assertEqual(options['stdin'], subprocess.DEVNULL)
        self.assertEqual(options['stdout'], subprocess.DEVNULL)
        self.assertEqual(options['stderr'], subprocess.DEVNULL)
        self.assertTrue(options['creationflags'] & 0x08000000)
        self.assertNotIn('NODE_OPTIONS', options['env'])
        self.assertNotIn('ELECTRON_RUN_AS_NODE', options['env'])
        self.child.wait.assert_called_once()
        self.assertLessEqual(self.child.wait.call_args.kwargs['timeout'], 1)
        self.probe.assert_not_called()

    def test_existing_instance_handoff_exit_zero_is_only_acceptance(self):
        self.child.wait.side_effect = None
        self.child.wait.return_value = 0
        self.assert_dto(self.desktop.launch(self.root, 8765), state='launch_requested', code='DESKTOP_LAUNCH_REQUESTED')

    def test_spawn_failure_or_immediate_exit_cannot_claim_running_and_never_retries(self):
        self.spawn.side_effect = OSError('SYNTHETIC_PRIVATE executable path')
        first = self.desktop.launch(self.root, 8765)
        self.assert_dto(first, state='unknown', code='DESKTOP_LAUNCH_FAILED')
        self.assertEqual(self.desktop.launch(self.root, 8765), first)
        self.assertEqual(self.spawn.call_count, 1)
        self.clock[0] += 11
        self.spawn.side_effect = None
        self.child.wait.side_effect = None
        self.child.wait.return_value = 1
        self.assert_dto(self.desktop.launch(self.root, 8765), state='unknown', code='DESKTOP_LAUNCH_FAILED')

    def test_concurrent_and_cooldown_clicks_spawn_once_then_allow_a_later_explicit_request(self):
        started, release = threading.Event(), threading.Event()
        def spawn(*args, **kwargs):
            started.set()
            self.assertTrue(release.wait(3))
            return self.child
        self.spawn.side_effect = spawn
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.desktop.launch, self.root, 8765)
            self.assertTrue(started.wait(3))
            try:
                pending = pool.submit(self.desktop.launch, self.root, 8765).result(timeout=2)
                self.assert_dto(pending, state='launch_requested', code='DESKTOP_LAUNCH_PENDING')
            finally:
                release.set()
            self.assert_dto(first.result(timeout=3), state='launch_requested', code='DESKTOP_LAUNCH_REQUESTED')
        self.assert_dto(self.desktop.launch(self.root, 8765), state='launch_requested', code='DESKTOP_LAUNCH_PENDING')
        self.assertEqual(self.spawn.call_count, 1)
        self.clock[0] += 11
        self.desktop.launch(self.root, 8765)
        self.assertEqual(self.spawn.call_count, 2)

    def test_port_validation_rejects_dynamic_and_out_of_range_options(self):
        for port in ('8765', '--inspect', None, True, 0, 1, 1023, 65536):
            for operation in (self.desktop.status, self.desktop.launch):
                with self.subTest(port=port), self.assertRaises(ValueError):
                    operation(self.root, port)
        self.spawn.assert_not_called()
        self.probe.assert_not_called()

    def test_reparse_ancestor_is_rejected_before_launch_or_probe(self):
        real_lstat = Path.lstat
        def lstat(path, *args, **kwargs):
            if path == self.main / 'node_modules':
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            return real_lstat(path, *args, **kwargs)
        with mock.patch.object(Path, 'lstat', lstat):
            for operation in (self.desktop.status, self.desktop.launch):
                self.assert_dto(operation(self.root, 8765), state='unavailable',
                                code='DESKTOP_UNSAFE_PATH', installed=False)
        self.spawn.assert_not_called()
        self.probe.assert_not_called()


class DesktopApiTests(DesktopFixture):
    def setUp(self):
        super().setUp()
        patches = [mock.patch.object(app, 'REPO_ROOT', self.root),
                   mock.patch.object(app, 'DATA_DIR', self.root / 'data'),
                   mock.patch.object(app, 'CONTACTS_DIR', self.root / 'data' / 'contacts')]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.server = app.create_server(port=0, token='synthetic-desktop-token')
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def http(self, path, body=None, **headers):
        defaults = {'X-Local-Token': 'synthetic-desktop-token', 'Origin': self.base,
                    'Content-Type': 'application/json'}
        defaults.update(headers)
        defaults = {key: value for key, value in defaults.items() if value is not None}
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request('GET' if body is None else 'POST', path, body=body, headers=defaults)
            response = connection.getresponse()
            return response.status, json.loads(response.read()), dict(response.getheaders())
        finally:
            connection.close()

    def test_routes_use_fixed_root_and_bound_port_without_config_library_or_model(self):
        with mock.patch.object(app, 'module_registry', side_effect=AssertionError('module access')), \
             mock.patch.object(app, 'library_service', side_effect=AssertionError('archive access')), \
             mock.patch.object(app, 'list_bundles', side_effect=AssertionError('archive scan')), \
             mock.patch.object(ai, '_source', side_effect=AssertionError('message access')), \
             mock.patch.object(ai, 'config_status', side_effect=AssertionError('configuration access')), \
             mock.patch.object(ai, '_request_json', side_effect=AssertionError('cloud access')):
            code, result, headers = self.http('/api/copilot/desktop')
            self.assertEqual(code, 200)
            self.assert_dto(result, state='stopped', code='DESKTOP_STOPPED')
            self.spawn.assert_not_called()
            self.assertNotIn('Access-Control-Allow-Origin', headers)
            code, result, _ = self.http('/api/copilot/desktop/launch', b'{}')
            self.assertEqual(code, 202)
            self.assert_dto(result, state='launch_requested', code='DESKTOP_LAUNCH_REQUESTED')
        self.assertEqual(self.spawn.call_args.args[0],
                         [str(self.exe), str(self.main), f'--port={self.server.server_port}', '--show'])

    def test_session_host_and_origin_guards_block_process_operations(self):
        for path, body in [('/api/copilot/desktop', None), ('/api/copilot/desktop/launch', b'{}')]:
            for headers, expected in [({'X-Local-Token': None}, 403), ({'Host': 'evil.invalid'}, 421),
                                      ({'Origin': 'https://evil.invalid'}, 403)]:
                with self.subTest(path=path, headers=headers):
                    self.assertEqual(self.http(path, body, **headers)[0], expected)
        for headers in ({'Origin': None}, {'Origin': 'null'},
                        {'Host': f'localhost:{self.server.server_port}'},
                        {'Origin': self.base + '/'}):
            self.assertEqual(self.http('/api/copilot/desktop/launch', b'{}', **headers)[0], 403)
        self.spawn.assert_not_called()
        self.probe.assert_not_called()

    def test_httponly_browser_session_can_request_launch(self):
        session = self.server.issue_session()
        code, result, _ = self.http('/api/copilot/desktop/launch', b'{}',
            **{'X-Local-Token': None, 'Cookie': f'{app.SESSION_COOKIE}={session}'})
        self.assertEqual(code, 202)
        self.assertEqual(result['desktop']['state'], 'launch_requested')

    def test_exact_empty_object_rejects_all_dynamic_options_and_malformed_json(self):
        for body in (b'[]', b'null', b'true', b'1', b'""', b'', b'{', b'{}{}', b'\xff',
                     b'{"port":1234}', b'{"path":"SYNTHETIC_PRIVATE"}', b'{"args":[]}',
                     b'{"url":"https://evil.invalid"}', b'{"env":{}}', b'{"exe":"x"}'):
            with self.subTest(body=body):
                code, result, _ = self.http('/api/copilot/desktop/launch', body)
                self.assertEqual(code, 400)
                self.assertNotIn('SYNTHETIC_PRIVATE', json.dumps(result))
        self.spawn.assert_not_called()

    def test_query_fragment_path_suffix_and_encoded_endpoint_never_launch(self):
        for suffix in ('?port=1', '?', '#fragment', '#', ';param'):
            self.assertEqual(self.http('/api/copilot/desktop/launch' + suffix, b'{}')[0], 400)
        for path in ('/api/copilot/desktop/launch/', '/api/copilot/desktop/%6caunch',
                     '/api/copilot/desktop/../desktop/launch'):
            self.assertEqual(self.http(path, b'{}')[0], 404)
        self.assertEqual(self.http('/api/copilot/desktop?path=SYNTHETIC_PRIVATE')[0], 400)
        self.spawn.assert_not_called()
        self.probe.assert_not_called()

    def test_request_size_and_framing_are_rejected_before_any_body_read(self):
        for length, transfer, duplicate in [('1025', None, False), ('99999999', None, False),
                ('0', None, False), ('-1', None, False), ('PRIVATE', None, False),
                ('2', 'chunked', False), ('2', None, True)]:
            handler = object.__new__(app.DashboardHandler)
            handler.server = self.server
            handler.path = '/api/copilot/desktop/launch'
            handler.headers = Message()
            for key, value in {'Host': f'127.0.0.1:{self.server.server_port}', 'Origin': self.base,
                               'X-Local-Token': 'synthetic-desktop-token', 'Content-Length': length}.items():
                handler.headers[key] = value
            if transfer:
                handler.headers['Transfer-Encoding'] = transfer
            if duplicate:
                handler.headers['Content-Length'] = length
            handler.rfile = mock.Mock(spec=io.BytesIO)
            handler.rfile.read.side_effect = AssertionError('Body must not be read')
            handler._json = mock.Mock()
            handler.do_POST()
            self.assertEqual(handler._json.call_args.args[1], 400)
            handler.rfile.read.assert_not_called()
        self.spawn.assert_not_called()

    def test_exact_1024_byte_empty_object_is_accepted_and_1025_rejected(self):
        self.assertEqual(self.http('/api/copilot/desktop/launch', b' ' * 1022 + b'{}')[0], 202)
        self.assertEqual(self.http('/api/copilot/desktop/launch', b' ' * 1023 + b'{}')[0], 400)

    def test_truncated_request_cannot_launch_even_if_its_partial_json_is_empty(self):
        handler = object.__new__(app.DashboardHandler)
        handler.server = self.server
        handler.path = '/api/copilot/desktop/launch'
        handler.headers = Message()
        for key, value in {'Host': f'127.0.0.1:{self.server.server_port}', 'Origin': self.base,
                           'X-Local-Token': 'synthetic-desktop-token', 'Content-Length': '3'}.items():
            handler.headers[key] = value
        handler.rfile = io.BytesIO(b'{}')
        handler._json = mock.Mock()
        handler.do_POST()
        self.assertEqual(handler._json.call_args.args[1], 400)
        self.spawn.assert_not_called()


class DesktopProbeTests(unittest.TestCase):
    def test_probe_is_present_and_matches_only_synthetic_fixed_process_rows(self):
        source = Path(__file__).resolve().parents[1] / 'dashboard' / 'copilot' / 'desktop_probe.ps1'
        self.assertTrue(source.is_file(), 'The bounded fixed-source process probe must exist')
        source.read_bytes().decode('ascii')
        if os.name != 'nt':
            self.skipTest('Windows command-line parsing requires Windows')
        # The lowest process-query boundary is replaced before the helper runs.
        # No native inventory, account, title, or window is queried by this test.
        harness = r'''
param($source)
$ErrorActionPreference = 'Stop'
$exe = 'C:\Synthetic Fixture\desktop\copilot\node_modules\electron\dist\electron.exe'
$main = 'C:\Synthetic Fixture\desktop\copilot'
$global:syntheticProbeRows = @()
$global:syntheticProbeFail = $false
function Get-CimInstance {
    param($ClassName, $Filter, $Property, $OperationTimeoutSec)
    if ($ClassName -ne 'Win32_Process' -or $Filter -ne ("ExecutablePath = '" + $exe.Replace('\', '\\') + "'")) { throw 'BAD_QUERY' }
    if (($Property -join ',') -ne 'ExecutablePath,CommandLine' -or $OperationTimeoutSec -gt 3) { throw 'BAD_FIELDS' }
    if ($global:syntheticProbeFail) { throw 'SYNTHETIC_PRIVATE_ERROR' }
    return $global:syntheticProbeRows
}
function Expect($expected) {
    $value = & $source -Executable $exe -Main $main -Port 8765
    if ($value -cne $expected) { throw ('EXPECTED_' + $expected + '_GOT_' + $value + ':' + $Error[0]) }
}
Expect 'stopped'
$global:syntheticProbeRows = @([pscustomobject]@{ExecutablePath=$exe; CommandLine=('"' + $exe + '" "' + $main + '" --port=8765')})
Expect 'running'
$global:syntheticProbeRows[0].CommandLine += ' --show'
Expect 'running'
$global:syntheticProbeRows[0].CommandLine = ('"' + $exe + '" "' + $main + '"')
Expect 'running'
$global:syntheticProbeRows[0].CommandLine = ('"' + $exe + '" "' + $main + '" --port=9876')
Expect 'unknown'
$global:syntheticProbeRows[0].CommandLine = ('"' + $exe + '" "' + $main + '" --port=8765 --inspect')
Expect 'unknown'
$global:syntheticProbeRows[0].CommandLine = ('"' + $exe + '" --type=renderer "' + $main + '"')
Expect 'stopped'
$global:syntheticProbeRows[0].CommandLine = ('"' + $exe + '" "' + $main + '-unrelated" --port=8765')
Expect 'stopped'
$global:syntheticProbeRows[0].CommandLine = ''
Expect 'unknown'
$global:syntheticProbeFail = $true
Expect 'unknown'
[Console]::WriteLine('SYNTHETIC_PROBE_OK')
'''
        command = '& {\n' + harness + "\n} '" + str(source).replace("'", "''") + "'"
        shell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
        result = subprocess.run([str(shell), '-NoProfile', '-NonInteractive', '-Command', command],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr[-1800:])
        self.assertEqual(result.stdout.strip(), 'SYNTHETIC_PROBE_OK')


if __name__ == '__main__':
    unittest.main()
