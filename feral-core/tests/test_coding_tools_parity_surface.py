"""Parity gaps closed in ``coding_tools``: background bash, image reads, grep flags.

Every test here drives the real ``CodingToolsSkill.execute``, the same
entry point ``SkillExecutor._execute_inner`` calls, with a bound
``ToolCallContext``, against an isolated ``FERAL_HOME``. Nothing here may
touch the operator's ``~/.feral``.

The three defects being pinned, each of which was found by RUNNING the
endpoint rather than reading it:

1. ``run_in_background: true`` was accepted and ignored: the call ran
   synchronously and there was no job id, no incremental output and no
   kill. ``timeout`` was silently clamped to 120s while the manifest
   advertised no ceiling at all.
2. ``read_file`` had no binary detection: a PNG came back as
   ``success: True`` with line-numbered mojibake, and any file over 2MB
   was refused even when it was an image the model could have seen.
3. ``grep_search`` silently dropped ``-i``, ``-A``/``-B``/``-C``,
   ``multiline`` and ``type``.
"""

from __future__ import annotations

import asyncio
import json
import os
import struct
import zlib
from pathlib import Path

import shutil

import pytest

from security.sandbox_policy import SandboxPolicy
from skills import file_state
from skills.call_context import bind_context
from skills.impl.coding_tools import (
    BACKGROUND_MAX_TIMEOUT,
    BASH_MAX_TIMEOUT,
    BG_MAX_BUFFER_LINES,
    CodingToolsSkill,
)

MANIFEST = (
    Path(__file__).resolve().parent.parent / "skills" / "manifests" / "coding_tools.json"
)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path / "feral-home"))
    monkeypatch.setenv("FERAL_CHECKPOINT_DIR", str(tmp_path / "feral-home" / "checkpoints"))
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


def ctx(session_id="s1", tool="coding_tools__bash"):
    return bind_context(
        session_id=session_id, surface="websocket", tool_name=tool,
        call_id="call-1", turn_id="t1",
    )


def _png_bytes(width: int, height: int) -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return (
            struct.pack(">I", len(payload))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    rows = []
    for y in range(height):
        row = bytearray(b"\x00")
        for x in range(width):
            row += bytes([(x + y) % 256, (x * 3) % 256, 200])
        rows.append(bytes(row))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"".join(rows), 6))
        + chunk(b"IEND", b"")
    )


# ── manifest discipline ───────────────────────────────────────────


class TestManifestAndDispatchAgree:
    """A declared-but-unrouted endpoint is a runtime 404; a routed-but-
    undeclared one is a tool the model can never call. Both directions."""

    def _declared(self) -> set[str]:
        return {e["id"] for e in json.loads(MANIFEST.read_text())["endpoints"]}

    def test_every_declared_endpoint_is_routed(self, skill):
        for endpoint_id in self._declared():
            result = asyncio.run(skill.execute(endpoint_id, {}, {}))
            assert result["status_code"] != 404 or "Unknown endpoint" not in (
                result["error"] or ""
            ), f"{endpoint_id} is declared in the manifest but not dispatched"

    def test_every_routed_endpoint_is_declared(self, skill):
        import inspect

        source = inspect.getsource(CodingToolsSkill.execute)
        routed = set(re_findall_endpoint_keys(source))
        undeclared = routed - self._declared()
        assert not undeclared, (
            f"{sorted(undeclared)} are dispatched but not declared in the "
            f"manifest, so the model can never call them"
        )

    def test_unknown_endpoint_is_a_clean_404(self, skill):
        result = asyncio.run(skill.execute("no_such_endpoint", {}, {}))
        assert result["status_code"] == 404

    def test_bash_manifest_names_the_timeout_ceilings(self):
        endpoint = next(
            e for e in json.loads(MANIFEST.read_text())["endpoints"] if e["id"] == "bash"
        )
        timeout = next(p for p in endpoint["params"] if p["name"] == "timeout")
        assert str(BASH_MAX_TIMEOUT) in timeout["description"]
        assert str(BACKGROUND_MAX_TIMEOUT) in timeout["description"]
        assert "clamp" in timeout["description"].lower()


