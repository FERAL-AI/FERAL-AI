"""Ephemeral, explicitly requested observation of the attached local browser.

This is an operator viewing lease, not permission to act or an agent task lease.
No pixels, URL, title, form text or credentials are persisted here. Actions retain
their existing ToolRunner authority; this interface has no input operations.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import re
import secrets
import time
from typing import Any, Callable
import uuid


class BrowserViewError(Exception):
    def __init__(self, code: str, status: int = 409):
        self.code = code
        self.status = status
        super().__init__(code)


@dataclass
class _View:
    token: str = field(repr=False)
    target: str
    controller: Any
    page: Any
    cdp: Any
    deadline: float
    expires_at: float
    sequence: int = 0
    last_frame: float | None = None
    in_flight: bool = False


class BrowserViewManager:
    ttl = 300.0
    minimum_interval = 0.5
    maximum_views = 4

    def __init__(self, controller_supplier: Callable[[], Any], *,
                 clock: Callable[[], float] = time.monotonic,
                 wallclock: Callable[[], float] = time.time):
        self._controller = controller_supplier
        self._clock = clock
        self._wallclock = wallclock
        self._views: dict[str, _View] = {}
        self._lock = asyncio.Lock()

    def _active(self) -> tuple[Any, str]:
        controller = self._controller()
        target = getattr(controller, "_attached_target_id", "")
        if (controller is None or not getattr(controller, "connected", False)
                or not isinstance(target, str)
                or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", target) is None):
            raise BrowserViewError("view_unavailable")
        return controller, target

    def _valid(self, view: _View) -> bool:
        try:
            controller, target = self._active()
        except BrowserViewError:
            return False
        return (self._clock() < view.deadline and controller is view.controller
                and target == view.target
                and getattr(controller, "_page", None) is view.page
                and getattr(controller, "_cdp", None) is view.cdp)

    def _prune(self) -> None:
        for key, view in list(self._views.items()):
            if not self._valid(view):
                del self._views[key]

    async def targets(self) -> dict:
        try:
            _, target = self._active()
        except BrowserViewError:
            return {"success": True, "connected": False,
                    "targets": [], "active_target_id": None}
        return {"success": True, "connected": True, "active_target_id": target,
                "targets": [{"id": target, "label": "Connected browser tab"}]}

    async def start(self, target_id: str, consent: bool) -> dict:
        if consent is not True:
            raise BrowserViewError("view_consent_required", 400)
        async with self._lock:
            self._prune()
            controller, target = self._active()
            if target_id != target:
                raise BrowserViewError("view_target_changed")
            if len(self._views) >= self.maximum_views:
                raise BrowserViewError("view_capacity", 429)
            key = str(uuid.uuid4())
            view = _View(secrets.token_urlsafe(32), target, controller,
                         getattr(controller, "_page", None),
                         getattr(controller, "_cdp", None),
                         self._clock() + self.ttl, self._wallclock() + self.ttl)
            self._views[key] = view
            return {"success": True, "view_id": key, "token": view.token,
                    "target_id": target, "expires_at": view.expires_at}

    def _authorized(self, view_id: str, token: str) -> _View:
        view = self._views.get(view_id)
        if (view is None or not isinstance(token, str)
                or not secrets.compare_digest(view.token, token)):
            raise BrowserViewError("view_not_authorized", 403)
        if not self._valid(view):
            self._views.pop(view_id, None)
            raise BrowserViewError("view_expired_or_changed", 410)
        return view

    async def frame(self, view_id: str, token: str) -> dict:
        async with self._lock:
            view = self._authorized(view_id, token)
            now = self._clock()
            if view.in_flight:
                raise BrowserViewError("view_capture_busy", 409)
            if view.last_frame is not None and now - view.last_frame < self.minimum_interval:
                raise BrowserViewError("view_rate_limited", 429)
            view.in_flight = True
            view.last_frame = now
        try:
            # The controller rechecks page/document identity during capture.
            result = await asyncio.wait_for(
                view.controller.capture_view_frame(view.target), timeout=8.0)
            async with self._lock:
                if self._views.get(view_id) is not view or not self._valid(view):
                    self._views.pop(view_id, None)
                    raise BrowserViewError("view_expired_or_changed", 410)
                if not isinstance(result, dict) or result.get("success") is not True:
                    code = result.get("error_code") if isinstance(result, dict) else None
                    allowed = {"view_target_changed", "view_unavailable", "view_mask_failed",
                               "view_capture_failed", "view_frame_too_large"}
                    raise BrowserViewError(code if code in allowed else "view_capture_failed")
                if result.get("target_id") != view.target:
                    raise BrowserViewError("view_target_changed")
                if result.get("masked_password_fields") is not True:
                    raise BrowserViewError("view_mask_failed")
                image = result.get("image_b64")
                if not isinstance(image, str) or not image or len(image) > 2 * 1024 * 1024:
                    raise BrowserViewError("view_frame_too_large")
                view.sequence += 1
                # Return only the documented observation fields. Never forward
                # unexpected controller diagnostics, titles or input values.
                fields = ("image_b64", "format", "width", "height", "captured_at", "cursor",
                          "masked_password_fields", "viewport_width", "viewport_height",
                          "device_scale_factor", "scroll_x", "scroll_y")
                return {**{name: result[name] for name in fields if name in result},
                        "success": True, "view_id": view_id, "target_id": view.target,
                        "sequence": view.sequence}
        except (TimeoutError, OSError):
            raise BrowserViewError("view_capture_failed") from None
        except BrowserViewError:
            raise
        except Exception:
            # Controller diagnostics may include private page content.
            raise BrowserViewError("view_capture_failed") from None
        finally:
            view.in_flight = False

    async def stop(self, view_id: str, token: str) -> dict:
        async with self._lock:
            view = self._views.get(view_id)
            # Exact token is required even for expired/rebound views. Unknown
            # requests never disclose whether another viewer exists.
            if (view is None or not isinstance(token, str)
                    or not secrets.compare_digest(view.token, token)):
                raise BrowserViewError("view_not_authorized", 403)
            del self._views[view_id]
            return {"success": True}
