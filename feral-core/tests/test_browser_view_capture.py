"""Memory-only selected-tab capture, masking and stale-owner refusal."""
import asyncio
import base64
import io
import os
from pathlib import Path

from PIL import Image
import pytest

from skills.impl.browser_use import BrowserController, CDPConnection


def jpeg(width=1600, height=900):
    output = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(output, format="JPEG")
    return base64.b64encode(output.getvalue()).decode()


class CaptureCDP:
    connected = True
    target_id = "page-a"

    def __init__(self):
        self.calls = []
        self.root = {"backendNodeId": 10, "children": []}
        self.viewport = {"width": 800, "height": 450, "scale": 2, "scroll_x": 0, "scroll_y": 0}
        self.data = jpeg()
        self.mask_ok = True
        self.change = None
        self.document_calls = 0

    async def send_command(self, method, params=None, timeout=30):
        self.calls.append((method, params))
        if self.change:
            self.change(method, params, len(self.calls))
        if method == "DOM.getDocument":
            self.document_calls += 1
            return {"root": dict(self.root)}
        if method == "Runtime.evaluate":
            expression = params["expression"]
            if "FERAL_VIEW_MASK_INSTALL" in expression:
                return {"result": {"value": self.mask_ok}}
            if "FERAL_VIEW_MASK_VERIFY" in expression:
                return {"result": {"value": dict(self.viewport) if self.mask_ok else None}}
            return {"result": {"value": True}}
        if method == "Page.captureScreenshot":
            return {"data": self.data}
        return {}


def controller():
    ctrl = BrowserController()
    cdp = CaptureCDP()
    ctrl._cdp = cdp
    ctrl._attached_target_id = "page-a"
    return ctrl, cdp