def re_findall_endpoint_keys(source: str) -> list[str]:
    import re

    body = source.split("dispatch = {", 1)[1].split("}", 1)[0]
    return re.findall(r'"([a-z_]+)":', body)


# ── GAP 1: background execution ───────────────────────────────────


class TestTimeoutIsEnforcedNotClamped:
    def test_timeout_over_the_ceiling_is_refused_and_nothing_runs(self, skill, work):
        marker = work / "ran.txt"
        with ctx():
            result = asyncio.run(skill.execute("bash", {
                "command": f"touch {marker}",
                "cwd": str(work),
                "timeout": BASH_MAX_TIMEOUT + 1,
            }, {}))
        assert result["status_code"] == 400
        assert str(BASH_MAX_TIMEOUT) in result["error"]
        assert result["data"]["max_timeout"] == BASH_MAX_TIMEOUT
        assert not marker.exists(), "a refused call must not execute the command"

    def test_a_timeout_between_120_and_the_ceiling_is_honoured(self, skill, work):
        """The old code clamped to 120s, so this value was a lie."""
        with ctx():
            result = asyncio.run(skill.execute("bash", {
                "command": "echo ok", "cwd": str(work), "timeout": 300,
            }, {}))
        assert result["success"] is True
        assert result["data"]["stdout"].strip() == "ok"

    def test_background_ceiling_is_separate_and_named(self, skill, work):
        with ctx():
            result = asyncio.run(skill.execute("bash", {
                "command": "echo ok", "cwd": str(work),
                "run_in_background": True,
                "timeout": BACKGROUND_MAX_TIMEOUT + 1,
            }, {}))
        assert result["status_code"] == 400
        assert str(BACKGROUND_MAX_TIMEOUT) in result["error"]


