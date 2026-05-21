"""REST routes for STT / TTS provider + config discovery.

The CLI setup wizard and v2 /setup page render the same provider list
from ``GET /api/audio/providers`` so a user's choice between local
(faster-whisper / piper) and cloud (OpenAI) never drifts between the
two surfaces.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from api.state import state
from perception.audio_pipeline import detect_local_audio_capabilities

logger = logging.getLogger("feral.api.audio")

router = APIRouter()


# ----------------------------------------------------------------------
# Provider catalog for STT + TTS
# ----------------------------------------------------------------------


_STT_PROVIDERS = (
    {
        "id": "openai",
        "display_name": "OpenAI Whisper (cloud)",
        "needs_api_key": True,
        "credential_env_var": "OPENAI_API_KEY",
        "is_local": False,
        "aliases": ("openai", "whisper", "whisper-cloud"),
        "default_model": "whisper-1",
        "available_models": ["whisper-1"],
    },
    {
        "id": "faster-whisper",
        "display_name": "faster-whisper (local)",
        "needs_api_key": False,
        "credential_env_var": "",
        "is_local": True,
        "aliases": ("local", "whisper-local", "local-whisper"),
        "default_model": "base",
        "available_models": ["tiny", "base", "small", "medium", "large"],
    },
)


_TTS_PROVIDERS = (
    {
        "id": "openai",
        "display_name": "OpenAI TTS (cloud)",
        "needs_api_key": True,
        "credential_env_var": "OPENAI_API_KEY",
        "is_local": False,
        "aliases": ("openai", "openai-tts"),
        "default_model": "tts-1",
        "default_voice": "nova",
        "available_models": ["tts-1", "tts-1-hd"],
        "available_voices": ["alloy", "echo", "fable", "onyx", "nova", "shimmer"],
    },
    {
        "id": "piper",
        "display_name": "Piper (local)",
        "needs_api_key": False,
        "credential_env_var": "",
        "is_local": True,
        "aliases": ("local", "piper-local"),
        "default_model": "piper",
        "default_voice": "en_US-lessac-medium",
        "available_models": ["piper"],
        "available_voices": [
            "en_US-lessac-medium",
            "en_US-amy-low",
            "en_GB-alan-medium",
        ],
    },
)


def _enrich_with_local_capabilities() -> tuple[list[dict], list[dict]]:
    caps = detect_local_audio_capabilities()
    stt: list[dict] = []
    for entry in _STT_PROVIDERS:
        row = dict(entry)
        if row["is_local"]:
            row["available"] = caps.get("local_stt", False)
            row["available_models"] = list(caps.get("stt_models") or row["available_models"])
        else:
            row["available"] = True
        stt.append(row)

    tts: list[dict] = []
    for entry in _TTS_PROVIDERS:
        row = dict(entry)
        if row["is_local"]:
            row["available"] = caps.get("local_tts", False)
            row["available_voices"] = list(caps.get("tts_voices") or row["available_voices"])
        else:
            row["available"] = True
        tts.append(row)
    return stt, tts


@router.get("/api/audio/providers")
async def list_audio_providers():
    stt, tts = _enrich_with_local_capabilities()
    return {"stt": stt, "tts": tts}


@router.get("/api/audio/providers/{kind}/{provider_id}/models")
async def list_audio_models(kind: str, provider_id: str):
    if kind not in ("stt", "tts"):
        raise HTTPException(status_code=400, detail="kind must be 'stt' or 'tts'")
    stt, tts = _enrich_with_local_capabilities()
    rows = stt if kind == "stt" else tts
    for row in rows:
        if row["id"] == provider_id:
            return {"provider_id": provider_id, "models": list(row.get("available_models", []))}
    raise HTTPException(status_code=404, detail=f"unknown {kind} provider {provider_id!r}")


@router.get("/api/audio/providers/{provider_id}/voices")
async def list_audio_voices(provider_id: str):
    _, tts = _enrich_with_local_capabilities()
    for row in tts:
        if row["id"] == provider_id:
            return {"provider_id": provider_id, "voices": list(row.get("available_voices", []))}
    raise HTTPException(status_code=404, detail=f"unknown tts provider {provider_id!r}")


# ----------------------------------------------------------------------
# Active audio config (settings.json-backed)
# ----------------------------------------------------------------------


@router.get("/api/audio/config")
async def get_audio_config():
    if state.config is None:
        raise HTTPException(status_code=503, detail="ConfigLoader not initialised")
    return {
        "stt_provider": state.config.get("audio", "stt_provider", "openai"),
        "stt_model": state.config.get("audio", "stt_model", "whisper-1"),
        "tts_provider": state.config.get("audio", "tts_provider", "openai"),
        "tts_model": state.config.get("audio", "tts_model", "tts-1"),
        "tts_voice": state.config.get("audio", "tts_voice", "nova"),
    }


class AudioConfigRequest(BaseModel):
    stt_provider: Optional[str] = None
    stt_model: Optional[str] = None
    tts_provider: Optional[str] = None
    tts_model: Optional[str] = None
    tts_voice: Optional[str] = None


def _audio_ids(rows) -> set[str]:
    return {row["id"] for row in rows}


@router.post("/api/audio/config")
async def set_audio_config(req: AudioConfigRequest):
    if state.config is None:
        raise HTTPException(status_code=503, detail="ConfigLoader not initialised")
    stt, tts = _enrich_with_local_capabilities()
    stt_ids = _audio_ids(stt)
    tts_ids = _audio_ids(tts)

    for key, value in (
        ("stt_provider", req.stt_provider),
        ("stt_model", req.stt_model),
        ("tts_provider", req.tts_provider),
        ("tts_model", req.tts_model),
        ("tts_voice", req.tts_voice),
    ):
        if value is None:
            continue
        if key == "stt_provider" and value not in stt_ids:
            raise HTTPException(status_code=400, detail=f"unknown stt_provider {value!r}")
        if key == "tts_provider" and value not in tts_ids:
            raise HTTPException(status_code=400, detail=f"unknown tts_provider {value!r}")
        state.config.update_settings("audio", key, value)

    return await get_audio_config()


# ----------------------------------------------------------------------
# Lane 05 W8: voice provider sovereignty (THESIS_SCENARIOS S4)
# ----------------------------------------------------------------------
#
# /api/voice/providers — flat list of every realtime / STT / TTS
# provider the brain knows about, with `configured` (probe-derived)
# and `probe_status` so the iPhone Settings → Voice picker (Wave 3
# Lane 11) and the WebUI Settings → Voice panel (Wave 3 Lane 12) can
# render an honest "key valid?" badge per row.
#
# /api/voice/providers/probe — explicit POST that re-runs probes
# bypassing the 60s probe cache. Used by the "Test connection"
# button in Settings.


@router.get("/api/voice/providers")
async def list_voice_providers():
    """Return the structured voice provider catalogue.

    Each entry: ``{id, name, kind: 'realtime'|'stt'|'tts',
    configured: bool, probe_status: 'ok'|'unauthorized'|'no_key'|...,
    latency_ms?: float}``.

    The list is populated from
    ``security.probe.voice_provider_catalogue()`` (Lane 05 W7); the
    probe status is read from the cached probe registry — call
    ``POST /api/voice/providers/probe`` to force a refresh.
    """
    from security.probe import probe, voice_provider_catalogue

    rows: list[dict] = []
    for entry in voice_provider_catalogue():
        try:
            result = await probe(entry["id"])
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("voice provider probe %s raised: %s", entry["id"], exc)
            rows.append({
                **entry,
                "configured": False,
                "probe_status": "probe_error",
                "probe_detail": str(exc),
            })
            continue
        rows.append({
            **entry,
            "configured": result.ok,
            "probe_status": result.reason,
            "probe_detail": result.detail,
            "latency_ms": result.latency_ms,
        })
    return {"providers": rows}


class VoiceProbeRequest(BaseModel):
    provider_id: Optional[str] = Field(
        default=None,
        description=(
            "Specific provider to probe. Omit to refresh every voice "
            "provider in the catalogue."
        ),
    )


@router.post("/api/voice/providers/probe")
async def probe_voice_providers(req: VoiceProbeRequest):
    """Force-refresh one (or all) voice provider probes."""
    from security.probe import (
        probe,
        voice_provider_catalogue,
        VOICE_PROVIDER_CATALOGUE,
    )

    catalogue_ids = {pid for pid, _, _ in VOICE_PROVIDER_CATALOGUE}

    if req.provider_id:
        if req.provider_id not in catalogue_ids:
            raise HTTPException(
                status_code=404,
                detail=f"unknown voice provider {req.provider_id!r}; "
                f"valid: {sorted(catalogue_ids)}",
            )
        result = await probe(req.provider_id, force=True)
        return {
            "provider_id": req.provider_id,
            "ok": result.ok,
            "reason": result.reason,
            "status_code": result.status_code,
            "latency_ms": result.latency_ms,
            "detail": result.detail,
        }

    rows: list[dict] = []
    for entry in voice_provider_catalogue():
        result = await probe(entry["id"], force=True)
        rows.append({
            **entry,
            "ok": result.ok,
            "reason": result.reason,
            "status_code": result.status_code,
            "latency_ms": result.latency_ms,
        })
    return {"providers": rows}
