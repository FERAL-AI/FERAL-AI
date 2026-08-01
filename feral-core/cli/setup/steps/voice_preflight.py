"""Voice provider preflight (audit-r14 / lane-07 ).

After the operator picks an LLM provider + model, this step runs the
Wave 2 Lane 05 voice catalogue through ``security.probe.probe`` and
shows them which realtime provider + STT/TTS chain are usable. The
operator picks a primary realtime provider (or skips voice) which is
persisted as ``audio.realtime_primary`` in ``settings.json``.

The catalogue is the single source of truth shared with
``feral voice providers`` (parent ack reminder #3) — no separate
provider list lives in this step.
"""

from __future__ import annotations

import asyncio

from ..helpers import (
    SkipStep,
    Option,
    ask_choice,
    ask_text,
    confirm,
    existing_provider_key,
    get_console,
    _RICH_AVAILABLE,
)
from ..state import WizardState


# Map each voice provider id to the upstream vendor's canonical
# provider id (the one ``security.vault_keys`` knows) + env var. Used
# by :func:`_maybe_reuse_provider_key` to detect "OpenAI key already
# configured for chat — reuse for realtime voice?" so the wizard
# doesn't re-prompt for the same key on the voice step.
# How many times a rejected key may be retyped before the step gives up
# and moves on with whatever was last entered.
_MAX_KEY_ATTEMPTS = 3


_VOICE_KEY_SOURCES = {
    "openai_realtime": ("openai", "OPENAI_API_KEY"),
    "openai_tts": ("openai", "OPENAI_API_KEY"),
    "openai_whisper": ("openai", "OPENAI_API_KEY"),
    "gemini_live": ("gemini", "GEMINI_API_KEY"),
    "deepgram": ("deepgram", "DEEPGRAM_API_KEY"),
    "elevenlabs": ("elevenlabs", "ELEVENLABS_API_KEY"),
    "cartesia": ("cartesia", "CARTESIA_API_KEY"),
    "groq_whisper": ("groq", "GROQ_API_KEY"),
}


