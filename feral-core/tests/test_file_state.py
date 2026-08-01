"""Contract for read-before-edit / staleness tracking (skills/file_state.py)."""

from __future__ import annotations

import asyncio
import os

import pytest

from skills import file_state
from skills.file_state import (
    VERDICT_GONE,
    VERDICT_NEVER_READ,
    VERDICT_OK,
    VERDICT_STALE,
    FileStateTracker,
    bash_is_read_only,
    enforcement_mode,
)


@pytest.fixture
def tracker() -> FileStateTracker:
    return FileStateTracker()


@pytest.fixture(autouse=True)
def _warn_mode(monkeypatch):
    monkeypatch.setenv("FERAL_READ_BEFORE_EDIT", "warn")


# ── verdicts ──────────────────────────────────────────────────────────


def test_new_file_needs_no_read(tracker, tmp_path):
    check = tracker.check_write("s1", tmp_path / "brand-new.txt")
    assert check.verdict == VERDICT_OK
    assert check.allowed is True


def test_never_read(tracker, tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hello\n")
    check = tracker.check_write("s1", target)
    assert check.verdict == VERDICT_NEVER_READ


def test_read_then_write_is_ok(tracker, tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hello\n")
    tracker.record_read("s1", target)
    assert tracker.check_write("s1", target).verdict == VERDICT_OK


def test_stale_when_content_changed(tracker, tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hello\n")
    tracker.record_read("s1", target)
    target.write_text("somebody else wrote this\n")
    assert tracker.check_write("s1", target).verdict == VERDICT_STALE


def test_stale_detected_even_when_mtime_is_restored(tracker, tmp_path):
    """mtime alone is not enough: editors restore it on save-in-place and
    FERAL's own writes land inside the same clock second as the read."""
    target = tmp_path / "a.txt"
    target.write_text("hello\n")
    tracker.record_read("s1", target)
    original = target.stat()
    target.write_text("tampered but same length!\n"[: len("hello\n")])
    os.utime(target, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert target.stat().st_mtime_ns == original.st_mtime_ns
    assert target.stat().st_size == original.st_size
    assert tracker.check_write("s1", target).verdict == VERDICT_STALE


def test_gone(tracker, tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hello\n")
    tracker.record_read("s1", target)
    target.unlink()
    assert tracker.check_write("s1", target).verdict == VERDICT_GONE


def test_observations_are_per_session(tracker, tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hello\n")
    tracker.record_read("s1", target)
    assert tracker.check_write("s2", target).verdict == VERDICT_NEVER_READ


def test_note_write_refreshes_the_observation(tracker, tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hello\n")
    tracker.record_read("s1", target)
    target.write_text("agent wrote this\n")
    tracker.note_write("s1", target)
    assert tracker.check_write("s1", target).verdict == VERDICT_OK


# ── no `partial` verdict ──────────────────────────────────────────────


def test_partial_read_is_recorded_but_never_gates(tracker, tmp_path):
    """Deliberately not a verdict. The model legitimately reads a big
    file in windows and then edits outside the window; refusing that is a
    false positive that trains it to spam whole-file reads. The flag is
    recorded so a later lane can measure it."""
    target = tmp_path / "big.txt"
    target.write_text("\n".join(str(i) for i in range(500)))
    obs = tracker.record_read("s1", target, partial=True, window=(1, 50))
    assert obs is not None and obs.partial is True and obs.window == (1, 50)

    check = tracker.check_write("s1", target)
    assert check.verdict == VERDICT_OK
    assert check.allowed is True
    assert check.partial_read is True
    assert check.as_dict()["read_was_partial"] is True


# ── enforcement modes ─────────────────────────────────────────────────


def test_default_mode_is_warn(monkeypatch):
    monkeypatch.delenv("FERAL_READ_BEFORE_EDIT", raising=False)
    assert enforcement_mode() == "warn"


def test_warn_mode_allows_the_write(tracker, tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_READ_BEFORE_EDIT", "warn")
    target = tmp_path / "a.txt"
    target.write_text("hello\n")
    check = tracker.check_write("s1", target)
    assert check.verdict == VERDICT_NEVER_READ
    assert check.allowed is True


def test_enforce_mode_blocks_the_write(tracker, tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_READ_BEFORE_EDIT", "enforce")
    target = tmp_path / "a.txt"
    target.write_text("hello\n")
    check = tracker.check_write("s1", target)
    assert check.verdict == VERDICT_NEVER_READ
    assert check.allowed is False


def test_off_mode_disables_the_check(tracker, tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_READ_BEFORE_EDIT", "off")
    target = tmp_path / "a.txt"
    target.write_text("hello\n")
    assert tracker.check_write("s1", target).verdict == VERDICT_OK


# ── locking ───────────────────────────────────────────────────────────


def test_lock_is_per_path_and_stable(tracker, tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    assert tracker.lock_for(a) is tracker.lock_for(a)
    assert tracker.lock_for(a) is not tracker.lock_for(b)


async def test_lock_serialises_check_then_write(tracker, tmp_path):
    """`spawn_subagents` runs up to six concurrent workers with full
    coding_tools access, so check-then-write is a real TOCTOU window."""
    target = tmp_path / "a.txt"
    target.write_text("0")
    order: list[str] = []

    async def worker(name: str):
        async with tracker.lock_for(target):
            order.append(f"{name}-enter")
            await asyncio.sleep(0.01)
            order.append(f"{name}-exit")

    await asyncio.gather(worker("a"), worker("b"))
    # Never interleaved: each enter is immediately followed by its exit.
    assert order[0].endswith("-enter") and order[1] == order[0].replace("enter", "exit")


# ── bash invalidation ─────────────────────────────────────────────────


@pytest.mark.parametrize("command", [
    "ls -la", "cat foo.txt", "git status", "git log --oneline",
    "grep -rn pattern .", "rg pattern", "wc -l foo", "head -5 a | wc -l",
    "pwd", "find . -name '*.py'",
])
def test_read_only_commands_do_not_invalidate(command):
    assert bash_is_read_only(command) is True


@pytest.mark.parametrize("command", [
    "sed -i s/a/b/ foo.py", "python build.py", "npm install",
    "git checkout .", "git apply patch.diff", "rm foo",
    "echo hi > foo.txt", "cat a >> b", "ls | tee out.txt",
    "make", "black .", "ls && npm run build",
])
def test_write_capable_commands_invalidate(command):
    assert bash_is_read_only(command) is False


def test_invalidate_session_drops_every_observation(tracker, tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("a")
    b.write_text("b")
    tracker.record_read("s1", a)
    tracker.record_read("s1", b)
    tracker.record_read("s2", a)

    assert tracker.invalidate_session("s1", reason="bash") == 2
    assert tracker.check_write("s1", a).verdict == VERDICT_NEVER_READ
    # Other sessions are untouched.
    assert tracker.check_write("s2", a).verdict == VERDICT_OK


def test_module_exposes_a_singleton():
    assert file_state.get_tracker() is file_state.get_tracker()
