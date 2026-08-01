"""Contract for post-write diagnostics (skills/diagnostics.py)."""

from __future__ import annotations

import shutil

import pytest

from skills.diagnostics import diagnose


# ── absent means absent, not clean ────────────────────────────────────


async def test_unknown_extension_returns_none(tmp_path):
    """The caller omits the diagnostics block entirely on None. An empty
    findings list would read to the model as "checked, and clean"."""
    f = tmp_path / "a.rb"
    f.write_text("puts 1\n")
    assert await diagnose(f, before_text="") is None


async def test_typescript_is_skipped(tmp_path):
    """tsc on one file without the project tsconfig emits a flood of
    phantom errors; with it, it type-checks the world and blows the
    timeout. Half-checking TypeScript is worse than not checking it."""
    for name in ("a.ts", "a.tsx"):
        f = tmp_path / name
        f.write_text("let x: = ;\n")
        assert await diagnose(f, before_text="") is None


async def test_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_POST_EDIT_DIAGNOSTICS", "off")
    f = tmp_path / "a.json"
    f.write_text("{not json}")
    assert await diagnose(f, before_text="{}") is None


async def test_missing_file_returns_none(tmp_path):
    assert await diagnose(tmp_path / "nope.py", before_text="") is None


# ── it actually catches things ────────────────────────────────────────


async def test_python_syntax_error_is_reported(tmp_path):
    f = tmp_path / "a.py"
    before = "x = 1\n"
    f.write_text("x = 1\ndef broken(:\n")
    result = await diagnose(f, before_text=before)
    assert result is not None
    assert result["new_count"] >= 1
    assert result["findings"]


async def test_python_ast_fallback_when_ruff_is_absent(tmp_path, monkeypatch):
    """ruff is not a declared dependency of feral-core, so absent is the
    common case; ast.parse still catches the failure that matters."""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    f = tmp_path / "a.py"
    f.write_text("def broken(:\n")
    result = await diagnose(f, before_text="x = 1\n")
    assert result is not None
    assert result["checker"] == "python-ast"
    assert result["findings"][0]["severity"] == "error"


async def test_json_error_is_reported(tmp_path):
    f = tmp_path / "a.json"
    f.write_text("{bad}")
    result = await diagnose(f, before_text="{}")
    assert result is not None
    assert result["checker"] == "json"
    assert result["new_count"] == 1


async def test_bash_syntax_error_is_reported(tmp_path):
    if not shutil.which("bash"):
        pytest.skip("bash not installed")
    f = tmp_path / "a.sh"
    f.write_text("if true; then\n")
    result = await diagnose(f, before_text="echo hi\n")
    assert result is not None
    assert result["new_count"] == 1


# ── baseline diffing is the feature ───────────────────────────────────


async def test_pre_existing_findings_are_not_reported(tmp_path, monkeypatch):
    """Editing one line of a legacy file must not dump its existing
    warnings into the context; the model has no way to know they are not
    its fault and starts 'fixing' them."""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    f = tmp_path / "a.py"
    before = "def broken(:\n"
    f.write_text("def broken(:\n# a harmless new comment\n")
    result = await diagnose(f, before_text=before)
    assert result is not None
    assert result["new_count"] == 0
    assert result["findings"] == []


async def test_a_clean_edit_of_a_dirty_file_reports_nothing_new(tmp_path):
    f = tmp_path / "a.json"
    before = "{oops"
    f.write_text("{oops ")
    result = await diagnose(f, before_text=before)
    assert result is not None
    assert result["new_count"] == 0


async def test_missing_baseline_is_labelled(tmp_path):
    f = tmp_path / "a.json"
    f.write_text("{bad}")
    result = await diagnose(f, before_text=None)
    assert result is not None
    assert result["baseline"] == "unavailable"


# ── bounded output ────────────────────────────────────────────────────


async def test_findings_are_capped_and_errors_sort_first(tmp_path):
    if not shutil.which("ruff"):
        pytest.skip("ruff not installed")
    f = tmp_path / "a.py"
    body = "".join(f"import mod_{i}\n" for i in range(40))
    f.write_text(body + "def broken(:\n")
    result = await diagnose(f, before_text="")
    assert result is not None
    assert len(result["findings"]) <= 10
    if result["new_count"] > len(result["findings"]):
        assert result["truncated"] is True
    severities = [x["severity"] for x in result["findings"]]
    assert severities == sorted(severities, key=lambda s: 0 if s == "error" else 1)


async def test_messages_are_length_capped(tmp_path):
    f = tmp_path / "a.json"
    f.write_text("{" + "x" * 5000)
    result = await diagnose(f, before_text="{}")
    assert result is not None
    for finding in result["findings"]:
        assert len(finding["message"]) <= 200


# ── never fatal ───────────────────────────────────────────────────────


async def test_a_raising_checker_yields_none_not_an_exception(tmp_path, monkeypatch):
    import skills.diagnostics as mod

    def _boom(*a, **kw):
        raise RuntimeError("checker exploded")

    monkeypatch.setattr(mod, "_pick_checker", _boom)
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    assert await diagnose(f, before_text="") is None


async def test_baseline_snapshot_is_cleaned_up(tmp_path):
    if not shutil.which("ruff"):
        pytest.skip("ruff not installed")
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    await diagnose(f, before_text="y = 2\n")
    leftovers = list(tmp_path.glob(".feral-baseline-*"))
    assert leftovers == []
