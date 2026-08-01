"""Speech in / out step — the missing piece from the old wizard.

Renders two side-by-side tables (STT + TTS) with their ready-state,
lets the user pick a provider + model + voice, writes the choice
into settings.json.audio so AudioPipeline actually honours it at
runtime.

No more hand-editing ``settings.json`` to go fully-local.
"""

from __future__ import annotations

from perception.audio_pipeline import detect_local_audio_capabilities

from cli import ui_kit

from ..helpers import (
    STATUS_NEEDS_KEY,
    STATUS_READY,
    STATUS_UNAVAILABLE,
    Option,
    ask_choice,
    ask_text,
    confirm,
    get_console,
    render_provider_table,
    _RICH_AVAILABLE,
)
from ..state import WizardState


_STT_PROVIDERS = (
    {
        "id": "openai",
        "label": "OpenAI Whisper (cloud)",
        "needs_key": True,
        "env": "OPENAI_API_KEY",
        "is_local": False,
        "aliases": ("openai", "whisper", "whisper-cloud"),
        "default_model": "whisper-1",
        "available_models": ["whisper-1"],
    },
    {
        "id": "faster-whisper",
        "label": "faster-whisper (local)",
        "needs_key": False,
        "env": "",
        "is_local": True,
        "aliases": ("local", "whisper-local", "local-whisper", "faster_whisper"),
        "default_model": "base",
        "available_models": ["tiny", "base", "small", "medium", "large"],
    },
)


_TTS_PROVIDERS = (
    {
        "id": "openai",
        "label": "OpenAI TTS (cloud)",
        "needs_key": True,
        "env": "OPENAI_API_KEY",
        "is_local": False,
        "aliases": ("openai", "openai-tts"),
        "default_model": "tts-1",
        "default_voice": "nova",
        "available_models": ["tts-1", "tts-1-hd"],
        "available_voices": ["alloy", "echo", "fable", "onyx", "nova", "shimmer"],
    },
    {
        "id": "piper",
        "label": "Piper (local)",
        "needs_key": False,
        "env": "",
        "is_local": True,
        "aliases": ("local", "piper-local"),
        "default_model": "piper",
        "default_voice": "en_US-lessac-medium",
        "available_models": ["piper"],
        "available_voices": [
            "en_US-lessac-medium", "en_US-amy-low", "en_GB-alan-medium",
        ],
    },
)


async def run(state: WizardState) -> None:
    console = get_console()
    caps = detect_local_audio_capabilities()
    has_local_stt = bool(caps.get("local_stt"))
    has_local_tts = bool(caps.get("local_tts"))
    has_openai_key = bool(
        state.credentials.get("OPENAI_API_KEY")
        or state.has_credential("OPENAI_API_KEY")
    )

    # The state machine owns the "Step N of M" header; a hardcoded
    # number here contradicted it the moment the step list changed.
    if _RICH_AVAILABLE:
        console.print(
            "Pick how FERAL should listen + speak."
        )

    if not _wants_voice(state, console):
        # NOTE: no ``audio.*`` writes on the skip path. This used to
        # materialise ``stt_provider``/``tts_provider`` as "openai" even
        # when the operator declined voice, so an install with no OpenAI
        # key ended up with a settings file explicitly asserting the
        # OpenAI speech providers and the finish summary printing
        # "STT: openai · ?". The values match DEFAULT_SETTINGS anyway,
        # so writing nothing keeps "operator skipped" distinguishable
        # from "operator chose OpenAI".
        return

    if confirm("  Prefer fully-local voice? (no cloud, no keys)", default=has_local_stt and has_local_tts):
        _configure_local(state, has_local_stt, has_local_tts, console)
        return

    _configure_provider(state, "stt", _STT_PROVIDERS, has_local_stt, has_openai_key, caps, console)
    _configure_provider(state, "tts", _TTS_PROVIDERS, has_local_tts, has_openai_key, caps, console)
    _configure_fallback_tts(state, console, has_openai_key, has_local_tts)


