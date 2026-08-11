"""Blocking calls inside ``async def`` freeze every other coroutine.

AUDIT-FIXES F-05. Six sites ran synchronous work on the event loop thread:

    api/routes/apps.py:379                subprocess.run  git clone, timeout=120
    api/routes/apps.py:397                shutil.rmtree   of the fresh clone
    skills/marketplace.py:245             subprocess.run  git pull, timeout=30
    api/routes/system_permissions.py:115  subprocess.run  open, timeout=3
    security/docker_sandbox.py:199        shutil.rmtree   of the sandbox tmpdir
    skills/impl/code_interpreter.py:448   shutil.rmtree   of the run dir

The worst is the clone: a coroutine that never awaits holds the loop, so a
slow or hostile git remote stops voice streaming, websocket heartbeats and
every in-flight HTTP request for up to two minutes.

Measurement follows the pattern established in tests/perf/test_memory_latency.py
and tests/test_embedding_loop_liveness.py: count the ticks of a 1 ms pulse
coroutine running alongside the work. Wall clock says the work took N ms
either way; only the pulse count says whether anything else could run while
it did. Where the fix is ``asyncio.to_thread`` the tests also assert the work
happened on a different thread, because a wrapper that awaits something
trivial and then still calls the blocking function inline would satisfy a
tick count on a fast machine.

Nothing here touches the network or a real git. The subprocess tests put a
fake ``git`` / ``open`` shell script on PATH that sleeps, so the real spawn,
argv, exit code and stderr capture are all exercised; the rmtree tests
substitute a deliberate ``time.sleep``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

# Long enough that a blocked loop is unambiguous, short enough to keep the
# suite quick. A free loop ticks ~100+ times in this window; a blocked one
# ticks approximately zero.
_BLOCK_SECONDS = 0.20
_PULSE_MS = 0.001
_MIN_TICKS = 20

# Timeout-and-kill tests: the child outlives its (monkeypatched) budget, and
# we then wait past the child's own runtime to prove it never finished.
_CHILD_RUNTIME = 0.6
_SHORT_TIMEOUT = 0.15
_WAIT_PAST_CHILD = 0.9


class _Ticks:
    def __init__(self) -> None:
        self.ticks = 0


@contextlib.asynccontextmanager
async def _pulsing():
    """Count 1 ms loop ticks that fire while the body runs."""
    counter = _Ticks()
    stop = False

    async def _pulse():
        nonlocal stop
        while not stop:
            await asyncio.sleep(_PULSE_MS)
            counter.ticks += 1

    task = asyncio.create_task(_pulse())
    try:
        yield counter
    finally:
        stop = True
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class _ShutilProxy:
    """Stands in for a module's ``shutil`` global with a slow ``rmtree``.

    Substituting the module attribute rather than patching the real
    ``shutil.rmtree`` keeps the slow version scoped to the module under test,
    and lets the proxy call the genuine rmtree without recursing.
    """

    def __init__(self, seen: dict) -> None:
        self._seen = seen

    def rmtree(self, path, ignore_errors=False):
        self._seen["thread"] = threading.get_ident()
        time.sleep(_BLOCK_SECONDS)
        shutil.rmtree(path, ignore_errors=ignore_errors)

    def __getattr__(self, name):
        return getattr(shutil, name)


@pytest.fixture
def fake_bin(tmp_path):
    """Put a scripted stand-in for a real binary first on PATH.

    PATH is saved and restored by hand rather than with monkeypatch.setenv:
    the shared monkeypatch fixture is torn down *after* conftest's
    ``restore_process_env`` takes its snapshot, so setenv here would be
    reported as an environment leak by the suite-level guard.
    """
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    previous_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{bindir}{os.pathsep}{previous_path}"

    def _make(name, *, sleep=0.0, exit_code=0, stderr="", marker=None):
        lines = ["#!/bin/sh"]
        if sleep:
            lines.append(f"sleep {sleep}")
        if marker is not None:
            lines.append(f': > "{marker}"')
        if stderr:
            lines.append(f"printf '%s' '{stderr}' >&2")
        lines.append(f"exit {exit_code}")
        path = bindir / name
        path.write_text("\n".join(lines) + "\n")
        path.chmod(0o755)
        return path

    try:
        yield _make
    finally:
        os.environ["PATH"] = previous_path


# ─── api/routes/apps.py — git clone + rmtree ──────────────────────────


@pytest.fixture
def apps_route(monkeypatch):
    from api.routes import apps as mod

    class _Registry:
        def install_app(self, source, **kwargs):
            raise RuntimeError("install must not be reached in these tests")

    monkeypatch.setattr(mod.state, "app_registry", _Registry(), raising=False)
    return mod


def _git_request(apps_route):
    return apps_route.InstallRequest(git_url="https://example.invalid/app.git")


class TestInstallFromGitUrl:
    @pytest.mark.asyncio
    async def test_clone_does_not_freeze_the_loop(self, apps_route, fake_bin):
        """The regression. Before the fix the pulse count is ~0 for the whole
        clone, which is up to 120s of dead loop in production."""
        from fastapi import HTTPException

        fake_bin("git", sleep=_BLOCK_SECONDS, exit_code=1, stderr="boom")

        async with _pulsing() as pulse:
            with pytest.raises(HTTPException) as excinfo:
                await apps_route.install_app(_git_request(apps_route))

        # Error semantics are unchanged: same status, same detail string.
        assert excinfo.value.status_code == 400
        assert excinfo.value.detail == "git clone failed: boom"
        assert pulse.ticks > _MIN_TICKS, (
            f"the event loop ticked only {pulse.ticks} times during a "
            f"{_BLOCK_SECONDS}s git clone, so the clone ran on the loop "
            f"thread and every other coroutine was frozen"
        )

    @pytest.mark.asyncio
    async def test_a_timed_out_clone_kills_the_child(
        self, apps_route, fake_bin, tmp_path, monkeypatch
    ):
        """asyncio.wait_for cancels the await, not the process. Without an
        explicit kill every timed-out install leaks a running git clone."""
        marker = tmp_path / "clone-ran-to-completion"
        fake_bin("git", sleep=_CHILD_RUNTIME, marker=marker)
        monkeypatch.setattr(apps_route, "GIT_CLONE_TIMEOUT_S", _SHORT_TIMEOUT)

        with pytest.raises(subprocess.TimeoutExpired):
            await apps_route.install_app(_git_request(apps_route))

        await asyncio.sleep(_WAIT_PAST_CHILD)
        assert not marker.exists(), "the timed-out git clone was left running"

    @pytest.mark.asyncio
    async def test_clone_cleanup_runs_off_the_loop_thread(
        self, apps_route, fake_bin, monkeypatch
    ):
        """The finally block deletes a freshly cloned repository, so it is
        thousands of unlink() calls, not one."""
        from fastapi import HTTPException

        fake_bin("git", exit_code=1, stderr="nope")
        seen: dict = {}
        monkeypatch.setattr(apps_route, "shutil", _ShutilProxy(seen))
        loop_thread = threading.get_ident()

        async with _pulsing() as pulse:
            with pytest.raises(HTTPException):
                await apps_route.install_app(_git_request(apps_route))

        assert seen["thread"] != loop_thread, "rmtree ran on the event loop thread"
        assert pulse.ticks > _MIN_TICKS, (
            f"the event loop ticked only {pulse.ticks} times while the clone "
            f"directory was deleted"
        )


# ─── skills/marketplace.py — git pull ─────────────────────────────────


@pytest.fixture
def marketplace(monkeypatch, tmp_path):
    from skills import marketplace as mod

    skills_dir = tmp_path / "skills"
    (skills_dir / "demo" / ".git").mkdir(parents=True)
    monkeypatch.setattr(mod, "SKILLS_DIR", skills_dir)

    client = mod.MarketplaceClient.__new__(mod.MarketplaceClient)
    client._skill_registry = None
    return mod, client


class TestMarketplaceUpdate:
    @pytest.mark.asyncio
    async def test_pull_does_not_freeze_the_loop(self, marketplace, fake_bin):
        """The regression, 30s of dead loop in production."""
        mod, client = marketplace
        fake_bin("git", sleep=_BLOCK_SECONDS)

        async with _pulsing() as pulse:
            out = await client.update("demo")

        assert out == {"success": True, "skill_id": "demo", "method": "git_pull"}
        assert pulse.ticks > _MIN_TICKS, (
            f"the event loop ticked only {pulse.ticks} times during a "
            f"{_BLOCK_SECONDS}s git pull"
        )

    @pytest.mark.asyncio
    async def test_a_failed_pull_returns_the_same_error_string(
        self, marketplace, fake_bin
    ):
        """Preservation guard, passes before and after. ``check=True`` has no
        equivalent on create_subprocess_exec, so CalledProcessError is now
        raised by hand; this pins the returned error text to what
        subprocess.run produced."""
        mod, client = marketplace
        fake_bin("git", exit_code=3, stderr="not a fast-forward")

        out = await client.update("demo")

        expected = str(subprocess.CalledProcessError(3, ["git", "pull", "--ff-only"]))
        assert out == {"success": False, "error": expected}

    @pytest.mark.asyncio
    async def test_a_timed_out_pull_kills_the_child(
        self, marketplace, fake_bin, tmp_path, monkeypatch
    ):
        """An abandoned git pull would keep writing into the skill directory
        the caller is about to re-read."""
        mod, client = marketplace
        marker = tmp_path / "pull-ran-to-completion"
        fake_bin("git", sleep=_CHILD_RUNTIME, marker=marker)
        monkeypatch.setattr(mod, "GIT_PULL_TIMEOUT_S", _SHORT_TIMEOUT)

        out = await client.update("demo")

        expected = str(
            subprocess.TimeoutExpired(["git", "pull", "--ff-only"], _SHORT_TIMEOUT)
        )
        assert out == {"success": False, "error": expected}
        await asyncio.sleep(_WAIT_PAST_CHILD)
        assert not marker.exists(), "the timed-out git pull was left running"


# ─── api/routes/system_permissions.py — open <deeplink> ───────────────


class TestOpenSystemPermission:
    @pytest.mark.asyncio
    async def test_open_does_not_freeze_the_loop(self, fake_bin, monkeypatch):
        from api.routes import system_permissions as mod

        fake_bin("open", sleep=_BLOCK_SECONDS)
        monkeypatch.setattr(mod, "platform", SimpleNamespace(system=lambda: "Darwin"))

        async with _pulsing() as pulse:
            out = await mod.open_system_permission({"permission_key": "accessibility"})

        assert out == {"ok": True}
        assert pulse.ticks > _MIN_TICKS, (
            f"the event loop ticked only {pulse.ticks} times while waiting on "
            f"`open`"
        )

    @pytest.mark.asyncio
    async def test_a_timed_out_open_is_reported_and_killed(
        self, fake_bin, tmp_path, monkeypatch
    ):
        from api.routes import system_permissions as mod

        marker = tmp_path / "open-ran-to-completion"
        fake_bin("open", sleep=_CHILD_RUNTIME, marker=marker)
        monkeypatch.setattr(mod, "platform", SimpleNamespace(system=lambda: "Darwin"))
        monkeypatch.setattr(mod, "OPEN_DEEPLINK_TIMEOUT_S", _SHORT_TIMEOUT)

        out = await mod.open_system_permission({"permission_key": "accessibility"})

        assert out["ok"] is False
        assert "timed out" in out["reason"]
        await asyncio.sleep(_WAIT_PAST_CHILD)
        assert not marker.exists(), "the timed-out `open` was left running"


# ─── security/docker_sandbox.py — tmpdir cleanup ──────────────────────


class TestDockerSandboxCleanup:
    @pytest.mark.asyncio
    async def test_tempdir_cleanup_runs_off_the_loop_thread(self, monkeypatch):
        """The directory holds whatever the sandboxed run wrote, so untrusted
        code sets how long this rmtree takes."""
        from security import docker_sandbox as mod

        async def _no_docker():
            return False

        monkeypatch.setattr(mod, "_check_docker_async", _no_docker)
        seen: dict = {}
        monkeypatch.setattr(mod, "shutil", _ShutilProxy(seen))
        sandbox = mod.DockerSandbox(image="feral-sandbox:test")
        loop_thread = threading.get_ident()

        async with _pulsing() as pulse:
            out = await sandbox.execute("print(1)")

        # Host fallback still refuses to execute unsandboxed code.
        assert out["exit_code"] == 1
        assert seen["thread"] != loop_thread, "rmtree ran on the event loop thread"
        assert pulse.ticks > _MIN_TICKS, (
            f"the event loop ticked only {pulse.ticks} times while the sandbox "
            f"temp directory was deleted"
        )


# ─── skills/impl/code_interpreter.py — run dir cleanup ────────────────


class TestCodeInterpreterCleanup:
    @pytest.mark.asyncio
    async def test_run_dir_cleanup_runs_off_the_loop_thread(
        self, monkeypatch, tmp_path
    ):
        from skills.impl import code_interpreter as mod

        skill = mod.CodeInterpreterSkill()
        # Set after construction rather than via FERAL_ARTIFACTS_DIR so the
        # test writes no artifacts into the real data home and leaks no env.
        skill._artifacts_root = tmp_path / "artifacts"

        async def _fake_run_sandboxed(
            code, language, work_dir, timeout, *, allow_unsandboxed_fallback=True
        ):
            return {"stdout": "ok", "stderr": "", "exit_code": 0, "sandbox": "docker"}

        monkeypatch.setattr(mod, "_run_sandboxed", _fake_run_sandboxed)
        seen: dict = {}
        monkeypatch.setattr(mod, "shutil", _ShutilProxy(seen))
        loop_thread = threading.get_ident()

        async with _pulsing() as pulse:
            out = await skill._run_code("python", {"code": "print(1)"})

        assert out["success"] is True
        assert seen["thread"] != loop_thread, "rmtree ran on the event loop thread"
        assert pulse.ticks > _MIN_TICKS, (
            f"the event loop ticked only {pulse.ticks} times while the run "
            f"directory was deleted"
        )
