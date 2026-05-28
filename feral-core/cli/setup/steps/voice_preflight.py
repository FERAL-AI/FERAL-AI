"""Voice provider preflight (audit-r14 / lane-07 W7).

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

from cli import ui_kit

from ..helpers import (
    BackNavigation,
    QuitNavigation,
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

    try:
        wants_voice = confirm(
            "Configure voice now? (Skip if you only need text chat — voice can be set up later.)",
            default=True,
        )
    except (KeyboardInterrupt, BackNavigation, QuitNavigation):
        raise

    if not wants_voice:
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
            _maybe_reuse_provider_key(state, chosen.id, console, prompt_if_missing=True)
            # Lane U2 — after the operator picks a realtime provider,
            # offer the catalogue's model list (when present) so they
            # don't have to type the id by hand. Entries without a
            # ``models`` list (every realtime entry except OpenAI
            # today) silently skip with a single console hint.
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
                model_opts = [
                    Option(id=mid, label=mid, aliases=(mid,), status="ready")
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
            _maybe_reuse_provider_key(state, chosen.id, console, prompt_if_missing=False)

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
            _maybe_reuse_provider_key(state, chosen.id, console, prompt_if_missing=False)

    state.set_setting("audio", "configured_via_wizard", True)


def _maybe_reuse_provider_key(
    state: WizardState,
    voice_provider_id: str,
    console,
    *,
    prompt_if_missing: bool = False,
) -> None:
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
    """
    mapping = _VOICE_KEY_SOURCES.get(voice_provider_id)
    if not mapping:
        return
    vendor_id, env_var = mapping

    secret, source, _labels = existing_provider_key(vendor_id, env_var, state)
    if secret:
        from security.vault_keys import mask_key as _mask_key

        masked = _mask_key(secret)
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
        return

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
        return

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
        return
    key = ask_text(
        f"  Enter your {vendor_id} API key",
        allow_empty=False,
        secret=True,
    )
    import os as _os

    state.set_credential(env_var, key)
    _os.environ[env_var] = key
    try:
        from security import vault_keys

        vault_keys.add_provider_key(vendor_id, "default", key, set_active=True)
    except Exception:
        pass


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
    for opt in options:
        if opt.status == "ready":
            return opt.id
    if options and options[0].id != "__none__":
        return options[0].id
    return "__none__"
