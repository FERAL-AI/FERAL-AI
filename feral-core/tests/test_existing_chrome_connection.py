"""Existing-session discovery, real controller reuse and exact flattened fencing."""

import asyncio
import base64
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest

from agents.tool_runner import ToolRunner
from models.skill_manifest import BrandProfile, SkillEndpoint, SkillManifest
from security.exec_approvals import ApprovalManager
from security.trust_ledger import TrustLedger
from skills.call_context import bind_context
from skills.registry import SkillRegistry
from skills.impl.browser_use import BrowserController, CDPConnection
from skills.impl.existing_chrome import (
    ExistingChromeConnector,
    ExistingChromeError,
    _endpoint,
    _internal,
)


class Socket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.product = "Chrome/150.0.0.0"
        self.held_method = None
        self.held = []
        self.arrived = asyncio.Event()
        self.delivered = asyncio.Event()
        self.page_type = "page"

    def __aiter__(self):
        return self

    async def __anext__(self):
        packet = await self.incoming.get()
        if packet is None:
            raise StopAsyncIteration
        self.delivered.set()
        return json.dumps(packet)

    async def send(self, raw):
        packet = json.loads(raw)
        self.sent.append(packet)
        if packet["method"] == self.held_method:
            self.held.append(packet)
            self.arrived.set()
        else:
            self.reply(packet)

    def reply(self, packet, *, result=None, session=None):
        method, params = packet["method"], packet.get("params", {})
        if result is None:
            if method == "Browser.getVersion":
                result = {"product": self.product}
            elif method == "Target.getTargets":
                result = {
                    "targetInfos": [
                        {
                            "type": "page",
                            "targetId": "page-a",
                            "url": "http://127.0.0.1/fixture",
                            "title": "Private synthetic title",
                        },
                        {"type": "page", "targetId": "page-b", "url": "about:blank"},
                        {
                            "type": "page",
                            "targetId": "settings",
                            "url": "chrome://settings",
                        },
                    ]
                }
            elif method == "Target.attachToTarget":
                result = {"sessionId": "session-" + params["targetId"]}
            elif method == "Target.getTargetInfo":
                result = {
                    "targetInfo": {
                        "targetId": params["targetId"],
                        "type": self.page_type,
                    }
                }
            elif method == "DOM.getDocument":
                result = {"root": {"backendNodeId": 1, "children": []}}
            elif method == "Runtime.evaluate":
                expression = params.get("expression", "")
                value = (
                    {
                        "width": 200,
                        "height": 100,
                        "scale": 1,
                        "scroll_x": 0,
                        "scroll_y": 0,
                    }
                    if "FERAL_VIEW_MASK_VERIFY" in expression
                    else True
                )
                result = {"result": {"value": value}}
            elif method == "Page.captureScreenshot":
                buffer = io.BytesIO()
                Image.new("RGB", (200, 100), "white").save(buffer, format="JPEG")
                result = {"data": base64.b64encode(buffer.getvalue()).decode()}
            else:
                result = {}
        response = {"id": packet["id"], "result": result}
        if session is not None or "sessionId" in packet:
            response["sessionId"] = (
                session if session is not None else packet["sessionId"]
            )
        self.incoming.put_nowait(response)

    async def close(self):
        self.closed = True
        self.incoming.put_nowait(None)


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    profile = tmp_path / "chrome-profile"
    profile.mkdir(mode=0o700)
    (profile / "DevToolsActivePort").write_text(
        "49151\n/devtools/browser/owned-browser\n"
    )
    sockets, endpoints = [], []

    async def connect(endpoint, **kwargs):
        socket = Socket()
        sockets.append(socket)
        endpoints.append((endpoint, kwargs))
        return socket

    monkeypatch.setattr("websockets.connect", connect)
    connector = ExistingChromeConnector("owner-a", profile_dir=profile)
    return connector, sockets, endpoints, profile


@contextmanager
def owner():
    with bind_context(
        session_id="owner-a", surface="http_api", tool_name="browser__type_text"
    ):
        yield


@contextmanager
def admitted(connector, tool="browser__type_text"):
    runner = ToolRunner(
        SimpleNamespace(
            _browser_resource_supplier=lambda: connector.controller.approval_binding()
            if connector.controller
            else None
        )
    )
    with (
        bind_context(session_id="owner-a", surface="http_api", tool_name=tool),
        runner.browser_resource_scope(tool, "owner-a"),
    ):
        yield runner