def _wants_voice(state: WizardState, console) -> bool:
    """Whether to configure the speech pipeline, without re-asking.

    The preceding ``voice_preflight`` step opens with "Set up voice
    now?" and records the answer at ``audio.configured_via_wizard``.
    This step used to ask a verbatim-identical "Configure voice now?"
    one screen later with the OPPOSITE default, so an operator pressing
    enter through the wizard answered yes to voice on one step and no
    on the next. The two steps write disjoint key sets and nothing
    reconciles them, so the result was a half-configured voice stack
    (e.g. ``realtime_primary=gemini_live`` from the first step next to
    ``stt_provider=openai`` from the second's skip default).

    Now the answer is carried forward. The prompt only reappears when
    ``voice_preflight`` never got to ask, which happens when the voice
    catalogue could not be imported or came back empty. That prompt
    defaults to yes, matching ``voice_preflight``, so enter-through
    behaves the same on both paths.
    """
    answered = state.get_setting("audio", "configured_via_wizard", None)
    if answered is False:
        console.print(
            "  [dim]Voice was skipped on the previous step.[/]"
            if _RICH_AVAILABLE else
            "  Voice was skipped on the previous step."
        )
        return False
    if answered is True:
        return True
    return confirm("  Set up speech in / out now?", default=True)


def _configure_fallback_tts(
    state: WizardState,
    console,
    has_openai_key: bool,
    has_local_tts: bool,
) -> None:
    """Audit-r11: prompt for the fallback TTS provider chain.

    When the primary realtime provider (OpenAI Realtime) closes a
    session with insufficient_quota / 429 / invalid_api_key, the
    voice router walks this list and synthesises chunked TTS so the
    assistant keeps speaking. Empty list -> ``voice_status
    state=unavailable`` and clients render a banner instead of
    silent failure.
    """
    if _RICH_AVAILABLE:
        console.print()
        console.print("[bold]Fallback voice (when realtime fails)[/]")
        console.print(
            "If OpenAI Realtime runs out of credit or hits a rate limit, "
            "FERAL can keep speaking via a chunked TTS fallback so iOS / "
            "web don't go silent."
        )

    options: list[Option] = []
    if has_openai_key:
        options.append(Option(
            id="whisper", label="OpenAI /audio/speech (cheap mp3)",
            aliases=("whisper", "openai", "openai-tts"),
            status=STATUS_READY, hint="reuses OPENAI_API_KEY",
        ))
    else:
        options.append(Option(
            id="whisper", label="OpenAI /audio/speech (cheap mp3)",
            aliases=("whisper", "openai", "openai-tts"),
            status=STATUS_NEEDS_KEY, hint="OPENAI_API_KEY",
        ))
    if has_local_tts:
        options.append(Option(
            id="piper", label="Piper (local, no quota)",
            aliases=("piper", "local"), status=STATUS_READY,
            hint="ready",
        ))
    options.append(Option(
        id="none", label="None — go silent (still shows banner)",
        aliases=("none", "off"), status=STATUS_READY, hint="last resort",
    ))

    title = "Fallback TTS provider"
    render_provider_table(title, options)
    default_list = state.get_setting("audio", "fallback_tts_providers", ["whisper"]) or ["whisper"]
    default = default_list[0] if default_list else "whisper"
    chosen = ask_choice(f"Choose {title}", options, default=default)
    if chosen.id == "none":
        state.set_setting("audio", "fallback_tts_providers", [])
    else:
        state.set_setting("audio", "fallback_tts_providers", [chosen.id])