async def run(state: WizardState) -> None:
    console = get_console()

    try:
        from security.probe import voice_provider_catalogue, probe
    except Exception as exc:
        console.print(
            f"[yellow]Voice catalogue unavailable: {exc}[/]"
            if _RICH_AVAILABLE else f"Voice catalogue unavailable: {exc}"
        )
        raise SkipStep()

    catalogue = voice_provider_catalogue()
    if not catalogue:
        raise SkipStep()

    if _RICH_AVAILABLE:
        console.print(
            "FERAL supports two voice modes — "
            "[bold]realtime[/bold] (single bidirectional WebSocket: OpenAI Realtime "
            "/ Gemini Live) or [bold]chained[/bold] (Deepgram/Whisper STT + "
            "ElevenLabs/Cartesia TTS). Pick the primary mode now; you can change it "
            "later with `feral voice providers` or in Settings → Voice."
        )

    # This question gates BOTH voice steps. The "Speech in / out" step
    # that follows used to ask a verbatim-identical "Configure voice
    # now?" with the opposite default, so pressing enter answered yes
    # here and no there and left a half-configured voice stack. It now
    # reads ``audio.configured_via_wizard`` instead of re-asking, so the
    # wording here has to cover the whole of voice, not just realtime.
    #
    # It is a three-way rather than a yes/no because "fully local" is a
    # different shape of setup, not a different provider inside the
    # same one: it has no credentials to collect, it downloads weights
    # instead, and realtime does not exist for it at all.
    stack = _ask_voice_stack(console)

    if stack == "skip":
        # Stamp a sentinel so we don't re-prompt next time.
        state.set_setting("audio", "configured_via_wizard", False)
        raise SkipStep()

    # Probe every catalogue entry once and group results by kind.
    realtime_entries = [e for e in catalogue if e["kind"] == "realtime"]
    stt_entries = [e for e in catalogue if e["kind"] == "stt"]
    tts_entries = [e for e in catalogue if e["kind"] == "tts"]

    console.print()
    console.print("Probing voice providers… (5s ceiling per provider)")
    probe_results = await _probe_all(probe, [e["id"] for e in catalogue])

    if stack == "local":
        await _run_local_stack(
            state, console, stt_entries, tts_entries, probe_results, probe,
        )
        state.set_setting("audio", "configured_via_wizard", True)
        return

    # Cloud path: local engines are not offered here, so a picker that
    # listed them would let an operator choose an engine this branch
    # never configures weights for.
    realtime_entries = [e for e in realtime_entries if not e.get("local")]
    stt_entries = [e for e in stt_entries if not e.get("local")]
    tts_entries = [e for e in tts_entries if not e.get("local")]

    # Render the side-by-side preflight table — same columns as
    # `feral voice providers` for parity with the standalone CLI
    # surface (parent ack reminder #3).
    _render_table(console, catalogue, probe_results)

    # ── Pick primary realtime provider (optional) ────────────────────
    if realtime_entries:
        opts = []
        for entry in realtime_entries:
            res = probe_results.get(entry["id"])
            status = _option_status(res)
            opts.append(Option(
                id=entry["id"],
                label=entry["name"],
                aliases=(entry["id"], entry["kind"]),
                status=status,
            ))
        opts.append(Option(id="__none__", label="(skip realtime)", status=""))

        default_id = state.get_setting("audio", "realtime_primary") or _first_ready(opts)
        chosen = ask_choice(
            "Pick the primary realtime voice provider",
            opts, default=default_id,
        )
        if chosen.id != "__none__":
            state.set_setting("audio", "realtime_primary", chosen.id)
            # Bug 4 — "one key, many surfaces". If the operator
            # already configured an upstream vendor's key (e.g.
            # OpenAI for chat) and the realtime provider belongs to
            # the same vendor, reuse it instead of re-prompting. The
            # vault hot-path resolver in
            # ``security.vault_keys.get_active_key`` falls back to
            # env + legacy default-namespace, so writing the key
            # through ``state.credentials`` here ensures every voice
            # surface (router, probe, realtime proxy) sees the same
            # secret without an extra round-trip.
            #
            # Bug 1 — we MUST pass the live probe result down: a
            # stored key the provider just rejected (HTTP 401) or
            # could not be reached on must NOT be silently reused.
            reuse_result = await _maybe_reuse_provider_key(
                state,
                chosen.id,
                console,
                prompt_if_missing=True,
                probe_result=probe_results.get(chosen.id),
            )
            if reuse_result == "skip":
                # Operator picked "Skip voice" after seeing the
                # rejected-key warning — clear the realtime pick
                # so the brain doesn't try to dial a broken vendor.
                state.set_setting("audio", "realtime_primary", "")
            else:
                # Lane U2 — after the operator picks a realtime
                # provider, offer the catalogue's model list (when
                # present) so they don't have to type the id by
                # hand. Entries without a ``models`` list (every
                # realtime entry except OpenAI today) silently skip
                # with a single console hint.
                chosen_entry = next(
                    (e for e in realtime_entries if e["id"] == chosen.id),
                    None,
                )
                model_ids = list((chosen_entry or {}).get("models") or [])
                if model_ids:
                    default_model = (
                        state.get_setting("audio", "realtime_model")
                        or (chosen_entry or {}).get("default_model")
                        or model_ids[0]
                    )
                    # Bug 1 — model badge must reflect the provider's
                    # real probe status. If the provider is
                    # unreachable / key-rejected, every model under
                    # it is just as broken; tagging the rows "ready"
                    # would contradict the warning we just printed.
                    provider_res = probe_results.get(chosen.id)
                    model_status = _option_status(provider_res)
                    model_opts = [
                        Option(
                            id=mid, label=mid, aliases=(mid,),
                            status=model_status,
                        )
                        for mid in model_ids
                    ]
                    chosen_model = ask_choice(
                        f"Pick the {chosen_entry.get('name', chosen.id)} model",
                        model_opts, default=default_model,
                    )
                    state.set_setting("audio", "realtime_model", chosen_model.id)
                else:
                    console.print("(no model picker — using default)")
        else:
            state.set_setting("audio", "realtime_primary", "")

    # ── Pick chained STT (optional) ──────────────────────────────────
    if stt_entries:
        opts = []
        for entry in stt_entries:
            res = probe_results.get(entry["id"])
            status = _option_status(res)
            opts.append(Option(
                id=entry["id"], label=entry["name"], aliases=(entry["id"],),
                status=status,
            ))
        opts.append(Option(id="__none__", label="(skip STT)", status=""))
        default_id = state.get_setting("audio", "chained_stt_provider") or _first_ready(opts)
        chosen = ask_choice(
            "Pick the chained-pipeline STT provider",
            opts, default=default_id,
        )
        if chosen.id != "__none__":
            state.set_setting("audio", "chained_stt_provider", chosen.id)
            stt_result = await _maybe_reuse_provider_key(
                state, chosen.id, console,
                prompt_if_missing=False,
                probe_result=probe_results.get(chosen.id),
            )
            if stt_result == "skip":
                state.set_setting("audio", "chained_stt_provider", "")
            else:
                _set_chained_fallback(state, "stt_provider", chosen.id)

    # ── Pick chained TTS (optional) ──────────────────────────────────
    if tts_entries:
        opts = []
        for entry in tts_entries:
            res = probe_results.get(entry["id"])
            status = _option_status(res)
            opts.append(Option(
                id=entry["id"], label=entry["name"], aliases=(entry["id"],),
                status=status,
            ))
        opts.append(Option(id="__none__", label="(skip TTS)", status=""))
        default_id = state.get_setting("audio", "chained_tts_provider") or _first_ready(opts)
        chosen = ask_choice(
            "Pick the chained-pipeline TTS provider",
            opts, default=default_id,
        )
        if chosen.id != "__none__":
            state.set_setting("audio", "chained_tts_provider", chosen.id)
            tts_result = await _maybe_reuse_provider_key(
                state, chosen.id, console,
                prompt_if_missing=False,
                probe_result=probe_results.get(chosen.id),
            )
            if tts_result == "skip":
                state.set_setting("audio", "chained_tts_provider", "")
            else:
                _set_chained_fallback(state, "tts_provider", chosen.id)

    state.set_setting("audio", "configured_via_wizard", True)


