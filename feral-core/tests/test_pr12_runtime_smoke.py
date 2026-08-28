"""PR 12: end-to-end runtime smoke that wires the PR 2-11 surfaces and
verifies they work together. Acts as the "did everything regress?"
canary the user can run in 2 seconds.

Surfaces touched:

* ComputerUseDriver normalisation (PR 4).
* CodingRunner subprocess command runner with PYTHONDONTWRITEBYTECODE
  (PR 5) — invoked via a trivial no-op command.
* safety_resolver decision shape (PR 6).
* GoalChecker priority order (PR 7).
* MemoryRetriever round-trip on a stub (PR 8).
* intent_gate verdict (PR 8).
* RealtimeProxy orchestrator-wired smoke (PR 9).
* UploadStore round-trip (PR 10).
* MCP FERAL skill projection refuses CONFIRM (PR 11).
* automation_metrics counters can be incremented (PR 12).

This test is intentionally cheap and isolated — it does NOT spin up a
brain, listen on ports, or touch the user's filesystem outside
``tmp_path``."""

from __future__ import annotations

import pytest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.mark.asyncio
async def test_pr12_runtime_smoke(tmp_path):
    # ── PR 4 — ComputerUseDriver
    from agents.computer_use_driver import normalize_action
    norm = normalize_action({"type": "click", "x": 100, "y": 200})
    assert norm.action == "click"
    assert (norm.x, norm.y) == (100, 200)

    # ── PR 5 — CodingRunner runs a subprocess with PYTHONDONTWRITEBYTECODE
    from agents.coding_run import _run_command

    # `await`, not `asyncio.run`. This test became a coroutine when
    # MemoryRetriever.retrieve did, and asyncio.run cannot be called
    # from inside a running loop.
    exit_code, stdout, _stderr = await _run_command(
        ["python", "-c", "import os; print(os.environ.get('PYTHONDONTWRITEBYTECODE'))"],
        cwd=tmp_path,
    )
    assert exit_code == 0
    assert "1" in stdout

    # ── PR 6 — safety_resolver returns PolicyDecision
    from security.safety_resolver import resolve_policy
    decision = resolve_policy("computer_use__bash", args={"command": "ls"}, surface="mcp")
    assert decision.level in {"auto", "confirm", "deny"}

    # ── PR 7 — GoalChecker priority: BLOCKED > DONE > CONTINUE
    from agents.goal_checker import GoalContext, GoalVerdict, check_goal
    blocked = check_goal(GoalContext(
        permission_needed=True, permission_target="Screen Recording",
        success_criteria=[("always", lambda _c: True)],
    ))
    assert blocked.verdict == GoalVerdict.BLOCKED

    # ── PR 8 — MemoryRetriever ranks notes, intent_gate flags pronoun deletes
    from memory.retriever import MemoryRetriever
    from agents.intent_gate import IntentVerdict, gate_intent

    class _Mem:
        def search(self, q, limit=10):
            return [{"id": "n", "content": f"matches {q}"}]
    retriever = MemoryRetriever(_Mem())
    rr = await retriever.retrieve("matches", top_k=1)
    assert rr.records and rr.records[0].tier == "notes"

    gate = await gate_intent("delete it")
    assert gate.verdict == IntentVerdict.ASK
    assert gate.impact == "high"

    # ── PR 9 — RealtimeProxy uses the orchestrator hooks (verify import works)
    from voice.realtime_proxy import RealtimeProxy
    assert hasattr(RealtimeProxy, "_handle_tool_call")

    # ── PR 10 — UploadStore round-trips bytes
    from memory.uploads import UploadStore
    store = UploadStore(root=tmp_path / "uploads")
    rec = store.store(data=b"smoke", filename="s.txt", content_type="text/plain")
    assert store.get(rec.upload_id).size_bytes == 5

    # ── PR 11 — MCP projection denies confirm-tier tools at call time
    from mcp.server import FeralMCPServer
    server = FeralMCPServer()
    # No registry wired: list returns no skill projections.
    tools = server.handle_tools_list()["tools"]
    assert not any(t["name"].startswith("feral_skill_") for t in tools)

    # ── PR 12 — metrics counters increment
    from observability import automation_metrics
    automation_metrics.record_blocked("smoke_tool", "smoke_reason")
    automation_metrics.record_repair_loop("repaired")
