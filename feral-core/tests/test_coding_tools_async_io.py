"""``coding_tools`` file I/O must not block the event loop.

``_read_file`` / ``_write_file`` / ``_edit_file`` are ``async def`` but used
to call ``Path.read_text`` / ``Path.write_text`` / ``Path.mkdir`` directly,
so every read and write stalled the whole brain. These functions are also
where fuzzy matching, blob compression and subprocess work land next, which
would make the stall far worse.

The thread-identity assertions below are the actual regression guard: the
blocking work must run on a worker thread, not the loop thread. The rest of
the file pins the pre-existing behaviour and error shapes so the move stays
behaviour-preserving.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from security.sandbox_policy import SandboxPolicy  # noqa: E402
from skills.impl.coding_tools import CodingToolsSkill  # noqa: E402


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    SandboxPolicy.load_default().grant_folder(str(tmp_path), mode="readwrite")
    return tmp_path


@pytest.fixture
def skill() -> CodingToolsSkill:
    return CodingToolsSkill()


class _ThreadSpy:
    """Records the thread each patched Path method ran on."""

    def __init__(self, monkeypatch, method_names: tuple[str, ...]) -> None:
        self.threads: dict[str, int] = {}
        for name in method_names:
            monkeypatch.setattr(Path, name, self._wrap(name, getattr(Path, name)))

    def _wrap(self, name, original):
        def wrapper(inner_self, *a, **kw):
            self.threads[name] = threading.get_ident()
            return original(inner_self, *a, **kw)
        return wrapper

    def assert_off_loop(self, loop_thread: int) -> None:
        assert self.threads, "patched method was never called"
        for name, ident in self.threads.items():
            assert ident != loop_thread, (
                f"Path.{name} ran on the event loop thread; blocking I/O "
                f"must be handed to asyncio.to_thread"
            )


@pytest.mark.asyncio
class TestFileIOIsOffTheEventLoop:
    async def test_read_file_reads_on_a_worker_thread(self, skill, workspace, monkeypatch):
        target = workspace / "sample.txt"
        target.write_text("alpha\nbeta\ngamma\n")
        loop_thread = threading.get_ident()
        spy = _ThreadSpy(monkeypatch, ("read_text",))

        result = await skill._read_file({"path": str(target)})

        assert result["success"] is True, result
        spy.assert_off_loop(loop_thread)

    async def test_write_file_writes_on_a_worker_thread(self, skill, workspace, monkeypatch):
        target = workspace / "nested" / "out.txt"
        loop_thread = threading.get_ident()
        spy = _ThreadSpy(monkeypatch, ("write_text", "mkdir"))

        result = await skill._write_file({"path": str(target), "content": "hello"})

        assert result["success"] is True, result
        assert target.read_text() == "hello"
        spy.assert_off_loop(loop_thread)

    async def test_edit_file_reads_and_writes_on_a_worker_thread(self, skill, workspace, monkeypatch):
        target = workspace / "edit.txt"
        target.write_text("one two three")
        loop_thread = threading.get_ident()
        spy = _ThreadSpy(monkeypatch, ("read_text", "write_text"))

        result = await skill._edit_file(
            {"path": str(target), "old_text": "two", "new_text": "TWO"},
        )

        assert result["success"] is True, result
        spy.assert_off_loop(loop_thread)

    async def test_loop_stays_responsive_during_a_slow_read(self, skill, workspace, monkeypatch):
        """A slow read must not starve other coroutines on the loop."""
        target = workspace / "slow.txt"
        target.write_text("payload")
        original = Path.read_text

        def slow_read(inner_self, *a, **kw):
            # Deliberately synchronous: on the loop thread this freezes
            # everything, on a worker thread the ticker keeps counting.
            threading.Event().wait(0.2)
            return original(inner_self, *a, **kw)

        monkeypatch.setattr(Path, "read_text", slow_read)

        ticks = 0

        async def ticker():
            nonlocal ticks
            for _ in range(20):
                await asyncio.sleep(0.01)
                ticks += 1

        tick_task = asyncio.create_task(ticker())
        result = await skill._read_file({"path": str(target)})
        # Sample immediately: after awaiting the ticker to completion the
        # count would always reach 20 and prove nothing.
        ticks_during_read = ticks
        await tick_task

        assert result["success"] is True, result
        assert ticks_during_read >= 5, (
            f"event loop was blocked for the whole read (ticks={ticks_during_read})"
        )


@pytest.mark.asyncio
class TestBehaviourPreserved:
    async def test_read_file_numbers_lines_and_reports_total(self, skill, workspace):
        target = workspace / "numbered.txt"
        target.write_text("a\nb\nc\n")

        result = await skill._read_file({"path": str(target)})

        assert result == {
            "success": True,
            "status_code": 200,
            "data": {
                "path": str(target),
                "content": "     1|a\n     2|b\n     3|c",
                "total_lines": 3,
            },
            "error": None,
        }

    async def test_read_file_honours_offset_and_limit(self, skill, workspace):
        target = workspace / "window.txt"
        target.write_text("l1\nl2\nl3\nl4\nl5\n")

        result = await skill._read_file({"path": str(target), "offset": 2, "limit": 2})

        assert result["data"]["content"] == "     2|l2\n     3|l3"
        assert result["data"]["total_lines"] == 5

    async def test_read_file_missing_is_404(self, skill, workspace):
        result = await skill._read_file({"path": str(workspace / "nope.txt")})
        assert result["success"] is False
        assert result["status_code"] == 404
        assert result["data"] is None
        assert "File not found" in result["error"]

    async def test_read_file_directory_is_400(self, skill, workspace):
        sub = workspace / "adir"
        sub.mkdir()
        result = await skill._read_file({"path": str(sub)})
        assert result["success"] is False
        assert result["status_code"] == 400
        assert "Not a file" in result["error"]

    async def test_read_file_oversize_is_413(self, skill, workspace):
        target = workspace / "big.txt"
        target.write_text("x" * 2_000_001)
        result = await skill._read_file({"path": str(target)})
        assert result["success"] is False
        assert result["status_code"] == 413
        assert "File too large" in result["error"]

    async def test_write_file_creates_parents_and_counts_bytes(self, skill, workspace):
        target = workspace / "deep" / "deeper" / "f.txt"

        result = await skill._write_file({"path": str(target), "content": "héllo"})

        assert result == {
            "success": True,
            "status_code": 200,
            "data": {"path": str(target), "bytes_written": len("héllo".encode())},
            "error": None,
        }
        assert target.read_text() == "héllo"

    async def test_edit_file_replaces_unique_match(self, skill, workspace):
        target = workspace / "e.txt"
        target.write_text("keep OLD keep")

        result = await skill._edit_file(
            {"path": str(target), "old_text": "OLD", "new_text": "NEW"},
        )

        assert result["success"] is True
        assert result["status_code"] == 200
        assert result["error"] is None
        # The envelope this originally asserted on exactly has since gained
        # additive keys from the reliability lane (`match_strategy`,
        # `matched_lines`, and the checkpoint/diagnostics fields when those
        # are active), all declared in the manifest's returns_description.
        # Pin the fields this test is actually about and let the rest grow.
        assert result["data"]["path"] == str(target)
        assert result["data"]["replacements"] == 1
        assert target.read_text() == "keep NEW keep"

    async def test_edit_file_missing_is_404_before_old_text_check(self, skill, workspace):
        result = await skill._edit_file({"path": str(workspace / "gone.txt")})
        assert result["status_code"] == 404
        assert "File not found" in result["error"]

    async def test_edit_file_requires_old_text(self, skill, workspace):
        target = workspace / "e2.txt"
        target.write_text("body")
        result = await skill._edit_file({"path": str(target), "old_text": ""})
        assert result["status_code"] == 400
        assert result["error"] == "old_text is required"

    async def test_edit_file_no_match_is_404(self, skill, workspace):
        target = workspace / "e3.txt"
        target.write_text("body")
        result = await skill._edit_file(
            {"path": str(target), "old_text": "absent", "new_text": "x"},
        )
        assert result["status_code"] == 404
        # Wording changed with the fallback matchers: a miss now means
        # "not found byte-exactly and not found under any fallback", which
        # is a stronger and more useful statement than the original. The
        # status code and the no-write guarantee are what this pins.
        assert "not found" in result["error"]
        assert target.read_text() == "body"

    async def test_edit_file_ambiguous_match_is_409_and_does_not_write(self, skill, workspace):
        target = workspace / "e4.txt"
        target.write_text("dup dup")

        result = await skill._edit_file(
            {"path": str(target), "old_text": "dup", "new_text": "x"},
        )

        assert result["status_code"] == 409
        assert "matches 2 locations" in result["error"]
        assert target.read_text() == "dup dup"
