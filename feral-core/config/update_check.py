"""Is there a newer feral-ai than the one installed here?

`config/staleness.py` answers the local half of the upgrade question:
is this process executing the version that is on disk. It deliberately
stops there, because the other half ("is there a newer release in the
world") cannot be answered without leaving the machine.

This module is that other half, and it is OFF by default.

WHY OFF BY DEFAULT

FERAL is local-first. The promise is that the brain does not talk to
anyone the operator did not point it at, and a check for updates is a
request to pypi.org that carries this machine's IP, a timestamp and the
name of the package it runs. That is a small disclosure, but it is a
disclosure, and one made on the operator's behalf without being asked.
"Small" is the argument every phone-home feature makes.

There is a second reason, less philosophical and more practical: on an
airgapped or filtered network an unrequested outbound request is not
free. It hangs until it times out, it appears in somebody's egress log,
and it makes the brain look like it is doing something it was told not
to do.

So the default is False, the operator turns it on with one env var or
one settings key, and every path in this module treats "off" as a
first-class state that reports `disabled` rather than `unknown`. The
distinction matters: `unknown` invites the operator to go debug their
network, `disabled` tells them the truth, which is that nobody asked.

HOW IT AVOIDS BLOCKING ANYTHING

Nothing here fetches on a request path. `update_status()` reads a cache
file and never opens a socket, so `GET /api/dashboard`, which the whole
shell polls, costs one small local read whatever pypi.org is doing.
The fetch happens in exactly two places: a background refresher in the
brain that first sleeps past boot, and the operator explicitly running
`feral update`. The cache TTL is a day, because releases are not
hourly, and a failed check is cached for an hour so a transient outage
does not blind the check for a day, and does not turn into a retry
loop either.

HOW IT FAILS

Silently and as `unknown`. No network, DNS down, PyPI 503, a proxy
returning HTML, a truncated body: every one of them ends in
`status: "unknown"` with a `detail` an operator can read. This is a
diagnostic. It never raises into a caller, and it never produces a
scary banner from a failed lookup, for the same reason
`config/staleness.py` refuses to call an unreadable version "stale".

VERSION COMPARISON

The versions are calver (`2026.8.25`), and string comparison gets calver
wrong in the direction that matters: `"2026.8.9" > "2026.8.10"` is True
as strings and False as versions, so a naive check would sit silent
exactly during the first nine days of a release month.

`packaging.version` is used when it can be imported, and it is present
in most installs (pytest, huggingface_hub and onnxruntime all pull it
in), but it is NOT a declared dependency of feral-ai, so a minimal
`pip install feral-ai` can genuinely lack it. The fallback below parses
plain dotted-numeric releases only and refuses everything else, which
means the worst case is `unknown` rather than a wrong answer.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("feral.update_check")

#: The distribution this asks about. Same name `config/staleness.py`
#: resolves, so both halves of the upgrade story describe one package.
PACKAGE = "feral-ai"

#: PyPI's per-project JSON. Overridable so an operator behind an
#: internal mirror can point this at their index, and so the tests can
#: aim it at a local server instead of the internet.
DEFAULT_PYPI_URL = f"https://pypi.org/pypi/{PACKAGE}/json"

#: Cache lifetime for a successful answer. A day is generous: releases
#: are not hourly, and the cost of being a day behind on "a newer
#: version exists" is nil next to the cost of asking every boot.
DEFAULT_TTL_HOURS = 24

#: Cache lifetime for a FAILED answer. Shorter, because a failure is
#: usually the network being briefly unavailable rather than a fact
#: about the world, and caching it for a full day would mean one
#: unlucky moment blinds the check until tomorrow.
FAILURE_TTL_SECONDS = 3600

#: Socket timeout. The refresher runs off the request path so nothing
#: waits on this, but an unbounded read on a background task is still a
#: thread parked forever.
DEFAULT_TIMEOUT_S = 5.0

#: Ceiling on the response body we will read. PyPI's JSON for this
#: package is tens of kilobytes; anything past this is a mirror
#: misbehaving or a captive-portal page, and neither deserves memory.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


# ---------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------


def update_check_enabled() -> bool:
    """Has the operator asked for this check? Default False.

    Precedence matches `config/runtime.py::brain_tls_enabled`, which is
    the closest existing analogue: env wins so ops can pin the answer
    for a systemd or docker deployment without touching settings, then
    the persisted `updates.check_pypi` key, then off.

    The default is the whole design. See the module docstring: this is
    the only code in the brain that contacts a server the operator did
    not configure, so it does not run unless they say so.
    """
    raw = os.getenv("FERAL_UPDATE_CHECK")
    if raw is not None and raw.strip() != "":
        return raw.strip().lower() in _TRUE
    persisted = _settings_get("updates", "check_pypi")
    if isinstance(persisted, bool):
        return persisted
    if isinstance(persisted, str):
        return persisted.strip().lower() in _TRUE
    return False


def ttl_seconds() -> float:
    """How long a successful answer stays fresh."""
    raw = os.getenv("FERAL_UPDATE_CHECK_TTL_HOURS")
    hours: Optional[float] = None
    if raw:
        try:
            hours = float(raw)
        except ValueError:
            logger.debug("ignoring unparseable FERAL_UPDATE_CHECK_TTL_HOURS=%r", raw)
    if hours is None:
        persisted = _settings_get("updates", "ttl_hours")
        if isinstance(persisted, (int, float)) and not isinstance(persisted, bool):
            hours = float(persisted)
    if hours is None or hours <= 0:
        hours = float(DEFAULT_TTL_HOURS)
    return hours * 3600.0


def pypi_url() -> str:
    return os.getenv("FERAL_PYPI_JSON_URL", "").strip() or DEFAULT_PYPI_URL


def _settings_get(*path: str) -> object | None:
    """Best-effort nested read from ``settings.json``. Never raises.

    Delegates to ``config.runtime`` rather than re-parsing the file, so
    there is one parse path for persisted settings and this cannot
    drift from how every other runtime toggle is resolved.
    """
    try:
        from config.runtime import _settings_get as _get

        return _get(*path)
    except Exception as exc:
        logger.debug("settings lookup for %s failed: %s", ".".join(path), exc)
        return None


# ---------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------

_NUMERIC_RELEASE = re.compile(r"^\d+(?:\.\d+)*$")


def parse_version(text: str):
    """Return a comparable key for ``text``, or None if unparseable.

    Two implementations, and which one runs depends on the install. See
    the module docstring for why `packaging` cannot simply be assumed.
    Both return objects that compare correctly against their own kind;
    callers must never mix them, which is why `compare_versions` parses
    both operands through this one function.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    text = text.strip()
    try:
        from packaging.version import InvalidVersion, Version

        try:
            return Version(text)
        except InvalidVersion:
            return None
    except ImportError:
        pass
    # Fallback: plain dotted-numeric releases only. It gets calver right
    # (that is the case that matters here) and refuses anything with a
    # pre/post/dev/local segment rather than guessing at an ordering it
    # cannot know. A refusal surfaces as "unknown", never as a wrong
    # "you are up to date".
    if not _NUMERIC_RELEASE.match(text):
        return None
    return tuple(int(part) for part in text.split("."))


