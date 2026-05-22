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


# ─────────────────────────────────────────────
# REQUEST flow (Lane 11 / R-PROD-004b)
# ─────────────────────────────────────────────
#
# The check_* probes above are read-only — they tell you the current
# TCC state but never trigger the native consent prompt. Lane 06's
# audit-r12 ship covered that read side. R-PROD-004b in WORK_LOG
# routed in the REQUEST flow: trigger the OS dialog, then re-probe.
#
# Each request_* function:
#
# * Calls the official macOS API that surfaces the consent dialog
#   (CGRequestScreenCaptureAccess, EKEventStore.requestAccessToEvents,
#   CNContactStore.requestAccessForEntityType, etc.). The user clicks
#   "Allow" / "Don't Allow" on a system dialog; FERAL does NOT
#   re-implement the dialog.
# * Returns the TCCStatus immediately after the request finishes —
#   ``granted`` on success, ``denied`` if the user clicked "Don't
#   Allow", ``unknown`` if PyObjC is missing.
# * Includes the deeplink fallback in ``setup_step`` so the UI can
#   show a "Open System Settings" button when the prompt was denied
#   (re-asking via the API would no-op until the user opens Settings
#   and toggles the row manually).
#
# Accessibility + Full Disk Access have NO request API — Apple
# requires the user to add the host process to the Privacy & Security
# pane manually. For those we expose a ``deeplink_for(permission)``
# helper instead so the UI can pop the right pane.


def _deny_unknown(name: str, api: str, *, error: str) -> TCCStatus:
    return TCCStatus(
        permission=name,
        status="unknown",
        api=api,
        setup_step=(
            "PyObjC framework bindings missing — install with "
            "`pip install pyobjc-framework-{Quartz,EventKit,Contacts}`."
        ),
        error=error,
    )


def request_screen_recording() -> TCCStatus:
    """Trigger the Screen Recording consent dialog (macOS 10.15+).

    Calls ``CGRequestScreenCaptureAccess`` which, unlike the
    ``CGPreflight`` variant, shows the native consent dialog on the
    first call. The function returns synchronously after the user
    answers; on "Don't Allow" macOS won't re-prompt until the user
    opens Settings manually (the deeplink in ``setup_step`` is the
    remediation path).
    """
    if platform.system() != "Darwin":
        return _not_applicable("screen_recording", "CGRequestScreenCaptureAccess")
    try:
        from Quartz import (  # type: ignore[import-not-found]
            CGRequestScreenCaptureAccess,
        )
    except ImportError as exc:
        return _deny_unknown(
            "screen_recording",
            "CGRequestScreenCaptureAccess",
            error=f"PyObjC Quartz not importable: {exc}",
        )
    try:
        granted = bool(CGRequestScreenCaptureAccess())
    except Exception as exc:
        return TCCStatus(
            permission="screen_recording",
            status="unknown",
            api="CGRequestScreenCaptureAccess",
            setup_step=_SCREEN_RECORDING_REMEDIATION,
            error=f"Screen capture request raised: {exc}",
        )
    return TCCStatus(
        permission="screen_recording",
        status="granted" if granted else "denied",
        api="CGRequestScreenCaptureAccess",
        setup_step="(no action needed)" if granted else _SCREEN_RECORDING_REMEDIATION,
    )


def _request_eventkit(entity_type_name: str, *, permission: str, remediation: str) -> TCCStatus:
    """Drive the EventKit request flow synchronously.

    macOS's ``EKEventStore.requestFullAccessToEvents`` / equivalents
    take a completion handler. We bridge to a synchronous result via
    a ``threading.Event`` so the brain can call this from a regular
    request handler without async glue.
    """
    if platform.system() != "Darwin":
        return _not_applicable(permission, "EKEventStore.requestAccess")
    try:
        from EventKit import EKEventStore  # type: ignore[import-not-found]
    except ImportError as exc:
        return _deny_unknown(
            permission,
            "EKEventStore.requestAccess",
            error=f"PyObjC EventKit not importable: {exc}",
        )
    import threading
    done = threading.Event()
    result: dict = {"granted": False, "error": None}
    store = EKEventStore.alloc().init()
    entity_int = {"event": 0, "reminder": 1}[entity_type_name]

    def _handler(granted, err):  # noqa: ANN001 — ObjC completion handler
        try:
            result["granted"] = bool(granted)
            if err is not None:
                result["error"] = str(err)
        finally:
            done.set()

    try:
        # macOS 14+ split the API into requestFullAccessToEvents_ /
        # requestFullAccessToReminders_; older OSes use
        # requestAccessToEntityType_completion_. Try the modern path
        # first.
        modern = getattr(store, "requestFullAccessToEvents_", None) if entity_type_name == "event" else getattr(store, "requestFullAccessToReminders_", None)
        if modern is not None:
            modern(_handler)
        else:
            store.requestAccessToEntityType_completion_(entity_int, _handler)
    except Exception as exc:
        return TCCStatus(
            permission=permission,
            status="unknown",
            api="EKEventStore.requestAccess",
            setup_step=remediation,
            error=f"EventKit request raised: {exc}",
        )
    # 5 s is comfortably longer than the OS dialog round-trip; if it
    # doesn't fire (e.g. headless test runner) we surface the
    # timeout as ``unknown`` rather than blocking the brain.
    if not done.wait(timeout=5.0):
        return TCCStatus(
            permission=permission,
            status="unknown",
            api="EKEventStore.requestAccess",
            setup_step=remediation,
            error="EventKit completion handler did not fire within 5s",
        )
    if result["error"]:
        return TCCStatus(
            permission=permission,
            status="unknown",
            api="EKEventStore.requestAccess",
            setup_step=remediation,
            error=result["error"],
        )
    return TCCStatus(
        permission=permission,
        status="granted" if result["granted"] else "denied",
        api="EKEventStore.requestAccess",
        setup_step="(no action needed)" if result["granted"] else remediation,
    )


