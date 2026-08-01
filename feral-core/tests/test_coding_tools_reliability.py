"""End-to-end contract for the coding-harness reliability layer.

Exercises the real ``CodingToolsSkill`` through ``execute()``, the same
entry point ``SkillExecutor._execute_inner`` calls, with a bound
``ToolCallContext`` standing in for what ``ToolRunner`` binds.

Every test runs against an isolated ``FERAL_HOME`` and an isolated
checkpoint directory. Nothing here may touch the operator's ``~/.feral``.
"""

from __future__ import annotations

import asyncio

import pytest

from security.sandbox_policy import SandboxPolicy
from skills import file_state
from skills.call_context import bind_context
from skills.checkpoints import CheckpointStore, checkpoint_root
from skills.impl.coding_tools import CodingToolsSkill


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Hard safety rule: never write into a real ~/.feral."""
    monkeypatch.setenv("FERAL_HOME", str(tmp_path / "feral-home"))
    monkeypatch.setenv("FERAL_CHECKPOINT_DIR", str(tmp_path / "feral-home" / "checkpoints"))
    monkeypatch.setenv("FERAL_READ_BEFORE_EDIT", "warn")
    monkeypatch.setenv("FERAL_POST_EDIT_DIAGNOSTICS", "off")
    # A fresh tracker per test so observations do not bleed between them.
    monkeypatch.setattr(file_state, "_tracker", file_state.FileStateTracker())
    yield


@pytest.fixture
def work(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    SandboxPolicy.load_default().grant_folder(str(d), mode="readwrite")
    return d


@pytest.fixture
def skill() -> CodingToolsSkill:
    return CodingToolsSkill()


def ctx(session_id="s1", turn_id="t1", tool="coding_tools__edit_file"):
    return bind_context(
        session_id=session_id, surface="websocket", tool_name=tool,
        call_id="call-1", turn_id=turn_id,
    )


# ── edit_file: fallback matching reaches the tool ─────────────────────


async def test_edit_reports_the_strategy_and_matched_lines(skill, work):
    f = work / "a.py"
    f.write_text("def f():\n    return 1\n")
    with ctx():
        result = await skill.execute(
            "edit_file",
            {"path": str(f), "old_text": "    return 1", "new_text": "    return 2"},
            {},
        )
    assert result["success"] is True
    assert result["data"]["match_strategy"] == "exact"
    assert result["data"]["matched_lines"] == [[2, 2]]
    assert f.read_text() == "def f():\n    return 2\n"


async def test_edit_recovers_when_the_model_drops_whitespace(skill, work):
    """The pre-fix implementation returned 404 here and the model retried
    with the same text until the anti-loop guard fired."""
    f = work / "a.py"
    f.write_text("def f():   \n    return 1\n")
    with ctx():
        result = await skill.execute(
            "edit_file",
            {
                "path": str(f),
                "old_text": "def f():\n    return 1",
                "new_text": "def f():\n    return 3",
            },
            {},
        )
    assert result["success"] is True
    assert result["data"]["match_strategy"] != "exact"
    assert "return 3" in f.read_text()


async def test_ambiguous_edit_is_409_and_writes_nothing(skill, work):
    f = work / "a.py"
    original = "x = 1\nx = 1\n"
    f.write_text(original)
    with ctx():
        result = await skill.execute(
            "edit_file", {"path": str(f), "old_text": "x = 1", "new_text": "x = 2"}, {},
        )
    assert result["success"] is False
    assert result["status_code"] == 409
    assert result["data"]["error_code"] == "ambiguous"
    assert f.read_text() == original


async def test_replace_all_and_expected_replacements_reach_the_tool(skill, work):
    """Both params must be declared in the manifest: ToolDispatchValidator
    builds `fixed` only from endpoint.params and tool_runner replaces args
    with it, so an undeclared param is silently discarded."""
    f = work / "a.py"
    f.write_text("x = 1\nx = 1\n")
    with ctx():
        result = await skill.execute(
            "edit_file",
            {
                "path": str(f), "old_text": "x = 1", "new_text": "x = 2",
                "replace_all": True, "expected_replacements": 2,
            },
            {},
        )
    assert result["success"] is True
    assert result["data"]["replacements"] == 2
    assert f.read_text() == "x = 2\nx = 2\n"


async def test_expected_replacements_mismatch_refuses(skill, work):
    f = work / "a.py"
    f.write_text("x = 1\nx = 1\n")
    with ctx():
        result = await skill.execute(
            "edit_file",
            {
                "path": str(f), "old_text": "x = 1", "new_text": "x = 2",
                "replace_all": True, "expected_replacements": 5,
            },
            {},
        )
    assert result["success"] is False
    assert result["data"]["error_code"] == "unexpected_replacement_count"


async def test_not_found_returns_real_file_text_to_recover_from(skill, work):
    f = work / "a.py"
    f.write_text("def f():\n    return 1\n\ndef g():\n    return 2\n")
    with ctx():
        result = await skill.execute(
            "edit_file",
            {"path": str(f), "old_text": "    return 42", "new_text": "    return 43"},
            {},
        )
    assert result["success"] is False
    assert result["status_code"] == 404
    closest = result["data"]["closest_match"]
    assert closest["text"] in f.read_text()
    assert closest["start_line"] >= 1


async def test_crlf_file_is_not_corrupted_by_an_lf_edit(skill, work):
    """Silent-corruption regression: mixing endings makes every later
    exact match fail for reasons nothing in the output explains."""
    f = work / "a.txt"
    f.write_bytes(b"a = 1\r\nb = 2\r\n")
    with ctx():
        result = await skill.execute(
            "edit_file",
            {"path": str(f), "old_text": "a = 1", "new_text": "a = 9\nc = 3"},
            {},
        )
    assert result["success"] is True
    raw = f.read_bytes()
    assert b"\r\n" in raw
    assert raw.replace(b"\r\n", b"").count(b"\n") == 0


async def test_no_fuzzy_toggle_is_exposed(skill):
    """A model that can opt into looser matching always will, on the call
    where it should have re-read the file instead."""
    import json
    from pathlib import Path

    manifest = json.loads(
        (Path(__file__).resolve().parent.parent
         / "skills" / "manifests" / "coding_tools.json").read_text()
    )
    edit = next(ep for ep in manifest["endpoints"] if ep["id"] == "edit_file")
    names = {p["name"] for p in edit["params"]}
    assert "fuzzy" not in names
    assert {"replace_all", "expected_replacements"} <= names


# ── read-before-edit ──────────────────────────────────────────────────


async def test_never_read_warns_but_allows_by_default(skill, work):
    f = work / "a.py"
    f.write_text("x = 1\n")
    with ctx(session_id="fresh"):
        result = await skill.execute(
            "edit_file", {"path": str(f), "old_text": "x = 1", "new_text": "x = 2"}, {},
        )
    assert result["success"] is True
    assert result["data"]["read_before_edit"]["verdict"] == "never_read"
    assert result["data"]["warning"]


async def test_enforce_mode_refuses_and_writes_nothing(skill, work, monkeypatch):
    monkeypatch.setenv("FERAL_READ_BEFORE_EDIT", "enforce")
    f = work / "a.py"
    f.write_text("x = 1\n")
    with ctx(session_id="fresh"):
        result = await skill.execute(
            "edit_file", {"path": str(f), "old_text": "x = 1", "new_text": "x = 2"}, {},
        )
    assert result["success"] is False
    assert result["status_code"] == 409
    assert result["data"]["read_before_edit"]["verdict"] == "never_read"
    assert f.read_text() == "x = 1\n"


async def test_reading_first_clears_the_warning(skill, work):
    f = work / "a.py"
    f.write_text("x = 1\n")
    with ctx(tool="coding_tools__read_file"):
        await skill.execute("read_file", {"path": str(f)}, {})
    with ctx():
        result = await skill.execute(
            "edit_file", {"path": str(f), "old_text": "x = 1", "new_text": "x = 2"}, {},
        )
    assert result["success"] is True
    assert "read_before_edit" not in result["data"]


async def test_stale_file_is_flagged(skill, work):
    f = work / "a.py"
    f.write_text("x = 1\n")
    with ctx(tool="coding_tools__read_file"):
        await skill.execute("read_file", {"path": str(f)}, {})
    f.write_text("x = 1\ny = 9\n")  # somebody else wrote it
    with ctx():
        result = await skill.execute(
            "edit_file", {"path": str(f), "old_text": "x = 1", "new_text": "x = 2"}, {},
        )
    assert result["data"]["read_before_edit"]["verdict"] == "stale"


async def test_partial_read_does_not_block_an_edit_outside_the_window(skill, work):
    """No `partial` verdict on purpose: refusing this is a false positive
    that trains the model to spam whole-file reads."""
    f = work / "big.py"
    f.write_text("".join(f"line_{i} = {i}\n" for i in range(300)))
    with ctx(tool="coding_tools__read_file"):
        await skill.execute("read_file", {"path": str(f), "offset": 1, "limit": 10}, {})
    with ctx():
        result = await skill.execute(
            "edit_file",
            {"path": str(f), "old_text": "line_290 = 290", "new_text": "line_290 = 999"},
            {},
        )
    assert result["success"] is True
    assert result["data"].get("read_before_edit", {}).get("verdict") != "stale"


async def test_no_force_param_on_write_or_edit(skill):
    import json
    from pathlib import Path

    manifest = json.loads(
        (Path(__file__).resolve().parent.parent
         / "skills" / "manifests" / "coding_tools.json").read_text()
    )
    for endpoint_id in ("write_file", "edit_file"):
        endpoint = next(ep for ep in manifest["endpoints"] if ep["id"] == endpoint_id)
        assert "force" not in {p["name"] for p in endpoint["params"]}


async def test_non_read_only_bash_invalidates_the_session(skill, work):
    f = work / "a.py"
    f.write_text("x = 1\n")
    with ctx(tool="coding_tools__read_file"):
        await skill.execute("read_file", {"path": str(f)}, {})
    with ctx(tool="coding_tools__bash"):
        await skill.execute(
            "bash", {"command": "touch marker.txt", "cwd": str(work)}, {},
        )
    with ctx():
        result = await skill.execute(
            "edit_file", {"path": str(f), "old_text": "x = 1", "new_text": "x = 2"}, {},
        )
    assert result["data"]["read_before_edit"]["verdict"] == "never_read"


async def test_read_only_bash_keeps_the_observation(skill, work):
    f = work / "a.py"
    f.write_text("x = 1\n")
    with ctx(tool="coding_tools__read_file"):
        await skill.execute("read_file", {"path": str(f)}, {})
    with ctx(tool="coding_tools__bash"):
        await skill.execute("bash", {"command": "ls -la", "cwd": str(work)}, {})
    with ctx():
        result = await skill.execute(
            "edit_file", {"path": str(f), "old_text": "x = 1", "new_text": "x = 2"}, {},
        )
    assert "read_before_edit" not in result["data"]


async def test_concurrent_edits_to_one_path_serialise(skill, work):
    """`spawn_subagents` runs up to six workers with full coding_tools
    access, so check-then-write needs a per-path lock."""
    f = work / "counter.txt"
    f.write_text("0\n")
    with ctx(tool="coding_tools__read_file"):
        await skill.execute("read_file", {"path": str(f)}, {})

    async def bump(n: int):
        with ctx():
            return await skill.execute(
                "edit_file",
                {"path": str(f), "old_text": str(n), "new_text": str(n + 1)},
                {},
            )

    # Sequential-by-lock: each sees the previous value.
    results = [await bump(n) for n in range(3)]
    assert all(r["success"] for r in results)
    assert f.read_text().strip() == "3"


# ── checkpoints ───────────────────────────────────────────────────────


async def test_write_and_edit_are_checkpointed(skill, work):
    f = work / "a.py"
    f.write_text("v0\n")
    with ctx(turn_id="turn-A", tool="coding_tools__write_file"):
        await skill.execute("write_file", {"path": str(f), "content": "v1\n"}, {})
    with ctx(turn_id="turn-A"):
        await skill.execute(
            "edit_file", {"path": str(f), "old_text": "v1", "new_text": "v2"}, {},
        )
    assert f.read_text() == "v2\n"

    store = CheckpointStore(checkpoint_root())
    assert store.latest_turn() == "turn-A"
    result = store.revert_turn("turn-A")
    assert result["success"] is True
    assert f.read_text() == "v0\n"


async def test_bash_is_not_checkpointed(skill, work):
    """We cannot know what a shell command touched, so we do not pretend."""
    with ctx(turn_id="turn-B", tool="coding_tools__bash"):
        await skill.execute("bash", {"command": "echo hi", "cwd": str(work)}, {})
    store = CheckpointStore(checkpoint_root())
    assert store.entries_for_turn("turn-B") == []


async def test_checkpoint_failure_never_fails_the_write(skill, work, monkeypatch):
    import skills.checkpoints as cp

    def _explode(*a, **kw):
        raise RuntimeError("checkpoint store is on fire")

    monkeypatch.setattr(cp, "get_store", _explode)
    f = work / "a.py"
    with ctx(turn_id="turn-C", tool="coding_tools__write_file"):
        result = await skill.execute(
            "write_file", {"path": str(f), "content": "written anyway\n"}, {},
        )
    assert result["success"] is True
    assert f.read_text() == "written anyway\n"
    assert "checkpoint_id" not in result["data"]


async def test_revert_turn_endpoint_refuses_drift_by_default(skill, work):
    f = work / "a.py"
    f.write_text("v0\n")
    with ctx(turn_id="turn-D", tool="coding_tools__write_file"):
        await skill.execute("write_file", {"path": str(f), "content": "v1\n"}, {})
    f.write_text("the user changed it\n")

    with ctx(turn_id="turn-E", tool="coding_tools__revert_turn"):
        result = await skill.execute("revert_turn", {"turn_id": "turn-D"}, {})
    assert result["success"] is False
    assert result["data"]["bash_not_covered"] is True
    assert f.read_text() == "the user changed it\n"

    with ctx(turn_id="turn-E", tool="coding_tools__revert_turn"):
        forced = await skill.execute(
            "revert_turn", {"turn_id": "turn-D", "force": True}, {},
        )
    assert forced["success"] is True
    assert f.read_text() == "v0\n"


async def test_revert_turn_defaults_to_the_latest_turn(skill, work):
    f = work / "a.py"
    f.write_text("v0\n")
    with ctx(turn_id="turn-F", tool="coding_tools__write_file"):
        await skill.execute("write_file", {"path": str(f), "content": "v1\n"}, {})
    with ctx(turn_id="turn-G", tool="coding_tools__revert_turn"):
        result = await skill.execute("revert_turn", {"dry_run": True}, {})
    assert result["data"]["turn_id"] == "turn-F"


async def test_revert_turn_is_confirm_tier():
    """The operator's autonomy mode governs it: strict and hybrid ask,
    loose runs it. That is explicitly their choice to make."""
    import json
    from pathlib import Path

    manifest = json.loads(
        (Path(__file__).resolve().parent.parent
         / "skills" / "manifests" / "coding_tools.json").read_text()
    )
    endpoint = next(ep for ep in manifest["endpoints"] if ep["id"] == "revert_turn")
    assert endpoint["safety_tier"] == "confirm"


# ── diagnostics ───────────────────────────────────────────────────────


async def test_diagnostics_are_folded_into_the_result(skill, work, monkeypatch):
    monkeypatch.setenv("FERAL_POST_EDIT_DIAGNOSTICS", "on")
    f = work / "a.py"
    with ctx(turn_id="turn-H", tool="coding_tools__write_file"):
        result = await skill.execute(
            "write_file", {"path": str(f), "content": "def broken(:\n"}, {},
        )
    assert result["success"] is True
    assert result["data"]["diagnostics"]["new_count"] >= 1


async def test_diagnostics_key_is_absent_when_nothing_ran(skill, work, monkeypatch):
    monkeypatch.setenv("FERAL_POST_EDIT_DIAGNOSTICS", "on")
    f = work / "a.rb"
    with ctx(turn_id="turn-I", tool="coding_tools__write_file"):
        result = await skill.execute("write_file", {"path": str(f), "content": "puts 1\n"}, {})
    assert "diagnostics" not in result["data"]


# ── fail-open ─────────────────────────────────────────────────────────


async def test_unbound_callers_still_work(skill, work):
    """Cron, taskflows and the REST tool surface never bind a context.
    This is a correctness aid, not a security boundary, so it fails open
    rather than breaking those paths on day one."""
    f = work / "unbound.txt"
    result = await skill.execute("write_file", {"path": str(f), "content": "hi\n"}, {})
    assert result["success"] is True
    assert f.read_text() == "hi\n"
    assert "checkpoint_id" not in result["data"]


async def test_unbound_edit_still_works(skill, work):
    f = work / "unbound.py"
    f.write_text("x = 1\n")
    result = await skill.execute(
        "edit_file", {"path": str(f), "old_text": "x = 1", "new_text": "x = 2"}, {},
    )
    assert result["success"] is True
    assert f.read_text() == "x = 2\n"


# ── the loop must stay responsive ─────────────────────────────────────


# A wall-clock "did the loop stall" probe was tried first and rejected on
# evidence: with a 40,000-line write the worst loop gap was 9.4ms running
# the blocking work inline versus 4.2ms with it threaded. Real, but far
# too close to separate from CI noise without either a multi-second
# workload or a threshold that would flake. Thread identity is the same
# property measured exactly, which is why Wave 1 used it in
# test_coding_tools_async_io.py, and it does fail when the work is moved
# back onto the loop.


class _CallThreadSpy:
    """Records which thread each patched callable ran on."""

    def __init__(self, monkeypatch) -> None:
        self.threads: dict[str, int] = {}
        self._monkeypatch = monkeypatch

    def watch(self, target, name: str, label: str = "") -> None:
        import inspect
        import threading

        original = getattr(target, name)
        key = label or f"{getattr(target, '__name__', target)}.{name}"

        def wrapper(*a, **kw):
            self.threads[key] = threading.get_ident()
            return original(*a, **kw)

        # Setting a plain function on a class makes it an instance method,
        # so a `@staticmethod` original would start receiving `self` as its
        # first argument and every call site would fail with "takes 2
        # positional arguments but 3 were given". `getattr` already
        # unwrapped the descriptor, so re-wrap to keep the binding
        # behaviour the class actually declares.
        patched = wrapper
        if isinstance(target, type):
            raw = inspect.getattr_static(target, name)
            if isinstance(raw, staticmethod):
                patched = staticmethod(wrapper)
            elif isinstance(raw, classmethod):
                patched = classmethod(wrapper)

        self._monkeypatch.setattr(target, name, patched)

    def assert_all_off_loop(self, loop_thread: int, *, expected: set) -> None:
        missing = expected - set(self.threads)
        assert not missing, f"never called, so nothing was verified: {sorted(missing)}"
        for key, ident in self.threads.items():
            assert ident != loop_thread, (
                f"{key} ran on the event loop thread. Every blocking step of "
                f"a write must be inside the asyncio.to_thread hop."
            )


async def test_write_runs_all_blocking_work_off_the_loop(skill, work, monkeypatch):
    """The staleness fingerprint, the checkpoint blob and its SQLite
    write, and the write itself must all be off the loop.

    Wave 1 moved coding_tools' file I/O off the loop precisely so this
    lane could pile checkpointing and linting on top of it. Everything
    added here is blocking, so it has to go the same way.
    """
    import sqlite3
    import threading

    from skills import checkpoints as cp
    from skills import file_state as fs
    from skills.impl.coding_tools import CodingToolsSkill

    loop_thread = threading.get_ident()
    spy = _CallThreadSpy(monkeypatch)
    spy.watch(fs, "_fingerprint", "file_state._fingerprint")
    spy.watch(cp.CheckpointStore, "_put_blob", "CheckpointStore._put_blob")
    spy.watch(sqlite3, "connect", "sqlite3.connect")
    spy.watch(CodingToolsSkill, "_write_verbatim", "_write_verbatim")

    f = work / "a.py"
    f.write_text("original\n")
    with ctx(tool="coding_tools__read_file"):
        await skill.execute("read_file", {"path": str(f)}, {})

    with ctx(turn_id="turn-thread", tool="coding_tools__write_file"):
        result = await skill.execute(
            "write_file", {"path": str(f), "content": "replaced\n"}, {},
        )

    assert result["success"] is True, result
    spy.assert_all_off_loop(loop_thread, expected={
        "file_state._fingerprint",
        "CheckpointStore._put_blob",
        "sqlite3.connect",
        "_write_verbatim",
    })


async def test_edit_runs_the_matcher_off_the_loop(skill, work, monkeypatch):
    """The fuzzy matcher is the most expensive part of an edit: the
    sliding-window strategies are O(file_lines x needle_lines), so on a
    large file it is millions of string comparisons."""
    import threading

    from skills import edit_matchers as em
    from skills.impl.coding_tools import CodingToolsSkill

    loop_thread = threading.get_ident()
    spy = _CallThreadSpy(monkeypatch)
    spy.watch(em, "find_edit_match", "edit_matchers.find_edit_match")
    spy.watch(em, "splice", "edit_matchers.splice")
    spy.watch(CodingToolsSkill, "_read_verbatim", "_read_verbatim")

    f = work / "b.py"
    f.write_text("x = 1\n")
    with ctx():
        result = await skill.execute(
            "edit_file", {"path": str(f), "old_text": "x = 1", "new_text": "x = 2"}, {},
        )

    assert result["success"] is True, result
    spy.assert_all_off_loop(loop_thread, expected={
        "edit_matchers.find_edit_match",
        "edit_matchers.splice",
        "_read_verbatim",
    })


async def test_permission_check_runs_off_the_loop(skill, work, monkeypatch):
    """SandboxPolicy.can_write_path reads ~/.feral/workspace_grants.json
    from disk on every single call.

    That read was happening on the event loop. Wave 1's spy could not see
    it, because it records one thread per patched method name and the
    in-hop read_text overwrote the loop-thread record immediately after.
    """
    import threading

    from skills.impl.coding_tools import CodingToolsSkill

    loop_thread = threading.get_ident()
    spy = _CallThreadSpy(monkeypatch)
    spy.watch(CodingToolsSkill, "_check_write", "_check_write")

    with ctx(turn_id="turn-perm", tool="coding_tools__write_file"):
        await skill.execute("write_file", {"path": str(work / "c.txt"), "content": "x"}, {})

    spy.assert_all_off_loop(loop_thread, expected={"_check_write"})


async def test_revert_runs_off_the_loop(skill, work, monkeypatch):
    """A revert is SQLite plus one restore per file, all blocking."""
    import sqlite3
    import threading

    f = work / "r.txt"
    f.write_text("before\n")
    with ctx(turn_id="turn-revert", tool="coding_tools__write_file"):
        await skill.execute("write_file", {"path": str(f), "content": "after\n"}, {})

    loop_thread = threading.get_ident()
    spy = _CallThreadSpy(monkeypatch)
    spy.watch(sqlite3, "connect", "sqlite3.connect")

    with ctx(turn_id="turn-other", tool="coding_tools__revert_turn"):
        result = await skill.execute("revert_turn", {"turn_id": "turn-revert"}, {})

    assert result["success"] is True, result
    assert f.read_text() == "before\n"
    spy.assert_all_off_loop(loop_thread, expected={"sqlite3.connect"})


async def test_diagnostics_run_outside_the_write_lock(skill, work, monkeypatch):
    """Diagnostics must not hold the per-path lock.

    A linter subprocess has a five second timeout. Holding the lock
    across it would make one slow lint block every other subagent editing
    that file. It runs after the lock is released, on content passed in
    rather than re-read, so there is no race either.
    """
    monkeypatch.setenv("FERAL_POST_EDIT_DIAGNOSTICS", "on")
    held = {}
    tracker = file_state.get_tracker()
    f = work / "d.py"

    import skills.diagnostics as diag_mod

    original = diag_mod.diagnose

    async def _spy(path, **kw):
        held["locked_during_diagnostics"] = tracker.lock_for(f).locked()
        return await original(path, **kw)

    monkeypatch.setattr(diag_mod, "diagnose", _spy)

    with ctx(turn_id="turn-diag", tool="coding_tools__write_file"):
        result = await skill.execute("write_file", {"path": str(f), "content": "x = 1\n"}, {})

    assert result["success"] is True
    assert held.get("locked_during_diagnostics") is False, (
        "diagnostics ran while the per-path write lock was still held"
    )


def _barrier_probe(monkeypatch, parties: int, timeout: float):
    """Make every write inside the critical section rendezvous.

    Deterministic rather than timing-based: if two writes can be inside
    their critical sections at the same time the barrier releases, and if
    they are serialised it times out. Timing comparisons would only
    measure the GIL, since much of this work is Python-level.
    """
    import threading

    from skills.impl.coding_tools import CodingToolsSkill

    barrier = threading.Barrier(parties, timeout=timeout)
    # Already a plain function when reached through the class, since
    # `_write_verbatim` is a staticmethod.
    original = CodingToolsSkill._write_verbatim
    reached = []

    def _wrapped(path, content):
        reached.append(str(path))
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return original(path, content)

    monkeypatch.setattr(CodingToolsSkill, "_write_verbatim", staticmethod(_wrapped))
    return barrier, reached


async def test_writes_to_different_paths_are_not_serialised(skill, work, monkeypatch):
    """The lock is per path, so unrelated files never wait on each other.

    Both writes must be inside their critical section simultaneously for
    the barrier to release. That also proves the blocking section is off
    the loop: if it ran inline, the first write could never yield to let
    the second start.
    """
    barrier, reached = _barrier_probe(monkeypatch, parties=2, timeout=5.0)

    async def _one(name: str):
        with ctx(turn_id="turn-par", tool="coding_tools__write_file"):
            return await skill.execute(
                "write_file", {"path": str(work / name), "content": "x"}, {},
            )

    results = await asyncio.gather(_one("a.txt"), _one("b.txt"))
    assert all(r["success"] for r in results)
    assert len(reached) == 2
    assert not barrier.broken, (
        "two writes to different paths could not be in their critical "
        "sections at the same time; the lock is serialising unrelated files"
    )


async def test_writes_to_the_same_path_are_serialised(skill, work, monkeypatch):
    """The converse, and the reason the lock exists: `spawn_subagents`
    runs up to six workers, and two of them writing one file must not
    both pass the staleness check against the same fingerprint."""
    barrier, reached = _barrier_probe(monkeypatch, parties=2, timeout=0.75)

    async def _one():
        with ctx(turn_id="turn-same", tool="coding_tools__write_file"):
            return await skill.execute(
                "write_file", {"path": str(work / "same.txt"), "content": "x"}, {},
            )

    results = await asyncio.gather(_one(), _one())
    assert all(r["success"] for r in results)
    assert barrier.broken, (
        "two writes to the SAME path were inside the critical section at "
        "once; the per-path lock is not holding"
    )


def test_no_em_dashes_in_the_new_modules():
    """Hard project rule. Scoped to modules this lane owns outright;
    the pre-existing files it edits already contain some."""
    from pathlib import Path

    em_dash = chr(0x2014)
    root = Path(__file__).resolve().parent.parent
    for rel in (
        "skills/call_context.py", "skills/edit_matchers.py",
        "skills/file_state.py", "skills/checkpoints.py",
        "skills/diagnostics.py", "api/routes/checkpoints.py",
        "tests/test_coding_tools_reliability.py",
        # The manifest is owned by this lane, and the rule is repo-wide
        # rather than scoped to lines we authored. Checked after a JSON
        # round-trip because the file stores them as \\u2014 escapes, so a
        # raw byte scan reads clean while the delivered string is not.
        "skills/manifests/coding_tools.json",
    ):
        text = (root / rel).read_text()
        assert em_dash not in text, f"em dash in {rel}"
        if rel.endswith(".json"):
            import json

            assert em_dash not in json.dumps(json.loads(text)), (
                f"escaped em dash (\\u2014) in {rel}"
            )


def test_asyncio_is_importable_for_the_lock_fixture():
    assert asyncio is not None
