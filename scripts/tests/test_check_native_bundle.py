import importlib.util
from pathlib import Path
import plistlib
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("bundle_check", Path(__file__).parents[1] / "check_native_bundle.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


class BundleAuditTests(unittest.TestCase):
    def test_identity_is_not_dependency(self):
        text = """Load command 0
          cmd LC_ID_DYLIB
          name /Users/build/private/library.dylib (offset 24)
        Load command 1
          cmd LC_LOAD_DYLIB
          name /System/Library/Frameworks/Foundation.framework/Foundation (offset 24)
        Load command 2
          cmd LC_LOAD_WEAK_DYLIB
          name /Users/build/private/required.dylib (offset 24)
        Load command 3
          cmd LC_REEXPORT_DYLIB
          name @loader_path/a library.dylib (offset 24)
        """
        self.assertEqual(check.dylib_loads(text), ["/System/Library/Frameworks/Foundation.framework/Foundation", "/Users/build/private/required.dylib", "@loader_path/a library.dylib"])

    def fixture(self, root):
        (root / "Contents").mkdir(parents=True, exist_ok=True)
        self.write_info(root)
        for relative in ["Contents/MacOS/feral-native", "Contents/Resources/python/bin/python3", "Contents/Resources/opencode/bin/opencode", "Contents/Resources/feral-core/api/server.py", "Contents/Resources/native_backend_launcher.py"]:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture")
            path.chmod(0o755)

    def write_info(self, root, updates=None):
        info = {"CFBundleIdentifier": "ai.feral.native.preview", "CFBundleExecutable": "feral-native",
                "CFBundlePackageType": "APPL", "CFBundleName": "FERAL Native Preview",
                "CFBundleDisplayName": "FERAL Native Preview", "CFBundleShortVersionString": "2026.9.22",
                "CFBundleVersion": "2026100113", "LSMinimumSystemVersion": "13.0",
                "NSMicrophoneUsageDescription": "Uses microphone after explicit start.",
                "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True}}
        info.update(updates or {})
        (root / "Contents/Info.plist").write_bytes(plistlib.dumps(info))

    def test_symlink_containment_and_broken_link(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            root = Path(directory) / "app with spaces.app"
            self.fixture(root)
            (root / "internal").symlink_to("Contents/Resources/python/bin/python3")
            self.assertTrue(check.audit_bundle(root)["ok"])
            outside = Path(directory) / "outside"
            outside.write_text("private fixture value")
            (root / "escaped").symlink_to(outside)
            (root / "broken").symlink_to("missing")
            result = check.audit_bundle(root)
            self.assertFalse(result["ok"])
            self.assertEqual({i["code"] for i in result["issues"]}, {"symlink_escape", "symlink_broken_or_cycle"})
            self.assertNotIn("private fixture value", str(result))

    def test_private_filename_ca_and_real_load(self):
        self.assertFalse(check.private_filename("python/site-packages/certifi/cacert.pem"))
        self.assertTrue(check.private_filename("user/private.pem"))
        self.assertTrue(check.private_filename("memory.db-wal"))
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            root = Path(directory) / "fixture.app"
            self.fixture(root)
            binary = root / "library.dylib"
            binary.write_bytes(b"\xcf\xfa\xed\xfefixture")
            result = check.audit_bundle(root, inspector=lambda _: ["/Users/build/required.dylib"])
            self.assertFalse(result["ok"])
            self.assertEqual(result["issues"], [{"code": "non_system_absolute_dylib_load", "path": "library.dylib"}])

    def test_metadata_leak_and_critical_executable(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            root = Path(directory) / "fixture.app"
            self.fixture(root)
            (root / "editable.pth").write_text("/Users/private/source-checkout\n")
            (root / "Contents/Resources/opencode/bin/opencode").chmod(0o644)
            result = check.audit_bundle(root)
            self.assertEqual({i["code"] for i in result["issues"]}, {"external_path_metadata", "critical_resource_not_executable"})
            self.assertNotIn("/Users/private/source-checkout", str(result))

    def test_missing_metadata_cannot_pass_with_executable_resources(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            root = Path(directory) / "fixture.app"
            self.fixture(root)
            (root / "Contents/Info.plist").unlink()
            result = check.audit_bundle(root)
            self.assertFalse(result["ok"])
            self.assertIn("preview_metadata_missing_or_external", {i["code"] for i in result["issues"]})

    def test_identity_path_escape_legacy_bundle_and_false_versions_refused(self):
        cases = [({"CFBundleExecutable": "../../private-secret-executable"}, "preview_launch_identity_mismatch"),
                 ({"CFBundleIdentifier": "ai.feral.desktop"}, "preview_launch_identity_mismatch"),
                 ({"CFBundlePackageType": "BNDL"}, "preview_launch_identity_mismatch"),
                 ({"CFBundleVersion": True}, "preview_version_metadata_invalid"),
                 ({"CFBundleShortVersionString": "2026.9.22\n"}, "preview_version_metadata_invalid"),
                 ({"LSMinimumSystemVersion": "12.0"}, "preview_minimum_macos_invalid"),
                 ({"NSMicrophoneUsageDescription": ""}, "preview_microphone_description_invalid"),
                 ({"NSAppTransportSecurity": {"NSAllowsLocalNetworking": 1}}, "preview_local_transport_metadata_invalid"),
                 ({"NSAppTransportSecurity": {"NSAllowsLocalNetworking": True, "NSAllowsArbitraryLoads": True}}, "preview_local_transport_metadata_invalid")]
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            root = Path(directory) / "fixture.app"
            self.fixture(root)
            for changes, expected in cases:
                with self.subTest(expected=expected, changes=list(changes)):
                    self.write_info(root, changes)
                    result = check.audit_bundle(root)
                    self.assertFalse(result["ok"])
                    self.assertIn(expected, {i["code"] for i in result["issues"]})
                    self.assertNotIn("private-secret-executable", str(result))

    def test_malformed_oversized_external_and_binary_plist_are_bounded(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            root = Path(directory) / "fixture.app"
            self.fixture(root)
            info = root / "Contents/Info.plist"
            info.write_bytes(b"not a plist; private fixture secret")
            result = check.audit_bundle(root)
            self.assertIn("preview_metadata_invalid", {i["code"] for i in result["issues"]})
            self.assertNotIn("private fixture secret", str(result))
            info.write_bytes(b'<?xml version="1.0"?><plist><dict><key>private fixture secret</key>')
            result = check.audit_bundle(root)
            self.assertIn("preview_metadata_invalid", {i["code"] for i in result["issues"]})
            self.assertNotIn("private fixture secret", str(result))
            info.write_bytes(b"x" * (check.MAX_INFO_PLIST + 1))
            self.assertIn("preview_metadata_budget_exceeded", {i["code"] for i in check.audit_bundle(root)["issues"]})
            info.unlink()
            outside = Path(directory) / "outside.plist"
            outside.write_bytes(plistlib.dumps({"private": "fixture secret"}))
            info.symlink_to(outside)
            self.assertIn("preview_metadata_missing_or_external", {i["code"] for i in check.audit_bundle(root)["issues"]})
            info.unlink()
            self.write_info(root)
            value = plistlib.loads(info.read_bytes())
            info.write_bytes(plistlib.dumps(value, fmt=plistlib.FMT_BINARY))
            self.assertTrue(check.audit_bundle(root)["ok"])


if __name__ == "__main__":
    unittest.main()