def _configure_local(state: WizardState, has_stt: bool, has_tts: bool, console) -> None:
    if has_stt:
        state.set_setting("audio", "stt_provider", "faster-whisper")
        state.set_setting("audio", "stt_model", "base")
    else:
        state.set_setting("audio", "stt_provider", "faster-whisper")
        state.set_setting("audio", "stt_model", "base")
        console.print(
            "  [yellow]faster-whisper isn't installed.[/] Run "
            "`pip install feral-ai[stt]` and voice input will auto-enable."
            if _RICH_AVAILABLE else
            "  faster-whisper isn't installed. Run: pip install feral-ai[stt]"
        )
    if has_tts:
        state.set_setting("audio", "tts_provider", "piper")
        state.set_setting("audio", "tts_model", "piper")
        state.set_setting("audio", "tts_voice", "en_US-lessac-medium")
    else:
        state.set_setting("audio", "tts_provider", "piper")
        # Written on both arms so the finish summary does not print
        # "TTS: piper · ?" for a value the step already knows. The
        # default from DEFAULT_SETTINGS is the OpenAI "tts-1", which is
        # wrong for a local Piper pipeline.
        state.set_setting("audio", "tts_model", "piper")
        state.set_setting("audio", "tts_voice", "en_US-lessac-medium")
        console.print(
            "  [yellow]piper isn't installed.[/] Run `pip install feral-ai[tts]`."
            if _RICH_AVAILABLE else
            "  piper isn't installed. Run: pip install feral-ai[tts]"
        )


def _configure_provider(
    state: WizardState,
    kind: str,
    providers: tuple[dict, ...],
    local_available: bool,
    has_openai_key: bool,
    caps: dict,
    console,
) -> None:
    options = []
    for prov in providers:
        if prov["is_local"]:
            status = STATUS_READY if local_available else STATUS_UNAVAILABLE
            hint = "ready" if local_available else f"install feral-ai[{'stt' if kind == 'stt' else 'tts'}]"
        else:
            status = STATUS_READY if has_openai_key else STATUS_NEEDS_KEY
            hint = prov["env"]
        options.append(
            Option(id=prov["id"], label=prov["label"], aliases=prov["aliases"], status=status, hint=hint)
        )

    title = "Speech-to-text" if kind == "stt" else "Text-to-speech"
    render_provider_table(title, options)
    default = state.get_setting("audio", f"{kind}_provider", "openai")
    chosen = ask_choice(f"Choose {title} provider", options, default=default)
    state.set_setting("audio", f"{kind}_provider", chosen.id)

    # Model + voice
    prov_def = next(p for p in providers if p["id"] == chosen.id)
    models = list(prov_def["available_models"])
    if chosen.id == "faster-whisper":
        models = list(caps.get("stt_models") or models)
    default_model = state.get_setting("audio", f"{kind}_model", prov_def["default_model"])
    model = _pick_or_text(
        "  Model",
        models,
        default=default_model,
        console=console,
    )
    state.set_setting("audio", f"{kind}_model", model)

    if kind == "tts":
        voices = list(prov_def.get("available_voices") or [])
        if chosen.id == "piper":
            voices = list(caps.get("tts_voices") or voices)
        default_voice = state.get_setting("audio", "tts_voice", prov_def.get("default_voice", ""))
        voice = _pick_or_text(
            "  Voice",
            voices,
            default=default_voice or (voices[0] if voices else ""),
            console=console,
        )
        state.set_setting("audio", "tts_voice", voice)


def _pick_or_text(
    prompt: str,
    options: list[str],
    *,
    default: str,
    console,
) -> str:
    """Picker-first selector for audio model / voice fields.

    When ``options`` is non-empty we use ``ui_kit.fuzzy_pick`` so the
    operator gets the same one-keystroke filter UX as the LLM model
    step instead of having to retype the model id from the printed
    Models: line. When the live catalog is empty we fall back to
    ``ask_text`` with a clear "no live catalog available" message so
    the operator still has an escape hatch for newly-released voices.
    """
    if not options:
        return ask_text(
            f"{prompt} (no live catalog available — type the id)",
            default=default,
            allow_empty=False,
        )
    if _RICH_AVAILABLE:
        console.print(f"  {prompt.strip()} catalog: " + ", ".join(options))
    choices = [{"name": opt, "value": opt} for opt in options]
    try:
        picked = ui_kit.fuzzy_pick(
            prompt.strip() or "Choose",
            choices,
            default=default if default in options else None,
        )
    except KeyboardInterrupt:
        from ..helpers import QuitNavigation
        raise QuitNavigation()
    picked = getattr(picked, "value", picked)
    if not picked:
        return ask_text(prompt, default=default, allow_empty=False)
    return picked