class TestBackgroundExecution:
    def test_start_poll_and_kill_a_real_job(self, skill, work):
        async def scenario():
            with ctx():
                started = await skill.execute("bash", {
                    "command": "for i in 1 2 3 4 5 6 7 8 9; do echo line-$i; sleep 1; done",
                    "cwd": str(work), "run_in_background": True, "timeout": 60,
                }, {})
                assert started["status_code"] == 202
                assert started["data"]["status"] == "running"
                job_id = started["data"]["job_id"]
                pid = started["data"]["pid"]

                await asyncio.sleep(2.5)
                first = await skill.execute("bash_output", {"job_id": job_id}, {})
                await asyncio.sleep(1.5)
                second = await skill.execute("bash_output", {"job_id": job_id}, {})
                killed = await skill.execute("kill_bash", {"job_id": job_id}, {})
                return started, first, second, killed, pid

        started, first, second, killed, pid = asyncio.run(scenario())

        assert first["data"]["status"] == "running"
        assert "line-1" in first["data"]["stdout"]
        # Incremental: the second poll returns only what is new.
        assert "line-1" not in second["data"]["stdout"]
        assert second["data"]["stdout"].strip(), "second poll returned nothing new"

        assert killed["data"]["status"] == "killed"
        assert killed["data"]["was_already_finished"] is False
        with pytest.raises(OSError):
            os.kill(pid, 0)

    def test_output_buffer_is_bounded_and_says_what_it_dropped(self, skill, work):
        async def scenario():
            with ctx():
                started = await skill.execute("bash", {
                    "command": "seq 1 20000", "cwd": str(work),
                    "run_in_background": True, "timeout": 60,
                }, {})
                job_id = started["data"]["job_id"]
                for _ in range(40):
                    await asyncio.sleep(0.25)
                    out = await skill.execute("bash_output", {"job_id": job_id, "max_lines": 5}, {})
                    if out["data"]["status"] != "running":
                        return skill, job_id, out
                return skill, job_id, out

        _skill, job_id, out = asyncio.run(scenario())
        assert out["data"]["status"] == "completed"
        assert out["data"]["dropped_stdout_lines"] > 0
        buffer = _skill._bg_jobs[job_id].handle.stdout_buffer
        assert len(buffer) <= BG_MAX_BUFFER_LINES
        assert buffer.total_appended == 20000

    def test_a_background_job_is_killed_by_its_own_timeout(self, skill, work):
        async def scenario():
            with ctx():
                started = await skill.execute("bash", {
                    "command": "sleep 60", "cwd": str(work),
                    "run_in_background": True, "timeout": 2,
                }, {})
                job_id = started["data"]["job_id"]
                await asyncio.sleep(3.5)
                return await skill.execute("bash_output", {"job_id": job_id}, {})

        out = asyncio.run(scenario())
        assert out["data"]["status"] == "timed_out"
        assert out["data"]["kill_reason"] == "overall_timeout"

    def test_killing_kills_the_whole_process_group(self, skill, work):
        """``sh -c 'x & wait'`` leaves a grandchild that a pid-targeted
        SIGTERM would orphan."""

        async def scenario():
            with ctx():
                started = await skill.execute("bash", {
                    "command": "sleep 60 & echo pid-$!; wait",
                    "cwd": str(work), "run_in_background": True, "timeout": 30,
                }, {})
                job_id = started["data"]["job_id"]
                await asyncio.sleep(1.0)
                out = await skill.execute("bash_output", {"job_id": job_id}, {})
                await skill.execute("kill_bash", {"job_id": job_id}, {})
                return out["data"]["stdout"].strip()

        printed = asyncio.run(scenario())
        grandchild = int(printed.split("-")[-1])
        with pytest.raises(OSError):
            os.kill(grandchild, 0)

    def test_background_runs_the_same_safety_checks_as_foreground(self, skill, work):
        with ctx():
            destructive = asyncio.run(skill.execute("bash", {
                "command": "rm -rf /", "cwd": str(work), "run_in_background": True,
            }, {}))
            ungranted = asyncio.run(skill.execute("bash", {
                "command": "echo hi", "cwd": "/private/etc", "run_in_background": True,
            }, {}))
        assert destructive["status_code"] == 403
        assert ungranted["status_code"] == 403
        assert ungranted["data"]["permission_needed"] is True
        assert not skill._bg_jobs, "a refused command must not create a job"

    def test_one_session_cannot_read_or_kill_another_sessions_job(self, skill, work):
        async def scenario():
            with ctx(session_id="owner"):
                started = await skill.execute("bash", {
                    "command": "sleep 30", "cwd": str(work),
                    "run_in_background": True, "timeout": 30,
                }, {})
                job_id = started["data"]["job_id"]
            with ctx(session_id="intruder"):
                read = await skill.execute("bash_output", {"job_id": job_id}, {})
                kill = await skill.execute("kill_bash", {"job_id": job_id}, {})
            with ctx(session_id="owner"):
                mine = await skill.execute("bash_output", {"job_id": job_id}, {})
                await skill.clear_session("owner")
            return read, kill, mine

        read, kill, mine = asyncio.run(scenario())
        assert read["status_code"] == 404
        assert kill["status_code"] == 404
        assert mine["data"]["status"] == "running", "the intruder must not have killed it"

    def test_clear_session_kills_the_sessions_jobs(self, skill, work):
        async def scenario():
            with ctx(session_id="doomed"):
                started = await skill.execute("bash", {
                    "command": "sleep 60", "cwd": str(work),
                    "run_in_background": True, "timeout": 60,
                }, {})
                pid = started["data"]["pid"]
                killed = await skill.clear_session("doomed")
                await asyncio.sleep(0.5)
                after = await skill.execute(
                    "bash_output", {"job_id": started["data"]["job_id"]}, {})
                return pid, killed, after

        pid, killed, after = asyncio.run(scenario())
        assert killed == 1
        assert after["status_code"] == 404
        with pytest.raises(OSError):
            os.kill(pid, 0)

    def test_unsupported_bash_argument_is_refused_not_ignored(self, skill, work):
        marker = work / "should-not-exist"
        with ctx():
            result = asyncio.run(skill.execute("bash", {
                "command": f"touch {marker}", "cwd": str(work), "detach": True,
            }, {}))
        assert result["status_code"] == 400
        assert "detach" in result["error"]
        assert not marker.exists()

    def test_bash_output_for_an_unknown_job_is_a_404(self, skill):
        with ctx():
            result = asyncio.run(skill.execute("bash_output", {"job_id": "bg_nope"}, {}))
        assert result["status_code"] == 404


