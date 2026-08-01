"""Mute for the voice system, end to end.

The defect
==========
``feral-client-v2/src/pages/phone/VoiceFullscreen.jsx`` has shipped a
mute button since the voice UX landed. Tapping it flipped a local
``isMuted`` boolean, dimmed the orb, and sent a ``voice_mute`` envelope.

Nothing in ``feral-core`` has ever handled ``voice_mute``, and nothing
on the client stopped the microphone: ``BrowserNode`` kept its
``AudioWorklet`` running and kept posting ``audio_chunk`` frames the
whole time. Mute was a picture of a mute button. Every syllable spoken
"muted" still reached the brain and still reached whichever cloud
realtime provider was live.

What mute means here
====================
Mute is an INPUT control, and it is enforced in two places on purpose:

* At the client, capture stops (``BrowserNode.setMicMuted`` disables the
  MediaStreamTrack). That is the only place that can promise the audio
  never leaves the device, which is the promise a mute button makes.
* At the brain, ingress is dropped (:meth:`VoiceRouter.is_session_muted`
  gates both audio entry points). Client-side enforcement alone is one
  bug away from failing open, and a session can have more than one
  surface bound to it.

Mute does NOT stop synthesis coming back. Silencing the assistant is a
different control (barge-in / ``voice_interrupt`` already exists for
cutting a reply short), and a user who mutes mid-answer to stop a
background conversation being transcribed still wants to hear the
answer they asked for.

Mute survives a reconnect. The ledger is session-scoped and is NOT
cleared by a node disconnect: a voice session that came back unmuted
after a dropped LTE connection is exactly the failure the control
exists to prevent. It fails safe toward muted, and only an explicit
unmute clears it.

The state is observable: every ``voice_status`` frame carries ``muted``,
so a UI cannot render "listening" while the brain considers the session
muted.
"""
from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock

import pytest

from voice.router import VoiceRouter

AUDIO = base64.b64encode(b"\x00" * 320).decode()


def _sent_voice_status(send_mock) -> list[dict]:
    frames = []
    for call in send_mock.await_args_list:
        msg = call.args[1]
        if getattr(msg, "type", "") == "voice_status":
            frames.append(msg.payload)
    return frames


@pytest.fixture()
def router():
    rt = MagicMock(available=True)
    rt.get_session = MagicMock(return_value=None)
    rt.start_session = AsyncMock()
    rt.stop_session = AsyncMock()
    rt.evict_dead_session = AsyncMock(return_value=False)
    rt._node_to_session = {}
    r = VoiceRouter(
        realtime_proxy=rt,
        audio_pipeline=MagicMock(process_audio_chunk=AsyncMock(return_value=None)),
        send_to_session=AsyncMock(),
    )
    return r


# ── State ────────────────────────────────────────────────────────

def test_sessions_start_unmuted(router):
    assert router.is_session_muted("s1") is False


async def test_set_session_muted_records_state(router):
    await router.set_session_muted("s1", True)
    assert router.is_session_muted("s1") is True
    await router.set_session_muted("s1", False)
    assert router.is_session_muted("s1") is False


async def test_mute_detail_names_who_asked(router):
    await router.set_session_muted("s1", True, source="client")
    detail = router.session_mute_detail("s1")
    assert detail["muted"] is True
    assert detail["source"] == "client"


# ── Observability: the UI cannot show "listening" while muted ────

async def test_mute_emits_voice_status(router):
    await router.set_session_muted("s1", True)
    frames = _sent_voice_status(router._send_to_session)
    assert frames, "muting must publish a voice_status frame"
    assert frames[-1]["muted"] is True


async def test_unmute_emits_voice_status(router):
    await router.set_session_muted("s1", True)
    await router.set_session_muted("s1", False)
    frames = _sent_voice_status(router._send_to_session)
    assert frames[-1]["muted"] is False


