"""Latitude that widens with a track record, and stops at what undo covers.

FERAL already measured per-skill reliability (``agents/learner.py:247``)
and wired it to skill *routing* (``orchestrator.py:5666``). The approval
gate never saw it: ``tool_runner.py`` decided approval from three static
branches on the autonomy mode, with no notion of whether a tool had ever
behaved.

So the gate was binary and static, which is the shape every runtime in
this market ships, and which produces the documented failure that people
start approving without reading.

These tests pin the three boundaries that make widening latitude
defensible. Each one is a way this feature could quietly become unsafe:

1. **Undo bounds trust.** A tool is only ever promoted if
   ``skills/checkpoints.py`` can revert it. You cannot honestly stop
   asking about something you cannot take back.
2. **strict is never overridden.** An operator who chose strict asked to
   be consulted; a track record is not consent.
3. **One bad outcome revokes immediately.** Trust is slow to gain and
   instant to lose. A gate that keeps quiet through a failure has taught
   the operator it means nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from security.trust_ledger import (  # noqa: E402
    DEFAULT_PROMOTE_AFTER,
    UNDOABLE_TOOLS,
    TrustLedger,
)

WRITE = "coding_tools__write_file"
EDIT = "coding_tools__edit_file"
# Real, confirm-tier, and NOT revertible: bash can rewrite anything and
# checkpoints explicitly do not cover it (skills/checkpoints.py:72).
BASH = "coding_tools__bash"


@pytest.fixture
def ledger(tmp_path):
    return TrustLedger(promote_after=3, path=tmp_path / "trust.json")


def _earn(ledger, tool, n):
    for _ in range(n):
        ledger.record(tool, success=True)


# ----------------------------------------------------------------------
# 1. Undo bounds trust
# ----------------------------------------------------------------------

def test_the_undoable_set_matches_what_checkpoints_actually_covers():
    """The load-bearing invariant of the whole feature.

    ``UNDOABLE_TOOLS`` is a promise that every name in it can be
    reverted. If checkpoints stops covering one, or this set gains a name
    checkpoints never covered, the feature starts granting latitude over
    actions nobody can take back.
    """
    note = (ROOT / "skills" / "checkpoints.py").read_text()
    for tool in UNDOABLE_TOOLS:
        endpoint = tool.split("__", 1)[1]
        assert endpoint in note, (
            f"{tool} is treated as undoable but {endpoint} is not named in "
            "skills/checkpoints.py"
        )
    assert UNDOABLE_TOOLS == {WRITE, EDIT}, (
        "the undoable set changed; confirm checkpoints really covers the "
        "new members before widening trust to them"
    )


def test_an_unrevertible_tool_is_never_trusted(ledger):
    """bash is confirm-tier and runs constantly. It is exactly the tool
    an operator would most like to stop being asked about, and exactly
    the one that must keep asking."""
    _earn(ledger, BASH, 50)
    assert ledger.is_trusted(BASH) is False


def test_an_unrevertible_tool_does_not_even_accumulate(ledger):
    """Defence in depth: no streak means a later bug that widened the
    eligible set could not instantly promote a tool on stale credit."""
    _earn(ledger, BASH, 50)
    assert ledger.state(BASH)["clean_runs"] == 0


@pytest.mark.parametrize("tool", sorted(UNDOABLE_TOOLS))
def test_a_revertible_tool_earns_trust(tool, ledger):
    assert ledger.is_trusted(tool) is False
    _earn(ledger, tool, 3)
    assert ledger.is_trusted(tool) is True


def test_trust_is_not_granted_early(ledger):
    _earn(ledger, WRITE, 2)
    assert ledger.is_trusted(WRITE) is False


def test_trust_does_not_transfer_between_tools(ledger):
    _earn(ledger, WRITE, 10)
    assert ledger.is_trusted(EDIT) is False


# ----------------------------------------------------------------------
# 2. Instant revocation
# ----------------------------------------------------------------------

def test_one_failure_revokes_trust(ledger):
    _earn(ledger, WRITE, 3)
    assert ledger.is_trusted(WRITE) is True

    ledger.record(WRITE, success=False)

    assert ledger.is_trusted(WRITE) is False


def test_a_failure_resets_rather_than_decrements(ledger):
    """Slow to gain, instant to lose.

    Decrementing would let a tool that fails every other call hover just
    under the threshold and keep most of its credit.
    """
    _earn(ledger, WRITE, 2)
    ledger.record(WRITE, success=False)
    assert ledger.state(WRITE)["clean_runs"] == 0


def test_a_revert_revokes_trust(ledger):
    """Stronger than a failure: the tool reported success and the human
    disagreed."""
    _earn(ledger, WRITE, 3)
    ledger.revoke(WRITE, reason="turn reverted")
    assert ledger.is_trusted(WRITE) is False


def test_reverting_a_turn_revokes_every_tool(ledger):
    """A turn is reverted as a whole; the operator is not saying which
    of its writes was the wrong one."""
    _earn(ledger, WRITE, 3)
    _earn(ledger, EDIT, 3)
    ledger.revoke_all(reason="turn reverted")
    assert ledger.is_trusted(WRITE) is False
    assert ledger.is_trusted(EDIT) is False


def test_trust_can_be_earned_again_after_revocation(ledger):
    """Revocation is a reset, not a ban. An agent that can never recover
    would make the first failure permanent and the feature pointless."""
    _earn(ledger, WRITE, 3)
    ledger.record(WRITE, success=False)
    _earn(ledger, WRITE, 3)
    assert ledger.is_trusted(WRITE) is True


# ----------------------------------------------------------------------
# 3. The gate: autonomy modes
# ----------------------------------------------------------------------

class TestTheGate:
    """Drives the real ``enforce_safety``, not a reimplementation."""

    @staticmethod
    def _runner(tmp_path, mode, promote_after=3):
        from unittest.mock import AsyncMock, MagicMock

        from agents.orchestrator import Orchestrator
        from security.exec_approvals import ApprovalManager

        reg = MagicMock()
        reg.skills = {}
        reg.find_skills_for_query = MagicMock(return_value=[])
        reg.get_tools_for_skills = MagicMock(return_value=[])
        orch = Orchestrator(
            skill_registry=reg,
            send_to_client=AsyncMock(),
            daemons={},
            memory=None,
            vision_buffer=None,
            perception=None,
            learner=None,
            approval_manager=ApprovalManager(db_path=":memory:"),
        )
        runner = orch.tool_runner
        runner._autonomy_mode = mode
        runner._trust = TrustLedger(
            promote_after=promote_after, path=tmp_path / f"trust-{mode}.json"
        )
        return runner

    def test_hybrid_asks_before_trust_is_earned(self, tmp_path):
        runner = self._runner(tmp_path, "hybrid")
        pending = runner.enforce_safety(WRITE, {"path": "a.py"}, session_id="s")
        assert pending is not None
        assert pending["status"] == "pending_approval"

    def test_hybrid_stops_asking_once_trust_is_earned(self, tmp_path):
        """The whole point of the feature."""
        runner = self._runner(tmp_path, "hybrid")
        for _ in range(3):
            runner.record_trust_outcome(WRITE, success=True)

        assert runner.enforce_safety(WRITE, {"path": "a.py"}, session_id="s") is None

    def test_strict_keeps_asking_no_matter_the_record(self, tmp_path):
        """An operator who chose strict asked to be consulted.

        This is the boundary most likely to be 'simplified' away by
        someone who reads the feature as 'skip approval when reliable'.
        """
        runner = self._runner(tmp_path, "strict")
        for _ in range(50):
            runner.record_trust_outcome(WRITE, success=True)

        pending = runner.enforce_safety(WRITE, {"path": "a.py"}, session_id="s")
        assert pending is not None, "strict was overridden by a track record"

    def test_an_unrevertible_tool_keeps_asking_under_hybrid(self, tmp_path):
        runner = self._runner(tmp_path, "hybrid")
        for _ in range(50):
            runner.record_trust_outcome(BASH, success=True)

        assert runner.enforce_safety(BASH, {"command": "ls"}, session_id="s") is not None

    def test_a_failure_puts_the_prompt_back(self, tmp_path):
        runner = self._runner(tmp_path, "hybrid")
        for _ in range(3):
            runner.record_trust_outcome(WRITE, success=True)
        assert runner.enforce_safety(WRITE, {"path": "a.py"}, session_id="s") is None

        runner.record_trust_outcome(WRITE, success=False)

        assert runner.enforce_safety(WRITE, {"path": "a.py"}, session_id="s") is not None

    def test_revoking_puts_the_prompt_back(self, tmp_path):
        runner = self._runner(tmp_path, "hybrid")
        for _ in range(3):
            runner.record_trust_outcome(WRITE, success=True)
        runner.revoke_trust(reason="turn reverted")

        assert runner.enforce_safety(WRITE, {"path": "a.py"}, session_id="s") is not None


# ----------------------------------------------------------------------
# Durability and the escape hatch
# ----------------------------------------------------------------------

def test_trust_survives_a_restart(tmp_path):
    """Losing it on every restart would make the feature invisible on
    exactly the workloads it is for."""
    path = tmp_path / "trust.json"
    first = TrustLedger(promote_after=3, path=path)
    _earn(first, WRITE, 3)

    assert TrustLedger(promote_after=3, path=path).is_trusted(WRITE) is True


def test_a_tool_that_loses_undo_coverage_loses_its_saved_streak(tmp_path):
    """Persisted credit is filtered through the CURRENT undoable set.

    If a release stops checkpointing a tool, its streak on disk must not
    silently promote it under the new rules.
    """
    path = tmp_path / "trust.json"
    path.write_text('{"version": 1, "streaks": {"' + BASH + '": 99}}')

    assert TrustLedger(promote_after=3, path=path).is_trusted(BASH) is False


def test_a_corrupt_ledger_fails_towards_asking(tmp_path):
    """Every failure mode here must produce more prompts, never fewer."""
    path = tmp_path / "trust.json"
    path.write_text("{ not json")

    ledger = TrustLedger(promote_after=3, path=path)
    assert ledger.is_trusted(WRITE) is False


def test_the_feature_can_be_switched_off(tmp_path, monkeypatch):
    ledger = TrustLedger(promote_after=3, path=tmp_path / "t.json")
    _earn(ledger, WRITE, 3)
    assert ledger.is_trusted(WRITE) is True

    monkeypatch.setenv("FERAL_TRUST_DISABLED", "1")
    assert ledger.is_trusted(WRITE) is False


def test_the_threshold_is_configurable(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_TRUST_PROMOTE_AFTER", "2")
    ledger = TrustLedger(path=tmp_path / "t.json")
    _earn(ledger, WRITE, 2)
    assert ledger.is_trusted(WRITE) is True


def test_a_nonsense_threshold_falls_back_to_the_default(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_TRUST_PROMOTE_AFTER", "banana")
    assert TrustLedger(path=tmp_path / "t.json").promote_after == DEFAULT_PROMOTE_AFTER


# ----------------------------------------------------------------------
# Receipts
# ----------------------------------------------------------------------

def test_the_operator_can_see_where_every_tool_stands(ledger):
    """"It stopped asking" is only acceptable if you can find out why."""
    _earn(ledger, WRITE, 2)
    rows = {r["tool_name"]: r for r in ledger.snapshot()}

    assert rows[WRITE]["clean_runs"] == 2
    assert rows[WRITE]["promote_after"] == 3
    assert rows[WRITE]["trusted"] is False
    assert rows[WRITE]["undoable"] is True


# ----------------------------------------------------------------------
# The receipt endpoint
# ----------------------------------------------------------------------

class TestTrustEndpoint:
    """``GET /api/approvals/trust`` -- why it stopped asking."""

    @staticmethod
    def _client(monkeypatch, tmp_path, mode="hybrid"):
        from unittest.mock import AsyncMock, MagicMock

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from agents.orchestrator import Orchestrator
        from api.routes import approvals as mod
        from security.exec_approvals import ApprovalManager

        reg = MagicMock()
        reg.skills = {}
        reg.find_skills_for_query = MagicMock(return_value=[])
        reg.get_tools_for_skills = MagicMock(return_value=[])
        orch = Orchestrator(
            skill_registry=reg, send_to_client=AsyncMock(), daemons={},
            memory=None, vision_buffer=None, perception=None, learner=None,
            approval_manager=ApprovalManager(db_path=":memory:"),
        )
        orch.tool_runner._autonomy_mode = mode
        orch.tool_runner._trust = TrustLedger(
            promote_after=3, path=tmp_path / "trust.json"
        )
        monkeypatch.setattr(mod.state, "orchestrator", orch, raising=False)

        app = FastAPI()
        app.include_router(mod.router)
        return TestClient(app, raise_server_exceptions=False), orch

    def test_it_reports_where_each_tool_stands(self, monkeypatch, tmp_path):
        client, orch = self._client(monkeypatch, tmp_path)
        for _ in range(2):
            orch.tool_runner.record_trust_outcome(WRITE, success=True)

        body = client.get("/api/approvals/trust").json()

        row = next(r for r in body["tools"] if r["tool_name"] == WRITE)
        assert row["clean_runs"] == 2
        assert row["trusted"] is False
        assert row["promote_after"] == 3

    def test_it_only_lists_revertible_tools(self, monkeypatch, tmp_path):
        """The list is short on purpose: latitude never exceeds undo."""
        client, _ = self._client(monkeypatch, tmp_path)
        names = {r["tool_name"] for r in client.get("/api/approvals/trust").json()["tools"]}
        assert names == UNDOABLE_TOOLS
        assert BASH not in names

    def test_it_says_when_the_feature_is_not_in_play(self, monkeypatch, tmp_path):
        """Under strict or loose nothing is earned, and a reader must be
        able to tell that from the response rather than concluding the
        feature is broken."""
        client, _ = self._client(monkeypatch, tmp_path, mode="strict")
        body = client.get("/api/approvals/trust").json()
        assert body["active"] is False
        assert body["autonomy_mode"] == "strict"

    def test_it_is_active_under_hybrid(self, monkeypatch, tmp_path):
        client, _ = self._client(monkeypatch, tmp_path, mode="hybrid")
        assert client.get("/api/approvals/trust").json()["active"] is True
