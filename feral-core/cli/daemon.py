"""FERAL service-lifecycle management — launchd (macOS) + systemd (Linux).

v2026.5.28 turned ``feral start`` into a real macOS service: by default
the brain detaches into a launchd LaunchAgent (``com.feral.brain``)
and the terminal returns immediately, so closing the shell no longer
kills the brain. ``feral stop`` / ``feral status`` / ``feral logs`` /
``feral restart`` manage the running service via ``launchctl``.

Foreground mode is still available with ``feral start --foreground``
for ops that want REPL-attached behaviour (and the launchd plist itself
delegates to that path via ``ProgramArguments`` so the same banner
chrome ends up in the log file).

This module deliberately avoids any direct knowledge of the brain — it
just wraps ``launchctl`` / ``systemctl`` against an absolute ``feral``
path, a stable Label, and ``~/.feral/logs/``. The brain side of the
contract is ``cmd_serve`` (foreground server) and ``cmd_start_service``
(this module's caller).

Migration: prior versions installed under ``ai.feral.brain``. On every
``feral start`` we bootout the old label and bootstrap the new one.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import platform
from pathlib import Path
from typing import Optional


# ----- Stable identifiers ----------------------------------------------------

SERVICE_LABEL = "com.feral.brain"

# v2026.5.27 and earlier installs used the ``ai.feral.brain`` label.
# We bootout that legacy plist on every install so operators don't
# end up running two copies of the brain side by side.
LEGACY_LABELS = ("ai.feral.brain",)


# ----- Helpers ---------------------------------------------------------------


def _logs_dir() -> Path:
    d = Path.home() / ".feral" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    # Owner-only. The brain's stderr carried the Telegram bot token on
    # 5,983 lines and the Gemini key on 1,857 lines (httpx logs request
    # URLs at INFO), and launchd/systemd created the files 0644 under a
    # 0755 directory. Redaction now happens in the logger, this is the
    # second layer for anything that still gets through.
    try:
        os.chmod(str(d), 0o700)
    except OSError:
        pass
    return d


def _stdout_log() -> Path:
    return _logs_dir() / "brain.log"


def _stderr_log() -> Path:
    return _logs_dir() / "brain.err"


def _ensure_private_file(path: Path, *, create: bool) -> None:
    """Make ``path`` owner-read/write only, creating it first if asked.

    launchd and systemd open the log paths themselves with a default
    umask, so the file has to exist with the right mode before the
    service is (re)installed. Existing files are tightened in place, so
    an operator on an older install gets 0600 the next time ``feral
    start`` or ``feral logs`` runs. ``feral logs`` tails as the same
    user, so 0600 does not lock it out.
    """
    if create:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            os.close(fd)
        except OSError:
            return
    if path.exists():
        try:
            os.chmod(str(path), 0o600)
        except OSError:
            pass


def _prepare_log_files(*, create: bool = True) -> tuple[Path, Path]:
    out, err = _stdout_log(), _stderr_log()
    _ensure_private_file(out, create=create)
    _ensure_private_file(err, create=create)
    return out, err


def _resolve_feral_bin() -> str:
    """Return the absolute path to the ``feral`` console script.

    Falls back to ``sys.executable -m cli.main`` shape when the
    ``feral`` console-script binary isn't on PATH (e.g. ``pip install
    -e .`` developer checkouts that didn't install the shim).
    """
    found = shutil.which("feral")
    if found:
        return str(Path(found).resolve())
    # Last-resort fallback. Re-shape callers handle this via a
    # multi-arg list rather than a single string.
    return sys.executable


def _resolve_program_arguments() -> list[str]:
    """``ProgramArguments`` array for the plist / systemd unit.

    Always launches the **foreground** path so launchd's
    `StandardOutPath` / `StandardErrorPath` capture the same banner
    chrome an operator sees when running interactively. Without
    ``--foreground`` we would recursively re-launch the service.
    """
    feral_bin = _resolve_feral_bin()
    if Path(feral_bin).name == Path(sys.executable).name:
        # v2026.5.29 — fall back to ``cli.main start --foreground``,
        # NOT ``cli.main serve``. ``serve`` skips the first-run check
        # and the readiness Progress / ready panel; if a brand-new
        # operator has never run ``feral setup`` interactively, the
        # service would happily come up with no credentials and the
        # WebUI would error on every LLM call. Routing back through
        # ``start --foreground`` keeps the service-mode child on the
        # same boot path as the interactive REPL.
        return [feral_bin, "-m", "cli.main", "start", "--foreground", "--no-browser"]
    return [feral_bin, "start", "--foreground", "--no-browser"]


def _user_uid() -> int:
    return os.getuid()


def _bootstrap_domain() -> str:
    return f"gui/{_user_uid()}"


def _service_target() -> str:
    return f"{_bootstrap_domain()}/{SERVICE_LABEL}"


def _launchctl(*args: str, check: bool = False, capture: bool = False) -> subprocess.CompletedProcess:
    """Wrap ``launchctl`` so callers don't need to remember capture flags."""
    return subprocess.run(
        ["launchctl", *args],
        check=check,
        capture_output=capture,
        text=True,
    )


