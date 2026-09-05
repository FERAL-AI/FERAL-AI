from __future__ import annotations

import json
import logging

from models.protocol import ErrorPayload, FeralMessage, SDUIPayload, TextResponsePayload
from agents.chat_sanitizer import sanitize_assistant_display_text

logger = logging.getLogger("feral.orchestrator")


LLM_PROVIDER_ERROR_CODE = "llm_provider_error"


async def send_error(
    orchestrator,
    session_id: str,
    message: str,
    *,
    code: str = LLM_PROVIDER_ERROR_CODE,
    recoverable: bool = True,
):
    """Deliver a failure as an ``error`` frame, not as assistant prose.

    Provider failures used to travel through ``send_text`` as a
    ``text_response``, so the web client drew "HTTP 400 ..." as an
    assistant bubble, the orchestrator recorded it in
    ``conversation_history`` and the next turn fed it back to the model
    as its own earlier reply. ``feral-client-v2`` (Chat.jsx) already
    renders ``type="error"`` frames as an inline notice keyed on
    ``payload.message`` / ``payload.code`` / ``payload.recoverable``,
    which is exactly ``ErrorPayload``.

    Deliberately does NOT go through ``orchestrator._send_text``: that
    helper calls ``_note_outbound_text``, which is how prose gets
    committed to the transcript by ``_finalize_turn``.
    """
    try:
        await orchestrator.send(
            session_id,
            FeralMessage(
                session_id=session_id,
                hop="brain",
                type="error",
                payload=ErrorPayload(
                    code=code,
                    message=str(message or "The brain reported an error."),
                    recoverable=recoverable,
                ).model_dump(),
            ),
        )
    except Exception:
        logger.exception("error frame delivery failed for %s", session_id[:8])


async def send_text(
    orchestrator,
    session_id: str,
    text: str,
    *,
    model: str = "",
    usage: dict | None = None,
):
    # Defense in depth: even non-streaming callers that hand us raw
    # model text benefit from the same artifact scrubber the stream
    # path uses.
    clean = sanitize_assistant_display_text(text) if text else text

    # Audit-r11 fix — Bug 1: double assistant bubble on iOS. The phone
    # `/v1/node chat_request` handler sets
    # ``_text_response_suppressed[sid] = True`` for the duration of
    # the turn so the orchestrator's broadcast ``text_response`` no
    # longer reaches the phone *in addition to* the synchronous
    # ``chat_response`` reply (see ``api/server.py`` chat_request
    # branch). When desktop owns the session WS the flag stays False,
    # so desktop still gets ``text_response`` as before.
    suppressed = bool(
        getattr(orchestrator, "_text_response_suppressed", {}).get(session_id, False)
    )
    if not suppressed:
        await orchestrator.send(
            session_id,
            FeralMessage(
                session_id=session_id,
                hop="brain",
                type="text_response",
                payload=TextResponsePayload(
                    text=clean,
                    model=model or "",
                    usage=usage or {},
                ).model_dump(),
            ),
        )

    # Audit-r11 fix — Bug 3 (voice fallback): when the realtime
    # provider died mid-session (OpenAI 1013 insufficient_quota etc),
    # the router marked the session degraded. Drive the whisper TTS
    # fallback HERE — every assistant turn after the failure event
    # synthesises mp3 ``tts_chunk`` frames so iOS / WebUI keep hearing
    # audio. The guard is best-effort; failures emit a final
    # ``voice_status state=unavailable`` and proceed.
    router = getattr(orchestrator, "voice_router", None)
    if (
        router is not None
        and clean
        and getattr(router, "is_session_degraded", None)
        and router.is_session_degraded(session_id)
    ):
        # v2026.5.43 — after S4 morph (router.handle_realtime_failure
        # promoted the session onto the chained STT→LLM→TTS pipeline),
        # the chained pipeline's own ``send_text`` path emits
        # ``tts_chunk`` frames. Running the whisper synthesiser too
        # would play the assistant turn twice. Skip the whisper fan-out
        # whenever the session is already on the chained path.
        voice_mode = getattr(router, "_session_voice_mode", {}) or {}
        if voice_mode.get(session_id) == "chained":
            return
        try:
            await router.synthesize_assistant_speech(session_id, clean)
        except Exception:
            logger.exception("Whisper fallback synth failed for %s", session_id[:8])


async def try_send_sdui(
    orchestrator,
    session_id: str,
    text: str,
    *,
    model: str = "",
    usage: dict | None = None,
):
    """Try to parse text as SDUI JSON, fall back to plain text.

    Attribution rides along to the plain-text fallback only. An SDUI card
    is a rendered widget with nowhere to put a token count, and the
    multi-agent path (the main caller) reaches here for every answer.
    """
    try:
        cleaned = text.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:-3].strip()
        elif cleaned.startswith("```\n"):
            cleaned = cleaned[4:-3].strip()
        elif cleaned.startswith("```"):
            cleaned = cleaned[3:-3].strip()
        sdui = json.loads(cleaned)
        if "type" in sdui:
            await orchestrator.send(
                session_id,
                FeralMessage(
                    session_id=session_id,
                    hop="brain",
                    type="sdui",
                    payload=SDUIPayload(root=sdui).model_dump(),
                ),
            )
            return
    except json.JSONDecodeError:
        pass
    await send_text(orchestrator, session_id, text, model=model, usage=usage)


async def try_genui_for_result(orchestrator, session_id: str, tool_call: dict, result_data: dict):
    """Generate and send SDUI for tool results when the data is rich enough."""
    if not isinstance(result_data, dict):
        return

    display_data = result_data.get("data") if isinstance(result_data.get("data"), dict) else result_data
    envelope_keys = {
        "success",
        "status_code",
        "error",
        "ok",
        "status",
        "created_at",
        "_anti_loop_guidance",
        "_anti_loop_streak",
    }
    display_data = {k: v for k, v in display_data.items() if k not in envelope_keys} if isinstance(display_data, dict) else display_data
    if not isinstance(display_data, dict) or not display_data:
        return

    parts = tool_call["name"].split("__", 1)
    skill_id = parts[0] if len(parts) == 2 else tool_call["name"]
    endpoint_id = parts[1] if len(parts) == 2 else ""
    skill = orchestrator.skills.skills.get(skill_id)
    if not skill:
        return

    endpoint = next((ep for ep in skill.endpoints if ep.id == endpoint_id), None)
    ui_hint = endpoint.ui_hint if endpoint else None

    try:
        engine = getattr(orchestrator, "_genui_engine", None)
        if engine is not None:
            sdui = await engine.generate_for_data(
                data=display_data,
                skill_brand=skill.brand.model_dump(),
                ui_hint=ui_hint,
                endpoint_id=endpoint_id,
            )
        else:
            sdui = orchestrator.genui.generate(
                data=display_data,
                skill_brand=skill.brand.model_dump(),
                ui_hint=ui_hint,
                endpoint_id=endpoint_id,
            )
        if sdui and "type" in sdui:
            await orchestrator.send(
                session_id,
                FeralMessage(
                    session_id=session_id,
                    hop="brain",
                    type="sdui",
                    payload=SDUIPayload(root=sdui).model_dump(),
                ),
            )
    except Exception as e:
        logger.debug(f"GenUI generation for {tool_call['name']} skipped: {e}")
