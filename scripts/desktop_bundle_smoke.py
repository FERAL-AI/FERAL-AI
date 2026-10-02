#!/usr/bin/env python3
"""Relocate and smoke-test a macOS desktop app's bundled brain payload.

Uses only the stdlib of the verification host; runtime checks execute the
app's own interpreter with no external Python on PATH. Does not launch the
native GUI, test its Quit handler, sign/notarize, or contact model providers.
"""
from __future__ import annotations

import argparse
from fnmatch import fnmatch
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from urllib.error import URLError
from urllib.parse import urljoin
from urllib.request import ProxyHandler, Request, build_opener


def assert_no_development_data(resources: Path) -> None:
    """Inspect source payload filenames only, including installed brain packages."""
    roots = [resources / "feral-core"]
    for metadata in (resources / "python").glob("lib/python*/site-packages/feral_ai-*.dist-info"):
        top_level = metadata / "top_level.txt"
        if not top_level.is_file():
            raise RuntimeError("installed brain package lacks top-level source inventory")
        for name in top_level.read_text().splitlines():
            if not name.isidentifier():
                raise RuntimeError("invalid installed brain source inventory")
            source = metadata.parent / name
            if source.is_dir():
                roots.append(source)
    forbidden_names = {
        ".regress_home", ".feral", ".mypy_cache", ".ruff_cache", "coverage.xml",
        "credentials.json", "credentials.enc", "secrets.json", "secrets.enc",
    }
    forbidden_patterns = (".env*", ".coverage*", "*.db", "*.db-*", "*.sqlite*",
                          "*.pem", "*.p12", "*.pfx", "*.key")
    found = []
    for root in roots:
        for directory, dirs, files in os.walk(root):
            for name in dirs + files:
                if name in forbidden_names or any(fnmatch(name, pattern) for pattern in forbidden_patterns):
                    found.append(str((Path(directory) / name).relative_to(resources)))
            # Filenames suffice; do not descend into a private runtime vault.
            dirs[:] = [name for name in dirs if name not in forbidden_names]
    if found:
        raise RuntimeError("development or credential data in bundle: " + ", ".join(sorted(found)))


def stop_process_group(child: subprocess.Popen) -> None:
    """Reap the brain and stop descendants belonging to this smoke run."""
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=5)
    # A parent may terminate before one of its children. Kill the remaining
    # isolated group, never an unrelated process listening on the chosen port.
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def verify_bundle_signature(app: Path) -> None:
    verification = subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
        capture_output=True, text=True, timeout=30,
    )
    if verification.returncode:
        raise RuntimeError("bundle signature integrity failed: " + verification.stderr[:1500])


