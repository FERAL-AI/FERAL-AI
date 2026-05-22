"""Phase 11 (audit-r10 overhaul) — tcc_card SDUI flow for Mac TCC denials.

Mac-side mirror of ``agents/permission_card.py`` (Phase 6 iOS
permission denial cards). When the brain's ``desktop_control``
facade tries to drive a Mac app via AppleScript and macOS rejects
the Automation request, the AppleScript runner returns an error
string shaped ``tcc_denied:<permission>``. This module turns that
string into a structured ``tcc_card`` SDUI element the iOS chat
client (and Phase 7b web) render with the right macOS Settings
URL-scheme deeplink — and, when running on macOS, also fires
``open`` against that URL so the relevant Settings pane comes to
the foreground on the operator's Mac.

Wire shape (one entry as ``FeralMessage(type="sdui", payload={"root": ...})``)::

    {
      "type": "tcc_card",
      "permission_key": "automation:com.apple.FaceTime",
      "title": "FERAL needs Automation access to FaceTime",
      "description": "...",
      "macos_deeplink": "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation",
      "macos_deeplink_label": "Open System Settings",
      "skill_id": "desktop_facetime",
      "action": "desktop.facetime.start",
      "retryable": true
    }

The iOS PermissionCardView from Phase 7a renders this with a
read-only "macOS · open Settings on your Mac" hint — the iPhone
can't deeplink to the operator's Mac, but the Mac side opens the
pane itself when this card is minted.
"""
from __future__ import annotations

import logging
import platform
import subprocess
from typing import Optional

logger = logging.getLogger("feral.tcc_card")


_PREFIX = "tcc_denied:"


# Bundle id (or non-Automation key like "accessibility") → card copy.
# Keep titles short — iOS / web rendering caps them visually.
TCC_CATALOG: dict[str, dict] = {
    "accessibility": {
        "title": "FERAL needs Accessibility access on the Mac",
        "description": (
            "Synthetic keystrokes, mouse clicks, and any "
            "non-AppleScript GUI automation require Accessibility "
            "trust for the brain's host process."
        ),
        "macos_deeplink": (
            "x-apple.systempreferences:com.apple.preference.security?"
            "Privacy_Accessibility"
        ),
    },
    "screen_recording": {
        "title": "FERAL needs Screen Recording access on the Mac",
        "description": (
            "Reading the screen, capturing windows for vision input, "
            "and listing on-screen elements all require Screen "
            "Recording entitlement."
        ),
        "macos_deeplink": (
            "x-apple.systempreferences:com.apple.preference.security?"
            "Privacy_ScreenCapture"
        ),
    },
    "full_disk_access": {
        "title": "FERAL needs Full Disk Access on the Mac",
        "description": (
            "Some FERAL skills read files outside the app sandbox "
            "(Mail, Messages databases, Library/Containers). Grant "
            "Full Disk Access to the brain's host process."
        ),
        "macos_deeplink": (
            "x-apple.systempreferences:com.apple.preference.security?"
            "Privacy_AllFiles"
        ),
    },
    # audit-r12 ship — Calendar / Reminders / Contacts get their own
    # deeplink rows alongside the original Accessibility / Screen
    # Recording / FDA entries so the new
    # ``macos_permissions.check_calendar`` / ``check_reminders`` /
    # ``check_contacts`` probes can mint a structured card on denial
    # (consumed by the iOS PermissionCardView + Wave 3 Lane 07's
    # ``feral doctor``).
    "calendar": {
        "title": "FERAL needs Calendar access on the Mac",
        "description": (
            "Calendar reads (event listing, conflict checks, ambient "
            "morning briefing) require an EventKit grant. macOS will "
            "add the row the first time FERAL asks; toggle it on in "
            "System Settings -> Privacy & Security -> Calendars."
        ),
        "macos_deeplink": (
            "x-apple.systempreferences:com.apple.preference.security?"
            "Privacy_Calendars"
        ),
    },
    "reminders": {
        "title": "FERAL needs Reminders access on the Mac",
        "description": (
            "macOS 14 split Reminders out of the Calendar grant. "
            "Toggle FERAL on in System Settings -> Privacy & "
            "Security -> Reminders."
        ),
        "macos_deeplink": (
            "x-apple.systempreferences:com.apple.preference.security?"
            "Privacy_Reminders"
        ),
    },
    "contacts": {
        "title": "FERAL needs Contacts access on the Mac",
        "description": (
            "Contacts reads (resolving \"text my brother\" to a "
            "phone number, listing recent senders) require a "
            "Contacts grant. Toggle FERAL on in System Settings -> "
            "Privacy & Security -> Contacts."
        ),
        "macos_deeplink": (
            "x-apple.systempreferences:com.apple.preference.security?"
            "Privacy_Contacts"
        ),
    },
}

