#!/usr/bin/env python3
"""Verify a relocated, extracted unsigned Linux x86_64 Debian payload.

Runs only its bundled Python/OpenCode and a disposable no-provider brain. This
is not native GUI, clean-machine installation, OS-permission or release signing
acceptance. The caller extracts the actual .deb with dpkg-deb, not a source tree.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from desktop_bundle_smoke import assert_no_development_data, stop_process_group

_HELPER = Path(__file__).parents[1] / "desktop/scripts/bundle_platform.py"
_SPEC = importlib.util.spec_from_file_location("linux_smoke_platform", _HELPER)
platform_check = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = platform_check
_SPEC.loader.exec_module(platform_check)

MAX_ENTRIES = 150_000
MAX_PAYLOAD_BYTES = 3 * 1024**3
MAX_HTTP_BYTES = 16 * 1024**2


class LinuxBundleError(ValueError):
    """Fixed public diagnostics; payload contents and subprocess stderr excluded."""


def inventory(root: Path) -> dict:
    """Content/mode/link digest, excluding external or special-file payloads."""
    if root.is_symlink() or not root.is_dir():
        raise LinuxBundleError("extracted_payload_root_invalid")
    root = root.resolve(strict=True)
    digest = hashlib.sha256()
    entries = total = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs.sort()
        files.sort()
        for name in sorted(dirs + files):
            path = Path(directory) / name
            entries += 1
            if entries > MAX_ENTRIES:
                raise LinuxBundleError("payload_entry_budget_exceeded")
            mode = path.lstat().st_mode
            relative = path.relative_to(root).as_posix()
            digest.update(json.dumps([relative, mode], separators=(",", ":")).encode())
            if stat.S_ISLNK(mode):
                try:
                    if not path.resolve(strict=True).is_relative_to(root):
                        raise LinuxBundleError("payload_symlink_external")
                except (OSError, RuntimeError) as error:
                    raise LinuxBundleError("payload_symlink_unresolvable") from error
                digest.update(os.readlink(path).encode())
            elif stat.S_ISREG(mode):
                size = path.stat().st_size
                total += size
                if total > MAX_PAYLOAD_BYTES:
                    raise LinuxBundleError("payload_byte_budget_exceeded")
                file_digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(128 * 1024), b""):
                        file_digest.update(chunk)
                digest.update(file_digest.digest())
            elif not stat.S_ISDIR(mode):
                raise LinuxBundleError("payload_special_file_refused")
    return {"entries": entries, "bytes": total, "sha256": digest.hexdigest()}


def payload_paths(root: Path) -> tuple[Path, Path, Path, Path]:
    resources = root / "usr/lib/feral-desktop"
    native = root / "usr/bin/feral-desktop"
    python = resources / "python/bin/python3"
    opencode = resources / "opencode/bin/opencode"
    core = resources / "feral-core"
    for path in (native, python, opencode, core / "api/server.py", core / "webui_v2/index.html"):
        if not path.is_file() or not path.resolve(strict=True).is_relative_to(root.resolve(strict=True)):
            raise LinuxBundleError("required_payload_file_missing_or_external")
    for executable in (native, python, opencode):
        if not os.access(executable, os.X_OK):
            raise LinuxBundleError("required_payload_file_not_executable")
        with executable.open("rb") as stream:
            platform_check.validate_binary_header(stream.read(64), platform_check.select_platform("Linux", "x86_64", "glibc"))
    if (resources / "python/pyvenv.cfg").exists():
        raise LinuxBundleError("bundled_python_is_virtualenv")
    return resources, core, python, opencode


def isolated_environment(workspace: Path, opencode: Path) -> dict[str, str]:
    home = workspace / "feral-home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({
        "llm": {"provider": "none", "fallback_providers": []},
        "features": {"proactive": False, "self_learning": False},
        "memory": {"sync": {"enabled": False}},
    }))
    for name in ("empty-path", "user-home", "xdg-cache", "xdg-config", "xdg-data"):
        (workspace / name).mkdir()
    return {
        "PATH": str(workspace / "empty-path"), "HOME": str(workspace / "user-home"),
        "TMPDIR": str(workspace), "LANG": "C.UTF-8",
        "XDG_CACHE_HOME": str(workspace / "xdg-cache"),
        "XDG_CONFIG_HOME": str(workspace / "xdg-config"),
        "XDG_DATA_HOME": str(workspace / "xdg-data"),
        # Actual backend policy puts config/data under FERAL_HOME. Do not imply
        # that a different DATA_HOME was honored by the current runtime.
        "FERAL_HOME": str(home), "FERAL_DATA_HOME": str(home),
        "FERAL_EMBED_PROVIDER": "hash", "FERAL_LLM_PROVIDER": "none",
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "FERAL_API_KEY": secrets.token_hex(32), "FERAL_BIND_HOST": "127.0.0.1",
        "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
        "FERAL_OPENCODE_BIN": str(opencode),
    }


def runtime_probe(python: Path, core: Path, env: dict[str, str], workspace: Path) -> dict:
    # Internal JSON paths are inspected but not emitted as credential or task data.
    code = (
        "import json,sys,sqlite3,os,keyring,feral_sdk,api.server; "
        "c=sqlite3.connect(':memory:'); c.execute('CREATE VIRTUAL TABLE f USING fts5(content)'); "
        "assert type(keyring.get_keyring()).__module__ == 'keyring.backends.null'; "
        "print(json.dumps({'executable':sys.executable,'prefix':sys.base_prefix,"
        "'stdlib':os.__file__,'sys_path':sys.path,'sqlite':sqlite3.sqlite_version,"
        "'python_version':'.'.join(map(str,sys.version_info[:3])),"
        "'server':api.server.__file__,'sdk':feral_sdk.__file__}))"
    )
    result = subprocess.run([str(python), "-c", code], cwd=core, env=env,
                            capture_output=True, timeout=60, check=False)
    if result.returncode or len(result.stdout) > 131_072:
        raise LinuxBundleError("bundled_runtime_probe_failed")
    try:
        runtime = json.loads(result.stdout)
        python_root = python.parent.parent.resolve(strict=True)
        for key in ("executable", "prefix", "stdlib", "sdk"):
            if not Path(runtime[key]).resolve(strict=True).is_relative_to(python_root):
                raise LinuxBundleError("external_runtime_dependency")
        if Path(runtime["server"]).resolve(strict=True) != (core / "api/server.py").resolve(strict=True):
            raise LinuxBundleError("external_core_import")
        for entry in runtime["sys_path"]:
            if entry and not Path(entry).resolve().is_relative_to(core.parents[3].resolve(strict=True)):
                raise LinuxBundleError("external_import_path")
    except (KeyError, TypeError, ValueError, OSError) as error:
        raise LinuxBundleError("bundled_runtime_identity_invalid") from error
    (workspace / "runtime-probe.json").write_text(json.dumps(runtime, indent=2) + "\n")
    return runtime


def asset_references(html: str) -> list[str]:
    refs = re.findall(r'(?:src|href)=["\']([^"\']*assets/[^"\']+\.(?:js|css))["\']', html)
    if not any(ref.endswith(".js") for ref in refs) or not any(ref.endswith(".css") for ref in refs):
        raise LinuxBundleError("dashboard_entrypoints_missing")
    if len(refs) > 64 or any(len(ref) > 2048 for ref in refs):
        raise LinuxBundleError("dashboard_asset_budget_exceeded")
    for ref in refs:
        parsed = urlsplit(ref)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or "%" in parsed.path or ".." in parsed.path.split("/"):
            raise LinuxBundleError("dashboard_asset_external")
    return refs


def check_coding_catalog(catalog: dict, opencode: Path) -> None:
    if not isinstance(catalog, dict) or not isinstance(catalog.get("agents"), list):
        raise LinuxBundleError("coding_catalog_shape_invalid")
    entries = [entry for entry in catalog["agents"] if isinstance(entry, dict) and entry.get("agent_id") == "opencode"]
    if len(entries) != 1 or entries[0].get("available") is not True:
        raise LinuxBundleError("bundled_OpenCode_catalog_unavailable")
    command = entries[0].get("command")
    if not isinstance(command, list) or not command or not isinstance(command[0], str) or Path(command[0]).resolve() != opencode.resolve():
        raise LinuxBundleError("bundled_OpenCode_catalog_external")


def check_runtime_identity(runtime: dict, expected_python: str) -> None:
    if runtime.get("python_version") != expected_python:
        raise LinuxBundleError("bundled_python_version_mismatch")


def run(extracted: Path, workspace: Path, timeout: float, expected_python: str) -> dict:
    selected = platform_check.host_platform()
    if selected.binary_format != "elf":
        raise LinuxBundleError("Linux_x86_64_glibc_verification_host_required")
    if workspace.is_symlink() or not workspace.is_dir():
        raise LinuxBundleError("owned_workspace_invalid")
    extracted = extracted.absolute()
    workspace = workspace.resolve(strict=True)
    source_root = extracted.resolve(strict=True)
    if workspace.is_relative_to(source_root) or source_root.is_relative_to(workspace):
        raise LinuxBundleError("source_and_workspace_overlap")
    original = inventory(extracted)
    payload_paths(extracted)
    if shutil.disk_usage(workspace).free < original["bytes"] + 1024**3:
        raise LinuxBundleError("relocation_disk_budget_unavailable")
    relocated = workspace / "Relocated Linux Payload"
    shutil.copytree(extracted, relocated, symlinks=True)
    baseline = inventory(relocated)
    if baseline != original:
        raise LinuxBundleError("relocation_payload_identity_mismatch")
    resources, core, python, opencode = payload_paths(relocated)
    assert_no_development_data(resources)
    env = isolated_environment(workspace, opencode)
    coding = subprocess.run([str(opencode), "--version"], env=env, cwd=workspace,
                            capture_output=True, timeout=20, check=False)
    if coding.returncode or coding.stdout.strip() != b"1.18.10":
        raise LinuxBundleError("bundled_OpenCode_version_mismatch")
    runtime = runtime_probe(python, core, env, workspace)
    check_runtime_identity(runtime, expected_python)
    env["PATH"] = os.pathsep.join((str(python.parent), str(opencode.parent)))
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    env["FERAL_PORT"] = str(port)
    instance = "linux-bundle-smoke-" + secrets.token_hex(16)
    env["FERAL_DESKTOP_INSTANCE_ID"] = instance
    base = f"http://127.0.0.1:{port}"
    opener = build_opener(ProxyHandler({}))

    def fetch(path: str) -> tuple[bytes, str]:
        request = Request(urljoin(base + "/", path), headers={"Authorization": "Bearer " + env["FERAL_API_KEY"]})
        with opener.open(request, timeout=2) as response:
            if path == "/health" and response.headers.get("X-Feral-Desktop-Instance") != instance:
                raise LinuxBundleError("health_responder_owner_mismatch")
            body = response.read(MAX_HTTP_BYTES + 1)
            if len(body) > MAX_HTTP_BYTES:
                raise LinuxBundleError("HTTP_response_budget_exceeded")
            return body, response.headers.get("Content-Type", "")

    log_path = workspace / "brain.log"
    with log_path.open("wb") as log:
        child = subprocess.Popen([str(python), "-m", "api.server"], cwd=core, env=env,
                                 stdout=log, stderr=log, start_new_session=True)
        try:
            deadline = time.monotonic() + timeout
            while True:
                if child.poll() is not None:
                    raise LinuxBundleError("packaged_brain_exited_before_health")
                try:
                    health, _ = fetch("/health")
                    if json.loads(health).get("status") == "ok":
                        break
                except (URLError, TimeoutError, ValueError) as error:
                    # Connection/startup failures may settle, but a wrong owned
                    # responder or oversized response must never be accepted.
                    if isinstance(error, LinuxBundleError):
                        raise
                if time.monotonic() >= deadline:
                    raise LinuxBundleError("packaged_brain_health_deadline_exceeded")
                time.sleep(0.2)
            coding_body, _ = fetch("/api/coding")
            catalog = json.loads(coding_body)
            check_coding_catalog(catalog, opencode)
            refs = asset_references((core / "webui_v2/index.html").read_text())
            body, _ = fetch("/")
            if not all(ref in body.decode() for ref in refs):
                raise LinuxBundleError("HTTP_dashboard_identity_mismatch")
            for ref in refs:
                asset, content_type = fetch(ref)
                if not asset or "text/html" in content_type:
                    raise LinuxBundleError("HTTP_dashboard_asset_missing")
        finally:
            stop_process_group(child)
    with socket.socket() as check:
        check.settimeout(1)
        if check.connect_ex(("127.0.0.1", port)) == 0:
            raise LinuxBundleError("owned_listener_survived_shutdown")
    if inventory(relocated) != baseline or inventory(extracted) != original:
        raise LinuxBundleError("payload_changed_during_smoke")
    return {"payload_smoke": "passed", "payload_inventory": baseline,
            "python_version": runtime["python_version"], "sqlite": runtime["sqlite"],
            "OpenCode_version": "1.18.10", "OpenCode_catalog": "bundled executable verified",
            "assets_verified": len(refs), "source_preserved": True, "owned_listener_stopped": True,
            "profile_layout": "FERAL_HOME and FERAL_DATA_HOME are the same disposable root",
            "providers": "none; no task submitted", "os_keychain": "test-only null backend",
            "native_gui": "not tested", "clean_machine_installation": "not tested",
            "release_signing": "not tested", "log": str(log_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extracted-root", type=Path, required=True)
    parser.add_argument("--work-parent", type=Path, required=True)
    parser.add_argument("--python-pin", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args()
    if not 5 <= args.timeout <= 180:
        parser.error("timeout must be between 5 and 180 seconds")
    expected_python = args.python_pin.read_text().strip()
    if not re.fullmatch(r"3\.11\.[0-9]{1,3}", expected_python):
        parser.error("python pin is not a supported version")
    args.work_parent.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="feral-linux-bundle-smoke-", dir=args.work_parent))
    try:
        receipt = run(args.extracted_root, workspace, args.timeout, expected_python)
    except (LinuxBundleError, platform_check.BundlePlatformError, OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        receipt = {"payload_smoke": "failed", "error_class": type(error).__name__,
                   "workspace": str(workspace), "evidence": "preserved; inspect owned brain.log"}
        (workspace / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(receipt))
        return 1
    (workspace / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
