"""Is this process running the code that is currently installed?

A Python process never reloads its source. It reads the package once at
import and holds it in memory for its whole life, so
`pip install --upgrade feral-ai` cannot reach a brain that is already
running. The upgrade succeeds, nothing errors, and the operator keeps
using last week's code with no symptom at all.

That is not hypothetical. On 2026-08-23 a brain had been serving for
two days and one hour, from code loaded before four releases had
landed. The operator had upgraded from PyPI, correctly, and every fix
in those releases was inert. The dashboard kept rendering. It just kept
rendering the old build.

Nothing in the product noticed. There is no version check, the README
documents installing and not upgrading, and `feral restart` exists but
nothing ever suggests running it.

HOW THIS DETECTS IT, WITH NO NETWORK

`version.VERSION` is `importlib.metadata.version("feral-ai")` evaluated
at import, so it is frozen at the moment the process started: it is the
version RUNNING. Asking importlib again re-reads the installed
distribution from disk, which is the version INSTALLED. When they
disagree, somebody upgraded underneath a live process.

Deliberately local. This never contacts PyPI. Whether a newer release
exists in the world is a different question, needs the network, and on
a local-first privacy product is not something to do unasked.

WHAT THIS DOES NOT CATCH

An editable install (`pip install -e`, i.e. every developer checkout)
resolves both sides from the same dist-info, which is written once at
install time and does not track edits to the source. So on a dev
machine this reports "not stale" no matter how far the working tree has
moved, and the version it prints may be older than the code. That is
correct behaviour rather than a hole: an editable install genuinely has
no second version to compare against. It is stated here so nobody reads
a green row in a checkout as evidence the check works, which is exactly
the mistake the top-level-module placement above already invited once.

The case this is FOR is the packaged one: a user on a wheel, who ran
`pip install --upgrade feral-ai` against a brain that was already
running. There, the frozen and the on-disk versions genuinely differ.

WHY THIS LIVES IN `config/` AND NOT AT THE TOP LEVEL

It was written as `feral-core/runtime_staleness.py` first, and the
`feral doctor` row reported `No module named 'runtime_staleness'`.
`version.py` is the ONLY top-level module the wheel ships, so a new one
next to it is importable from a source checkout and absent for every
user who installed from PyPI. A diagnostic that exists only on the
developer's machine is worse than none, because it reports healthy from
the one place the bug cannot happen.
"""
from __future__ import annotations

import importlib
import importlib.metadata
import logging
import os
import time
from typing import Optional

logger = logging.getLogger("feral.staleness")

#: The version this process is EXECUTING. Frozen at import by
#: `version.py`, which is the property that makes the comparison work.
try:
    from version import VERSION as RUNNING_VERSION
except Exception:  # pragma: no cover - packaging accident
    RUNNING_VERSION = "unknown"

#: When this process started. Used to say how long a stale brain has
#: been stale, because "restart it" lands differently at four minutes
#: and at two days.
_STARTED_AT = time.time()


def installed_version() -> Optional[str]:
    """What is installed on disk right now, or None if unreadable.

    `importlib.invalidate_caches()` first. Measured, the metadata finder
    does usually notice on its own, because it keys its cached directory
    listing on the directory mtime and a pip install changes that. The
    invalidation is here because "usually" is the wrong guarantee for
    the one function whose entire job is to notice a change on disk: a
    cached answer would make this agree with itself and never fire,
    which is the silent failure mode this module exists to end.
    """
    try:
        importlib.invalidate_caches()
        return importlib.metadata.version("feral-ai")
    except Exception as exc:
        logger.debug("could not read the installed feral-ai version: %s", exc)
        return None


def runtime_staleness() -> dict:
    """Whether the running code matches the installed code.

    Returns a dict rather than a bool so a caller can explain itself.
    `stale` is only ever True on real evidence: an unreadable or unknown
    version reports `stale: False` with a reason, because telling
    somebody to restart on the strength of a failed lookup is worse than
    staying quiet.
    """
    running = RUNNING_VERSION
    installed = installed_version()
    uptime = max(0.0, time.time() - _STARTED_AT)

    result = {
        "running_version": running,
        "installed_version": installed,
        "stale": False,
        "uptime_s": round(uptime, 1),
        "pid": os.getpid(),
        "detail": "",
    }

    if not installed or running in ("", "unknown"):
        result["detail"] = (
            "Could not compare: one of the two versions is unreadable. "
            "Not treating that as stale."
        )
        return result

    if installed == running:
        result["detail"] = f"Running the installed version ({running})."
        return result

    result["stale"] = True
    result["detail"] = (
        f"This brain is running {running} but {installed} is installed. "
        f"A running process never reloads its code, so the upgrade has "
        f"not taken effect. Restart the brain (`feral restart`, or stop "
        f"and re-run `feral serve`) to pick it up. Uptime "
        f"{_human_uptime(uptime)}."
    )
    return result


def _human_uptime(seconds: float) -> str:
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def log_if_stale() -> bool:
    """Warn once, at boot, if the process is already behind.

    Cheap to call and worth calling: a brain restarted right after an
    upgrade reports nothing, and one that somehow starts stale says so
    in the operator's own log rather than waiting to be asked.
    """
    state = runtime_staleness()
    if state["stale"]:
        logger.warning("feral.staleness: %s", state["detail"])
    return bool(state["stale"])
