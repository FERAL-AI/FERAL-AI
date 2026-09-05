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
import time
from pathlib import Path
from typing import Any, Dict, Optional

from skills.base import BaseSkill
from skills.impl import register_skill


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
        ts = int(time.time() * 1000)
        save_path = Path(args.get("path") or f"/tmp/feral_screen_{ts}.png").expanduser()
        save_path.parent.mkdir(parents=True, exist_ok=True)

        region = str(args.get("region", "") or "").strip()
        capture = await self._capture_screen(save_path, region=region)
        if not capture["success"]:
            return {"success": False, "status_code": 500, "data": None, "error": capture["error"]}

        raw = save_path.read_bytes()
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
                if query and analysis:
                    answer = analysis.get("answer") or analysis.get("scene_description")
                    if answer:
                        data["analysis_text"] = answer
            else:
                data["analysis_error"] = (
                    "No VLM available. Set OPENAI_API_KEY or FERAL_VLM_PROVIDER."
                )

        return {"success": True, "status_code": 200, "data": data, "error": None}

    async def _capture_screen(self, save_path: Path, region: str = "") -> dict:
        system = platform.system().lower()
        if system == "darwin":
            return await self._capture_macos(save_path, region=region)
        if system == "linux":
            return await self._capture_linux(save_path)
        return {"success": False, "error": f"Unsupported OS for screen capture: {system}"}

    async def _capture_macos(self, save_path: Path, region: str = "") -> dict:
        cmd = ["screencapture", "-x"]
        if region:
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
