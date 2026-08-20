"""Phase 11 (audit-r10 overhaul) — AppleScript runner.

Single, hardened ``run_applescript(script)`` entrypoint that wraps
``osascript`` invocation with:

* Platform guard — non-Darwin hosts raise
  ``AppleScriptUnsupportedPlatform`` immediately so callers don't get
  a misleading subprocess error.
* Timeout — never blocks the orchestrator turn forever on a hung Mac
  app dialog.
* TCC error detection — parses ``osascript`` stderr for the macOS
  Automation denial pattern and returns a structured
  ``tcc_denied:<bundle_id>`` token in ``error`` so the caller can
  emit a ``tcc_card`` SDUI element (Phase 11 mirror of Phase 6).

The runner is intentionally low-level: callers pass full
AppleScript source. High-level wrappers (FaceTime / Music / etc.)
live in ``facade.py`` so this module stays small + auditable.
"""
from __future__ import annotations

import logging
import platform
import re
import subprocess
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("feral.desktop_control.applescript")


class AppleScriptUnsupportedPlatform(RuntimeError):
    """Raised when ``run_applescript`` is called on a non-macOS host."""


@dataclass
class AppleScriptResult:
    success: bool
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    # When the call hit a macOS Automation denial, ``tcc_target_bundle``
    # carries the bundle ID the script tried to control so callers
    # (tool_runner) can mint a ``tcc_card`` deeplinking to the right
    # Automation row in System Settings.
    tcc_target_bundle: Optional[str] = None
    # When the call hit an *Accessibility* denial instead (osascript is
    # a separate TCC subject from the Python host process, so it can be
    # denied while `AXIsProcessTrustedWithOptions` says the brain is
    # trusted), this carries the permission key `tcc_card` understands
    #, currently always "accessibility".
    tcc_permission: Optional[str] = None

    def to_envelope(self, *, action: str = "") -> dict:
        """Map to the standard tool envelope shape so the orchestrator
        sees brain-host actions identically to node actions.
        """
        if self.success:
            return {
                "success": True,
                "status_code": 200,
                "data": {"stdout": self.stdout.rstrip("\n")},
            }
        if self.tcc_permission:
            return {
                "success": False,
                "status_code": 403,
                "error": f"tcc_denied:{self.tcc_permission}",
                "data": {"stderr": self.stderr.rstrip("\n"), "action": action},
            }
        if self.tcc_target_bundle:
            return {
                "success": False,
                "status_code": 403,
                "error": f"tcc_denied:automation:{self.tcc_target_bundle}",
                "data": {"stderr": self.stderr.rstrip("\n"), "action": action},
            }
        return {
            "success": False,
            "status_code": self.exit_code if self.exit_code else 500,
            "error": (self.stderr.rstrip("\n") or "AppleScript failed without stderr"),
            "data": {"action": action},
        }


# `osascript` writes Automation denials to stderr with two stable
# markers depending on macOS version:
#
#   1. "Not authorized to send Apple events to <App>." (>= Catalina)
#   2. "execution error: <App> got an error: ... (-1743)" (older)
#
# We match both. Capture the application name when present so the
# tcc_card can deeplink to the right row in Privacy & Security.
_AUTO_DENIAL_PATTERNS = [
    re.compile(r"Not authorized to send Apple events to ([^\.\n]+)\.?"),
    re.compile(r"execution error: ([^ ]+) got an error.*-1743", re.DOTALL),
    re.compile(r"errAEEventNotPermitted"),
]

# Accessibility (assistive access) denials are a *different* TCC bucket
# from Automation and deeplink to a different System Settings pane. The
# markers osascript emits:
#
#   1. "System Events got an error: osascript is not allowed assistive
#      access. (-25211)"                        (kAXErrorAPIDisabled)
#   2. "... is not allowed to send keystrokes. (-25211)"
#   3. bare "(-25211)" / "errAXAPIDisabled" on older releases
#
# Before this list existed, every one of these fell through to the
# generic branch of `to_envelope` and surfaced as a raw 500 with an
# opaque stderr string, so the operator never got a permission card.
#
# NOTE: -25211 is emitted by System Events for the *bulk* window query
# form (`every window of every process whose visible is true`) even when
# assistive access IS granted. That is a System Events quirk, not a real
# denial, which is why `gui_computer_use` no longer uses that form. The
# patterns below stay because a genuine denial reports identically and
# there is no way to tell them apart from stderr alone.
_ACCESSIBILITY_DENIAL_PATTERNS = [
    re.compile(r"not allowed assistive access", re.IGNORECASE),
    re.compile(r"not allowed to send keystrokes", re.IGNORECASE),
    re.compile(r"-25211"),
    re.compile(r"errAXAPIDisabled"),
]


