"""
FERAL Screen Capture Skill
===========================
Capture desktop screenshots and optionally analyze them with a VLM.
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import platform
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

from skills.base import BaseSkill
from skills.impl import register_skill


class _WindowCaptureError(ValueError):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.status = code, status


@register_skill
class ScreenCaptureSkill(BaseSkill):
    def __init__(self):
        super().__init__(skill_id="screen_capture")
        self._llm = None
        self._scene = None

    async def execute(self, endpoint_id: str, args: Dict[str, Any], vault: Dict[str, str]) -> Dict[str, Any]:
        _ = vault
        if endpoint_id not in ("capture", "capture_screen"):
            return {
                "success": False,
                "status_code": 404,
                "data": None,
                "error": f"Unknown endpoint: {endpoint_id}",
            }
        try:
            return await self._capture(args)
        except Exception as e:
            return {"success": False, "status_code": 500, "data": None, "error": str(e)}

    async def _capture(self, args: dict) -> dict:
        region = str(args.get("region", "") or "").strip()
        try:
            window = self._window_selection(args, region)
        except _WindowCaptureError as exc:
            return self._window_error(exc)
        ts = int(time.time() * 1000)
        save_path = Path(args.get("path") or f"/tmp/feral_screen_{ts}.png").expanduser()
        identity = None
        if window is not None:
            try:
                raw, identity = await self._capture_window(save_path, window)
            except _WindowCaptureError as exc:
                return self._window_error(exc)
        else:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            capture = await self._capture_screen(save_path, region=region)
            if not capture["success"]:
                return {"success": False, "status_code": 500, "data": None, "error": capture["error"]}
            raw = save_path.read_bytes()
        if identity is not None:
            image_b64, encoding = await asyncio.to_thread(self._encode_image, raw, save_path.suffix.lower())
        else:
            image_b64, encoding = self._encode_image(raw, save_path.suffix.lower())

        analyze = bool(args.get("analyze", False))
        query = str(args.get("query", "") or "").strip()
        include_image = bool(args.get("include_image", True))

        data: dict[str, Any] = {
            "path": str(save_path),
            "encoding": encoding,
            "captured_at": ts,
            "size_bytes": len(raw),
            "region": region or None,
        }
        if identity is not None:
            data.update(capture_source="macos_window", window=identity)
        if include_image:
            data["image_b64"] = image_b64

        if analyze or query:
            scene = self._get_scene_analyzer()
            # ``configured``, not ``available``: a VLM that is set up but
            # currently unreachable is still this install's vision path,
            # and an explicit capture is a fine moment to probe it (see
            # SceneAnalyzer.analyze_frame, which forces here).
            if scene and getattr(scene, "configured", getattr(scene, "available", False)):
                mode = "query" if query else "general"
                analysis = await scene.analyze_frame(
                    data_b64=image_b64,
                    encoding=encoding,
                    node_id="screen_capture",
                    force=True,
                    mode=mode,
                    query=query,
                )
                data["analysis"] = analysis
                if analysis is None:
                    health = getattr(scene, "provider_health", {}) or {}
                    if health.get("unreachable"):
                        # Naming the provider beats "no VLM available",
                        # which is what an unreachable-but-configured VLM
                        # used to report and is a different problem with a
                        # different fix.
                        data["analysis_error"] = (
                            f"Vision provider {health.get('provider', '?')} is "
                            f"unreachable: {health.get('detail', 'no connection')}"
                        )
                    else:
                        data["analysis_error"] = (
                            "Screenshot captured, but the selected vision provider produced no analysis. "
                            "The screen has not been interpreted."
                        )
                if query and analysis:
                    answer = analysis.get("answer") or analysis.get("scene_description")
                    if answer:
                        data["analysis_text"] = answer
            else:
                data["analysis_error"] = (
                    "No VLM available. Set OPENAI_API_KEY or FERAL_VLM_PROVIDER."
                )

        return {"success": True, "status_code": 200, "data": data, "error": None}

    @staticmethod
    def _window_error(exc: _WindowCaptureError) -> dict:
        return {"success": False, "status_code": exc.status, "data": None,
                "error": str(exc), "error_code": exc.code}

    @staticmethod
    def _window_selection(args: dict, region: str) -> dict | None:
        keys = ("window_id", "window_pid", "window_title", "window_app")
        if not any(key in args for key in keys):
            return None
        if (type(args.get("window_id")) is not int or not 0 < args["window_id"] < 2**32
                or type(args.get("window_pid")) is not int or not 0 < args["window_pid"] < 2**31
                or not isinstance(args.get("window_title"), str) or len(args["window_title"]) > 2048
                or ("window_app" in args and (not isinstance(args["window_app"], str)
                    or not args["window_app"] or len(args["window_app"]) > 256)) or region):
            raise _WindowCaptureError("window_capture_invalid",
                "Window capture requires exact window_id, window_pid and window_title from window_list; region cannot be combined.", 400)
        if platform.system().lower() != "darwin":
            raise _WindowCaptureError("window_capture_unsupported", "Window capture is supported on macOS only.", 400)
        return {"window_id": args["window_id"], "pid": args["window_pid"],
                "title": args["window_title"], "app": args.get("window_app")}

    @staticmethod
    def _window_metadata(selection: dict) -> dict:
        # Use the same actual CoreGraphics source as window_list. AppleScript
        # fallback rows have no CGWindowID and cannot select a capture surface.
        from skills.impl.gui_computer_use import _windows_via_quartz
        try:
            windows, error = _windows_via_quartz()
        except Exception:
            windows, error = None, "unavailable"
        if error or windows is None:
            raise _WindowCaptureError("window_capture_metadata_unavailable",
                "CoreGraphics window metadata is unavailable; no image was returned.", 503)
        matches = [row for row in windows if row.get("window_id") == selection["window_id"]]
        if len(matches) != 1:
            raise _WindowCaptureError("window_capture_stale", "Selected window is no longer uniquely available; refresh window_list.")
        row = matches[0]
        if (row.get("pid") != selection["pid"] or row.get("title") != selection["title"]
                or not isinstance(row.get("app"), str) or not row["app"]
                or (selection.get("app") is not None and row["app"] != selection["app"])
                or row.get("width", 0) <= 0 or row.get("height", 0) <= 0):
            raise _WindowCaptureError("window_capture_stale", "Selected window identity changed; refresh window_list.")
        return {key: row[key] for key in ("window_id", "pid", "app", "title")}

    async def _read_window_metadata(self, selection: dict) -> dict:
        try:
            return await asyncio.wait_for(asyncio.to_thread(self._window_metadata, selection), timeout=5)
        except asyncio.TimeoutError:
            raise _WindowCaptureError("window_capture_metadata_timeout", "Window identity check timed out; no image was returned.", 504) from None

    async def _capture_window(self, save_path: Path, selection: dict) -> tuple[bytes, dict]:
        identity = await self._read_window_metadata(selection)
        directory = await asyncio.to_thread(tempfile.mkdtemp, prefix="feral_window_")
        staging = Path(directory) / "capture.png"
        try:
            capture = await self._capture_macos(staging, window_id=selection["window_id"])
            if not capture["success"]:
                raise _WindowCaptureError("window_capture_failed",
                    "Selected window capture failed; check Screen Recording permission and window availability.", 500)
            if await self._read_window_metadata(identity) != identity:
                raise _WindowCaptureError("window_capture_stale", "Selected window identity changed during capture; no image was returned.")
            def read_image():
                from PIL import Image
                with Image.open(staging) as image:
                    image.verify()
                return staging.read_bytes()
            try:
                raw = await asyncio.to_thread(read_image)
            except Exception:
                raise _WindowCaptureError("window_capture_invalid_image", "Selected window capture produced no valid image.", 500) from None
            def publish():
                save_path.parent.mkdir(parents=True, exist_ok=True)
                save_path.write_bytes(raw)
            await asyncio.to_thread(publish)
            return raw, identity
        finally:
            await asyncio.to_thread(shutil.rmtree, directory)

    async def _capture_screen(self, save_path: Path, region: str = "") -> dict:
        system = platform.system().lower()
        if system == "darwin":
            return await self._capture_macos(save_path, region=region)
        if system == "linux":
            return await self._capture_linux(save_path)
        return {"success": False, "error": f"Unsupported OS for screen capture: {system}"}

    async def _capture_macos(self, save_path: Path, region: str = "", window_id: int | None = None) -> dict:
        cmd = ["screencapture", "-x"]
        if window_id is not None:
            cmd.extend(["-o", "-l", str(window_id)])
        elif region:
            cmd.append(f"-R{region}")
        cmd.append(str(save_path))
        return await self._run_capture_cmd(cmd)

    async def _capture_linux(self, save_path: Path) -> dict:
        candidates = []
        if shutil.which("import"):
            candidates.append(["import", "-window", "root", str(save_path)])
        if shutil.which("gnome-screenshot"):
            candidates.append(["gnome-screenshot", "-f", str(save_path)])
        if shutil.which("scrot"):
            candidates.append(["scrot", str(save_path)])

        if not candidates:
            return {
                "success": False,
                "error": "No screenshot tool found. Install ImageMagick ('import') or gnome-screenshot.",
            }

        last_err = ""
        for cmd in candidates:
            out = await self._run_capture_cmd(cmd)
            if out["success"]:
                return out
            last_err = out["error"]
        return {"success": False, "error": last_err or "Failed to capture screenshot"}

    async def _run_capture_cmd(self, cmd: list[str]) -> dict:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=20)
            if proc.returncode != 0:
                err = (stderr_b.decode(errors="replace") or stdout_b.decode(errors="replace")).strip()
                return {"success": False, "error": err or f"Capture command failed: {' '.join(cmd)}"}
            return {"success": True, "error": None}
        except asyncio.TimeoutError:
            return {"success": False, "error": "Screenshot capture timed out"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _get_scene_analyzer(self):
        if self._scene is not None:
            return self._scene
        try:
            # Reuse the initialized runtime's selection, budget and health.
            # Creating a second chat provider here performs unrelated discovery
            # and can use a different endpoint from the active vision runtime.
            from api.state import state

            if state.scene is not None:
                return state.scene
            from agents.llm_provider import LLMProvider
            from perception.scene import SceneAnalyzer

            self._llm = LLMProvider()
            self._scene = SceneAnalyzer(llm=self._llm)
            self._scene.set_cooldown(1.0)
        except Exception:
            self._scene = None
        return self._scene

    @staticmethod
    def _encode_image(raw: bytes, suffix: str) -> tuple[str, str]:
        encoding = suffix.lstrip(".").lower() or "png"
        try:
            from PIL import Image

            img = Image.open(io.BytesIO(raw))
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            if img.width > 1920:
                ratio = 1920 / img.width
                img = img.resize((1920, int(img.height * ratio)), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=80, optimize=True)
            return base64.b64encode(buf.getvalue()).decode("ascii"), "jpeg"
        except Exception:
            return base64.b64encode(raw).decode("ascii"), encoding
