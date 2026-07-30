"""``coding_tools__bash`` must never silently fall back to the host when
the caller is generated code, and must never execute outside a granted
workspace.

Originally this file pinned ``computer_use__bash`` (PR2): the impl had to
honour ``_feral_require_sandbox`` with a truthful 503 instead of running on
the host. ``computer_use`` has since been folded into ``coding_tools`` (the
two manifests exposed identical endpoints with identical trigger phrases),
so the same contract is pinned here against the surviving skill.

The workspace-scoped host mode added alongside does not weaken it: a
generated-code caller still requires Docker, and a developer shell still
requires a grant.
"""

from __future__ import annotations

import pytest

from security.sandbox_policy import SandboxPolicy
from skills.impl.coding_tools import CodingToolsSkill


@pytest.mark.asyncio
async def test_bash_refuses_host_when_sandbox_required_and_docker_missing(
    monkeypatch, tmp_path,
) -> None:
    skill = CodingToolsSkill()

    # Force the docker probe to report nothing healthy, the same situation
    # as a laptop without Docker Desktop running.
    monkeypatch.setattr(skill, "_resolve_docker_sandbox", lambda: None)

    # Grant the folder so the refusal cannot be attributed to a missing
    # grant: generated code is refused even inside a granted workspace.
    SandboxPolicy.load_default().grant_folder(str(tmp_path), mode="readwrite")

    result = await skill.execute(
        "bash",
        {"command": "echo hi", "cwd": str(tmp_path), "_feral_require_sandbox": True},
        vault={},
    )

    assert result["success"] is False
    assert result["status_code"] == 503
    err = result.get("error", "") or ""
    assert "docker" in err.lower()
    data = result.get("data") or {}
    assert data.get("sandbox") == "unavailable"
    assert data.get("needs") == "docker"
    setup = data.get("setup_step") or ""
    assert "Docker" in setup
    # Crucially: must NOT have executed on host. We assert this by the
    # absence of stdout/exit_code keys.
    assert "stdout" not in data
    assert "exit_code" not in data


@pytest.mark.asyncio
async def test_bash_runs_on_host_inside_a_granted_workspace(monkeypatch, tmp_path) -> None:
    """Without ``requires_sandbox``, a shell in a granted folder runs on the
    host. Docker does not need to be present, which was the whole bug."""
    skill = CodingToolsSkill()

    monkeypatch.setattr(skill, "_resolve_docker_sandbox", lambda: None)
    monkeypatch.setenv("FERAL_SANDBOX_BASH", "false")
    SandboxPolicy.load_default().grant_folder(str(tmp_path), mode="readwrite")

    result = await skill.execute(
        "bash", {"command": "echo canonical-test", "cwd": str(tmp_path)}, vault={},
    )
    assert result["success"] is True
    data = result.get("data") or {}
    assert data.get("sandbox") == "host"
    assert data.get("execution_mode") == "host_workspace"
    assert "canonical-test" in (data.get("stdout") or "")