async def selected(fixture):
    connector, sockets, _, _ = fixture
    with owner():
        await connector.connect(True)
        await connector.select("page-a")
    return connector, sockets[0], connector.controller


@pytest.mark.parametrize(
    "raw",
    [
        "9222\nws://example.com/devtools/browser/evil\n",
        "49151\n/devtools/page/page-a\n",
        "49151\n/devtools/browser/../evil\n",
        "49151\n/devtools/browser/id?secret=x\n",
        "true\n/devtools/browser/id\n",
        "65536\n/devtools/browser/id\n",
        "1\n/devtools/browser/id\n",
        "49151\n/devtools/browser/id\nextra\n",
        "x" * 1025,
    ],
)
def test_strict_local_browser_endpoint(tmp_path, raw):
    (tmp_path / "DevToolsActivePort").write_text(raw)
    with pytest.raises(ExistingChromeError) as caught:
        _endpoint(tmp_path)
    assert str(caught.value).startswith("existing_chrome_")
    assert "secret" not in str(caught.value)


def test_owned_regular_file_and_profile_only(tmp_path):
    elsewhere = tmp_path / "external"
    elsewhere.write_text("49151\n/devtools/browser/id\n")
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "DevToolsActivePort").symlink_to(elsewhere)
    with pytest.raises(ExistingChromeError):
        _endpoint(profile)
    alias = tmp_path / "profile-alias"
    alias.symlink_to(profile)
    with pytest.raises(ExistingChromeError):
        _endpoint(alias)


@pytest.mark.asyncio
async def test_no_consent_no_context_no_file_access_no_launch(fixture, monkeypatch):
    connector, sockets, _, _ = fixture
    accessed = []
    monkeypatch.setattr(
        "skills.impl.existing_chrome._endpoint", lambda *_: accessed.append(True)
    )
    with pytest.raises(ExistingChromeError, match="consent_required"):
        await connector.connect(False)
    with pytest.raises(ExistingChromeError, match="owner_required"):
        await connector.connect(True)
    with owner():
        monkeypatch.setenv("FERAL_TOOL_CALL_CONTEXT", "off")
        with pytest.raises(ExistingChromeError, match="context_disabled"):
            await connector.connect(True)
    assert not accessed and not sockets and connector.controller is None


@pytest.mark.asyncio
async def test_absent_chrome_refuses_without_launch(fixture):
    connector, sockets, _, profile = fixture
    (profile / "DevToolsActivePort").unlink()
    with owner(), pytest.raises(ExistingChromeError, match="unavailable"):
        await connector.connect(True)
    assert not sockets and connector.status()["connected"] is False


@pytest.mark.asyncio
async def test_connect_does_not_select_or_initialize_and_labels_are_consent_scoped(
    fixture,
):
    connector, sockets, endpoints, _ = fixture
    with owner():
        result = await connector.connect(True)
        rows = await connector.targets()
    assert result["connected"] is True and result["selected_target_id"] is None
    assert connector.controller is None
    assert endpoints[0][0] == "ws://127.0.0.1:49151/devtools/browser/owned-browser"
    assert endpoints[0][1]["proxy"] is None
    assert rows == [
        {"id": "page-a", "label": "Chrome tab 1: Private synthetic title"},
        {"id": "page-b", "label": "Chrome tab 2"},
    ]
    assert [p["method"] for p in sockets[0].sent] == [
        "Browser.getVersion",
        "Target.getTargets",
    ]
    await connector.disconnect()


@pytest.mark.parametrize(
    "owner_id", ["", " leading", "trailing ", "control\nvalue", "control\u0085value",
                 "x" * 1025, 1, True, False, None]
)
def test_owner_is_exact_canonical_session_identity(owner_id):
    with pytest.raises(ExistingChromeError, match="owner_required"):
        ExistingChromeConnector(owner_id)


def test_canonical_maximum_owner_identity_is_preserved():
    identity = "界" * 1024
    assert ExistingChromeConnector(identity).owner_session_id == identity