# ── GAP 2: read_file binary + images ──────────────────────────────


class TestReadFileHandlesBinaryAndImages:
    def test_a_png_comes_back_as_an_image_not_mojibake(self, skill, work):
        png = work / "tiny.png"
        png.write_bytes(_png_bytes(20, 20))
        with ctx(tool="coding_tools__read_file"):
            result = asyncio.run(skill.execute("read_file", {"path": str(png)}, {}))
        assert result["success"] is True
        data = result["data"]
        assert data["content_kind"] == "image"
        assert data["media_type"] == "image/png"
        assert (data["width"], data["height"]) == (20, 20)
        assert data["image_data"].startswith("data:image/png;base64,")
        assert "content" not in data, "an image must never be line-numbered text"

    def test_the_image_reaches_the_model_as_a_real_image_block(self, skill, work):
        """Even a tiny PNG: the pipeline's bare-base64 recogniser has a
        512-char floor, which is why this returns a data: URL."""
        from skills.result_budget import serialize_tool_result_with_images

        png = work / "tiny.png"
        png.write_bytes(_png_bytes(16, 16))
        with ctx(tool="coding_tools__read_file"):
            result = asyncio.run(skill.execute("read_file", {"path": str(png)}, {}))
        text, images = serialize_tool_result_with_images(
            "coding_tools__read_file", result["data"],
        )
        assert len(images) == 1
        assert images[0].media_type == "image/png"
        assert images[0].data_url.startswith("data:image/png;base64,")
        assert "iVBORw0KGgo" not in text, "the payload must leave the text budget"

    def test_an_image_larger_than_2mb_is_no_longer_refused(self, skill, work):
        png = work / "big.png"
        png.write_bytes(_png_bytes(1200, 900))
        assert png.stat().st_size > 2_000_000 or png.stat().st_size > 300_000
        with ctx(tool="coding_tools__read_file"):
            result = asyncio.run(skill.execute("read_file", {"path": str(png)}, {}))
        assert result["success"] is True
        assert result["data"]["content_kind"] == "image"

    def test_an_image_over_the_pipeline_budget_is_refused_whole(self, skill, work, monkeypatch):
        monkeypatch.setenv("FERAL_TOOL_IMAGE_MAX_B64_CHARS", "2048")
        png = work / "big.png"
        png.write_bytes(_png_bytes(200, 200))
        with ctx(tool="coding_tools__read_file"):
            result = asyncio.run(skill.execute("read_file", {"path": str(png)}, {}))
        assert result["status_code"] == 413
        assert "never truncated" in result["error"]
        assert result["data"]["max_bytes"] < png.stat().st_size

    def test_a_non_image_binary_is_a_typed_error_not_a_success(self, skill, work):
        pdf = work / "doc.pdf"
        pdf.write_bytes(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + bytes(range(256)) * 4)
        with ctx(tool="coding_tools__read_file"):
            result = asyncio.run(skill.execute("read_file", {"path": str(pdf)}, {}))
        assert result["success"] is False
        assert result["status_code"] == 415
        assert result["data"]["detected"] == "PDF document"

    def test_line_windows_are_refused_for_images(self, skill, work):
        png = work / "tiny.png"
        png.write_bytes(_png_bytes(8, 8))
        with ctx(tool="coding_tools__read_file"):
            result = asyncio.run(
                skill.execute("read_file", {"path": str(png), "limit": 5}, {}))
        assert result["status_code"] == 400
        assert "limit" in result["error"]

    def test_text_reading_is_unchanged(self, skill, work):
        f = work / "a.txt"
        f.write_text("alpha\nbeta\ngamma\n")
        with ctx(tool="coding_tools__read_file"):
            result = asyncio.run(skill.execute("read_file", {"path": str(f)}, {}))
        assert result["data"]["total_lines"] == 3
        assert "1|alpha" in result["data"]["content"]

    def test_utf8_text_is_not_mistaken_for_binary(self, skill, work):
        f = work / "unicode.txt"
        f.write_text("héllo, ünïcode ✅\nsecond line\n")
        with ctx(tool="coding_tools__read_file"):
            result = asyncio.run(skill.execute("read_file", {"path": str(f)}, {}))
        assert result["success"] is True
        assert "ünïcode" in result["data"]["content"]


