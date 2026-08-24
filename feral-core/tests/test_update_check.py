"""The update-availability check: off by default, cached, and silent when it fails.

`config/staleness.py` answers "am I running what is installed", locally.
`config/update_check.py` answers "does something newer exist", which
needs the network, and every property that makes that acceptable on a
local-first product is pinned here:

* it is OFF unless the operator turns it on,
* the dashboard path never opens a socket,
* every network failure ends as "unknown" rather than an error,
* the cache means one answer a day, not one a poll,
* and the comparison is a version comparison, not a string comparison,
  because `"2026.8.9" > "2026.8.10"` is True as strings and would keep
  the check silent for the first nine days of every release month.

Nothing here talks to pypi.org. The end-to-end failure tests point the
checker at 127.0.0.1, either at a closed port or at a local server that
answers with garbage, so they exercise the real urllib path with real
errors and no internet.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import config.update_check as uc


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Never touch the operator's real ~/.feral.

    Every test in this module reads or writes the cache file, which
    lives under FERAL_HOME. Pointing it at tmp_path also guarantees a
    cold cache per test, which several of these assertions depend on.
    """
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    monkeypatch.delenv("FERAL_UPDATE_CHECK", raising=False)
    monkeypatch.delenv("FERAL_UPDATE_CHECK_TTL_HOURS", raising=False)
    monkeypatch.delenv("FERAL_PYPI_JSON_URL", raising=False)
    return tmp_path


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("FERAL_UPDATE_CHECK", "1")


# ---------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------