@pytest.mark.asyncio
async def test_consented_title_labels_strip_controls_and_bound_utf8(fixture):
    connector, sockets, _, _ = fixture
    with owner():
        await connector.connect(True)
        socket = sockets[0]
        socket.held_method = "Target.getTargets"
        task = asyncio.create_task(connector.targets())
        await asyncio.wait_for(socket.arrived.wait(), 1)
        socket.reply(
            socket.held[0],
            result={
                "targetInfos": [
                    {
                        "type": "page",
                        "targetId": "a",
                        "url": "https://example.com/private",
                        "title": "\n\u202e" + "🦊" * 200 + "\x00",
                    },
                    {
                        "type": "page",
                        "targetId": "b",
                        "url": "about:blank",
                        "title": " \n\x00",
                    },
                ]
            },
        )
        rows = await task
    assert len(rows[0]["label"].encode("utf-8")) <= 180
    assert rows[0]["label"].startswith("Chrome tab 1: 🦊")
    assert "\n" not in rows[0]["label"] and "\u202e" not in rows[0]["label"]
    assert rows[1]["label"] == "Chrome tab 2"
    assert "private" not in json.dumps(rows)
    await connector.disconnect()


@pytest.mark.asyncio
async def test_actual_controller_flat_selected_session_and_masked_operator_view(
    fixture,
):
    connector, socket, controller = await selected(fixture)
    assert isinstance(controller, BrowserController) and isinstance(
        controller._cdp, CDPConnection
    )
    assert controller._cdp.is_page_target and controller._cdp.target_id == "page-a"
    assert (
        controller._page is None
        and controller._browser is None
        and controller._playwright is None
    )
    with owner():
        assert controller.approval_binding() == {
            "connection_id": connector.status()["connection_id"],
            "target_id": "page-a",
            "owner_session_id": "owner-a",
        }
    assert await controller.initialize() is False
    result = await controller.capture_view_frame("page-a")
    assert result["success"] is True and result["masked_password_fields"] is True
    with Image.open(io.BytesIO(base64.b64decode(result["image_b64"]))) as image:
        assert image.format == "JPEG" and image.size == (200, 100)
    page_commands = [
        p
        for p in socket.sent
        if p["method"]
        in {
            "Page.enable",
            "DOM.getDocument",
            "Page.captureScreenshot",
            "Runtime.evaluate",
        }
    ]
    assert page_commands and all(
        p["sessionId"] == "session-page-a" for p in page_commands
    )
    assert all(p["method"] != "Browser.close" for p in socket.sent)
    await connector.disconnect()


@pytest.mark.asyncio
async def test_same_owner_without_central_capture_cannot_act_or_read_cached_state(
    fixture,
):
    connector, socket, controller = await selected(fixture)
    count = len(socket.sent)
    with owner():
        assert (await controller.type_text("#right", "blocked"))["success"] is False
        assert (await controller.get_console_logs())["success"] is False
        with pytest.raises(ExistingChromeError):
            await controller._cdp.send_command("Input.insertText", {"text": "blocked"})
    assert len(socket.sent) == count
    await connector.disconnect()


@pytest.mark.asyncio
async def test_real_toolrunner_capture_admits_selected_unicode_input_and_refuses_foreign_context(
    fixture,
):
    connector, socket, controller = await selected(fixture)
    with admitted(connector):
        assert (await controller.type_text("#right", "café 漢字 🦊"))["success"] is True
        before = len(socket.sent)
        with bind_context(session_id="other-owner"):
            assert (await controller.type_text("#right", "blocked"))["success"] is False
        assert len(socket.sent) == before
    inserts = [p for p in socket.sent if p["method"] == "Input.insertText"]
    assert len(inserts) == 1 and inserts[0]["params"] == {"text": "café 漢字 🦊"}
    assert inserts[0]["sessionId"] == "session-page-a"
    await connector.disconnect()


