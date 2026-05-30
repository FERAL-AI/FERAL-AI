"""subagent policy allowlist + supervisor audit on deny.

Contract: default-deny, explicit allow, supervisor row recorded with
``decision="denied"``. Part of the canonical lifecycle test set:
allowlist / cron-note / lifecycle / model / scope.
"""

from __future__ import annotations

import pytest

from agents import subagent_policy
from agents.subagent_spawner import (
    SubagentNotAllowed,
    get_registry,
    register_supervisor,
    spawn_subsession,
)
from agents.supervisor import Supervisor, SupervisorStore


pytestmark = pytest.mark.no_auto_feral_home


@pytest.fixture
def supervisor(tmp_path):
    sup = Supervisor(store=SupervisorStore(db_path=str(tmp_path / "sup.db")))
    register_supervisor(sup)
    yield sup
    register_supervisor(None)


@pytest.fixture(autouse=True)
def reset_state(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    subagent_policy.clear()
    get_registry().reset()
    yield
    subagent_policy.clear()
    get_registry().reset()


@pytest.mark.asyncio
async def test_default_allowlist_permits_known_kinds(supervisor):
    child_id = await spawn_subsession(
        "parent-1", "tool_runner", scope_key="alpha"
    )
    assert isinstance(child_id, str) and len(child_id) >= 8
    rows = supervisor.recent(decision="allowed")
    assert any(r["kind"] == "subagent_spawn" for r in rows)


@pytest.mark.asyncio
async def test_default_allowlist_denies_unknown_kind(supervisor):
    with pytest.raises(SubagentNotAllowed) as excinfo:
        await spawn_subsession("parent-1", "shell_exec", scope_key="alpha")
    assert "shell_exec" in str(excinfo.value)

    denied = supervisor.recent(decision="denied")
    assert len(denied) == 1
    row = denied[0]
    assert row["kind"] == "subagent_spawn"
    assert row["session_id"] == "parent-1"
    assert row["decision"] == "denied"
    assert row["detail"]["child_kind"] == "shell_exec"
    assert row["detail"]["reason"] == "policy_denied"


@pytest.mark.asyncio
async def test_register_allowed_extends_policy(supervisor):
    with pytest.raises(SubagentNotAllowed):
        await spawn_subsession("parent-1", "data_pipeline", scope_key="alpha")
    subagent_policy.register_allowed("orchestrator", ["data_pipeline"])
    child_id = await spawn_subsession("parent-1", "data_pipeline", scope_key="alpha")
    assert child_id


@pytest.mark.asyncio
async def test_unknown_parent_kind_defaults_to_deny(supervisor):
    get_registry().set_parent_kind("worker-7", "research")
    with pytest.raises(SubagentNotAllowed):
        await spawn_subsession("worker-7", "tool_runner", scope_key="alpha")
    denied = supervisor.recent(decision="denied")
    assert any(
        r["detail"].get("parent_kind") == "research"
        and r["detail"].get("child_kind") == "tool_runner"
        for r in denied
    )


@pytest.mark.asyncio
async def test_disk_policy_overrides_when_present(tmp_path, monkeypatch, supervisor):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    (tmp_path / "subagent_policy.json").write_text(
        '{"orchestrator": ["custom_kind"]}'
    )
    subagent_policy.clear()

    with pytest.raises(SubagentNotAllowed):
        await spawn_subsession("parent-1", "tool_runner", scope_key="alpha")

    child_id = await spawn_subsession("parent-1", "custom_kind", scope_key="alpha")
    assert child_id


@pytest.mark.asyncio
async def test_clear_resets_to_defaults(supervisor):
    subagent_policy.register_allowed("orchestrator", ["scratch"])
    assert subagent_policy.is_allowed("orchestrator", "scratch")
    subagent_policy.clear()
    assert not subagent_policy.is_allowed("orchestrator", "scratch")
    assert subagent_policy.is_allowed("orchestrator", "tool_runner")