# ----- macOS LaunchAgent -----------------------------------------------------


def _launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"


def _legacy_launchd_plist_paths() -> list[Path]:
    return [
        Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        for label in LEGACY_LABELS
    ]


def _build_environment_vars() -> dict[str, str]:
    """Variables propagated into the plist's ``EnvironmentVariables`` dict.

    launchd does not source shell rc files, so anything ``feral start``
    relies on (PATH, HOME, FERAL_*) has to be explicit. We propagate
    the operator's current FERAL_* env so a one-off
    ``FERAL_TLS=1 feral start`` survives across reboots until they run
    ``feral start`` again with a different env.
    """
    env: dict[str, str] = {
        "HOME": os.environ.get("HOME", str(Path.home())),
        "PATH": os.environ.get(
            "PATH",
            "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin",
        ),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
    }
    if "USER" in os.environ:
        env["USER"] = os.environ["USER"]
    # Forward every FERAL_* env the operator currently has set so the
    # service inherits the same config the interactive shell uses.
    for key, value in os.environ.items():
        if key.startswith("FERAL_") and key not in env:
            env[key] = value
    return env


def _render_plist(program_arguments: list[str], environment: dict[str, str]) -> str:
    def _escape(s: str) -> str:
        return (
            s.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    args_xml = "\n".join(
        f"        <string>{_escape(a)}</string>" for a in program_arguments
    )
    env_xml = "\n".join(
        f"        <key>{_escape(k)}</key>\n        <string>{_escape(v)}</string>"
        for k, v in environment.items()
    )
    working_dir = environment.get("HOME", str(Path.home()))

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        f"    <key>Label</key>\n    <string>{SERVICE_LABEL}</string>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        f"{args_xml}\n"
        "    </array>\n"
        "    <key>RunAtLoad</key>\n    <true/>\n"
        "    <key>KeepAlive</key>\n    <true/>\n"
        "    <key>ThrottleInterval</key>\n    <integer>5</integer>\n"
        "    <key>ProcessType</key>\n    <string>Interactive</string>\n"
        f"    <key>WorkingDirectory</key>\n    <string>{_escape(working_dir)}</string>\n"
        f"    <key>StandardOutPath</key>\n    <string>{_escape(str(_stdout_log()))}</string>\n"
        f"    <key>StandardErrorPath</key>\n    <string>{_escape(str(_stderr_log()))}</string>\n"
        "    <key>EnvironmentVariables</key>\n"
        "    <dict>\n"
        f"{env_xml}\n"
        "    </dict>\n"
        "</dict>\n"
        "</plist>\n"
    )


def _write_launchd_plist() -> Path:
    plist_path = _launchd_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    # Before launchd opens them with its own umask.
    _prepare_log_files()
    content = _render_plist(_resolve_program_arguments(), _build_environment_vars())
    plist_path.write_text(content)
    os.chmod(str(plist_path), 0o644)
    return plist_path