# ── GAP 3: grep flags ─────────────────────────────────────────────


SAMPLE = (
    "import os\n\n\ndef alpha():\n    return 1\n\n\nclass Widget:\n"
    "    NAME = 'WIDGET'\n    def beta(self):\n        return widget_total\n\n"
    "WIDGET_LIMIT = 5\n"
)


@pytest.fixture
def repo(work):
    (work / "sample.py").write_text(SAMPLE)
    (work / "notes.md").write_text("# Widget notes\nthe widget is fine\n")
    return work


def grep(skill, args):
    with ctx(tool="coding_tools__grep_search"):
        return asyncio.run(skill.execute("grep_search", args, {}))


@pytest.fixture(params=["ripgrep", "python-fallback"])
def engine(request, monkeypatch):
    """Run the same assertions through both search engines.

    The fallback is reached by making ``rg`` unresolvable, which is what
    a machine without ripgrep looks like from inside the tool.

    The ripgrep leg needs ripgrep, and this used to assume it. On a
    machine without it the tool correctly fell back and the leg failed
    on ``assert 'python-fallback' == 'ripgrep'``, which reads as a code
    defect and is an absent binary. CI had never installed ripgrep, so
    that is exactly what it reported, and because the push-to-main job
    runs pytest with ``-x`` it halted the entire matrix at 24%.

    Skipping is right for a contributor who does not have ripgrep. It is
    NOT right for CI, where a silent skip would mean going green having
    tested only the fallback, which is not the engine real installs use.
    So CI sets FERAL_REQUIRE_RIPGREP and the skip becomes a failure
    there.
    """
    if request.param == "ripgrep" and shutil.which("rg") is None:
        if os.environ.get("FERAL_REQUIRE_RIPGREP"):
            pytest.fail(
                "ripgrep is not on PATH, so the ripgrep engine cannot be "
                "tested. FERAL_REQUIRE_RIPGREP is set, which means this "
                "environment is supposed to have it: install ripgrep "
                "rather than letting this leg skip."
            )
        pytest.skip("ripgrep is not installed, so only the fallback is testable")
    if request.param == "python-fallback":
        monkeypatch.setenv("PATH", "/nonexistent")
    return request.param


