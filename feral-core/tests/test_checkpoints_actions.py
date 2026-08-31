"""Contract for undo beyond files: reversal by compensation.

A file write is undone by putting bytes back. A calendar event, a
reminder and a routine have no prior bytes, so they are undone by calling
their inverse. These tests pin the four properties that make it safe to
tell ``security/trust_ledger.py`` those creations are undoable:

1. **The compensation comes from the RESULT.** The created object's id
   does not exist until the call has succeeded, and a call that failed
   created nothing to take back.
2. **Both kinds revert in one turn.** A turn that writes a file and
   creates an event has to undo both, and the file half must keep its
   existing envelope and drift semantics exactly.
3. **An object the user already deleted is not a failure.** Revert has to
   be safe to run twice, and safe to run after the user got there first.
4. **A partial revert says so.** One failed compensation must not report
   as a completed undo, and must not abandon the restores that worked.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from skills.call_context import bind_context
from skills.checkpoints import (
    CHECKPOINTED_FILE_TOOLS,
    REVERSIBLE_ACTIONS,
    REVERT_INCOMPLETE,
    REVERT_REFUSED_DRIFT,
    CheckpointStore,
    extract_target_id,
)

CAL = "calendar_google__create_event"
REM = "feral_reminders__create"
ROU = "feral_routines__create"


@pytest.fixture
def store(tmp_path) -> CheckpointStore:
    return CheckpointStore(tmp_path / "checkpoints")


@pytest.fixture
def work(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    return d


class Calls:
    """A compensator double that records what it was asked to do."""

    def __init__(self, outcome=None, outcomes=None):
        self.seen: list[tuple[str, dict]] = []
        self._outcome = outcome if outcome is not None else {"success": True}
        self._outcomes = outcomes or {}

    def __call__(self, tool_name: str, args: dict) -> dict:
        self.seen.append((tool_name, dict(args)))
        return self._outcomes.get(tool_name, self._outcome)


def _write(store: CheckpointStore, path, content: str, *, turn_id: str):
    cp = store.capture(path, turn_id=turn_id, session_id="s1",
                       tool_name="coding_tools__write_file")
    path.write_text(content)
    store.record_after(cp, path)
    return cp


def _create_event(store: CheckpointStore, event_id: str, *, turn_id: str):
    return store.capture_action(
        tool_name=CAL,
        result={"success": True, "data": {"id": event_id, "summary": "Standup"}},
        turn_id=turn_id,
        session_id="s1",
    )


# ── 1. recorded from the result, never from the args ──────────────────


def test_a_successful_create_records_its_inverse(store):
    assert _create_event(store, "evt_1", turn_id="t1")

    rows = store.reversals_for_turn("t1")
    assert len(rows) == 1
    assert rows[0]["target_id"] == "evt_1"
    assert rows[0]["inverse_tool"] == "calendar_google__delete_event"
    assert rows[0]["inverse_arg"] == "event_id"
    assert rows[0]["tool_name"] == CAL


def test_a_failed_create_records_nothing(store):
    """There is nothing to compensate: the event was never created."""
    assert store.capture_action(
        tool_name=CAL,
        result={"success": False, "error": "Not connected to Google Calendar"},
        turn_id="t1",
    ) is None
    assert store.reversals_for_turn("t1") == []


def test_a_pending_approval_records_nothing(store):
    """A question is not an outcome. The call has not run yet."""
    assert store.capture_action(
        tool_name=CAL,
        result={"status": "pending_approval", "tool_name": CAL, "args": {}},
        turn_id="t1",
    ) is None
    assert store.reversals_for_turn("t1") == []


def test_a_success_with_no_id_records_nothing(store):
    """Fails towards no undo, which means no trust, never towards a
    reversal record that names nothing."""
    assert store.capture_action(
        tool_name=CAL,
        result={"success": True, "data": {"summary": "Standup"}},
        turn_id="t1",
    ) is None
    assert store.reversals_for_turn("t1") == []


def test_an_unregistered_tool_records_nothing(store):
    """The registry is the whole boundary. Anything not in it has no
    undo, whatever it returns."""
    assert store.capture_action(
        tool_name="email__send",
        result={"success": True, "data": {"id": "msg_1"}},
        turn_id="t1",
    ) is None
    assert store.reversals_for_turn("t1") == []


def test_the_id_is_taken_from_the_result_not_the_arguments(store):
    """The load-bearing one. An id in the arguments is the caller's
    guess; the id in the result is what the provider actually made."""
    store.capture_action(
        tool_name=CAL,
        result={"success": True, "data": {"id": "server_assigned"}},
        turn_id="t1",
    )
    assert store.reversals_for_turn("t1")[0]["target_id"] == "server_assigned"


def _endpoint(tool_name: str):
    """Find ``skill__endpoint`` on the shipped manifests, or None."""
    import json
    from pathlib import Path

    manifests = Path(__file__).resolve().parent.parent / "skills" / "manifests"
    skill_id, _, endpoint_id = tool_name.partition("__")
    for path in manifests.glob("*.json"):
        data = json.loads(path.read_text())
        if data.get("skill_id") != skill_id:
            continue
        for endpoint in data.get("endpoints", []):
            if endpoint.get("id") == endpoint_id:
                return endpoint
    return None


@pytest.mark.parametrize("tool", sorted(REVERSIBLE_ACTIONS))
def test_every_registered_action_has_a_real_inverse(tool):
    """Both halves must be endpoints that exist on a real manifest, and
    the inverse must take the id in the parameter the spec names. A
    registry entry that points at nothing is a promise of undo with
    nothing behind it, and it widens what auto-executes."""
    spec = REVERSIBLE_ACTIONS[tool]

    assert _endpoint(tool) is not None, f"{tool} is on no manifest"
    found = _endpoint(spec.inverse_tool)
    assert found is not None, f"{spec.inverse_tool} is on no manifest"
    names = {p["name"] for p in found.get("params", [])}
    assert spec.inverse_arg in names, (
        f"{spec.inverse_tool} takes {sorted(names)}, not {spec.inverse_arg!r}"
    )


def test_the_calendar_id_path_matches_what_the_integration_returns():
    """Pinned against the real parser, not a hand-written fixture."""
    from integrations.calendar import CalendarIntegration

    parsed = CalendarIntegration._parse_gcal_event({"id": "gcal_42", "summary": "x"})
    result = {"success": True, "data": parsed}

    assert extract_target_id(REVERSIBLE_ACTIONS[CAL], result) == "gcal_42"


async def test_the_reminder_id_path_matches_what_the_skill_returns(tmp_path, monkeypatch):
    """Drives the real create endpoint end to end."""
    monkeypatch.setenv("FERAL_HOME", str(tmp_path / "home"))
    from skills.impl.feral_reminders import FeralRemindersSkill

    result = await FeralRemindersSkill().execute(
        "create", {"title": "call mom", "due": "2026-09-01T17:00:00Z"}, {},
    )
    target = extract_target_id(REVERSIBLE_ACTIONS[REM], result)

    assert target and target == result["data"]["reminder"]["id"]


def test_the_routine_id_path_matches_what_the_skill_returns():
    """Pinned against the real row serialiser the create endpoint uses."""
    from skills.impl.feral_routines import _job_to_dict

    class _Job:
        id = 17
        job_type = "prompt"
        cron_expr = "daily 17:00"
        description = ""
        payload = {}
        session_id = "s1"
        created_at = 0.0
        last_run = None
        next_run = 0.0
        enabled = True
        run_count = 0

    result = {"success": True, "data": {"routine": _job_to_dict(_Job()), "verified": True}}

    assert extract_target_id(REVERSIBLE_ACTIONS[ROU], result) == "17"


# ── 2. one turn, both kinds ───────────────────────────────────────────


def test_a_turn_that_wrote_a_file_and_made_an_event_undoes_both(store, work):
    f = work / "a.txt"
    f.write_text("original\n")
    _write(store, f, "agent edit\n", turn_id="t1")
    _create_event(store, "evt_1", turn_id="t1")
    calls = Calls()

    result = store.revert_turn("t1", compensate=calls)

    assert result["success"] is True
    assert f.read_text() == "original\n"
    assert calls.seen == [("calendar_google__delete_event", {"event_id": "evt_1"})]
    assert result["reverted_count"] == 2


def test_the_files_key_never_holds_an_action(store, work):
    """Back-compat: everything that read `files` keeps reading files."""
    f = work / "a.txt"
    f.write_text("original\n")
    _write(store, f, "agent edit\n", turn_id="t1")
    _create_event(store, "evt_1", turn_id="t1")

    plan = store.plan_revert("t1")

    assert [e["path"] for e in plan["files"]] == [str(f.resolve())]
    assert {e["kind"] for e in plan["actions"]} == {"action"}
    assert len(plan["entries"]) == 2


def test_a_turn_with_only_an_action_is_listed_and_revertible(store):
    _create_event(store, "evt_1", turn_id="t-action-only")
    calls = Calls()

    turns = store.list_turns()
    assert [t["turn_id"] for t in turns] == ["t-action-only"]
    assert turns[0]["writes"] == 0
    assert turns[0]["actions"] == 1
    assert store.latest_turn() == "t-action-only"

    assert store.revert_turn("t-action-only", compensate=calls)["success"] is True
    assert calls.seen


def test_all_three_domains_compensate_with_their_own_parameter(store):
    store.capture_action(
        tool_name=CAL,
        result={"success": True, "data": {"id": "evt_1"}}, turn_id="t1",
    )
    store.capture_action(
        tool_name=REM,
        result={"success": True, "data": {"reminder": {"id": "rem_1"}}}, turn_id="t1",
    )
    store.capture_action(
        tool_name=ROU,
        result={"success": True, "data": {"routine": {"id": 7}}}, turn_id="t1",
    )
    calls = Calls()

    store.revert_turn("t1", compensate=calls)

    assert calls.seen == [
        ("calendar_google__delete_event", {"event_id": "evt_1"}),
        ("feral_reminders__delete", {"id": "rem_1"}),
        ("feral_routines__delete", {"routine_id": "7"}),
    ]


# ── 3. already gone is not a failure ──────────────────────────────────


@pytest.mark.parametrize("gone", [
    {"success": False, "status_code": 404, "error": "Reminder rem_1 not found"},
    {"success": False, "status_code": 410, "error": "gone"},
    # The exact shape integrations/_http_errors.http_error_detail produces.
    {"success": False, "error": "HTTP 404: {\"error\": {\"message\": \"Not Found\"}}"},
])
def test_an_object_the_user_already_deleted_does_not_fail_the_revert(store, gone):
    _create_event(store, "evt_1", turn_id="t1")

    result = store.revert_turn("t1", compensate=Calls(outcome=gone))

    assert result["success"] is True
    assert result["partial"] is False
    assert [e["status"] for e in result["actions"]] == ["already_reverted"]


def test_an_already_compensated_action_is_not_called_a_second_time(store):
    _create_event(store, "evt_1", turn_id="t1")
    first = Calls()
    store.revert_turn("t1", compensate=first)

    second = Calls()
    result = store.revert_turn("t1", compensate=second)

    assert second.seen == [], "revert called the inverse twice"
    assert result["success"] is True
    assert [e["status"] for e in result["actions"]] == ["already_reverted"]


@pytest.mark.parametrize("failure", [
    {"success": False, "status_code": 500, "error": "Internal Server Error"},
    {"success": False, "status_code": 401, "error": "token expired"},
    {"success": False, "error": "HTTP 503: upstream unavailable"},
    {"success": False, "error": "the calendar did not answer in 30s"},
    # A "not found" that is not a 404 must not be read as one: the phrase
    # appears in plenty of errors that mean the request never landed.
    {"success": False, "error": "host not found: www.googleapis.com"},
])
def test_an_unreachable_provider_is_a_failure_not_an_already_gone(store, failure):
    """The dangerous direction. Reading a dead provider as 'already
    deleted' would report a revert that never happened."""
    _create_event(store, "evt_1", turn_id="t1")

    result = store.revert_turn("t1", compensate=Calls(outcome=failure))

    assert result["success"] is False
    assert [e["status"] for e in result["actions"]] == ["failed"]
    # And it stays outstanding, so a later revert tries again.
    assert store.reversals_for_turn("t1")[0]["reverted_at"] is None