def _ask_voice_stack(console) -> str:
    """Fully local, cloud, or skip. Returns ``local|cloud|skip``.

    The wording is load-bearing. "Fully local voice" is a claim a
    privacy-motivated operator will read as "nothing I say leaves this
    machine", and that is not what it means: the transcript still goes
    to whatever LLM provider is configured, which for almost every
    install is a remote API. Saying so here, once, in the sentence that
    offers the option, is the only place it cannot be missed.
    """
    console.print()
    if _RICH_AVAILABLE:
        console.print(
            "[bold]Local voice[/bold] runs speech-to-text, text-to-speech and "
            "endpointing on this machine. No audio is uploaded and no API key "
            "is needed for them.\n"
            "[yellow]Your words still reach your LLM provider.[/] FERAL "
            "transcribes locally and then sends the [bold]text[/bold] to the "
            "model you configured in the previous step, which is a remote "
            "service unless you pointed it at Ollama or LM Studio. "
            "\"Fully local voice\" means the audio, not the conversation."
        )
    else:
        console.print(
            "Local voice runs speech-to-text, text-to-speech and endpointing "
            "on this machine. No audio is uploaded and no API key is needed "
            "for them.\n"
            "Your words still reach your LLM provider: FERAL transcribes "
            "locally and then sends the TEXT to the model you configured, "
            "which is a remote service unless you pointed it at Ollama or "
            "LM Studio. \"Fully local voice\" means the audio, not the "
            "conversation."
        )
    console.print()

    chosen = ask_choice(
        "How should voice run?",
        [
            Option(
                id="cloud",
                label="Cloud providers (lowest latency, needs API keys)",
                aliases=("cloud", "remote", "api"),
            ),
            Option(
                id="local",
                label="Fully local audio (no keys, downloads models; LLM still remote)",
                aliases=("local", "offline", "private"),
            ),
            Option(
                id="skip",
                label="Skip voice for now (text chat only)",
                aliases=("skip", "none", "no"),
            ),
        ],
        default="cloud",
    )
    return chosen.id


async def _run_local_stack(
    state: WizardState,
    console,
    stt_entries: list,
    tts_entries: list,
    probe_results: dict,
    probe_fn,
) -> None:
    """Configure a fully-local chained pipeline.

    Deliberately narrower than the cloud path:

    * No realtime pick. Every realtime provider is a bidirectional
      socket to a vendor; there is no local one, so offering the
      question at all would imply otherwise.
    * No credential prompts. Readiness for these engines is
      "importable, and the weights are on disk", so the remedy for a
      not-ready engine is a download, not a key.
    * Endpointing is configured too. Silero VAD is what makes the
      local path feel like a conversation instead of a walkie-talkie
      (measured: 2218ms to end an utterance without it, 309ms with),
      and it is 2.2MB, so it is offered first and defaulted on.
    """
    state.set_setting("audio", "realtime_primary", "")

    await _offer_vad_download(console)

    local_stt = [e for e in stt_entries if e.get("local")]
    local_tts = [e for e in tts_entries if e.get("local")]

    if local_stt:
        opts = [
            Option(
                id=e["id"], label=e["name"], aliases=(e["id"],),
                status=_option_status(probe_results.get(e["id"])),
            )
            for e in local_stt
        ]
        opts.append(Option(id="__none__", label="(skip local STT)", status=""))
        default_id = (
            state.get_setting("audio", "chained_stt_provider")
            or _default_local_stt(local_stt)
        )
        chosen = ask_choice(
            "Pick the local speech-to-text engine", opts, default=default_id,
        )
        if chosen.id != "__none__":
            state.set_setting("audio", "chained_stt_provider", chosen.id)
            _set_chained_fallback(state, "stt_provider", chosen.id)
            _set_voice_chained(state, "stt_provider", chosen.id)
            await _offer_stt_download(console, state, chosen.id, probe_fn)

    if local_tts:
        opts = [
            Option(
                id=e["id"], label=e["name"], aliases=(e["id"],),
                status=_option_status(probe_results.get(e["id"])),
            )
            for e in local_tts
        ]
        opts.append(Option(id="__none__", label="(skip local TTS)", status=""))
        default_id = (
            state.get_setting("audio", "chained_tts_provider")
            or _default_local_tts(local_tts, probe_results)
        )
        chosen = ask_choice(
            "Pick the local text-to-speech engine", opts, default=default_id,
        )
        if chosen.id != "__none__":
            state.set_setting("audio", "chained_tts_provider", chosen.id)
            _set_chained_fallback(state, "tts_provider", chosen.id)
            _set_voice_chained(state, "tts_provider", chosen.id)
            if chosen.id == "piper":
                _warn_piper_licence(console)
                await _offer_piper_download(console, state)

    # The chained pipeline is the only mode that can run local engines,
    # so pin it rather than leave the router to infer it from a
    # half-configured realtime block.
    state.set_setting("audio", "fallback_mode", "chained")