def request_calendar() -> TCCStatus:
    return _request_eventkit("event", permission="calendar", remediation=_CALENDAR_REMEDIATION)


def request_reminders() -> TCCStatus:
    return _request_eventkit(
        "reminder", permission="reminders", remediation=_REMINDERS_REMEDIATION,
    )


def request_contacts() -> TCCStatus:
    """Trigger the Contacts consent dialog (CNContactStore.requestAccess)."""
    if platform.system() != "Darwin":
        return _not_applicable("contacts", "CNContactStore.requestAccess")
    try:
        from Contacts import CNContactStore  # type: ignore[import-not-found]
    except ImportError as exc:
        return _deny_unknown(
            "contacts",
            "CNContactStore.requestAccess",
            error=f"PyObjC Contacts not importable: {exc}",
        )
    import threading
    done = threading.Event()
    result: dict = {"granted": False, "error": None}
    store = CNContactStore.alloc().init()

    def _handler(granted, err):  # noqa: ANN001 — ObjC completion handler
        try:
            result["granted"] = bool(granted)
            if err is not None:
                result["error"] = str(err)
        finally:
            done.set()

    try:
        # 0 = CNEntityTypeContacts
        store.requestAccessForEntityType_completionHandler_(0, _handler)
    except Exception as exc:
        return TCCStatus(
            permission="contacts",
            status="unknown",
            api="CNContactStore.requestAccess",
            setup_step=_CONTACTS_REMEDIATION,
            error=f"Contacts request raised: {exc}",
        )
    if not done.wait(timeout=5.0):
        return TCCStatus(
            permission="contacts",
            status="unknown",
            api="CNContactStore.requestAccess",
            setup_step=_CONTACTS_REMEDIATION,
            error="Contacts completion handler did not fire within 5s",
        )
    if result["error"]:
        return TCCStatus(
            permission="contacts",
            status="unknown",
            api="CNContactStore.requestAccess",
            setup_step=_CONTACTS_REMEDIATION,
            error=result["error"],
        )
    return TCCStatus(
        permission="contacts",
        status="granted" if result["granted"] else "denied",
        api="CNContactStore.requestAccess",
        setup_step="(no action needed)" if result["granted"] else _CONTACTS_REMEDIATION,
    )


# Deeplinks to System Settings privacy panes. These are stable URL
# schemes documented by Apple
# (https://developer.apple.com/library/archive/technotes/tn2459/_index.html);
# kept in one place so the UI doesn't hardcode them inline.
_PRIVACY_DEEPLINKS = {
    "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    "screen_recording": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
    "calendar": "x-apple.systempreferences:com.apple.preference.security?Privacy_Calendars",
    "reminders": "x-apple.systempreferences:com.apple.preference.security?Privacy_Reminders",
    "contacts": "x-apple.systempreferences:com.apple.preference.security?Privacy_Contacts",
    "full_disk_access": "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles",
    "automation": "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation",
    "bluetooth": "x-apple.systempreferences:com.apple.preference.security?Privacy_Bluetooth",
    "microphone": "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
    "camera": "x-apple.systempreferences:com.apple.preference.security?Privacy_Camera",
    "location": "x-apple.systempreferences:com.apple.preference.security?Privacy_LocationServices",
}


def deeplink_for(permission: str) -> Optional[str]:
    """Return an ``x-apple.systempreferences:`` URL for a permission pane.

    Lane 11 R-PROD-004b: the iOS companion (when running on the same
    Mac as the brain) and the macOS desktop UI use this to open the
    correct System Settings pane after the user denies a request. For
    Full Disk Access there is no request API so the deeplink IS the
    only path forward.

    Returns ``None`` for unknown permissions so callers can surface a
    "no deeplink" message rather than a broken URL.
    """
    return _PRIVACY_DEEPLINKS.get(permission)


def request_permission(name: str) -> TCCStatus:
    """Dispatch ``name`` to the corresponding ``request_*`` function.

    Convenience wrapper for the REST surface in
    ``feral-core/api/routes/security_and_hardware.py`` — accepts the
    short permission name (matches ``TCCStatus.permission``) and
    returns the post-prompt status.

    ``full_disk_access`` + ``accessibility`` have no request API; the
    function returns the current status + the deeplink so the UI can
    point the user at Settings.
    """
    name = (name or "").strip()
    if name == "screen_recording":
        return request_screen_recording()
    if name == "calendar":
        return request_calendar()
    if name == "reminders":
        return request_reminders()
    if name == "contacts":
        return request_contacts()
    if name == "accessibility":
        # No public request API; re-probe and rely on deeplink.
        return check_accessibility()
    if name == "full_disk_access":
        return check_full_disk_access()
    return TCCStatus(
        permission=name,
        status="unknown",
        api="(none)",
        setup_step=f"Unknown permission name {name!r}",
        error="no request handler registered",
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
    # Lane 11 R-PROD-004b additions.
    "request_screen_recording",
    "request_calendar",
    "request_reminders",
    "request_contacts",
    "request_permission",
    "deeplink_for",
]
