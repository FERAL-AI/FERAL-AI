"""Disposable payload contract fixtures; not actual Linux artifact acceptance."""
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("linux_bundle_smoke", SCRIPTS / "linux_desktop_bundle_smoke.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def elf():
    data = bytearray(64)
    data[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", data, 16, 3)
    struct.pack_into("<H", data, 18, 62)
    return bytes(data)


class LinuxSmokeContractTests(unittest.TestCase):
    def fixture(self, root):
        for name in ("usr/bin/feral-desktop", "usr/lib/feral-desktop/python/bin/python3",
                     "usr/lib/feral-desktop/opencode/bin/opencode"):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(elf())
            path.chmod(0o755)
        core = root / "usr/lib/feral-desktop/feral-core"
        (core / "api").mkdir(parents=True)
        (core / "api/server.py").write_text("# Synthetic source fixture\n")
        (core / "webui_v2").mkdir()
        (core / "webui_v2/index.html").write_text('<script src="/assets/index.js"></script><link href="/assets/index.css">')
        return core

    def test_actual_declared_resource_layout_not_arbitrary_discovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            core = self.fixture(root)
            resources, actual_core, python, opencode = smoke.payload_paths(root)
            self.assertEqual(actual_core, core)
            self.assertEqual(resources, core.parent)
            self.assertEqual(python, resources / "python/bin/python3")
            self.assertEqual(opencode, resources / "opencode/bin/opencode")
            (core / "api/server.py").unlink()
            with self.assertRaises(smoke.LinuxBundleError):
                smoke.payload_paths(root)

    def test_inventory_content_mode_and_source_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            core = self.fixture(root)
            before = smoke.inventory(root)
            self.assertEqual(before, smoke.inventory(root))
            (core / "api/server.py").write_text("# Different synthetic source\n")
            self.assertNotEqual(before["sha256"], smoke.inventory(root)["sha256"])
            before = smoke.inventory(root)
            (core / "api/server.py").chmod(0o700)
            self.assertNotEqual(before["sha256"], smoke.inventory(root)["sha256"])

    def test_internal_relative_symlink_allowed_external_or_cycle_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "payload"
            self.fixture(root)
            link = root / "internal"
            link.symlink_to("usr/bin/feral-desktop")
            smoke.inventory(root)
            link.unlink()
            link.symlink_to(Path(temporary))
            with self.assertRaisesRegex(smoke.LinuxBundleError, "external"):
                smoke.inventory(root)
            link.unlink()
            link.symlink_to("internal")
            with self.assertRaisesRegex(smoke.LinuxBundleError, "unresolvable"):
                smoke.inventory(root)

    def test_special_file_and_inventory_budgets_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            with patch.object(smoke, "MAX_ENTRIES", 1), self.assertRaisesRegex(smoke.LinuxBundleError, "entry_budget"):
                smoke.inventory(root)
            with patch.object(smoke, "MAX_PAYLOAD_BYTES", 1), self.assertRaisesRegex(smoke.LinuxBundleError, "byte_budget"):
                smoke.inventory(root)
            os.mkfifo(root / "pipe")
            with self.assertRaisesRegex(smoke.LinuxBundleError, "special_file"):
                smoke.inventory(root)

    def test_virtualenv_and_wrong_architecture_refused_before_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            core = self.fixture(root)
            venv = core.parent / "python/pyvenv.cfg"
            venv.write_text("home = /private/synthetic/builder")
            with self.assertRaisesRegex(smoke.LinuxBundleError, "virtualenv"):
                smoke.payload_paths(root)
            venv.unlink()
            (root / "usr/bin/feral-desktop").write_bytes(b"MZ" + bytes(62))
            with self.assertRaises(smoke.platform_check.BundlePlatformError):
                smoke.payload_paths(root)

    def test_catalog_exact_bundled_executable_and_boolean_availability(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            core = self.fixture(root)
            opencode = core.parent / "opencode/bin/opencode"
            good = {"agents": [{"agent_id": "opencode", "available": True, "command": [str(opencode), "acp"]}]}
            smoke.check_coding_catalog(good, opencode)
            for bad in [{}, {"agents": []}, {"agents": [good["agents"][0], good["agents"][0]]},
                        {"agents": [{"agent_id": "opencode", "available": 1, "command": [str(opencode)]}]},
                        {"agents": [{"agent_id": "opencode", "available": True, "command": ["/usr/bin/opencode"]}]}]:
                with self.subTest(bad=bad), self.assertRaises(smoke.LinuxBundleError):
                    smoke.check_coding_catalog(bad, opencode)

    def test_assets_require_built_local_js_and_css(self):
        good = '<script src="/assets/main.js"></script><link href="/assets/main.css">'
        self.assertEqual(smoke.asset_references(good), ["/assets/main.js", "/assets/main.css"])
        for bad in ["not a dashboard", '<script src="/assets/a.js"></script>',
                    good.replace("/assets/main.js", "https://outside.example/assets/main.js"),
                    good.replace("/assets/main.js", "../assets/main.js"),
                    good.replace("/assets/main.js", "/assets/main.js?private=value")]:
            with self.assertRaises(smoke.LinuxBundleError):
                smoke.asset_references(bad)

    def test_environment_has_no_inherited_credentials_and_actual_same_root_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(os.environ, {"OPENAI_API_KEY": "private synthetic credential", "FERAL_HOME": "wrong"}):
                env = smoke.isolated_environment(root, root / "opencode")
            self.assertEqual(env["FERAL_HOME"], env["FERAL_DATA_HOME"])
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("private synthetic credential", json.dumps(env))
            self.assertEqual(env["FERAL_LLM_PROVIDER"], "none")
            self.assertEqual(env["FERAL_EMBED_PROVIDER"], "hash")
            self.assertEqual(env["PYTHON_KEYRING_BACKEND"], "keyring.backends.null.Keyring")
            self.assertEqual(json.loads((root / "feral-home/settings.json").read_text())["llm"]["fallback_providers"], [])

    def test_runtime_probe_enforces_bundled_import_and_stdlib_containment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "payload"
            workspace = Path(temporary) / "evidence"
            workspace.mkdir()
            core = self.fixture(root)
            python_root = core.parent / "python"
            sdk = python_root / "lib/python3.11/site-packages/feral_sdk/__init__.py"
            sdk.parent.mkdir(parents=True)
            sdk.write_text("# synthetic SDK")
            stdlib = python_root / "lib/python3.11/os.py"
            stdlib.write_text("# synthetic stdlib")
            runtime = {"executable": str(python_root / "bin/python3"), "prefix": str(python_root),
                       "stdlib": str(stdlib), "sdk": str(sdk), "server": str(core / "api/server.py"),
                       "sys_path": ["", str(core), str(sdk.parent)], "sqlite": "synthetic", "python_version": "3.11.15"}
            result = subprocess.CompletedProcess([], 0, json.dumps(runtime).encode(), b"")
            with patch.object(smoke.subprocess, "run", return_value=result):
                self.assertEqual(smoke.runtime_probe(python_root / "bin/python3", core, {}, workspace), runtime)
            for key, outside in [("server", str(stdlib)), ("stdlib", str(core / "api/server.py")),
                                 ("sys_path", ["/private/synthetic/builder-path"])]:
                bad = dict(runtime)
                bad[key] = outside
                result = subprocess.CompletedProcess([], 0, json.dumps(bad).encode(), b"private synthetic stderr")
                with patch.object(smoke.subprocess, "run", return_value=result), self.assertRaises(smoke.LinuxBundleError) as caught:
                    smoke.runtime_probe(python_root / "bin/python3", core, {}, workspace)
                self.assertNotIn("private synthetic stderr", str(caught.exception))

    def test_wrong_host_refuses_before_copy_or_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(smoke.platform_check, "host_platform", return_value=smoke.platform_check.select_platform("Darwin", "arm64")), patch.object(smoke.shutil, "copytree") as copy, patch.object(smoke.subprocess, "Popen") as start:
                with self.assertRaisesRegex(smoke.LinuxBundleError, "verification_host_required"):
                    smoke.run(root, root, 5, "3.11.15")
            copy.assert_not_called()
            start.assert_not_called()

    def test_source_workspace_overlap_refuses_before_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            selected = smoke.platform_check.select_platform("Linux", "x86_64", "glibc")
            with patch.object(smoke.platform_check, "host_platform", return_value=selected), patch.object(smoke.shutil, "copytree") as copy:
                with self.assertRaisesRegex(smoke.LinuxBundleError, "overlap"):
                    smoke.run(root, root, 5, "3.11.15")
                copy.assert_not_called()

    def test_owned_process_group_cleanup_reaps_only_started_child(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
        try:
            smoke.stop_process_group(child)
            self.assertIsNotNone(child.poll())
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)

    def test_python_pin_comparison_is_not_host_python_assumption(self):
        smoke.check_runtime_identity({"python_version": "3.11.15"}, "3.11.15")
        with self.assertRaises(smoke.LinuxBundleError):
            smoke.check_runtime_identity({"python_version": "3.11.14"}, "3.11.15")


if __name__ == "__main__":
    unittest.main()