def test_a_compensator_that_raises_is_one_failed_entry_not_a_crash(store, work):
    f = work / "a.txt"
    f.write_text("original\n")
    _write(store, f, "agent edit\n", turn_id="t1")
    _create_event(store, "evt_1", turn_id="t1")

    def _explode(tool_name, args):
        raise RuntimeError("connection reset")

    result = store.revert_turn("t1", compensate=_explode)

    assert result["success"] is False
    assert f.read_text() == "original\n", "the file restore was abandoned"
    assert result["actions"][0]["status"] == "failed"
    assert "connection reset" in result["actions"][0]["detail"]


# ── 4. partial reverts report as partial ──────────────────────────────


def test_a_failed_compensation_reports_partial_and_still_restores_files(store, work):
    f = work / "a.txt"
    f.write_text("original\n")
    _write(store, f, "agent edit\n", turn_id="t1")
    _create_event(store, "evt_1", turn_id="t1")

    result = store.revert_turn("t1", compensate=Calls(outcome={
        "success": False, "status_code": 500, "error": "boom",
    }))

    assert result["success"] is False
    assert result["partial"] is True, "half a revert reported as none"
    assert result["error_code"] == REVERT_INCOMPLETE
    assert result["refused"] is False
    assert f.read_text() == "original\n"
    assert result["reverted"] == [str(f.resolve())]
    assert result["reverted_actions"] == []
    assert "1 action(s)" in result["error"]


