"""
Docker Sandbox for FERAL
Executes LLM-generated code in isolated Docker containers.
Falls back to host subprocess when Docker is unavailable.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from security.sandbox_image import resolve_image_tag

logger = logging.getLogger("feral.docker_sandbox")


def _check_docker() -> bool:
    """Return True if the Docker CLI can reach a daemon (`docker info` succeeds)."""
    docker_bin = shutil.which("docker")
    if not docker_bin:
        return False
    try:
        r = subprocess.run(
            [docker_bin, "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        logger.debug("docker info check failed: %s", e)
        return False


async def _check_docker_async() -> bool:
    """Async Docker daemon check for execute paths."""
    docker_bin = shutil.which("docker")
    if not docker_bin:
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            docker_bin,
            "info",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        code = await proc.wait()
        return code == 0
    except FileNotFoundError:
        return False
    except Exception as e:
        logger.debug("docker async check failed: %s", e)
        return False


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return default


def _resolve_default_image() -> str:
    prefer_registry = _env_flag("FERAL_SANDBOX_PREFER_REGISTRY", False)
    try:
        return resolve_image_tag("minimal", prefer_registry=prefer_registry)
    except Exception as exc:
        logger.warning("sandbox image tag resolution failed, using latest tag: %s", exc)
        return "feral-sandbox:latest"


def _normalize_seccomp_profile(raw_value: str) -> str:
    profile = str(raw_value or "").strip()
    if not profile:
        return ""
    if profile.lower() == "unconfined":
        logger.warning("Ignoring insecure seccomp profile value 'unconfined'")
        return ""
    return profile


class DockerSandbox:
    """
    Run untrusted code in a container when Docker is available; otherwise run
    the same commands on the host with timeout (best-effort fallback).
    """

    def __init__(
        self,
        image: str | None = None,
        timeout: int = 30,
        network: bool = False,
        memory_limit: str = "256m",
        pids_limit: int = 128,
        cap_drop: str = "ALL",
        no_new_privileges: bool = True,
        seccomp_profile: str = "",
    ):
        self._image = image or _resolve_default_image()
        self._timeout = timeout
        self._network = network
        self._memory_limit = memory_limit
        # Runtime hardening defaults: remove Linux capabilities, prevent
        # privilege escalation, and constrain process fan-out.
        try:
            env_pids = int(str(os.environ.get("FERAL_SANDBOX_PIDS_LIMIT", pids_limit)))
        except Exception:
            env_pids = pids_limit
        self._pids_limit = max(16, int(env_pids))
        self._cap_drop = str(os.environ.get("FERAL_SANDBOX_CAP_DROP", cap_drop) or "").strip()
        self._no_new_privileges = _env_flag(
            "FERAL_SANDBOX_NO_NEW_PRIVILEGES",
            no_new_privileges,
        )
        seccomp_env = os.environ.get("FERAL_SANDBOX_SECCOMP_PROFILE", seccomp_profile)
        self._seccomp_profile = _normalize_seccomp_profile(str(seccomp_env or ""))

    def available(self) -> bool:
        """Return True if `docker` CLI can reach the daemon."""
        return _check_docker()

    def _docker_base_cmd(self) -> list[str]:
        cmd: list[str] = [
            "docker",
            "run",
            "--rm",
            "--memory",
            self._memory_limit,
            "--cpus",
            "1",
            "--pids-limit",
            str(self._pids_limit),
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,size=128m",
        ]
        if self._cap_drop:
            cmd.extend(["--cap-drop", self._cap_drop])
        if self._no_new_privileges:
            cmd.extend(["--security-opt", "no-new-privileges"])
        if self._seccomp_profile:
            cmd.extend(["--security-opt", f"seccomp={self._seccomp_profile}"])
        cmd.extend([
            "--user",
            "sandbox",
            "-e",
            "HOME=/tmp",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "-e",
            "TMPDIR=/tmp",
        ])
        if not self._network:
            cmd.extend(["--network", "none"])
        return cmd

    async def execute(self, code: str, language: str = "python") -> dict[str, Any]:
        """Write `code` to a temp file and run it under Docker or on the host."""
        lang = (language or "python").lower().strip()
        if lang not in ("python", "python3", "node", "bash"):
            return {
                "success": False,
                "stdout": "",
                "stderr": f"Unsupported language: {language}",
                "exit_code": -1,
                "execution_time_ms": 0.0,
            }
        if lang in ("python", "python3"):
            lang = "python"
            suffix, argv_inner = ".py", ["python3", "/work/script.py"]
        elif lang == "node":
            suffix, argv_inner = ".js", ["node", "/work/script.js"]
        else:
            suffix, argv_inner = ".sh", ["bash", "/work/script.sh"]

        tmpdir = tempfile.mkdtemp(prefix="feral_sbx_")
        script_path = Path(tmpdir) / f"script{suffix}"
        try:
            script_path.write_text(code, encoding="utf-8")
            script_path.chmod(0o644)
            use_docker = await _check_docker_async()
            if use_docker:
                return await self._run_in_docker(tmpdir, argv_inner)
            return await self._run_on_host(script_path, argv_inner, tmpdir)
        finally:
            # AUDIT-FIXES F-05. rmtree is blocking filesystem I/O on the loop
            # thread, and the directory holds whatever the sandboxed run
            # wrote, so its cost is set by untrusted code. Offloaded, not
            # removed: the temp dir must still be deleted on every path.
            await asyncio.to_thread(shutil.rmtree, tmpdir, ignore_errors=True)

    async def _run_in_docker(self, host_dir: str, argv_inner: list[str]) -> dict[str, Any]:
        cmd = self._docker_base_cmd()
        cmd.extend(["-v", f"{host_dir}:/work:ro", self._image])
        cmd.extend(argv_inner)
        return await self._stream_command(cmd, cwd=None)

    async def _run_on_host(self, script_path: Path, argv_inner: list[str], cwd: str) -> dict[str, Any]:
        logger.warning("Docker unavailable — refusing to execute unsandboxed code")
        return {"exit_code": 1, "stdout": "", "stderr": "Docker unavailable. Refusing to execute unsandboxed code.", "timed_out": False}

    async def execute_shell(self, command: str) -> dict[str, Any]:
        """Run a shell command inside the sandbox image (or host fallback)."""
        if not command.strip():
            return {
                "success": False,
                "stdout": "",
                "stderr": "empty command",
                "exit_code": -1,
                "execution_time_ms": 0.0,
            }
        use_docker = await _check_docker_async()
        if use_docker:
            cmd = self._docker_base_cmd()
            cmd.extend([self._image, "bash", "-c", command])
            return await self._stream_command(cmd, cwd=None)
        logger.warning("Docker unavailable — refusing to execute unsandboxed code")
        return {"exit_code": 1, "stdout": "", "stderr": "Docker unavailable. Refusing to execute unsandboxed code.", "timed_out": False}

    async def _stream_command(self, argv: list[str], *, cwd: str | None) -> dict[str, Any]:
        start = time.perf_counter()
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._timeout,
            )
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            out = stdout_b.decode(errors="replace") if stdout_b else ""
            err = stderr_b.decode(errors="replace") if stderr_b else ""
            code = proc.returncode if proc.returncode is not None else -1
            return {
                "success": code == 0,
                "stdout": out,
                "stderr": err,
                "exit_code": code,
                "execution_time_ms": round(elapsed_ms, 3),
            }
        except asyncio.TimeoutError:
            if proc is not None:
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass
                except Exception:
                    pass
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            return {
                "success": False,
                "stdout": "",
                "stderr": f"Timed out after {self._timeout}s",
                "exit_code": -1,
                "execution_time_ms": round(elapsed_ms, 3),
            }
        except FileNotFoundError as e:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            return {
                "success": False,
                "stdout": "",
                "stderr": str(e),
                "exit_code": -1,
                "execution_time_ms": round(elapsed_ms, 3),
            }


def get_sandbox() -> DockerSandbox | None:
    """
    Factory: return a DockerSandbox if the Docker CLI is available; otherwise None.
    """
    if not shutil.which("docker"):
        return None
    if _check_docker():
        return DockerSandbox()
    return None