def _default_local_stt(entries: list) -> str:
    """whisper.cpp on macOS, faster-whisper elsewhere.

    Not a preference, a hardware fact: CTranslate2 (faster-whisper) has
    no Metal backend, so on Apple Silicon it runs on CPU while
    whisper.cpp uses the GPU.
    """
    import platform

    ids = {e["id"] for e in entries}
    if platform.system() == "Darwin" and "whispercpp" in ids:
        return "whispercpp"
    if "faster_whisper" in ids:
        return "faster_whisper"
    return "__none__"


def _default_local_tts(entries: list, probe_results: dict) -> str:
    """macOS ``say`` when it is there, otherwise Piper.

    ``say`` is already installed, needs no download, and carries no
    licensing question. Piper is GPL-3.0-or-later, so it is never the
    default on a machine that has an alternative.
    """
    ids = {e["id"] for e in entries}
    if "macos_say" in ids:
        result = probe_results.get("macos_say")
        if result is not None and getattr(result, "ok", False):
            return "macos_say"
    if "piper" in ids:
        return "piper"
    return "__none__"


def _warn_piper_licence(console) -> None:
    message = (
        "Piper is licensed GPL-3.0-or-later, while FERAL is Apache-2.0. "
        "It is not installed by default and is not pulled in by any other "
        "extra. Installing it is your explicit choice: "
        "pip install 'feral-ai[tts-piper]'"
    )
    console.print(f"  [yellow]{message}[/]" if _RICH_AVAILABLE else f"  {message}")


async def _offer_vad_download(console) -> None:
    """Offer the 2.2MB Silero VAD download. Defaults to yes."""
    try:
        from voice import local_models
        from voice.vad import vad_available
    except Exception as exc:
        console.print(f"  Voice activity detection unavailable: {exc}")
        return

    ready, reason = vad_available()
    if ready:
        console.print(
            "  [green]OK[/] Server voice-activity detection ready (Silero VAD)"
            if _RICH_AVAILABLE else
            "  OK Server voice-activity detection ready (Silero VAD)"
        )
        return
    if "onnxruntime" in reason:
        console.print(
            "  [yellow]Voice activity detection needs onnxruntime:[/] "
            "pip install 'feral-ai[vad]'"
            if _RICH_AVAILABLE else
            "  Voice activity detection needs onnxruntime: "
            "pip install 'feral-ai[vad]'"
        )
        return

    console.print(
        "  Server voice-activity detection (Silero VAD, MIT, 2.2MB) decides "
        "when you have stopped speaking. Without it FERAL waits for silence "
        "on the wire instead, which adds about 1.9 seconds to every reply."
    )
    if not confirm("  Download Silero VAD now (2.2MB)?", default=True):
        return
    try:
        path = await asyncio.to_thread(
            local_models.ensure_silero_vad, allow_download=True
        )
        console.print(f"  Downloaded {path}")
    except Exception as exc:
        console.print(f"  [red]Download failed:[/] {exc}" if _RICH_AVAILABLE
                      else f"  Download failed: {exc}")


