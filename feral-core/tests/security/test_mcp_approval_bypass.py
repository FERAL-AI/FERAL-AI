""" — MCP tool approval-bypass: a high-risk MCP tool cannot run
without a per-tool consent record in ``security.exec_approvals``.

The MCP server (`feral-core/mcp/server.py`) does not carry the
approval check itself; that's the gateway-side contract documented
in :mod:`security.dangerous_tools` (`requires_approval`,
`is_tool_allowed`) and :mod:`security.exec_approvals`
(`ApprovalManager`). This test file simulates a remote MCP client
invoking a high-risk tool and demonstrates:

  1. CRITICAL-tier tool with NO consent → denied + audited, side-effect
     never runs.
  2. WARN-tier tool with NO consent → denied + audited.
  3. Per-session approval granted to session A does NOT leak into
     session B.

The boundary holds when the side-effect list stays empty for every
denied path AND the supervisor records a tool_invoke denial for each.
"""

from __future__ import annotations

import pytest

from agents.supervisor import Supervisor, SupervisorStore
from security.dangerous_tools import (
    DangerLevel,
    get_danger_level,
    is_tool_allowed,
    requires_approval,
)
from security.exec_approvals import ApprovalManager, ApprovalPolicy


HIGH_RISK_TOOL = "shell.exec"
WARN_TOOL = "image.generate"


@pytest.fixture
def supervisor(tmp_path):
    return Supervisor(store=SupervisorStore(str(tmp_path / "supervisor.db")))


@pytest.fixture
def mgr(tmp_path):
    return ApprovalManager(
        policy=ApprovalPolicy.ALLOWLIST,
        db_path=str(tmp_path / "approvals.db"),
    )


@pytest.fixture
def side_effects():
    """Mutable list the simulated tool body appends to on each call."""
    return []


def _mcp_invoke(tool: str, session_id: str, mgr, sup, side_effects, *, surface: str = "http_api"):
    """Mirror the gateway-side per-tool consent check.

    Returns the gate decision dict; only on ``ok=True`` does the
    simulated tool body actually run (and append to ``side_effects``).
    """
    if not is_tool_allowed(tool, surface):
        sup.record(
            source="mcp",
            kind="tool_invoke",
            session_id=session_id,
            actor="system",
            payload={"tool": tool, "surface": surface},
            decision="denied",
            detail={"reason": "surface_denied"},
        )
        return {"ok": False, "reason": "surface_denied"}

    if requires_approval(tool):
        approved, reason = mgr.check_approval(tool, session_id)
        if not approved:
            sup.record(
                source="mcp",
                kind="tool_invoke",
                session_id=session_id,
                actor="system",
                payload={"tool": tool, "surface": surface},
                decision="denied",
                detail={"reason": "no_consent_record", "approval_check": reason},
            )
            return {"ok": False, "reason": "no_consent_record"}

    side_effects.append((tool, session_id))
    sup.record(
        source="mcp",
        kind="tool_invoke",
        session_id=session_id,
        actor="system",
        payload={"tool": tool, "surface": surface},
        decision="allowed",
        detail={},
    )
    return {"ok": True}


def test_critical_tool_blocked_without_consent(mgr, supervisor, side_effects):
    assert get_danger_level(HIGH_RISK_TOOL) == DangerLevel.CRITICAL

    # On the http_api surface this tool is also outright denied via
    # the surface deny-list — that's the first line of defense.
    decision = _mcp_invoke(
        HIGH_RISK_TOOL, "session-1", mgr, supervisor, side_effects,
        surface="http_api",
    )

    assert decision["ok"] is False
    assert side_effects == [], (
        "boundary FAILED: critical tool ran without consent on http_api"
    )
    denials = supervisor.recent(decision="denied")
    assert any(e["kind"] == "tool_invoke" for e in denials)


def test_critical_tool_blocked_on_websocket_without_consent(
    mgr, supervisor, side_effects,
):
    """``shell.exec`` is not on the websocket surface deny-list, so it
    falls through to the per-tool consent check. Without a record,
    the gate must deny."""
    assert is_tool_allowed(HIGH_RISK_TOOL, "websocket")
    decision = _mcp_invoke(
        HIGH_RISK_TOOL, "session-1", mgr, supervisor, side_effects,
        surface="websocket",
    )
    assert decision["ok"] is False
    assert decision["reason"] == "no_consent_record"
    assert side_effects == []


def test_warn_level_tool_also_requires_consent(mgr, supervisor, side_effects):
    assert get_danger_level(WARN_TOOL) == DangerLevel.WARN

    decision = _mcp_invoke(
        WARN_TOOL, "s1", mgr, supervisor, side_effects, surface="websocket",
    )

    assert decision["ok"] is False
    assert side_effects == []
    denials = supervisor.recent(decision="denied")
    assert any(
        e["detail"].get("reason") == "no_consent_record" for e in denials
    )


def test_consent_record_does_not_leak_across_sessions(
    mgr, supervisor, side_effects,
):
    """Approval granted to session-A must NOT promote session-B."""
    mgr.grant_approval(WARN_TOOL, "session-A", scope="session")

    a = _mcp_invoke(
        WARN_TOOL, "session-A", mgr, supervisor, side_effects,
        surface="websocket",
    )
    b = _mcp_invoke(
        WARN_TOOL, "session-B", mgr, supervisor, side_effects,
        surface="websocket",
    )

    assert a["ok"] is True
    assert b["ok"] is False, (
        "boundary FAILED: per-session approval leaked to a sibling session"
    )
    assert side_effects == [(WARN_TOOL, "session-A")], (
        "boundary FAILED: only session-A's call should have side-effects"
    )