def test_nothing_reverted_is_not_reported_as_partial(store):
    _create_event(store, "evt_1", turn_id="t1")

    result = store.revert_turn("t1", compensate=Calls(outcome={
        "success": False, "status_code": 500, "error": "boom",
    }))

    assert result["success"] is False
    assert result["partial"] is False
    assert result["error_code"] == REVERT_INCOMPLETE


def test_without_a_compensator_actions_are_unrecoverable_never_dropped(store):
    """`feral checkpoints revert` runs with no brain behind it. It has to
    say what it could not undo instead of quietly leaving it out."""
    _create_event(store, "evt_1", turn_id="t1")

    result = store.revert_turn("t1")

    assert result["success"] is False
    assert [e["status"] for e in result["actions"]] == ["unrecoverable"]
    assert "calendar_google__delete_event" in result["actions"][0]["detail"]
    assert store.reversals_for_turn("t1")[0]["reverted_at"] is None


def test_a_dry_run_makes_no_compensating_call(store):
    """A preview must not delete anybody's calendar event."""
    _create_event(store, "evt_1", turn_id="t1")
    calls = Calls()

    result = store.revert_turn("t1", dry_run=True, compensate=calls)

    assert calls.seen == []
    assert result["dry_run"] is True
    assert [e["status"] for e in result["actions"]] == ["reversible"]
    assert store.reversals_for_turn("t1")[0]["reverted_at"] is None


