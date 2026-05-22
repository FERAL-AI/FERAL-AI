"""``feral voice`` — list voice providers and test STT/TTS.

Wraps Wave 2 Lane 05's voice catalogue + voice provider modules. The
catalogue (``security.probe.voice_provider_catalogue``) is the single
source of truth — both this CLI and the WebUI Settings → Voice picker
read the same list. Pure-local: ``providers`` reads catalogue + cached
probe state; ``test`` runs an actual probe + transcript/synthesis
round-trip when an audio file is provided.

Usage
-----

    feral voice providers
    feral voice test --provider deepgram --input ~/sample.wav
    feral voice test --provider elevenlabs --text "hello world" --out /tmp/out.mp3

The ``test`` paths require an API key for the provider — `feral key
add --provider <id>` first if a probe row says ℹ "not configured".
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Optional

from cli import ui_kit


# ─────────────────────────────────────────────────────────────────────
# argparse registration (called from cli/main.py)
# ─────────────────────────────────────────────────────────────────────


def register_voice_subparser(sub: "argparse._SubParsersAction") -> None:
    """Register ``feral voice {providers,test}`` under ``feral``."""
    voice_p = sub.add_parser(
        "voice",
        help="Voice provider catalogue + STT/TTS round-trip tester",
    )
    voice_sub = voice_p.add_subparsers(dest="action")

    voice_sub.add_parser(
        "providers",
        help="List all voice providers (realtime / STT / TTS) with probe status.",
    )

    test_p = voice_sub.add_parser(
        "test",
        help="Round-trip a voice provider against a real audio file or text.",
    )
    test_p.add_argument("--provider", required=True, help="Provider id (deepgram / elevenlabs / ...)")
    test_p.add_argument(
        "--input", default="",
        help="Path to a WAV file for STT providers (16 kHz mono PCM recommended).",
    )
    test_p.add_argument(
        "--text", default="",
        help="Text to synthesize for TTS providers.",
    )
    test_p.add_argument(
        "--out", default="",
        help="Output file path for TTS audio (default: /tmp/feral-voice-test.<ext>).",
    )


def dispatch_voice_subcommand(args) -> int:
    action = getattr(args, "action", None) or "providers"
    if action == "providers":
        return cmd_voice_providers()
    if action == "test":
        return cmd_voice_test(
            provider=getattr(args, "provider", "") or "",
            input_path=getattr(args, "input", "") or "",
            text=getattr(args, "text", "") or "",
            out_path=getattr(args, "out", "") or "",
        )
    print(f"Unknown action: {action}. Try one of: providers, test.")
    return 2


# ─────────────────────────────────────────────────────────────────────
# providers — list catalogue + probe status
# ─────────────────────────────────────────────────────────────────────


def _probe_all(provider_ids: list[str]) -> dict:
    """Run every catalogue probe in parallel and return id→ProbeResult."""
    from security.probe import probe

    async def _gather():
        return await asyncio.gather(*[probe(pid) for pid in provider_ids])

    try:
        results = asyncio.run(_gather())
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            results = loop.run_until_complete(_gather())
        finally:
            loop.close()
    return dict(zip(provider_ids, results))


def cmd_voice_providers() -> int:
    """Print the voice catalogue with green/yellow/red probe status."""
    try:
        from security.probe import voice_provider_catalogue
    except Exception as exc:
        print(f"  Voice catalogue unavailable: {exc}")
        return 1

    catalogue = voice_provider_catalogue()
    if not catalogue:
        print("  No voice providers registered.")
        return 0

    ids = [entry["id"] for entry in catalogue]
    results = _probe_all(ids)

    try:
        from rich.console import Console
        from rich.table import Table
        console = Console()
    except ImportError:
        console = None

    if console is not None:
        table = Table(title="Voice providers", show_lines=False)
        table.add_column("Kind", style="dim")
        table.add_column("Provider", style="bold")
        table.add_column("Probe")
        table.add_column("Detail")
        for entry in catalogue:
            res = results.get(entry["id"])
            if res is None:
                cell, detail = "[dim]—[/dim]", "no probe registered"
            elif res.ok:
                cell = "[green]✔ ok[/green]"
                detail = f"{res.detail or 'OK'} ({res.latency_ms:.0f}ms)"
            else:
                reason = (res.reason or "").lower()
                if any(s in reason for s in ("missing", "no_key", "no_token", "not_configured")):
                    cell, detail = "[cyan]ℹ not configured[/cyan]", res.detail or reason
                elif res.status_code in (401, 403):
                    cell, detail = "[red]✘ key rejected[/red]", res.detail or "auth_failed"
                else:
                    cell, detail = "[yellow]⚠ error[/yellow]", res.detail or reason
            table.add_row(entry["kind"], entry["name"], cell, detail)
        console.print(table)
    else:
        for entry in catalogue:
            res = results.get(entry["id"])
            mark = "ok " if (res and res.ok) else "off"
            print(f"  [{entry['kind']:<8}] {entry['name']:<30} {mark}")

    # Summary count
    ok_count = sum(1 for r in results.values() if r and r.ok)
    print()
    print(f"  {ok_count}/{len(catalogue)} providers green.")
    return 0


# ─────────────────────────────────────────────────────────────────────
# test — STT or TTS round-trip
# ─────────────────────────────────────────────────────────────────────


def cmd_voice_test(
    *,
    provider: str,
    input_path: str = "",
    text: str = "",
    out_path: str = "",
) -> int:
    """Round-trip a voice provider against a real audio file (STT) or
    text (TTS).

    The provider id MUST appear in
    ``security.probe.voice_provider_catalogue``. We dispatch on
    ``kind`` so callers don't have to remember which providers are
    STT vs TTS — a wrong-input pairing surfaces a clear error.
    """
    try:
        from security.probe import voice_provider_catalogue
    except Exception as exc:
        print(f"  Voice catalogue unavailable: {exc}")
        return 1

    if not provider:
        print("  --provider is required.")
        return 2

    catalogue = voice_provider_catalogue()
    entry = next((e for e in catalogue if e["id"] == provider), None)
    if entry is None:
        print(f"  Unknown voice provider: {provider!r}")
        print(f"  Known: {', '.join(e['id'] for e in catalogue)}")
        return 2

    kind = entry["kind"]
    if kind == "stt":
        if not input_path:
            print("  --input <wav file> is required for STT providers.")
            return 2
        return _run_stt_test(provider, input_path)
    if kind == "tts":
        if not text:
            print("  --text \"...\" is required for TTS providers.")
            return 2
        return _run_tts_test(provider, text, out_path)
    if kind == "realtime":
        # Realtime providers are bidirectional WebSocket streams; a
        # round-trip "test" requires mic + speaker capture which the
        # CLI doesn't have in scope. Fall back to a probe-only run.
        return _run_realtime_probe(provider)

    print(f"  Unknown provider kind: {kind!r}")
    return 2


def _run_stt_test(provider: str, input_path: str) -> int:
    """Send a WAV file to ``provider`` and print the transcript."""
    from voice.stt_providers import get_stt_provider

    path = Path(input_path).expanduser().resolve()
    if not path.is_file():
        print(f"  Input file not found: {path}")
        return 2

    api_key = _resolve_api_key_for(provider)
    if not api_key and provider not in ("local_whisper", "faster_whisper"):
        print(f"  No API key found for {provider} — run `feral key add --provider {provider}`.")
        return 1

    try:
        stt = get_stt_provider(provider, api_key=api_key)
    except Exception as exc:
        print(f"  Could not initialise {provider}: {exc}")
        return 1

    audio_bytes = path.read_bytes()
    print(f"  Sending {len(audio_bytes):,} bytes ({path.name}) to {provider}…")

    async def _go():
        transcript_parts = []
        try:
            async def _drain():
                async for fragment in stt.open_stream():
                    if fragment.is_final or not fragment.is_partial:
                        transcript_parts.append(fragment.text)
                        if fragment.speech_final:
                            break

            drain_task = asyncio.create_task(_drain())
            # Send the whole file as one chunk for buffered providers;
            # streaming providers (Deepgram) accept the same path —
            # the provider itself handles chunk size.
            await stt.send_audio(audio_bytes)
            await stt.flush()
            try:
                await asyncio.wait_for(drain_task, timeout=30)
            except asyncio.TimeoutError:
                pass
        finally:
            await stt.close()
        return " ".join(t.strip() for t in transcript_parts if t.strip())

    try:
        transcript = asyncio.run(_go())
    except Exception as exc:
        print(f"  STT round-trip failed: {exc}")
        return 1

    if not transcript:
        print("  No transcript returned (provider may have rejected the audio format — try 16 kHz mono PCM WAV).")
        return 1
    print()
    print(f"  Transcript: {transcript}")
    return 0


def _run_tts_test(provider: str, text: str, out_path: str) -> int:
    """Synthesize ``text`` via ``provider`` and write the audio to disk."""
    from voice.tts_providers import get_tts_provider

    api_key = _resolve_api_key_for(provider)
    if not api_key:
        print(f"  No API key found for {provider} — run `feral key add --provider {provider}`.")
        return 1

    if not out_path:
        ext = "mp3" if provider in ("elevenlabs", "cartesia", "openai_tts") else "wav"
        out_path = f"/tmp/feral-voice-test.{ext}"

    try:
        tts = get_tts_provider(provider, api_key=api_key)
    except Exception as exc:
        print(f"  Could not initialise {provider}: {exc}")
        return 1

    print(f"  Synthesizing {len(text)} chars via {provider}…")

    async def _go():
        chunks: list[bytes] = []
        async for chunk in tts.synthesize(text):
            chunks.append(chunk)
        return b"".join(chunks)

    try:
        audio = asyncio.run(_go())
    except Exception as exc:
        print(f"  TTS round-trip failed: {exc}")
        return 1

    out = Path(out_path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(audio)
    print()
    print(f"  Wrote {len(audio):,} bytes to {out}")
    return 0


def _run_realtime_probe(provider: str) -> int:
    """Realtime providers don't have a 'test' round-trip from the CLI;
    fall back to a hard probe and report green/red."""
    from security.probe import probe

    print(f"  Realtime providers can't round-trip from the CLI — running a probe instead.")
    try:
        result = asyncio.run(probe(provider, force=True))
    except Exception as exc:
        print(f"  Probe failed: {exc}")
        return 1
    if result.ok:
        print(f"  ✔ {provider} probe OK ({result.latency_ms:.0f}ms)")
        return 0
    print(f"  ✘ {provider} probe FAILED — {result.detail or result.reason}")
    return 1


def _resolve_api_key_for(provider: str) -> str:
    """Resolve an API key for ``provider`` from vault → env. Vault
    takes precedence (matches runtime behaviour)."""
    import os
    from typing import cast

    env_map = {
        "deepgram": ("DEEPGRAM_API_KEY",),
        "elevenlabs": ("ELEVENLABS_API_KEY",),
        "cartesia": ("CARTESIA_API_KEY",),
        "openai_whisper": ("OPENAI_API_KEY",),
        "openai_tts": ("OPENAI_API_KEY",),
        "openai_realtime": ("OPENAI_API_KEY",),
        "groq_whisper": ("GROQ_API_KEY",),
        "gemini_live": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    }
    keys = env_map.get(provider, ())
    try:
        from security.vault import get_vault
        vault = get_vault()
        for k in keys:
            v = vault.get_credential(k)
            if v:
                return cast(str, v)
    except Exception:
        pass
    for k in keys:
        v = os.environ.get(k)
        if v:
            return v
    return ""
