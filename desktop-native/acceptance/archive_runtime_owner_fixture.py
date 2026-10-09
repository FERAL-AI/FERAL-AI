"""Bounded real-Process ownership acceptance in a tiny disposable app bundle.

Only the health fixture is synthetic. BrainRuntime, profile layout, health
coordinator and native_backend_launcher.py are compiled/copied unchanged.
No GUI, actual backend providers, accounts, keychain or personal defaults run.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import http.client
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

NATIVE = Path(__file__).resolve().parents[1]
TEST = NATIVE / "tests/NativeArchiveRuntimeOwnershipTests.swift"
SOURCES = [NATIVE / name for name in (
    "BrainRuntime.swift", "NativeProfileLayoutFeature.swift", "NativeRuntimePortFeature.swift", "NativeRuntimeHealthFeature.swift",
)] + [TEST]
LAUNCHER = NATIVE / "native_backend_launcher.py"
ASSEMBLER = NATIVE / "assemble.py"
DIAGNOSTIC_FILES = ("manifest.json", "evidence.json", "failure.json", "phases.json",
                    "stdout.log", "stderr.log", "compile.log", "fixture-owned-processes.json",
                    "fixture-server-events.jsonl", "fixture-passive-health.json",
                    "profile/native-desktop.log")
MAX_DIAGNOSTIC_BYTES = 65536

SERVER = '''"""Disposable owned-health fixture, never a real agent backend."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import TCPServer

root = Path(os.environ["FERAL_ARCHIVE_OWNER_FIXTURE_ROOT"])
profile = root / "profile"
if (not str(root).startswith("/private/tmp/feral-native-archive-owner-")
        or root.resolve() != root or os.environ["FERAL_HOME"] != str(profile)
        or os.environ["FERAL_DATA_HOME"] != str(profile)):
    raise RuntimeError("The disposable fixture profile was not verified")
event_count = 0
def event(phase, request_kind="none"):
    global event_count
    if event_count >= 128:
        return
    event_count += 1
    with (root / "fixture-server-events.jsonl").open("a") as stream:
        stream.write(json.dumps({"phase": phase, "request_kind": request_kind,
                                "pid": os.getpid(), "uptime": time.monotonic()}) + "\\n")
    os.chmod(root / "fixture-server-events.jsonl", 0o600)

event("server_module_entered")
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
metadata = {"backend_pid": os.getpid(), "child_pid": child.pid,
            "port": int(os.environ["FERAL_PORT"]), "instance_id": os.environ["FERAL_DESKTOP_INSTANCE_ID"]}
(root / "fixture-owned-processes.json").write_text(json.dumps(metadata))
os.chmod(root / "fixture-owned-processes.json", 0o600)
event("child_spawned")

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        kind = "observer" if self.headers.get("X-Feral-Fixture-Observer") == "passive-parent" else "native"
        event("health_get_entered", kind)
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
        self.wfile.flush()
        event("health_write_completed", kind)

    def log_message(self, *args):
        pass

class OwnedLoopbackHTTPServer(HTTPServer):
    def server_bind(self):
        if self.server_address[0] != "127.0.0.1":
            raise ValueError("The synthetic health listener must remain IPv4 loopback")
        event("socket_bind_entered")
        TCPServer.server_bind(self)
        event("socket_bind_returned")
        # HTTPServer normally resolves a hostname here. This synthetic listener
        # is an explicit IPv4 endpoint; external reverse DNS is not its contract.
        self.server_name = "localhost"
        self.server_port = self.server_address[1]

    def server_activate(self):
        event("listener_activate_entered")
        super().server_activate()
        event("listener_activate_returned")

try:
    server = OwnedLoopbackHTTPServer(("127.0.0.1", int(os.environ["FERAL_PORT"])), Handler)
    event("listener_ready")
except BaseException:
    child.terminate()
    child.wait(timeout=2)
    raise
def cleanup(signum, frame):
    event("cleanup_entered")
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


def local_network_policy() -> dict:
    """Read the assembler's literal app policy without running assembly."""
    tree = ast.parse(ASSEMBLER.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "info" for target in node.targets):
            policy = ast.literal_eval(node.value).get("NSAppTransportSecurity")
            if policy == {"NSAllowsLocalNetworking": True} and type(policy["NSAllowsLocalNetworking"]) is bool:
                return policy
    raise ValueError("The actual app local-network policy could not be verified")