def test_a_drift_refusal_makes_no_compensating_call(store, work):
    """The refusal is whole-turn. Compensating while refusing to restore
    would leave the turn in a state nobody ever saw."""
    f = work / "a.txt"
    f.write_text("original\n")
    _write(store, f, "agent edit\n", turn_id="t1")
    _create_event(store, "evt_1", turn_id="t1")
    f.write_text("the user edited this afterwards\n")
    calls = Calls()

    result = store.revert_turn("t1", compensate=calls)

    assert result["refused"] is True
    assert result["error_code"] == REVERT_REFUSED_DRIFT
    assert calls.seen == []
    assert f.read_text() == "the user edited this afterwards\n"


def test_forcing_past_drift_does_compensate(store, work):
    f = work / "a.txt"
    f.write_text("original\n")
    _write(store, f, "agent edit\n", turn_id="t1")
    _create_event(store, "evt_1", turn_id="t1")
    f.write_text("user work\n")
    calls = Calls()

    result = store.revert_turn("t1", force=True, compensate=calls)

    assert result["success"] is True
    assert calls.seen
    assert f.read_text() == "original\n"


def test_the_note_names_what_has_no_undo(store):
    _create_event(store, "evt_1", turn_id="t1")

    note = store.revert_turn("t1", dry_run=True)["note"]

    assert "bash" in note
    for named in ("calendar_google__create_event", "feral_reminders__create",
                  "feral_routines__create"):
        assert named in note
    assert "email" in note.lower()


