"""Pins the reference-codebase grep/glob ergonomics adopted in coding_tools
(AUDIT-r14 round3 engine spec #4): grep defaults to file names, paginates
via head_limit/offset, and all paths are relativized to the workspace root;
glob lists via ripgrep with a truncation flag.
"""
import asyncio
import inspect
import os

import pytest

import skills.impl.coding_tools as ct


def _skill():
    cls = next(
        getattr(ct, n)
        for n in dir(ct)
        if inspect.isclass(getattr(ct, n))
        and hasattr(getattr(ct, n), "_grep_search")
        and getattr(ct, n).__module__ == ct.__name__
    )
    s = cls.__new__(cls)
    s._check_read = lambda p: None  # bypass sandbox for the unit under test
    return s


@pytest.fixture(autouse=True)
def _workspace(monkeypatch):
    # Relativize against feral-core so assertions are deterministic.
    root = os.path.dirname(os.path.dirname(os.path.abspath(ct.__file__)))
    monkeypatch.setenv("FERAL_WORKSPACE", root)
    return root


def test_grep_defaults_to_files_with_matches_and_relativizes():
    s = _skill()
    r = asyncio.run(s._grep_search({"pattern": "def _grep_search", "path": "skills/impl"}))
    assert r["success"] is True
    assert r["data"]["mode"] == "files_with_matches"
    files = r["data"]["files"]
    assert files, "expected at least one matching file"
    # Paths are relative to the workspace root, never absolute.
    assert all(not f.startswith("/") for f in files), files
    assert any(f.endswith("coding_tools.py") for f in files)


def test_grep_content_mode_paginates():
    s = _skill()
    r = asyncio.run(
        s._grep_search(
            {"pattern": "async def", "path": "skills/impl/coding_tools.py", "output_mode": "content", "head_limit": 3}
        )
    )
    assert r["data"]["mode"] == "content"
    assert len(r["data"]["matches"]) == 3
    assert r["data"].get("truncated") is True
    assert r["data"]["pagination"]["limit"] == 3
    assert r["data"]["pagination"]["next_offset"] == 3


def test_grep_offset_advances_window():
    s = _skill()
    first = asyncio.run(
        s._grep_search({"pattern": "async def", "path": "skills/impl/coding_tools.py", "output_mode": "content", "head_limit": 2, "offset": 0})
    )
    second = asyncio.run(
        s._grep_search({"pattern": "async def", "path": "skills/impl/coding_tools.py", "output_mode": "content", "head_limit": 2, "offset": 2})
    )
    # Different windows → different first match line numbers.
    assert first["data"]["matches"][0] != second["data"]["matches"][0]


def test_glob_relativizes_and_truncates():
    s = _skill()
    r = asyncio.run(s._glob_search({"pattern": "**/*.py", "path": "agents", "head_limit": 5}))
    assert r["success"] is True
    files = r["data"]["files"]
    assert len(files) == 5
    assert all(not f.startswith("/") for f in files), files
    assert r["data"]["total"] >= 5
    assert r["data"].get("truncated") is True
