"""The two operator-facing surfaces over checkpoints.

The CLI half matters most: it must read the SQLite directly rather than
call the REST route, because the moment you need an undo is the moment
the brain is wedged.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from skills.checkpoints import CheckpointStore  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path / "feral-home"))
    monkeypatch.delenv("FERAL_CHECKPOINT_DIR", raising=False)
    return tmp_path / "feral-home"


@pytest.fixture
def seeded(home, tmp_path):
    store = CheckpointStore(home / "checkpoints")
    target = tmp_path / "work.txt"
    target.write_text("original\n")
    cp = store.capture(target, turn_id="t1", session_id="s1",
                       tool_name="coding_tools__write_file")
    target.write_text("agent edit\n")
    store.record_after(cp, target)
    return store, target


# ── CLI ───────────────────────────────────────────────────────────────


def _args(**kw):
    return argparse.Namespace(**kw)


def test_cli_is_registered_and_classified():
    import cli.main as cli_main

    assert "checkpoints" in cli_main.PURE_LOCAL_SUBCOMMANDS


def test_cli_list(seeded, capsys):
    from cli.main import cmd_checkpoints

    rc = cmd_checkpoints(_args(action="list", session="", limit=20))
    out = capsys.readouterr().out
    assert rc == 0
    assert "t1" in out
    assert "bash" in out  # the not-covered note is always printed


def test_cli_show_reports_the_plan(seeded, capsys):
    from cli.main import cmd_checkpoints

    rc = cmd_checkpoints(_args(action="show", turn_id="t1"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "restorable" in out


def test_cli_revert_restores(seeded, capsys):
    from cli.main import cmd_checkpoints

    _store, target = seeded
    rc = cmd_checkpoints(
        _args(action="revert", turn_id="t1", force=False, cp_dry_run=False),
    )
    assert rc == 0
    assert target.read_text() == "original\n"
    assert "Reverted 1 file" in capsys.readouterr().out


def test_cli_revert_refuses_drift_without_force(seeded, capsys):
    from cli.main import cmd_checkpoints

    _store, target = seeded
    target.write_text("the user changed it\n")
    rc = cmd_checkpoints(
        _args(action="revert", turn_id="t1", force=False, cp_dry_run=False),
    )
    assert rc == 1
    assert target.read_text() == "the user changed it\n"
    assert "drifted" in capsys.readouterr().out

    rc = cmd_checkpoints(
        _args(action="revert", turn_id="t1", force=True, cp_dry_run=False),
    )
    assert rc == 0
    assert target.read_text() == "original\n"


def test_cli_dry_run_touches_nothing(seeded):
    from cli.main import cmd_checkpoints

    _store, target = seeded
    cmd_checkpoints(_args(action="revert", turn_id="t1", force=False, cp_dry_run=True))
    assert target.read_text() == "agent edit\n"


def test_cli_handles_an_empty_store(home, capsys):
    from cli.main import cmd_checkpoints

    rc = cmd_checkpoints(_args(action="list", session="", limit=20))
    assert rc == 0
    assert "No checkpoints" in capsys.readouterr().out


def test_cli_never_calls_the_rest_surface():
    """Structural: the recovery path must not depend on the thing you are
    recovering from."""
    import ast
    import inspect
    import textwrap

    from cli.main import cmd_checkpoints

    source = inspect.getsource(cmd_checkpoints)
    tree = ast.parse(textwrap.dedent(source))
    func = tree.body[0]
    called = {
        node.func.id
        for node in ast.walk(func)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_http_get" not in called
    assert "urlopen" not in called

    # No REST path in any string the function actually evaluates. The
    # function's own docstring explains why it does not use one, so a
    # plain substring check over the source would only test the prose.
    body = func.body[1:] if ast.get_docstring(func) else func.body
    literals = [
        node.value
        for stmt in body
        for node in ast.walk(stmt)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert not any("/api/" in text for text in literals)


# ── REST ──────────────────────────────────────────────────────────────


def test_rest_routes_are_registered():
    from api.server import app

    paths = {route.path for route in app.routes}
    assert "/api/checkpoints/turns" in paths
    assert "/api/checkpoints/revert" in paths


async def test_rest_list_and_revert(seeded):
    from api.routes.checkpoints import list_turns, revert, turn_detail

    _store, target = seeded

    listing = await list_turns()
    assert [t["turn_id"] for t in listing["turns"]] == ["t1"]
    assert "bash" in listing["note"]

    detail = await turn_detail("t1")
    assert detail["writes"]
    assert detail["plan"]["bash_not_covered"] is True

    result = await revert({"turn_id": "t1"})
    assert result["success"] is True
    assert target.read_text() == "original\n"
    assert result["bash_not_covered"] is True


async def test_rest_revert_refuses_drift(seeded):
    from api.routes.checkpoints import revert

    _store, target = seeded
    target.write_text("user work\n")

    result = await revert({"turn_id": "t1"})
    assert result["success"] is False
    assert target.read_text() == "user work\n"

    forced = await revert({"turn_id": "t1", "force": True})
    assert forced["success"] is True
    assert target.read_text() == "original\n"


async def test_rest_revert_refuses_the_whole_turn_not_just_the_drifted_file(
    home, tmp_path,
):
    """Drift in one file blocks the revert of its CLEAN siblings too.

    The single-file fixture above cannot see this, and the route's
    docstring used to describe the opposite ("listed and skipped"),
    which would mean the clean files still revert. A live run against a
    two-file turn showed nothing reverts. Pin the real contract: a turn
    reverts whole or not at all.
    """
    from api.routes.checkpoints import revert

    store = CheckpointStore(home / "checkpoints")
    clean = tmp_path / "clean.txt"
    dirty = tmp_path / "dirty.txt"
    for path, original in ((clean, "orig clean\n"), (dirty, "orig dirty\n")):
        path.write_text(original)
        cp = store.capture(path, turn_id="t2", session_id="s2",
                           tool_name="coding_tools__write_file")
        path.write_text("agent edit\n")
        store.record_after(cp, path)

    dirty.write_text("somebody else's work\n")

    result = await revert({"turn_id": "t2"})

    assert result["success"] is False
    assert result["reverted_count"] == 0
    # The clean file is untouched: not reverted, and not clobbered.
    assert clean.read_text() == "agent edit\n"
    assert dirty.read_text() == "somebody else's work\n"
    assert [e["path"] for e in result["drifted"]] == [str(dirty)]


async def test_rest_refused_revert_is_distinguishable_from_a_preview(
    home, tmp_path,
):
    """A refusal and a preview are now distinguishable on every field.

    They used to be byte-identical. ``revert_turn`` checked drift BEFORE
    ``dry_run``, so previewing a drifted turn returned the refusal
    envelope: same ``dry_run: true``, same ``success: false``, same
    ``error``. Not even ``success`` separated them, which the previous
    version of this test asserted without noticing what it proved.

    A dry run writes nothing, so there is nothing to refuse. The preview
    is now answered first and reports the drift as data.
    """
    from api.routes.checkpoints import revert

    store = CheckpointStore(home / "checkpoints")
    target = tmp_path / "drifted.txt"
    target.write_text("original\n")
    cp = store.capture(target, turn_id="t3", session_id="s3",
                       tool_name="coding_tools__write_file")
    target.write_text("agent edit\n")
    store.record_after(cp, target)
    target.write_text("user work\n")

    refused = await revert({"turn_id": "t3", "dry_run": False})
    preview = await revert({"turn_id": "t3", "dry_run": True})

    # The refusal: not a dry run, because the caller did not ask for one.
    assert refused["success"] is False
    assert refused["refused"] is True
    assert refused["dry_run"] is False
    assert refused["error_code"] == "revert_refused_drift"
    assert refused["error"]

    # The preview: succeeds, applies nothing, and reports the drift as data.
    assert preview["success"] is True
    assert preview["refused"] is False
    assert preview["dry_run"] is True
    assert preview["error_code"] == ""
    assert not preview.get("error")
    assert preview["reverted_count"] == 0

    # Both still surface the drifted path; that was never the problem.
    assert [e["path"] for e in refused["drifted"]] == [str(target)]
    assert [e["path"] for e in preview["drifted"]] == [str(target)]

    # And the preview did not touch the file.
    assert target.read_text() == "user work\n"

    # And drifted entries are NOT in `skipped`, despite reading that way.
    assert refused["skipped"] == []
    assert len(refused["drifted"]) == 1
    assert refused["drifted"][0]["action"] == "restore"


async def test_rest_unknown_turn_is_404(seeded):
    from fastapi import HTTPException

    from api.routes.checkpoints import turn_detail

    with pytest.raises(HTTPException) as exc:
        await turn_detail("no-such-turn")
    assert exc.value.status_code == 404
