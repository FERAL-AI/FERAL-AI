"""-A14 regression tests for narrow state facades.

Verifies the ``SandboxPort`` facade is honored by the high-impact skill
execution paths (``SkillExecutor`` and ``WorkspaceScriptsSkill``) and that
those paths no longer require touching the global ``api.state`` module to
swap out sandbox behavior.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock

import pytest

from models.skill_manifest import BrandProfile, SkillEndpoint, SkillManifest
from skills.executor import SkillExecutor
from skills.impl.workspace_scripts import WorkspaceScriptsSkill
from skills.sandbox_ports import (
    BrainStateSandboxPort,
    SandboxPort,
    default_sandbox_port,
)

pytestmark = pytest.mark.no_auto_feral_home


class _StubPort:
    """Minimal in-memory sandbox port for tests."""

    def __init__(
        self,
        docker: Optional[Any] = None,
        wasm: Optional[Any] = None,
    ) -> None:
        self._docker = docker
        self._wasm = wasm
        self.docker_calls = 0
        self.wasm_calls = 0

    def get_docker_sandbox(self) -> Optional[Any]:
        self.docker_calls += 1
        return self._docker

    def get_wasm_sandbox(self) -> Optional[Any]:
        self.wasm_calls += 1
        return self._wasm


def _sandbox_required_skill() -> tuple[SkillManifest, SkillEndpoint]:
    skill = SkillManifest(
        skill_id="workspace_scripts",
        requires_sandbox=True,
        brand=BrandProfile(name="Workspace Scripts"),
        description="run scripts",
        endpoints=[SkillEndpoint(id="run", method="CUSTOM", url="", description="run")],
    )
    return skill, skill.endpoints[0]


def test_sandbox_port_protocol_is_satisfied() -> None:
    port = BrainStateSandboxPort()
    assert isinstance(port, SandboxPort)
    assert isinstance(default_sandbox_port(), SandboxPort)


def test_sandbox_port_docker_unhealthy_reports_unavailable() -> None:
    """SkillExecutor consults the injected port (no api.state lookup)."""

    docker = SimpleNamespace(available=lambda: False)
    port = _StubPort(docker=docker)
    executor = SkillExecutor(daemons={}, sandbox_port=port)
    skill, endpoint = _sandbox_required_skill()

    ok, reason = executor._sandbox_requirement_status(skill, endpoint)
    assert ok is False
    assert reason == "Docker sandbox is not healthy"
    assert port.docker_calls == 1


def test_sandbox_port_docker_missing_reports_unavailable() -> None:
    port = _StubPort(docker=None)
    executor = SkillExecutor(daemons={}, sandbox_port=port)
    skill, endpoint = _sandbox_required_skill()

    ok, reason = executor._sandbox_requirement_status(skill, endpoint)
    assert ok is False
    assert reason == "Docker sandbox unavailable"


def test_sandbox_port_docker_healthy_passes_gate() -> None:
    docker = SimpleNamespace(available=lambda: True)
    port = _StubPort(docker=docker)
    executor = SkillExecutor(daemons={}, sandbox_port=port)
    skill, endpoint = _sandbox_required_skill()

    ok, reason = executor._sandbox_requirement_status(skill, endpoint)
    assert ok is True
    assert reason == "ok"


def test_sandbox_port_wasm_uses_port_when_executor_has_no_local_wasm() -> None:
    wasm = SimpleNamespace(available=lambda: True)
    port = _StubPort(docker=None, wasm=wasm)
    executor = SkillExecutor(daemons={}, sandbox_port=port)
    skill = SimpleNamespace(runtime="wasm")
    endpoint = SimpleNamespace(runtime=None)

    ok, reason = executor._sandbox_requirement_status(skill, endpoint)
    assert ok is True
    assert reason == "ok"
    assert port.wasm_calls == 1


@pytest.mark.asyncio
async def test_executor_set_sandbox_port_swaps_facade() -> None:
    executor = SkillExecutor(daemons={})
    skill, endpoint = _sandbox_required_skill()

    healthy = _StubPort(docker=SimpleNamespace(available=lambda: True))
    executor.set_sandbox_port(healthy)
    ok, _ = executor._sandbox_requirement_status(skill, endpoint)
    assert ok is True

    broken = _StubPort(docker=None)
    executor.set_sandbox_port(broken)
    ok, reason = executor._sandbox_requirement_status(skill, endpoint)
    assert ok is False
    assert reason == "Docker sandbox unavailable"


@pytest.mark.asyncio
async def test_executor_propagates_sandbox_port_to_backing_impl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Impl:
        def __init__(self) -> None:
            self.bound_port = None

        def set_sandbox_port(self, port) -> None:
            self.bound_port = port

        async def execute(self, endpoint_id: str, args: dict, vault: dict) -> dict:
            return {"success": True, "status_code": 200, "data": {"ok": True}}

    impl = _Impl()
    port = _StubPort(docker=SimpleNamespace(available=lambda: True))
    executor = SkillExecutor(daemons={}, sandbox_port=port)

    import skills.impl as impl_module
    monkeypatch.setattr(
        impl_module,
        "get_implementation",
        lambda skill_id: impl if skill_id == "sandboxed_example" else None,
    )

    skill = SkillManifest(
        skill_id="sandboxed_example",
        requires_sandbox=True,
        brand=BrandProfile(name="Sandboxed"),
        description="sandbox test",
        endpoints=[SkillEndpoint(id="run", method="CUSTOM", url="", description="run")],
    )
    endpoint = skill.endpoints[0]

    out = await executor.execute("sandboxed_example__run", {}, skill, endpoint)
    assert out["success"] is True
    assert impl.bound_port is port


@pytest.mark.asyncio
async def test_workspace_scripts_uses_injected_port_for_execution(
    tmp_path: Path,
) -> None:
    script = tmp_path / "run.py"
    script.write_text("print('ok')", encoding="utf-8")

    docker = SimpleNamespace(
        available=lambda: True,
        execute=AsyncMock(
            return_value={"stdout": "sb", "stderr": "", "exit_code": 0}
        ),
    )
    port = _StubPort(docker=docker)
    skill = WorkspaceScriptsSkill(sandbox_port=port)

    out = await skill._execute_script(
        "python", script, timeout=5, forwarded_args="", require_sandbox=True
    )

    assert out["exit_code"] == 0
    assert out["sandboxed"] is True
    assert out["stdout"] == "sb"
    docker.execute.assert_awaited_once()
    assert port.docker_calls == 1


@pytest.mark.asyncio
async def test_workspace_scripts_strict_fails_closed_via_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Strict-mode execution fails closed without touching api.state.

    The host runner must not be invoked when the port reports no sandbox.
    """

    script = tmp_path / "run.py"
    script.write_text("print('ok')", encoding="utf-8")

    host_runner = AsyncMock(
        return_value={"stdout": "host", "stderr": "", "exit_code": 0, "sandboxed": False}
    )
    monkeypatch.setattr(WorkspaceScriptsSkill, "_run_on_host", host_runner)

    skill = WorkspaceScriptsSkill(sandbox_port=_StubPort(docker=None))
    out = await skill._execute_script(
        "python", script, timeout=5, forwarded_args="", require_sandbox=True
    )

    assert out["exit_code"] == 1
    assert "Sandbox required" in (out["stderr"] or "")
    host_runner.assert_not_awaited()