# ── retention and the raw store ───────────────────────────────────────


def test_reversals_expire_on_the_same_clock_as_file_checkpoints(store, monkeypatch):
    _create_event(store, "evt_1", turn_id="t1")
    # Shortened after the row exists, so the row is inside the window when
    # capture's own opportunistic prune runs and outside it here.
    monkeypatch.setenv("FERAL_CHECKPOINT_RETENTION_DAYS", "0")

    assert store.prune() >= 1
    assert store.reversals_for_turn("t1") == []


def test_the_cli_prints_what_it_could_not_undo(wired, capsys, tmp_path):
    """The `feral checkpoints` half of the same honesty rule.

    It runs without a brain by design, so it cannot delete a calendar
    event. It has to say that out loud and exit non-zero, not print
    "Reverted 1 file(s)" and leave the event standing.
    """
    import argparse

    from cli.main import cmd_checkpoints

    f = tmp_path / "a.txt"
    f.write_text("original\n")
    _write(wired, f, "agent edit\n", turn_id="cli-turn")
    _create_event(wired, "evt_cli", turn_id="cli-turn")

    rc = cmd_checkpoints(argparse.Namespace(
        action="revert", turn_id="cli-turn", force=False, cp_dry_run=False,
    ))
    out = capsys.readouterr().out

    assert rc == 1
    assert f.read_text() == "original\n", "the files should still come back"
    assert "unrecoverable" in out
    assert "calendar event evt_cli" in out
    assert "1 file(s) and 0 action(s)" in out


