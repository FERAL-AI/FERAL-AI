"""Bounded, read-only native bundle audit. Runtime probes require --probe-runtime.

Reports paths and fixed diagnostic codes, never file contents or process stderr.
This is not a complete secret scan or signing/notarization acceptance test.
"""
import argparse
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import tempfile
from xml.parsers.expat import ExpatError

MAX_FILES = 100_000
MAX_PATH_TEXT = 131_072
MAX_INFO_PLIST = 65_536
PREVIEW_BUNDLE_ID = "ai.feral.native.preview"
MACH_MAGIC = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xfe\xed\xfa\xcf", b"\xbe\xba\xfe\xca"}
LOAD_COMMANDS = {"LC_LOAD_DYLIB", "LC_LOAD_WEAK_DYLIB", "LC_REEXPORT_DYLIB", "LC_LOAD_UPWARD_DYLIB", "LC_LAZY_LOAD_DYLIB"}


def dylib_loads(output):
    """Parse otool -l commands; LC_ID_DYLIB is identity, not a dependency."""
    command = None
    names = []
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("Load command "):
            command = None
        elif line.startswith("cmd "):
            command = line[4:]
        elif command in LOAD_COMMANDS and line.startswith("name "):
            names.append(line[5:].rsplit(" (offset ", 1)[0])
    return names


def private_filename(relative):
    p = Path(relative)
    # These are shipped public CA roots, not private keys.
    if p.name == "cacert.pem" and "certifi" in p.parts:
        return False
    return (p.name.startswith(".env") or p.name in {"vault.enc", "settings.json", "identity.json", "pyvenv.cfg"}
            or p.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".log", ".pem", ".key"}
            or re.search(r"\.(?:db|sqlite|sqlite3)-(?:wal|shm|journal)$", p.name) is not None)


def inside(path, root):
    return path == root or root in path.parents


def inspect_macho(path):
    result = subprocess.run(["/usr/bin/otool", "-l", str(path)], capture_output=True, text=True, timeout=10)
    if result.returncode or len(result.stdout) > 4_194_304:
        raise ValueError("macho_inspection_failed")
    return dylib_loads(result.stdout)


def preview_metadata_issues(root):
    """Launch/preview-isolation metadata only; never report untrusted values.

    A passing identifier is not a data migration or preference isolation proof.
    This audit targets the current arm64 macOS13 preview, not Linux/release apps.
    """
    relative = "Contents/Info.plist"
    path = root / relative
    def finding(code):
        return [{"code": code, "path": relative}]
    try:
        if not path.is_file() or not inside(path.resolve(strict=True), root):
            return finding("preview_metadata_missing_or_external")
        if path.stat().st_size > MAX_INFO_PLIST:
            return finding("preview_metadata_budget_exceeded")
        with path.open("rb") as handle:
            data = handle.read(MAX_INFO_PLIST + 1)
        if len(data) > MAX_INFO_PLIST:
            return finding("preview_metadata_budget_exceeded")
        info = plistlib.loads(data)
    except (OSError, ValueError, TypeError, OverflowError, RecursionError, plistlib.InvalidFileException, ExpatError):
        return finding("preview_metadata_invalid")
    if type(info) is not dict:
        return finding("preview_metadata_invalid")
    findings = []
    def fail(code):
        findings.append({"code": code, "path": relative})
    if (info.get("CFBundleIdentifier") != PREVIEW_BUNDLE_ID
            or info.get("CFBundleExecutable") != "feral-native"
            or info.get("CFBundlePackageType") != "APPL"):
        fail("preview_launch_identity_mismatch")
    for key in ("CFBundleName", "CFBundleDisplayName"):
        value = info.get(key)
        if type(value) is not str or not value.strip() or len(value.encode("utf-8")) > 256:
            fail("preview_display_metadata_invalid")
            break
    for key in ("CFBundleShortVersionString", "CFBundleVersion"):
        value = info.get(key)
        if type(value) is not str or not re.fullmatch(r"[0-9]{1,16}(?:\.[0-9]{1,16}){0,3}", value) or len(value) > 48:
            fail("preview_version_metadata_invalid")
            break
    # Build script compiles to macOS13: an older declared minimum lies to users.
    minimum = info.get("LSMinimumSystemVersion")
    if (type(minimum) is not str or not re.fullmatch(r"[0-9]{1,2}\.[0-9]{1,2}(?:\.[0-9]{1,2})?", minimum)
            or int(minimum.split(".")[0]) < 13):
        fail("preview_minimum_macos_invalid")
    microphone = info.get("NSMicrophoneUsageDescription")
    if type(microphone) is not str or not microphone.strip() or len(microphone.encode("utf-8")) > 4096:
        fail("preview_microphone_description_invalid")
    ats = info.get("NSAppTransportSecurity")
    if (type(ats) is not dict or ats.get("NSAllowsLocalNetworking") is not True
            or ats.get("NSAllowsArbitraryLoads", False) is not False):
        fail("preview_local_transport_metadata_invalid")
    return findings