@pytest.mark.asyncio
async def test_queued_action_is_revoked_before_socket_write_on_selection_change(
    fixture, monkeypatch
):
    connector, socket, old = await selected(fixture)
    lock = old._cdp._socket._write_lock
    retired = asyncio.Event()
    listed = asyncio.Event()
    continue_selection = asyncio.Event()
    original_close = old.close
    original_targets = connector.targets

    async def targets():
        rows = await original_targets()
        await lock.acquire()
        listed.set()
        await continue_selection.wait()
        return rows

    async def close():
        retired.set()
        await original_close()

    monkeypatch.setattr(old, "close", close)
    monkeypatch.setattr(connector, "targets", targets)
    with admitted(connector):
        replacement = asyncio.create_task(connector.select("page-b"))
        await asyncio.wait_for(listed.wait(), 1)
        action = asyncio.create_task(
            old._cdp.send_command("Input.insertText", {"text": "must never send"})
        )
        await asyncio.sleep(
            0
        )  # Queue on the held lock before replacing the selected resource.
        continue_selection.set()
        await asyncio.wait_for(retired.wait(), 1)
        assert old._cdp._retired
        lock.release()
        with pytest.raises(ExistingChromeError, match="connection_changed"):
            await action
        assert (await replacement)["selected_target_id"] == "page-b"
    assert not any(packet["method"] == "Input.insertText" for packet in socket.sent)
    await connector.disconnect()


@pytest.mark.asyncio
async def test_supported_accessibility_snapshot_is_bound_to_selected_page(fixture):
    connector, socket, controller = await selected(fixture)
    before = len(socket.sent)
    with admitted(connector, "browser__snapshot"):
        assert (await controller.snapshot())["success"] is True
    assert [packet["method"] for packet in socket.sent[before:]] == [
        "DOM.enable",
        "Accessibility.enable",
        "Accessibility.getFullAXTree",
    ]
    assert all(
        packet["sessionId"] == "session-page-a" for packet in socket.sent[before:]
    )
    await connector.disconnect()


@pytest.mark.asyncio
async def test_real_controller_metadata_allows_exact_http_review_without_action_context(
    fixture,
):
    connector, socket, controller = await selected(fixture)
    binding = controller.approval_binding()
    altered = controller.approval_binding()
    altered["owner_session_id"] = "foreign"
    assert controller.approval_binding() == binding
    registry = SkillRegistry()
    registry.register(
        SkillManifest(
            skill_id="browser",
            brand=BrandProfile(name="Synthetic Chrome review"),
            description="Selected synthetic browser",
            endpoints=[
                SkillEndpoint(
                    id="click",
                    method="PYTHON",
                    url="",
                    description="Click synthetic element",
                    requires_user_approval=True,
                    safety_tier="confirm",
                )
            ],
        )
    )
    orch = SimpleNamespace(
        skills=registry,
        _session_surfaces={},
        _active_turns={},
        daemons={},
        _browser_resource_supplier=lambda: connector.controller.approval_binding()
        if connector.controller
        else None,
    )
    runner = ToolRunner(
        orch,
        autonomy_mode="strict",
        trust_ledger=TrustLedger(persist=False),
        approval_manager=ApprovalManager(db_path=":memory:"),
    )
    with owner():
        pending = runner.enforce_safety(
            "browser__click", {"selector": "#button"}, "owner-a"
        )
    assert (
        pending["status"] == "pending_approval"
        and pending["browser_resource"] == binding
    )
    assert runner.approve_pending(pending["request_id"], session_id="foreign") is None
    approved = runner.approve_pending(pending["request_id"], session_id="owner-a")
    assert approved is not None and "approval" in approved
    assert not runner._approval_mgr.check_approval("browser__click", "owner-a")[0]
    assert runner.approve_pending(pending["request_id"], session_id="owner-a") is None
    assert (await controller.click("#button"))["success"] is False
    with owner():
        stale = runner.enforce_safety(
            "browser__click", {"selector": "#other"}, "owner-a"
        )
        await connector.select("page-b")
    assert runner.approve_pending(stale["request_id"], session_id="owner-a") is None
    with pytest.raises(ExistingChromeError, match="connection_changed"):
        controller.approval_binding()
    assert not any(packet["method"].startswith("Input.") for packet in socket.sent)
    await connector.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_phase", ["lock", "send"])
