"""B4 — a persisted tool-call argument blob must still be JSON.

``memory/store.py:log_execution`` wrote ``json.dumps(args)[:2000]``, and
``voice/realtime_proxy.py`` / ``voice/gemini_realtime.py`` wrote the same
blind slice for their episode ``detail``. Cutting a serialized document at
a byte offset does not "lose the tail": it truncates mid-token, so the
WHOLE record stops parsing. ``json.loads`` raises
``Unterminated string starting at ...`` and every reader gets nothing,
not a shortened version.

That matters because nothing else persists a tool call's arguments
(``memory/execution_audit.py`` says so in its module docstring) and
``memory/retriever.py:_collect_execution_log`` reads these rows straight
back into recall.

The repo already solved this class of defect once, in
``skills/result_budget.py``: shrink the *structure* until the serialized
form fits, so every intermediate is a real Python object and the output
always parses. These tests bind all three sites to that guarantee.
"""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from memory.store import MemoryStore  # noqa: E402


def _oversized_args() -> dict:
    """Args whose serialized form comfortably exceeds the 2000-char bound
    and whose overflow lands inside a string literal."""
    return {
        "path": "/etc/hosts",
        "prompt": "x" * 6000,
        "options": {"retries": 3, "verbose": True},
    }


@pytest.fixture
def store(tmp_path):
    return MemoryStore(db_path=str(tmp_path / "audit.db"))


async def test_log_execution_args_survive_json_loads(store):
    """The stored ``args`` column must round-trip through ``json.loads``."""
    await store.log_execution(
        session_id="s1",
        skill_id="coding_tools",
        endpoint_id="read_file",
        args=_oversized_args(),
        result_status="success",
        result_summary="ok",
    )
    rows = await store.log_recent("coding_tools", 5)
    assert rows, "log_execution wrote no row"
    raw = rows[0]["args"]
    assert isinstance(raw, str)
    decoded = json.loads(raw)  # must not raise
    assert isinstance(decoded, dict)


async def test_log_execution_args_keep_the_small_keys(store):
    """Shrinking must drop text, not the identifying scalars a human or
    the recall layer needs to know WHICH call this row describes."""
    await store.log_execution(
        session_id="s1",
        skill_id="coding_tools",
        endpoint_id="read_file",
        args=_oversized_args(),
        result_status="success",
    )
    rows = await store.log_recent("coding_tools", 5)
    decoded = json.loads(rows[0]["args"])
    assert decoded.get("path") == "/etc/hosts"
    assert decoded.get("options") == {"retries": 3, "verbose": True}
    assert decoded.get("_truncated") is True, (
        "a shrunk record must say so; a silent cut reads as the whole call"
    )


async def test_log_execution_small_args_are_untouched(store):
    """The common case must not gain an envelope it does not need."""
    await store.log_execution(
        session_id="s1",
        skill_id="weather",
        endpoint_id="current",
        args={"city": "Cairo"},
        result_status="success",
    )
    rows = await store.log_recent("weather", 5)
    assert json.loads(rows[0]["args"]) == {"city": "Cairo"}


async def test_log_execution_stays_within_the_column_budget(store):
    """The bound is still enforced — this is not "just stop truncating"."""
    await store.log_execution(
        session_id="s1",
        skill_id="coding_tools",
        endpoint_id="read_file",
        args={"blob": "y" * 200_000},
        result_status="success",
    )
    rows = await store.log_recent("coding_tools", 5)
    raw = rows[0]["args"]
    assert len(raw) <= 2_400, f"args column grew to {len(raw)} chars"
    json.loads(raw)


async def test_log_execution_survives_unserialisable_args(store):
    """``default=str`` behaviour must be preserved: an arbitrary object in
    the args must not turn the audit write into an exception."""
    class Weird:
        def __repr__(self):
            return "<weird>"

    await store.log_execution(
        session_id="s1",
        skill_id="x",
        endpoint_id="y",
        args={"obj": Weird()},
        result_status="success",
    )
    rows = await store.log_recent("x", 5)
    assert json.loads(rows[0]["args"]) == {"obj": "<weird>"}


# ── the two voice proxies build the same kind of blob for `detail` ──


def _voice_detail_payload() -> dict:
    return {
        "category": "action",
        "tool_name": "cutebot__drive",
        "skill_id": "cutebot",
        "endpoint": "drive",
        "params": {"direction": "forward", "note": "z" * 6000},
        "success": True,
        "verified": True,
        "observed": None,
        "expected": None,
        "source": "voice_realtime",
        "ts": time.time(),
    }


@pytest.mark.parametrize("module_name", [
    "voice.realtime_proxy",
    "voice.gemini_realtime",
])
def test_voice_detail_blob_survives_json_loads(module_name):
    """Both realtime proxies must serialize episode ``detail`` through the
    shared, structure-aware helper rather than a byte slice."""
    import importlib

    from skills.result_budget import serialize_for_storage

    mod = importlib.import_module(module_name)
    source = open(mod.__file__).read()
    assert "serialize_for_storage" in source, (
        f"{module_name} still builds `detail` with a raw slice"
    )

    detail = serialize_for_storage(_voice_detail_payload())
    decoded = json.loads(detail)
    assert decoded["skill_id"] == "cutebot"
    assert len(detail) <= 2_400