def compare_versions(left: str, right: str) -> Optional[int]:
    """-1 / 0 / 1 for left vs right, or None if either is unparseable.

    None is a real answer here and not an error: an unparseable version
    means we do not know, and the caller reports `unknown` rather than
    inventing an ordering.
    """
    a = parse_version(left)
    b = parse_version(right)
    if a is None or b is None:
        return None
    # A tuple and a packaging Version never meet: `parse_version` picks
    # one implementation per process, not per call.
    if a < b:
        return -1
    if a > b:
        return 1
    return 0


def is_prerelease(text: str) -> bool:
    """True only when we can positively tell. Unknown counts as False.

    Used to keep a release candidate from being announced as "an update
    is available" to an operator on the stable channel. Under the
    numeric fallback nothing parses as a prerelease, but nothing parses
    a prerelease string at all either, so such versions are dropped
    earlier as unparseable.
    """
    try:
        from packaging.version import InvalidVersion, Version

        try:
            return bool(Version(text).is_prerelease)
        except InvalidVersion:
            return False
    except ImportError:
        return False


# ---------------------------------------------------------------------
# The fetch
# ---------------------------------------------------------------------


def _pick_latest(payload: object) -> Optional[str]:
    """Newest non-yanked, non-prerelease version in a PyPI JSON body.

    `info.version` alone is not enough: it can name a prerelease, and it
    can name a release whose files were all yanked. Walking `releases`
    and taking the greatest version we can actually parse gives the
    answer pip would give a default `pip install --upgrade`, which is
    the command this check exists to recommend.
    """
    if not isinstance(payload, dict):
        return None

    candidates: list[str] = []
    releases = payload.get("releases")
    if isinstance(releases, dict):
        for version, files in releases.items():
            if not isinstance(version, str):
                continue
            if isinstance(files, list) and files:
                # Every file yanked means the release is withdrawn; pip
                # will not resolve to it, so neither will we.
                if all(isinstance(f, dict) and f.get("yanked") for f in files):
                    continue
            candidates.append(version)

    if not candidates:
        info = payload.get("info")
        if isinstance(info, dict) and isinstance(info.get("version"), str):
            candidates = [info["version"]]

    best: Optional[str] = None
    best_key = None
    for version in candidates:
        if is_prerelease(version):
            continue
        key = parse_version(version)
        if key is None:
            continue
        if best_key is None or key > best_key:
            best, best_key = version, key
    return best