def _migrate_legacy_plists() -> None:
    """Bootout + remove any old-label plists so the new one is canonical."""
    for legacy in _legacy_launchd_plist_paths():
        if not legacy.exists():
            continue
        # Best-effort unload — if it's not loaded, launchctl exits non-zero.
        _launchctl("bootout", _bootstrap_domain(), str(legacy))
        try:
            legacy.unlink()
        except OSError:
            pass


def _is_service_installed_macos() -> bool:
    return _launchd_plist_path().exists()


def _is_service_running_macos() -> bool:
    result = _launchctl("print", _service_target(), capture=True)
    return result.returncode == 0


def _install_and_start_macos(*, restart_if_running: bool = True) -> None:
    _migrate_legacy_plists()
    plist_path = _write_launchd_plist()

    if _is_service_running_macos():
        if not restart_if_running:
            return
        # Re-bootstrap so the new plist's ProgramArguments / env take
        # effect. ``bootout`` is idempotent; ``bootstrap`` will fail
        # softly if the service stays loaded due to dependent jobs.
        _launchctl("bootout", _bootstrap_domain(), str(plist_path))

    _launchctl("bootstrap", _bootstrap_domain(), str(plist_path))
    # ``kickstart -k`` forces a (re)start even if launchd is otherwise
    # holding the job in a throttled state.
    _launchctl("kickstart", "-k", _service_target())


def _stop_macos() -> bool:
    plist_path = _launchd_plist_path()
    if not plist_path.exists():
        return False
    _launchctl("bootout", _bootstrap_domain(), str(plist_path))
    return True


def _status_macos() -> dict[str, object]:
    plist_path = _launchd_plist_path()
    installed = plist_path.exists()
    if not installed:
        return {"installed": False, "running": False}

    result = _launchctl("print", _service_target(), capture=True)
    running = result.returncode == 0
    pid: Optional[int] = None
    state: Optional[str] = None
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("pid = "):
            try:
                pid = int(line.split("=", 1)[1].strip())
            except ValueError:
                pid = None
        elif line.startswith("state = "):
            state = line.split("=", 1)[1].strip()

    return {
        "installed": True,
        "running": running,
        "pid": pid,
        "state": state,
        "plist": str(plist_path),
        "stdout_log": str(_stdout_log()),
        "stderr_log": str(_stderr_log()),
    }


# ----- Linux user systemd unit ----------------------------------------------


def _systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / "feral-brain.service"


def _install_and_start_linux() -> None:
    unit_path = _systemd_unit_path()
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    # Before systemd opens them with ``append:`` and its own umask.
    _prepare_log_files()
    program = " ".join(_resolve_program_arguments())
    feral_dir = os.path.dirname(_resolve_feral_bin())
    feral_env_lines = "\n".join(
        f'Environment={k}={v}'
        for k, v in _build_environment_vars().items()
        if k.startswith("FERAL_")
    )
    unit = (
        "[Unit]\n"
        "Description=FERAL Brain — AI Operating System\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={program}\n"
        f"WorkingDirectory={Path.home()}\n"
        f"StandardOutput=append:{_stdout_log()}\n"
        f"StandardError=append:{_stderr_log()}\n"
        "Restart=always\n"
        "RestartSec=5\n"
        "StartLimitBurst=5\n"
        "StartLimitIntervalSec=60\n"
        "KillMode=control-group\n"
        f"Environment=PATH=/usr/local/bin:/usr/bin:/bin:{feral_dir}\n"
        f"{feral_env_lines}\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    unit_path.write_text(unit)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    subprocess.run(
        ["systemctl", "--user", "enable", "feral-brain.service"],
        check=False,
    )
    subprocess.run(
        ["systemctl", "--user", "restart", "feral-brain.service"],
        check=False,
    )