_FRIENDLY_BUNDLE_NAMES = {
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


def _automation_card(bundle_id: str) -> dict:
    friendly = _FRIENDLY_BUNDLE_NAMES.get(bundle_id, bundle_id)
    return {
        "title": f"FERAL needs Automation access to {friendly}",
        "description": (
            f"macOS asks per-target permission to script {friendly}. "
            "Toggle the FERAL host process's row for "
            f"\u201c{friendly}\u201d in System Settings -> Privacy & "
            "Security -> Automation, then retry."
        ),
        "macos_deeplink": (
            "x-apple.systempreferences:com.apple.preference.security?"
            "Privacy_Automation"
        ),
    }


def parse_tcc_error(error: str | None) -> Optional[str]:
    """Return the permission key from a ``tcc_denied:<key>`` error
    string, or ``None`` when the error doesn't match the contract.

    Examples
    --------
    >>> parse_tcc_error("tcc_denied:automation:com.apple.FaceTime")
    'automation:com.apple.FaceTime'
    >>> parse_tcc_error("tcc_denied:accessibility")
    'accessibility'
    >>> parse_tcc_error("some other error") is None
    True
    """
    if not error or not isinstance(error, str):
        return None
    if not error.startswith(_PREFIX):
        return None
    key = error[len(_PREFIX):].strip()
    return key or None


def build_tcc_card(
    permission_key: str,
    *,
    skill_id: str = "",
    action: str = "",
    original_error: str = "",
    retryable: bool = True,
    open_settings_on_mac: bool = True,
) -> dict:
    """Construct a tcc_card SDUI dict.

    Side effect (when ``open_settings_on_mac=True`` AND the brain is
    running on macOS): fire ``open <macos_deeplink>`` so the relevant
    Settings pane is already in front of the operator by the time
    they look up from the iOS chat. Best-effort; failures are logged
    but never raise.
    """
    if permission_key.startswith("automation:"):
        bundle = permission_key.split(":", 1)[1]
        catalog_entry = _automation_card(bundle)
    else:
        catalog_entry = TCC_CATALOG.get(permission_key)

    if not catalog_entry:
        catalog_entry = {
            "title": "FERAL needs a macOS permission",
            "description": (
                "macOS reported that FERAL doesn't have the "
                "permission needed to complete this action. Open "
                "System Settings -> Privacy & Security and grant the "
                "requested access, then ask again."
            ),
            "macos_deeplink": (
                "x-apple.systempreferences:com.apple.preference.security"
            ),
        }

    card = {
        "type": "tcc_card",
        "permission_key": permission_key,
        "title": catalog_entry["title"],
        "description": catalog_entry["description"],
        "macos_deeplink": catalog_entry["macos_deeplink"],
        "macos_deeplink_label": "Open System Settings",
        "retryable": bool(retryable),
    }
    if skill_id:
        card["skill_id"] = skill_id
    if action:
        card["action"] = action
    if original_error:
        card["_original_error"] = original_error

    if open_settings_on_mac and platform.system() == "Darwin":
        try:
            subprocess.run(
                ["open", catalog_entry["macos_deeplink"]],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("tcc_card: opening macOS settings pane failed: %s", exc)

    return card


def card_for_action_result(
    result: dict,
    *,
    skill_id: str = "",
    action: str = "",
) -> Optional[dict]:
    """Inspect a brain-host action result envelope; return a tcc_card
    iff the envelope carries a ``tcc_denied:`` error.

    Returns ``None`` otherwise. Designed as a drop-in companion to
    ``permission_card.card_for_action_result`` so
    ``execute_capability_action`` can probe both in sequence.
    """
    if not isinstance(result, dict):
        return None
    if result.get("success") is True:
        return None
    err = result.get("error")
    key = parse_tcc_error(err)
    if key is None:
        return None
    return build_tcc_card(
        key,
        skill_id=skill_id,
        action=action,
        original_error=err or "",
    )


__all__ = [
    "TCC_CATALOG",
    "build_tcc_card",
    "card_for_action_result",
    "parse_tcc_error",
]