def observe_owned_health(root: Path, stop: threading.Event, deadline: float) -> None:
    """Passive independent health observation; never runtime authority or actions."""
    observations = []
    attempts: dict[tuple[int, str], int] = {}
    while not stop.is_set() and time.monotonic() < deadline:
        path = root / "fixture-owned-processes.json"
        summary = None
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
                stop.wait(0.05)
                continue
            owner = json.loads(path.read_text())
            pid, child, port, instance = (owner[key] for key in ("backend_pid", "child_pid", "port", "instance_id"))
            if (type(pid) is not int or type(child) is not int or min(pid, child) <= 1 or pid == child
                    or type(port) is not int or not 0 < port <= 65535 or not isinstance(instance, str)
                    or str(uuid.UUID(instance)).upper() != instance.upper()
                    or os.getpgid(pid) != pid or os.getpgid(child) != pid):
                stop.wait(0.05)
                continue
            identity = (pid, instance)
            attempt = attempts.get(identity, 0)
            if attempt >= 10:
                stop.wait(0.05)
                continue
            attempts[identity] = attempt + 1
            summary = {"backend_pid": pid, "attempt": attempt, "uptime": time.monotonic(), "code": "transport_failure"}
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=min(0.25, max(0.001, deadline - time.monotonic())))
            try:
                connection.request("GET", "/health", headers={"X-Feral-Fixture-Observer": "passive-parent"})
                response = connection.getresponse()
                payload = response.read(8193)
                if len(payload) > 8192:
                    summary["code"] = "response_too_large"
                else:
                    body = json.loads(payload)
                    matched = (response.status == 200 and response.getheader("X-Feral-Desktop-Instance") == instance
                               and isinstance(body, dict) and body.get("fixture_role") == "health-only"
                               and body.get("fixture_pid") == pid and body.get("fixture_pgrp") == pid
                               and body.get("fixture_child_pid") == child and body.get("fixture_child_pgrp") == pid
                               and os.getpgid(pid) == pid and os.getpgid(child) == pid)
                    summary["code"] = "exact_owned_health_verified" if matched else "identity_or_http_refused"
                    if matched:
                        attempts[identity] = 10
            finally:
                connection.close()
        except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
            if summary is None:
                stop.wait(0.05)
                continue
        observations.append(summary)
        observations = observations[-32:]
        destination = root / "fixture-passive-health.json"
        temporary = root / "fixture-passive-health.tmp"
        temporary.write_text(json.dumps({"synthetic_fixture": True, "passive_only": True, "observations": observations}, sort_keys=True))
        os.chmod(temporary, 0o600)
        temporary.replace(destination)
        stop.wait(0.1)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def export_diagnostics(root: Path, destination: Path) -> None:
    """Export only bounded synthetic fixture evidence, never the app/profile tree."""
    entries = []
    for relative in DIAGNOSTIC_FILES:
        source = root / relative
        if source.is_symlink() or not source.is_file():
            continue
        with source.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            if size > MAX_DIAGNOSTIC_BYTES:
                stream.seek(size - MAX_DIAGNOSTIC_BYTES)
            payload = stream.read(MAX_DIAGNOSTIC_BYTES)
        output = destination / relative.replace("/", "-")
        with output.open("xb") as stream:
            os.chmod(output, 0o600)
            stream.write(payload)
        entries.append({"file": output.name, "original_bytes": size, "exported_bytes": len(payload),
                        "truncated": size > MAX_DIAGNOSTIC_BYTES,
                        "export_sha256": hashlib.sha256(payload).hexdigest()})
    summary = {"synthetic_fixture": True, "fixture_root": str(root),
               "maximum_bytes_per_file": MAX_DIAGNOSTIC_BYTES, "files": entries}
    (destination / "diagnostics.json").write_text(json.dumps(summary, sort_keys=True, indent=2) + "\n")
    os.chmod(destination / "diagnostics.json", 0o600)


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
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        saved_port = listener.getsockname()[1]
    settings = root / "profile/settings.json"
    settings.write_text(json.dumps({"network": {"port": saved_port}}))
    os.chmod(settings, 0o600)
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
        "NSAppTransportSecurity": local_network_policy(),
    }))
    architecture = platform.machine()
    if architecture not in {"arm64", "x86_64"}:
        raise ValueError("Unsupported native fixture architecture")
    manifest = {"saved_port": saved_port, "source_sha256": {path.name: digest(path) for path in SOURCES},
                "launcher_sha256": digest(LAUNCHER), "python_sha256": digest(python),
                "python_version": version, "fixture_server_sha256": digest(resources / "feral-core/api/server.py"),
                "assembler_sha256": digest(ASSEMBLER), "local_network_policy": local_network_policy(),
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
    if digest(ASSEMBLER) != manifest["assembler_sha256"]:
        raise ValueError("The reviewed app-info source changed during fixture preparation")
    manifest["executable_sha256"] = digest(binary)
    (root / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    return binary, manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, default=NATIVE.parent / ".venv/bin/python", help="Explicit pinned test interpreter; never a model/provider executable")
    parser.add_argument("--prepare-only", action="store_true", help="Compile the disposable bundle without launching its processes")
    parser.add_argument("--evidence-output", type=Path, help="Fresh directory for bounded synthetic diagnostic files only")
    parser.add_argument("--execution-timeout", type=float, default=30,
                        help="Bounded fixture execution deadline; lower values support failure-path checks (maximum 30 seconds)")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("This fixture requires macOS Foundation and process ownership")
    if not 0 < args.execution_timeout <= 30:
        parser.error("Execution timeout must be greater than zero and at most 30 seconds")
    evidence_output = None
    if args.evidence_output:
        target = args.evidence_output.absolute()
        if target.exists() or target.is_symlink() or target.parent.resolve() != target.parent:
            parser.error("Use a fresh evidence directory beneath an existing canonical parent")
        try:
            target.mkdir(mode=0o700)
            evidence_output = target
        except OSError:
            parser.error("The fresh evidence directory could not be created")
    root = Path(tempfile.mkdtemp(prefix="feral-native-archive-owner-", dir="/private/tmp"))
    entered = time.monotonic()
    try:
        binary, manifest = stage(root, args.python)
        if args.prepare_only:
            print(json.dumps({"status": "prepared", "fixture_root": str(root), "executable": str(binary), "manifest": manifest}, sort_keys=True))
            return 0
        environment = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "FERAL_HOME": str(root / "profile"),
                       "FERAL_DATA_HOME": str(root / "profile"), "FERAL_ARCHIVE_OWNER_FIXTURE_ROOT": str(root),
                       "FERAL_ARCHIVE_OWNER_SAVED_PORT": str(manifest["saved_port"])}
        # File streams persist even if run() kills the native host on timeout.
        # Do not lose the actual startup/retirement phase as the old harness did.
        with (root / "stdout.log").open("xb") as stdout, (root / "stderr.log").open("xb") as stderr:
            os.chmod(root / "stdout.log", 0o600)
            os.chmod(root / "stderr.log", 0o600)
            process = subprocess.Popen([str(binary)], env=environment, stdout=stdout, stderr=stderr)
            deadline = time.monotonic() + args.execution_timeout
            observer_stop = threading.Event()
            observer = threading.Thread(target=observe_owned_health, args=(root, observer_stop, deadline), daemon=True)
            observer.start()
            try:
                returncode = process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                # Same native-host deadline and cleanup semantics as run(timeout=).
                # Closing its lifeline lets the unchanged production launcher
                # retire only its own backend group, without observer signals.
                process.kill()
                process.wait(timeout=2)
                raise
            finally:
                observer_stop.set()
                observer.join(timeout=1)
        if returncode != 0:
            raise ValueError("Real runtime ownership fixture failed; inspect stdout.log/stderr.log")
        if (root / "stdout.log").stat().st_size > MAX_DIAGNOSTIC_BYTES:
            raise ValueError("Fixture receipt exceeds the bounded evidence contract")
        evidence = json.loads((root / "stdout.log").read_text())
        if evidence.get("status") != "passed" or evidence.get("exact_quiescence_verified") is not True:
            raise ValueError("Fixture did not produce verified ownership evidence")
        if any(digest(path) != manifest["source_sha256"][path.name] for path in SOURCES):
            raise ValueError("Fixture inputs changed during execution")
        if digest(ASSEMBLER) != manifest["assembler_sha256"]:
            raise ValueError("The reviewed app-info source changed during execution")
        (root / "evidence.json").write_text(json.dumps(evidence, sort_keys=True, indent=2) + "\n")
        print(json.dumps({"status": "passed", "fixture_root": str(root), "assertions": evidence["assertions"],
                          "production_sources": manifest["source_sha256"], "launcher_sha256": manifest["launcher_sha256"],
                          "executable_sha256": manifest["executable_sha256"], "signed_app_verified": False,
                          "native_model_archive_host_verified": False}, sort_keys=True))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        failure = {"status": "failed", "synthetic_fixture": True, "fixture_root": str(root),
                   "reason": str(error), "elapsed_seconds": time.monotonic() - entered,
                   "execution_timeout_seconds": args.execution_timeout}
        (root / "failure.json").write_text(json.dumps(failure, sort_keys=True, indent=2) + "\n")
        os.chmod(root / "failure.json", 0o600)
        print(json.dumps(failure, sort_keys=True), file=sys.stderr)
        return 1
    finally:
        if evidence_output:
            export_diagnostics(root, evidence_output)


if __name__ == "__main__":
    raise SystemExit(main())
