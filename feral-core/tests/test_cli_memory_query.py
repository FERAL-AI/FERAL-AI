"""audit-r14 / lane-07 () — `feral memory query <text>` closes
THESIS_SCENARIOS S1 from the CLI side.

S1 ("What did I do yesterday?") fans out through the brain's
orchestrator → memory tool → fused timeline result. The CLI version
of the same thesis is to send a free-text query directly to the
brain's memory layer and render the structured result without the
GenUI step. This is a needs-brain command — `cli.main` classifies
it under ``NEEDS_BRAIN_SUBCOMMANDS``.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def fake_brain(monkeypatch):
    """Replace ``cli.memory_cmd._http_request`` with a deterministic
    stub so we can exercise the rendering + arg validation without
    booting the brain."""
    from cli import memory_cmd

    state: dict = {"calls": [], "next_response": []}

    def _fake(method, path):
        state["calls"].append((method, path))
        return state["next_response"]

    monkeypatch.setattr(memory_cmd, "_http_request", _fake)
    return state


# ----------------------------------------------------------------------
# query
# ----------------------------------------------------------------------


def test_query_sends_get_search_with_url_encoded_text(fake_brain, capsys):
    """``feral memory query "what did I do yesterday"`` MUST GET
    ``/internal/memory/search?query=...&limit=N``."""
    from cli.memory_cmd import cmd_memory

    fake_brain["next_response"] = [
        {
            "content": "Wrote the Wave 3 Lane 07 PR",
            "created_at": "2026-05-22T10:30:00Z",
            "tags": ["work", "feral"],
            "score": 0.91,
        },
        {
            "content": "Met with Mahmoud about the v2026.5.40 release",
            "created_at": "2026-05-22T13:00:00Z",
            "tags": [],
            "score": 0.78,
        },
    ]

    cmd_memory("query", "what did I do yesterday")

    out = capsys.readouterr().out
    assert "2 hit(s)" in out
    assert "Wrote the Wave 3 Lane 07 PR" in out
    assert "Met with Mahmoud" in out
    # The query is URL-encoded.
    assert any(
        "/internal/memory/search?query=" in path for _m, path in fake_brain["calls"]
    )


def test_query_no_text_exits_2(capsys):
    from cli.memory_cmd import cmd_memory

    with pytest.raises(SystemExit) as excinfo:
        cmd_memory("query", None)
    assert excinfo.value.code == 2
    out = capsys.readouterr().out
    assert "feral memory query" in out


def test_query_brain_offline_surfaces_error(monkeypatch, capsys):
    """When the brain is offline, ``_http_request`` returns
    ``{ok: False, error: ...}`` — query MUST exit 1 and print the
    error rather than spamming a traceback."""
    from cli import memory_cmd

    def _offline(_m, _p):
        return {"ok": False, "error": "Could not reach brain at ws://localhost:9090"}

    monkeypatch.setattr(memory_cmd, "_http_request", _offline)

    with pytest.raises(SystemExit) as excinfo:
        memory_cmd.cmd_memory("query", "anything")
    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "Memory query failed" in out
    assert "Could not reach brain" in out


def test_query_zero_hits_renders_friendly_empty(fake_brain, capsys):
    fake_brain["next_response"] = []
    from cli.memory_cmd import cmd_memory

    cmd_memory("query", "obscure term")
    out = capsys.readouterr().out
    assert "No memory hits" in out


def test_memory_subcommand_classified_needs_brain():
    from cli import main as cli_main

    assert "memory" in cli_main.NEEDS_BRAIN_SUBCOMMANDS
