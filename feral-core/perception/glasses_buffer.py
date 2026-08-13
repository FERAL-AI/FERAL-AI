"""GlassesBuffer — per-device circular buffer for HUP v1.3.0 ``glasses_frame``.

The brain's ``daemon_session`` handler writes accepted ``glasses_frame``
payloads into ``state.glasses_buffer`` via :meth:`GlassesBuffer.ingest`.
The orchestrator's vision-context-attach (Lane 08, owns
``perception/context_attach.py``) reads via
:meth:`GlassesBuffer.latest` / :meth:`GlassesBuffer.recent` with a
30 s freshness gate.

Design choices the parent acked + Lane 08 agreed on (see ASOS/AUDIT-r14/
phase2/WORK_LOG.md Agent 11 entry):

- Bucket by ``device_id``, not HUP node id. A phone forwarding W610
  frames carries the phone as the HUP-level ``node_id`` and the
  glasses serial as ``device_id``; queries like "what is the W610
  seeing right now?" must hit the glasses bucket, not the phone.
- Default ring size 30 frames per ``device_id``. A typical glasses
  stream is ~1-2 fps so 30 frames covers the last 15-30 s of activity
  comfortably and stays bounded under sustained streaming.
- Default freshness gate 30 s. Tunable via the brain-side setting
  ``vision.glasses_buffer.max_age_s``; the orchestrator's
  vision-context-attach honors a different gate so a 2-second turn
  doesn't pull a 28-second-old frame and pretend it's "what the user
  sees right now".
- Buffer is observation-only: no callbacks fire when frames land. The
  orchestrator polls on its own turn cadence — frames don't push
  context attach. Avoids a feedback loop where every frame triggers a
  vision-LLM call.
- ``GlassesFrame`` is immutable (``frozen=True``) so the orchestrator
  reading the buffer can't accidentally mutate a frame and confuse a
  later reader. Cheap to construct (no copying of base64).
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


logger = logging.getLogger("feral.perception.glasses_buffer")


# Source-of-truth list of provenance labels. Kept open (we accept any
# string the daemon sends) but the brain uses these to cost-budget
# vision-LLM calls (e.g. cheaper tier for ``camera_fallback`` than
# ``w610``). Lane 08 reads this catalog; Lane 11 owns it.
KNOWN_SOURCES = frozenset({
    "glasses",
    "phone_camera",
    "screen_loop",
    "w610",
    "camera_fallback",
    "jw_w300",
    "browser_camera",
})


@dataclass(frozen=True)
class GlassesFrame:
    """One frame in the glasses buffer.

    Fields exactly mirror the HUP v1.3.0 ``glasses_frame`` payload
    schema documented in ``feral-nodes/HUP_SPEC.md`` §5.4.3, plus
    ``node_id`` (the HUP-level node id that forwarded the frame —
    needed so the orchestrator can route ``hup_action_request`` back
    through the correct WS) and ``ingested_at`` (the brain-side
    wall-clock when the frame landed, used for the freshness gate
    when ``timestamp`` is suspect / missing).

    ``data_b64`` is stored verbatim. The orchestrator's
    vision-context-attach builds a ``data:image/{encoding};base64,...``
    URL via :meth:`to_data_url` when attaching to an LLM call so the
    base64 string is computed once at ingest and reused on every read.
    """

    device_id: str
    timestamp: float
    data_b64: str
    encoding: str = "jpeg"
    source: str = "glasses"
    node_id: str = ""
    width: Optional[int] = None
    height: Optional[int] = None
    sequence: Optional[int] = None
    ingested_at: float = field(default_factory=time.time)

    def age_seconds(self, *, now: Optional[float] = None) -> float:
        """Seconds elapsed since the frame was captured.

        Uses ``timestamp`` (capture wall-clock) when available, falling
        back to ``ingested_at`` (brain-side receive time) when the
        daemon-supplied timestamp is missing / zero / clock-skewed
        backwards. Negative ages clamp to 0 because a future-stamped
        frame is almost certainly a clock drift bug and we don't want
        the freshness gate to short-circuit.
        """
        ref = now if now is not None else time.time()
        ts = self.timestamp if self.timestamp and self.timestamp > 0 else self.ingested_at
        age = ref - ts
        return age if age > 0 else 0.0

    def to_data_url(self) -> str:
        """Serialize the frame as a ``data:`` URL for LLM ``image_url`` input.

        The orchestrator builds multimodal LLM requests with
        ``{"type": "image_url", "image_url": {"url": frame.to_data_url()}}``
        — matches OpenAI / Anthropic / Gemini conventions.
        """
        encoding = self.encoding if self.encoding in {"jpeg", "png", "webp"} else "jpeg"
        # MIME type 'image/jpeg' is correct (not 'image/jpg').
        mime = f"image/{encoding}"
        return f"data:{mime};base64,{self.data_b64}"


class GlassesBuffer:
    """Per-``device_id`` ring buffer of :class:`GlassesFrame` instances.

    Thread-safety: the brain runs single-threaded asyncio so explicit
    locking would be overkill. All ``deque`` operations used here are
    atomic in CPython under the GIL, and the buffer is only ever
    touched from the event loop (`_handle_glasses_frame` on the write
    side, `context_attach.py` on the read side).

    Capacity policy: 30 frames per device by default. The newest frame
    evicts the oldest when the ring is full; no per-device frame
    counter is kept because the ``deque`` handles it natively.
    """

    DEFAULT_MAX_FRAMES = 30
    DEFAULT_MAX_AGE_S = 30.0

    def __init__(
        self,
        *,
        max_frames_per_device: int = DEFAULT_MAX_FRAMES,
        max_age_s: float = DEFAULT_MAX_AGE_S,
    ) -> None:
        if max_frames_per_device <= 0:
            raise ValueError("max_frames_per_device must be > 0")
        self._max_frames = int(max_frames_per_device)
        self._max_age_s = float(max_age_s)
        self._buckets: dict[str, deque[GlassesFrame]] = {}

    # ─────────────────────────────────────────────
    # Write path (Lane 11 owns)
    # ─────────────────────────────────────────────

    def ingest(self, payload: dict, *, node_id: str = "") -> Optional[GlassesFrame]:
        """Ingest a ``glasses_frame`` payload into the buffer.

        The payload is the inner ``payload`` dict of the HUP envelope
        (already pydantic-validated at the daemon_session level when
        the wire dispatcher matched it against ``GlassesFramePayload``,
        but we also accept partially-validated dicts here so the
        handler can call us without re-validating).

        Returns the constructed :class:`GlassesFrame` for tests + the
        orchestrator's ``resolve_pending_frame`` path, or ``None`` if
        the payload is unusable (missing ``device_id`` or ``data_b64``).
        Never raises — write path failures must not crash the WS.
        """
        if not isinstance(payload, dict):
            logger.debug("glasses_buffer.ingest: non-dict payload dropped")
            return None
        device_id = str(payload.get("device_id") or "").strip()
        data_b64 = payload.get("data_b64") or ""
        if not device_id or not data_b64:
            logger.debug(
                "glasses_buffer.ingest: missing device_id or data_b64; dropped"
            )
            return None

        # Coerce numerics defensively — the HUP envelope is JSON so
        # numbers can come through as ints OR floats; sequence
        # specifically often arrives as a string from older daemons.
        timestamp_raw = payload.get("timestamp")
        try:
            timestamp = float(timestamp_raw) if timestamp_raw is not None else time.time()
        except (TypeError, ValueError):
            timestamp = time.time()

        def _int_or_none(key: str) -> Optional[int]:
            v = payload.get(key)
            if v is None:
                return None
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        frame = GlassesFrame(
            device_id=device_id,
            timestamp=timestamp,
            data_b64=data_b64,
            encoding=str(payload.get("encoding") or "jpeg"),
            source=str(payload.get("source") or "glasses"),
            node_id=node_id or "",
            width=_int_or_none("width"),
            height=_int_or_none("height"),
            sequence=_int_or_none("sequence"),
        )
        bucket = self._buckets.get(device_id)
        if bucket is None:
            bucket = deque(maxlen=self._max_frames)
            self._buckets[device_id] = bucket
        bucket.append(frame)
        return frame

    # ─────────────────────────────────────────────
    # Read path (Lane 08 owns via context_attach.py)
    # ─────────────────────────────────────────────

    def latest(
        self,
        device_id: Optional[str] = None,
        *,
        max_age_s: Optional[float] = None,
    ) -> Optional[GlassesFrame]:
        """Return the freshest frame, optionally filtered by ``device_id``.

        When ``device_id`` is None, pick the freshest frame across all
        devices — matches the orchestrator's "pick best camera" logic
        in ``perception.pick_best_camera``. When ``device_id`` is set,
        look up just that device's ring.

        ``max_age_s`` defaults to the buffer's configured limit
        (30 s). Pass ``float('inf')`` to disable the age gate (e.g.
        for diagnostics / introspection endpoints).
        """
        gate = float(max_age_s) if max_age_s is not None else self._max_age_s
        now = time.time()

        if device_id is not None:
            bucket = self._buckets.get(device_id)
            if not bucket:
                return None
            candidate = bucket[-1]
            return candidate if candidate.age_seconds(now=now) <= gate else None

        # Pick across all buckets — freshest first.
        best: Optional[GlassesFrame] = None
        for bucket in self._buckets.values():
            if not bucket:
                continue
            candidate = bucket[-1]
            if candidate.age_seconds(now=now) > gate:
                continue
            if best is None or candidate.timestamp > best.timestamp:
                best = candidate
        return best

    def recent(
        self,
        device_id: Optional[str] = None,
        *,
        max_count: int = 3,
        max_age_s: Optional[float] = None,
    ) -> list[GlassesFrame]:
        """Return up to ``max_count`` recent frames, freshest first.

        When ``device_id`` is None, returns recent frames across all
        devices interleaved by ``timestamp``. Used by the
        orchestrator's multi-actuator chain to attach the last
        2-3 frames so the LLM sees the user's recent gaze, not a
        single instantaneous snapshot.
        """
        if max_count <= 0:
            return []
        gate = float(max_age_s) if max_age_s is not None else self._max_age_s
        now = time.time()

        if device_id is not None:
            bucket = self._buckets.get(device_id)
            if not bucket:
                return []
            frames = [f for f in bucket if f.age_seconds(now=now) <= gate]
        else:
            frames = []
            for bucket in self._buckets.values():
                frames.extend(
                    f for f in bucket if f.age_seconds(now=now) <= gate
                )

        frames.sort(key=lambda f: f.timestamp, reverse=True)
        return frames[:max_count]

    def latest_data_urls(
        self,
        *,
        max_count: int = 3,
        max_age_s: Optional[float] = None,
    ) -> list[str]:
        """Convenience wrapper for the orchestrator's LLM attach path.

        Returns up to ``max_count`` ``data:image/...;base64,...`` URLs
        across all devices, freshest first. Empty list when no fresh
        frames are available — the orchestrator falls back to
        ``VisionBuffer`` (legacy phone vision path) when this returns
        empty per the Lane 08 contract.
        """
        return [f.to_data_url() for f in self.recent(
            max_count=max_count, max_age_s=max_age_s
        )]

    def device_ids_with_frames(self) -> list[str]:
        """List devices currently holding at least one (any-age) frame."""
        return [device_id for device_id, bucket in self._buckets.items() if bucket]


# ─────────────────────────────────────────────
# Process-wide accessor (the read path's entry point)
# ─────────────────────────────────────────────
#
# WHY this exists: ``perception/context_attach.py`` is the only production
# reader of this buffer, and it resolved the buffer with
#
#     getattr(glasses_buffer, "get_glasses_buffer", lambda: None)()
#
# This module never defined ``get_glasses_buffer``, so the probe fell
# through to ``lambda: None`` on every turn and the reader concluded
# "Lane 11's buffer has not merged yet". Measured on a real brain
# (TestClient + a real ``/v1/node`` socket): a ``glasses_frame`` landed
# in ``state.glasses_buffer`` (``device_ids_with_frames() ==
# ['w610-PROBE']``) and the very next ``orchestrator._attach_vision_context``
# on a voice turn attached no image. Every frame a pair of glasses ever
# sent was written to a buffer that nothing could read.
#
# The instance that matters is the one on ``BrainState`` (api/state.py),
# because that is what ``_handle_glasses_frame`` writes to. So the
# accessor returns the registered instance, never a fresh empty one: a
# second buffer would read as "connected but seeing nothing", which is
# the failure this replaces.
_active_buffer: Optional[GlassesBuffer] = None


def set_glasses_buffer(buffer: Optional[GlassesBuffer]) -> None:
    """Register the process-wide buffer. Called by ``BrainState.__init__``."""
    global _active_buffer
    _active_buffer = buffer


def get_glasses_buffer() -> Optional[GlassesBuffer]:
    """Return the live glasses buffer, or ``None`` when the brain has none.

    Prefers the explicitly registered instance. Falls back to reading
    ``api.state.state.glasses_buffer`` so a brain built before
    :func:`set_glasses_buffer` existed (or a test that constructs
    ``BrainState`` directly) still resolves the same object the write
    path uses. Never fabricates an empty buffer.
    """
    global _active_buffer
    if _active_buffer is not None:
        return _active_buffer
    try:
        from api.state import state as _brain_state

        candidate = getattr(_brain_state, "glasses_buffer", None)
    except Exception:  # pragma: no cover - api.state absent (unit tests)
        logger.debug("get_glasses_buffer: api.state unavailable", exc_info=True)
        return None
    if isinstance(candidate, GlassesBuffer):
        _active_buffer = candidate
    return _active_buffer


__all__ = [
    "GlassesBuffer",
    "GlassesFrame",
    "KNOWN_SOURCES",
    "get_glasses_buffer",
    "set_glasses_buffer",
]
