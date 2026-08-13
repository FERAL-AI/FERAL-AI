"""Vision context attach — Lane 08 WS4 (gates S5 Roomba scenario).

The orchestrator calls into this module after assembling the user
message but BEFORE handing the messages to the LLM. When the user is
in voice mode, ``vision.enabled=true`` in settings, AND a glasses /
phone-camera frame within the freshness window exists, this module
attaches the frame as an OpenAI-style ``image_url`` content block so
the model can ground "the room is messy, send the Roomba" against
what the user is actually looking at.

## Buffer contract (Lane 08 ↔ Lane 11)

Lane 11 owns ``feral-core/perception/glasses_buffer.py`` (HUP
``glasses_frame`` ingest). The buffer's read API is parent-acked:

```python
class GlassesBuffer:
    def ingest(self, payload: dict, *, node_id: str = "") -> GlassesFrame | None: ...
    def latest(
        self,
        device_id: str | None = None,   # None = freshest across devices
        *,
        max_age_s: float | None = None,  # default: buffer's 30 s gate
    ) -> GlassesFrame | None: ...
    def device_ids_with_frames(self) -> list[str]: ...

def get_glasses_buffer() -> GlassesBuffer | None: ...
```

The three names above are what the module actually exports; this
block previously documented `push` / `device_ids`, neither of which
ever existed, which is how the missing `get_glasses_buffer` went
unnoticed.

This module imports it through a lazy lookup so we don't crash if
the buffer module is missing, but a lookup that fails now logs at
WARNING instead of falling through silently.

## Parent acceptance reminder #1 (2026-05-22T18:40Z)

When ``vision.enabled=true`` but no frame within 30 s, **do NOT
emit a stale image**. Emit nothing and let the LLM ask if it needs
visual context. Pinned by
``tests/test_vision_context_attach.py::test_stale_frame_omitted``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional, Union

try:
    # Module-level so tests can monkeypatch
    # ``perception.context_attach.load_settings``. The import is
    # cheap (Lane 06 + Lane 03's ConfigLoader is side-effect-free per
    # R2-001) so we don't lazy-load this anymore.
    from config.loader import load_settings as _load_settings_from_loader
except Exception:  # pragma: no cover - defensive
    _load_settings_from_loader = None


def load_settings() -> dict:
    """Return the merged settings dict; ``{}`` on any failure.

    Wrapper around ``config.loader.load_settings`` so tests can
    monkeypatch this single symbol without reaching into the loader.
    """
    if _load_settings_from_loader is None:
        return {}
    try:
        return _load_settings_from_loader() or {}
    except Exception:
        logger.debug("load_settings raised", exc_info=True)
        return {}


logger = logging.getLogger("feral.perception.context_attach")


# ── Public contract ───────────────────────────────────────────────


# OpenAI / Anthropic vision payload shape.
ImageUrlBlock = dict[str, Any]
TextBlock = dict[str, Any]
LLMUserContent = Union[str, list[dict]]


# ──────────────────────────────────────────────────────────────────
# Freshness + voice-mode gating
# ──────────────────────────────────────────────────────────────────


_DEFAULT_MAX_FRAME_AGE_S: float = 30.0


def is_voice_mode(context: Optional[dict]) -> bool:
    """Return ``True`` when this turn is a voice turn.

    The orchestrator marks voice turns by setting ``channel`` to
    ``voice`` / ``voice_command`` or ``voice_mode=True`` in the
    context envelope.
    """
    if not isinstance(context, dict):
        return False
    channel = str(context.get("channel") or "").lower()
    if channel in ("voice", "voice_command"):
        return True
    if context.get("voice_mode") is True:
        return True
    return False


def _should_attempt_attach(context: Optional[dict]) -> bool:
    """Both voice turns AND the explicit phone ``vision_ask``
    channel trigger the vision attach. Text/chat turns do NOT —
    the user owns the camera-reach decision in those modes.
    """
    if is_voice_mode(context):
        return True
    if isinstance(context, dict):
        channel = str(context.get("channel") or "").lower()
        if channel == "vision_ask":
            return True
    return False


def is_vision_enabled() -> bool:
    """Read ``vision.enabled`` (and the ``features.vision`` mirror)
    from the merged settings dict. Defaults to ``False`` when
    settings can't be loaded so we never accidentally exfiltrate a
    frame on a misconfigured brain.
    """
    settings = load_settings()
    if not isinstance(settings, dict):
        return False

    vision_cfg = settings.get("vision") or {}
    features_cfg = settings.get("features") or {}

    if isinstance(vision_cfg, dict) and vision_cfg.get("enabled") is True:
        return True
    if isinstance(features_cfg, dict) and features_cfg.get("vision") is True:
        return True
    return False


# ──────────────────────────────────────────────────────────────────
# Buffer lookup
# ──────────────────────────────────────────────────────────────────


_MISSING_ACCESSOR_WARNED = False


def _get_glasses_buffer() -> Any | None:
    """Return Lane 11's glasses buffer singleton or ``None``.

    Lazy import so the orchestrator boot path never hard-requires
    Lane 11's module. Production: ``perception.glasses_buffer`` is
    available and serves real frames. Tests: tests inject a fake via
    :func:`attach_vision_context`'s ``glasses_buffer`` keyword override.

    The accessor lookup used to be
    ``getattr(glasses_buffer, "get_glasses_buffer", lambda: None)()``.
    The module never defined that symbol, so the default silently won
    on every turn: the buffer had frames in it and this function
    returned ``None`` anyway, for the whole life of the feature. A
    getattr probe for a name that does not exist is indistinguishable
    from "the feature is not installed", so the absence is now logged
    once at WARNING with the fix in the message instead of vanishing.
    """
    global _MISSING_ACCESSOR_WARNED
    try:
        from perception import glasses_buffer  # type: ignore
    except Exception:
        logger.debug("perception.glasses_buffer import failed", exc_info=True)
        return None
    accessor = getattr(glasses_buffer, "get_glasses_buffer", None)
    if not callable(accessor):
        if not _MISSING_ACCESSOR_WARNED:
            _MISSING_ACCESSOR_WARNED = True
            logger.warning(
                "perception.glasses_buffer has no get_glasses_buffer(); no "
                "glasses frame can ever be attached to a turn. Frames are "
                "still being written to state.glasses_buffer and read by "
                "nobody. Fix: export get_glasses_buffer() from "
                "perception/glasses_buffer.py."
            )
        return None
    try:
        return accessor()
    except Exception:
        logger.warning("get_glasses_buffer() raised", exc_info=True)
        return None


def _frame_data_url(frame: Any) -> str:
    """Best-effort extraction of a data URL from a frame object.

    Supports the Lane 11 ``GlassesFrame`` ``to_data_url()`` method
    AND the legacy ``api.state.VisionBuffer`` dict shape
    (``{"data_b64": ..., "encoding": ...}``) so the orchestrator can
    fall through to the existing phone-camera vision_ask path when
    Lane 11's buffer hasn't been wired yet.
    """
    if frame is None:
        return ""

    # Lane 11 contract — ``GlassesFrame.to_data_url()``.
    to_url = getattr(frame, "to_data_url", None)
    if callable(to_url):
        try:
            url = to_url()
            if isinstance(url, str) and url.startswith("data:"):
                return url
        except Exception:
            logger.debug("frame.to_data_url() raised", exc_info=True)

    # Legacy ``VisionBuffer`` dict shape.
    if isinstance(frame, dict):
        data_b64 = frame.get("data_b64") or ""
        encoding = frame.get("encoding", "jpeg")
        if data_b64:
            return f"data:image/{encoding};base64,{data_b64}"

    return ""


def _frame_age_seconds(frame: Any, *, now: Optional[float] = None) -> float:
    """Compute the age of a frame in seconds.

    Honours the Lane 11 ``GlassesFrame.age_seconds()`` method when
    present, otherwise falls back to ``now - frame["timestamp"]``
    for dict-shaped frames. Returns ``+inf`` when the timestamp is
    unknown so unknown-age frames are always treated as stale.
    """
    now = now if now is not None else time.time()
    age_fn = getattr(frame, "age_seconds", None)
    if callable(age_fn):
        try:
            return float(age_fn(now=now))
        except TypeError:
            try:
                return float(age_fn())
            except Exception:
                pass
        except Exception:
            pass

    ts = None
    if isinstance(frame, dict):
        ts = frame.get("timestamp")
    else:
        ts = getattr(frame, "timestamp", None)
    if ts is None:
        return float("inf")
    try:
        return float(now) - float(ts)
    except Exception:
        return float("inf")


# ──────────────────────────────────────────────────────────────────
# Public API consumed by the orchestrator
# ──────────────────────────────────────────────────────────────────


def attach_vision_context(
    user_content: LLMUserContent,
    *,
    context: Optional[dict],
    session_id: str,
    glasses_buffer: Any | None = None,
    vision_buffer: Any | None = None,
    max_age_s: float = _DEFAULT_MAX_FRAME_AGE_S,
    now: Optional[float] = None,
) -> LLMUserContent:
    """Return ``user_content`` possibly augmented with an ``image_url``
    block.

    Args:
        user_content: The current LLM-bound user content. Either a
            plain string (text-only turn) or an OpenAI-style content
            list. Both shapes are honoured.
        context: The orchestrator's per-turn context envelope.
            ``channel`` / ``voice_mode`` are read off this.
        session_id: For logging only.
        glasses_buffer: Optional override for Lane 11's buffer.
            Defaults to the module singleton via
            :func:`_get_glasses_buffer`. Tests inject a fake.
        vision_buffer: Optional legacy ``api.state.VisionBuffer``;
            consulted ONLY when the glasses buffer is empty AND the
            turn's channel is ``vision_ask``. Production passes
            ``orchestrator.vision_buffer`` here.
        max_age_s: Freshness window. Frames older than this are
            treated as if they don't exist — they're NOT attached.
            See parent-acked rule: "do not emit a stale image."
        now: Override for the freshness clock; tests pin against
            this for determinism.

    Returns:
        The (possibly augmented) user content. When no attach is
        warranted, the input is returned unchanged.
    """
    # Gate 1 — voice mode (or the explicit phone ``vision_ask``
    # channel) is the trigger for proactive attach. Text turns never
    # auto-attach so the user controls when their camera feed
    # reaches the LLM.
    if not _should_attempt_attach(context):
        return user_content

    # Gate 2 — feature flag. Defaults to off; the user opts in.
    if not is_vision_enabled():
        return user_content

    # Gate 3 — find a fresh frame.
    frame_url = _resolve_fresh_frame_url(
        glasses_buffer=glasses_buffer,
        vision_buffer=vision_buffer,
        context=context,
        max_age_s=max_age_s,
        now=now,
        session_id=session_id,
    )
    if not frame_url:
        # PARENT REMINDER #1 (2026-05-22T18:40Z): when vision is
        # enabled but the buffer is empty or stale, emit NOTHING.
        # The LLM gets the text-only turn and can ask if it needs
        # visual context.
        logger.debug(
            "[%s] vision_attach: no fresh frame within %.0fs — skipping",
            session_id[:8] if len(session_id) >= 8 else session_id,
            max_age_s,
        )
        return user_content

    return _merge_image_into_content(user_content, frame_url)


def _resolve_fresh_frame_url(
    *,
    glasses_buffer: Any | None,
    vision_buffer: Any | None,
    context: Optional[dict],
    max_age_s: float,
    now: Optional[float],
    session_id: str,
) -> str:
    """Find a fresh frame across the configured buffers.

    Priority:
      1. Lane 11 glasses buffer (any device_id within ``max_age_s``).
      2. Legacy ``vision_buffer.latest_data_url(node_id)`` when the
         turn is a ``vision_ask`` channel and a node_id is known
         (preserves the existing phone-camera fast path).
    """
    buf = glasses_buffer if glasses_buffer is not None else _get_glasses_buffer()
    if buf is not None:
        try:
            frame = buf.latest(device_id=None, max_age_s=max_age_s)
        except TypeError:
            # Older buffer signature without kw-only max_age_s.
            try:
                frame = buf.latest(None, max_age_s=max_age_s)
            except Exception:
                frame = None
        except Exception:
            frame = None
        if frame is not None:
            # Defensive — the buffer SHOULD only return frames within
            # max_age_s, but if a buggy implementation returns stale
            # frames we still gate here.
            age = _frame_age_seconds(frame, now=now)
            if age <= max_age_s:
                url = _frame_data_url(frame)
                if url:
                    logger.info(
                        "[%s] vision_attach: glasses frame age=%.1fs source=%s",
                        session_id[:8] if len(session_id) >= 8 else session_id,
                        age,
                        getattr(frame, "source", "glasses"),
                    )
                    return url

    # Legacy VisionBuffer fallback — only when the explicit vision_ask
    # channel is in use AND the caller passed a node id.
    if vision_buffer is not None and isinstance(context, dict):
        node_id = context.get("source_node") or context.get("node_id") or ""
        channel = str(context.get("channel") or "").lower()
        if node_id and channel == "vision_ask":
            try:
                url = vision_buffer.latest_data_url(node_id)
                if isinstance(url, str) and url.startswith("data:"):
                    logger.info(
                        "[%s] vision_attach: legacy vision_buffer node=%s",
                        session_id[:8] if len(session_id) >= 8 else session_id,
                        node_id,
                    )
                    return url
            except Exception:
                logger.debug("legacy vision_buffer lookup failed", exc_info=True)

    return ""


def _merge_image_into_content(
    user_content: LLMUserContent,
    image_url: str,
) -> LLMUserContent:
    """Splice an ``image_url`` block into the user content.

    If the content is a plain string, return ``[{text}, {image_url}]``.
    If it's already a list, append the image block AFTER existing
    text blocks so the LLM reads "what the user said" → "and here is
    what they're seeing" in that order.
    """
    image_block: ImageUrlBlock = {
        "type": "image_url",
        "image_url": {"url": image_url, "detail": "low"},
    }

    if isinstance(user_content, str):
        return [
            {"type": "text", "text": user_content},
            image_block,
        ]

    if isinstance(user_content, list):
        # Don't double-attach: if a vision_data_url is already there
        # (e.g. PerceptionFrame populated by the legacy phone path)
        # we leave the content unchanged.
        has_image = any(
            isinstance(b, dict) and b.get("type") == "image_url"
            for b in user_content
        )
        if has_image:
            return user_content
        return list(user_content) + [image_block]

    # Unknown shape — be conservative and return unchanged.
    return user_content


__all__ = [
    "attach_vision_context",
    "is_voice_mode",
    "is_vision_enabled",
]
