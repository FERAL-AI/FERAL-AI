"""Viewing consent, exact target binding and revocation during real awaits."""
import asyncio
from types import SimpleNamespace

import pytest

from api.browser_view import BrowserViewError, BrowserViewManager


class Controller:
    connected = True
    _attached_target_id = "tab-a"

    def __init__(self):
        self._page = object()
        self._cdp = object()
        self.calls = []
        self.entered = asyncio.Event()
        self.release = None
        self.result = {"success": True, "target_id": "tab-a", "format": "jpeg",
                       "image_b64": "fixture-pixels", "width": 10, "height": 10,
                       "captured_at": 1000.0, "masked_password_fields": True}

    async def capture_view_frame(self, target):
        self.calls.append(target)
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        return self.result


@pytest.fixture
def harness():
    clock = SimpleNamespace(now=10.0)
    source = SimpleNamespace(controller=Controller())
    manager = BrowserViewManager(lambda: source.controller, clock=lambda: clock.now,
                                 wallclock=lambda: 1000.0)
    return manager, source, clock


@pytest.mark.asyncio
async def test_no_browser_discloses_no_targets_and_never_initializes(harness):
    manager, source, _ = harness
    source.controller = None
    assert await manager.targets() == {"success": True, "connected": False,
                                      "targets": [], "active_target_id": None}
    with pytest.raises(BrowserViewError, match="view_unavailable"):
        await manager.start("tab-a", True)


@pytest.mark.asyncio
async def test_explicit_consent_exact_tab_and_generic_label(harness):
    manager, source, _ = harness
    for consent in (False, None, "true", 1):
        with pytest.raises(BrowserViewError, match="view_consent_required"):
            await manager.start("tab-a", consent)
    with pytest.raises(BrowserViewError, match="view_target_changed"):
        await manager.start("tab-b", True)
    targets = await manager.targets()
    assert targets["targets"] == [{"id": "tab-a", "label": "Connected browser tab"}]
    assert source.controller.calls == []


@pytest.mark.asyncio
async def test_exact_credentials_sequence_and_no_extra_fields(harness):
    manager, source, clock = harness
    lease = await manager.start("tab-a", True)
    assert len(lease["token"]) == 43 and lease["expires_at"] == 1300.0
    source.controller.result.update(title="private", url="secret", token="credential")
    with pytest.raises(BrowserViewError, match="view_not_authorized"):
        await manager.frame(lease["view_id"], "wrong")
    first = await manager.frame(lease["view_id"], lease["token"])
    assert first["sequence"] == 1 and first["view_id"] == lease["view_id"]
    assert not {"title", "url", "token"}.intersection(first)
    with pytest.raises(BrowserViewError, match="view_rate_limited"):
        await manager.frame(lease["view_id"], lease["token"])
    clock.now += 0.5
    assert (await manager.frame(lease["view_id"], lease["token"]))["sequence"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["expired", "target", "page", "cdp", "controller", "disconnected"])
async def test_revokes_binding_before_capture(harness, change):
    manager, source, clock = harness
    original = source.controller
    lease = await manager.start("tab-a", True)
    if change == "expired":
        clock.now += 300
    elif change == "target":
        original._attached_target_id = "tab-b"
    elif change == "page":
        original._page = object()
    elif change == "cdp":
        original._cdp = object()
    elif change == "controller":
        source.controller = Controller()
    else:
        original.connected = False
    with pytest.raises(BrowserViewError, match="view_expired_or_changed"):
        await manager.frame(lease["view_id"], lease["token"])
    assert original.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["stop", "target", "controller", "expiry"])
async def test_revocation_while_capture_in_flight_never_delivers_pixels(harness, change):
    manager, source, clock = harness
    original = source.controller
    original.release = asyncio.Event()
    lease = await manager.start("tab-a", True)
    pending = asyncio.create_task(manager.frame(lease["view_id"], lease["token"]))
    await original.entered.wait()
    with pytest.raises(BrowserViewError, match="view_capture_busy"):
        await manager.frame(lease["view_id"], lease["token"])
    if change == "stop":
        assert await manager.stop(lease["view_id"], lease["token"]) == {"success": True}
    elif change == "target":
        original._attached_target_id = "tab-b"
    elif change == "controller":
        source.controller = Controller()
    else:
        clock.now += 300
    original.release.set()
    with pytest.raises(BrowserViewError, match="view_expired_or_changed"):
        await pending


@pytest.mark.asyncio
async def test_stop_cannot_revoke_other_view_and_revoked_view_cannot_resume(harness):
    manager, _, _ = harness
    lease = await manager.start("tab-a", True)
    with pytest.raises(BrowserViewError, match="view_not_authorized"):
        await manager.stop(lease["view_id"], "wrong")
    await manager.stop(lease["view_id"], lease["token"])
    with pytest.raises(BrowserViewError, match="view_not_authorized"):
        await manager.frame(lease["view_id"], lease["token"])


@pytest.mark.asyncio
async def test_capacity_expiry_pruning_and_no_recording_calls(harness):
    manager, source, clock = harness
    for _ in range(4):
        await manager.start("tab-a", True)
    with pytest.raises(BrowserViewError, match="view_capacity"):
        await manager.start("tab-a", True)
    clock.now += 300
    await manager.start("tab-a", True)
    assert source.controller.calls == [] and len(manager._views) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("result,code", [
    ({"success": False, "error_code": "view_mask_failed"}, "view_mask_failed"),
    ({"success": False, "error_code": "private content"}, "view_capture_failed"),
    ({"success": True, "target_id": "tab-b"}, "view_target_changed"),
    ({"success": True, "target_id": "tab-a"}, "view_mask_failed"),
    ({"success": True, "target_id": "tab-a", "masked_password_fields": True,
      "image_b64": "X" * (2 * 1024 * 1024 + 1)}, "view_frame_too_large"),
])
async def test_invalid_controller_output_has_no_pixels(harness, result, code):
    manager, source, _ = harness
    source.controller.result = result
    lease = await manager.start("tab-a", True)
    with pytest.raises(BrowserViewError, match=code):
        await manager.frame(lease["view_id"], lease["token"])