async def _offer_stt_download(
    console, state: WizardState, provider_id: str, probe_fn,
) -> None:
    """Offer the whisper weights download for the chosen local STT."""
    try:
        from voice import local_models
    except Exception:
        return

    if provider_id == "whispercpp":
        try:
            import pywhispercpp  # noqa: F401
        except Exception:
            console.print(
                "  [yellow]whisper.cpp is not installed:[/] "
                "pip install 'feral-ai[stt-local]'"
                if _RICH_AVAILABLE else
                "  whisper.cpp is not installed: pip install 'feral-ai[stt-local]'"
            )
            return
        model = state.get_setting("audio", "chained_stt_model") or "base.en"
        if local_models.whispercpp_model_present(model):
            console.print(f"  [green]OK[/] whisper.cpp model {model} already present"
                          if _RICH_AVAILABLE else
                          f"  OK whisper.cpp model {model} already present")
            return
        size = local_models.whispercpp_model_size_mb(model)
        if not confirm(
            f"  Download the whisper.cpp {model} model now (~{size}MB)?",
            default=True,
        ):
            return
        try:
            path = await asyncio.to_thread(
                local_models.ensure_whispercpp_model, model, allow_download=True,
            )
            console.print(f"  Downloaded {path}")
            _set_voice_chained(state, "stt_model", model)
        except Exception as exc:
            console.print(f"  [red]Download failed:[/] {exc}" if _RICH_AVAILABLE
                          else f"  Download failed: {exc}")
        return

    if provider_id == "faster_whisper":
        try:
            import faster_whisper  # noqa: F401
        except Exception:
            console.print(
                "  [yellow]faster-whisper is not installed:[/] "
                "pip install 'feral-ai[stt]'"
                if _RICH_AVAILABLE else
                "  faster-whisper is not installed: pip install 'feral-ai[stt]'"
            )
            return
        console.print(
            "  faster-whisper downloads its model on first use "
            "(~150MB for base.en)."
        )


async def _offer_piper_download(console, state: WizardState) -> None:
    try:
        from voice import local_models
        from voice.tts_providers.piper import DEFAULT_VOICE
    except Exception:
        return
    try:
        import piper  # noqa: F401
    except Exception:
        console.print(
            "  [yellow]Piper is not installed:[/] pip install 'feral-ai[tts-piper]'"
            if _RICH_AVAILABLE else
            "  Piper is not installed: pip install 'feral-ai[tts-piper]'"
        )
        return
    voice = state.get_setting("audio", "chained_tts_voice") or DEFAULT_VOICE
    if local_models.piper_voice_present(voice):
        console.print(f"  [green]OK[/] Piper voice {voice} already present"
                      if _RICH_AVAILABLE else
                      f"  OK Piper voice {voice} already present")
        return
    if not confirm(f"  Download the Piper voice {voice} now (~60MB)?", default=True):
        return
    try:
        path = await asyncio.to_thread(
            local_models.ensure_piper_voice, voice, allow_download=True,
        )
        console.print(f"  Downloaded {path}")
        _set_voice_chained(state, "tts_voice_id", voice)
    except Exception as exc:
        console.print(f"  [red]Download failed:[/] {exc}" if _RICH_AVAILABLE
                      else f"  Download failed: {exc}")


def _set_voice_chained(state: WizardState, key: str, value: str) -> None:
    """Write a pick into ``voice.chained``, the block the router reads first.

    ``_resolve_chained_config`` gives ``voice.chained.*`` precedence
    over ``audio.chained_fallback.*`` because the former is a UI choice
    and the latter is a wizard/headless default. A local pick made here
    is an explicit choice, so it goes in both: the nested audio key
    keeps the picker's own default stable across re-runs, and this one
    is what the running pipeline obeys even if the phone Settings panel
    later writes the block.
    """
    voice = state.settings.setdefault("voice", {})
    chained = dict(voice.get("chained") or {})
    chained[key] = value
    voice["chained"] = chained


def _set_chained_fallback(state: WizardState, key: str, provider_id: str) -> None:
    """Mirror a chained pick into ``audio.chained_fallback``.

    ``audio.chained_stt_provider`` / ``audio.chained_tts_provider`` are
    this step's own resume defaults — nothing at runtime reads them.
    The voice router reads ``audio.chained_fallback.stt_provider`` and
    ``.tts_provider`` (see ``voice/router.py::_try_chained_morph``), so
    a chained pick made here had no effect on the chained pipeline that
    actually runs. Write both: the flat key keeps the picker's default
    stable across re-runs, the nested one is what the brain obeys.
    """
    audio = state.settings.setdefault("audio", {})
    chained = dict(audio.get("chained_fallback") or {})
    chained[key] = provider_id
    audio["chained_fallback"] = chained