class TestVersionComparison:
    """Calver is where string comparison goes wrong, and it goes wrong
    quietly: it does not crash, it just stops reporting updates."""

    def test_the_calver_case_string_comparison_gets_wrong(self):
        # The whole reason this is not `latest > current`.
        assert "2026.8.9" > "2026.8.10"          # as strings, wrong
        assert uc.compare_versions("2026.8.9", "2026.8.10") == -1

    @pytest.mark.parametrize("current, latest, expected", [
        ("2026.8.8", "2026.8.25", -1),
        ("2026.8.25", "2026.8.25", 0),
        ("2026.8.26", "2026.8.25", 1),
        ("2026.8.9", "2026.9.1", -1),
        ("2025.12.31", "2026.1.1", -1),
        ("2026.10.1", "2026.9.30", 1),
        ("2026.8.2", "2026.8.10", -1),
    ])
    def test_orders_calver_numerically(self, current, latest, expected):
        assert uc.compare_versions(current, latest) == expected

    def test_a_prerelease_sorts_below_its_release(self):
        """Only meaningful where `packaging` is importable, which is the
        case in this environment and in CI."""
        pytest.importorskip("packaging.version")
        assert uc.compare_versions("2026.9.1rc1", "2026.9.1") == -1
        assert uc.compare_versions("2026.9.1", "2026.9.1rc1") == 1
        assert uc.is_prerelease("2026.9.1rc1") is True
        assert uc.is_prerelease("2026.9.1") is False

    def test_unparseable_input_is_unknown_never_a_guess(self):
        assert uc.compare_versions("not-a-version", "2026.8.25") is None
        assert uc.compare_versions("2026.8.25", "") is None
        assert uc.compare_versions("2026.8.25", None) is None  # type: ignore[arg-type]

    def test_the_fallback_parser_gets_calver_right_without_packaging(self, monkeypatch):
        """`packaging` is not a declared dependency of feral-ai, so a
        minimal install can lack it. The fallback must still order
        calver correctly, and must refuse rather than guess on anything
        it does not understand."""
        import builtins

        real_import = builtins.__import__

        def _no_packaging(name, *args, **kwargs):
            if name.startswith("packaging"):
                raise ImportError("simulated: packaging is not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_packaging)

        assert uc.parse_version("2026.8.9") == (2026, 8, 9)
        assert uc.compare_versions("2026.8.9", "2026.8.10") == -1
        assert uc.compare_versions("2026.8.25", "2026.8.25") == 0
        # Anything with a pre/post/dev segment is refused, not ordered.
        assert uc.parse_version("2026.9.1rc1") is None
        assert uc.compare_versions("2026.9.1rc1", "2026.9.1") is None


# ---------------------------------------------------------------------
# Off by default
# ---------------------------------------------------------------------


class TestItIsOffUntilAsked:

    def test_disabled_by_default(self):
        assert uc.update_check_enabled() is False

    def test_the_env_var_turns_it_on(self, monkeypatch):
        monkeypatch.setenv("FERAL_UPDATE_CHECK", "1")
        assert uc.update_check_enabled() is True
        monkeypatch.setenv("FERAL_UPDATE_CHECK", "false")
        assert uc.update_check_enabled() is False

    def test_the_settings_key_turns_it_on(self, isolated_home, monkeypatch):
        (isolated_home / "settings.json").write_text(
            json.dumps({"updates": {"check_pypi": True}})
        )
        assert uc.update_check_enabled() is True

    def test_env_beats_settings(self, isolated_home, monkeypatch):
        (isolated_home / "settings.json").write_text(
            json.dumps({"updates": {"check_pypi": True}})
        )
        monkeypatch.setenv("FERAL_UPDATE_CHECK", "0")
        assert uc.update_check_enabled() is False

    def test_disabled_never_fetches(self, monkeypatch):
        """The default must not merely report "off", it must not ask."""
        calls = []

        def _spy(*args, **kwargs):
            calls.append(args)
            return ("2026.99.99", "")

        monkeypatch.setattr(uc, "fetch_latest", _spy)
        entry = uc.refresh()
        assert calls == []
        assert entry["disabled"] is True
        assert entry["latest"] is None

    def test_disabled_reports_disabled_not_unknown(self):
        """"Unknown" sends the operator to debug their network.
        "Disabled" tells them the truth: nobody asked."""
        status = uc.update_status()
        assert status["enabled"] is False
        assert status["status"] == "disabled"
        assert "FERAL_UPDATE_CHECK" in status["detail"]


# ---------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------


class TestTheCache:

    def test_a_fresh_answer_is_served_without_a_second_call(self, enabled, monkeypatch):
        """The point of the cache: releases are not hourly."""
        calls = []

        def _spy(timeout=uc.DEFAULT_TIMEOUT_S):
            calls.append(time.time())
            return ("2026.8.25", "")

        monkeypatch.setattr(uc, "fetch_latest", _spy)

        first = uc.refresh()
        second = uc.refresh()
        third = uc.refresh()

        assert len(calls) == 1, f"cache did not prevent repeat calls: {len(calls)} fetches"
        assert first["latest"] == second["latest"] == third["latest"] == "2026.8.25"

    def test_an_expired_answer_is_refetched(self, enabled, monkeypatch):
        calls = []

        def _spy(timeout=uc.DEFAULT_TIMEOUT_S):
            calls.append(1)
            return ("2026.8.25", "")

        monkeypatch.setattr(uc, "fetch_latest", _spy)
        uc.refresh()
        assert len(calls) == 1

        # Age the entry past the TTL by rewriting its timestamp.
        entry = uc.read_cache()
        entry["checked_at"] = time.time() - (uc.ttl_seconds() + 60)
        uc.write_cache(entry)

        uc.refresh()
        assert len(calls) == 2

    def test_a_failure_is_cached_briefly_not_for_a_day(self, enabled, monkeypatch):
        """A transient outage must not blind the check until tomorrow,
        and must not turn into a retry loop either."""
        monkeypatch.setattr(uc, "fetch_latest", lambda timeout=None: (None, "boom"))
        uc.refresh()
        entry = uc.read_cache()
        assert entry["ok"] is False

        # Just inside the failure TTL: still cached.
        entry["checked_at"] = time.time() - (uc.FAILURE_TTL_SECONDS - 30)
        uc.write_cache(entry)
        assert uc._cache_is_fresh(uc.read_cache()) is True

        # Past it: eligible for another try, long before the 24h
        # success TTL would have expired.
        entry["checked_at"] = time.time() - (uc.FAILURE_TTL_SECONDS + 30)
        uc.write_cache(entry)
        assert uc._cache_is_fresh(uc.read_cache()) is False
        assert uc.FAILURE_TTL_SECONDS < uc.ttl_seconds()

    def test_force_bypasses_both_the_gate_and_the_ttl(self, monkeypatch):
        """`feral update` is an explicit operator action: typing the
        command IS the consent."""
        calls = []

        def _spy(timeout=uc.DEFAULT_TIMEOUT_S):
            calls.append(1)
            return ("2026.8.25", "")

        monkeypatch.setattr(uc, "fetch_latest", _spy)
        assert uc.update_check_enabled() is False
        uc.refresh(force=True)
        uc.refresh(force=True)
        assert len(calls) == 2

    def test_a_corrupt_cache_file_is_survived(self, enabled, isolated_home):
        uc.cache_path().write_text("{this is not json")
        assert uc.read_cache() is None
        status = uc.update_status()
        assert status["status"] == "unknown"

    def test_a_clock_that_jumped_backwards_does_not_freeze_the_cache(self, enabled):
        uc.write_cache({"ok": True, "latest": "2026.8.25",
                        "checked_at": time.time() + 86_400})
        assert uc._cache_is_fresh(uc.read_cache()) is False


# ---------------------------------------------------------------------
# Failing safe
# ---------------------------------------------------------------------


class _GarbageHandler(BaseHTTPRequestHandler):
    """Answers every request with something that is not the JSON we want."""

    body = b"<html><body>Sign in to the guest network</body></html>"
    status = 200

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
        self.send_response(self.status)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        return


class _ServerErrorHandler(_GarbageHandler):
    body = b"service unavailable"
    status = 503


class _SlowHandler(_GarbageHandler):
    def do_GET(self):  # noqa: N802
        time.sleep(5)


class _LocalServer:
    """A tiny HTTP server on 127.0.0.1. No internet involved.

    Threading, not the single-threaded HTTPServer: the slow handler
    below sleeps deliberately, and a single-threaded server would make
    `shutdown()` wait for it, turning a timeout test into a stalled
    suite.
    """

    def __init__(self, handler):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}/pypi/feral-ai/json"

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def _closed_port() -> int:
    """A port nothing is listening on: bind, read, release."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestItFailsSafe:
    """Every one of these is a real urllib call that really fails."""

    def test_no_listener_reports_unknown(self, enabled, monkeypatch):
        monkeypatch.setenv(
            "FERAL_PYPI_JSON_URL",
            f"http://127.0.0.1:{_closed_port()}/pypi/feral-ai/json",
        )
        latest, error = uc.fetch_latest(timeout=2.0)
        assert latest is None
        assert error
        status = _status_after_refresh()
        assert status["status"] == "unknown"

    def test_dns_failure_reports_unknown(self, enabled, monkeypatch):
        monkeypatch.setenv(
            "FERAL_PYPI_JSON_URL",
            "http://this-host-does-not-exist.invalid/pypi/feral-ai/json",
        )
        latest, error = uc.fetch_latest(timeout=3.0)
        assert latest is None
        assert error
        assert _status_after_refresh()["status"] == "unknown"

    def test_a_timeout_reports_unknown_and_returns_promptly(self, enabled, monkeypatch):
        with _LocalServer(_SlowHandler) as url:
            monkeypatch.setenv("FERAL_PYPI_JSON_URL", url)
            started = time.time()
            latest, error = uc.fetch_latest(timeout=0.5)
            elapsed = time.time() - started
        assert latest is None
        assert error
        assert elapsed < 10, f"a 0.5s timeout took {elapsed:.1f}s"
        assert _status_after_refresh(timeout=0.5)["status"] == "unknown"

    def test_a_503_reports_unknown(self, enabled, monkeypatch):
        with _LocalServer(_ServerErrorHandler) as url:
            monkeypatch.setenv("FERAL_PYPI_JSON_URL", url)
            latest, error = uc.fetch_latest(timeout=5.0)
            assert latest is None
            assert "503" in error or "HTTP" in error
            assert _status_after_refresh()["status"] == "unknown"

    def test_a_malformed_body_reports_unknown(self, enabled, monkeypatch):
        """A captive portal answering 200 with HTML is the realistic
        version of this, and it must not raise."""
        with _LocalServer(_GarbageHandler) as url:
            monkeypatch.setenv("FERAL_PYPI_JSON_URL", url)
            latest, error = uc.fetch_latest(timeout=5.0)
            assert latest is None
            assert error
            assert _status_after_refresh()["status"] == "unknown"

    def test_valid_json_without_a_version_reports_unknown(self, enabled, monkeypatch):
        class _EmptyJSON(_GarbageHandler):
            body = json.dumps({"info": {}, "releases": {}}).encode()

        with _LocalServer(_EmptyJSON) as url:
            monkeypatch.setenv("FERAL_PYPI_JSON_URL", url)
            latest, error = uc.fetch_latest(timeout=5.0)
        assert latest is None
        assert error

    def test_a_real_index_answer_is_parsed(self, enabled, monkeypatch):
        """The happy path, served from 127.0.0.1 in the same shape PyPI
        uses: newest wins, a prerelease does not, and a fully yanked
        release does not."""
        payload = {
            "info": {"version": "2026.9.1rc1"},
            "releases": {
                "2026.8.9": [{"filename": "a", "yanked": False}],
                "2026.8.10": [{"filename": "b", "yanked": False}],
                "2026.8.11": [{"filename": "c", "yanked": True}],
                "2026.9.1rc1": [{"filename": "d", "yanked": False}],
            },
        }

        class _PyPILike(_GarbageHandler):
            body = json.dumps(payload).encode()

        with _LocalServer(_PyPILike) as url:
            monkeypatch.setenv("FERAL_PYPI_JSON_URL", url)
            latest, error = uc.fetch_latest(timeout=5.0)

        assert error == ""
        assert latest == "2026.8.10", (
            "expected the newest non-yanked stable release: 2026.8.11 is "
            "yanked and 2026.9.1rc1 is a prerelease"
        )

    def test_an_oversized_body_is_refused_rather_than_read(self, enabled, monkeypatch):
        class _Huge(_GarbageHandler):
            body = b"x" * (uc.MAX_RESPONSE_BYTES + 4096)

        with _LocalServer(_Huge) as url:
            monkeypatch.setenv("FERAL_PYPI_JSON_URL", url)
            latest, error = uc.fetch_latest(timeout=10.0)
        assert latest is None
        assert "too large" in error

    def test_a_non_http_index_url_is_refused(self, enabled, monkeypatch):
        """`urlopen` also speaks file://, and this URL comes from an env
        var. A version check must not become a local file read."""
        monkeypatch.setenv("FERAL_PYPI_JSON_URL", "file:///etc/passwd")
        latest, error = uc.fetch_latest(timeout=1.0)
        assert latest is None
        assert "http(s)" in error

    def test_update_status_never_raises(self, enabled, monkeypatch):
        """It is rendered on /api/dashboard, which the whole shell
        polls. A diagnostic must never cost the page."""
        def _boom():
            raise RuntimeError("cache subsystem exploded")

        monkeypatch.setattr(uc, "read_cache", _boom)
        status = uc.update_status()
        assert status["status"] == "unknown"
        assert status["detail"] == "unavailable"


def _status_after_refresh(timeout: float = 3.0) -> dict:
    uc.refresh(timeout=timeout)
    return uc.update_status()


# ---------------------------------------------------------------------
# What the operator is told
# ---------------------------------------------------------------------


class TestTheReportedStatus:

    def test_an_available_update_says_what_to_run(self, enabled, monkeypatch):
        monkeypatch.setattr(uc, "_current_version", lambda: "2026.8.8")
        uc.write_cache({"ok": True, "latest": "2026.8.25", "checked_at": time.time()})
        status = uc.update_status()
        assert status["status"] == "update-available"
        assert status["update_available"] is True
        assert status["latest_version"] == "2026.8.25"
        assert "feral update" in status["detail"]

    def test_being_current_is_not_reported_as_an_update(self, enabled, monkeypatch):
        monkeypatch.setattr(uc, "_current_version", lambda: "2026.8.25")
        uc.write_cache({"ok": True, "latest": "2026.8.25", "checked_at": time.time()})
        status = uc.update_status()
        assert status["status"] == "current"
        assert status["update_available"] is False

    def test_a_local_build_ahead_of_the_index_is_not_an_update(self, enabled, monkeypatch):
        monkeypatch.setattr(uc, "_current_version", lambda: "2026.9.1")
        uc.write_cache({"ok": True, "latest": "2026.8.25", "checked_at": time.time()})
        assert uc.update_status()["status"] == "current"

    def test_no_check_yet_is_unknown_not_current(self, enabled):
        status = uc.update_status()
        assert status["status"] == "unknown"
        assert status["update_available"] is None

    def test_it_compares_the_installed_version_not_the_running_one(self, enabled, monkeypatch):
        """An operator who upgraded but has not restarted has the new
        version on disk. Telling them an update is available would send
        them to re-run an upgrade they already did; what they need is
        the staleness row, which is a different question."""
        import config.staleness as staleness

        monkeypatch.setattr(staleness, "RUNNING_VERSION", "2026.8.8")
        monkeypatch.setattr(staleness, "installed_version", lambda: "2026.8.25")
        uc.write_cache({"ok": True, "latest": "2026.8.25", "checked_at": time.time()})
        status = uc.update_status()
        assert status["current_version"] == "2026.8.25"
        assert status["status"] == "current"


# ---------------------------------------------------------------------
# The dashboard surface
# ---------------------------------------------------------------------


class TestTheDashboardCarriesIt:

    def test_the_route_helper_returns_a_dict(self):
        from api.routes.dashboard import _update_availability

        result = _update_availability()
        assert isinstance(result, dict)
        assert "status" in result

    def test_the_route_helper_opens_no_socket(self, enabled, monkeypatch):
        """The hard requirement. /api/dashboard is polled by the entire
        shell and must not degrade because pypi.org is slow, so the
        request path does not talk to pypi.org at all."""
        from api.routes.dashboard import _update_availability

        attempts = []
        real_connect = socket.socket.connect

        def _refused(self, addr, *args, **kwargs):
            attempts.append(addr)
            raise AssertionError(f"the dashboard path tried to connect to {addr!r}")

        monkeypatch.setattr(socket.socket, "connect", _refused, raising=True)
        try:
            result = _update_availability()
        finally:
            monkeypatch.setattr(socket.socket, "connect", real_connect, raising=True)

        assert attempts == []
        assert isinstance(result, dict)

    def test_the_field_still_answers_when_the_network_is_gone(self, enabled, monkeypatch):
        """No network, and the dashboard field is still a dict that says
        unknown. This is the property the whole design turns on."""
        from api.routes.dashboard import _update_availability

        monkeypatch.setenv(
            "FERAL_PYPI_JSON_URL",
            f"http://127.0.0.1:{_closed_port()}/pypi/feral-ai/json",
        )
        uc.refresh(timeout=2.0)  # a real, really-failing fetch
        result = _update_availability()
        assert result["status"] == "unknown"
        assert result["enabled"] is True
        assert result["detail"], "unknown must still explain itself"

    def test_the_brain_only_schedules_the_refresher_when_asked(self):
        """Pinned at the source, the way `test_untrusted_transport` pins
        uvicorn's proxy kwargs: booting a brain to observe which
        background tasks exist is a far heavier test than the property
        deserves, and the property is a one-line gate.

        Three things matter and all three are here: the task is created
        only inside the `enabled` branch (an off install schedules
        nothing at all), the fetch goes through `asyncio.to_thread`
        because a blocking urllib GET inside `async def` would stall the
        event loop, and the loop sleeps before its first check so the
        network is never on the boot path.
        """
        import inspect

        import api.server as server

        src = inspect.getsource(server.startup)
        assert "_update_check_enabled()" in src
        assert "feral-update-check-refresher" in src

        gate = src.index("if _update_check_enabled():")
        creation = src.index("feral-update-check-refresher")
        assert gate < creation, "the task must be created inside the gate"

        body = src[gate:creation]
        assert "asyncio.to_thread" in body, "a blocking GET would stall the loop"
        assert "asyncio.sleep(120)" in body, "the first check must be off the boot path"

    def test_the_helper_survives_the_module_being_broken(self, monkeypatch):
        from api.routes import dashboard as dash
        import builtins

        real_import = builtins.__import__

        def _broken(name, *args, **kwargs):
            if name == "config.update_check":
                raise RuntimeError("simulated import failure")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _broken)
        result = dash._update_availability()
        assert result["status"] == "unknown"
        assert result["detail"] == "unavailable"