def test_the_cli_can_read_reversals_straight_out_of_sqlite(store):
    """Same property the file rows have: the recovery path must not need
    the brain's REST surface to find out what there is to undo."""
    _create_event(store, "evt_1", turn_id="t1")

    conn = sqlite3.connect(str(store.db_path))
    try:
        rows = conn.execute(
            "SELECT turn_id, target_id, inverse_tool FROM reversals"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [("t1", "evt_1", "calendar_google__delete_event")]


# ── the wiring: recorded at the executor, from the result ─────────────


@pytest.fixture
def wired(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_CHECKPOINT_DIR", str(tmp_path / "cp"))
    from skills.checkpoints import get_store

    return get_store()


def test_record_reversal_stores_from_the_bound_turn(wired):
    from skills.checkpoint_actions import record_reversal

    with bind_context(session_id="s1", turn_id="turn-X", tool_name=CAL):
        assert record_reversal(CAL, {"success": True, "data": {"id": "evt_9"}})

    assert wired.reversals_for_turn("turn-X")[0]["target_id"] == "evt_9"


def test_record_reversal_ignores_a_failed_call(wired):
    from skills.checkpoint_actions import record_reversal

    with bind_context(session_id="s1", turn_id="turn-X", tool_name=CAL):
        assert record_reversal(CAL, {"success": False, "error": "nope"}) is None

    assert wired.reversals_for_turn("turn-X") == []


def test_a_create_with_no_undo_record_loses_its_earned_trust(wired, tmp_path):
    """The guard that keeps the two halves honest.

    ``UNDOABLE_TOOLS`` promises the record exists. If the result shape
    drifts and no record can be written, the tool has to go back to
    asking rather than keep auto-approving on a promise it is no longer
    keeping.
    """
    import security.trust_ledger as tl
    from skills.checkpoint_actions import record_reversal

    ledger = tl.TrustLedger(promote_after=2, path=tmp_path / "trust.json")
    for _ in range(2):
        ledger.record(CAL, success=True)
    assert ledger.is_trusted(CAL) is True

    tl._ledger = ledger
    try:
        with bind_context(session_id="s1", turn_id="turn-X", tool_name=CAL):
            # Succeeded, but the result carries no id to undo.
            record_reversal(CAL, {"success": True, "data": {"summary": "x"}})
    finally:
        tl._ledger = None

    assert ledger.is_trusted(CAL) is False


def test_the_executor_records_the_reversal_at_its_chokepoint():
    """Structural. Five of the seven production dispatch paths never
    touch ToolRunner, so recording anywhere else would leave undo
    working on some lanes and not others while trust widened on all."""
    import ast
    import inspect
    import textwrap

    from skills.executor import SkillExecutor

    tree = ast.parse(textwrap.dedent(inspect.getsource(SkillExecutor.execute)))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_record_action_reversal" in called


async def test_the_compensator_calls_the_real_executor_and_bypasses_the_gate(wired):
    """Both halves of the compensator, end to end.

    Under hybrid every one of the three inverses resolves to CONFIRM, so
    without the exemption every action revert would come back
    pending_approval, from a call that never passed through ToolRunner
    and so has no way to be resumed. The exemption is what makes action
    undo work at all under the default autonomy mode.
    """
    import sys
    import types
    from unittest.mock import MagicMock

    from skills.checkpoint_actions import compensating_call, make_compensator

    seen = {}

    class _Executor:
        async def execute(self, tool_name, args, manifest, endpoint):
            # What _gate would see, read from inside the same task.
            seen["gate_sees"] = compensating_call()
            seen["call"] = (tool_name, dict(args))
            return {"success": True, "data": {"deleted": args.get("event_id")}}

    endpoint = MagicMock()
    endpoint.id = "delete_event"
    manifest = MagicMock()
    manifest.endpoints = [endpoint]
    registry = MagicMock()
    registry.skills = {"calendar_google": manifest}

    state_mod = types.ModuleType("api.state")
    state_mod.state = MagicMock()
    state_mod.state.skill_executor = _Executor()
    state_mod.state.skill_registry = registry
    previous = sys.modules.get("api.state")
    sys.modules["api.state"] = state_mod
    try:
        _create_event(wired, "evt_live", turn_id="turn-live")
        compensate = make_compensator()
        assert compensate is not None

        result = await asyncio.to_thread(
            wired.revert_turn, "turn-live", compensate=compensate,
        )
    finally:
        if previous is not None:
            sys.modules["api.state"] = previous
        else:
            sys.modules.pop("api.state", None)

    assert result["success"] is True
    assert seen["call"] == ("calendar_google__delete_event", {"event_id": "evt_live"})
    assert seen["gate_sees"] == ("calendar_google__delete_event", "evt_live")
    # And the exemption does not outlive the call it was made for.
    assert compensating_call() is None


def test_the_gate_exemption_only_matches_the_call_it_was_set_for():
    """It must not become an ambient 'skip the gate' flag."""
    from skills.checkpoint_actions import _COMPENSATING
    from skills.executor import SkillExecutor

    assert SkillExecutor._is_compensating_call("calendar_google__delete_event") is False

    token = _COMPENSATING.set(("calendar_google__delete_event", "evt_1"))
    try:
        assert SkillExecutor._is_compensating_call("calendar_google__delete_event") is True
        # Anything else running in the same task is still gated.
        assert SkillExecutor._is_compensating_call("coding_tools__bash") is False
        assert SkillExecutor._is_compensating_call("email__send") is False
    finally:
        _COMPENSATING.reset(token)

    assert SkillExecutor._is_compensating_call("calendar_google__delete_event") is False


def test_no_compensator_is_built_without_a_brain():
    """The CLI case. It must answer None rather than half a compensator."""
    import sys

    from skills.checkpoint_actions import make_compensator

    previous = sys.modules.pop("api.state", None)
    try:
        assert make_compensator(loop=asyncio.new_event_loop()) is None
    finally:
        if previous is not None:
            sys.modules["api.state"] = previous


def test_the_undoable_set_and_the_checkpoint_registry_are_the_same_set():
    """Duplicated here as well as in test_earned_autonomy.py on purpose:
    this is the invariant that bounds how much runs without asking, and
    it should fail loudly from either side."""
    from security.trust_ledger import UNDOABLE_TOOLS

    assert UNDOABLE_TOOLS == CHECKPOINTED_FILE_TOOLS | set(REVERSIBLE_ACTIONS)
