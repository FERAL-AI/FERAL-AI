"""``feral update``: upgrade the environment that is actually running.

WHY THIS IS A COMMAND AND NOT A DOC LINE

`pip install --upgrade feral-ai` is one line, and the README has it. The
line is not the hard part. The hard part is that on a real machine
there is usually more than one Python environment, `pip` belongs to
whichever one is first on PATH, and the brain is running out of a
different one.

That is a reported failure, not a hypothetical: an operator with a
pyenv install and a venv install ran the upgrade, watched it succeed,
and served stale code for two days. Nothing was broken. `pip` upgraded
the environment it belonged to, correctly, and the brain kept running
the other one. The only evidence available to the operator was that the
bug they had just seen fixed was still there.

`sys.executable` is the whole fix. It is the interpreter running THIS
process, so `sys.executable -m pip` is guaranteed to be the pip of the
environment the `feral` command came from, whatever PATH says. This
command prints which environment that is BEFORE touching it, because
the operator is the one who knows whether it is the right one.

WHAT IT REFUSES TO DO

An editable or source install is not upgraded. `pip install --upgrade`
against a checkout would either no-op or replace the checkout with a
wheel, and neither is what somebody working from git wants. It says so
and points at `git pull`.

A brain running under a different interpreter than the CLI is reported
rather than ignored, because upgrading this environment would leave
that one exactly as stale as it started, which is the original bug
wearing a friendlier face.

WHAT IT DOES AFTERWARDS

Restarts the brain, through the existing `cmd_restart`, because a
Python process never reloads its source: an upgrade without a restart
changes precisely nothing on a machine that is already serving. That is
the failure `config/staleness.py` exists to detect, and doing the
upgrade without the restart would be shipping a command whose whole
purpose is to leave the operator in it.

WHAT IT IS NOT

There is no API endpoint that performs this. An upgrade replaces the
code of the process that would have to perform the restart, so a
half-completed self-upgrade leaves nothing running that is able to
finish the job. `GET /api/dashboard` may REPORT that a release exists;
acting on it is operator-initiated, here, from a process that is not
the brain.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Callable, Optional

PACKAGE = "feral-ai"


# ─────────────────────────────────────────────────────────────────────
# argparse registration
# ─────────────────────────────────────────────────────────────────────


def register_update_subparser(sub) -> None:
    """Register ``feral update`` under ``feral``."""
    update_p = sub.add_parser(
        "update",
        help="Upgrade feral-ai in the environment that is running the brain, then restart it",
    )
    update_p.add_argument(
        "--check", action="store_true",
        help="Report what an upgrade would do and change nothing.",
    )
    update_p.add_argument(
        "--no-restart", action="store_true",
        help="Upgrade but do not restart the brain (it keeps running the old code until you do).",
    )


def dispatch_update(args) -> int:
    return cmd_update(
        check_only=bool(getattr(args, "check", False)),
        restart=not bool(getattr(args, "no_restart", False)),
    )


# ─────────────────────────────────────────────────────────────────────
# Which environment is this, and what kind of install
# ─────────────────────────────────────────────────────────────────────


def install_kind() -> dict:
    """How ``feral-ai`` got here: wheel, editable, or unreadable.

    Reads the dist-info's ``direct_url.json``, which pip writes for any
    install that came from a path or URL rather than an index, and whose
    ``dir_info.editable`` is the authoritative answer to "is this a
    checkout". Falling back to guessing from file paths would misread a
    wheel installed out of a local directory.
    """
    result = {
        "editable": False,
        "from_local_dir": False,
        "source": "",
        "version": None,
        "location": None,
        "detail": "",
    }
    try:
        import importlib.metadata as md

        dist = md.distribution(PACKAGE)
        result["version"] = dist.version
        try:
            result["location"] = str(dist.locate_file(""))
        except Exception:
            result["location"] = None

        raw = None
        try:
            raw = dist.read_text("direct_url.json")
        except Exception:
            raw = None
        if raw:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                result["source"] = str(payload.get("url") or "")
                dir_info = payload.get("dir_info")
                if isinstance(dir_info, dict):
                    result["from_local_dir"] = True
                    result["editable"] = bool(dir_info.get("editable"))
        result["detail"] = "editable install" if result["editable"] else "wheel install"
        return result
    except Exception as exc:
        result["detail"] = f"could not inspect the install: {exc}"
        return result


def brain_process_executable(pid: int) -> Optional[str]:
    """The interpreter binary a running PID is executing, or None.

    Two implementations because the platforms disagree about where this
    lives, and one caveat worth knowing:

    * Linux: ``/proc/<pid>/exe``, which resolves symlinks. A venv's
      ``bin/python`` IS a symlink to its base interpreter, so two
      different venvs built on one base look identical here. That makes
      the Linux check able to miss a real mismatch, never able to
      invent one, which is the right way round for something that
      refuses to upgrade when it fires.
    * macOS: ``ps -o comm=``, which reports the full path that was
      exec'd, so a venv keeps its own identity. Verified against a
      console script: the shebang means the process image is the
      interpreter, so the path is directly comparable to
      ``sys.executable``.
    """
    try:
        if sys.platform.startswith("linux"):
            link = f"/proc/{int(pid)}/exe"
            # readlink FIRST, and not realpath.
            #
            # os.path.realpath does not raise for a path that is not
            # there; it hands back the string it was given. So a dead
            # pid returned the literal "/proc/<pid>/exe", which is not
            # an interpreter, does not equal sys.executable, and made
            # `feral update` refuse with "the brain is running on a
            # different interpreter" when the brain was not running at
            # all. Caught by CI on Linux; the macOS path this was
            # written on cannot reach it.
            #
            # os.readlink raises instead: FileNotFoundError when the
            # process is gone, PermissionError when it belongs to
            # somebody else. Both mean "cannot tell", which is what the
            # caller needs to hear. realpath still does the resolving
            # afterwards, so the symlink caveat above is unchanged.
            os.readlink(link)
            return os.path.realpath(link)
        proc = subprocess.run(
            ["ps", "-p", str(int(pid)), "-o", "comm="],
            capture_output=True, text=True, timeout=10,
        )
        value = (proc.stdout or "").strip()
        return value or None
    except Exception:
        # Every failure here (no such process, no ps, a container with
        # no /proc) means the same thing: we cannot tell. The caller
        # reports that rather than assuming a match.
        return None


def _same_interpreter(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    return os.path.realpath(a) == os.path.realpath(b)


def running_brain_interpreter() -> dict:
    """What interpreter the managed brain service is running, if any.

    ``status`` is one of:
      ``none``     no brain service is installed or running,
      ``match``    it runs the interpreter this CLI is running,
      ``mismatch`` it runs a different one (upgrading here misses it),
      ``unknown``  something is running and we could not read it.
    """
    out = {"status": "unknown", "pid": None, "executable": None, "detail": ""}
    try:
        from cli import daemon as _daemon

        if not _daemon.is_service_supported():
            out["status"] = "none"
            out["detail"] = "service control is only available on macOS and Linux"
            return out
        status = _daemon.service_status()
        pid = status.get("pid")
        if not status.get("running") or not pid:
            out["status"] = "none"
            out["detail"] = "no brain service is currently running"
            return out
        out["pid"] = int(pid)
        exe = brain_process_executable(int(pid))
        out["executable"] = exe
        if not exe:
            out["detail"] = f"could not read the interpreter of pid {pid}"
            return out
        if _same_interpreter(exe, sys.executable):
            out["status"] = "match"
            out["detail"] = f"pid {pid} runs this interpreter"
        else:
            out["status"] = "mismatch"
            out["detail"] = (
                f"pid {pid} is running {exe}, but this CLI is {sys.executable}"
            )
        return out
    except Exception as exc:
        out["detail"] = f"could not inspect the brain service: {exc}"
        return out


# ─────────────────────────────────────────────────────────────────────
# The command
# ─────────────────────────────────────────────────────────────────────


def pip_upgrade_command() -> list[str]:
    """The upgrade, aimed at THIS interpreter's environment.

    ``sys.executable -m pip`` rather than a bare ``pip``: a bare pip is
    whichever one PATH resolves first, which is exactly how an operator
    upgrades the environment that is not running the brain.
    """
    return [sys.executable, "-m", "pip", "install", "--upgrade", PACKAGE]


def cmd_update(
    check_only: bool = False,
    restart: bool = True,
    runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
) -> int:
    """Upgrade this environment's feral-ai and restart the brain.

    ``runner`` is a seam, not a feature: it lets the tests exercise
    every decision this makes without a real pip install, which is not
    something a test suite is allowed to do to the machine it runs on.
    """
    run = runner or subprocess.run
    say = print

    kind = install_kind()
    current = kind.get("version")

    say("")
    say("FERAL update")
    say(f"  environment : {sys.executable}")
    say(f"  package     : {PACKAGE} {current or 'unknown'}")
    if kind.get("location"):
        say(f"  installed at: {kind['location']}")

    # An editable checkout is not pip's to upgrade. Say so and stop,
    # rather than running a command that would either no-op or quietly
    # replace somebody's working tree with a wheel.
    if kind.get("editable"):
        say("")
        say("This is an editable install (`pip install -e`), i.e. a source checkout.")
        if kind.get("source"):
            say(f"  source: {kind['source']}")
        say("Upgrading it from PyPI would replace your checkout with a release wheel.")
        say("Update it the way it was installed instead:")
        say("  git pull")
        say("  feral restart")
        return 1

    status = _availability(current)
    say("")
    say(f"  latest      : {status.get('latest_version') or 'unknown'}")
    if status.get("status") == "unknown":
        say("")
        say("Could not reach the package index, so there is nothing to compare against.")
        if status.get("detail"):
            say(f"  {status['detail']}")
        say("Nothing was changed. Try again when the network is back.")
        return 1

    if not status.get("update_available"):
        say("")
        say(f"Already on the newest release ({current}). Nothing to do.")
        # Not silent about the other half of the story: installed and
        # running are different questions, and this command is where an
        # operator who thinks they are current would find out they are
        # not.
        _report_staleness(say)
        return 0

    latest = status.get("latest_version")
    say("")
    say(f"An update is available: {current} -> {latest}")

    brain = running_brain_interpreter()
    if brain["status"] == "mismatch":
        say("")
        say("The running brain is NOT using this environment.")
        say(f"  brain (pid {brain['pid']}): {brain['executable']}")
        say(f"  this CLI              : {sys.executable}")
        say("Upgrading here would leave the running brain exactly as stale as it is now.")
        say("Run the upgrade with the interpreter that is actually serving:")
        say(f"  {brain['executable']} -m pip install --upgrade {PACKAGE}")
        say("  feral restart")
        return 1
    if brain["status"] == "unknown":
        say(f"  note: {brain['detail']}")

    if check_only:
        say("")
        say("--check: stopping here. Re-run `feral update` without --check to install it.")
        return 0

    command = pip_upgrade_command()
    say("")
    say("Upgrading with:")
    say(f"  {' '.join(command)}")
    say("")

    try:
        completed = run(command)
    except Exception as exc:
        say(f"The upgrade could not be started: {exc}")
        return 1

    code = int(getattr(completed, "returncode", 1) or 0)
    if code != 0:
        say("")
        say(f"pip exited {code}. Nothing was restarted, so the brain is still serving the old code.")
        return code

    installed_now = install_kind().get("version")
    say("")
    say(f"Installed: {PACKAGE} {installed_now or 'unknown'}")

    if not restart:
        say("")
        say("--no-restart: the brain is still running the previous code.")
        say("A running process never reloads its source. Run `feral restart` to pick this up.")
        return 0

    if brain["status"] == "none":
        say("")
        say("No running brain service to restart. The next `feral start` picks this up.")
        return 0

    say("")
    say("Restarting the brain so it picks up the new code...")
    try:
        from cli.main import cmd_restart

        cmd_restart()
    except Exception as exc:
        say(f"The restart failed: {exc}")
        say("The upgrade is installed. Run `feral restart` by hand to load it.")
        return 1
    return 0


def _availability(current: Optional[str]) -> dict:
    """Ask the index, bypassing the opt-in gate.

    `feral update` is the one place the network check does not need
    permission: the operator typed a command whose entire meaning is
    "go and see if there is a newer release". That is the consent, and
    it is why `config/update_check.refresh` takes a ``force`` flag
    rather than this module reaching around it.
    """
    try:
        from config.update_check import compare_versions, refresh

        entry = refresh(force=True)
        latest = entry.get("latest") if isinstance(entry, dict) else None
        if not isinstance(latest, str) or not latest:
            return {
                "status": "unknown",
                "latest_version": None,
                "update_available": None,
                "detail": str(entry.get("error") or "") if isinstance(entry, dict) else "",
            }
        if not current:
            return {
                "status": "unknown",
                "latest_version": latest,
                "update_available": None,
                "detail": "this install's own version could not be read",
            }
        comparison = compare_versions(current, latest)
        if comparison is None:
            return {
                "status": "unknown",
                "latest_version": latest,
                "update_available": None,
                "detail": f"could not compare {current} against {latest}",
            }
        return {
            "status": "update-available" if comparison < 0 else "current",
            "latest_version": latest,
            "update_available": comparison < 0,
            "detail": "",
        }
    except Exception as exc:
        return {
            "status": "unknown",
            "latest_version": None,
            "update_available": None,
            "detail": f"{type(exc).__name__}: {exc}",
        }


def _report_staleness(say) -> None:
    """Mention it if the live process is behind what is installed."""
    try:
        from config.staleness import runtime_staleness

        state = runtime_staleness()
        if state.get("stale"):
            say("")
            say(state.get("detail", ""))
    except Exception:
        # A note that could not be assembled is not worth a word to the
        # operator, and certainly not worth failing an otherwise
        # successful command.
        return


__all__ = [
    "register_update_subparser",
    "dispatch_update",
    "cmd_update",
    "install_kind",
    "running_brain_interpreter",
    "brain_process_executable",
    "pip_upgrade_command",
]