async def _maybe_reuse_provider_key(
    state: WizardState,
    voice_provider_id: str,
    console,
    *,
    prompt_if_missing: bool = False,
    probe_result=None,
) -> str:
    """Bug 4 — share one key across chat + realtime voice + future video.

    When the operator already configured a vendor's key in an earlier
    step (chat / LLM) and now picks a voice provider from the same
    vendor, reuse the stored key instead of re-prompting. We print a
    one-line confirmation so the operator sees that "one key, many
    surfaces" actually happened.

    When the voice provider belongs to a DIFFERENT vendor (e.g.
    Gemini Live picked but only OpenAI is configured), we render a
    short inline prompt for the missing key — that way the operator
    can finish voice setup without quitting the wizard to run
    ``feral key add`` separately.

    The key always lands in ``state.credentials`` + the process env
    var + (best-effort) the labeled-key vault so the same value is
    visible to ``security.probe`` and to the voice router's
    boot-time hydration path.

    Bug 1 — when ``probe_result`` is a non-OK ``ProbeResult`` whose
    status indicates the live provider rejected the stored secret
    (HTTP 401/403) or is genuinely unreachable, we MUST NOT silently
    reuse the stored key. We surface a loud warning and offer the
    operator a three-way choice: replace it now, keep it anyway
    (operator might know the probe is a false negative), or skip
    this voice provider entirely. Probes that came back ``None`` or
    show "not configured" / "no_key" reasons are treated as
    informational (the prior happy-path silent-reuse keeps the
    wizard quiet when the key is actually fine).

    Returns one of:
    * ``""`` — silent reuse / standard path completed.
    * ``"skip"`` — operator picked "skip voice provider" after a
      rejected-key warning; caller should NOT advance to the
      model picker and should clear the relevant ``audio.*`` setting.
    * ``"replaced"`` — operator typed a fresh key.
    * ``"kept"`` — operator kept the bad key anyway.
    """
    mapping = _VOICE_KEY_SOURCES.get(voice_provider_id)
    if not mapping:
        return ""
    vendor_id, env_var = mapping

    secret, source, _labels = existing_provider_key(vendor_id, env_var, state)
    if secret:
        from security.vault_keys import mask_key as _mask_key

        masked = _mask_key(secret)

        if _probe_indicates_bad_key(probe_result):
            # The probe just told us this exact key is broken. Don't
            # paper over it with a green ✓ — show the operator what
            # we saw and ask what to do.
            return await _handle_bad_existing_key(
                state=state,
                voice_provider_id=voice_provider_id,
                vendor_id=vendor_id,
                env_var=env_var,
                console=console,
                masked=masked,
                source=source,
                probe_result=probe_result,
            )

        if _RICH_AVAILABLE:
            console.print(
                f"  [green]✓[/] Reusing existing {vendor_id} key "
                f"[bold]{masked}[/] for {voice_provider_id} "
                f"[dim](source: {source})[/]"
            )
        else:
            console.print(
                f"  ✓ Reusing existing {vendor_id} key {masked} for {voice_provider_id}"
            )
        # Make sure ``credentials.json`` + env carry the value so the
        # voice router + probe surface see it on the next boot.
        import os as _os

        if env_var:
            state.set_credential(env_var, secret)
            _os.environ[env_var] = secret
        return ""

    # No key for this vendor anywhere → realtime is the only surface
    # that prompts inline (the chained STT/TTS paths just note the
    # missing key so the operator can fix it with ``feral key add``
    # later without blocking the wizard).
    if not prompt_if_missing:
        if _RICH_AVAILABLE:
            console.print(
                f"  [yellow]Note:[/] no {vendor_id} key found — "
                f"{voice_provider_id} won't work until one is added "
                f"(`feral key add --provider {vendor_id}`)."
            )
        else:
            console.print(
                f"  Note: no {vendor_id} key found — {voice_provider_id} won't work "
                f"until you run `feral key add --provider {vendor_id}`."
            )
        return ""

    if _RICH_AVAILABLE:
        console.print(
            f"  [yellow]No {vendor_id} key found yet[/] — "
            f"voice provider {voice_provider_id} needs one to work."
        )
    else:
        console.print(
            f"  No {vendor_id} key found yet — {voice_provider_id} needs one."
        )
    if not confirm(f"  Enter a {vendor_id} API key now?", default=True):
        return ""
    await _prompt_and_verify_key(
        state=state,
        vendor_id=vendor_id,
        env_var=env_var,
        voice_provider_id=voice_provider_id,
        console=console,
        prompt=f"  Enter your {vendor_id} API key",
    )
    return "replaced"


