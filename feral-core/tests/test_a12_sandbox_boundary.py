"""-A12 regression tests for sandbox boundary enforcement."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from models.skill_manifest import BrandProfile, SkillEndpoint, SkillManifest
from skills.executor import SkillExecutor
from skills.impl.workspace_scripts import WorkspaceScriptsSkill

pytestmark = pytest.mark.no_auto_feral_home


def test_manifest_parses_requires_sandbox_flag() -> None:
    skill = SkillManifest(
        skill_id="sandboxed_example",
        requires_sandbox=True,
        brand=BrandProfile(name="Sandboxed"),
        description="sandbox test",
        endpoints=[
            SkillEndpoint(
                id="run",
                method="PYTHON",
                url="",
                description="run",
                requires_sandbox=True,
            )
        ],
    )
    assert skill.requires_sandbox is True
    assert skill.endpoints[0].requires_sandbox is True


@pytest.mark.asyncio
async def test_executor_refuses_requires_sandbox_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = SkillExecutor(daemons={})
    skill = SkillManifest(
        skill_id="workspace_scripts",
        requires_sandbox=True,
        brand=BrandProfile(name="Workspace Scripts"),
        description="run scripts",
        endpoints=[SkillEndpoint(id="run", method="CUSTOM", url="", description="run")],
    )
    endpoint = skill.endpoints[0]

    monkeypatch.setattr(
        executor,
        "_sandbox_requirement_status",
        lambda _skill, _endpoint: (False, "Docker sandbox unavailable"),
    )

    out = await executor.execute("workspace_scripts__run", {"code": "print(1)"}, skill, endpoint)
    assert out["success"] is False
    assert out["status_code"] == 503
    assert "Sandbox required" in (out["error"] or "")


@pytest.mark.asyncio
async def test_workspace_scripts_strict_mode_refuses_host_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "run.py"
    script.write_text("print('ok')", encoding="utf-8")

    # Ensure `from api.state import state` resolves to an object without
    # docker sandbox so strict mode must fail closed.
    import api.state as state_module

    monkeypatch.setattr(state_module, "state", SimpleNamespace(docker_sandbox=None))

    host_runner = AsyncMock(
        return_value={"stdout": "host", "stderr": "", "exit_code": 0, "sandboxed": False}
    )
    monkeypatch.setattr(WorkspaceScriptsSkill, "_run_on_host", host_runner)

    skill = WorkspaceScriptsSkill()
    out = await skill._execute_script(
        "python",
        script,
        timeout=5,
        forwarded_args="",
        require_sandbox=True,
    )

    assert out["exit_code"] == 1
    assert "Sandbox required" in (out["stderr"] or "")
    host_runner.assert_not_awaited()