def fetch_latest(timeout: float = DEFAULT_TIMEOUT_S) -> tuple[Optional[str], str]:
    """Ask PyPI. Returns ``(version_or_None, error_text)``. Never raises.

    stdlib urllib rather than httpx on purpose: this runs from the CLI
    as well as the brain, it is one GET, and it must not depend on an
    event loop or on an httpx client's lifecycle.
    """
    url = pypi_url()
    # http(s) only. `urlopen` also speaks file:// and ftp://, and this
    # URL comes from an env var, so a typo or a bad shell profile could
    # turn a version check into a local file read. Refuse instead.
    if not url.startswith(("http://", "https://")):
        return None, "the configured index URL is not http(s)"
    try:
        import urllib.request

        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                # Identify honestly. A check that hides what it is
                # would be a worse privacy story, not a better one.
                "User-Agent": f"{PACKAGE}-update-check",
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            return None, "response too large to be a package index answer"
        payload = json.loads(body.decode("utf-8", "replace"))
    except Exception as exc:
        # Deliberately broad. Every failure mode of a network call
        # (DNS, TLS, timeout, HTTP error, proxy HTML, truncated body,
        # bad JSON) has the same meaning to the caller: we do not know.
        # Logged with context at debug so it is diagnosable, and never
        # re-raised, because nothing here is worth costing a caller.
        logger.debug("update check against %s failed: %s", url, exc)
        return None, f"{type(exc).__name__}: {exc}"

    latest = _pick_latest(payload)
    if not latest:
        return None, "index answered without a usable version"
    return latest, ""


# ---------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------


def cache_path() -> Path:
    """Where the answer is remembered.

    Under FERAL_HOME so it is per-install and disappears with the rest
    of the brain's state, rather than in a global user cache dir that
    would outlive an uninstall.
    """
    from config.loader import feral_home

    return feral_home() / "update-check.json"


def read_cache() -> Optional[dict]:
    """The last recorded answer, or None. Never raises."""
    try:
        path = cache_path()
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.debug("could not read the update-check cache: %s", exc)
        return None


def write_cache(entry: dict) -> bool:
    """Record an answer. Returns whether it landed. Never raises."""
    try:
        path = cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so a crash mid-write cannot leave a
        # half-written file that every later read has to survive.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(entry, indent=2))
        tmp.replace(path)
        return True
    except Exception as exc:
        logger.debug("could not write the update-check cache: %s", exc)
        return False


def _cache_is_fresh(entry: Optional[dict], now: Optional[float] = None) -> bool:
    if not entry:
        return False
    checked_at = entry.get("checked_at")
    if not isinstance(checked_at, (int, float)):
        return False
    now = time.time() if now is None else now
    age = now - float(checked_at)
    if age < 0:
        # A clock that moved backwards. Treat the entry as stale rather
        # than as fresh forever.
        return False
    ttl = ttl_seconds() if entry.get("ok") else FAILURE_TTL_SECONDS
    return age < ttl