async def test_deadline_bounds_socket_lock_and_send_without_replay(
    fixture, monkeypatch, blocked_phase
):
    connector, socket, controller = await selected(fixture)
    lock = controller._cdp._socket._write_lock
    if blocked_phase == "lock":
        await lock.acquire()
    else:
        socket.held_method = "Input.insertText"
        original_send = socket.send

        async def blocked_send(raw):
            await original_send(raw)
            await asyncio.Event().wait()

        monkeypatch.setattr(socket, "send", blocked_send)
    with admitted(connector), pytest.raises(ExistingChromeError, match="timeout"):
        await asyncio.wait_for(
            controller._cdp.send_command(
                "Input.insertText", {"text": "once"}, timeout=0.02
            ),
            1,
        )
    if blocked_phase == "lock":
        lock.release()
    assert not controller._cdp._socket._pending
    assert len(
        [packet for packet in socket.sent if packet["method"] == "Input.insertText"]
    ) == (blocked_phase == "send")
    await connector.disconnect()


@pytest.mark.asyncio
async def test_selection_generation_a_b_a_cannot_reuse_old_approval(fixture):
    connector, _, old = await selected(fixture)
    with admitted(connector):
        old_binding = old.approval_binding()
        await connector.select("page-b")
        await connector.select("page-a")
        assert connector.controller.approval_binding() != old_binding
        assert (await connector.controller.type_text("#right", "stale approval"))[
            "success"
        ] is False
        assert old.connected is False
    with admitted(connector):
        assert (await connector.controller.type_text("#right", "new review"))[
            "success"
        ] is True
    await connector.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method",
    [
        "list_tabs",
        "switch_tab",
        "attach_to_tab",
        "new_tab",
        "close_tab",
        "evaluate",
        "select",
        "save_cookies",
        "restore_cookies",
        "enable_network_monitor",
        "start_recording",
        "start_har",
        "upload_file",
        "set_download_path",
        "get_page_pdf",
    ],
)
async def test_global_or_unreviewed_endpoints_refuse_before_any_io(fixture, method):
    connector, socket, controller = await selected(fixture)
    with admitted(connector):
        before = len(socket.sent)
        result = await getattr(controller, method)()
        assert result == {
            "success": False,
            "error_code": "existing_chrome_endpoint_unsupported",
        }
        assert len(socket.sent) == before
    await connector.disconnect()


@pytest.mark.asyncio
async def test_foreign_flattened_reply_and_event_do_not_complete_or_influence_selected_page(
    fixture,
):
    connector, socket, controller = await selected(fixture)
    socket.held_method = "Input.insertText"
    events = []
    controller._cdp.add_event_listener(events.append)
    with admitted(connector):
        task = asyncio.create_task(
            controller._cdp.send_command("Input.insertText", {"text": "one"})
        )
        await asyncio.wait_for(socket.arrived.wait(), 1)
        packet = socket.held[0]
        socket.delivered.clear()
        socket.reply(packet, result={"foreign": True}, session="foreign-session")
        await asyncio.wait_for(socket.delivered.wait(), 1)
        assert not task.done()
        socket.delivered.clear()
        socket.incoming.put_nowait(
            {
                "method": "Page.loadEventFired",
                "sessionId": "foreign-session",
                "params": {},
            }
        )
        await asyncio.wait_for(socket.delivered.wait(), 1)
        assert not events
        socket.reply(packet, result={"owned": True})
        assert await task == {"owned": True}
        socket.delivered.clear()
        socket.incoming.put_nowait(
            {
                "method": "Page.loadEventFired",
                "sessionId": "session-page-a",
                "params": {},
            }
        )
        await asyncio.wait_for(socket.delivered.wait(), 1)
        assert events[0]["sessionId"] == "session-page-a"
    await connector.disconnect()


@pytest.mark.asyncio
async def test_timeout_late_response_no_replay_and_cancel_pending_cleanup(fixture):
    connector, socket, controller = await selected(fixture)
    socket.held_method = "Input.insertText"
    with admitted(connector):
        with pytest.raises(ExistingChromeError, match="timeout"):
            await controller._cdp.send_command(
                "Input.insertText", {"text": "one"}, timeout=0.02
            )
        assert not connector._socket._pending and not connector._socket._expected
        socket.reply(socket.held[0], result={"late": True})
        task = asyncio.create_task(
            controller._cdp.send_command("Input.insertText", {"text": "two"})
        )
        while len(socket.held) < 2:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len([p for p in socket.sent if p["method"] == "Input.insertText"]) == 2
    assert not connector._socket._pending and not connector._socket._expected
    await connector.disconnect()


