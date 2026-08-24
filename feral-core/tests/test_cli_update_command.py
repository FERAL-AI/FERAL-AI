"""`feral update` upgrades the environment that is actually running.

The failure this command exists for is not exotic. An operator had two
installs, a pyenv one and a venv one, ran `pip install --upgrade
feral-ai`, and upgraded the one that was not running. Nothing errored.
They served stale code for two days believing they were current.

So the decision logic is the product here, and it is what this file
tests: which interpreter, what kind of install, whether there is
anything to do at all, and whether the restart that makes an upgrade
mean anything actually happens.

NOTHING HERE RUNS PIP. `cmd_update` takes a ``runner`` seam and every
test passes a stub that records the argv it was handed. A test suite
does not get to mutate the interpreter it is running on, so the one
thing that cannot be proven here is that a real
`pip install --upgrade` succeeds; what is proven is exactly which
command would be run, in which environment, and under what conditions
it is not run at all.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from cli import update_command as uco


class _Runner:
    """Stands in for subprocess.run and records what it was asked to do."""

    def __init__(self, returncode: int = 0):
        self.calls: list[list[str]] = []
        self.returncode = returncode

    def __call__(self, command, *args, **kwargs):
        self.calls.append(list(command))
        return subprocess.CompletedProcess(command, self.returncode)


@pytest.fixture
def wheel_install(monkeypatch):
    """Pretend this process is a plain wheel install of 2026.8.8."""
    monkeypatch.setattr(uco, "install_kind", lambda: {
        "editable": False,
        "from_local_dir": False,
        "source": "",
        "version": "2026.8.8",
        "location": "/opt/env/lib/python3.11/site-packages",
        "detail": "wheel install",
    })


@pytest.fixture
def update_available(monkeypatch):
    monkeypatch.setattr(uco, "_availability", lambda current: {
        "status": "update-available",
        "latest_version": "2026.8.25",
        "update_available": True,
        "detail": "",
    })


@pytest.fixture
def brain_matches(monkeypatch):
    monkeypatch.setattr(uco, "running_brain_interpreter", lambda: {
        "status": "match", "pid": 4242, "executable": sys.executable,
        "detail": "pid 4242 runs this interpreter",
    })


@pytest.fixture
def restart_spy(monkeypatch):
    calls = []
    import cli.main as cli_main

    monkeypatch.setattr(cli_main, "cmd_restart", lambda: calls.append(1))
    return calls


# ---------------------------------------------------------------------
# Which environment
# ---------------------------------------------------------------------


class TestItAimsAtTheRunningInterpreter:

    def test_the_command_is_this_interpreters_pip(self):
        """`sys.executable -m pip`, never a bare `pip`. A bare pip is
        whichever one PATH resolves first, which is precisely how the
        reported two-day outage happened."""
        command = uco.pip_upgrade_command()
        assert command[0] == sys.executable
        assert command[1:3] == ["-m", "pip"]
        assert command[-1] == "feral-ai"
        assert "--upgrade" in command

    def test_it_prints_the_environment_before_touching_it(
        self, capsys, wheel_install, update_available, brain_matches, restart_spy,
    ):
        runner = _Runner()
        uco.cmd_update(runner=runner)
        out = capsys.readouterr().out
        assert sys.executable in out
        # The environment is named before the upgrade command is shown,
        # because the operator is the one who knows whether it is right.
        assert out.index(sys.executable) < out.index("Upgrading with:")

    def test_our_own_pid_reports_our_own_interpreter(self):
        """The mechanism the mismatch check rests on, against a real
        process: this one."""
        exe = uco.brain_process_executable(os.getpid())
        assert exe, "could not read this process's own interpreter"
        assert os.path.realpath(exe) == os.path.realpath(sys.executable)

    def test_a_dead_pid_reports_nothing_rather_than_raising(self):
        # 2^31-ish: no such process on any machine this runs on.
        assert uco.brain_process_executable(2_147_480_000) is None

    def test_a_brain_on_another_interpreter_is_refused_not_ignored(
        self, capsys, wheel_install, update_available, monkeypatch, restart_spy,
    ):
        """The reported failure, caught. Upgrading this environment
        would leave the running brain exactly as stale as it started."""
        monkeypatch.setattr(uco, "running_brain_interpreter", lambda: {
            "status": "mismatch",
            "pid": 991,
            "executable": "/Users/op/.pyenv/versions/3.11.9/bin/python",
            "detail": "different interpreter",
        })
        runner = _Runner()
        code = uco.cmd_update(runner=runner)
        out = capsys.readouterr().out

        assert code == 1
        assert runner.calls == [], "it upgraded the wrong environment anyway"
        assert restart_spy == []
        assert "/Users/op/.pyenv/versions/3.11.9/bin/python" in out
        assert sys.executable in out
        # It must hand over the command that WOULD work.
        assert "-m pip install --upgrade feral-ai" in out

    def test_an_unreadable_brain_interpreter_is_noted_and_does_not_block(
        self, capsys, wheel_install, update_available, monkeypatch, restart_spy,
    ):
        monkeypatch.setattr(uco, "running_brain_interpreter", lambda: {
            "status": "unknown", "pid": 991, "executable": None,
            "detail": "could not read the interpreter of pid 991",
        })
        runner = _Runner()
        code = uco.cmd_update(runner=runner)
        out = capsys.readouterr().out
        assert code == 0
        assert len(runner.calls) == 1
        assert "could not read the interpreter of pid 991" in out


# ---------------------------------------------------------------------
# What kind of install
# ---------------------------------------------------------------------


class TestEditableInstalls:

    def test_an_editable_install_is_refused_with_the_right_remedy(
        self, capsys, monkeypatch, restart_spy,
    ):
        monkeypatch.setattr(uco, "install_kind", lambda: {
            "editable": True,
            "from_local_dir": True,
            "source": "file:///home/dev/feral/feral-core",
            "version": "2026.8.8",
            "location": "/home/dev/feral/feral-core",
            "detail": "editable install",
        })
        runner = _Runner()
        code = uco.cmd_update(runner=runner)
        out = capsys.readouterr().out

        assert code == 1
        assert runner.calls == [], "pip must not be run against a checkout"
        assert restart_spy == []
        assert "git pull" in out
        assert "editable" in out.lower()

    def test_the_detector_agrees_with_the_dist_metadata(self):
        """Cross-checked against ``direct_url.json`` read independently,
        so this holds whether the suite runs on an editable checkout
        (which is how CI installs) or on a wheel."""
        import importlib.metadata as md

        dist = md.distribution("feral-ai")
        raw = dist.read_text("direct_url.json")
        expected = False
        if raw:
            payload = json.loads(raw)
            expected = bool((payload.get("dir_info") or {}).get("editable"))

        kind = uco.install_kind()
        assert kind["editable"] is expected
        assert kind["version"] == dist.version


# ---------------------------------------------------------------------
# Is there anything to do
# ---------------------------------------------------------------------


class TestAlreadyCurrent:

    def test_it_does_nothing_and_says_so(
        self, capsys, wheel_install, monkeypatch, restart_spy,
    ):
        """Safe to run when there is nothing to do: no pip, no restart,
        exit 0."""
        monkeypatch.setattr(uco, "_availability", lambda current: {
            "status": "current",
            "latest_version": "2026.8.8",
            "update_available": False,
            "detail": "",
        })
        runner = _Runner()
        code = uco.cmd_update(runner=runner)
        out = capsys.readouterr().out

        assert code == 0
        assert runner.calls == []
        assert restart_spy == []
        assert "Already on the newest release" in out

    def test_being_current_still_mentions_an_unrestarted_upgrade(
        self, capsys, wheel_install, monkeypatch, restart_spy,
    ):
        """The one case where "you are current" is misleading: the new
        version is on disk and the live process is still the old one."""
        monkeypatch.setattr(uco, "_availability", lambda current: {
            "status": "current", "latest_version": "2026.8.8",
            "update_available": False, "detail": "",
        })
        import config.staleness as staleness

        monkeypatch.setattr(staleness, "RUNNING_VERSION", "2026.8.1")
        monkeypatch.setattr(staleness, "installed_version", lambda: "2026.8.8")

        uco.cmd_update(runner=_Runner())
        out = capsys.readouterr().out
        assert "feral restart" in out


class TestTheIndexIsUnreachable:

    def test_it_refuses_rather_than_guessing(
        self, capsys, wheel_install, monkeypatch, restart_spy,
    ):
        monkeypatch.setattr(uco, "_availability", lambda current: {
            "status": "unknown",
            "latest_version": None,
            "update_available": None,
            "detail": "URLError: [Errno 8] nodename nor servname provided",
        })
        runner = _Runner()
        code = uco.cmd_update(runner=runner)
        out = capsys.readouterr().out

        assert code == 1
        assert runner.calls == []
        assert restart_spy == []
        assert "Nothing was changed" in out

    def test_availability_reports_unknown_when_the_fetch_fails(self, monkeypatch, tmp_path):
        """Through the real `_availability`, with only the socket work
        stubbed out."""
        monkeypatch.setenv("FERAL_HOME", str(tmp_path))
        import config.update_check as uc

        monkeypatch.setattr(uc, "fetch_latest", lambda timeout=None: (None, "no route to host"))
        result = uco._availability("2026.8.8")
        assert result["status"] == "unknown"
        assert result["update_available"] is None

    def test_availability_compares_properly_through_the_real_path(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FERAL_HOME", str(tmp_path))
        import config.update_check as uc

        monkeypatch.setattr(uc, "fetch_latest", lambda timeout=None: ("2026.8.10", ""))
        # The calver trap: "2026.8.9" sorts ABOVE "2026.8.10" as a string.
        assert uco._availability("2026.8.9")["update_available"] is True
        assert uco._availability("2026.8.10")["update_available"] is False
        assert uco._availability("2026.8.11")["update_available"] is False

    def test_availability_forces_the_check_past_the_opt_in_gate(self, monkeypatch, tmp_path):
        """The setting governs the brain checking on its own. Typing
        `feral update` is the operator asking, which is consent."""
        monkeypatch.setenv("FERAL_HOME", str(tmp_path))
        monkeypatch.delenv("FERAL_UPDATE_CHECK", raising=False)
        import config.update_check as uc

        assert uc.update_check_enabled() is False
        calls = []

        def _spy(timeout=None):
            calls.append(1)
            return ("2026.8.25", "")

        monkeypatch.setattr(uc, "fetch_latest", _spy)
        result = uco._availability("2026.8.8")
        assert calls == [1]
        assert result["update_available"] is True


# ---------------------------------------------------------------------
# The upgrade, and the restart that makes it mean something
# ---------------------------------------------------------------------


class TestTheUpgrade:

    def test_it_runs_pip_then_restarts(
        self, capsys, wheel_install, update_available, brain_matches, restart_spy,
    ):
        runner = _Runner()
        code = uco.cmd_update(runner=runner)

        assert code == 0
        assert runner.calls == [uco.pip_upgrade_command()]
        assert restart_spy == [1], "an upgrade without a restart changes nothing"

    def test_check_only_changes_nothing(
        self, capsys, wheel_install, update_available, brain_matches, restart_spy,
    ):
        runner = _Runner()
        code = uco.cmd_update(check_only=True, runner=runner)
        out = capsys.readouterr().out

        assert code == 0
        assert runner.calls == []
        assert restart_spy == []
        assert "2026.8.8 -> 2026.8.25" in out

    def test_no_restart_says_the_brain_is_still_on_the_old_code(
        self, capsys, wheel_install, update_available, brain_matches, restart_spy,
    ):
        runner = _Runner()
        code = uco.cmd_update(restart=False, runner=runner)
        out = capsys.readouterr().out

        assert code == 0
        assert len(runner.calls) == 1
        assert restart_spy == []
        assert "feral restart" in out

    def test_a_failed_pip_does_not_restart(
        self, capsys, wheel_install, update_available, brain_matches, restart_spy,
    ):
        """Restarting after a failed upgrade would take the brain down
        to load the same code it already had."""
        runner = _Runner(returncode=1)
        code = uco.cmd_update(runner=runner)
        out = capsys.readouterr().out

        assert code == 1
        assert restart_spy == []
        assert "still serving the old code" in out

    def test_a_runner_that_explodes_is_reported_not_raised(
        self, capsys, wheel_install, update_available, brain_matches, restart_spy,
    ):
        def _explode(command, *args, **kwargs):
            raise OSError("no pip on this interpreter")

        code = uco.cmd_update(runner=_explode)
        out = capsys.readouterr().out
        assert code == 1
        assert restart_spy == []
        assert "could not be started" in out

    def test_no_running_service_means_no_restart_attempt(
        self, capsys, wheel_install, update_available, monkeypatch, restart_spy,
    ):
        monkeypatch.setattr(uco, "running_brain_interpreter", lambda: {
            "status": "none", "pid": None, "executable": None,
            "detail": "no brain service is currently running",
        })
        runner = _Runner()
        code = uco.cmd_update(runner=runner)
        out = capsys.readouterr().out

        assert code == 0
        assert len(runner.calls) == 1
        assert restart_spy == []
        assert "No running brain service to restart" in out

    def test_a_failed_restart_is_reported_and_does_not_raise(
        self, capsys, wheel_install, update_available, brain_matches, monkeypatch,
    ):
        import cli.main as cli_main

        def _boom():
            raise RuntimeError("launchctl said no")

        monkeypatch.setattr(cli_main, "cmd_restart", _boom)
        code = uco.cmd_update(runner=_Runner())
        out = capsys.readouterr().out
        assert code == 1
        assert "feral restart" in out


class TestItReusesTheExistingRestart:
    """`feral restart` already exists and already knows about launchd
    kickstart and `systemctl --user restart`. Reimplementing it here
    would be a second, worse copy that drifts."""

    def test_it_calls_cmd_restart_rather_than_shelling_out(
        self, wheel_install, update_available, brain_matches, monkeypatch,
    ):
        import cli.main as cli_main

        seen = []
        monkeypatch.setattr(cli_main, "cmd_restart", lambda: seen.append("cmd_restart"))
        runner = _Runner()
        uco.cmd_update(runner=runner)

        assert seen == ["cmd_restart"]
        # The only subprocess it ran is pip. No launchctl, no systemctl.
        assert all(call[:3] == [sys.executable, "-m", "pip"] for call in runner.calls)


# ---------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------


class TestItIsWiredIn:

    def test_update_is_classified_for_the_r2_002_gate(self):
        """Every registered subcommand must sit in one of the two sets.
        `update` reaches the package index, so it is not pure-local."""
        from cli.main import NEEDS_BRAIN_SUBCOMMANDS, PURE_LOCAL_SUBCOMMANDS

        assert "update" in NEEDS_BRAIN_SUBCOMMANDS
        assert "update" not in PURE_LOCAL_SUBCOMMANDS

    def test_the_subcommand_parses(self):
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="subcommand")
        uco.register_update_subparser(sub)

        args = parser.parse_args(["update"])
        assert args.subcommand == "update"
        assert args.check is False
        assert args.no_restart is False

        args = parser.parse_args(["update", "--check", "--no-restart"])
        assert args.check is True
        assert args.no_restart is True

    def test_dispatch_passes_the_flags_through(self, monkeypatch):
        import argparse

        seen = {}
        monkeypatch.setattr(
            uco, "cmd_update",
            lambda check_only=False, restart=True: seen.update(
                check_only=check_only, restart=restart
            ) or 0,
        )
        uco.dispatch_update(argparse.Namespace(check=True, no_restart=True))
        assert seen == {"check_only": True, "restart": False}
