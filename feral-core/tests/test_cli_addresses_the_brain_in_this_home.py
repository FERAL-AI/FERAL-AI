"""A command pointed at a FERAL_HOME must reach the brain living there.

``brain_public_port`` read environment variables and nothing else, so
every ``feral`` command resolved to localhost:9090 regardless of which
FERAL_HOME it was given. Running a second brain was therefore
impossible to administer: the commands succeeded, against the wrong
brain, and reported success.

Measured on 2026-09-07 while bringing up two brains to test scoped
replication. ``FERAL_HOME=<B> feral sync peer scope grant <A> work``
granted the scope on A, to A itself. B's roster stayed empty, so B
refused every operation A sent, and federation looked broken while the
enforcement was working exactly as designed. Both invites went to A's
roster too, which is why the identity handshake kept failing with
``invalid_peer_grant``.

The fix is that a serving brain records its port in its own home, and
resolution consults that record between the environment and the 9090
default. Env still wins: an operator behind a proxy is describing
something this process cannot observe.
"""

from __future__ import annotations

import json

import pytest

from config import runtime


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("FERAL_PUBLIC_PORT", "FERAL_BRAIN_PORT", "FERAL_PORT",
              "FERAL_PUBLIC_BASE_URL"):
        monkeypatch.delenv(k, raising=False)


class TestRecordAndRead:
    def test_a_recorded_port_round_trips(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FERAL_HOME", str(tmp_path))
        runtime.record_runtime_endpoint(9091)
        assert runtime.running_brain_port() == 9091

    def test_nothing_recorded_reads_as_unknown(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FERAL_HOME", str(tmp_path))
        assert runtime.running_brain_port() is None

    def test_a_corrupt_record_reads_as_unknown(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FERAL_HOME", str(tmp_path))
        (tmp_path / "runtime.json").write_text("{ not json")
        assert runtime.running_brain_port() is None

    def test_a_nonsense_port_reads_as_unknown(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FERAL_HOME", str(tmp_path))
        (tmp_path / "runtime.json").write_text(json.dumps({"port": 70000}))
        assert runtime.running_brain_port() is None

    def test_recording_never_raises_on_an_unwritable_home(self, tmp_path, monkeypatch):
        """Boot must not fail because the endpoint record could not be written."""
        blocked = tmp_path / "home"
        blocked.mkdir()
        (blocked / "runtime.json").mkdir()  # a directory where the file goes
        monkeypatch.setenv("FERAL_HOME", str(blocked))
        runtime.record_runtime_endpoint(9091)  # must not raise
        assert runtime.running_brain_port() is None


class TestResolution:
    def test_the_home_is_consulted_before_the_9090_default(self, tmp_path, monkeypatch):
        """The reported bug, reduced to one assertion."""
        monkeypatch.setenv("FERAL_HOME", str(tmp_path))
        runtime.record_runtime_endpoint(9091)
        assert runtime.brain_public_port() == 9091, (
            "a command pointed at this home addressed some other brain"
        )

    def test_env_still_wins_over_the_record(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FERAL_HOME", str(tmp_path))
        runtime.record_runtime_endpoint(9091)
        monkeypatch.setenv("FERAL_PUBLIC_PORT", "8443")
        assert runtime.brain_public_port() == 8443

    def test_an_unserved_home_still_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FERAL_HOME", str(tmp_path))
        assert runtime.brain_public_port() == 9090

    def test_two_homes_resolve_to_two_brains(self, tmp_path, monkeypatch):
        """The case the whole change exists for."""
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        monkeypatch.setenv("FERAL_HOME", str(a))
        runtime.record_runtime_endpoint(9090)
        monkeypatch.setenv("FERAL_HOME", str(b))
        runtime.record_runtime_endpoint(9091)

        monkeypatch.setenv("FERAL_HOME", str(a))
        assert runtime.brain_public_port() == 9090
        monkeypatch.setenv("FERAL_HOME", str(b))
        assert runtime.brain_public_port() == 9091

    def test_the_base_url_follows(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FERAL_HOME", str(tmp_path))
        runtime.record_runtime_endpoint(9091)
        assert runtime.brain_public_base_url().endswith(":9091")


def test_both_serve_entrypoints_record_their_port():
    """Neither entrypoint may serve without leaving the record behind."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    server = (root / "api" / "server.py").read_text()
    assert "record_runtime_endpoint(" in server, "api.server serves without recording"

    cli = (root / "cli" / "main.py").read_text()
    assert "record_runtime_endpoint(" in cli, "feral serve serves without recording"
