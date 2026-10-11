"""Pure target checks plus an actual no-mutation staging refusal."""
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HELPER = Path(__file__).parents[2] / "desktop/scripts/bundle_platform.py"
spec = importlib.util.spec_from_file_location("desktop_bundle_platform", HELPER)
platform_check = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = platform_check
spec.loader.exec_module(platform_check)


def header(system="Linux", machine="x86_64"):
    data = bytearray(64)
    if system == "Linux":
        data[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<H", data, 16, 3)
        struct.pack_into("<H", data, 18, 62)
    else:
        data[:4] = b"\xcf\xfa\xed\xfe"
        struct.pack_into("<I", data, 4, 0x0100000C if machine == "arm64" else 0x01000007)
    return bytes(data)


class DesktopPlatformTests(unittest.TestCase):
    def test_supported_exact_profiles(self):
        for system, machine, libc, package in [
            ("Darwin", "arm64", "", "opencode-darwin-arm64"),
            ("Darwin", "x86_64", "", "opencode-darwin-x64"),
            ("Linux", "x86_64", "glibc", "opencode-linux-x64-baseline"),
        ]:
            with self.subTest(package=package):
                selected = platform_check.select_platform(system, machine, libc)
                self.assertEqual(selected.package, package)
                platform_check.validate_binary_header(header(system, machine), selected)

    def test_unsupported_profiles_fail_closed(self):
        for profile in [("Linux", "x86_64", "musl"), ("Linux", "x86_64", ""),
                        ("Linux", "arm64", "glibc"), ("Linux", "aarch64", "glibc"),
                        ("Windows", "AMD64", ""), ("Darwin", "unknown", "")]:
            with self.subTest(profile=profile), self.assertRaises(platform_check.BundlePlatformError):
                platform_check.select_platform(*profile)

    def fixture(self, root):
        selected = platform_check.select_platform("Linux", "x86_64", "glibc")
        package = root / selected.package
        (package / "bin").mkdir(parents=True)
        (package / "package.json").write_text(json.dumps({
            "name": selected.package, "version": "1.18.10", "os": ["linux"], "cpu": ["x64"]}))
        binary = package / "bin/opencode"
        binary.write_bytes(header())
        binary.chmod(0o755)
        return package, selected

    def test_package_metadata_and_target_before_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            package, selected = self.fixture(Path(temporary))
            with patch.object(platform_check.subprocess, "run") as probe:
                probe.return_value = subprocess.CompletedProcess([], 0, "1.18.10\n", "")
                self.assertEqual(platform_check.validate_package(package, selected), package / "bin/opencode")
                self.assertEqual(probe.call_args.kwargs["timeout"], 20)
                for key, bad in [("version", "1.18.9"), ("name", "opencode-linux-x64"),
                                 ("os", ["darwin"]), ("cpu", ["arm64"])]:
                    metadata = json.loads((package / "package.json").read_text())
                    original = metadata[key]
                    metadata[key] = bad
                    (package / "package.json").write_text(json.dumps(metadata))
                    probe.reset_mock()
                    with self.assertRaises(platform_check.BundlePlatformError):
                        platform_check.validate_package(package, selected)
                    probe.assert_not_called()
                    metadata[key] = original
                    (package / "package.json").write_text(json.dumps(metadata))

    def test_binary_wrong_target_or_missing_never_executes(self):
        with tempfile.TemporaryDirectory() as temporary:
            package, selected = self.fixture(Path(temporary))
            binary = package / "bin/opencode"
            for bad in [header("Darwin", "arm64"), b"#!/bin/sh\nexit 0\n" + bytes(30),
                        header()[:18] + b"\xb7\x00" + header()[20:]]:
                binary.write_bytes(bad)
                with patch.object(platform_check.subprocess, "run") as probe, self.assertRaises(platform_check.BundlePlatformError):
                    platform_check.validate_package(package, selected)
                probe.assert_not_called()
            binary.unlink()
            with self.assertRaises(platform_check.BundlePlatformError):
                platform_check.validate_package(package, selected)

    def test_symlink_package_metadata_and_executable_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package, selected = self.fixture(root)
            binary = package / "bin/opencode"
            target = root / "outside"
            binary.rename(target)
            binary.symlink_to(target)
            with self.assertRaises(platform_check.BundlePlatformError):
                platform_check.validate_package(package, selected, probe_version=False)
            binary.unlink()
            target.rename(binary)
            metadata = package / "package.json"
            target = root / "outside.json"
            metadata.rename(target)
            metadata.symlink_to(target)
            with self.assertRaises(platform_check.BundlePlatformError):
                platform_check.validate_package(package, selected, probe_version=False)

    def test_probe_error_timeout_and_wrong_version_are_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            package, selected = self.fixture(Path(temporary))
            for failure in [OSError("private fixture sentinel"), subprocess.TimeoutExpired("private", 20)]:
                with patch.object(platform_check.subprocess, "run", side_effect=failure), self.assertRaises(platform_check.BundlePlatformError) as caught:
                    platform_check.validate_package(package, selected)
                self.assertNotIn("private", str(caught.exception))
            with patch.object(platform_check.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "9.99", "")), self.assertRaises(platform_check.BundlePlatformError):
                platform_check.validate_package(package, selected)

    def test_stage_unsupported_refusal_before_bootstrap_or_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "desktop/scripts"
            scripts.mkdir(parents=True)
            shutil.copy2(HELPER.parent / "stage_bundle.sh", scripts / "stage_bundle.sh")
            (root / ".python-pin").write_text("3.11.15")
            fake_path = root / "commands"
            fake_path.mkdir()
            fake_python = fake_path / "python3"
            fake_python.write_text("#!/bin/sh\nexit 1\n")
            fake_python.chmod(0o755)
            environment = {"PATH": str(fake_path) + ":/usr/bin:/bin"}
            result = subprocess.run(["/bin/bash", str(scripts / "stage_bundle.sh")], env=environment,
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Unsupported", result.stderr)
            self.assertFalse((root / "desktop/src-tauri/resources").exists())
            self.assertFalse((root / ".uv").exists())


if __name__ == "__main__":
    unittest.main()