def _is_accessibility_denial(stderr: str) -> bool:
    """True when osascript stderr reports an Accessibility denial."""
    return any(p.search(stderr or "") for p in _ACCESSIBILITY_DENIAL_PATTERNS)

# Friendly name → bundle ID. Keep in sync with
# `security.macos_permissions.DESKTOP_CONTROL_TARGETS` so callers can
# pass the bundle straight to `check_automation_for`.
_NAME_TO_BUNDLE = {
    "FaceTime": "com.apple.FaceTime",
    "Music": "com.apple.Music",
    "Mail": "com.apple.Mail",
    "Notes": "com.apple.Notes",
    "Messages": "com.apple.MobileSMS",
    "Reminders": "com.apple.Reminders",
    "Calendar": "com.apple.iCal",
    "iCal": "com.apple.iCal",
    "Safari": "com.apple.Safari",
    "Finder": "com.apple.Finder",
    "System Events": "com.apple.systemevents",
}


def _resolve_denial_target(stderr: str, default_bundle: Optional[str]) -> Optional[str]:
    """Best-effort: extract the Automation target bundle id from
    osascript stderr. Falls back to ``default_bundle`` when the
    caller passed one but the stderr text was opaque."""
    for pattern in _AUTO_DENIAL_PATTERNS:
        m = pattern.search(stderr)
        if not m:
            continue
        if m.groups():
            name = m.group(1).strip().strip("\"'")
            return _NAME_TO_BUNDLE.get(name, default_bundle or name)
        return default_bundle
    return None


def run_applescript(
    script: str,
    *,
    timeout_s: float = 10.0,
    target_bundle: Optional[str] = None,
) -> AppleScriptResult:
    """Execute ``script`` via ``osascript`` and return a structured
    result.

    Parameters
    ----------
    script : str
        AppleScript source. Multi-line OK.
    timeout_s : float
        Hard subprocess timeout. The orchestrator's per-tool timeout
        is typically 30s; defaulting to 10s here keeps a misbehaving
        Mac app from blocking a whole orchestrator turn.
    target_bundle : str, optional
        The macOS bundle id the script is trying to control. Used as
        a fallback when stderr doesn't name the target explicitly so
        the resulting ``tcc_card`` can still deeplink to the right
        Settings row. Pass for every facade call; leave None for
        generic AppleScript that doesn't tell another app to do
        something (e.g. pure ``System Events`` keystrokes).

    Returns
    -------
    AppleScriptResult
        Always returns; never raises (except ``AppleScriptUnsupportedPlatform``).
    """
    if platform.system() != "Darwin":
        raise AppleScriptUnsupportedPlatform(
            "run_applescript is macOS-only; got platform="
            + platform.system()
        )

    import time
    start = time.monotonic()
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        return AppleScriptResult(
            success=False,
            stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
            stderr=f"osascript timed out after {timeout_s}s",
            exit_code=124,
            duration_ms=int((time.monotonic() - start) * 1000),
        )
    except FileNotFoundError:
        return AppleScriptResult(
            success=False,
            stdout="",
            stderr="osascript binary not found on PATH",
            exit_code=127,
            duration_ms=int((time.monotonic() - start) * 1000),
        )
    duration_ms = int((time.monotonic() - start) * 1000)

    if proc.returncode == 0:
        return AppleScriptResult(
            success=True,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            exit_code=0,
            duration_ms=duration_ms,
        )

    stderr_text = proc.stderr or ""
    tcc_bundle = _resolve_denial_target(stderr_text, target_bundle)
    tcc_permission = "accessibility" if _is_accessibility_denial(stderr_text) else None
    return AppleScriptResult(
        success=False,
        stdout=proc.stdout or "",
        stderr=stderr_text,
        exit_code=proc.returncode,
        duration_ms=duration_ms,
        tcc_target_bundle=tcc_bundle,
        tcc_permission=tcc_permission,
    )


__all__ = [
    "AppleScriptResult",
    "AppleScriptUnsupportedPlatform",
    "run_applescript",
    "_is_accessibility_denial",
]
