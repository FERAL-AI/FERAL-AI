"""
macOS TCC permission probes for FERAL's GUI / vision computer-use stack.

Two privacy-protected entitlements gate everything FERAL needs to drive
a Mac:

* **Accessibility** — required by ``pyautogui`` (and any synthetic
  click/keystroke) so the OS will accept input events from a
  non-Apple-signed process. Apple's API: ``AXIsProcessTrustedWithOptions``.
* **Screen Recording** — required by ``screencapture`` and ``CGWindowList``
  to see anything beyond the menu bar wallpaper. Apple's API:
  ``CGPreflightScreenCaptureAccess``.

We deliberately do NOT call ``tccutil``: that tool resets the privacy
database from the command line and does not reliably *read* the current
grant state for an arbitrary process. The only honest readout is via
the ApplicationServices / Quartz APIs themselves, gated behind PyObjC.

If PyObjC isn't installed, we say so in ``status="unknown"`` and surface
the exact remediation step (``pip install pyobjc-framework-ApplicationServices
pyobjc-framework-Quartz``) — never a green checkmark masquerading as
real availability.

This module is import-safe on every platform: on non-Darwin hosts the
probe returns ``status="not_applicable"`` immediately so callers don't
need to branch.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class TCCStatus:
    """Result of a single TCC probe.

    * ``permission`` — short name (``accessibility`` | ``screen_recording``).
    * ``status`` — one of:
        - ``granted`` — Apple's API confirmed access.
        - ``denied`` — API returned False/0 (no access).
        - ``unknown`` — PyObjC missing or the API raised; we cannot tell.
        - ``not_applicable`` — not running on macOS.
    * ``api`` — the underlying API used (e.g. ``AXIsProcessTrustedWithOptions``).
    * ``setup_step`` — exact human/CLI instruction to remediate.
    * ``error`` — diagnostic detail when ``status`` is ``unknown``.
    """

    permission: str
    status: str
    api: str
    setup_step: str
    error: Optional[str] = None

    def to_dict(self) -> dict:
        out = {
            "permission": self.permission,
            "status": self.status,
            "api": self.api,
            "setup_step": self.setup_step,
        }
        if self.error:
            out["error"] = self.error
        return out


_ACCESSIBILITY_REMEDIATION = (
    "Open System Settings -> Privacy & Security -> Accessibility, "
    "click the lock to unlock, and enable the FERAL host process "
    "(usually 'Terminal', 'iTerm', or your launching app). Restart "
    "FERAL afterwards so the new grant takes effect for the running "
    "process."
)

_SCREEN_RECORDING_REMEDIATION = (
    "Open System Settings -> Privacy & Security -> Screen Recording, "
    "click the lock to unlock, and enable the FERAL host process. "
    "macOS forces a quit-and-relaunch of the granted app the first "
    "time you enable Screen Recording — restart FERAL after toggling."
)

_PYOBJC_REMEDIATION_AX = (
    "Install PyObjC ApplicationServices bindings to enable an honest "
    "Accessibility readout: pip install pyobjc-framework-ApplicationServices"
)

_PYOBJC_REMEDIATION_SR = (
    "Install PyObjC Quartz bindings to enable an honest Screen Recording "
    "readout: pip install pyobjc-framework-Quartz"
)


def _not_applicable(name: str, api: str) -> TCCStatus:
    return TCCStatus(
        permission=name,
        status="not_applicable",
        api=api,
        setup_step="Skipped: macOS-only permission",
    )


def check_accessibility() -> TCCStatus:
    """Probe Accessibility (synthetic input) entitlement.

    Uses ``AXIsProcessTrustedWithOptions`` with the prompt option
    explicitly disabled — we never want a doctor probe to silently
    pop a system permission dialog.
    """
    if platform.system() != "Darwin":
        return _not_applicable("accessibility", "AXIsProcessTrustedWithOptions")

    try:
        # `HIServices` is the public umbrella for AX in modern macOS;
        # the legacy import path lives under `ApplicationServices`.
        from ApplicationServices import (  # type: ignore[import-not-found]
            AXIsProcessTrustedWithOptions,
            kAXTrustedCheckOptionPrompt,
        )
        from CoreFoundation import (  # type: ignore[import-not-found]
            CFDictionaryCreate,
            kCFTypeDictionaryKeyCallBacks,
            kCFTypeDictionaryValueCallBacks,
            kCFBooleanFalse,
        )
    except ImportError as exc:
        return TCCStatus(
            permission="accessibility",
            status="unknown",
            api="AXIsProcessTrustedWithOptions",
            setup_step=_PYOBJC_REMEDIATION_AX,
            error=f"PyObjC ApplicationServices not importable: {exc}",
        )

    try:
        options = CFDictionaryCreate(
            None,
            (kAXTrustedCheckOptionPrompt,),
            (kCFBooleanFalse,),
            1,
            kCFTypeDictionaryKeyCallBacks,
            kCFTypeDictionaryValueCallBacks,
        )
        granted = bool(AXIsProcessTrustedWithOptions(options))
    except Exception as exc:  # PyObjC sometimes raises on framework issues
        return TCCStatus(
            permission="accessibility",
            status="unknown",
            api="AXIsProcessTrustedWithOptions",
            setup_step=_ACCESSIBILITY_REMEDIATION,
            error=f"AX probe raised: {exc}",
        )

    if granted:
        return TCCStatus(
            permission="accessibility",
            status="granted",
            api="AXIsProcessTrustedWithOptions",
            setup_step="(no action needed)",
        )
    return TCCStatus(
        permission="accessibility",
        status="denied",
        api="AXIsProcessTrustedWithOptions",
        setup_step=_ACCESSIBILITY_REMEDIATION,
    )


def check_screen_recording() -> TCCStatus:
    """Probe Screen Recording entitlement.

    Uses ``CGPreflightScreenCaptureAccess`` from Quartz: this returns a
    boolean without prompting the user, which is exactly what a doctor
    needs.
    """
    if platform.system() != "Darwin":
        return _not_applicable("screen_recording", "CGPreflightScreenCaptureAccess")

    try:
        from Quartz import (  # type: ignore[import-not-found]
            CGPreflightScreenCaptureAccess,
        )
    except ImportError as exc:
        return TCCStatus(
            permission="screen_recording",
            status="unknown",
            api="CGPreflightScreenCaptureAccess",
            setup_step=_PYOBJC_REMEDIATION_SR,
            error=f"PyObjC Quartz not importable: {exc}",
        )

    try:
        granted = bool(CGPreflightScreenCaptureAccess())
    except Exception as exc:
        return TCCStatus(
            permission="screen_recording",
            status="unknown",
            api="CGPreflightScreenCaptureAccess",
            setup_step=_SCREEN_RECORDING_REMEDIATION,
            error=f"CG probe raised: {exc}",
        )

    if granted:
        return TCCStatus(
            permission="screen_recording",
            status="granted",
            api="CGPreflightScreenCaptureAccess",
            setup_step="(no action needed)",
        )
    return TCCStatus(
        permission="screen_recording",
        status="denied",
        api="CGPreflightScreenCaptureAccess",
        setup_step=_SCREEN_RECORDING_REMEDIATION,
    )


_AUTOMATION_REMEDIATION_TMPL = (
    "Open System Settings -> Privacy & Security -> Automation, click "
    "the lock to unlock, and enable the FERAL host process's row for "
    "'{target}'. macOS adds the row the first time FERAL asks to "
    "control {target}; if you don't see it yet, retry the action so "
    "the dialog appears, then approve."
)


def check_automation_for(target_bundle_id: str) -> TCCStatus:
    """Probe Automation entitlement for a specific target app bundle.

    Phase 11 (audit-r10 overhaul). AppleScript-driven control of
    FaceTime / Music / Mail / Notes / Messages / Reminders / Calendar
    on macOS 10.14+ requires per-target Automation grants under the
    Privacy & Security pane. There is no single "Automation: on"
    flag — each scripted app gets its own row.

    macOS doesn't expose a public Boolean preflight for Automation,
    only a side-effecting test: sending a benign AppleEvent and
    catching the ``errAEEventNotPermitted`` (-1743) result. We do
    NOT execute that probe here because it would prompt the user
    every time the doctor runs. Instead we read the cached value
    from a previous tool invocation in
    `state.desktop_control_tcc_cache` when available, otherwise
    return ``unknown`` with the structured remediation step.

    Callers that genuinely need a live readout call the AppleScript
    runner once and check the result envelope.
    """
    if platform.system() != "Darwin":
        return _not_applicable(f"automation:{target_bundle_id}", "AEDeterminePermissionToAutomateTarget")
    return TCCStatus(
        permission=f"automation:{target_bundle_id}",
        status="unknown",
        api="AEDeterminePermissionToAutomateTarget",
        setup_step=_AUTOMATION_REMEDIATION_TMPL.format(target=_friendly_name(target_bundle_id)),
        error=(
            "Automation grants are per-target and have no public "
            "preflight Boolean; status is resolved at first invoke."
        ),
    )


_FRIENDLY_NAMES = {
    "com.apple.FaceTime": "FaceTime",
    "com.apple.Music": "Music",
    "com.apple.Mail": "Mail",
    "com.apple.Notes": "Notes",
    "com.apple.MobileSMS": "Messages",
    "com.apple.Reminders": "Reminders",
    "com.apple.iCal": "Calendar",
    "com.apple.Safari": "Safari",
    "com.apple.Finder": "Finder",
    "com.apple.systemevents": "System Events",
}


def _friendly_name(bundle_id: str) -> str:
    return _FRIENDLY_NAMES.get(bundle_id, bundle_id)


def all_gui_permission_statuses() -> list[TCCStatus]:
    """Convenience wrapper that returns every GUI-relevant TCC probe.

    audit-r12 ship — adds Calendar / Reminders / Contacts / Full Disk
    Access alongside the original Accessibility + Screen Recording
    pair so ``feral doctor`` (consumed by Lane 07) and the iOS Brain
    Network row can surface every TCC the brain actually exercises
    against, not just the two macOS provides a public preflight for.
    """
    return [
        check_accessibility(),
        check_screen_recording(),
        check_calendar(),
        check_reminders(),
        check_contacts(),
        check_full_disk_access(),
    ]


# Bundle IDs the brain's desktop_control facade will script. Probed
# eagerly by `all_desktop_control_permission_statuses` so the iOS
# Brain Network section can render a row per target right next to
# Accessibility + Screen Recording.
DESKTOP_CONTROL_TARGETS = (
    "com.apple.FaceTime",
    "com.apple.Music",
    "com.apple.Mail",
    "com.apple.Notes",
    "com.apple.MobileSMS",
    "com.apple.Reminders",
    "com.apple.iCal",
    "com.apple.Safari",
)


def all_desktop_control_permission_statuses() -> list[TCCStatus]:
    """Union of GUI TCC probes + the Automation targets the brain's
    desktop_control facade can script. Used by the Phase 11
    `/api/system/permissions` endpoint."""
    out: list[TCCStatus] = list(all_gui_permission_statuses())
    for bundle in DESKTOP_CONTROL_TARGETS:
        out.append(check_automation_for(bundle))
    return out


_CALENDAR_REMEDIATION = (
    "Open System Settings -> Privacy & Security -> Calendars, click "
    "the lock to unlock, and enable the FERAL host process. macOS "
    "will populate the row the first time FERAL asks to read "
    "calendar events."
)

_REMINDERS_REMEDIATION = (
    "Open System Settings -> Privacy & Security -> Reminders, click "
    "the lock to unlock, and enable the FERAL host process."
)

_CONTACTS_REMEDIATION = (
    "Open System Settings -> Privacy & Security -> Contacts, click "
    "the lock to unlock, and enable the FERAL host process."
)

_FULL_DISK_ACCESS_REMEDIATION = (
    "Open System Settings -> Privacy & Security -> Full Disk Access, "
    "click the lock to unlock, click '+' and add the FERAL host "
    "process (Terminal / iTerm / your launching app). macOS forces a "
    "quit-and-relaunch of the granted app the first time you enable "
    "FDA — restart FERAL after toggling."
)


def _eventkit_status(entity_type_name: str) -> tuple[str, Optional[str]]:
    """Return ``(status, error)`` from ``EKEventStore.authorizationStatusForEntityType``.

    macOS EventKit publishes the authorization status as one of:

    * ``EKAuthorizationStatusNotDetermined`` (``0``) — no decision yet
    * ``EKAuthorizationStatusRestricted`` (``1``) — parental controls / MDM
    * ``EKAuthorizationStatusDenied`` (``2``) — user said no
    * ``EKAuthorizationStatusAuthorized`` (``3``) — granted (or
      ``EKAuthorizationStatusFullAccess`` ``3`` / ``WriteOnly`` ``4``
      on macOS 14+; we treat any positive grant as ``granted``).
    """
    try:
        from EventKit import EKEventStore  # type: ignore[import-not-found]
    except ImportError as exc:
        return "unknown", f"PyObjC EventKit not importable: {exc}"
    try:
        entity_type = {"event": 0, "reminder": 1}[entity_type_name]
        status = EKEventStore.authorizationStatusForEntityType_(entity_type)
    except Exception as exc:
        return "unknown", f"EventKit probe raised: {exc}"
    # 3 = legacy authorized; 3/4 on macOS 14+ = full / write-only — both count.
    if status in (3, 4):
        return "granted", None
    if status == 2:
        return "denied", None
    if status == 1:
        return "restricted", None
    return "denied", None  # not-determined treated as denied until the user grants


def check_calendar() -> TCCStatus:
    """Probe Calendar (EventKit) entitlement.

    Uses ``EKEventStore.authorizationStatusForEntityType_(EKEntityTypeEvent)``,
    which is a non-prompting read. PyObjC must be installed to resolve
    the framework; without it we return ``unknown`` with the install
    hint instead of guessing.
    """
    if platform.system() != "Darwin":
        return _not_applicable("calendar", "EKEventStore.authorizationStatusForEntityType")
    status, error = _eventkit_status("event")
    if status == "granted":
        return TCCStatus(
            permission="calendar",
            status="granted",
            api="EKEventStore.authorizationStatusForEntityType",
            setup_step="(no action needed)",
        )
    return TCCStatus(
        permission="calendar",
        status=status,
        api="EKEventStore.authorizationStatusForEntityType",
        setup_step=_CALENDAR_REMEDIATION,
        error=error,
    )


def check_reminders() -> TCCStatus:
    """Probe Reminders (EventKit) entitlement.

    Same API surface as :func:`check_calendar` but with
    ``EKEntityTypeReminder``. macOS 14+ split this from the Calendar
    grant so they need separate doctor rows.
    """
    if platform.system() != "Darwin":
        return _not_applicable("reminders", "EKEventStore.authorizationStatusForEntityType")
    status, error = _eventkit_status("reminder")
    if status == "granted":
        return TCCStatus(
            permission="reminders",
            status="granted",
            api="EKEventStore.authorizationStatusForEntityType",
            setup_step="(no action needed)",
        )
    return TCCStatus(
        permission="reminders",
        status=status,
        api="EKEventStore.authorizationStatusForEntityType",
        setup_step=_REMINDERS_REMEDIATION,
        error=error,
    )


def check_contacts() -> TCCStatus:
    """Probe Contacts entitlement via
    ``CNContactStore.authorizationStatusForEntityType``.

    Non-prompting read; requires PyObjC's ``Contacts`` framework
    bindings.
    """
    if platform.system() != "Darwin":
        return _not_applicable("contacts", "CNContactStore.authorizationStatusForEntityType")
    try:
        from Contacts import CNContactStore  # type: ignore[import-not-found]
    except ImportError as exc:
        return TCCStatus(
            permission="contacts",
            status="unknown",
            api="CNContactStore.authorizationStatusForEntityType",
            setup_step=(
                "Install PyObjC Contacts bindings: "
                "pip install pyobjc-framework-Contacts"
            ),
            error=f"PyObjC Contacts not importable: {exc}",
        )
    try:
        status = CNContactStore.authorizationStatusForEntityType_(0)
    except Exception as exc:
        return TCCStatus(
            permission="contacts",
            status="unknown",
            api="CNContactStore.authorizationStatusForEntityType",
            setup_step=_CONTACTS_REMEDIATION,
            error=f"Contacts probe raised: {exc}",
        )
    # 3 = legacy authorized; 4 = limited (macOS 14+) — both count as
    # "FERAL can read", which is what the doctor cares about.
    if status in (3, 4):
        return TCCStatus(
            permission="contacts",
            status="granted",
            api="CNContactStore.authorizationStatusForEntityType",
            setup_step="(no action needed)",
        )
    if status == 2:
        return TCCStatus(
            permission="contacts",
            status="denied",
            api="CNContactStore.authorizationStatusForEntityType",
            setup_step=_CONTACTS_REMEDIATION,
        )
    if status == 1:
        return TCCStatus(
            permission="contacts",
            status="restricted",
            api="CNContactStore.authorizationStatusForEntityType",
            setup_step=_CONTACTS_REMEDIATION,
        )
    return TCCStatus(
        permission="contacts",
        status="denied",
        api="CNContactStore.authorizationStatusForEntityType",
        setup_step=_CONTACTS_REMEDIATION,
    )


def check_full_disk_access() -> TCCStatus:
    """Best-effort probe for Full Disk Access.

    There is no public preflight Boolean for FDA on macOS — TCC
    deliberately doesn't expose one. The convention is to attempt a
    read of a TCC-protected file and observe success/failure. We try
    ``~/Library/Application Support/com.apple.TCC/TCC.db`` (the TCC
    DB itself; an FDA-granted process can ``os.access(... os.R_OK)``,
    a non-FDA process gets ``False`` without prompting).

    The probe is read-only and bounded by ``os.access`` — no file is
    opened, no side effects. On non-macOS we return ``not_applicable``.
    """
    import os as _os

    if platform.system() != "Darwin":
        return _not_applicable("full_disk_access", "os.access ~/Library/.../TCC.db")
    tcc_db = Path(
        _os.path.expanduser("~/Library/Application Support/com.apple.TCC/TCC.db")
    )
    if not tcc_db.exists():
        return TCCStatus(
            permission="full_disk_access",
            status="unknown",
            api="os.access ~/Library/.../TCC.db",
            setup_step=_FULL_DISK_ACCESS_REMEDIATION,
            error="TCC database path missing — install path probe inconclusive",
        )
    try:
        readable = _os.access(str(tcc_db), _os.R_OK)
    except OSError as exc:
        return TCCStatus(
            permission="full_disk_access",
            status="unknown",
            api="os.access ~/Library/.../TCC.db",
            setup_step=_FULL_DISK_ACCESS_REMEDIATION,
            error=f"FDA probe raised: {exc}",
        )
    if readable:
        return TCCStatus(
            permission="full_disk_access",
            status="granted",
            api="os.access ~/Library/.../TCC.db",
            setup_step="(no action needed)",
        )
    return TCCStatus(
        permission="full_disk_access",
        status="denied",
        api="os.access ~/Library/.../TCC.db",
        setup_step=_FULL_DISK_ACCESS_REMEDIATION,
    )


__all__ = [
    "TCCStatus",
    "check_accessibility",
    "check_screen_recording",
    "check_automation_for",
    "check_calendar",
    "check_reminders",
    "check_contacts",
    "check_full_disk_access",
    "all_gui_permission_statuses",
    "all_desktop_control_permission_statuses",
    "DESKTOP_CONTROL_TARGETS",
]
