"""Two host conditions that silently ruin long work.

Both failures happened while this project was being built:

* the machine slowed to a crawl on a 99% full disk and nothing in the
  brain noticed or said so, and
* a long agent task died mid-run with "your computer went to sleep",
  losing the work with no record of why.

Neither had any code. ``caffeinate`` appeared once in the whole tree, as
a command the agent is allowed to run, never as something the brain does
for itself.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from system.preflight import (
    CRITICAL_DISK_BYTES,
    LOW_DISK_BYTES,
    DiskStatus,
    StayAwake,
    check_disk,
)


def _pid_alive(pid: int) -> bool:
    """Ask the OS, not FERAL's bookkeeping."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _caffeinate_argv(pid: int) -> list[str]:
    """The real argv of a running caffeinate, straight from ps."""
    out = subprocess.run(
        ["ps", "-o", "command=", "-p", str(pid)],
        capture_output=True, text=True, timeout=10,
    )
    return out.stdout.strip().split()


class TestDisk:
    def test_it_measures_the_real_volume(self, tmp_path):
        status = check_disk(tmp_path)
        assert status.total_bytes > 0
        assert status.free_bytes > 0
        assert status.level in {"ok", "low", "critical"}

    def test_an_unwritten_path_walks_up_to_a_real_one(self, tmp_path):
        """FERAL_HOME may not exist yet on first boot, and 'cannot
        measure' would be the wrong answer for a directory whose parent
        is perfectly measurable."""
        deep = tmp_path / "not" / "created" / "yet"
        assert check_disk(deep).level != "unknown"

    def test_it_never_raises_on_a_hopeless_path(self):
        status = check_disk("/dev/null/definitely/not/a/directory")
        assert isinstance(status, DiskStatus)

    def test_the_thresholds_are_ordered(self):
        assert CRITICAL_DISK_BYTES < LOW_DISK_BYTES

    def test_percent_used_survives_a_zero_total(self):
        """A volume reporting zero total must not divide by zero."""
        assert DiskStatus(__import__("pathlib").Path("/"), 0, 0, "unknown", "").percent_used == 0.0

    def test_the_detail_says_what_to_do_when_it_is_bad(self, monkeypatch):
        """A number with no remedy is not actionable."""
        monkeypatch.setattr(
            shutil, "disk_usage",
            lambda _p: shutil._ntuple_diskusage(100 * 1024**3, 100 * 1024**3, 1024 * 1024),
        )
        status = check_disk("/")
        assert status.level == "critical"
        assert "Free space" in status.detail or "free" in status.detail.lower()


class TestStayAwake:
    @pytest.fixture(autouse=True)
    def _reset(self):
        while StayAwake.depth():
            StayAwake.release()
        yield
        while StayAwake.depth():
            StayAwake.release()

    def test_it_reference_counts(self):
        """Concurrent background jobs share one assertion, and the last
        one out releases it. Without this, two jobs and one release would
        leave the machine awake forever."""
        StayAwake.acquire("first")
        StayAwake.acquire("second")
        assert StayAwake.depth() == 2
        StayAwake.release()
        assert StayAwake.depth() == 1
        StayAwake.release()
        assert StayAwake.depth() == 0

    def test_release_below_zero_is_harmless(self):
        StayAwake.release()
        StayAwake.release()
        assert StayAwake.depth() == 0

    def test_the_context_manager_releases_on_an_exception(self):
        """A job that raises must not leak an assertion."""
        with pytest.raises(RuntimeError):
            with StayAwake("boom"):
                raise RuntimeError("boom")
        assert StayAwake.depth() == 0

    def test_it_nests(self):
        with StayAwake("outer"):
            with StayAwake("inner"):
                assert StayAwake.depth() == 2
            assert StayAwake.depth() == 1
        assert StayAwake.depth() == 0

    def test_an_unsupported_host_still_runs_the_work(self, monkeypatch):
        """Inhibiting sleep is best effort. A machine that will not hold
        an assertion must still start the job."""
        monkeypatch.setattr(StayAwake, "supported", classmethod(lambda cls: False))
        assert StayAwake.acquire("x") is False
        assert StayAwake.depth() == 1
        StayAwake.release()
        assert StayAwake.depth() == 0

    @pytest.mark.skipif(sys.platform != "darwin", reason="caffeinate is macOS only")
    def test_it_really_holds_an_assertion_on_this_mac(self):
        assert StayAwake.supported()
        with StayAwake("test"):
            assert StayAwake.held()
        assert not StayAwake.held()


