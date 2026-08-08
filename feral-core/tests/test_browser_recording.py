"""Session video recording on ``BrowserController`` (CDP screencast).

The three things worth pinning here are the ones that break silently in
production:

* ``Page.screencastFrameAck``. Chrome buffers only a couple of
  unacknowledged frames and then stops emitting. A recorder that forgets
  the ack captures the opening seconds and then a still image, and looks
  fine in a smoke test. Both the ack-gated fake and the real-WebSocket
  stub below emit more frames than the buffer holds, so a missing ack
  fails the test instead of quietly shortening the video.
* the ffmpeg-missing path, which must report a named dependency, not a video
  path that does not exist.
* per-frame timings. A screencast is variable-rate, so assembling at an
  assumed constant framerate would not replay what the user saw.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import shutil
import subprocess
from pathlib import Path

import pytest

from skills.impl.browser_use import (
    BrowserController,
    RECORDING_MAX_FRAME_SECONDS,
    RECORDING_MIN_FRAME_SECONDS,
    redact_recording_text,
    safe_recording_name,
)

FRAME_BYTES = b"\xff\xd8\xff\xe0not-a-real-jpeg"
FRAME_B64 = base64.b64encode(FRAME_BYTES).decode()


@pytest.fixture()
def feral_home(tmp_path, monkeypatch):
    """Point the FERAL data home at a temp dir.

    Recordings are written under ``feral_data_home()``; without this the
    suite would drop frames into the developer's real ``~/.feral``.
    """
    monkeypatch.setenv("FERAL_HOME", str(tmp_path / "feral"))
    return tmp_path / "feral"


class AckGatedCDP:
    """CDP stand-in that models Chrome's screencast backpressure.

    Chrome will not emit another ``Page.screencastFrame`` once ``window``
    frames are outstanding. Reproducing that is the only way a unit test
    can tell an acking recorder from a non-acking one.
    """

    def __init__(self, total: int = 12, window: int = 2):
        self.total = total
        self.window = window
        self.sent = 0
        self.acked = 0
        self.started = False
        self.connected = True
        # Recording refuses a browser-level socket, so the fake must
        # present itself as attached to a tab.
        self.is_page_target = True
        self.commands: list[tuple[str, dict]] = []
        self._event_listeners: list = []

    def add_event_listener(self, listener):
        self._event_listeners.append(listener)

    async def send_command(self, method: str, params: dict = None, timeout: float = 30.0):
        self.commands.append((method, params or {}))
        if method == "Page.startScreencast":
            self.started = True
            self._pump()
        elif method == "Page.screencastFrameAck":
            self.acked += 1
            self._pump()
        elif method == "Page.stopScreencast":
            self.started = False
        elif method == "Runtime.evaluate":
            return {"result": {"value": True}}
        return {}

    def _pump(self):
        if not self.started:
            return
        while self.sent < self.total and (self.sent - self.acked) < self.window:
            self.sent += 1
            msg = {
                "method": "Page.screencastFrame",
                "params": {
                    "data": FRAME_B64,
                    "sessionId": self.sent,
                    "metadata": {
                        # 20fps of wall-clock capture time
                        "timestamp": 1_700_000_000.0 + self.sent * 0.05,
                        "deviceWidth": 320,
                        "deviceHeight": 200,
                        "scrollOffsetY": 0,
                    },
                },
            }
            for listener in list(self._event_listeners):
                asyncio.get_running_loop().create_task(listener(msg))


class TestRedaction:
    def test_email_uuid_and_tenant_id_are_scrubbed(self) -> None:
        text = (
            "https://app.example.com/t/9f1c2d3e-4a5b-6c7d-8e9f-0a1b2c3d4e5f"
            "?tenant_id=acme-prod&who=ada@example.com"
        )
        out = redact_recording_text(text)
        assert "ada@example.com" not in out
        assert "9f1c2d3e-4a5b-6c7d-8e9f-0a1b2c3d4e5f" not in out
        assert "acme-prod" not in out
        assert "app.example.com" in out

    def test_oauth_token_params_are_scrubbed(self) -> None:
        out = redact_recording_text("https://x.test/cb?code=abc123&access_token=zzz&page=2")
        assert "abc123" not in out
        assert "zzz" not in out
        assert "page=2" in out

    def test_safe_recording_name_cannot_escape_the_recordings_root(self) -> None:
        assert "/" not in safe_recording_name("../../etc/passwd")
        assert safe_recording_name("/abs/path") == "abs-path"
        assert safe_recording_name("demo run 1") == "demo-run-1"
        assert safe_recording_name("...") == ""


class TestScreencastAck:
    async def test_fake_models_backpressure_when_frames_are_not_acked(self) -> None:
        """Sanity check on the fake itself.

        If a non-acking listener still received every frame, the ack test
        below would pass for the wrong reason.
        """
        cdp = AckGatedCDP(total=12, window=2)
        received: list[dict] = []

        async def ignores_acks(msg):
            received.append(msg)

        cdp.add_event_listener(ignores_acks)
        await cdp.send_command("Page.startScreencast", {})
        await asyncio.sleep(0.05)
        assert len(received) == 2, "fake must stall once the ack window is full"

    async def test_every_frame_is_acked_and_persisted(self, feral_home) -> None:
        ctrl = BrowserController()
        cdp = AckGatedCDP(total=12, window=2)
        ctrl._cdp = cdp

        started = await ctrl.start_recording(name="ack-test")
        assert started["success"] is True

        # Frames are delivered as tasks off the receive loop; let them drain.
        for _ in range(200):
            if cdp.sent >= cdp.total and cdp.acked >= cdp.total:
                break
            await asyncio.sleep(0.01)

        stopped = await ctrl.stop_recording(assemble=False)
        assert stopped["success"] is True
        assert stopped["frame_count"] == 12, "frames stopped arriving; ack is broken"
        assert stopped["ack_errors"] == 0
        assert stopped["write_errors"] == 0

        acks = [params for method, params in cdp.commands if method == "Page.screencastFrameAck"]
        assert len(acks) == 12
        assert [a["sessionId"] for a in acks] == list(range(1, 13))

        frames_dir = Path(stopped["directory"]) / "frames"
        assert sorted(p.name for p in frames_dir.glob("*.jpg")) == [
            f"{n:06d}.jpg" for n in range(1, 13)
        ]
        assert (frames_dir / "000001.jpg").read_bytes() == FRAME_BYTES

    async def test_manifest_records_real_timestamps_and_durations(self, feral_home) -> None:
        ctrl = BrowserController()
        cdp = AckGatedCDP(total=6, window=2)
        ctrl._cdp = cdp
        await ctrl.start_recording(name="timing-test")
        for _ in range(200):
            if cdp.acked >= cdp.total:
                break
            await asyncio.sleep(0.01)
        stopped = await ctrl.stop_recording(assemble=False)

        manifest = json.loads(Path(stopped["manifest_path"]).read_text(encoding="utf-8"))
        assert manifest["schema_version"] == 1
        assert manifest["frame_count"] == 6
        offsets = [f["offset_seconds"] for f in manifest["frames"]]
        assert offsets[0] == 0.0
        assert offsets == sorted(offsets)
        # The fake captures at 20fps, so every hold is 0.05s including the
        # synthesised tail: 6 frames of wall clock, not 6 / assumed-fps.
        assert all(abs(f["duration_seconds"] - 0.05) < 1e-6 for f in manifest["frames"])
        assert abs(manifest["duration_seconds"] - 0.30) < 1e-6

    async def test_frame_cap_truncates_instead_of_filling_the_disk(self, feral_home) -> None:
        ctrl = BrowserController()
        cdp = AckGatedCDP(total=10, window=2)
        ctrl._cdp = cdp
        await ctrl.start_recording(name="cap-test", max_frames=4)
        for _ in range(200):
            if cdp.acked >= cdp.total:
                break
            await asyncio.sleep(0.01)
        stopped = await ctrl.stop_recording(assemble=False)
        assert stopped["frame_count"] == 4
        assert stopped["truncated"] is True
        assert stopped["degraded"] == "frame_cap_reached"

    async def test_redact_selectors_blur_the_live_page(self, feral_home) -> None:
        ctrl = BrowserController()
        cdp = AckGatedCDP(total=1, window=2)
        ctrl._cdp = cdp
        started = await ctrl.start_recording(
            name="mask-test", redact_selectors=[".account-email", "#tenant"]
        )
        assert started["mask_applied"] is True
        injected = [
            params["expression"]
            for method, params in cdp.commands
            if method == "Runtime.evaluate"
        ]
        assert any(".account-email" in expr and "blur(14px)" in expr for expr in injected)

        await ctrl.stop_recording(assemble=False)
        removed = [
            params["expression"]
            for method, params in cdp.commands
            if method == "Runtime.evaluate"
        ]
        assert any("getElementById" in expr and "remove()" in expr for expr in removed)


class TestLifecycleErrors:
    async def test_start_without_cdp_is_truthful(self, feral_home) -> None:
        from skills.impl.browser_use import CDPConnection

        ctrl = BrowserController()
        # Port 1 refuses instantly, and pinning it keeps the test from
        # latching onto a real Chrome the developer happens to be running
        # on the default debugging port.
        ctrl._cdp = CDPConnection(host="127.0.0.1", port=1)
        out = await ctrl.start_recording()
        assert out["success"] is False
        assert "CDP not connected to a page target" in out["error"]
        assert "--remote-debugging-port" in out["error"]

    async def test_stop_without_start_is_an_error(self, feral_home) -> None:
        ctrl = BrowserController()
        out = await ctrl.stop_recording()
        assert out["success"] is False
        assert "No active recording" in out["error"]

    async def test_double_start_is_rejected(self, feral_home) -> None:
        ctrl = BrowserController()
        ctrl._cdp = AckGatedCDP(total=1, window=2)
        assert (await ctrl.start_recording(name="one"))["success"] is True
        second = await ctrl.start_recording(name="two")
        assert second["success"] is False
        assert "Already recording" in second["error"]
        await ctrl.stop_recording(assemble=False)

    def test_recordings_root_lives_under_the_feral_data_home(self, feral_home) -> None:
        ctrl = BrowserController()
        root = ctrl._recordings_root
        assert root == feral_home / "browser" / "recordings"
        assert root.is_dir()


class TestAssembly:
    async def test_ffmpeg_missing_reports_a_named_degradation(
        self, feral_home, monkeypatch, caplog
    ) -> None:
        ctrl = BrowserController()
        cdp = AckGatedCDP(total=4, window=2)
        ctrl._cdp = cdp
        await ctrl.start_recording(name="no-ffmpeg")
        for _ in range(200):
            if cdp.acked >= cdp.total:
                break
            await asyncio.sleep(0.01)

        monkeypatch.setattr("skills.impl.browser_use.shutil.which", lambda _name: None)
        with caplog.at_level(logging.WARNING, logger="feral.skill.browser"):
            stopped = await ctrl.stop_recording(assemble=True)

        assert stopped["degraded"] == "ffmpeg_missing"
        assert stopped["video_path"] == ""
        assembled = stopped["assembled"]
        assert assembled["success"] is False
        assert assembled["missing_dependency"] == "ffmpeg"
        assert "ffmpeg" in assembled["error"]
        assert Path(assembled["frames_dir"]).is_dir()
        assert assembled["frame_count"] == 4
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("ffmpeg not found on PATH" in w for w in warnings), warnings

        # The frames survive, so the recording can be assembled later.
        assert len(list(Path(stopped["directory"], "frames").glob("*.jpg"))) == 4

    async def test_assemble_missing_recording_is_an_error(self, feral_home) -> None:
        ctrl = BrowserController()
        out = await ctrl.assemble_recording("does-not-exist")
        assert out["success"] is False
        assert "No recording manifest" in out["error"]

    def test_concat_script_uses_absolute_paths_and_repeats_the_last_frame(
        self, tmp_path
    ) -> None:
        frames = [
            {"file": "000001.jpg", "duration_seconds": 0.05},
            {"file": "000002.jpg", "duration_seconds": 0.25},
        ]
        script = BrowserController._build_concat_script(tmp_path, frames)
        lines = script.strip().splitlines()
        assert lines[0] == "ffconcat version 1.0"
        assert lines[1] == f"file '{tmp_path / 'frames' / '000001.jpg'}'"
        assert lines[2] == "duration 0.050000"
        assert lines[4] == "duration 0.250000"
        # Final entry repeated, or the concat demuxer drops the last frame.
        assert lines[-1] == f"file '{tmp_path / 'frames' / '000002.jpg'}'"

    def test_frame_durations_clamp_stalls_and_duplicates(self) -> None:
        frames = [
            {"timestamp": 100.0},
            {"timestamp": 100.0},      # duplicate timestamp -> min clamp
            {"timestamp": 400.0},      # five-minute stall -> max clamp
            {"timestamp": 400.1},
        ]
        durations = BrowserController._frame_durations(frames)
        assert len(durations) == 4
        assert durations[0] == pytest.approx(RECORDING_MIN_FRAME_SECONDS, rel=1e-3)
        assert durations[1] == RECORDING_MAX_FRAME_SECONDS
        assert durations[2] == pytest.approx(0.1, abs=1e-6)
        assert RECORDING_MIN_FRAME_SECONDS <= durations[3] <= RECORDING_MAX_FRAME_SECONDS

    @pytest.mark.skipif(
        not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
        reason="ffmpeg/ffprobe not installed",
    )
    async def test_real_ffmpeg_assembles_a_playable_mp4(self, feral_home) -> None:
        pil = pytest.importorskip("PIL.Image")
        ctrl = BrowserController()
        directory = ctrl._recordings_root / "real-ffmpeg"
        frames_dir = directory / "frames"
        frames_dir.mkdir(parents=True)

        frames = []
        for index in range(1, 13):
            image = pil.new("RGB", (320, 200), (index * 20 % 256, 40, 200))
            image.save(frames_dir / f"{index:06d}.jpg", "JPEG", quality=70)
            frames.append({
                "index": index,
                "file": f"{index:06d}.jpg",
                "timestamp": 1_700_000_000.0 + index * 0.1,
                "offset_seconds": (index - 1) * 0.1,
                "duration_seconds": 0.1,
            })
        (directory / "manifest.json").write_text(
            json.dumps({
                "schema_version": 1,
                "recording_id": "real-ffmpeg",
                "duration_seconds": 1.2,
                "frames": frames,
            }),
            encoding="utf-8",
        )

        out = await ctrl.assemble_recording("real-ffmpeg")
        assert out["success"] is True, out
        assert out["degraded"] == ""
        video = Path(out["video_path"])
        assert video.is_file() and video.stat().st_size > 0

        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(video)],
            capture_output=True, text=True, check=True,
        )
        duration = float(json.loads(probe.stdout)["format"]["duration"])
        # 12 frames held 0.1s each; tolerate container rounding.
        assert 0.9 <= duration <= 1.5, duration

    async def test_list_recordings_reports_stored_sessions(self, feral_home) -> None:
        ctrl = BrowserController()
        cdp = AckGatedCDP(total=3, window=2)
        ctrl._cdp = cdp
        await ctrl.start_recording(name="listed")
        for _ in range(200):
            if cdp.acked >= cdp.total:
                break
            await asyncio.sleep(0.01)
        await ctrl.stop_recording(assemble=False)

        listed = await ctrl.list_recordings()
        assert listed["success"] is True
        ids = [r["recording_id"] for r in listed["recordings"]]
        assert "listed" in ids
        entry = next(r for r in listed["recordings"] if r["recording_id"] == "listed")
        assert entry["frame_count"] == 3


class TestAgainstStubCDPServer:
    """End-to-end over real WebSockets, against a stub that speaks CDP.

    The fake above drives ``_on_screencast_frame`` directly; this one goes
    through ``CDPConnection``'s receive loop, so it also covers listener
    dispatch, out-of-band event routing, and the ack travelling back over
    the socket as a normal command.

    The stub also reproduces the second trap: ``/json/version`` hands back
    the *browser* target, which rejects every ``Page.*`` command. A real
    Chrome behaves exactly this way, and recording has to notice and
    attach to a tab instead.
    """

    @staticmethod
    def _stub_app(total: int, window: int, port_holder: dict):
        import aiohttp
        from aiohttp import web

        async def json_version(_request):
            port = port_holder["port"]
            return web.json_response({
                "webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/browser/b1",
            })

        async def json_list(_request):
            port = port_holder["port"]
            return web.json_response([{
                "id": "page1", "type": "page", "title": "Stub", "url": "about:blank",
                "webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/page/1",
            }])

        async def browser_ws(request):
            """Browser-level target: no Page domain, exactly like Chrome."""
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                if str(data.get("method", "")).startswith("Page."):
                    await ws.send_json({
                        "id": data["id"],
                        "error": {"code": -32601, "message": f"'{data['method']}' wasn't found"},
                    })
                else:
                    await ws.send_json({"id": data["id"], "result": {}})
            return ws

        async def page_ws(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            state = {"sent": 0, "acked": 0, "started": False}

            async def pump():
                while (
                    state["started"]
                    and state["sent"] < total
                    and (state["sent"] - state["acked"]) < window
                ):
                    state["sent"] += 1
                    await ws.send_json({
                        "method": "Page.screencastFrame",
                        "params": {
                            "data": FRAME_B64,
                            "sessionId": state["sent"],
                            "metadata": {
                                "timestamp": 1_700_000_000.0 + state["sent"] * 0.04,
                                "deviceWidth": 320,
                                "deviceHeight": 200,
                            },
                        },
                    })

            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                method = data.get("method", "")
                if method == "Runtime.evaluate":
                    await ws.send_json({"id": data["id"], "result": {
                        "result": {"value": json.dumps({"url": "https://stub.test/x", "title": "Stub"})},
                    }})
                    continue
                await ws.send_json({"id": data["id"], "result": {}})
                if method == "Page.startScreencast":
                    state["started"] = True
                    await pump()
                elif method == "Page.screencastFrameAck":
                    state["acked"] += 1
                    await pump()
                elif method == "Page.stopScreencast":
                    state["started"] = False
            return ws

        app = web.Application()
        app.router.add_get("/json/version", json_version)
        app.router.add_get("/json", json_list)
        app.router.add_get("/devtools/browser/b1", browser_ws)
        app.router.add_get("/devtools/page/1", page_ws)
        return app

    async def test_recording_survives_the_ack_window_over_a_socket(
        self, feral_home
    ) -> None:
        from aiohttp import web
        from skills.impl.browser_use import CDPConnection

        total, window = 15, 2
        port_holder: dict = {"port": 0}
        runner = web.AppRunner(self._stub_app(total, window, port_holder))
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port_holder["port"] = site._server.sockets[0].getsockname()[1]

        try:
            ctrl = BrowserController()
            ctrl._cdp = CDPConnection(host="127.0.0.1", port=port_holder["port"])
            assert await ctrl._cdp.connect() is True
            # This is what the controller gets in production, and it is
            # the socket that cannot run a screencast.
            assert ctrl._cdp.is_page_target is False

            started = await ctrl.start_recording(name="socket-test")
            assert started["success"] is True, started
            assert started["start_url"] == "https://stub.test/x"

            for _ in range(400):
                if len(ctrl._recording["frames"]) >= total:
                    break
                await asyncio.sleep(0.01)

            stopped = await ctrl.stop_recording(assemble=False)
            await ctrl._cdp.disconnect()
        finally:
            await runner.cleanup()

        # More than `window` frames can only arrive if every one was acked.
        assert stopped["frame_count"] == total, stopped
        assert stopped["ack_errors"] == 0
        frames_dir = Path(stopped["directory"]) / "frames"
        assert len(list(frames_dir.glob("*.jpg"))) == total


class TestManifestEndpoints:
    def test_recording_endpoints_are_advertised(self) -> None:
        from skills.impl.browser_use import get_browser_skill_manifest

        ids = {e["id"] for e in get_browser_skill_manifest()["endpoints"]}
        assert {"start_recording", "stop_recording", "assemble_recording", "list_recordings"} <= ids
