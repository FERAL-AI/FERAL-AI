"""Bounded real-Process ownership acceptance in a tiny disposable app bundle.

Only the health fixture is synthetic. BrainRuntime, profile layout, health
coordinator and native_backend_launcher.py are compiled/copied unchanged.
No GUI, actual backend providers, accounts, keychain or personal defaults run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile

NATIVE = Path(__file__).resolve().parents[1]
TEST = NATIVE / "tests/NativeArchiveRuntimeOwnershipTests.swift"
SOURCES = [NATIVE / name for name in (
    "BrainRuntime.swift", "NativeProfileLayoutFeature.swift", "NativeRuntimeHealthFeature.swift",
)] + [TEST]
LAUNCHER = NATIVE / "native_backend_launcher.py"

SERVER = '''"""Disposable owned-health fixture, never a real agent backend."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

root = Path(os.environ["FERAL_ARCHIVE_OWNER_FIXTURE_ROOT"])
profile = root / "profile"
if (not str(root).startswith("/private/tmp/feral-native-archive-owner-")
        or root.resolve() != root or os.environ["FERAL_HOME"] != str(profile)
        or os.environ["FERAL_DATA_HOME"] != str(profile)):
    raise RuntimeError("The disposable fixture profile was not verified")
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
(root / "fixture-owned-processes.json").write_text(json.dumps({"backend_pid": os.getpid(), "child_pid": child.pid}))

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        body = {"status": "ok", "service_reachable": True, "agent_ready": True,
                "memory_available": True, "bootstrap_required": False, "orchestrator_available": True,
                "fixture_role": "health-only", "fixture_pid": os.getpid(), "fixture_pgrp": os.getpgrp(),
                "fixture_child_pid": child.pid, "fixture_child_pgrp": os.getpgid(child.pid),
                "fixture_home": str(profile), "fixture_data_home": str(profile)}
        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("X-Feral-Desktop-Instance", os.environ["FERAL_DESKTOP_INSTANCE_ID"])
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass

try:
    server = HTTPServer(("127.0.0.1", int(os.environ["FERAL_PORT"])), Handler)
except BaseException:
    child.terminate()
    child.wait(timeout=2)
    raise
def cleanup(signum, frame):
    server.server_close()
    if child.poll() is None:
        child.terminate()
    child.wait(timeout=2)
    raise SystemExit(0)
signal.signal(signal.SIGTERM, cleanup)
try:
    server.serve_forever(poll_interval=0.05)
finally:
    server.server_close()
    if child.poll() is None:
        child.terminate()
    child.wait(timeout=2)
'''


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stage(root: Path, interpreter: Path) -> tuple[Path, dict]:
    if (not str(root).startswith("/private/tmp/feral-native-archive-owner-")
            or root.resolve() != root or not root.is_dir() or any(root.iterdir())):
        raise ValueError("Use a fresh canonical disposable fixture directory")
    python = interpreter.resolve(strict=True)
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("The pinned fixture interpreter is not executable")
    version = subprocess.run([str(python), "--version"], check=True, capture_output=True, text=True, timeout=5).stdout.strip()
    app = root / "ArchiveOwnerFixture.app"
    contents, resources = app / "Contents", app / "Contents/Resources"
    binary = contents / "MacOS/archive-runtime-owner-tests"
    for directory in (binary.parent, resources / "python/bin", resources / "feral-core/api", root / "profile"):
        directory.mkdir(mode=0o700, parents=True)
    (resources / "python/bin/python3").symlink_to(python)
    shutil.copyfile(LAUNCHER, resources / LAUNCHER.name)
    (resources / "feral-core/api/__init__.py").write_text("")
    (resources / "feral-core/api/server.py").write_text(SERVER)
    (contents / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleExecutable": binary.name,
        "CFBundleIdentifier": "ai.feral.fixture.archive-runtime-owner",
        "CFBundleName": "Archive Owner Fixture",
        "CFBundlePackageType": "APPL",
        "LSUIElement": True,
    }))
    architecture = platform.machine()
    if architecture not in {"arm64", "x86_64"}:
        raise ValueError("Unsupported native fixture architecture")
    manifest = {"source_sha256": {path.name: digest(path) for path in SOURCES},
                "launcher_sha256": digest(LAUNCHER), "python_sha256": digest(python),
                "python_version": version, "fixture_server_sha256": digest(resources / "feral-core/api/server.py"),
                "bundle_identifier": "ai.feral.fixture.archive-runtime-owner", "signed_app": False}
    command = ["/usr/bin/xcrun", "swiftc", "-swift-version", "5", "-target", f"{architecture}-apple-macosx13.0",
               "-module-cache-path", str(root / "swift-cache"), "-parse-as-library", *map(str, SOURCES), "-o", str(binary)]
    compilation = subprocess.run(command, capture_output=True, text=True, timeout=90)
    (root / "compile.log").write_text(compilation.stdout + compilation.stderr)
    if compilation.returncode != 0:
        raise ValueError("Fixture compilation failed; inspect compile.log")
    if any(digest(path) != manifest["source_sha256"][path.name] for path in SOURCES):
        raise ValueError("Fixture production/test inputs changed during compilation")
    if digest(LAUNCHER) != manifest["launcher_sha256"] or digest(resources / LAUNCHER.name) != manifest["launcher_sha256"]:
        raise ValueError("The copied production launcher differs from its reviewed source")
    manifest["executable_sha256"] = digest(binary)
    (root / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    return binary, manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, default=NATIVE.parent / ".venv/bin/python", help="Explicit pinned test interpreter; never a model/provider executable")
    parser.add_argument("--prepare-only", action="store_true", help="Compile the disposable bundle without launching its processes")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("This fixture requires macOS Foundation and process ownership")
    root = Path(tempfile.mkdtemp(prefix="feral-native-archive-owner-", dir="/private/tmp"))
    try:
        binary, manifest = stage(root, args.python)
        if args.prepare_only:
            print(json.dumps({"status": "prepared", "fixture_root": str(root), "executable": str(binary), "manifest": manifest}, sort_keys=True))
            return 0
        environment = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "FERAL_HOME": str(root / "profile"),
                       "FERAL_DATA_HOME": str(root / "profile"), "FERAL_ARCHIVE_OWNER_FIXTURE_ROOT": str(root)}
        result = subprocess.run([str(binary)], env=environment, capture_output=True, text=True, timeout=30)
        (root / "stdout.log").write_text(result.stdout)
        (root / "stderr.log").write_text(result.stderr)
        if result.returncode != 0:
            raise ValueError("Real runtime ownership fixture failed; inspect stdout.log/stderr.log")
        evidence = json.loads(result.stdout)
        if evidence.get("status") != "passed" or evidence.get("exact_quiescence_verified") is not True:
            raise ValueError("Fixture did not produce verified ownership evidence")
        if any(digest(path) != manifest["source_sha256"][path.name] for path in SOURCES):
            raise ValueError("Fixture inputs changed during execution")
        (root / "evidence.json").write_text(json.dumps(evidence, sort_keys=True, indent=2) + "\n")
        print(json.dumps({"status": "passed", "fixture_root": str(root), "assertions": evidence["assertions"],
                          "production_sources": manifest["source_sha256"], "launcher_sha256": manifest["launcher_sha256"],
                          "executable_sha256": manifest["executable_sha256"], "signed_app_verified": False,
                          "native_model_archive_host_verified": False}, sort_keys=True))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "failed", "fixture_root": str(root), "reason": str(error)}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