def refresh(force: bool = False, timeout: float = DEFAULT_TIMEOUT_S) -> dict:
    """Fetch if allowed and needed, then return the cache entry.

    ``force`` is for `feral update`, where the operator typed the
    command and that IS the consent: it bypasses both the enable
    setting and the TTL. Everything else respects both, so a disabled
    check performs no request under any circumstances.

    Never raises.
    """
    if not force and not update_check_enabled():
        return {"ok": False, "disabled": True, "checked_at": None,
                "latest": None, "error": "update check is disabled"}

    cached = read_cache()
    if not force and _cache_is_fresh(cached):
        return cached  # type: ignore[return-value]

    latest, error = fetch_latest(timeout=timeout)
    entry = {
        "ok": bool(latest),
        "latest": latest,
        "checked_at": time.time(),
        "error": error,
        "source": pypi_url(),
    }
    write_cache(entry)
    return entry


# ---------------------------------------------------------------------
# The surface
# ---------------------------------------------------------------------


def update_status() -> dict:
    """What to show an operator. Reads the cache only, never the network.

    This is what `GET /api/dashboard` and `feral doctor` call, and the
    no-network rule is not a nicety: the dashboard is polled by the
    whole shell, and doctor is classified pure-local in
    ``cli/main.py::PURE_LOCAL_SUBCOMMANDS``. Either one reaching out
    would be a regression in something other than this feature.

    Never raises.
    """
    result = {
        "enabled": False,
        "status": "disabled",
        "latest_version": None,
        "current_version": None,
        "update_available": None,
        "checked_at": None,
        "age_s": None,
        "detail": "",
    }
    try:
        result["enabled"] = update_check_enabled()
        result["current_version"] = _current_version()

        if not result["enabled"]:
            result["detail"] = (
                "Update checks are off. FERAL does not contact PyPI unless "
                "asked: set FERAL_UPDATE_CHECK=1 or updates.check_pypi in "
                "settings.json. `feral update` checks on demand either way."
            )
            return result

        entry = read_cache()
        if not entry:
            result["status"] = "unknown"
            result["detail"] = "No check has been recorded yet."
            return result

        checked_at = entry.get("checked_at")
        if isinstance(checked_at, (int, float)):
            result["checked_at"] = float(checked_at)
            result["age_s"] = round(max(0.0, time.time() - float(checked_at)), 1)

        latest = entry.get("latest")
        if not entry.get("ok") or not isinstance(latest, str):
            result["status"] = "unknown"
            result["detail"] = (
                "Could not reach the package index on the last check"
                + (f": {entry.get('error')}" if entry.get("error") else ".")
            )
            return result

        result["latest_version"] = latest
        current = result["current_version"]
        if not current:
            result["status"] = "unknown"
            result["detail"] = (
                f"{latest} is the newest published release, but this "
                f"install's own version could not be read."
            )
            return result

        comparison = compare_versions(current, latest)
        if comparison is None:
            result["status"] = "unknown"
            result["detail"] = (
                f"Could not compare {current} against {latest}."
            )
            return result

        if comparison < 0:
            result["status"] = "update-available"
            result["update_available"] = True
            result["detail"] = (
                f"{latest} is available; this install is on {current}. "
                f"Run `feral update`."
            )
        else:
            result["status"] = "current"
            result["update_available"] = False
            result["detail"] = f"{current} is the newest release."
        return result
    except Exception as exc:
        # A diagnostic must never cost its caller. Same rule as
        # `config/staleness.py`: report "we do not know", stay quiet.
        logger.debug("update status could not be assembled: %s", exc)
        result["status"] = "unknown"
        result["detail"] = "unavailable"
        return result


def _current_version() -> Optional[str]:
    """The version installed on disk, or the running one as a fallback.

    Installed, not running, is the right base for "is something newer
    available": it is the version `pip install --upgrade` would replace.
    Whether the live process is actually executing it is the separate
    question `config/staleness.py` answers, and conflating the two would
    tell an operator who upgraded but did not restart that an update is
    available when they already have it on disk.
    """
    try:
        from config.staleness import RUNNING_VERSION, installed_version

        installed = installed_version()
        if installed:
            return installed
        return RUNNING_VERSION if RUNNING_VERSION not in ("", "unknown") else None
    except Exception as exc:
        logger.debug("could not resolve the current version: %s", exc)
        return None
