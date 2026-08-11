"""
FERAL Code Interpreter Skill
=============================
Run Python/Node snippets in a layered sandbox and capture generated artifacts.

Sandbox cascade (best-to-worst isolation):

  1. **Docker** — full container isolation (network=none, read-only fs,
     memory + cpu caps, --user nobody). Used when ``docker`` is on PATH
     and the daemon is reachable.

  2. **WASM CPython** (Python only) — in-process WebAssembly CPython via
     ``wasmtime`` + an operator-supplied CPython WASI binary referenced
     by ``FERAL_CPYTHON_WASM``. Strong syscall isolation, no host fs by
     default. The binary is not bundled — it's typically the official
     VMware Wasm Labs ``python-3.12.0.wasm`` (~80MB) and operators who
     can't run Docker install it once and point the env var at it.
     Closes AUDIT-r14 finding 16 fix #4 (code_interpreter honest
     fallback) and Lane 05 acceptance "code_interpreter works without
     Docker (WASM/pyodide fallback)".

  3. **Host with rlimit** — best-effort: tempfs CWD, ``PYTHONNOUSERSITE``,
     CPU + filesystem-write rlimits (Linux: also address-space rlimit).
     Last-resort path; flagged in the response payload so the caller
     can warn the user.

Each tier reports its label on the result so the UI / LLM can render
a "sandbox: docker | wasm | host-rlimit" badge instead of a binary
sandboxed-yes-or-no flag.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import os
import platform
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from config.loader import feral_data_home
from skills.base import BaseSkill
from skills.impl import register_skill

logger = logging.getLogger("feral.code_interpreter")

MAX_OUTPUT = 80_000
MAX_ARTIFACTS = 25
MAX_INLINE_IMAGE_BYTES = 2_000_000
MAX_INLINE_TEXT_BYTES = 200_000
TEXT_EXTS = {".txt", ".md", ".csv", ".tsv", ".json", ".html", ".xml", ".log"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

DOCKER_AVAILABLE = bool(shutil.which("docker"))


# ─────────────────────────────────────────────────────────────────
# WASM CPython runner (Python only)
# ─────────────────────────────────────────────────────────────────


def _wasm_cpython_path() -> Optional[Path]:
    """Resolve the configured CPython WASI binary, if any.

    Operators install (e.g. ``brew install wasmtime`` plus downloading
    the CPython WASI binary from https://github.com/vmware-labs/webassembly-language-runtimes/releases)
    and point ``FERAL_CPYTHON_WASM`` at the resulting ``python-X.Y.Z.wasm``.
    The cost of installing this is a one-time ~80MB download — too heavy
    to bundle, light enough to opt into.
    """
    raw = os.getenv("FERAL_CPYTHON_WASM", "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    if p.is_file():
        return p
    logger.warning(
        "FERAL_CPYTHON_WASM=%r does not point at a readable .wasm file — "
        "falling back to host-rlimit Python.",
        raw,
    )
    return None


def _wasmtime_available() -> bool:
    """``wasmtime-py`` is an optional install (extras=[wasm])."""
    try:
        import wasmtime  # noqa: F401
        return True
    except ImportError:
        return False


WASM_PYTHON_AVAILABLE = bool(_wasm_cpython_path()) and _wasmtime_available()


async def _run_wasm_python(
    code: str,
    work_dir: str,
    timeout: int = 45,
) -> dict:
    """Run *code* through the operator-installed WASM CPython.

    The wasmtime invocation runs as a subprocess so we can pipe stdout
    cleanly and enforce the timeout via ``asyncio.wait_for`` instead of
    threading wasmtime's fuel mechanism through the whole call (fuel
    measures execution time in wasm-instructions, not wall-clock — the
    subprocess gives us a wall-clock budget that matches the Docker
    path). The ``--`` separator passes the script path as argv[1] to the
    embedded Python.
    """
    wasm_bin = _wasm_cpython_path()
    if wasm_bin is None or not _wasmtime_available():
        return {
            "exit_code": 1,
            "stdout": "",
            "stderr": "WASM Python sandbox not configured (FERAL_CPYTHON_WASM unset or wasmtime missing).",
            "sandbox": "unavailable",
        }

    script_path = Path(work_dir) / "script.py"
    script_path.write_text(code)

    # Mount the work_dir into the wasm fs so the script can write
    # artifacts that the host then collects (mirrors the Docker path's
    # `-v $work_dir:/workspace`).
    cmd = [
        "wasmtime",
        "run",
        "--dir", f"{work_dir}::/workspace",
        "--env", "PYTHONHOME=/usr/local",
        "--env", "PYTHONPATH=/usr/local/lib/python3.12",
        str(wasm_bin),
        "--",
        "/workspace/script.py",
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=work_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return {
            "exit_code": proc.returncode if proc.returncode is not None else -1,
            "stdout": stdout.decode(errors="replace")[:50_000],
            "stderr": stderr.decode(errors="replace")[:10_000],
            "sandbox": "wasm",
        }
    except asyncio.TimeoutError:
        proc.kill()
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": "WASM execution timed out",
            "sandbox": "wasm",
        }
    except FileNotFoundError as exc:
        return {
            "exit_code": 1,
            "stdout": "",
            "stderr": f"wasmtime CLI not on PATH: {exc}",
            "sandbox": "unavailable",
        }
    except Exception as exc:  # pragma: no cover - defensive
        return {
            "exit_code": 1,
            "stdout": "",
            "stderr": f"WASM Python execution failed: {exc}",
            "sandbox": "unavailable",
        }


async def _try_wasm_then_host(
    code: str,
    language: str,
    work_dir: str,
    timeout: int,
    *,
    allow_unsandboxed_fallback: bool,
) -> dict:
    """Helper that picks the best non-Docker tier based on language and
    operator config. Returns a result dict with ``sandbox`` ∈
    {``wasm``, ``host-rlimit``, ``unavailable``}."""
    if language == "python" and WASM_PYTHON_AVAILABLE:
        result = await _run_wasm_python(code, work_dir, timeout)
        # If wasmtime invocation itself failed (CLI missing /
        # bad binary), fall through to host with the same
        # allow_unsandboxed_fallback gate.
        if result["sandbox"] == "wasm":
            return result
        logger.warning(
            "WASM Python sandbox unavailable at runtime: %s",
            result.get("stderr"),
        )
    if allow_unsandboxed_fallback:
        return await _run_unsandboxed(code, language, work_dir, timeout)
    return {
        "exit_code": 1,
        "stdout": "",
        "stderr": (
            "Sandbox required but Docker is unavailable and no WASM "
            "Python sandbox is configured (set FERAL_CPYTHON_WASM)."
        ),
        "sandbox": "unavailable",
    }


async def _run_sandboxed(
    code: str,
    language: str,
    work_dir: str,
    timeout: int = 300,
    *,
    allow_unsandboxed_fallback: bool = True,
) -> dict:
    """Run code in the strongest available sandbox tier.

    Cascade: Docker → WASM CPython (Python only, when configured) →
    host with rlimit. Each result carries a ``sandbox`` field labelling
    the tier that actually ran the code so callers can render an
    accurate badge.
    """
    if not DOCKER_AVAILABLE:
        return await _try_wasm_then_host(
            code, language, work_dir, timeout,
            allow_unsandboxed_fallback=allow_unsandboxed_fallback,
        )

    image = "python:3.12-slim" if language == "python" else "node:22-slim"
    script_name = "script.py" if language == "python" else "script.js"
    script_path = Path(work_dir) / script_name
    script_path.write_text(code)

    cmd = [
        "docker", "run", "--rm",
        "--network=none",
        "--memory=512m",
        "--cpus=1",
        "--read-only",
        "--tmpfs", "/tmp:rw,size=100m",
        "-v", f"{work_dir}:/workspace:rw",
        "-w", "/workspace",
        "--user", "nobody",
        image,
        "python3" if language == "python" else "node",
        f"/workspace/{script_name}",
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        stderr_str = stderr.decode(errors="replace")[:10000]
        if proc.returncode != 0 and ("docker" in stderr_str.lower() and ("daemon" in stderr_str.lower() or "connect" in stderr_str.lower())):
            logger.warning("Docker daemon not reachable — escalating to WASM/host tier")
            return await _try_wasm_then_host(
                code, language, work_dir, timeout,
                allow_unsandboxed_fallback=allow_unsandboxed_fallback,
            )
        return {
            "exit_code": proc.returncode,
            "stdout": stdout.decode(errors="replace")[:50000],
            "stderr": stderr_str,
            "sandbox": "docker",
        }
    except (asyncio.TimeoutError, OSError) as e:
        if isinstance(e, asyncio.TimeoutError):
            proc.kill()
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": "Execution timed out",
                "sandbox": "docker",
            }
        logger.warning(f"Docker execution failed: {e} — escalating to WASM/host tier")
        return await _try_wasm_then_host(
            code, language, work_dir, timeout,
            allow_unsandboxed_fallback=allow_unsandboxed_fallback,
        )


async def _run_unsandboxed(code: str, language: str, work_dir: str, timeout: int = 300) -> dict:
    """Last-resort tier: run on the host with rlimit + a scrubbed env.

    This is what ships on hosts without Docker AND without the WASM
    CPython opt-in. We do everything we can short of an OS-level
    sandbox:
      * cwd is the per-invocation tempfs directory
      * ``PYTHONNOUSERSITE`` blocks ``~/.local`` package leakage
      * env scrubbed to a minimal allowlist (PATH, HOME, LANG, TZ)
      * RLIMIT_CPU / RLIMIT_FSIZE always set; RLIMIT_AS on Linux

    The result is labelled ``sandbox: 'host-rlimit'`` so the LLM /
    UI can warn the user they're running on a soft sandbox.
    """
    script_name = "script.py" if language == "python" else "script.js"
    script_path = Path(work_dir) / script_name
    if not script_path.exists():
        script_path.write_text(code)

    argv = ["python3", script_name] if language == "python" else ["node", script_name]

    preexec = None
    if platform.system() in ("Linux", "Darwin"):
        import resource

        def _set_limits() -> None:
            resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout))
            resource.setrlimit(resource.RLIMIT_FSIZE, (100 * 1024 * 1024, 100 * 1024 * 1024))
            if platform.system() == "Linux":
                resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))

        preexec = _set_limits

    # Minimal env — the executing code shouldn't see the brain's
    # secrets, OAuth tokens, or arbitrary $PATH directories. PATH is
    # narrowed to the system locations where python3 / node live.
    safe_env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin",
        "HOME": work_dir,
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "TZ": os.environ.get("TZ", "UTC"),
        "PYTHONNOUSERSITE": "1",
        # NOTE: we intentionally do NOT pass PYTHONPATH or any FERAL_*
        # env. Code that needs more should run via Docker or WASM tiers.
    }

    logger.warning(
        "Docker + WASM unavailable — executing %s on host with best-effort rlimits",
        language,
    )

    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=work_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=preexec,
        env=safe_env,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return {
            "exit_code": proc.returncode if proc.returncode is not None else -1,
            "stdout": (stdout_b or b"").decode(errors="replace")[:50000],
            "stderr": (stderr_b or b"").decode(errors="replace")[:10000],
            "sandbox": "host-rlimit",
        }
    except asyncio.TimeoutError:
        proc.kill()
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": "Execution timed out",
            "sandbox": "host-rlimit",
        }


@register_skill
class CodeInterpreterSkill(BaseSkill):
    def __init__(self):
        super().__init__(skill_id="code_interpreter")
        default_artifacts_root = feral_data_home() / "artifacts"
        self._artifacts_root = Path(
            os.getenv("FERAL_ARTIFACTS_DIR", str(default_artifacts_root))
        ).expanduser()

    async def execute(self, endpoint_id: str, args: Dict[str, Any], vault: Dict[str, str]) -> Dict[str, Any]:
        _ = vault
        if endpoint_id == "run_python":
            return await self._run_code("python", args)
        if endpoint_id == "run_node":
            return await self._run_code("node", args)
        return {
            "success": False,
            "status_code": 404,
            "data": None,
            "error": f"Unknown endpoint: {endpoint_id}",
        }

    async def _run_code(self, language: str, args: dict) -> dict:
        code = str(args.get("code", "") or "")
        if not code.strip():
            return {"success": False, "status_code": 400, "data": None, "error": "code is required"}

        timeout = max(1, min(int(args.get("timeout", 45) or 45), 300))
        require_sandbox = bool(args.get("_feral_require_sandbox"))
        run_id = str(uuid.uuid4())[:10]
        temp_dir = Path(tempfile.mkdtemp(prefix=f"feral_code_{run_id}_"))
        script_name = "script.py" if language == "python" else "script.js"

        try:
            result = await _run_sandboxed(
                code,
                language,
                str(temp_dir),
                timeout,
                allow_unsandboxed_fallback=not require_sandbox,
            )

            stdout = result.get("stdout", "")[:MAX_OUTPUT]
            stderr = result.get("stderr", "")[:MAX_OUTPUT]
            exit_code = int(result.get("exit_code", -1))
            sandbox_tier = result.get("sandbox", "unavailable")
            # ``sandboxed`` (boolean) preserved for back-compat with
            # callers that still grep for the old field; the new
            # ``sandbox`` field is the authoritative tier label.
            sandboxed = sandbox_tier in ("docker", "wasm")

            artifacts = self._collect_artifacts(temp_dir, script_name=script_name, run_id=run_id)
            status_code = 200
            if require_sandbox and not sandboxed:
                status_code = 503
            return {
                "success": exit_code == 0,
                "status_code": status_code,
                "data": {
                    "language": language,
                    "run_id": run_id,
                    "stdout": stdout,
                    "stderr": stderr,
                    "exit_code": exit_code,
                    "sandbox": sandbox_tier,
                    "sandboxed": sandboxed,
                    "artifact_count": len(artifacts),
                    "artifacts": artifacts,
                    "artifact_dir": str(self._artifacts_root / run_id) if artifacts else None,
                },
                "error": stderr if exit_code != 0 else None,
            }
        except FileNotFoundError as e:
            return {
                "success": False,
                "status_code": 500,
                "data": None,
                "error": str(e),
            }
        finally:
            # AUDIT-FIXES F-05. Same class as the subprocess sites: blocking
            # filesystem I/O on the loop thread. The run directory holds the
            # artifacts the executed code produced, so an interpreter run can
            # make this arbitrarily large. Offloaded, not removed.
            await asyncio.to_thread(shutil.rmtree, temp_dir, ignore_errors=True)

    def _collect_artifacts(self, run_dir: Path, *, script_name: str, run_id: str) -> list[dict]:
        artifacts: list[dict] = []
        out_dir = self._artifacts_root / run_id
        out_dir.mkdir(parents=True, exist_ok=True)

        for file_path in sorted(run_dir.rglob("*")):
            if not file_path.is_file():
                continue
            if file_path.name == script_name:
                continue
            rel = file_path.relative_to(run_dir)
            dest = out_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file_path, dest)

            size = dest.stat().st_size
            ext = dest.suffix.lower()
            mime = mimetypes.guess_type(dest.name)[0] or "application/octet-stream"
            entry: dict[str, Any] = {
                "name": str(rel),
                "path": str(dest),
                "size_bytes": size,
                "mime_type": mime,
                "kind": "binary",
            }

            if ext in IMAGE_EXTS:
                entry["kind"] = "image"
                if size <= MAX_INLINE_IMAGE_BYTES:
                    entry["b64"] = base64.b64encode(dest.read_bytes()).decode("ascii")
            elif ext in TEXT_EXTS:
                entry["kind"] = "text" if ext in {".txt", ".md", ".log"} else "data"
                if size <= MAX_INLINE_TEXT_BYTES:
                    entry["text_preview"] = dest.read_text(errors="replace")[:20_000]

            artifacts.append(entry)
            if len(artifacts) >= MAX_ARTIFACTS:
                break

        return artifacts
