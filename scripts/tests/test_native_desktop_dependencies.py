"""Mac input closure gates with inert packages and no desktop actions."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).parents[1] / "check_native_bundle.py"
spec = importlib.util.spec_from_file_location("desktop_dependency_check", SCRIPT)
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)
STAGE = SCRIPT.parent.parent / "desktop/scripts/stage_bundle.sh"


class DesktopDependencyTests(unittest.TestCase):
    @staticmethod
    def process_result(code, output):
        def run(command, **kwargs):
            kwargs["stdout"].write(output.encode())
            kwargs["stderr"].write(b"private stderr")
            return subprocess.CompletedProcess(command, code)
        return run

    def fixture(self, root):
        site = root / "lib/python3.11/site-packages"
        for distribution, module in check.DESKTOP_DISTRIBUTIONS.items():
            source = site / module
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("# inert dependency\n")
            metadata = site / (distribution.replace("-", "_") + "-0.1.dist-info/METADATA")
            metadata.parent.mkdir(parents=True)
            metadata.write_text(f"Name: {distribution}\nVersion: 0.1\n")
        (site / "pyautogui/_pyautogui_osx.py").write_text("# inert native adapter\n")
        return site

    def test_static_closure_is_cross_platform_and_never_imports_or_executes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            with patch.object(check.subprocess, "run", side_effect=AssertionError("execution forbidden")):
                self.assertEqual(check.desktop_dependency_issues(root), [])

    def test_each_missing_dependency_refuses_probe_before_execution(self):
        for distribution, module in check.DESKTOP_DISTRIBUTIONS.items():
            with self.subTest(distribution=distribution), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                site = self.fixture(root)
                (site / module).unlink()
                with patch.object(check.subprocess, "run") as run:
                    result = check.probe_desktop_inputs(root)
                self.assertFalse(result["ok"])
                self.assertIn("desktop_module_missing_or_external", {x["code"] for x in result["issues"]})
                run.assert_not_called()

    def test_missing_metadata_duplicate_and_wrong_name_are_not_installed(self):
        for mode in ("missing", "duplicate", "wrong", "oversized"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                site = self.fixture(root)
                metadata = site / "pyautogui-0.1.dist-info/METADATA"
                if mode == "missing":
                    metadata.unlink()
                elif mode == "duplicate":
                    other = site / "pyautogui-0.2.dist-info/METADATA"
                    other.parent.mkdir()
                    other.write_text("Name: pyautogui\nVersion: 0.2\n")
                elif mode == "wrong":
                    metadata.write_text("Name: private-secret-name\nVersion: private-secret-version\n")
                else:
                    metadata.write_bytes(b"x" * (check.MAX_DEPENDENCY_METADATA + 1))
                result = check.probe_desktop_inputs(root)
                self.assertFalse(result["ok"])
                self.assertNotIn("private-secret", json.dumps(result))

    def test_external_module_and_missing_mac_adapter_are_not_readiness(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "python"
            site = self.fixture(root)
            external = Path(temporary) / "external.py"
            external.write_text("private dependency contents")
            path = site / "pyperclip/__init__.py"
            path.unlink()
            path.symlink_to(external)
            (site / "pyautogui/_pyautogui_osx.py").unlink()
            result = check.probe_desktop_inputs(root)
            self.assertFalse(result["ok"])
            self.assertEqual({x["code"] for x in result["issues"]}, {
                "desktop_module_missing_or_external", "desktop_macos_adapter_missing_or_external"})
            self.assertNotIn("private dependency contents", json.dumps(result))

    def test_import_probe_is_bounded_isolated_and_not_a_mac_window_provider(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            output = json.dumps({"ok": True, "modules": list(check.DESKTOP_IMPORTS)})
            with patch.object(check.subprocess, "run", side_effect=self.process_result(0, output)) as run:
                result = check.probe_desktop_inputs(root)
            self.assertTrue(result["ok"])
            command = run.call_args.args[0]
            self.assertEqual(command[:3], [str(root.resolve() / "bin/python3"), "-I", "-B"])
            self.assertIn("sys.platform=='darwin'", command[4])
            self.assertNotIn("pygetwindow", check.DESKTOP_IMPORTS)
            self.assertEqual(run.call_args.kwargs["timeout"], 20)
            self.assertNotIn("PYTHONPATH", run.call_args.kwargs["env"])
            self.assertNotIn("HOME", run.call_args.kwargs["env"])
            self.assertIn("FERAL_HOME", run.call_args.kwargs["env"])
            self.assertIn("FERAL_DATA_HOME", run.call_args.kwargs["env"])
            self.assertNotIn("capture_output", run.call_args.kwargs)
            self.assertEqual(run.call_args.kwargs["cwd"], run.call_args.kwargs["env"]["TMPDIR"])
            self.assertIn("resource.setrlimit(resource.RLIMIT_FSIZE,(16384,16384))", command[4])
            self.assertEqual(run.call_args.kwargs["env"]["PYTHONDONTWRITEBYTECODE"], "1")
            self.assertIn("no capture", result["scope"])

    def test_failed_or_forged_import_receipt_is_private_and_not_ready(self):
        for code, output in [(1, "private account error"), (0, "not json private content"),
                             (0, json.dumps({"ok": True, "modules": ["pyautogui"]})),
                             (0, "x" * 2049)]:
            with self.subTest(code=code, output_length=len(output)), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.fixture(root)
                with patch.object(check.subprocess, "run", side_effect=self.process_result(code, output)):
                    result = check.probe_desktop_inputs(root)
                self.assertEqual(result, {"ok": False, "code": "desktop_import_failed_private_details_withheld"})

    def test_import_timeout_remains_a_truthful_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            error = subprocess.TimeoutExpired("private command", 20, output="private output")
            with patch.object(check.subprocess, "run", side_effect=error):
                result = check.probe_desktop_inputs(root)
            self.assertEqual(result, {"ok": False, "code": "desktop_import_failed_private_details_withheld"})

    @unittest.skipUnless(os.name == "posix", "Mac/Linux file-size resource limit")
    def test_actual_child_output_flood_is_capped_before_dependency_import(self):
        actual_run = subprocess.run
        observed = {}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            (root / "bin").mkdir()
            (root / "bin/python3").symlink_to(sys.executable)

            def flood(command, **kwargs):
                bounded = list(command)
                # Deliberately flood before any native/dependency import.
                bounded[4] = bounded[4].replace("modules=[", "print('x'*100000); modules=[", 1)
                result = actual_run(bounded, **kwargs)
                observed["bytes"] = os.fstat(kwargs["stdout"].fileno()).st_size
                return result

            with patch.object(check.subprocess, "run", side_effect=flood):
                result = check.probe_desktop_inputs(root)
        self.assertEqual(result, {"ok": False, "code": "desktop_import_failed_private_details_withheld"})
        self.assertLessEqual(observed["bytes"], check.MAX_DESKTOP_PROBE_FILE_BYTES)
        self.assertGreater(observed["bytes"], 2048)

    def test_ambiguous_or_external_site_packages_refuses_before_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "python"
            self.fixture(root)
            (root / "lib/python3.12/site-packages").mkdir(parents=True)
            with patch.object(check.subprocess, "run") as run:
                result = check.probe_desktop_inputs(root)
            self.assertFalse(result["ok"])
            self.assertEqual(result["issues"], [{"code": "desktop_site_packages_missing_or_external", "path": ""}])
            run.assert_not_called()

    def test_stage_selects_existing_locked_desktop_extra_only_on_mac(self):
        source = STAGE.read_text()
        selection = source[source.index('STAGED_EXTRAS="llm"'):source.index('\n\nif [ "$(uname -s)"', source.index('STAGED_EXTRAS="llm"') + 1)]
        for platform, expected in [("Darwin", "llm,desktop"), ("Linux", "llm")]:
            with self.subTest(platform=platform):
                fixture = ('uname() { printf "%s\\n" "' + platform + '"; }; '
                           'uv_fixture() { printf "%s\\n" "$@"; }; '
                           'UV=uv_fixture; PYEXE=/fixture/python3; REPO_ROOT=/fixture/repo;\n')
                result = subprocess.run(["bash", "-c", fixture + selection], capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0)
                self.assertIn(f"/fixture/repo/feral-core[{expected}]", result.stderr.splitlines())
                self.assertIn("--constraint", result.stderr.splitlines())
                self.assertNotIn("[all]", result.stderr)
        self.assertIn('--probe-desktop-inputs "$STAGED_PY"', source)


if __name__ == "__main__":
    unittest.main()