@pytest.mark.asyncio
async def test_view_is_bounded_real_jpeg_memory_only_and_does_not_record(tmp_path):
    before = set(tmp_path.rglob("*"))
    ctrl, cdp = controller()
    result = await ctrl.capture_view_frame("page-a")
    assert result["success"] is True
    with Image.open(io.BytesIO(base64.b64decode(result["image_b64"]))) as image:
        assert image.size == (result["width"], result["height"]) == (1280, 720)
    assert len(result["image_b64"]) <= 2 * 1024 * 1024
    assert result["viewport_width"] == 800 and result["device_scale_factor"] == 2
    assert result["masked_password_fields"] is True and result["cursor"] is None
    assert set(tmp_path.rglob("*")) == before and ctrl._recording is None
    assert all(method not in {"Page.navigate", "Page.startScreencast", "Input.insertText"} for method, _ in cdp.calls)
    screenshot = next(params for method, params in cdp.calls if method == "Page.captureScreenshot")
    assert screenshot["captureBeyondViewport"] is False
    assert any("FERAL_VIEW_MASK_CLEANUP" in (params or {}).get("expression", "") for _, params in cdp.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("point", range(1, 10))
async def test_target_replacement_at_every_await_drops_pixels(point):
    ctrl, cdp = controller()

    def change(method, params, count):
        if count == point:
            ctrl._attached_target_id = "page-b"

    cdp.change = change
    result = await ctrl.capture_view_frame("page-a")
    assert result == {"success": False, "error_code": "view_target_changed"}
    assert "image_b64" not in result


@pytest.mark.asyncio
async def test_different_cdp_page_even_matching_controller_label_is_refused():
    ctrl, cdp = controller()
    cdp.target_id = "page-b"
    assert (await ctrl.capture_view_frame("page-a"))["success"] is False
    assert not cdp.calls


@pytest.mark.asyncio
async def test_mask_failure_never_captures_pixels():
    ctrl, cdp = controller()
    cdp.mask_ok = False
    assert await ctrl.capture_view_frame("page-a") == {"success": False, "error_code": "view_mask_failed"}
    assert all(method != "Page.captureScreenshot" for method, _ in cdp.calls)


@pytest.mark.asyncio
async def test_shadow_root_password_surface_refused_before_capture():
    ctrl, cdp = controller()
    cdp.root["children"] = [{"shadowRoots": [{"shadowRootType": "closed"}]}]
    assert (await ctrl.capture_view_frame("page-a"))["error_code"] == "view_mask_failed"
    assert all(method != "Page.captureScreenshot" for method, _ in cdp.calls)


@pytest.mark.asyncio
async def test_same_tab_navigation_after_screenshot_drops_pixels_and_pointer():
    ctrl, cdp = controller()

    def change(method, params, count):
        if method == "Page.captureScreenshot":
            cdp.root["backendNodeId"] = 11

    cdp.change = change
    assert (await ctrl.capture_view_frame("page-a"))["error_code"] == "view_target_changed"
    assert ctrl._view_document_id is None and ctrl._view_pointer is None


@pytest.mark.asyncio
async def test_resize_or_removed_mask_after_capture_refuses():
    ctrl, cdp = controller()

    def change(method, params, count):
        if method == "Page.captureScreenshot":
            cdp.viewport["width"] = 600

    cdp.change = change
    assert (await ctrl.capture_view_frame("page-a"))["success"] is False


@pytest.mark.asyncio
async def test_bad_capture_data_is_not_returned_or_logged(caplog):
    ctrl, cdp = controller()
    cdp.data = "PRIVATE-NONIMAGE-CONTENT"
    result = await ctrl.capture_view_frame("page-a")
    assert result == {"success": False, "error_code": "view_frame_invalid"}
    assert "PRIVATE-NONIMAGE" not in caplog.text


@pytest.mark.asyncio
async def test_only_actual_dispatched_click_produces_scaled_cursor():
    ctrl, cdp = controller()
    await ctrl.capture_view_frame("page-a")
    assert (await ctrl.click_at(200, 100))["success"] is True
    result = await ctrl.capture_view_frame("page-a")
    assert result["cursor"] == {"x": 320, "y": 160, "phase": "click"}
    assert [p["type"] for m, p in cdp.calls if m == "Input.dispatchMouseEvent"] == ["mousePressed", "mouseReleased"]
    cdp.root["backendNodeId"] = 11
    assert (await ctrl.capture_view_frame("page-a"))["cursor"] is None


@pytest.mark.asyncio
async def test_cancelled_capture_attempts_private_mask_cleanup():
    ctrl, cdp = controller()
    original = cdp.send_command

    async def send(method, params=None, timeout=30):
        if method == "Page.captureScreenshot":
            raise asyncio.CancelledError
        return await original(method, params, timeout)

    cdp.send_command = send
    with pytest.raises(asyncio.CancelledError):
        await ctrl.capture_view_frame("page-a")
    assert "FERAL_VIEW_MASK_CLEANUP" in cdp.calls[-1][1]["expression"]


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get("FERAL_BROWSER_VIEW_REAL") != "1", reason="explicit disposable local Chrome acceptance")
async def test_real_local_chrome_typing_mask_and_click(tmp_path):
    """No accounts/navigation beyond a synthetic page in an owned new profile."""
    import socket
    from playwright.async_api import async_playwright

    executable = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not executable.is_file():
        pytest.skip("installed local Chrome required; no download")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            str(tmp_path / "owned-chrome"), executable_path=str(executable), headless=True,
            args=[f"--remote-debugging-port={port}"], viewport={"width": 800, "height": 600},
        )
        ctrl = BrowserController()
        ctrl._cdp = CDPConnection(host="127.0.0.1", port=port)
        try:
            page = context.pages[0]
            await page.set_content('<body style="margin:0;background:white"><input id="wrong"><input id="right" value="replace this"><input type="password" value="secret" style="position:absolute;left:20px;top:100px;width:300px;height:80px;background:red"><button onclick="this.style.background=\'rgb(0,220,0)\'" style="position:absolute;left:20px;top:250px;width:100px;height:40px;background:blue">Click</button></body>')
            target_session = await context.new_cdp_session(page)
            target_id = (await target_session.send("Target.getTargetInfo"))["targetInfo"]["targetId"]
            await target_session.detach()
            assert await ctrl._cdp.connect(target_id=target_id)
            ctrl._attached_target_id = target_id
            viewports = []
            source_images = []
            original_send = ctrl._cdp.send_command

            async def observed_send(method, params=None, timeout=30):
                result = await original_send(method, params, timeout)
                if "FERAL_VIEW_MASK_VERIFY" in (params or {}).get("expression", ""):
                    viewports.append(result.get("result", {}).get("value"))
                if method == "Page.captureScreenshot" and result.get("data"):
                    with Image.open(io.BytesIO(base64.b64decode(result["data"]))) as image:
                        source_images.append((image.format, image.size))
                return result

            ctrl._cdp.send_command = observed_send
            await page.focus("#wrong")
            assert (await ctrl.type_text("#right", "café 🦦"))["success"] is True
            assert await page.input_value("#wrong") == ""
            assert await page.input_value("#right") == "café 🦦"
            frame = await ctrl.capture_view_frame(target_id)
            assert frame["success"] is True, ({k: v for k, v in frame.items() if k != "image_b64"}, viewports, source_images)
            with Image.open(io.BytesIO(base64.b64decode(frame["image_b64"]))) as image:
                # The red password rectangle is hidden before JPEG capture.
                scale = frame["width"] / frame["viewport_width"]
                pixel = image.getpixel((int(100 * scale), int(130 * scale)))
                assert min(pixel[:3]) > 220
            assert await page.locator('input[type="password"]').is_visible()
            assert (await ctrl.click_at(70, 270))["success"] is True
            clicked = await ctrl.capture_view_frame(target_id)
            assert clicked["success"] is True
            scale = clicked["width"] / clicked["viewport_width"]
            assert clicked["cursor"]["x"] == pytest.approx(70 * scale)
            # Thumbnail height rounds to an integer pixel; the mapped position
            # stays within one output pixel of the uniform source scale.
            assert clicked["cursor"]["y"] == pytest.approx(270 * scale, abs=0.5)
            assert clicked["cursor"]["phase"] == "click"
            with Image.open(io.BytesIO(base64.b64decode(clicked["image_b64"]))) as image:
                # An actual click changes the button to green. The reported
                # image-space cursor must land on that real changed region.
                pixel = image.getpixel((int(40 * scale), int(clicked["cursor"]["y"])))
                assert pixel[1] > 180 and pixel[0] < 60 and pixel[2] < 60
        finally:
            await ctrl._cdp.disconnect()
            await context.close()
