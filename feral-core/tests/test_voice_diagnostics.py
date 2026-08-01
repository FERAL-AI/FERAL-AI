"""Voice failure diagnosis: what is wrong, and what to do about it.

The gap
=======
The honest-degradation machinery already existed: ``_emit_voice_status``,
``emit_unavailable``, and a ``voice_status state=degraded`` frame. What
none of it carried was an explanation. A user whose voice stopped
working got a red banner reading "Voice unavailable" and a machine tag
like ``openai_realtime_auth``, and had to go and read the source to find
out that their key had expired.

This module builds the missing half: a diagnosis with a real cause and a
concrete next step, assembled from ``security/probe.py``'s existing voice
provider catalogue and probe machinery rather than a parallel one.

Two rules it must not break
===========================
1. Never fabricate. If the cause cannot be determined from evidence, the
   diagnosis says ``unknown`` and says so in words. A plausible-sounding
   guess is worse than "I do not know" because the user acts on it.
2. Never report a silent cloud fallback as success. Someone who chose
   local engines chose them so their voice does not leave the machine.
   Falling back to a cloud vendor is the exact outcome they were
   preventing, and reporting it as "degraded, using fallback" is a lie
   about a privacy property.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from security.probe import ProbeResult
from voice import diagnostics as diag


def _probe(provider: str, *, ok=False, reason="", detail="", status=None) -> ProbeResult:
    return ProbeResult(
        provider=provider,
        ok=ok,
        status_code=status,
        reason=reason,
        detail=detail,
        probed_at=time.time(),
        latency_ms=1.0,
    )


def _prober(table: dict[str, ProbeResult]):
    async def _fn(provider_id, **_kwargs):
        return table.get(
            provider_id,
            _probe(provider_id, reason="unknown_provider", detail="no probe"),
        )
    return _fn


# ── Probe reason -> cause mapping ────────────────────────────────

@pytest.mark.parametrize("reason,detail,status,expected", [
    ("no_key", "credential not configured", None, diag.CAUSE_NO_API_KEY),
    ("unauthorized", "401", 401, diag.CAUSE_KEY_REJECTED),
    ("forbidden", "403", 403, diag.CAUSE_KEY_REJECTED),
    ("timeout", "request timed out", None, diag.CAUSE_PROVIDER_UNREACHABLE),
    ("network_error", "connect failed", None, diag.CAUSE_PROVIDER_UNREACHABLE),
    ("http_error", "rate limited", 429, diag.CAUSE_QUOTA_EXHAUSTED),
    ("http_error", "insufficient_quota", 400, diag.CAUSE_QUOTA_EXHAUSTED),
])
def test_probe_reasons_map_to_causes(reason, detail, status, expected):
    result = _probe("openai_realtime", reason=reason, detail=detail, status=status)
    assert diag.cause_for_probe(result) == expected


def test_unrecognised_probe_reason_is_unknown_not_invented():
    result = _probe("openai_realtime", reason="something_new", detail="???")
    assert diag.cause_for_probe(result) == diag.CAUSE_UNKNOWN


def test_ok_probe_has_no_cause():
    assert diag.cause_for_probe(_probe("openai_realtime", ok=True, reason="ok")) == ""


# ── Local engines are diagnosed on the filesystem, not the network ──

@pytest.mark.parametrize("detail,expected", [
    ("pywhispercpp not installed (pip install 'feral-ai[stt-local]')",
     diag.CAUSE_ENGINE_NOT_INSTALLED),
    ("whisper.cpp model 'base.en' not downloaded",
     diag.CAUSE_MODEL_WEIGHTS_MISSING),
    ("piper cannot synthesise here: espeak data path missing",
     diag.CAUSE_ENGINE_BROKEN),
])
def test_local_engine_details_map_to_causes(detail, expected):
    result = _probe("whispercpp", reason="not_configured", detail=detail)
    assert diag.cause_for_probe(result) == expected


def test_unreadable_local_detail_is_unknown():
    result = _probe("piper", reason="not_configured", detail="something odd")
    assert diag.cause_for_probe(result) == diag.CAUSE_UNKNOWN


# ── Unverified engines must be reported truthfully ───────────────

def test_piper_carries_its_unverified_caveat():
    """Piper synthesis has never been verified in this repo."""
    assert "piper" in diag.ENGINE_CAVEATS
    assert "verif" in diag.ENGINE_CAVEATS["piper"].lower()


def test_whispercpp_and_faster_whisper_carry_caveats():
    for engine in ("whispercpp", "faster_whisper"):
        assert diag.ENGINE_CAVEATS.get(engine)


def test_faster_whisper_caveat_names_the_allow_download_defect():
    """The wizard promises a first-use download that cannot happen.

    ``cli/setup/steps/voice_preflight.py:555`` prints "faster-whisper
    downloads its model on first use", and nothing anywhere passes
    ``allow_download=True`` to ``FasterWhisperSTTProvider``, so a fresh
    install that picks it raises out of the constructor every session.
    """
    caveat = diag.ENGINE_CAVEATS["faster_whisper"]
    assert "allow_download" in caveat


def test_a_ready_probe_still_reports_the_caveat():
    """"Ready" for an unverified engine must not read as "works"."""
    finding = diag.finding_for(_probe("piper", ok=True, reason=""))
    assert finding.ok is True
    assert finding.caveat


def test_findings_for_verified_engines_have_no_caveat():
    assert diag.finding_for(_probe("macos_say", ok=True, reason="")).caveat == ""


# ── Whole-chain diagnosis ────────────────────────────────────────

async def test_first_working_provider_is_reported_as_the_fallback_taken():
    diagnosis = await diag.diagnose_chain(
        ["gemini_live", "openai_realtime"],
        prober=_prober({
            "gemini_live": _probe("gemini_live", reason="no_key"),
            "openai_realtime": _probe("openai_realtime", ok=True, reason="ok"),
        }),
    )
    assert diagnosis.ok is True
    assert diagnosis.fallback_taken == "openai_realtime"
    assert "openai" in diagnosis.summary.lower()


async def test_the_first_entry_working_is_not_a_fallback():
    diagnosis = await diag.diagnose_chain(
        ["openai_realtime", "gemini_live"],
        prober=_prober({
            "openai_realtime": _probe("openai_realtime", ok=True, reason="ok"),
        }),
    )
    assert diagnosis.ok is True
    assert diagnosis.fallback_taken == ""


async def test_everything_down_reports_exhaustion_and_the_first_real_cause():
    diagnosis = await diag.diagnose_chain(
        ["openai_realtime", "gemini_live"],
        prober=_prober({
            "openai_realtime": _probe(
                "openai_realtime", reason="unauthorized", detail="401", status=401,
            ),
            "gemini_live": _probe("gemini_live", reason="no_key"),
        }),
    )
    assert diagnosis.ok is False
    assert diagnosis.cause == diag.CAUSE_ALL_PROVIDERS_EXHAUSTED
    # The recommendation has to be actionable, so it names the specific
    # thing wrong with each link rather than "voice is broken".
    assert "openai_realtime" in diagnosis.recommendation
    assert "gemini_live" in diagnosis.recommendation


async def test_a_single_provider_chain_reports_that_provider_s_cause():
    diagnosis = await diag.diagnose_chain(
        ["openai_realtime"],
        prober=_prober({
            "openai_realtime": _probe("openai_realtime", reason="no_key"),
        }),
    )
    assert diagnosis.cause == diag.CAUSE_NO_API_KEY
    assert "OPENAI_API_KEY" in diagnosis.recommendation or "key" in diagnosis.recommendation


def test_key_advice_names_the_credential_not_the_product():
    """``feral key add --provider openai_realtime`` writes a dead key.

    OpenAI Realtime, OpenAI Whisper and OpenAI TTS are three selectable
    voice providers sharing ONE vault credential, stored under
    ``openai``. Advice that echoes the voice provider id back at the
    operator sends them to create a key nothing will ever read.
    """
    advice = diag.recommendation_for(diag.CAUSE_NO_API_KEY, "openai_realtime")
    assert "--provider openai " in advice or "--provider openai\n" in advice
    assert "--provider openai_realtime" not in advice
    assert "OPENAI_API_KEY" in advice

    gemini = diag.recommendation_for(diag.CAUSE_NO_API_KEY, "gemini_live")
    assert "--provider gemini " in gemini
    assert "--provider gemini_live" not in gemini


def test_key_advice_for_a_provider_with_its_own_credential_is_unchanged():
    advice = diag.recommendation_for(diag.CAUSE_NO_API_KEY, "deepgram")
    assert "--provider deepgram" in advice
    assert "shares the" not in advice


async def test_every_recommendation_is_a_concrete_next_step():
    """No cause may resolve to empty advice."""
    for cause in diag.ALL_CAUSES:
        if cause == diag.CAUSE_OK:
            continue
        assert diag.recommendation_for(cause, "openai_realtime").strip()


async def test_unknown_cause_admits_it_rather_than_guessing():
    diagnosis = await diag.diagnose_chain(
        ["openai_realtime"],
        prober=_prober({
            "openai_realtime": _probe("openai_realtime", reason="who_knows"),
        }),
    )
    assert diagnosis.cause == diag.CAUSE_UNKNOWN
    assert "could not" in diagnosis.summary.lower() or "unknown" in diagnosis.summary.lower()
    # And it must not pretend to know the fix either.
    assert "logs" in diagnosis.recommendation.lower()


async def test_client_fault_outranks_provider_probes():
    """A dead microphone is the cause even when every key is fine.

    Probing OpenAI and reporting "voice provider healthy" while the
    browser is refusing microphone access sends the user to fix the
    wrong thing.
    """
    diagnosis = await diag.diagnose_chain(
        ["openai_realtime"],
        prober=_prober({
            "openai_realtime": _probe("openai_realtime", ok=True, reason="ok"),
        }),
        client_fault={"code": "MIC_PERMISSION_DENIED", "detail": "NotAllowedError"},
    )
    assert diagnosis.ok is False
    assert diagnosis.cause == diag.CAUSE_NO_MIC_PERMISSION
    assert "permission" in diagnosis.recommendation.lower()


async def test_an_unrecognised_client_fault_does_not_become_a_mic_diagnosis():
    diagnosis = await diag.diagnose_chain(
        ["openai_realtime"],
        prober=_prober({
            "openai_realtime": _probe("openai_realtime", ok=True, reason="ok"),
        }),
        client_fault={"code": "SOMETHING_ELSE", "detail": ""},
    )
    assert diagnosis.cause != diag.CAUSE_NO_MIC_PERMISSION


# ── Privacy: a cloud fallback is never silent success ────────────

async def test_local_pick_falling_back_to_cloud_is_flagged():
    diagnosis = await diag.diagnose_chain(
        ["whispercpp", "openai_whisper"],
        prober=_prober({
            "whispercpp": _probe(
                "whispercpp", reason="not_configured",
                detail="whisper.cpp model 'base.en' not downloaded",
            ),
            "openai_whisper": _probe("openai_whisper", ok=True, reason="ok"),
        }),
        chose_local=True,
    )
    assert diagnosis.privacy_downgrade is True
    # Working is not the same as acceptable here.
    assert diagnosis.ok is False
    assert "local" in diagnosis.summary.lower()


async def test_local_pick_served_locally_is_not_flagged():
    diagnosis = await diag.diagnose_chain(
        ["whispercpp"],
        prober=_prober({"whispercpp": _probe("whispercpp", ok=True, reason="")}),
        chose_local=True,
    )
    assert diagnosis.privacy_downgrade is False
    assert diagnosis.ok is True


async def test_cloud_pick_falling_back_to_cloud_is_not_a_privacy_event():
    diagnosis = await diag.diagnose_chain(
        ["gemini_live", "openai_realtime"],
        prober=_prober({
            "gemini_live": _probe("gemini_live", reason="no_key"),
            "openai_realtime": _probe("openai_realtime", ok=True, reason="ok"),
        }),
        chose_local=False,
    )
    assert diagnosis.privacy_downgrade is False
    assert diagnosis.ok is True


# ── Wire shape ───────────────────────────────────────────────────

async def test_diagnosis_serialises_into_a_voice_status_payload():
    from models.protocol import VoiceStatusPayload

    diagnosis = await diag.diagnose_chain(
        ["openai_realtime"],
        prober=_prober({"openai_realtime": _probe("openai_realtime", reason="no_key")}),
    )
    payload = VoiceStatusPayload(**diagnosis.as_status_meta())
    assert payload.state == "unavailable"
    assert payload.cause == diag.CAUSE_NO_API_KEY
    assert payload.recommendation


async def test_a_healthy_diagnosis_serialises_as_available():
    from models.protocol import VoiceStatusPayload

    diagnosis = await diag.diagnose_chain(
        ["openai_realtime"],
        prober=_prober({
            "openai_realtime": _probe("openai_realtime", ok=True, reason="ok"),
        }),
    )
    payload = VoiceStatusPayload(**diagnosis.as_status_meta())
    assert payload.state == "available"


async def test_a_taken_fallback_serialises_as_degraded():
    from models.protocol import VoiceStatusPayload

    diagnosis = await diag.diagnose_chain(
        ["gemini_live", "openai_realtime"],
        prober=_prober({
            "gemini_live": _probe("gemini_live", reason="no_key"),
            "openai_realtime": _probe("openai_realtime", ok=True, reason="ok"),
        }),
    )
    payload = VoiceStatusPayload(**diagnosis.as_status_meta())
    assert payload.state == "degraded"
    assert payload.fallback_provider == "openai_realtime"


# ── Router integration ───────────────────────────────────────────

async def test_router_publishes_a_diagnosis(monkeypatch, tmp_path):
    from voice.router import VoiceRouter

    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    (tmp_path / "settings.json").write_text("{}")
    monkeypatch.delenv("FERAL_VOICE_PROVIDER", raising=False)

    async def _fake_probe(provider_id, **_kwargs):
        return _probe(provider_id, reason="no_key", detail="credential not configured")

    monkeypatch.setattr(diag, "_default_prober", _fake_probe)

    send = AsyncMock()
    r = VoiceRouter(
        realtime_proxy=MagicMock(available=False),
        audio_pipeline=MagicMock(),
        send_to_session=send,
    )
    diagnosis = await r.diagnose_voice("s1", publish=True)
    assert diagnosis.cause in (diag.CAUSE_NO_API_KEY, diag.CAUSE_ALL_PROVIDERS_EXHAUSTED)

    frames = [
        c.args[1].payload for c in send.await_args_list
        if getattr(c.args[1], "type", "") == "voice_status"
    ]
    assert frames, "diagnose_voice(publish=True) must emit voice_status"
    assert frames[-1]["recommendation"]
    assert frames[-1]["cause"] == diagnosis.cause


async def test_router_reports_a_client_fault_it_was_told_about(monkeypatch, tmp_path):
    from voice.router import VoiceRouter

    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    (tmp_path / "settings.json").write_text("{}")
    monkeypatch.delenv("FERAL_VOICE_PROVIDER", raising=False)

    async def _fake_probe(provider_id, **_kwargs):
        return _probe(provider_id, ok=True, reason="ok")

    monkeypatch.setattr(diag, "_default_prober", _fake_probe)

    r = VoiceRouter(
        realtime_proxy=MagicMock(available=True),
        audio_pipeline=MagicMock(),
        send_to_session=AsyncMock(),
    )
    r.report_client_fault("s1", "MIC_PERMISSION_DENIED", "NotAllowedError")
    diagnosis = await r.diagnose_voice("s1")
    assert diagnosis.cause == diag.CAUSE_NO_MIC_PERMISSION


async def test_router_reports_mute_before_hunting_for_faults(monkeypatch, tmp_path):
    """"Voice is not working" while muted has a one-word answer."""
    from voice.router import VoiceRouter

    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    (tmp_path / "settings.json").write_text("{}")

    r = VoiceRouter(
        realtime_proxy=MagicMock(available=True),
        audio_pipeline=MagicMock(),
        send_to_session=AsyncMock(),
    )
    await r.set_session_muted("s1", True)
    diagnosis = await r.diagnose_voice("s1")
    assert diagnosis.cause == diag.CAUSE_MUTED
    assert "unmute" in diagnosis.recommendation.lower()


async def test_local_operator_is_not_degraded_onto_a_cloud_tts(monkeypatch, tmp_path):
    """A local-voice operator must never be quietly moved to OpenAI.

    ``_pick_fallback_provider`` returns ``"whisper"`` -- the OpenAI
    ``/audio/speech`` endpoint -- whenever an OpenAI key exists. For an
    operator whose chained pair is local engines, taking that fallback
    ships their audio to a vendor and shows a green-ish "degraded, using
    fallback TTS" banner over it.
    """
    from voice.router import VoiceRouter

    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    (tmp_path / "settings.json").write_text(
        '{"voice": {"chained": {"stt_provider": "whispercpp",'
        ' "tts_provider": "macos_say"}}}'
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")

    send = AsyncMock()
    r = VoiceRouter(
        realtime_proxy=MagicMock(available=True),
        audio_pipeline=MagicMock(),
        send_to_session=send,
    )
    assert r._operator_chose_local_voice() is True
    await r.handle_realtime_failure(
        "s1", reason="openai_realtime_quota", detail="1013",
    )
    frames = [
        c.args[1].payload for c in send.await_args_list
        if getattr(c.args[1], "type", "") == "voice_status"
    ]
    assert frames
    assert frames[-1]["state"] == "unavailable"
    assert frames[-1]["fallback_provider"] != "whisper"


async def test_cloud_operator_still_gets_the_whisper_fallback(monkeypatch, tmp_path):
    """The privacy guard must not break the ordinary degrade path."""
    from voice.router import VoiceRouter

    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    (tmp_path / "settings.json").write_text(
        '{"voice": {"chained": {"stt_provider": "deepgram",'
        ' "tts_provider": "elevenlabs"}}}'
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")

    send = AsyncMock()
    r = VoiceRouter(
        realtime_proxy=MagicMock(available=True),
        audio_pipeline=MagicMock(),
        send_to_session=send,
    )
    assert r._operator_chose_local_voice() is False
    await r.handle_realtime_failure("s1", reason="network_down", detail="")
    frames = [
        c.args[1].payload for c in send.await_args_list
        if getattr(c.args[1], "type", "") == "voice_status"
    ]
    assert frames[-1]["state"] == "degraded"
    assert frames[-1]["fallback_provider"] == "whisper"


# ── The catalogue stays the single source of truth ───────────────

def test_caveats_only_name_providers_the_catalogue_knows():
    from security.probe import VOICE_PROVIDER_CATALOGUE

    known = {pid for pid, _kind, _name in VOICE_PROVIDER_CATALOGUE}
    assert set(diag.ENGINE_CAVEATS) <= known


def test_catalogue_exposes_caveats_to_callers():
    from security.probe import voice_provider_catalogue

    entries = {e["id"]: e for e in voice_provider_catalogue()}
    assert entries["piper"]["caveat"]
    assert entries["macos_say"]["caveat"] == ""
