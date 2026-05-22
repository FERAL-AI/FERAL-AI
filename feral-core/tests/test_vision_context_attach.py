"""Lane 08 WS4 — vision context attach (S5 prereq).

The Roomba scenario (``THESIS_SCENARIOS.md`` S5) needs the orchestrator
to attach the freshest glasses frame as an ``image_url`` content block
when:

  * The turn is in voice mode (``channel`` is ``voice``/``voice_command``
    or ``voice_mode=True`` in the context envelope)
  * ``vision.enabled`` is true in settings
  * Lane 11's ``glasses_buffer`` holds a frame within 30 seconds

This module pins:

  1. Happy path: voice + recent frame → image attached.
  2. Stale-frame negative case (PARENT REMINDER #1, 2026-05-22T18:40Z):
     voice + ``vision.enabled=true`` but no frame within 30s → emit
     NOTHING; the LLM can ask if it needs visual context.
  3. Text-mode never auto-attaches (user controls camera reach).
  4. Vision disabled → no attach even with a fresh frame.
  5. Buffer module missing (Lane 11 not yet merged) → no crash; the
     orchestrator returns the unmodified content.
  6. Multimodal content shape: when the user message is already a
     list, the image block is appended after existing text blocks
     so the LLM reads "user said X" then "here is what they see".
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional
from unittest.mock import patch

import pytest

from perception import context_attach
from perception.context_attach import attach_vision_context


# ── Fake glasses buffer (matches Lane 11 GlassesBuffer contract) ──


@dataclass
class _FakeFrame:
    device_id: str
    timestamp: float
    data_b64: str
    encoding: str = "jpeg"
    source: str = "glasses"

    def age_seconds(self, *, now: Optional[float] = None) -> float:
        now = now if now is not None else time.time()
        return now - self.timestamp

    def to_data_url(self) -> str:
        return f"data:image/{self.encoding};base64,{self.data_b64}"


class _FakeBuffer:
    """Test double for Lane 11's ``GlassesBuffer``."""

    def __init__(self, frame: Optional[_FakeFrame] = None) -> None:
        self.frame = frame

    def latest(
        self,
        device_id: Optional[str] = None,
        *,
        max_age_s: float = 30.0,
    ) -> Optional[_FakeFrame]:
        if self.frame is None:
            return None
        if self.frame.age_seconds() > max_age_s:
            return None
        if device_id and self.frame.device_id != device_id:
            return None
        return self.frame


# ── Helpers ────────────────────────────────────────────────────────


def _vision_enabled_settings(enabled: bool) -> dict:
    return {"vision": {"enabled": enabled}, "features": {}}


@pytest.fixture
def vision_on():
    with patch("perception.context_attach.load_settings",
               return_value=_vision_enabled_settings(True)):
        yield


@pytest.fixture
def vision_off():
    with patch("perception.context_attach.load_settings",
               return_value=_vision_enabled_settings(False)):
        yield


# ── Tests ──────────────────────────────────────────────────────────


class TestHappyPath:
    """Voice + vision.enabled + fresh frame → image attached."""

    def test_voice_voice_mode_flag_attaches_image(self, vision_on):
        now = time.time()
        buf = _FakeBuffer(_FakeFrame(
            device_id="glasses-1", timestamp=now - 5.0,
            data_b64="FRESH==", source="glasses",
        ))
        result = attach_vision_context(
            "the room is messy, start the vacuum",
            context={"voice_mode": True},
            session_id="s-aaaaaaaa",
            glasses_buffer=buf,
        )
        assert isinstance(result, list)
        text_blocks = [b for b in result if b.get("type") == "text"]
        image_blocks = [b for b in result if b.get("type") == "image_url"]
        assert len(text_blocks) == 1
        assert len(image_blocks) == 1
        assert text_blocks[0]["text"] == "the room is messy, start the vacuum"
        assert image_blocks[0]["image_url"]["url"].startswith("data:image/jpeg;base64,FRESH")

    @pytest.mark.parametrize("channel", ["voice", "voice_command"])
    def test_voice_channel_attaches_image(self, vision_on, channel):
        now = time.time()
        buf = _FakeBuffer(_FakeFrame(
            device_id="phone-cam", timestamp=now - 2.0, data_b64="P",
        ))
        result = attach_vision_context(
            "describe what you see",
            context={"channel": channel},
            session_id="s",
            glasses_buffer=buf,
        )
        assert isinstance(result, list)
        assert any(b.get("type") == "image_url" for b in result)