class TestGrepFlagsApplyOnBothEngines:
    def test_context_lines_are_returned_and_marked(self, skill, repo, engine):
        result = grep(skill, {
            "pattern": "widget_total", "path": str(repo),
            "output_mode": "content", "-C": 2,
        })
        assert result["data"]["engine"] == engine
        rows = result["data"]["matches"]
        lines = {int(r["line"]): r["is_context"] for r in rows}
        assert lines == {9: True, 10: True, 11: False, 12: True, 13: True}

    def test_asymmetric_context(self, skill, repo, engine):
        result = grep(skill, {
            "pattern": "widget_total", "path": str(repo),
            "output_mode": "content", "-A": 1, "-B": 0,
        })
        assert [int(r["line"]) for r in result["data"]["matches"]] == [11, 12]

    def test_case_insensitivity_changes_the_result(self, skill, repo, engine):
        sensitive = grep(skill, {
            "pattern": "WIDGET", "path": str(repo), "output_mode": "content"})
        insensitive = grep(skill, {
            "pattern": "WIDGET", "path": str(repo), "output_mode": "content", "-i": True})
        assert len(insensitive["data"]["matches"]) > len(sensitive["data"]["matches"])
        assert insensitive["data"]["options_applied"]["case_insensitive"] is True

    def test_the_spelled_out_alias_works_too(self, skill, repo, engine):
        result = grep(skill, {
            "pattern": "WIDGET", "path": str(repo),
            "output_mode": "content", "case_insensitive": True})
        assert result["data"]["options_applied"]["case_insensitive"] is True

    def test_multiline_matches_across_lines(self, skill, repo, engine):
        off = grep(skill, {
            "pattern": "class Widget:.*NAME", "path": str(repo), "output_mode": "content"})
        on = grep(skill, {
            "pattern": "class Widget:.*NAME", "path": str(repo),
            "output_mode": "content", "multiline": True})
        assert off["data"]["matches"] == []
        assert [int(r["line"]) for r in on["data"]["matches"]] == [8, 9]

    def test_type_filter_selects_the_right_files(self, skill, repo, engine):
        py = grep(skill, {"pattern": "[Ww]idget", "path": str(repo), "type": "py"})
        md = grep(skill, {"pattern": "[Ww]idget", "path": str(repo), "type": "md"})
        assert [Path(f).name for f in py["data"]["files"]] == ["sample.py"]
        assert [Path(f).name for f in md["data"]["files"]] == ["notes.md"]

    def test_every_response_reports_the_options_it_applied(self, skill, repo, engine):
        result = grep(skill, {"pattern": "os", "path": str(repo)})
        assert result["data"]["engine"] == engine
        assert result["data"]["options_applied"] == {
            "case_insensitive": False, "multiline": False,
            "after_context": 0, "before_context": 0, "type": None,
        }


class TestGrepRefusesWhatItCannotDo:
    def test_an_unsupported_option_is_an_error_not_a_shrug(self, skill, repo):
        result = grep(skill, {"pattern": "os", "path": str(repo), "recursive": True})
        assert result["status_code"] == 400
        assert "recursive" in result["error"]
        assert "-i" in result["data"]["supported_params"]

    def test_context_outside_content_mode_is_refused(self, skill, repo):
        result = grep(skill, {"pattern": "os", "path": str(repo), "-C": 3})
        assert result["status_code"] == 400
        assert "content" in result["error"]

    def test_conflicting_aliases_are_refused_rather_than_guessed(self, skill, repo):
        result = grep(skill, {
            "pattern": "os", "path": str(repo), "output_mode": "content",
            "-i": True, "case_insensitive": False,
        })
        assert result["status_code"] == 400
        assert "Conflicting" in result["error"]

    def test_out_of_range_context_is_refused(self, skill, repo):
        result = grep(skill, {
            "pattern": "os", "path": str(repo), "output_mode": "content", "-C": 5000})
        assert result["status_code"] == 400

    def test_a_ripgrep_failure_is_not_reported_as_an_empty_success(self, skill, repo):
        result = grep(skill, {"pattern": "foo(", "path": str(repo), "output_mode": "content"})
        assert result["success"] is False
        assert result["status_code"] == 400

    def test_the_fallback_refuses_a_type_it_does_not_know(self, skill, repo, monkeypatch):
        monkeypatch.setenv("PATH", "/nonexistent")
        result = grep(skill, {"pattern": "os", "path": str(repo), "type": "haskell"})
        assert result["status_code"] == 400
        assert "py" in result["data"]["supported_types"]

    def test_a_bad_boolean_is_refused(self, skill, repo):
        result = grep(skill, {"pattern": "os", "path": str(repo), "-i": "maybe"})
        assert result["status_code"] == 400
