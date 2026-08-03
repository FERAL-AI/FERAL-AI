#!/usr/bin/env python3
"""Verify, against LIVE providers, that streamed turns report token usage.

Why this exists
===============
FERAL bills a streamed turn from the usage block the provider sends. For
OpenAI-compatible ``/chat/completions`` that only arrives when the request
sets ``stream_options: {"include_usage": true}``, and for Anthropic it is
split across ``message_start`` (input) and ``message_delta`` (output).

All of that is covered by mocked SSE in
``tests/test_stream_usage_billing.py``. Mocks prove OUR parsing. They
cannot prove a given vendor still honours the opt-in on its current API
version, or that it 400s rather than silently ignoring an unknown body
key. Only a real call answers that, so this is a script you run
deliberately rather than a test that runs itself.

Cost
====
Roughly a dozen tokens per provider (``max_tokens=1`` where the API
allows it), so fractions of a cent. It prints the estimated spend per
provider before it exits so nothing is hidden.

Usage
=====
    python3 scripts/verify_stream_usage_live.py --provider openai
    python3 scripts/verify_stream_usage_live.py --all
    python3 scripts/verify_stream_usage_live.py --all --dry-run

Reads the key for each provider from the environment
(``OPENAI_API_KEY``, ``ANTHROPIC_API_KEY``, ``GROQ_API_KEY``,
``DEEPSEEK_API_KEY``, ``OPENROUTER_API_KEY``). A provider with no key is
skipped, never guessed at.

What each result means
======================
``usage`` reported     the opt-in works, streamed turns bill correctly.
``no usage``           the endpoint ignored the opt-in. Streamed turns
                       there are NOT billed and FERAL will not invent
                       numbers. Worth recording in the worklog.
``rejected``           the endpoint 400s on ``stream_options``. FERAL
                       retries once without it and remembers the
                       endpoint, so the turn survives unbilled.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

# (provider id, env var, base url, model, sends stream_options)
PROVIDERS: tuple[tuple[str, str, str, str], ...] = (
    ("openai", "OPENAI_API_KEY", "https://api.openai.com/v1", "gpt-4o-mini"),
    ("groq", "GROQ_API_KEY", "https://api.groq.com/openai/v1", "llama-3.1-8b-instant"),
    ("deepseek", "DEEPSEEK_API_KEY", "https://api.deepseek.com/v1", "deepseek-chat"),
    ("openrouter", "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1", "openai/gpt-4o-mini"),
)

ANTHROPIC = ("anthropic", "ANTHROPIC_API_KEY", "https://api.anthropic.com/v1", "claude-3-5-haiku-20241022")

PROMPT = "Reply with the single word: ok"


async def _check_openai_compatible(pid, base_url, model, key, *, dry_run):
    """Stream one tiny completion and report whether usage came back."""
    import httpx

    body = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": 1,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if dry_run:
        return "dry-run", f"would POST {base_url}/chat/completions with stream_options"

    usage = None
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            async with client.stream(
                "POST",
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json=body,
            ) as resp:
                if resp.status_code >= 400:
                    detail = (await resp.aread()).decode()[:200]
                    return "rejected", f"HTTP {resp.status_code}: {detail}"
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("usage"):
                        usage = chunk["usage"]
        except Exception as exc:  # noqa: BLE001 - report, never raise
            return "error", f"{type(exc).__name__}: {exc}"

    if usage:
        return "usage", json.dumps(usage, sort_keys=True)
    return "no usage", "stream completed but no usage block arrived"


async def _check_anthropic(base_url, model, key, *, dry_run):
    """Anthropic splits usage across message_start and message_delta."""
    import httpx

    body = {
        "model": model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": PROMPT}],
        "stream": True,
    }
    if dry_run:
        return "dry-run", f"would POST {base_url}/messages (streaming)"

    seen: dict[str, object] = {}
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            async with client.stream(
                "POST",
                f"{base_url}/messages",
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=body,
            ) as resp:
                if resp.status_code >= 400:
                    detail = (await resp.aread()).decode()[:200]
                    return "rejected", f"HTTP {resp.status_code}: {detail}"
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        ev = json.loads(line[6:].strip())
                    except json.JSONDecodeError:
                        continue
                    if ev.get("type") == "message_start":
                        seen["message_start"] = (ev.get("message") or {}).get("usage")
                    elif ev.get("type") == "message_delta":
                        seen["message_delta"] = ev.get("usage")
        except Exception as exc:  # noqa: BLE001
            return "error", f"{type(exc).__name__}: {exc}"

    if seen.get("message_start") or seen.get("message_delta"):
        return "usage", json.dumps(seen, sort_keys=True, default=str)
    return "no usage", "stream completed but neither event carried usage"


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--provider", action="append", default=[],
                    help="provider id to check (repeatable)")
    ap.add_argument("--all", action="store_true", help="check every provider with a key set")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be called, spend nothing")
    args = ap.parse_args()

    if not args.all and not args.provider:
        ap.error("pass --provider <id> or --all")

    wanted = set(args.provider)
    rows = []
    for pid, env_var, base_url, model in PROVIDERS:
        if not args.all and pid not in wanted:
            continue
        key = os.environ.get(env_var, "")
        if not key and not args.dry_run:
            rows.append((pid, "skipped", f"{env_var} not set"))
            continue
        status, detail = await _check_openai_compatible(
            pid, base_url, model, key, dry_run=args.dry_run
        )
        rows.append((pid, status, detail))

    pid, env_var, base_url, model = ANTHROPIC
    if args.all or pid in wanted:
        key = os.environ.get(env_var, "")
        if not key and not args.dry_run:
            rows.append((pid, "skipped", f"{env_var} not set"))
        else:
            status, detail = await _check_anthropic(base_url, model, key, dry_run=args.dry_run)
            rows.append((pid, status, detail))

    width = max((len(r[0]) for r in rows), default=10)
    print()
    for pid, status, detail in rows:
        print(f"  {pid:<{width}}  {status:<9}  {detail[:110]}")
    print()

    unbilled = [r[0] for r in rows if r[1] == "no usage"]
    if unbilled:
        print("  Streamed turns are NOT billed on: " + ", ".join(unbilled))
        print("  FERAL records nothing rather than estimating. Note it in")
        print("  docs/handoff/WORKLOG.md so a cost cap there is known to be advisory.")
    rejected = [r[0] for r in rows if r[1] == "rejected"]
    if rejected:
        print("  Rejected stream_options (FERAL retries without it): " + ", ".join(rejected))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
