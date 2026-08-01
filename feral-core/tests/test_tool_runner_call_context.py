"""ToolRunner must publish tool-call identity on the contextvar.

``SkillExecutor._execute_inner`` calls ``impl.execute(endpoint_id, args,
vault)`` with no session, and ``ToolRunner`` is the last layer that still
has one. These tests pin that it binds before dispatching, and that the
``turn_id`` it mints is stable across a turn and shared with subagents.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import skills.impl  # noqa: F401,E402 - register backing skills

from agents.tool_dispatch_validator import ToolDispatchValidator  # noqa: E402
from agents.tool_runner import ToolRunner  # noqa: E402
from models.skill_manifest import (  # noqa: E402
    BrandProfile,
    EndpointParam,
    SkillEndpoint,
    SkillManifest,
)
from skills.call_context import current_context  # noqa: E402


def _manifest() -> SkillManifest:
    return SkillManifest(
        skill_id="probe",
        brand=BrandProfile(name="Probe"),
        description="probe",
        endpoints=[
            SkillEndpoint(
                id="ping",
                method="PYTHON",
                url="",
                description="ping",
                params=[EndpointParam(name="value", type="string", required=False)],
                safety_tier="safe",
                read_only_hint=True,
            ),
        ],
    )


def _make_runner():
    """A runner whose executor records the context it was called under."""
    seen: list = []
    orch = MagicMock()
    manifest = _manifest()
    orch.skills = MagicMock()
    orch.skills.skills = {"probe": manifest}
    orch._mcp_client = None
    orch.daemons = {}
    orch._session_surfaces = {}
    orch._active_turns = {}

    async def _execute(**kwargs):
        seen.append(current_context())
        return {"success": True, "data": {}}

    orch.executor = MagicMock()
    orch.executor.execute = AsyncMock(side_effect=_execute)

    runner = ToolRunner(orch, autonomy_mode="loose")
    runner._dispatch_validator = ToolDispatchValidator(manifests={"probe": manifest})
    return runner, orch, seen


CALL = {"name": "probe__ping", "args": {"value": "x"}, "id": "tc-1"}


async def test_execute_tool_call_for_llm_binds_identity():
    runner, _orch, seen = _make_runner()
    await runner.execute_tool_call_for_llm("session-1", dict(CALL), [])
    assert len(seen) == 1
    ctx = seen[0]
    assert ctx.session_id == "session-1"
    assert ctx.tool_name == "probe__ping"
    assert ctx.call_id == "tc-1"
    assert ctx.surface == "websocket"
    assert ctx.turn_id


async def test_execute_tool_call_binds_identity():
    runner, _orch, seen = _make_runner()
    runner._orch.genui = MagicMock()
    runner._orch._send_text = AsyncMock()
    runner._orch.send = AsyncMock()
    await runner.execute_tool_call("session-2", dict(CALL), [])
    assert seen and seen[0].session_id == "session-2"


async def test_context_does_not_leak_after_the_call():
    runner, _orch, _seen = _make_runner()
    await runner.execute_tool_call_for_llm("session-1", dict(CALL), [])
    assert current_context().session_id == ""


async def test_surface_override_reaches_the_context():
    runner, _orch, seen = _make_runner()
    await runner.execute_tool_call_for_llm(
        "session-1", dict(CALL), [], surface="http_api",
    )
    assert seen[0].surface == "http_api"


# ── turn identity ─────────────────────────────────────────────────────


async def test_turn_id_is_stable_across_one_orchestrator_turn():
    """The orchestrator opens one record per user message in
    ``_begin_turn``; every tool call while answering that message must
    share a turn id, or a revert would undo one arbitrary file write
    instead of the turn."""
    runner, orch, seen = _make_runner()
    orch._active_turns["session-1"] = [{"text": "do the thing"}]

    await runner.execute_tool_call_for_llm("session-1", dict(CALL), [])
    await runner.execute_tool_call_for_llm("session-1", dict(CALL), [])
    assert seen[0].turn_id == seen[1].turn_id


async def test_a_new_user_message_gets_a_new_turn_id():
    runner, orch, seen = _make_runner()
    orch._active_turns["session-1"] = [{"text": "first"}]
    await runner.execute_tool_call_for_llm("session-1", dict(CALL), [])

    orch._active_turns["session-1"] = [{"text": "second"}]
    await runner.execute_tool_call_for_llm("session-1", dict(CALL), [])
    assert seen[0].turn_id != seen[1].turn_id


async def test_subagents_inherit_the_parent_turn_id():
    """`spawn_subagents` runs its workers under `<parent>:sub:<n>:<rand>`
    session ids. They must land in the parent's turn so "undo that"
    reverts the whole fan-out, not one worker's writes."""
    runner, orch, seen = _make_runner()
    orch._active_turns["session-1"] = [{"text": "fan out"}]

    await runner.execute_tool_call_for_llm("session-1", dict(CALL), [])
    await runner.execute_tool_call_for_llm("session-1:sub:1:abc123", dict(CALL), [])
    await runner.execute_tool_call_for_llm("session-1:sub:2:def456", dict(CALL), [])

    assert seen[0].turn_id == seen[1].turn_id == seen[2].turn_id
    assert seen[1].session_id == "session-1:sub:1:abc123"


async def test_turn_id_is_stamped_on_the_orchestrator_record():
    """Minted here rather than in orchestrator.py, which this lane must
    not touch. Adding a key to the record the orchestrator already keeps
    is additive: it only ever reads named fields from that dict."""
    runner, orch, _seen = _make_runner()
    turn = {"text": "hello"}
    orch._active_turns["session-1"] = [turn]
    await runner.execute_tool_call_for_llm("session-1", dict(CALL), [])
    assert turn["_feral_turn_id"]
    assert set(turn) == {"text", "_feral_turn_id"}


async def test_synthetic_turn_id_when_no_orchestrator_turn_exists():
    """Cron, taskflows and the REST tool surface have no turn record.
    They fall back to a per-session id that rotates after an idle gap,
    which is a heuristic and only used where the alternative is no
    grouping at all."""
    runner, orch, seen = _make_runner()
    orch._active_turns = {}
    await runner.execute_tool_call_for_llm("cron-session", dict(CALL), [])
    await runner.execute_tool_call_for_llm("cron-session", dict(CALL), [])
    assert seen[0].turn_id and seen[0].turn_id == seen[1].turn_id


async def test_synthetic_turn_id_rotates_after_the_idle_gap(monkeypatch):
    runner, orch, seen = _make_runner()
    orch._active_turns = {}
    monkeypatch.setenv("FERAL_TURN_IDLE_SECONDS", "0")
    await runner.execute_tool_call_for_llm("cron-session", dict(CALL), [])
    runner._synthetic_turns["cron-session"] = (
        runner._synthetic_turns["cron-session"][0], 0.0,
    )
    await runner.execute_tool_call_for_llm("cron-session", dict(CALL), [])
    assert seen[0].turn_id != seen[1].turn_id