def audit_bundle(bundle, inspector=inspect_macho):
    root = Path(bundle).resolve()
    issues = []
    counts = {"files": 0, "symlinks": 0, "macho": 0}

    def issue(code, path=""):
        # Bound reports; do not echo dependency strings or private file contents.
        if len(issues) < 100:
            issues.append({"code": code, "path": path})

    if not root.is_dir():
        issue("bundle_missing")
    else:
        issues.extend(preview_metadata_issues(root))
        for directory, dirs, files in os.walk(root, followlinks=False):
            for name in dirs + files:
                path = Path(directory) / name
                relative = str(path.relative_to(root))
                counts["files"] += 1
                if counts["files"] > MAX_FILES:
                    issue("inventory_budget_exceeded")
                    dirs[:] = []
                    break
                if path.is_symlink():
                    counts["symlinks"] += 1
                    try:
                        resolved = path.resolve(strict=True)
                        if not inside(resolved, root):
                            issue("symlink_escape", relative)
                    except (OSError, RuntimeError):
                        issue("symlink_broken_or_cycle", relative)
                    continue
                try:
                    mode = path.stat().st_mode
                    if not stat.S_ISREG(mode):
                        continue
                    if private_filename(relative):
                        issue("potential_private_filename", relative)
                    if path.suffix == ".pth" or path.name.startswith("__editable__"):
                        if path.stat().st_size > MAX_PATH_TEXT:
                            issue("path_metadata_budget_exceeded", relative)
                        elif re.search(r"/Users/|/home/|/opt/homebrew/|/private/tmp/|/tmp/|[A-Za-z]:\\", path.read_text(errors="replace")):
                            issue("external_path_metadata", relative)
                    with path.open("rb") as handle:
                        magic = handle.read(4)
                    if magic in MACH_MAGIC:
                        counts["macho"] += 1
                        for dependency in inspector(path):
                            if dependency.startswith("/") and not dependency.startswith(("/usr/lib/", "/System/Library/")):
                                # Internal absolute references also break after relocation.
                                issue("non_system_absolute_dylib_load", relative)
                except (OSError, ValueError, subprocess.SubprocessError):
                    issue("file_or_macho_inspection_failed", relative)
            if counts["files"] > MAX_FILES:
                break
        required = {
            "Contents/MacOS/feral-native": True,
            "Contents/Resources/python/bin/python3": True,
            "Contents/Resources/opencode/bin/opencode": True,
            "Contents/Resources/feral-core/api/server.py": False,
            "Contents/Resources/native_backend_launcher.py": False,
        }
        for relative, executable in required.items():
            path = root / relative
            if not path.is_file() or not inside(path.resolve(), root):
                issue("critical_resource_missing_or_external", relative)
            elif executable and not os.access(path, os.X_OK):
                issue("critical_resource_not_executable", relative)
    return {"ok": not issues, "counts": counts, "issues": issues,
            "scope": "Bounded macOS preview metadata/filename/path/dependency audit; not migration, complete secret or distribution acceptance."}


def probe_runtime(bundle):
    root = Path(bundle).resolve()
    python_root = root / "Contents/Resources/python"
    code = ("import os,sys,sqlite3,json; r=os.path.realpath(sys.argv[1]); "
            "inside=lambda p: os.path.realpath(p)==r or os.path.realpath(p).startswith(r+os.sep); "
            "c=sqlite3.connect(':memory:'); c.execute('CREATE VIRTUAL TABLE t USING fts5(x)'); "
            "print(json.dumps({'python':sys.version.split()[0],'sqlite':sqlite3.sqlite_version,"
            "'fts5':True,'base_prefix_in_bundle':inside(sys.base_prefix),'stdlib_in_bundle':inside(os.__file__)}))")
    try:
        with tempfile.TemporaryDirectory(prefix="feral-bundle-probe-", dir="/private/tmp") as home:
            env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": home, "TMPDIR": home}
            py = subprocess.run([str(python_root / "bin/python3"), "-I", "-B", "-c", code, str(python_root)], env=env, capture_output=True, text=True, timeout=20)
            engine = subprocess.run([str(root / "Contents/Resources/opencode/bin/opencode"), "--version"], env=env, capture_output=True, text=True, timeout=20)
            if py.returncode or engine.returncode or len(py.stdout) > 2048 or len(engine.stdout) > 128:
                raise ValueError("runtime_probe_failed")
            data = json.loads(py.stdout)
            version = engine.stdout.strip()
            if not re.fullmatch(r"\d+\.\d+\.\d+", version):
                raise ValueError("runtime_probe_failed")
            return {"ok": data.get("fts5") is True and data.get("base_prefix_in_bundle") is True and data.get("stdlib_in_bundle") is True and version == "1.18.10", "python": data.get("python"), "sqlite": data.get("sqlite"), "fts5": data.get("fts5"), "base_prefix_in_bundle": data.get("base_prefix_in_bundle"), "stdlib_in_bundle": data.get("stdlib_in_bundle"), "opencode": version}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"ok": False, "code": "runtime_probe_failed_private_details_withheld"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle")
    parser.add_argument("--probe-runtime", action="store_true", help="Execute isolated bundled Python FTS5 and OpenCode version only; no backend imports")
    args = parser.parse_args()
    result = audit_bundle(args.bundle)
    if args.probe_runtime:
        result["runtime"] = probe_runtime(args.bundle) if result["ok"] else {"ok": False, "code": "probe_skipped_failed_static_audit"}
        result["ok"] = result["ok"] and result["runtime"]["ok"]
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
