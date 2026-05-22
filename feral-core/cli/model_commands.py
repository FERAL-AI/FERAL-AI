"""``feral models`` — list / test / set the LLM model picker.

Wraps Wave 2 Lane 09's ``LLMProvider`` and the long-standing
``providers.catalog.ProviderCatalog`` so the CLI matches what the
WebUI Settings → LLM picker shows. ``list`` is pure-local: it reads
each provider's catalogue (cached models on disk; live refresh when
``--live`` is passed). ``test`` and ``set`` write to ``settings.json``
through the same path the wizard uses.

Usage
-----

    feral models list
    feral models list --provider openai --live
    feral models test --provider anthropic --model claude-opus-4-7
    feral models set --provider openai --model gpt-5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Optional

from cli import ui_kit
from config.loader import feral_home


# ─────────────────────────────────────────────────────────────────────
# argparse registration
# ─────────────────────────────────────────────────────────────────────


def register_models_subparser(sub: "argparse._SubParsersAction") -> None:
    """Register ``feral models {list,test,set}`` under ``feral``."""
    models_p = sub.add_parser(
        "models",
        help="List, test, and set the active LLM model per provider.",
    )
    models_sub = models_p.add_subparsers(dest="action")

    list_p = models_sub.add_parser(
        "list",
        help="List models for one or every configured provider.",
    )
    list_p.add_argument(
        "--provider", default="",
        help="Provider id (openai / anthropic / gemini / ...). "
             "Empty = list every configured provider.",
    )
    list_p.add_argument(
        "--live", action="store_true",
        help="Force a live refresh from the provider's /models endpoint.",
    )
    list_p.add_argument(
        "--test", action="store_true",
        help="After listing, probe the provider once to confirm the key works.",
    )

    test_p = models_sub.add_parser(
        "test",
        help="Run a one-token chat through a specific provider+model.",
    )
    test_p.add_argument("--provider", required=True, help="Provider id")
    test_p.add_argument("--model", default="", help="Model id (default: provider's default_model)")

    set_p = models_sub.add_parser(
        "set",
        help="Persist <provider>:<model> as the chat default in settings.json.",
    )
    set_p.add_argument("--provider", required=True, help="Provider id")
    set_p.add_argument("--model", required=True, help="Model id to make default")


def dispatch_models_subcommand(args) -> int:
    action = getattr(args, "action", None) or "list"
    if action == "list":
        return cmd_models_list(
            provider=getattr(args, "provider", "") or "",
            live=bool(getattr(args, "live", False)),
            run_probe=bool(getattr(args, "test", False)),
        )
    if action == "test":
        return cmd_models_test(
            provider=getattr(args, "provider", "") or "",
            model=getattr(args, "model", "") or "",
        )
    if action == "set":
        return cmd_models_set(
            provider=getattr(args, "provider", "") or "",
            model=getattr(args, "model", "") or "",
        )
    print(f"Unknown action: {action}. Try one of: list, test, set.")
    return 2


# ─────────────────────────────────────────────────────────────────────
# list
# ─────────────────────────────────────────────────────────────────────


def _build_catalog():
    """Build a fresh ProviderCatalog wired to the user's vault for keys.

    The catalog uses cached model lists on disk (~/.feral/.cache/...),
    so this stays pure-local unless the caller asks for ``--live``.
    """
    from providers.catalog import ProviderCatalog
    return ProviderCatalog()


def cmd_models_list(*, provider: str = "", live: bool = False, run_probe: bool = False) -> int:
    """List models for one or every configured provider."""
    try:
        catalog = _build_catalog()
    except Exception as exc:
        print(f"  Provider catalog unavailable: {exc}")
        return 1

    descriptors = catalog.list_providers()
    targets = []
    if provider:
        pid = catalog.resolve_alias(provider) or provider
        desc = catalog.get_descriptor(pid)
        if desc is None:
            print(f"  Unknown provider: {provider!r}")
            print(f"  Known: {', '.join(d.provider_id for d in descriptors)}")
            return 2
        targets = [desc]
    else:
        targets = descriptors

    try:
        from rich.console import Console
        from rich.table import Table
        console = Console()
    except ImportError:
        console = None

    async def _list_one(desc):
        try:
            cached = await catalog.list_models(desc.provider_id, live=live, force=live)
            return cached
        except Exception as exc:
            return exc

    async def _list_all():
        return await asyncio.gather(*[_list_one(d) for d in targets])

    results = asyncio.run(_list_all())

    for desc, res in zip(targets, results):
        header = f"{desc.display_name} ({desc.provider_id})"
        if console is not None:
            console.print()
            console.print(f"[bold]{header}[/bold]   default: [dim]{desc.default_model}[/dim]")
        else:
            print(f"\n  {header}   (default: {desc.default_model})")

        if isinstance(res, Exception):
            msg = f"  ✘ Could not list models: {res}"
            if console is not None:
                console.print(f"[red]{msg}[/red]")
            else:
                print(msg)
            continue

        models = list(res.models)
        if not models:
            note = "  ℹ No models cached. Run with --live to fetch from the provider."
            if console is not None:
                console.print(f"[cyan]{note}[/cyan]")
            else:
                print(note)
            continue

        if console is not None:
            for m in models[:50]:
                console.print(f"  - {m}")
            if len(models) > 50:
                console.print(f"  …and {len(models) - 50} more")
        else:
            for m in models[:50]:
                print(f"  - {m}")

        if res.warning:
            warn = f"  ⚠ {res.warning}"
            if console is not None:
                console.print(f"[yellow]{warn}[/yellow]")
            else:
                print(warn)

    if run_probe:
        # Run probe per target to render a green/red verdict in addition
        # to the model list. Useful for "this key works AND we can list
        # models" sanity checks.
        from security.probe import probe

        async def _probe_all():
            return await asyncio.gather(*[probe(d.provider_id) for d in targets])

        probe_results = asyncio.run(_probe_all())
        print()
        for desc, pr in zip(targets, probe_results):
            mark = "✔" if pr.ok else "✘"
            print(f"  {mark} {desc.provider_id}  probe: {pr.detail or pr.reason}")

    return 0


# ─────────────────────────────────────────────────────────────────────
# test — one-token chat round-trip
# ─────────────────────────────────────────────────────────────────────


def cmd_models_test(*, provider: str, model: str = "") -> int:
    """Send a single-token chat to ``provider``/``model`` and print
    the model's reply. Confirms keys + model + base_url + pricing all
    line up end-to-end without booting the brain."""
    try:
        from providers.catalog import ProviderCatalog
    except Exception as exc:
        print(f"  Provider catalog unavailable: {exc}")
        return 1

    if not provider:
        print("  --provider is required.")
        return 2

    catalog = ProviderCatalog()
    pid = catalog.resolve_alias(provider) or provider
    desc = catalog.get_descriptor(pid)
    if desc is None:
        print(f"  Unknown provider: {provider!r}")
        return 2

    if not model:
        model = desc.default_model

    adapter = catalog.get_adapter(pid)
    if adapter is None:
        print(f"  No adapter registered for {pid!r} — provider catalog may be misconfigured.")
        return 1

    chat = getattr(adapter, "chat", None)
    if chat is None:
        print(f"  Adapter for {pid!r} does not implement chat() — try `feral models list` instead.")
        return 1

    print(f"  Sending one-token probe to {pid}/{model}…")

    async def _go():
        return await chat(
            model=model,
            messages=[{"role": "user", "content": "Reply with the single word 'ok'."}],
            max_tokens=4,
            temperature=0.0,
        )

    try:
        result = asyncio.run(_go())
    except Exception as exc:
        print(f"  ✘ Chat round-trip failed: {exc}")
        return 1

    text = ""
    if isinstance(result, dict):
        text = result.get("content") or result.get("text") or ""
    print()
    print(f"  ✔ {pid}/{model} responded: {text!r}")
    return 0


# ─────────────────────────────────────────────────────────────────────
# set — persist (provider, model) as default
# ─────────────────────────────────────────────────────────────────────


def cmd_models_set(*, provider: str, model: str) -> int:
    """Persist ``llm.provider`` + ``llm.model`` in ``settings.json``.

    The brain re-reads settings on next start (or on the runtime hot-
    reload path); this command does NOT restart the brain. Same path
    the wizard uses, so values land in the canonical location.
    """
    if not provider or not model:
        print("  Both --provider and --model are required.")
        return 2

    settings_path = feral_home() / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings: dict = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except Exception as exc:
            print(f"  Existing settings.json is invalid: {exc}")
            return 1

    settings.setdefault("llm", {})
    settings["llm"]["provider"] = provider
    settings["llm"]["model"] = model
    settings_path.write_text(json.dumps(settings, indent=2, sort_keys=True))

    print(f"  Set llm.provider={provider}, llm.model={model} in {settings_path}.")
    print("  Restart the brain (`feral restart`) for the change to take effect.")
    return 0
