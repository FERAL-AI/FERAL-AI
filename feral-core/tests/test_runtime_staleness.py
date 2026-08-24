"""A running brain must notice when it is no longer the installed one.

A Python process never reloads its source, so
`pip install --upgrade feral-ai` cannot reach a brain that is already
running. The upgrade succeeds, nothing errors, and the operator keeps
using the old build with no symptom at all.

Measured on a real install on 2026-08-23: a brain had been serving for
two days and one hour from code loaded before four releases had landed.
The operator had upgraded from PyPI, correctly. Every fix in those
releases was inert, and nothing in the product noticed or said so.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from config.staleness import _human_uptime


FERAL_CORE = Path(__file__).resolve().parents[1]


class TestTheComparison:

    def test_matching_versions_are_not_stale(self, monkeypatch):
        import config.staleness as st

        monkeypatch.setattr(st, "RUNNING_VERSION", "2026.8.25")
        monkeypatch.setattr(st, "installed_version", lambda: "2026.8.25")
        result = st.runtime_staleness()
        assert result["stale"] is False
        assert "2026.8.25" in result["detail"]

    def test_an_upgrade_under_a_live_process_is_caught(self, monkeypatch):
        """The whole point."""
        import config.staleness as st

        monkeypatch.setattr(st, "RUNNING_VERSION", "2026.8.21")
        monkeypatch.setattr(st, "installed_version", lambda: "2026.8.25")
        result = st.runtime_staleness()
        assert result["stale"] is True
        assert result["running_version"] == "2026.8.21"
        assert result["installed_version"] == "2026.8.25"

    def test_the_message_says_what_to_do(self, monkeypatch):
        """A diagnostic that reports a problem without the remedy makes
        the operator go looking, and the remedy here is one word."""
        import config.staleness as st

        monkeypatch.setattr(st, "RUNNING_VERSION", "2026.8.21")
        monkeypatch.setattr(st, "installed_version", lambda: "2026.8.25")
        detail = st.runtime_staleness()["detail"]
        assert "restart" in detail.lower()
        assert "feral restart" in detail

    def test_an_unreadable_version_is_not_reported_as_stale(self, monkeypatch):
        """Telling somebody to restart on the strength of a failed
        lookup is worse than staying quiet."""
        import config.staleness as st

        monkeypatch.setattr(st, "RUNNING_VERSION", "2026.8.25")
        monkeypatch.setattr(st, "installed_version", lambda: None)
        result = st.runtime_staleness()
        assert result["stale"] is False
        assert result["detail"]

    def test_an_unknown_running_version_is_not_reported_as_stale(self, monkeypatch):
        import config.staleness as st

        monkeypatch.setattr(st, "RUNNING_VERSION", "unknown")
        monkeypatch.setattr(st, "installed_version", lambda: "2026.8.25")
        assert st.runtime_staleness()["stale"] is False

    def test_it_never_raises(self, monkeypatch):
        """This runs on /api/dashboard, the endpoint the whole shell
        polls. A diagnostic must never cost the page."""
        import config.staleness as st

        def _boom():
            raise RuntimeError("metadata exploded")

        monkeypatch.setattr(st, "installed_version", _boom)
        with pytest.raises(RuntimeError):
            st.installed_version()
        # The real guard is at the call site in the dashboard route.
        from api.routes.dashboard import _runtime_staleness

        assert isinstance(_runtime_staleness(), dict)


class TestUptimeRendering:
    """"Restart it" lands differently at four minutes and at two days."""

    @pytest.mark.parametrize("seconds, expected", [
        (0, "0m"),
        (90, "1m"),
        (3600, "1h 0m"),
        (7380, "2h 3m"),
        (86400, "1d 0h"),
        (2 * 86400 + 3600, "2d 1h"),
    ])
    def test_reads_as_a_human_would_say_it(self, seconds, expected):
        assert _human_uptime(seconds) == expected


class TestItIsActuallyReachable:
    """A diagnostic that only exists in a source checkout is worse than
    none: it reports healthy from the one place the bug cannot occur.

    The first version of this module was `feral-core/runtime_staleness.py`,
    and `feral doctor` answered `No module named 'runtime_staleness'`,
    because `version.py` is the only top-level module the wheel ships.
    """

    def test_it_lives_in_a_packaged_subpackage(self):
        assert (FERAL_CORE / "config" / "staleness.py").is_file()
        assert not (FERAL_CORE / "runtime_staleness.py").exists(), (
            "a top-level module here would not ship to users"
        )

    def test_the_cli_can_import_it(self):
        """`feral` is a console script, so its sys.path is not
        feral-core. This is the import that failed the first time."""
        proc = subprocess.run(
            [sys.executable, "-c",
             "from config.staleness import runtime_staleness;"
             "import json; print(json.dumps(runtime_staleness()))"],
            cwd=str(FERAL_CORE), capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode == 0, proc.stderr[-800:]
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        assert "stale" in payload and "running_version" in payload


class TestTheDashboardCarriesIt:

    def test_the_route_helper_returns_a_dict(self):
        from api.routes.dashboard import _runtime_staleness

        result = _runtime_staleness()
        assert isinstance(result, dict)
        assert "stale" in result
