"""perception_query — "what do I see right now?" skill.

Implements the natural-language path from the baseline-ideas-perception-share
plan: user says "FERAL, what am I looking at?" → orchestrator calls this
skill → best-camera picker finds the right HUP daemon (browser share, iOS
CameraPermissionAdapter, W610 glasses, anything) → capture request →
scene describe → reply with JPEG + natural-language summary inline.

The skill intentionally stays small: all heavy lifting (vision_request
round-trip, scene VLM) is already in the orchestrator + SceneAnalyzer.
This module only:

1. Picks the best camera daemon.
2. Delegates to ``orchestrator.request_frame(node_id, ...)``.
3. Fires ``scene.analyze_frame(data_b64, ...)`` on the returned frame.
4. Flattens everything into the SkillExecutor contract.

`autonomy_tier=user_confirm` lives in the manifest's ``categories`` +
``permissions`` arrays because we don't yet have a dedicated field on
SkillManifest. The tag is machine-readable and already respected by
callers that grep for it.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from skills.base import BaseSkill
from skills.impl import register_skill

logger = logging.getLogger("feral.skills.perception_query")

# Capability names advertised by real camera daemons. Matched in priority
# order — the first hit wins, with most-recent-frame as the tiebreaker.
CAMERA_CAPABILITIES = (
    "iphone_camera",
    "browser_camera",
    "w610_camera",
    "camera",
)


def _parse_resolution(text: str, fallback: tuple[int, int] = (640, 480)) -> tuple[int, int]:
    try:
        parts = str(text).lower().replace(" ", "").split("x")
        if len(parts) == 2:
            w = int(parts[0])
            h = int(parts[1])
            if w > 0 and h > 0:
                return (w, h)
    except Exception:
        pass
    return fallback


def _clamp_quality(raw) -> int:
    try:
        q = int(float(raw))
    except Exception:
        return 70
    return max(1, min(100, q))


def pick_best_camera(daemons: Dict[str, Any], vision_buffer=None) -> Optional[str]:
    """Return the node_id of the preferred camera daemon.

    Priority:
      1. Capability match order (``iphone_camera`` > ``browser_camera`` >
         ``w610_camera`` > ``camera``).
      2. Within a capability tier, prefer the daemon with the most recent
         frame in ``vision_buffer`` so stale disconnects don't win.
      3. Otherwise insertion order of ``state.daemons`` (stable).
    """
    if not daemons:
        return None

    def _latest_ts(node_id: str) -> float:
        if vision_buffer is None:
            return 0.0
        frame = getattr(vision_buffer, "latest", lambda _nid: None)(node_id)
        if not isinstance(frame, dict):
            return 0.0
        ts = frame.get("timestamp") or frame.get("ts") or 0.0
        try:
            return float(ts)
        except (TypeError, ValueError):
            return 0.0

    # Scan every capability tier in order so iPhone wins over W610 even
    # when both report frames.
    for cap in CAMERA_CAPABILITIES:
        tier: List[tuple[str, Any]] = []
        for node_id, ws in daemons.items():
            caps = list(getattr(ws, "_feral_capabilities", []) or [])
            if cap in caps:
                tier.append((node_id, ws))
        if not tier:
            continue
        if len(tier) == 1:
            return tier[0][0]
        tier.sort(key=lambda row: _latest_ts(row[0]), reverse=True)
        return tier[0][0]

    # Fallback: if any daemon has pushed a frame recently, use it.
    if vision_buffer is not None:
        nodes_with_frames = getattr(vision_buffer, "node_ids_with_frames", lambda: [])() or []
        for node_id in nodes_with_frames:
            if node_id in daemons:
                return node_id
    return None


@register_skill
class PerceptionQuerySkill(BaseSkill):
    def __init__(self) -> None:
        super().__init__(skill_id="perception_query")

    async def execute(
        self,
        endpoint_id: str,
        args: Dict[str, Any],
        vault: Dict[str, str],
    ) -> Dict[str, Any]:
        _ = vault
        if endpoint_id not in ("what_do_i_see", "perception_query"):
            return {
                "success": False,
                "status_code": 404,
                "data": None,
                "error": f"Unknown endpoint: {endpoint_id}",
            }
        try:
            return await self._capture(args)
        except Exception as exc:
            logger.exception("perception_query failed")
            return {
                "success": False,
                "status_code": 500,
                "data": None,
                "error": str(exc),
            }

    async def _capture(self, args: Dict[str, Any]) -> Dict[str, Any]:
        from api.state import state

        orchestrator = state.orchestrator
        if orchestrator is None:
            return self._err(503, "Orchestrator not initialised")

        requested_node = (args.get("node_id") or "").strip()
        resolution = str(args.get("resolution", "640x480") or "640x480")
        width, height = _parse_resolution(resolution)
        quality = _clamp_quality(args.get("quality", 70))
        reason = str(args.get("reason", "") or "User asked what's in view")

        daemons = getattr(state, "daemons", {}) or {}
        vision_buffer = getattr(state, "vision_buffer", None)

        if requested_node:
            if requested_node not in daemons:
                return self._err(404, f"Camera daemon {requested_node!r} is not connected")
            node_id = requested_node
        else:
            node_id = pick_best_camera(daemons, vision_buffer=vision_buffer)
            if not node_id:
                return self._err(
                    404,
                    "No camera is currently connected. Share your phone's camera from "
                    "the Devices page or plug in a FERAL-HUP glasses daemon, then try again.",
                )

        logger.info(
            "perception_query: capturing %dx%d from node=%s (quality=%d, reason=%r)",
            width, height, node_id, quality, reason,
        )

        frame_payload: Optional[dict] = None
        try:
            frame_payload = await orchestrator.request_frame(
                node_id=node_id,
                resolution=f"{width}x{height}",
                quality=quality,
                reason=reason,
                timeout=float(args.get("timeout", 10.0)),
            )
        except Exception as exc:
            logger.warning("request_frame raised: %s", exc)

        if not frame_payload:
            # Fall back to the last cached frame if a live capture
            # didn't return in time — still honest: returns
            # success=False when there's nothing to show.
            if vision_buffer is not None:
                latest = getattr(vision_buffer, "latest", lambda _nid: None)(node_id)
                if isinstance(latest, dict):
                    frame_payload = latest

        if not frame_payload:
            return self._err(504, f"Camera {node_id} did not return a frame in time")

        data_b64 = frame_payload.get("data_b64", "") or ""
        encoding = frame_payload.get("encoding", "jpeg")
        frame_resolution = frame_payload.get("resolution") or [width, height]
        frame_id = frame_payload.get("frame_id") or frame_payload.get("id") or ""

        scene_description: Optional[str] = None
        scene_raw: Optional[dict] = None
        scene = getattr(state, "scene", None)
        # ``configured``, not ``available``: this is an explicit request
        # and it forces, so it probes a provider that the periodic loop
        # has backed off from. See SceneAnalyzer.analyze_frame.
        scene_ready = getattr(
            scene, "configured", getattr(scene, "available", False),
        )
        if scene is not None and scene_ready and data_b64:
            try:
                scene_raw = await scene.analyze_frame(
                    data_b64=data_b64,
                    encoding=encoding,
                    node_id=node_id,
                    force=True,
                    mode="query" if reason else "general",
                    query=reason,
                )
                if isinstance(scene_raw, dict):
                    scene_description = (
                        scene_raw.get("answer")
                        or scene_raw.get("scene_description")
                        or scene_raw.get("primary_content")
                    )
            except Exception as exc:
                logger.warning("scene.analyze_frame failed: %s", exc)

        data = {
            "frame_id": frame_id,
            "node_id": node_id,
            "resolution": list(frame_resolution),
            "encoding": encoding,
            "data_b64": data_b64,
            "scene_description": scene_description or "",
            "scene_details": scene_raw or {},
            "captured_at": frame_payload.get("timestamp") or time.time(),
            "autonomy_tier": "user_confirm",
        }
        return {
            "success": True,
            "status_code": 200,
            "data": data,
            "error": None,
        }

    @staticmethod
    def _err(code: int, message: str) -> Dict[str, Any]:
        return {
            "success": False,
            "status_code": code,
            "data": None,
            "error": message,
        }
