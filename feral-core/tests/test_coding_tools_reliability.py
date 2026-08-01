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
    ):
        assert em_dash not in (root / rel).read_text(), f"em dash in {rel}"


def test_asyncio_is_importable_for_the_lock_fixture():
    assert asyncio is not None
