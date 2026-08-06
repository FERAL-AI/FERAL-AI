"""A timeout must not swallow what Tailscale said.

A user installed Tailscale, started it, logged in, and ran
`feral access remote-up`. He got:

    tailscale funnel --bg 9090 timed out after 20.0s

and nothing else. Two defects produced that, and neither was "Tailscale
is missing", which is what everyone assumed for a week:

1. ``subprocess.run`` inherited stdin. ``tailscale funnel`` prompts for
   confirmation when Funnel is not yet enabled on the tailnet: it prints
   an enable URL and waits. Called from a daemon or an API request there
   is no one to answer, so it blocked until the timeout.
2. ``subprocess.TimeoutExpired`` carries the output captured before the
   timeout. The handler discarded it, so the enable URL, the actual
   answer, was read from the pipe and thrown away.

These tests pin both, because the failure mode is silent: everything
"works", the operator just learns nothing.
"""

from __future__ import annotations

import subprocess

import pytest

from integrations import tailscale


@pytest.fixture(autouse=True)
def _tailscale_looks_installed(monkeypatch):
    monkeypatch.setattr(tailscale.shutil, "which", lambda name: "/usr/bin/tailscale")


class TestStdinIsClosed:
    def test_run_never_inherits_stdin(self, monkeypatch):
        """An inherited stdin is what let the CLI block forever."""
        seen = {}

        def _fake_run(cmd, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        tailscale._run(["status"])
        assert seen.get("stdin") is subprocess.DEVNULL

    def test_a_prompting_command_cannot_hang_on_input(self, monkeypatch):
        """With stdin closed the CLI reads EOF and exits, so a prompt
        becomes a fast error instead of a 20 second stall."""
        def _fake_run(cmd, **kwargs):
            assert kwargs.get("stdin") is subprocess.DEVNULL
            return subprocess.CompletedProcess(
                cmd, 1, "", "Funnel is not enabled on your tailnet."
            )

        monkeypatch.setattr(subprocess, "run", _fake_run)
        proc = tailscale._run(["funnel", "--bg", "9090"])
        assert proc.returncode == 1
        assert "Funnel is not enabled" in proc.stderr


class TestTimeoutOutputSurvives:
    @staticmethod
    def _timeout_with(stdout=None, stderr=None):
        def _fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(
                cmd, 20.0, output=stdout, stderr=stderr
            )
        return _fake_run

    def test_partial_stderr_reaches_the_operator(self, monkeypatch):
        """The exact regression: the answer was in the exception."""
        monkeypatch.setattr(
            subprocess, "run",
            self._timeout_with(stderr=(
                "Funnel is not enabled on your tailnet.\n"
                "To enable, visit:\n"
                "  https://login.tailscale.com/f/funnel?node=abc123\n"
            )),
        )
        with pytest.raises(tailscale.TailscaleError) as exc:
            tailscale._run(["funnel", "--bg", "9090"], timeout=20.0)
        message = str(exc.value)
        assert "login.tailscale.com/f/funnel" in message, message

    def test_partial_stdout_is_used_when_stderr_is_empty(self, monkeypatch):
        """The CLI prints the enable URL on stdout in some versions."""
        monkeypatch.setattr(
            subprocess, "run",
            self._timeout_with(stdout="visit https://login.tailscale.com/f/funnel?node=x"),
        )
        with pytest.raises(tailscale.TailscaleError) as exc:
            tailscale._run(["funnel", "--bg", "9090"], timeout=20.0)
        assert "login.tailscale.com" in str(exc.value)

    def test_bytes_output_does_not_crash_the_handler(self, monkeypatch):
        """text=True normally gives str, but do not trust it and raise a
        UnicodeDecodeError on top of the real failure."""
        monkeypatch.setattr(
            subprocess, "run",
            self._timeout_with(stderr=b"Funnel is not enabled \xff\xfe"),
        )
        with pytest.raises(tailscale.TailscaleError) as exc:
            tailscale._run(["funnel", "--bg", "9090"], timeout=20.0)
        assert "Funnel is not enabled" in str(exc.value)

    def test_a_silent_timeout_says_it_was_waiting_on_input(self, monkeypatch):
        """No output at all is itself diagnostic, and the old message
        gave the operator nowhere to go."""
        monkeypatch.setattr(subprocess, "run", self._timeout_with())
        with pytest.raises(tailscale.TailscaleError) as exc:
            tailscale._run(["funnel", "--bg", "9090"], timeout=20.0)
        message = str(exc.value)
        assert "timed out" in message
        assert "waiting on input" in message

    def test_a_classifiable_timeout_raises_the_typed_error(self, monkeypatch):
        """A timeout carrying a known message must produce the same typed
        error a non-zero exit would, so callers get one code path and the
        remediation they already wrote."""
        monkeypatch.setattr(
            subprocess, "run",
            self._timeout_with(stderr="Logged out."),
        )
        with pytest.raises(tailscale.TailscaleError) as exc:
            tailscale._run(["status"], timeout=5.0)
        # Whatever _classify_stderr maps "Logged out." to, it must not be
        # the generic subprocess failure.
        assert type(exc.value) is not tailscale.TailscaleSubprocessFailure


def test_timeout_seconds_are_still_reported(monkeypatch):
    """Keep the duration: it distinguishes a hang from a fast refusal."""
    def _fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 20.0, output="", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    with pytest.raises(tailscale.TailscaleError) as exc:
        tailscale._run(["funnel", "--bg", "9090"], timeout=20.0)
    assert "20.0s" in str(exc.value)
