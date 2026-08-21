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

import shutil
import sys

import pytest

from system.preflight import (
    CRITICAL_DISK_BYTES,
    LOW_DISK_BYTES,
    DiskStatus,
    StayAwake,
    check_disk,
)


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
