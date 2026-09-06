import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import bootstrap_local_exporter as bootstrap


class LocalExporterBootstrapTests(unittest.TestCase):
    @contextmanager
    def isolated_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            runtime = root / ".runtime" / "weflow-cli"
            with patch.object(bootstrap, "REPO_ROOT", root), patch.object(
                bootstrap, "RUNTIME_DIR", runtime
            ):
                yield root, runtime

    def write_manifest(self, runtime):
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / "package.json").write_text(
            json.dumps(bootstrap.PACKAGE_JSON, indent=2) + "\n",
            encoding="utf-8",
        )

    def write_lock(self, runtime, resolved=None, integrity=None, version=None):
        runtime.mkdir(parents=True, exist_ok=True)
        payload = {
            "name": bootstrap.RUNTIME_PACKAGE_NAME,
            "version": bootstrap.RUNTIME_PACKAGE_VERSION,
            "lockfileVersion": 3,
            "requires": True,
            "packages": {
                "": {
                    "name": bootstrap.RUNTIME_PACKAGE_NAME,
                    "version": bootstrap.RUNTIME_PACKAGE_VERSION,
                    "dependencies": {
                        bootstrap.PACKAGE_NAME: bootstrap.PACKAGE_VERSION,
                    },
                },
                f"node_modules/{bootstrap.PACKAGE_NAME}": {
                    "version": version or bootstrap.PACKAGE_VERSION,
                    "resolved": resolved or (
                        "https://registry.npmjs.org/weflow-cli/"
                        "-/weflow-cli-1.5.0.tgz"
                    ),
                    "integrity": integrity or bootstrap.PACKAGE_INTEGRITY,
                },
                "node_modules/example-transitive": {
                    "version": "1.0.0",
                    "resolved": (
                        "https://registry.npmjs.org/example-transitive/"
                        "-/example-transitive-1.0.0.tgz"
                    ),
                    "integrity": "sha512-dGVzdA==",
                },
            },
        }
        (runtime / "package-lock.json").write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )

    def write_installed_package(self, runtime, version=None, entry="cli.cjs"):
        package_root = runtime / "node_modules" / bootstrap.PACKAGE_NAME
        package_root.mkdir(parents=True, exist_ok=True)
        (package_root / "package.json").write_text(
            json.dumps({
                "name": bootstrap.PACKAGE_NAME,
                "version": version or bootstrap.PACKAGE_VERSION,
                "bin": {bootstrap.PACKAGE_NAME: entry},
            }),
            encoding="utf-8",
        )
        if entry == "cli.cjs":
            (package_root / entry).write_text("// fixture\n", encoding="utf-8")

    def test_policy_is_exact_local_and_official(self):
        self.assertEqual(bootstrap.PACKAGE_NAME, "weflow-cli")
        self.assertEqual(bootstrap.PACKAGE_VERSION, "1.5.0")
        self.assertEqual(bootstrap.PACKAGE_SPEC, "weflow-cli@1.5.0")
        self.assertEqual(bootstrap.OFFICIAL_REGISTRY, "https://registry.npmjs.org/")
        self.assertEqual(
            bootstrap.RUNTIME_DIR,
            REPO_ROOT / ".runtime" / "weflow-cli",
        )
        source = (SCRIPTS_DIR / "bootstrap_local_exporter.py").read_text(
            encoding="utf-8"
        ).lower()
        self.assertNotIn("ciphertalk", source)

    def test_default_mode_only_checks_and_does_not_create_or_execute(self):
        with self.isolated_runtime() as (root, runtime), patch(
            "bootstrap_local_exporter.subprocess.run"
        ) as run:
            output = io.StringIO()
            with redirect_stdout(output):
                return_code = bootstrap.main([])

            self.assertEqual(return_code, 1)
            self.assertFalse((root / ".runtime").exists())
            run.assert_not_called()
            report = json.loads(output.getvalue())
            self.assertEqual(report["status"], "missing")
            self.assertFalse(report["ready"])
            self.assertFalse(report["changed"])
            self.assertEqual(report["next_action"], "rerun_with_install")
            self.assertNotIn(str(runtime), output.getvalue())

    def test_install_generates_lock_and_uses_only_safe_local_npm_commands(self):
        calls = []
        with self.isolated_runtime() as (_, runtime), patch.object(
            bootstrap, "_find_node", return_value="C:/safe/node.exe"
        ), patch.object(
            bootstrap, "_find_npm", return_value="C:/safe/npm.cmd"
        ):
            def fake_run(command, cwd, env, timeout, stage):
                calls.append((list(command), Path(cwd), dict(env), stage))
                if stage == "node_check":
                    return "v24.18.0"
                if stage == "lock_generation":
                    self.write_lock(runtime)
                    return "raw npm output that must remain private"
                if stage == "package_installation":
                    self.write_installed_package(runtime)
                    return "another private npm log"
                self.fail(f"unexpected stage: {stage}")

            with patch.object(bootstrap, "_run_captured", side_effect=fake_run):
                self.assertTrue(bootstrap.install_runtime())

            self.assertTrue(bootstrap.verify_runtime_manifest(runtime))
            self.assertTrue(bootstrap.verify_lockfile(runtime))
            self.assertTrue(bootstrap.verify_installed_package(runtime))
            self.assertIn(
                f"registry={bootstrap.OFFICIAL_REGISTRY}",
                (runtime / ".npmrc").read_text(encoding="utf-8"),
            )

            npm_calls = [item for item in calls if item[3] != "node_check"]
            self.assertEqual([item[3] for item in npm_calls], [
                "lock_generation", "package_installation",
            ])
            for command, cwd, environment, _ in npm_calls:
                self.assertEqual(cwd, runtime)
                self.assertIn("--ignore-scripts", command)
                self.assertIn(
                    f"--registry={bootstrap.OFFICIAL_REGISTRY}", command
                )
                self.assertIn("--prefix", command)
                self.assertNotIn("-g", command)
                self.assertNotIn("--global", command)
                self.assertEqual(
                    environment["NPM_CONFIG_REGISTRY"],
                    bootstrap.OFFICIAL_REGISTRY,
                )
                self.assertEqual(
                    environment["NPM_CONFIG_IGNORE_SCRIPTS"], "true"
                )

            lock_command = npm_calls[0][0]
            self.assertEqual(lock_command[1], "install")
            self.assertIn("--package-lock-only", lock_command)
            self.assertIn("--save-exact", lock_command)
            self.assertIn(bootstrap.PACKAGE_SPEC, lock_command)
            self.assertEqual(npm_calls[1][0][1], "ci")

    def test_existing_valid_lock_is_verified_and_reused(self):
        stages = []
        with self.isolated_runtime() as (_, runtime):
            self.write_manifest(runtime)
            self.write_lock(runtime)

            def fake_run(command, cwd, env, timeout, stage):
                stages.append(stage)
                if stage == "node_check":
                    return "v22.12.0"
                if stage == "package_installation":
                    self.write_installed_package(runtime)
                    return ""
                self.fail(f"unexpected stage: {stage}")

            with patch.object(bootstrap, "_find_node", return_value="node"), patch.object(
                bootstrap, "_find_npm", return_value="npm"
            ), patch.object(bootstrap, "_run_captured", side_effect=fake_run):
                bootstrap.install_runtime()

        self.assertEqual(stages, ["node_check", "package_installation"])

    def test_lock_rejects_wrong_version_integrity_and_registry(self):
        with self.isolated_runtime() as (_, runtime):
            self.write_manifest(runtime)

            self.write_lock(runtime, version="1.5.1")
            self.assertFalse(bootstrap.verify_lockfile(runtime))

            self.write_lock(runtime, integrity="sha512-wrong")
            self.assertFalse(bootstrap.verify_lockfile(runtime))

            self.write_lock(
                runtime,
                resolved=(
                    "https://registry.npmmirror.com/weflow-cli/"
                    "-/weflow-cli-1.5.0.tgz"
                ),
            )
            self.assertFalse(bootstrap.verify_lockfile(runtime))

    def test_install_refuses_an_existing_invalid_lock(self):
        with self.isolated_runtime() as (_, runtime):
            self.write_manifest(runtime)
            self.write_lock(runtime, integrity="sha512-untrusted")
            with patch.object(bootstrap, "_find_node", return_value="node"), patch.object(
                bootstrap, "_find_npm", return_value="npm"
            ), patch.object(bootstrap, "_validate_node"), patch.object(
                bootstrap, "_run_captured"
            ) as run:
                with self.assertRaises(bootstrap.BootstrapError) as raised:
                    bootstrap.install_runtime()
            self.assertEqual(raised.exception.code, "lock_invalid")
            run.assert_not_called()

    def test_installed_package_requires_exact_version_and_safe_entry(self):
        with self.isolated_runtime() as (_, runtime):
            self.write_installed_package(runtime, version="1.5.1")
            self.assertFalse(bootstrap.verify_installed_package(runtime))

            self.write_installed_package(runtime, entry="../outside.cjs")
            self.assertFalse(bootstrap.verify_installed_package(runtime))

    def test_sensitive_environment_is_not_forwarded_to_npm(self):
        with self.isolated_runtime() as (_, runtime), patch.dict(os.environ, {
            "QCE_TOKEN": "private-token",
            "DEEPSEEK_API_KEY": "private-key",
            "NPM_TOKEN": "private-npm-token",
        }, clear=False):
            environment = bootstrap._sanitized_npm_environment(runtime)

        self.assertNotIn("QCE_TOKEN", environment)
        self.assertNotIn("DEEPSEEK_API_KEY", environment)
        self.assertNotIn("NPM_TOKEN", environment)
        self.assertEqual(
            environment["NPM_CONFIG_USERCONFIG"], str(runtime / ".npmrc")
        )
        self.assertEqual(
            environment["NPM_CONFIG_GLOBALCONFIG"],
            str(runtime / ".npmrc-global"),
        )

    def test_install_rejects_npm_shrinkwrap_control_file(self):
        with self.isolated_runtime() as (_, runtime):
            runtime.mkdir(parents=True)
            (runtime / "npm-shrinkwrap.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(bootstrap.BootstrapError) as raised:
                bootstrap.install_runtime()
        self.assertEqual(
            raised.exception.code, "unexpected_npm_control_file"
        )

    def test_raw_child_logs_and_local_paths_are_never_printed_on_failure(self):
        secret_stdout = "private chat body from npm child"
        secret_stderr = "QCE_TOKEN=do-not-print"
        with self.isolated_runtime() as (_, runtime), patch.object(
            bootstrap, "_find_node", return_value="C:/Users/private/node.exe"
        ), patch.object(
            bootstrap, "_find_npm", return_value="C:/Users/private/npm.cmd"
        ):
            responses = [
                subprocess.CompletedProcess([], 0, "v24.18.0\n", ""),
                subprocess.CompletedProcess([], 1, secret_stdout, secret_stderr),
            ]
            output = io.StringIO()
            with patch(
                "bootstrap_local_exporter.subprocess.run", side_effect=responses
            ), redirect_stdout(output):
                return_code = bootstrap.main(["--install"])

        rendered = output.getvalue()
        report = json.loads(rendered)
        self.assertEqual(return_code, 1)
        self.assertEqual(report["error_code"], "lock_generation_failed")
        self.assertNotIn(secret_stdout, rendered)
        self.assertNotIn(secret_stderr, rendered)
        self.assertNotIn(str(runtime), rendered)
        self.assertNotIn("C:/Users/private", rendered)

    def test_invalid_arguments_are_not_echoed(self):
        secret_argument = "--QCE_TOKEN=do-not-print"
        output = io.StringIO()
        with redirect_stdout(output):
            return_code = bootstrap.main([secret_argument])

        rendered = output.getvalue()
        self.assertEqual(return_code, 1)
        self.assertEqual(
            json.loads(rendered)["error_code"], "invalid_arguments"
        )
        self.assertNotIn(secret_argument, rendered)


if __name__ == "__main__":
    unittest.main()
