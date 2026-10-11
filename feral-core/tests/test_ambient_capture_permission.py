"""Background capture must not trigger a new macOS privacy prompt."""
from unittest.mock import AsyncMock

import pytest

from perception import screen_loop
from security.macos_permissions import TCCStatus


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["denied", "unknown", "restricted"])
async def test_background_capture_never_launches_capture_without_a_grant(monkeypatch, tmp_path, status):
    monkeypatch.setattr(screen_loop.platform, "system", lambda: "Darwin")
    monkeypatch.setattr("security.macos_permissions.check_screen_recording",
                        lambda: TCCStatus("screen_recording", status, "preflight", ""))
    request = AsyncMock(side_effect=AssertionError("background capture must not request permission"))
    monkeypatch.setattr("security.macos_permissions.request_screen_recording", request)
    spawn = AsyncMock(side_effect=AssertionError("screencapture may display a privacy prompt"))
    monkeypatch.setattr(screen_loop.asyncio, "create_subprocess_exec", spawn)
    assert not screen_loop.ambient_screen_capture_allowed()
    assert not await screen_loop._capture_screenshot(tmp_path / "screen.png")
    spawn.assert_not_called()
    request.assert_not_called()


def test_granted_capture_is_available_without_requesting_permission(monkeypatch):
    monkeypatch.setattr(screen_loop.platform, "system", lambda: "Darwin")
    monkeypatch.setattr("security.macos_permissions.check_screen_recording",
                        lambda: TCCStatus("screen_recording", "granted", "preflight", ""))
    assert screen_loop.ambient_screen_capture_allowed()


def test_other_platforms_do_not_query_macos_permissions(monkeypatch):
    monkeypatch.setattr(screen_loop.platform, "system", lambda: "Linux")
    monkeypatch.setattr("security.macos_permissions.check_screen_recording",
                        lambda: (_ for _ in ()).throw(AssertionError("macOS probe on Linux")))
    assert screen_loop.ambient_screen_capture_allowed()
