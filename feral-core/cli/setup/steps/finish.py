"""Summary + 'what's next' banner.

audit-r14 / lane-07  — this is the ONLY step that calls
:meth:`WizardState.mark_complete`, which sets
``settings.meta.setup_complete = True`` and removes the resume
sidecar. Quit / Ctrl+C / crash before this step runs leaves
``setup_complete`` at its previous value (False on a fresh
install) — closes finding 09's quit-marks-complete defect.
"""

from __future__ import annotations

from ..helpers import get_console, _RICH_AVAILABLE
from ..state import WizardState


def run(state: WizardState) -> None:
    console = get_console()
    llm = state.settings.get("llm", {}) or {}
    audio = state.settings.get("audio", {}) or {}
    ha_on = bool((state.settings.get("home_assistant") or {}).get("enabled"))
    channels = (state.settings.get("channels") or {}).get("configured") or []

    summary_lines = [
        "[bold]You're set up.[/]" if _RICH_AVAILABLE else "You're set up.",
        "",
        f"  LLM:     {llm.get('provider', '?')} · {llm.get('model', '?')}",
        f"  STT:     {audio.get('stt_provider', 'openai')} · {audio.get('stt_model', '?')}",
        f"  TTS:     {audio.get('tts_provider', 'openai')} · {audio.get('tts_model', '?')}"
        + (f" · voice {audio.get('tts_voice', '?')}" if audio.get('tts_voice') else ""),
    ]
    if ha_on:
        summary_lines.append("  HA:      enabled")
    if channels:
        summary_lines.append(f"  Channels: {', '.join(channels)}")
    pairing_mode = (state.settings.get("access") or {}).get("pairing_mode") or "localhost"
    summary_lines.append(f"  Access:  {pairing_mode}")

    # The wizard configures a subset of what the CLI can reach. Naming
    # only `feral start` / `feral setup` here left ~30 shipped
    # capabilities undiscoverable unless the operator read `--help`.
    summary_lines += [
        "",
        "Start here:",
        "  feral start                    launch the brain + chat",
        "  http://localhost:9090/settings web settings UI",
        "",
        "Add more, any time:",
        "  feral key add                  add or rotate a provider API key",
        "  feral models add               keep several models on tap",
        "  feral integrations connect     Google, Notion, Spotify, MS365, Gmail",
        "  feral grant add <path>         let file tools into a folder",
        "  feral voice providers          re-pick STT / TTS / realtime",
        "  feral memory switch            change the vector-store backend",
        "  feral twin grant               per-domain digital-twin policies",
        "  feral access status            check how this brain is reachable",
        "  feral doctor                   what's reachable, what needs setup",
        "  feral setup --from-step <name> re-run one step of this wizard",
    ]
    for line in summary_lines:
        console.print(line)

    # ── Lane 07  ────────────────────────────────────────────────────
    # Mark setup as complete + remove the resume sidecar. Reaching this
    # step is the contract for "the wizard finished cleanly"; any
    # earlier exit (quit / Ctrl+C / crash) leaves ``setup_complete``
    # alone so the dashboard's "all done" gate doesn't fire on a
    # half-finished install.
    state.mark_complete()
