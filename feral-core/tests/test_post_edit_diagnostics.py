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
    assert result["findings"][0]["line"] == 2


async def test_interpreter_messages_are_stable_across_runs(tmp_path):
    """node stamps its process id into stderr (``(node:97599)``). Left in
    the message, the baseline key never matches the after key and an
    untouched pre-existing error is reported as new on every edit."""
    if not shutil.which("node"):
        pytest.skip("node not installed")
    from skills.diagnostics import _run_node_check

    broken = "function f( {\n"
    first = await _run_node_check(broken, tmp_path / "m.js")
    second = await _run_node_check(broken, tmp_path / "m.js")
    assert first == second
    assert "node:" not in first[0]["message"]
    assert first[0]["message"].startswith("SyntaxError:")


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


# ── no temporary files, ever ──────────────────────────────────────────


@pytest.mark.parametrize("name,content,before", [
    ("a.py", "x = 1\n", "y = 2\n"),
    ("a.js", "const a = 1;\n", "const b = 2;\n"),
    ("a.sh", "echo hi\n", "echo bye\n"),
    ("a.json", "{}", '{"a": 1}'),
])
async def test_no_files_are_created_anywhere(tmp_path, name, content, before):
    """A stray baseline file survives SIGKILL (a `finally` does not),
    shows up in `git status`, and trips file watchers. This runs on every
    single edit, so the only safe number of temp files is zero."""
    f = tmp_path / name
    f.write_text(content)
    before_listing = sorted(p.name for p in tmp_path.iterdir())

    await diagnose(f, before_text=before)

    assert sorted(p.name for p in tmp_path.iterdir()) == before_listing
    assert list(tmp_path.glob(".feral-baseline-*")) == []
    # The file-based version also dropped a `.ruff_cache/` into whatever
    # directory it ran in, which is the user's source tree.
    assert not (tmp_path / ".ruff_cache").exists()


async def test_diagnostics_work_on_a_read_only_directory(tmp_path):
    """Corollary of writing nothing: a directory the agent cannot write
    to is no longer a reason for diagnostics to silently disappear."""
    if not shutil.which("bash"):
        pytest.skip("bash not installed")
    d = tmp_path / "ro"
    d.mkdir()
    f = d / "a.sh"
    f.write_text("if true; then\n")
    d.chmod(0o555)
    try:
        result = await diagnose(f, before_text="echo hi\n")
        assert result is not None
        assert result["new_count"] == 1
    finally:
        d.chmod(0o755)


async def test_ruff_resolves_the_projects_config_not_ferals(tmp_path):
    """`--stdin-filename` is the whole reason the temp file could go: it
    makes ruff resolve config by walking up from the real path while the
    source arrives on stdin."""
    if not shutil.which("ruff"):
        pytest.skip("ruff not installed")
    project = tmp_path / "project"
    project.mkdir()
    (project / "ruff.toml").write_text('[lint]\nselect=["E501"]\n')
    f = project / "a.py"
    f.write_text("import os\n")

    # F401 is outside the project's select list, so it must not fire.
    scoped = await diagnose(f, before_text=None)
    assert scoped is not None
    assert [x["code"] for x in scoped["findings"]] == []

    # The same source outside that project does report F401, which proves
    # the suppression above came from the project's config.
    loose = tmp_path / "b.py"
    loose.write_text("import os\n")
    unscoped = await diagnose(loose, before_text=None)
    assert unscoped is not None
    assert "F401" in [x["code"] for x in unscoped["findings"]]


async def test_baseline_and_after_use_the_same_channel(tmp_path):
    """Both sides go through stdin, so the diff stays sound even where
    that channel sees less context than the real file would."""
    if not shutil.which("node"):
        pytest.skip("node not installed")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "package.json").write_text('{"type":"commonjs"}')
    f = pkg / "m.js"
    # ESM syntax in a CJS package: `node --check` on the real path would
    # flag it, stdin does not. What matters is that it is judged the same
    # way before and after, so an untouched pre-existing condition is
    # never reported as newly introduced.
    f.write_text('import fs from "fs";\nexport const a = 1;\n')
    result = await diagnose(f, before_text='import fs from "fs";\n')
    assert result is not None
    assert result["new_count"] == 0