class TestStaleFrameOmitted:
    """PARENT REMINDER #1 — when no frame within 30s, do NOT emit a
    stale image. The LLM gets the text-only turn so it can ask if it
    needs visual context.
    """

    def test_stale_frame_omitted(self, vision_on):
        now = time.time()
        # 31 seconds old — exactly outside the window.
        buf = _FakeBuffer(_FakeFrame(
            device_id="glasses-1", timestamp=now - 31.0, data_b64="OLD==",
        ))
        result = attach_vision_context(
            "what do I see",
            context={"voice_mode": True},
            session_id="s",
            glasses_buffer=buf,
        )
        # Unchanged plain string — no image_url snuck in.
        assert result == "what do I see"

    def test_empty_buffer_omits_image(self, vision_on):
        buf = _FakeBuffer(frame=None)
        result = attach_vision_context(
            "what do I see",
            context={"voice_mode": True},
            session_id="s",
            glasses_buffer=buf,
        )
        assert result == "what do I see"

    def test_custom_max_age_window(self, vision_on):
        now = time.time()
        buf = _FakeBuffer(_FakeFrame(
            device_id="g", timestamp=now - 10.0, data_b64="X==",
        ))
        # 5s window — 10s-old frame fails the tighter freshness gate.
        result = attach_vision_context(
            "what do I see",
            context={"voice_mode": True},
            session_id="s",
            glasses_buffer=buf,
            max_age_s=5.0,
        )
        assert result == "what do I see"

        # Same frame, default 30s window — passes.
        result = attach_vision_context(
            "what do I see",
            context={"voice_mode": True},
            session_id="s",
            glasses_buffer=buf,
        )
        assert isinstance(result, list)


class TestGating:
    """Negative gates: text-mode never attaches; vision-off never
    attaches; missing buffer module never crashes."""

    def test_text_mode_never_attaches(self, vision_on):
        now = time.time()
        buf = _FakeBuffer(_FakeFrame(
            device_id="g", timestamp=now - 1.0, data_b64="F==",
        ))
        # Default text turn — no voice flag.
        result = attach_vision_context(
            "show me my notes",
            context={"channel": "chat"},
            session_id="s",
            glasses_buffer=buf,
        )
        assert result == "show me my notes"

    def test_vision_disabled_never_attaches(self, vision_off):
        now = time.time()
        buf = _FakeBuffer(_FakeFrame(
            device_id="g", timestamp=now - 1.0, data_b64="F==",
        ))
        result = attach_vision_context(
            "what do I see",
            context={"voice_mode": True},
            session_id="s",
            glasses_buffer=buf,
        )
        assert result == "what do I see"

    def test_missing_buffer_module_returns_unchanged(self, vision_on):
        # No glasses_buffer override AND module not installed → the
        # internal lazy import returns None. attach_vision_context
        # must NOT raise.
        with patch.object(context_attach, "_get_glasses_buffer",
                          return_value=None):
            result = attach_vision_context(
                "what do I see",
                context={"voice_mode": True},
                session_id="s",
            )
        assert result == "what do I see"


class TestContentShape:
    """When the user content is already a multimodal list, append
    the image after existing text blocks."""

    def test_appends_to_existing_list(self, vision_on):
        now = time.time()
        buf = _FakeBuffer(_FakeFrame(
            device_id="g", timestamp=now - 1.0, data_b64="A==",
        ))
        existing = [{"type": "text", "text": "describe this"}]
        result = attach_vision_context(
            existing,
            context={"voice_mode": True},
            session_id="s",
            glasses_buffer=buf,
        )
        assert isinstance(result, list)
        assert result[0] == {"type": "text", "text": "describe this"}
        assert result[1]["type"] == "image_url"

    def test_does_not_double_attach(self, vision_on):
        # If the content list ALREADY contains an image_url block
        # (e.g. PerceptionFrame already injected one via the phone
        # vision_ask path), we don't duplicate.
        now = time.time()
        buf = _FakeBuffer(_FakeFrame(
            device_id="g", timestamp=now - 1.0, data_b64="A==",
        ))
        existing = [
            {"type": "text", "text": "x"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,EXISTING=="}},
        ]
        result = attach_vision_context(
            existing,
            context={"voice_mode": True},
            session_id="s",
            glasses_buffer=buf,
        )
        # Unchanged — no second image_url block.
        image_blocks = [b for b in result if b.get("type") == "image_url"]
        assert len(image_blocks) == 1
        assert image_blocks[0]["image_url"]["url"].endswith("EXISTING==")


class TestLegacyVisionBufferFallback:
    """When Lane 11's buffer is empty but the explicit phone
    ``vision_ask`` channel is in use AND the caller supplied a
    legacy ``VisionBuffer``, fall through to the existing fast path.
    """

    def test_legacy_buffer_used_when_glasses_empty(self, vision_on):
        class _LegacyBuf:
            def latest_data_url(self, node_id: str) -> Optional[str]:
                if node_id == "phone-1":
                    return "data:image/jpeg;base64,LEGACY=="
                return None

        result = attach_vision_context(
            "what do I see",
            context={"channel": "vision_ask", "source_node": "phone-1"},
            session_id="s",
            glasses_buffer=_FakeBuffer(frame=None),  # empty
            vision_buffer=_LegacyBuf(),
        )
        assert isinstance(result, list)
        image = next(b for b in result if b.get("type") == "image_url")
        assert image["image_url"]["url"].endswith("LEGACY==")

    def test_legacy_buffer_skipped_outside_vision_ask(self, vision_on):
        class _LegacyBuf:
            def latest_data_url(self, node_id: str) -> Optional[str]:
                return "data:image/jpeg;base64,SHOULD_NOT_APPEAR=="

        # Voice mode but NOT vision_ask channel → legacy buffer is
        # gated out (only the new glasses_buffer is consulted).
        result = attach_vision_context(
            "what do I see",
            context={"voice_mode": True},
            session_id="s",
            glasses_buffer=_FakeBuffer(frame=None),  # empty
            vision_buffer=_LegacyBuf(),
        )
        assert result == "what do I see"