def run(app: Path, workspace: Path, timeout: float) -> dict:
    if not (app / "Contents/Resources").is_dir():
        raise RuntimeError(f"not an assembled macOS app: {app}")
    relocated = workspace / "Relocated FERAL.app"
    shutil.copytree(app, relocated, symlinks=True)
    with (relocated / "Contents/Info.plist").open("rb") as plist:
        bundle_info = plistlib.load(plist)
    executable_name = bundle_info.get("CFBundleExecutable", "")
    if not isinstance(executable_name, str) or not executable_name or Path(executable_name).name != executable_name:
        raise RuntimeError("bundle has an invalid native executable name")
    native_executable = relocated / "Contents/MacOS" / executable_name
    if (not native_executable.is_file() or not os.access(native_executable, os.X_OK)
            or not native_executable.resolve().is_relative_to(relocated)):
        raise RuntimeError("bundle lacks its self-contained native host executable")
    verify_bundle_signature(relocated)
    resources = relocated / "Contents/Resources"
    core = resources / "feral-core"
    python_root = resources / "python"
    python = python_root / "bin/python3"
    opencode = resources / "opencode/bin/opencode"
    if not python.is_file() or not (core / "api/server.py").is_file():
        raise RuntimeError("bundle lacks its interpreter or brain")
    if not opencode.is_file() or not os.access(opencode, os.X_OK):
        raise RuntimeError("bundle lacks its executable coding runtime")
    assert_no_development_data(resources)
    for path in resources.rglob("*"):
        if path.is_symlink() and not path.resolve().is_relative_to(relocated):
            raise RuntimeError(f"nonrelocatable runtime symlink: {path}")
    if (python_root / "pyvenv.cfg").exists():
        raise RuntimeError("bundled interpreter is a virtualenv")
    home = workspace / "feral-home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({
        "llm": {"provider": "none", "fallback_providers": []},
        "features": {"proactive": False, "self_learning": False},
        "memory": {"sync": {"enabled": False}},
    }))
    (workspace / "empty-path").mkdir()
    env = {
        "PATH": str(workspace / "empty-path"),
        "HOME": str(workspace / "user-home"),
        "TMPDIR": str(workspace),
        "LANG": "en_US.UTF-8",
        "FERAL_HOME": str(home), "FERAL_DATA_HOME": str(home / "data"),
        "FERAL_EMBED_PROVIDER": "hash", "FERAL_LLM_PROVIDER": "none",
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "FERAL_API_KEY": "desktop-smoke-local-only",
        "FERAL_BIND_HOST": "127.0.0.1", "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        # HOME/FERAL_HOME do not isolate macOS Keychain. This disposable
        # no-credentials verification must never query the user's OS vault.
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
        "FERAL_OPENCODE_BIN": str(opencode),
    }
    coding_probe = subprocess.run([str(opencode), "--version"], env=env,
                                  cwd=workspace, capture_output=True, text=True,
                                  timeout=15, check=True)
    coding_version = coding_probe.stdout.strip()
    if coding_version != "1.18.10":
        raise RuntimeError(f"unexpected bundled coding runtime: {coding_version}")
    subprocess.run([str(python), "-c", (
        "import keyring; "
        "assert type(keyring.get_keyring()).__module__ == 'keyring.backends.null'; "
        "keyring.set_password('feral-smoke-test', 'fixture', 'fake-test-value'); "
        "assert keyring.get_password('feral-smoke-test', 'fixture') is None; "
        "keyring.delete_password('feral-smoke-test', 'fixture')"
    )], cwd=workspace, env=env, capture_output=True, text=True, timeout=15, check=True)
    probe = subprocess.run([str(python), "-c", (
        "import json,sys,sqlite3,os; "
        "c=sqlite3.connect(':memory:'); "
        "c.execute('CREATE VIRTUAL TABLE f USING fts5(content)'); "
        "print(json.dumps({'executable':sys.executable,'prefix':sys.prefix,"
        "'stdlib':os.__file__,'sys_path':sys.path,'sqlite':sqlite3.sqlite_version}))"
    )], cwd=core, env=env, capture_output=True, text=True, timeout=15, check=True)
    runtime = json.loads(probe.stdout)
    for key in ("executable", "prefix", "stdlib"):
        if not Path(runtime[key]).resolve().is_relative_to(python_root):
            raise RuntimeError(f"external runtime dependency: {key}={runtime[key]}")
    for path in runtime["sys_path"]:
        if path and not Path(path).resolve().is_relative_to(relocated):
            raise RuntimeError(f"external import path: {path}")
    # Match the native host's internal runtime PATH, with no user executables.
    env["PATH"] = os.pathsep.join((str(python.parent), str(opencode.parent)))
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    env["FERAL_PORT"] = str(port)
    instance = f"bundle-smoke-{os.getpid()}-{time.time_ns()}"
    env["FERAL_DESKTOP_INSTANCE_ID"] = instance
    base = f"http://127.0.0.1:{port}"
    opener = build_opener(ProxyHandler({}))

    def fetch(path: str) -> tuple[bytes, str]:
        request = Request(urljoin(base + "/", path), headers={
            "Authorization": "Bearer desktop-smoke-local-only",
        })
        with opener.open(request, timeout=2) as response:
            if path == "/health" and response.headers.get("X-Feral-Desktop-Instance") != instance:
                raise RuntimeError("health responder is not this smoke run's brain")
            return response.read(), response.headers.get("Content-Type", "")

    log_path = workspace / "brain.log"
    with log_path.open("wb") as log:
        child = subprocess.Popen([str(python), "-m", "api.server"], cwd=core,
                                 env=env, stdout=log, stderr=log, start_new_session=True)
        try:
            deadline = time.monotonic() + timeout
            while True:
                if child.poll() is not None:
                    raise RuntimeError(f"packaged brain exited {child.returncode}; see {log_path}")
                try:
                    health, _ = fetch("/health")
                    if json.loads(health).get("status") == "ok":
                        break
                except (URLError, TimeoutError, ValueError):
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"brain health timed out; see {log_path}")
                time.sleep(0.2)
            expected = (core / "webui_v2/index.html").read_text()
            coding_body, _ = fetch("/api/coding")
            coding_catalog = json.loads(coding_body)
            coding_agent = next((entry for entry in coding_catalog.get("agents", [])
                                 if entry.get("agent_id") == "opencode"), None)
            if not coding_agent or not coding_agent.get("available"):
                raise RuntimeError("packaged coding catalog does not expose available OpenCode")
            command = coding_agent.get("command", [])
            if not command or Path(command[0]).resolve() != opencode.resolve():
                raise RuntimeError("coding catalog did not resolve the bundled executable")
            refs = re.findall(r'(?:src|href)=["\']([^"\']*assets/[^"\']+\.(?:js|css))["\']', expected)
            if not any(ref.endswith(".js") for ref in refs) or not any(ref.endswith(".css") for ref in refs):
                raise RuntimeError("built dashboard entrypoints absent")
            body, _ = fetch("/")
            html = body.decode()
            if not all(ref in html for ref in refs):
                raise RuntimeError("HTTP root is not the staged dashboard")
            for ref in refs:
                asset, content_type = fetch(ref)
                if not asset or "text/html" in content_type:
                    raise RuntimeError(f"dashboard asset unavailable: {ref}")
        finally:
            stop_process_group(child)
    with socket.socket() as check:
        check.settimeout(1)
        if check.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError("listener survived packaged-brain shutdown")
    # Startup must not write bytecode or any other state into sealed resources.
    verify_bundle_signature(relocated)
    return {"payload_smoke": "passed", "runtime": runtime, "assets": refs,
            "signature_integrity_before_and_after_boot": "passed",
            "os_keychain": "excluded by test-only null keyring backend",
            "bundle": {"executable": executable_name,
                       "identifier": bundle_info.get("CFBundleIdentifier"),
                       "version": bundle_info.get("CFBundleShortVersionString"),
                       "minimum_macos": bundle_info.get("LSMinimumSystemVersion")},
            "coding_runtime": {"version": coding_version, "external_node": False},
            "coding_catalog": coding_agent,
            "workspace": str(workspace), "log": str(log_path),
            "native_gui_lifecycle": "not tested", "linux": "not tested"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args()
    workspace = args.workspace or Path(tempfile.mkdtemp(prefix="feral-desktop-smoke-"))
    workspace.mkdir(parents=True, exist_ok=True)
    print(json.dumps(run(args.app.resolve(), workspace.resolve(), args.timeout), indent=2))


if __name__ == "__main__":
    main()