async def _prompt_and_verify_key(
    *,
    state: WizardState,
    vendor_id: str,
    env_var: str,
    voice_provider_id: str,
    console,
    prompt: str,
) -> bool:
    """Prompt for a key, persist it, then probe it.

    The step probed every provider up front and made a lot of noise
    about a stored key the vendor had rejected, but a key typed *during*
    the step went straight to the vault unverified. The operator could
    fix a bad key with a second bad key and the wizard would say
    nothing. Same store→probe→report→retry shape as ``feral key add``.

    Only a *verdict* re-prompts. A probe that comes back "not
    configured" is inconclusive, not a rejection: it means the probe
    could not see the credential we just stored (a voice provider that
    resolves its key from somewhere this process hasn't populated
    yet), and badgering the operator to retype a key we have no
    evidence is wrong would be worse than saying nothing. This is the
    same distinction :func:`_probe_indicates_bad_key` already draws for
    pre-existing keys.

    Every prompt accepts an empty line as "skip this credential". It
    used to be ``allow_empty=False``, and both the helper and
    ``ui_kit.password`` loop forever on an empty value, so an operator
    who reached this prompt with no key to type had no way out but
    Ctrl+C. The attempt counter only bounded REJECTED keys, never
    empty ones. A wizard prompt must always terminate.

    Returns True when the provider accepted the key.
    """
    import os as _os

    from security.probe import probe as _probe

    skippable_prompt = f"{prompt} (leave blank to skip)"

    # Bounded so a provider that rejects everything (expired billing,
    # region block) can't trap the operator in the wizard.
    for attempt in range(_MAX_KEY_ATTEMPTS):
        key = (ask_text(skippable_prompt, allow_empty=True, secret=True) or "").strip()
        if not key:
            console.print(
                f"  [dim]Skipped. No {vendor_id} key stored, add one later "
                f"with `feral key add --provider {vendor_id}`.[/]"
                if _RICH_AVAILABLE else
                f"  Skipped. No {vendor_id} key stored, add one later with "
                f"`feral key add --provider {vendor_id}`."
            )
            return False
        state.set_credential(env_var, key)
        _os.environ[env_var] = key
        try:
            from security import vault_keys

            vault_keys.add_provider_key(vendor_id, "default", key, set_active=True)
        except Exception:
            # Best-effort, exactly as before: the env + state.credentials
            # writes above are what the rest of this run reads.
            pass

        console.print(f"  Verifying the {vendor_id} key…")
        result = await _probe(voice_provider_id, force=True)

        if getattr(result, "ok", False):
            console.print(
                f"  [green]✔[/] {voice_provider_id} accepted the key."
                if _RICH_AVAILABLE else
                f"  ✔ {voice_provider_id} accepted the key."
            )
            return True

        if not _probe_indicates_bad_key(result):
            console.print(
                f"  [dim]Stored. {voice_provider_id} could not be verified "
                f"from here ({result.reason or 'not configured'}) — check it "
                f"with `feral voice providers` once the brain is up.[/]"
                if _RICH_AVAILABLE else
                f"  Stored. {voice_provider_id} could not be verified from "
                f"here ({result.reason or 'not configured'})."
            )
            return False

        verdict = (
            f"HTTP {result.status_code}"
            if result.status_code in (401, 403)
            else (result.reason or "unreachable")
        )
        console.print(
            f"  [red]✘[/] {voice_provider_id} rejected the key ({verdict})."
            if _RICH_AVAILABLE else
            f"  ✘ {voice_provider_id} rejected the key ({verdict})."
        )
        last_attempt = attempt == _MAX_KEY_ATTEMPTS - 1
        if last_attempt or not confirm(
            f"  Try a different {vendor_id} key?", default=True,
        ):
            console.print(
                f"  [yellow]Keeping the key as typed — {voice_provider_id} "
                f"may not work until it's replaced.[/]"
                if _RICH_AVAILABLE else
                f"  Keeping the key as typed — {voice_provider_id} may not "
                f"work until it's replaced."
            )
            return False
    return False


def _probe_indicates_bad_key(res) -> bool:
    """True when ``res`` is a probe verdict the operator should see
    before we silently reuse a stored key.

    "Bad" means: probe ran, came back ``ok=False`` AND the failure
    is something the stored key actually owns — HTTP 401/403 (key
    rejected) or "unreachable" (we couldn't tell the key from the
    transport, but the operator should still know before we
    advertise the provider as configured).

    A ``None`` result (probe not run / cancelled) or a reason like
    ``"missing" / "no_key" / "not_configured"`` is NOT bad-key — it
    just means the probe couldn't even try because nothing was
    configured at probe time. Those paths fall back to the silent-
    reuse happy path.
    """
    if res is None or getattr(res, "ok", False):
        return False
    reason = (getattr(res, "reason", "") or "").lower()
    if any(s in reason for s in ("missing", "no_key", "no_token", "not_configured")):
        return False
    return True