class TestBackgroundJobsAreWiredToIt:
    def test_the_job_lifecycle_acquires_and_releases(self):
        """Structural: the acquire is in the start path and the release
        is in the watcher's finally, so a crashed watcher cannot leak."""
        import inspect

        from skills.impl import coding_tools

        source = inspect.getsource(coding_tools)
        assert "_stay_awake_acquire(" in source
        watcher = inspect.getsource(coding_tools.CodingToolsSkill._forget_pgid_when_done)
        assert "_stay_awake_release()" in watcher
        finally_block = watcher.split("finally:")[-1]
        assert "_stay_awake_release()" in finally_block, (
            "the release must sit in finally, or a cancelled watcher leaks the assertion"
        )


class TestTheAssertionCannotOutliveTheBrain:
    """`caffeinate` is a child process, so it survives its parent.

    A sleep assertion nobody owns is invisible and effectively permanent:
    the user's Mac simply stops idle-sleeping and nothing on screen says
    why. `coding_tools` already registers an atexit reaper for its
    background process groups; the sleep assertion had none, so a brain
    that exited with a job still registered left one behind.
    """

    def test_release_all_ends_the_assertion_whatever_the_depth(self):
        StayAwake.release_all()
        StayAwake.acquire("one")
        StayAwake.acquire("two")
        assert StayAwake.depth() == 2
        # release() is correct to do nothing here; that is exactly why it
        # is the wrong thing to call at interpreter shutdown.
        StayAwake.release()
        assert StayAwake.depth() == 1
        StayAwake.release_all()
        assert StayAwake.depth() == 0
        assert not StayAwake.held()

    def test_release_all_is_safe_when_nothing_is_held(self):
        StayAwake.release_all()
        StayAwake.release_all()
        assert not StayAwake.held()

    def test_it_is_registered_with_atexit(self):
        """Structural, because atexit cannot be triggered in-process."""
        import inspect

        from system import preflight

        source = inspect.getsource(preflight)
        assert "atexit.register(StayAwake.release_all)" in source

    @pytest.mark.skipif(sys.platform != "darwin", reason="caffeinate is macOS only")
    def test_a_child_interpreter_leaves_no_caffeinate_behind(self):
        """The real thing: acquire in a subprocess, let it exit, check the OS.

        Asserting on FERAL's own bookkeeping would pass even while a real
        `caffeinate` kept running, which is the whole failure mode.
        """
        import subprocess as sp
        import sys as _sys
        import textwrap
        import time as _time

        script = textwrap.dedent(
            """
            import sys
            sys.path.insert(0, %r)
            from system.preflight import StayAwake
            StayAwake.acquire("leak-test")
            assert StayAwake.held()
            print(StayAwake._proc.pid, flush=True)
            # Exit WITHOUT releasing. atexit must do it for us.
            """
        ) % str(Path(__file__).resolve().parents[1])

        out = sp.run(
            [_sys.executable, "-c", script], capture_output=True, text=True, timeout=30,
        )
        assert out.returncode == 0, out.stderr
        pid = int(out.stdout.strip().splitlines()[-1])

        for _ in range(30):  # up to 3s for the child to be reaped
            if not _pid_alive(pid):
                break
            _time.sleep(0.1)
        assert not _pid_alive(pid), (
            f"caffeinate pid {pid} outlived the interpreter that started it; "
            "the machine would never idle-sleep again and nothing can find it"
        )

    @pytest.mark.skipif(sys.platform != "darwin", reason="caffeinate is macOS only")
    def test_the_assertion_carries_a_dead_mans_ceiling(self):
        """atexit does not run on SIGKILL, so the child needs its own limit."""
        StayAwake.release_all()
        StayAwake.acquire("ceiling")
        try:
            assert StayAwake.held()
            cmd = _caffeinate_argv(StayAwake._proc.pid)
            assert "-t" in cmd, f"no ceiling on the assertion: {cmd}"
            ceiling = int(cmd[cmd.index("-t") + 1])
            # Must not be short enough to cut a legitimate background job
            # short, and must not be unbounded.
            assert ceiling >= 3_600
            assert ceiling <= 86_400
        finally:
            StayAwake.release_all()
