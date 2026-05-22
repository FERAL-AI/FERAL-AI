"""Phase 11 (audit-r10 overhaul) — system permissions REST surface.

Exposes the macOS TCC permission state of the brain process so
clients (iOS BrainNetworkSection, web devices pane, future setup
wizards) can render a structured \u201cgrant / denied / unknown\u201d row per
permission instead of guessing.

Wire shape::

    GET /api/system/permissions
    {
      "platform": "darwin" | "linux" | ...,
      "permissions": [
        {"permission", "status", "api", "setup_step", "error"?}
      ]
    }

Read-only, LAN-public — same posture as ``/api/capabilities``.
"""
from __future__ import annotations

import logging
import platform
import subprocess

from fastapi import APIRouter, Body, HTTPException

from security.macos_permissions import (
    all_desktop_control_permission_statuses,
    deeplink_for,
    request_permission,
)

logger = logging.getLogger("feral.api.system_permissions")

router = APIRouter(tags=["system"])


@router.get("/api/system/permissions")
async def get_system_permissions():
    statuses = all_desktop_control_permission_statuses()
    return {
        "platform": platform.system().lower(),
        "permissions": [s.to_dict() for s in statuses],
    }


@router.post("/api/system/permissions/request")
async def request_system_permission(body: dict = Body(...)):
    """Lane 11 R-PROD-004b — trigger the native macOS consent prompt.

    Wire shape::

        POST /api/system/permissions/request
        { "permission": "screen_recording" | "calendar" | "reminders" |
                        "contacts" | "accessibility" | "full_disk_access" }

    Returns the post-prompt status:

        {
          "permission": "screen_recording",
          "status": "granted" | "denied" | "unknown" | "not_applicable",
          "api": "CGRequestScreenCaptureAccess",
          "setup_step": "(no action needed)" | "Open System Settings…",
          "deeplink": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
          "error": "PyObjC Quartz not importable: …" (optional)
        }

    Accessibility + Full Disk Access have no public request API; this
    endpoint returns the current status + the deeplink so the iOS
    companion can pop System Settings via the URL.
    """
    permission = (body.get("permission") or "").strip()
    if not permission:
        raise HTTPException(status_code=400, detail="permission is required")
    status = request_permission(permission)
    out = status.to_dict()
    deeplink = deeplink_for(permission)
    if deeplink:
        out["deeplink"] = deeplink
    return out


@router.post("/api/system/permissions/open")
async def open_system_permission(body: dict = Body(...)):
    """Open the macOS System Settings pane for a specific TCC permission.

    Used by the Phase 13 onboarding wizard's Mac TCC walkthrough step.
    The iOS wizard shows each Mac permission row with an "Open on Mac"
    button that hits this endpoint.
    """
    permission_key = body.get("permission_key", "")
    if not permission_key:
        raise HTTPException(status_code=400, detail="permission_key is required")

    from agents.tcc_card import TCC_CATALOG, _automation_card

    if permission_key.startswith("automation:"):
        bundle = permission_key.split(":", 1)[1]
        entry = _automation_card(bundle)
    else:
        entry = TCC_CATALOG.get(permission_key)

    if not entry:
        raise HTTPException(status_code=400, detail=f"Unknown permission key: {permission_key}")

    deeplink = entry.get("macos_deeplink", "")
    if not deeplink:
        raise HTTPException(status_code=400, detail=f"No deeplink for permission key: {permission_key}")

    if platform.system() != "Darwin":
        return {"ok": False, "reason": "Not running on macOS"}

    try:
        subprocess.run(
            ["open", deeplink],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        return {"ok": True}
    except Exception as exc:
        logger.warning("open_system_permission: open failed: %s", exc)
        return {"ok": False, "reason": str(exc)}