def _stop_linux() -> bool:
    result = subprocess.run(
        ["systemctl", "--user", "stop", "feral-brain.service"],
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def _status_linux() -> dict[str, object]:
    result = subprocess.run(
        ["systemctl", "--user", "is-active", "feral-brain.service"],
        check=False,
        capture_output=True,
        text=True,
    )
    return {
        "installed": _systemd_unit_path().exists(),
        "running": (result.stdout or "").strip() == "active",
        "pid": None,
        "state": (result.stdout or "").strip(),
        "stdout_log": str(_stdout_log()),
        "stderr_log": str(_stderr_log()),
    }


# ----- Public façade ---------------------------------------------------------


def start_service() -> dict[str, object]:
    """Idempotent install + start. Returns the same shape as ``service_status``."""
    system = platform.system()
    if system == "Darwin":
        _install_and_start_macos()
        return service_status()
    if system == "Linux":
        _install_and_start_linux()
        return service_status()
    raise RuntimeError(f"Service mode is not supported on {system}")


def stop_service() -> bool:
    system = platform.system()
    if system == "Darwin":
        return _stop_macos()
    if system == "Linux":
        return _stop_linux()
    raise RuntimeError(f"Service mode is not supported on {system}")


def restart_service() -> dict[str, object]:
    """Equivalent to ``stop_service`` + ``start_service`` but cheaper on macOS."""
    system = platform.system()
    if system == "Darwin":
        # bootout + bootstrap re-renders the plist; kickstart -k forces fresh process.
        _install_and_start_macos(restart_if_running=True)
        return service_status()
    if system == "Linux":
        subprocess.run(
            ["systemctl", "--user", "restart", "feral-brain.service"],
            check=False,
        )
        return service_status()
    raise RuntimeError(f"Service mode is not supported on {system}")


def service_status() -> dict[str, object]:
    system = platform.system()
    if system == "Darwin":
        return _status_macos()
    if system == "Linux":
        return _status_linux()
    return {"installed": False, "running": False, "platform_unsupported": system}


def log_paths() -> tuple[Path, Path]:
    """Return ``(stdout_log, stderr_log)`` for ``feral logs`` to tail.

    Also tightens the files to 0600 so an install that predates the
    private-log change is fixed the first time the operator looks.
    """
    return _prepare_log_files(create=False)


def is_service_supported() -> bool:
    return platform.system() in ("Darwin", "Linux")


# ----- Back-compat: ``feral install-service`` / ``feral uninstall-service`` --
#
# v2026.5.27 and earlier shipped two explicit subcommands. Keep them
# alive so existing scripts (in CI, in operator setups, in HOME
# config) keep working — they now route to the same façade.


def install_service() -> bool:
    """Back-compat shim — installs + starts the service."""
    try:
        start_service()
        return True
    except Exception as exc:
        sys.stderr.write(f"install_service failed: {exc}\n")
        return False


def uninstall_service() -> bool:
    """Back-compat shim — stops and removes the service.

    macOS: bootout the plist and delete it.
    Linux: stop, disable, and remove the unit file.
    Legacy ``ai.feral.brain`` labels are cleaned up too.
    """
    system = platform.system()
    if system == "Darwin":
        _stop_macos()
        try:
            _launchd_plist_path().unlink(missing_ok=True)
        except OSError:
            pass
        _migrate_legacy_plists()
        return True
    if system == "Linux":
        subprocess.run(
            ["systemctl", "--user", "stop", "feral-brain.service"],
            check=False,
        )
        subprocess.run(
            ["systemctl", "--user", "disable", "feral-brain.service"],
            check=False,
        )
        try:
            _systemd_unit_path().unlink(missing_ok=True)
        except OSError:
            pass
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        return True
    sys.stderr.write(f"Service mode not supported on {system}\n")
    return False


__all__ = [
    "SERVICE_LABEL",
    "LEGACY_LABELS",
    "start_service",
    "stop_service",
    "restart_service",
    "service_status",
    "log_paths",
    "is_service_supported",
    "install_service",
    "uninstall_service",
]