@pytest.mark.asyncio
async def test_operator_view_exemption_cannot_dispatch_input_or_escape_to_a_child_task(
    fixture,
):
    connector, socket, controller = await selected(fixture)
    before = len(socket.sent)
    with _internal(controller._cdp, "view"):
        with pytest.raises(ExistingChromeError):
            await controller._cdp.send_command(
                "Input.insertText", {"text": "not permitted"}
            )
        child = asyncio.create_task(controller._cdp.send_command("DOM.getDocument"))
        with pytest.raises(ExistingChromeError):
            await child
    assert len(socket.sent) == before
    await connector.disconnect()


@pytest.mark.asyncio
async def test_partial_select_failure_disconnects_only_own_socket(fixture):
    connector, sockets, _, _ = fixture
    with owner():
        await connector.connect(True)
        sockets[0].page_type = "service_worker"
        with pytest.raises(ExistingChromeError, match="target_unavailable"):
            await connector.select("page-a")
    assert connector.controller is None and connector.status()["connected"] is False
    assert sockets[0].closed and all(
        p["method"] != "Browser.close" for p in sockets[0].sent
    )


@pytest.mark.asyncio
async def test_disconnect_during_attach_cannot_install_late_controller(fixture):
    connector, sockets, _, _ = fixture
    with owner():
        await connector.connect(True)
        socket = sockets[0]
        socket.held_method = "Target.attachToTarget"
        task = asyncio.create_task(connector.select("page-a"))
        await asyncio.wait_for(socket.arrived.wait(), 1)
        await connector.disconnect()
        with pytest.raises(ExistingChromeError):
            await task
    assert (
        connector.controller is None
        and connector.status()["connected"] is False
        and socket.closed
    )
    assert all(p["method"] != "Browser.close" for p in socket.sent)


@pytest.mark.asyncio
async def test_detached_session_immediately_revokes_controller(fixture):
    connector, socket, controller = await selected(fixture)
    socket.delivered.clear()
    socket.incoming.put_nowait(
        {
            "method": "Target.detachedFromTarget",
            "params": {"sessionId": "session-page-a"},
        }
    )
    await asyncio.wait_for(socket.delivered.wait(), 1)
    assert not controller.connected and connector.controller is None
    assert (await controller.capture_view_frame("page-a"))["success"] is False
    await connector.disconnect()


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("FERAL_EXISTING_CHROME_REAL") != "1",
    reason="explicit disposable Chrome fixture required",
)
async def test_real_disposable_browser_root_selected_page_typing_and_masked_capture():
    profile = Path(os.environ["FERAL_EXISTING_CHROME_PROFILE"])
    require_root = profile.parent.name.startswith("feral-browser-937-chrome-")
    assert (
        require_root
        and profile.parent.parent == Path("/private/tmp")
        and not profile.is_symlink()
    )
    receipt = json.loads((profile.parent / "receipt.json").read_text())
    assert receipt["phase"] == "ready" and receipt["owned_debug_port"] != 9222
    connector = ExistingChromeConnector("owner-a", profile_dir=profile)
    try:
        with owner():
            await connector.connect(True)
            assert any(
                row["id"] == receipt["owned_target_id"]
                for row in await connector.targets()
            )
            await connector.select(receipt["owned_target_id"])
        with admitted(connector):
            result = await connector.controller.type_text(
                "#right", "Flat-session café 漢字 🦊"
            )
            assert result["success"] is True
            values = await connector.controller._cdp.send_command(
                "Runtime.evaluate",
                {
                    "expression": "({right:right.value,wrong:wrong.value,active:document.activeElement.id})",
                    "returnByValue": True,
                },
            )
            assert values["result"]["value"] == {
                "right": "Flat-session café 漢字 🦊",
                "wrong": "wrong-field-original",
                "active": "right",
            }
        frame = await connector.controller.capture_view_frame(
            receipt["owned_target_id"]
        )
        assert frame["success"] is True and frame["masked_password_fields"] is True
        with Image.open(io.BytesIO(base64.b64decode(frame["image_b64"]))) as image:
            assert image.format == "JPEG" and max(image.size) <= 1280
        assert (await connector.disconnect())["browser_closed"] is False
    finally:
        await connector.disconnect()