async def test_every_voice_status_frame_carries_the_mute_flag(router):
    """A degraded banner emitted while muted must still say muted.

    Otherwise the client reconciles ``muted`` from the newest frame and
    silently flips the mic indicator back to "listening".
    """
    await router.set_session_muted("s1", True)
    await router.emit_unavailable("s1", reason="openai_realtime_auth", detail="401")
    frames = _sent_voice_status(router._send_to_session)
    assert frames[-1]["state"] == "unavailable"
    assert frames[-1]["muted"] is True


async def test_redundant_mute_does_not_re_emit(router):
    await router.set_session_muted("s1", True)
    before = len(_sent_voice_status(router._send_to_session))
    changed = await router.set_session_muted("s1", True)
    assert changed is False
    assert len(_sent_voice_status(router._send_to_session)) == before


# ── Enforcement: audio actually stops ────────────────────────────

async def test_muted_session_drops_client_audio(router):
    router.set_session_voice_mode("s1", "whisper")
    await router.set_session_muted("s1", True)
    await router.handle_audio_from_client("s1", AUDIO)
    router._audio.process_audio_chunk.assert_not_awaited()


async def test_unmuted_session_still_forwards_client_audio(router):
    router.set_session_voice_mode("s1", "whisper")
    await router.set_session_muted("s1", True)
    await router.set_session_muted("s1", False)
    await router.handle_audio_from_client("s1", AUDIO)
    router._audio.process_audio_chunk.assert_awaited()


async def test_muted_session_drops_node_audio(router):
    router.register_voice_config("n1", {"mode": "whisper"})
    router.bind_node_to_session("n1", "s1")
    await router.set_session_muted("s1", True)
    await router.handle_audio_from_node("n1", "s1", AUDIO)
    router._audio.process_audio_chunk.assert_not_awaited()


async def test_muted_session_does_not_reach_a_realtime_provider(router):
    """The privacy-relevant case: nothing goes upstream to a vendor."""
    session = MagicMock(connected=True, send_audio=AsyncMock())
    router._realtime.get_session.return_value = session
    router.register_voice_config("n1", {"mode": "openai_realtime"})
    router.bind_node_to_session("n1", "s1")
    await router.set_session_muted("s1", True)
    await router.handle_audio_from_node("n1", "s1", AUDIO)
    session.send_audio.assert_not_awaited()


async def test_node_declared_mute_is_honoured(router):
    """``voice_config {"muted": true}`` gates ingress too.

    The phone/daemon surface can already reach
    ``register_voice_config`` over the wire, so this path mutes ingress
    without waiting for a new ``voice_mute`` handler in api/server.py.
    """
    router.register_voice_config("n1", {"mode": "whisper", "muted": True})
    await router.handle_audio_from_node("n1", "s1", AUDIO)
    router._audio.process_audio_chunk.assert_not_awaited()


# ── Reconnect ────────────────────────────────────────────────────

async def test_mute_survives_a_node_disconnect(router):
    """A dropped socket must not silently unmute the microphone."""
    router.register_voice_config("n1", {"mode": "whisper"})
    router.bind_node_to_session("n1", "s1")
    await router.set_session_muted("s1", True)
    await router.stop_node_voice("n1")
    assert router.is_session_muted("s1") is True


async def test_reopening_a_session_republishes_the_mute_state(router):
    """The reconnecting UI learns it is still muted without asking."""
    await router.set_session_muted("s1", True)
    router._send_to_session.reset_mock()
    await router.open_session("s1", "openai_realtime", {"node_id": "n1"})
    frames = _sent_voice_status(router._send_to_session)
    assert frames and frames[-1]["muted"] is True


async def test_explicit_stop_clears_the_mute_ledger(router):
    """Ending voice on purpose is the one thing that resets mute."""
    await router.set_session_muted("s1", True)
    await router.stop_session_voice("s1")
    assert router.is_session_muted("s1") is False


# ── Mute is input-only ───────────────────────────────────────────

async def test_mute_does_not_block_assistant_speech(router):
    """Muting the mic must not also mute the answer already asked for."""
    router._audio.synthesize_speech = AsyncMock(return_value=[{"data_b64": "AA"}])
    await router.set_session_muted("s1", True)
    delivered = await router.synthesize_assistant_speech("s1", "here you go")
    assert delivered is True