async def _handle_bad_existing_key(
    *,
    state: WizardState,
    voice_provider_id: str,
    vendor_id: str,
    env_var: str,
    console,
    masked: str,
    source: str,
    probe_result,
) -> str:
    """Bug 1 — render the "we found a key the provider just
    rejected" warning + Replace / Keep anyway / Skip three-way."""
    status_code = getattr(probe_result, "status_code", None)
    reason = (getattr(probe_result, "reason", "") or "").lower()
    if status_code in (401, 403):
        verdict = f"HTTP {status_code} — key rejected"
    elif reason:
        verdict = reason
    else:
        verdict = "unreachable"

    if _RICH_AVAILABLE:
        console.print(
            f"  [yellow]⚠ The existing {vendor_id} key "
            f"[bold]{masked}[/] was rejected by the provider "
            f"({verdict}).[/]  [dim]source: {source}[/]"
        )
    else:
        console.print(
            f"  ⚠ The existing {vendor_id} key {masked} was rejected "
            f"by the provider ({verdict}).  source: {source}"
        )

    action_opts = [
        Option(id="replace", label="Replace it now"),
        Option(id="keep", label="Keep it anyway (probe may be a false negative)"),
        Option(id="skip", label=f"Skip {voice_provider_id}"),
    ]
    chosen = ask_choice(
        f"  How should we handle the {vendor_id} key?",
        action_opts, default="replace",
    )
    if chosen.id == "skip":
        return "skip"
    if chosen.id == "keep":
        # Operator overrode the warning. Make sure env + state carry
        # the secret so downstream surfaces still see it.
        secret_now, _src, _lbls = existing_provider_key(vendor_id, env_var, state)
        import os as _os
        if env_var and secret_now:
            state.set_credential(env_var, secret_now)
            _os.environ[env_var] = secret_now
        return "kept"

    # replace → free-text prompt for a fresh key, persist everywhere,
    # then re-probe. Replacing a rejected key with another rejected key
    # used to be silently accepted.
    await _prompt_and_verify_key(
        state=state,
        vendor_id=vendor_id,
        env_var=env_var,
        voice_provider_id=voice_provider_id,
        console=console,
        prompt=f"  Enter a new {vendor_id} API key",
    )
    return "replaced"


async def _probe_all(probe_fn, provider_ids):
    """Run all probes in parallel and return id→ProbeResult."""
    results = await asyncio.gather(*[probe_fn(pid) for pid in provider_ids], return_exceptions=True)
    out = {}
    for pid, res in zip(provider_ids, results):
        out[pid] = res if not isinstance(res, Exception) else None
    return out


def _render_table(console, catalogue, results):
    if _RICH_AVAILABLE:
        try:
            from rich.table import Table
        except ImportError:
            Table = None
        if Table is not None:
            table = Table(title="Voice catalogue", show_lines=False)
            table.add_column("Kind", style="dim")
            table.add_column("Provider", style="bold")
            table.add_column("Probe")
            for entry in catalogue:
                res = results.get(entry["id"])
                cell = _probe_cell(res)
                table.add_row(entry["kind"], entry["name"], cell)
            console.print(table)
            return
    for entry in catalogue:
        res = results.get(entry["id"])
        mark = "ok " if (res and getattr(res, "ok", False)) else "off"
        console.print(f"  [{entry['kind']:<8}] {entry['name']:<28} {mark}")


def _probe_cell(res) -> str:
    if res is None:
        return "[dim]—[/dim]"
    if getattr(res, "ok", False):
        return "[green]✔ ready[/green]"
    reason = (getattr(res, "reason", "") or "").lower()
    if any(s in reason for s in ("missing", "no_key", "no_token", "not_configured")):
        return "[cyan]ℹ not configured[/cyan]"
    if getattr(res, "status_code", None) in (401, 403):
        return "[red]✘ key rejected[/red]"
    return f"[yellow]⚠ {reason or 'error'}[/yellow]"


def _option_status(res) -> str:
    """Map a ProbeResult to the helpers.Option status string."""
    if res is None or not getattr(res, "ok", False):
        reason = (getattr(res, "reason", "") or "").lower() if res else ""
        if any(s in reason for s in ("missing", "no_key", "no_token", "not_configured")):
            return "needs_api_key"
        if res is None:
            return "unavailable"
        return "unreachable"
    return "ready"


def _first_ready(options) -> str:
    """Default the picker to a provider that actually probed ready.

    This used to fall back to ``options[0]`` when nothing was ready,
    which on a fresh keyless install preselected the first catalogue
    entry (OpenAI Realtime). Pressing enter through the wizard then
    wrote ``audio.realtime_primary=openai_realtime`` on a machine with
    no OpenAI key, and walked the operator straight into a mandatory
    API-key prompt they never asked for. ``__none__`` is the honest
    default when nothing is configured: the operator can still pick a
    provider explicitly, and the enter-through path terminates.
    """
    for opt in options:
        if opt.status == "ready":
            return opt.id
    return "__none__"