@pytest.mark.asyncio
async def test_workspace_scripts_set_sandbox_port_overrides_default(
    tmp_path: Path,
) -> None:
    script = tmp_path / "run.py"
    script.write_text("print('ok')", encoding="utf-8")

    skill = WorkspaceScriptsSkill()
    docker = SimpleNamespace(
        available=lambda: True,
        execute=AsyncMock(
            return_value={"stdout": "via-set", "stderr": "", "exit_code": 0}
        ),
    )
    skill.set_sandbox_port(_StubPort(docker=docker))

    out = await skill._execute_script(
        "python", script, timeout=5, forwarded_args="", require_sandbox=True
    )
    assert out["sandboxed"] is True
    assert out["stdout"] == "via-set"


def test_default_port_lazy_imports_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default port re-reads ``api.state.state`` on every call.

    Guards the contract relied on by -A12 regression tests that swap the
    state module attribute at runtime.
    """

    import api.state as state_module

    sentinel = object()
    monkeypatch.setattr(
        state_module,
        "state",
        SimpleNamespace(docker_sandbox=sentinel, wasm_sandbox=None),
    )

    port = BrainStateSandboxPort()
    assert port.get_docker_sandbox() is sentinel
    assert port.get_wasm_sandbox() is None


def test_default_port_handles_missing_state_attribute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import api.state as state_module

    monkeypatch.delattr(state_module, "state", raising=False)
    port = BrainStateSandboxPort()
    assert port.get_docker_sandbox() is None
    assert port.get_wasm_sandbox() is None


def test_executor_sandbox_path_does_not_import_api_state_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_sandbox_requirement_status`` must not re-import ``api.state``.

    Catches accidental regressions of the -A14 facade indirection.
    """

    executor = SkillExecutor(
        daemons={},
        sandbox_port=_StubPort(docker=SimpleNamespace(available=lambda: True)),
    )
    skill, endpoint = _sandbox_required_skill()

    seen: list[str] = []
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def tracking_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "api.state":
            seen.append(name)
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", tracking_import)

    sys.modules.pop("api.state", None) if False else None  # don't actually evict
    ok, _ = executor._sandbox_requirement_status(skill, endpoint)
    assert ok is True
    assert seen == []
