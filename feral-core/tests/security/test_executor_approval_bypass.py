""" — Executor approval-bypass: the supervisor gate cannot be
bypassed via the  ``spawn_subsession`` escape hatch, and a paused
supervisor blocks every wrapped orchestrator entry point regardless of
forged "admin" context flags.

Bypass attempts simulated:
  1. Spawn a child kind that is NOT in the  default allowlist;
     the runner must NEVER be invoked, the supervisor must record a
     subagent_spawn denial.
  2. Wrap a fake orchestrator + pause the supervisor + invoke
     ``handle_command`` with a context dict full of forged "admin"
     flags. The supervisor must raise SupervisorBlocked and record
     decision="denied" reason="supervisor_paused".
"""

from __future__ import annotations

import pytest

from agents import subagent_policy
from agents.subagent_spawner import (
    SubagentNotAllowed,
    get_registry,
    register_parent_kind,
    register_supervisor,
    spawn_subsession,
)
from agents.supervisor import Supervisor, SupervisorBlocked, SupervisorStore


@pytest.fixture
def supervisor(tmp_path):
    return Supervisor(store=SupervisorStore(str(tmp_path / "supervisor.db")))


@pytest.fixture(autouse=True)
def _isolated_registry():
    reg = get_registry()
    reg.reset()
    subagent_policy.clear()
    yield
    reg.reset()
    subagent_policy.clear()


@pytest.mark.asyncio
async def test_disallowed_child_kind_is_blocked_and_audited(supervisor):
    register_supervisor(supervisor)
    parent_id = "parent-session-A"
    register_parent_kind(parent_id, "orchestrator")

    invoked: list[dict] = []

    async def runner(**kwargs):
        invoked.append(kwargs)

    get_registry().set_runner(runner)

    with pytest.raises(SubagentNotAllowed):
        await spawn_subsession(parent_id, "rce_payload", scope_key="evil")

    assert invoked == [], (
        "boundary FAILED: subagent runner reached despite policy deny"
    )

    denials = supervisor.recent(decision="denied")
    matched = [
        e for e in denials
        if e["kind"] == "subagent_spawn"
        and e["detail"].get("child_kind") == "rce_payload"
    ]
    assert matched, "supervisor must record a denied subagent_spawn event"


@pytest.mark.asyncio
async def test_paused_supervisor_blocks_handle_command_even_with_forged_context(
    supervisor,
):
    # v2026.5.39 Lane 08 WS6: a paused supervisor now BLOCKS via structured
    # refusal (returns None + records denial + emits refusal frame) instead
    # of raising SupervisorBlocked into the WS handler — the user-facing
    # error stopped looking like a 500. The security property is unchanged:
    # the wrapped command still does NOT execute. We assert that contract.
    inner_ran = []

    class FakeOrch:
        async def handle_command(self, session_id, text, context=None):
            inner_ran.append(True)
            return {"ran": True}

    orch = FakeOrch()
    supervisor.wrap(orch)
    supervisor.set_paused(True)

    forged_ctx = {
        "x_admin_bypass": True,
        "operator_admin": True,
        "supervisor_skip": "yes",
    }

    result = await orch.handle_command("s1", "do dangerous thing", forged_ctx)

    # Security property: inner orchestrator was NOT invoked.
    assert inner_ran == [], "paused supervisor must NOT execute the inner command"
    # Delivery contract: structured refusal (returns None; refusal frame emitted).
    assert result is None, f"expected None on paused refusal, got {result!r}"

    denials = supervisor.recent(decision="denied")
    assert any(
        e["detail"].get("reason") == "supervisor_paused" for e in denials
    ), "supervisor must record decision=denied reason=supervisor_paused"


@pytest.mark.asyncio
async def test_allowed_child_kind_baseline(supervisor):
    """Sanity: the harness allows the legitimate spawn path."""
    register_supervisor(supervisor)
    parent_id = "parent-session-B"
    register_parent_kind(parent_id, "orchestrator")

    invoked: list[dict] = []

    async def runner(**kwargs):
        invoked.append(kwargs)

    get_registry().set_runner(runner)

    child_id = await spawn_subsession(parent_id, "tool_runner", scope_key="legit")
    assert child_id, "harness regression: legitimate spawn must return a child id"
