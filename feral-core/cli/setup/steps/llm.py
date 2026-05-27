"""Provider + model selection step.

Reads the live ProviderCatalog, renders a side-by-side "ready vs
needs-key vs unreachable" table, accepts fuzzy provider names, lets
the user type any model id even if it's newer than the bundled
catalog.
"""

from __future__ import annotations

import os
from typing import Iterable

from providers.catalog import (
    ProviderCatalog,
    ProviderStatus,
    get_shared_catalog,
)

from cli import ui_kit

from ..helpers import (
    STATUS_NEEDS_KEY,
    STATUS_READY,
    STATUS_UNREACHABLE,
    Option,
    ask_choice,
    ask_text,
    confirm,
    get_console,
    render_provider_table,
    _RICH_AVAILABLE,
)
from ..state import WizardState


async def run_provider_step(state: WizardState) -> None:
    console = get_console()
    catalog = _catalog(state)

    # Probe every provider so the table shows reachable state. This is
    # parallel under a single refresh_all but we call probe() per-id so
    # the network results can differ (Ollama reachable, OpenAI not).
    statuses = await _probe_all(catalog)
    options = _build_options(catalog, statuses)
    extra_notes = _build_notes(catalog, statuses)

    # The state-machine prints the brand-coloured "Step N of M · LLM
    # Provider" header before this step runs, so we don't repeat it.
    if _RICH_AVAILABLE:
        console.print(
            "Pick the provider you want FERAL's brain to talk to. Local providers "
            "show [green]ready[/] when detected; cloud providers show "
            "[yellow]needs API key[/] until you enter one — you can still pick them "
            "and add the key next."
        )

    # On the typed-fallback path (off-tty / no InquirerPy) the picker
    # cannot show inline status badges, so the wide Rich table still
    # earns its keep. On the interactive path the picker draws each
    # option with the same status badge inline (see helpers._option_badge)
    # — rendering the table separately would dump the providers twice.
    if not (ui_kit.is_inquirer_available() and ui_kit.is_interactive()):
        render_provider_table("Available providers", options, extra_columns=extra_notes)

    default_id = state.get_setting("llm", "provider") or _default_choice(options)
    chosen = ask_choice("Choose a provider", options, default=default_id)
    state.set_setting("llm", "provider", chosen.id)

    desc = catalog.get_descriptor(chosen.id)
    if desc is None:
        return

    if desc.default_base_url and not state.get_setting("llm", "base_url"):
        state.set_setting("llm", "base_url", desc.default_base_url)

    # Cloud providers: capture API key unless one is already present
    # (either in env or in the vault-backed credentials.json we loaded).
    if desc.requires_api_key:
        env_var = desc.credential_env_var
        existing = (
            os.environ.get(env_var, "")
            or state.credentials.get(env_var, "")
        )
        if existing:
            keep = confirm(
                f"  Use existing {env_var} from your environment / credentials?",
                default=True,
            )
            if not keep:
                existing = ""
        if not existing:
            key = ask_text(
                f"  Enter your {desc.display_name} API key",
                allow_empty=False,
                secret=True,
            )
            state.set_credential(env_var, key)
            os.environ[env_var] = key
            catalog.configure(chosen.id, api_key=key)

        # Re-probe to flip the status from needs_key → ready.
        updated = await catalog.probe(chosen.id)
        if updated.reachable:
            console.print(f"  [green]✓[/] {desc.display_name} reachable")
        else:
            msg = updated.error or "unreachable"
            console.print(f"  [yellow]note:[/] probe said: {msg} — you can continue and re-probe later.")
    elif desc.provider_id == "ollama":
        status = statuses.get(desc.provider_id)
        if not (status and status.reachable):
            await _handle_ollama_unreachable(console)
        else:
            # Reachable but maybe zero models — offer to pull one.
            cached = await catalog.list_models(chosen.id, live=True, force=True)
            if not cached.models:
                await _handle_ollama_no_models(catalog, console)
    elif desc.provider_id == "lmstudio":
        status = statuses.get(desc.provider_id)
        if not (status and status.reachable):
            _show_lmstudio_instructions(console)
        else:
            cached = await catalog.list_models(chosen.id, live=True, force=True)
            if not cached.models:
                _show_lmstudio_no_model(console)


async def run_model_step(state: WizardState) -> None:
    console = get_console()
    catalog = _catalog(state)
    provider_id = state.get_setting("llm", "provider")
    if not provider_id:
        return
    desc = catalog.get_descriptor(provider_id)
    if desc is None:
        return

    try:
        cached = await catalog.list_models(provider_id, live=True, force=True)
    except Exception:
        cached = await catalog.list_models(provider_id, live=False)
    raw_models = list(cached.models)

    # v2026.5.23 — split chat-completions-safe models from the
    # responses-only Pro family. Without this, the picker shows
    # `gpt-5.5-pro` / `o3-pro` / deep-research as plain chat choices,
    # but on a fresh install the LLM provider didn't have the
    # /v1/responses adapter wired (now it does, v2026.5.23+). We keep
    # the disclosure regardless so the operator knows which models
    # take longer / cost more / behave differently before they pick.
    try:
        from providers.model_classes import classify_endpoint
        chat_models = [m for m in raw_models if classify_endpoint(provider_id, m) == "chat_completions"]
        responses_models = [m for m in raw_models if classify_endpoint(provider_id, m) == "responses"]
    except Exception:
        chat_models = raw_models
        responses_models = []
    models = chat_models

    # Header is owned by the state-machine step indicator now; we just
    # surface the discovered-count summary.
    if _RICH_AVAILABLE:
        extra = ""
        if responses_models:
            extra = (
                f" [dim](+ {len(responses_models)} responses-API models "
                f"hidden by default; type a custom id to use one)[/]"
            )
        console.print(
            f"Discovered [bold]{len(models)}[/] chat models for "
            f"[bold]{desc.display_name}[/] [dim](source: {cached.source})[/]{extra}."
        )

    default = state.get_setting("llm", "model") or desc.default_model or (models[0] if models else "")

    # Interactive arrow-key + fuzzy filter when InquirerPy is available
    # and the shell is a real TTY. This is what the operator gets in
    # day-to-day use; it scales to 100+ model ids per provider without
    # the unreadable scrollback dump the typed picker produced.
    if models and ui_kit.is_inquirer_available() and ui_kit.is_interactive():
        # Append a "type a custom id" sentinel so users can pick a
        # provider model that's newer than the cache (e.g. a freshly
        # released gpt-N).
        custom_sentinel = "__feral_custom_model__"
        choices: list[dict] = [{"name": m, "value": m} for m in models]
        # Sentinel is appended LAST so a normal Enter on the first
        # filtered live-catalog row never accidentally lands on the
        # custom-id row. The label is bracketed so a fuzzy filter
        # built from real model-id substrings (gpt, claude, llama)
        # won't surface the sentinel above the real choices.
        choices.append({
            "name": "[custom] type a model id not in the live catalog",
            "value": custom_sentinel,
        })
        try:
            # v2026.5.28 — fuzzy_pick (enter-on-cursor-position) instead
            # of fuzzy_select (space-to-mark + enter-to-confirm). The
            # mark-then-confirm UX confused every first-time operator
            # who typed a filter, hit enter, and got "press space to
            # mark exactly one option, then enter" — the operator's
            # screenshot of the model picker showing exactly that. Model
            # selection is the classic "I want this one, get me out of
            # this menu" interaction, so direct-pick is the right UX.
            picked = ui_kit.fuzzy_pick(
                f"Which model for {desc.display_name}?",
                choices,
                default=default if default in models else None,
            )
        except KeyboardInterrupt:
            from ..helpers import QuitNavigation
            raise QuitNavigation()
        # Defensive unwrap — ``fuzzy_pick`` already normalises away
        # InquirerPy ``Choice`` objects to their ``.value``, but a
        # caller-supplied Choice would still bypass that if the
        # InquirerPy path failed and the fallback path returned a
        # dict / Choice. Cheap to be explicit here.
        picked = getattr(picked, "value", picked)
        if picked == custom_sentinel:
            picked = ask_text(
                "Custom model id",
                default=default,
                allow_empty=False,
            )
        state.set_setting("llm", "model", picked)
        return

    # Typed fallback path — also the path pytest exercises (the suite
    # monkeypatches ``ask_text`` directly). Show the first 25 ids as a
    # quick numeric picker, then accept any free-text id.
    if models:
        preview = models[:25]
        for i, model in enumerate(preview, start=1):
            console.print(f"  {i:>3}. {model}")
        if len(models) > len(preview):
            console.print(f"  ... and {len(models) - len(preview)} more.")
    else:
        console.print(
            "  (Could not fetch a live list — type the exact model name for this provider.)"
        )

    while True:
        raw = ask_text(
            "Which model? (paste the id or pick a number above)",
            default=default,
            allow_empty=False,
        )
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(models):
                raw = models[idx]
            else:
                console.print("  number out of range")
                continue
        state.set_setting("llm", "model", raw)
        break


# ----------------------------------------------------------------------
# Internals
# ----------------------------------------------------------------------


def _catalog(state: WizardState) -> ProviderCatalog:
    # Cache on the state so every step in a single run shares one catalog.
    cached = getattr(state, "_catalog", None)
    if cached is not None:
        return cached
    cat = get_shared_catalog()
    setattr(state, "_catalog", cat)
    return cat


async def _probe_all(catalog: ProviderCatalog) -> dict[str, ProviderStatus]:
    out: dict[str, ProviderStatus] = {}
    for desc in catalog.list_providers():
        try:
            out[desc.provider_id] = await catalog.probe(desc.provider_id)
        except Exception:
            out[desc.provider_id] = catalog.status_for(desc.provider_id)
    return out


def _build_options(
    catalog: ProviderCatalog, statuses: dict[str, ProviderStatus]
) -> list[Option]:
    options: list[Option] = []
    for desc in catalog.list_providers():
        status = statuses.get(desc.provider_id)
        if desc.supports_local:
            if status and status.reachable:
                ui_status = STATUS_READY
            else:
                ui_status = STATUS_UNREACHABLE
        else:
            if status and status.configured and status.reachable:
                ui_status = STATUS_READY
            elif desc.requires_api_key and not (status and status.configured):
                ui_status = STATUS_NEEDS_KEY
            else:
                ui_status = STATUS_READY if (status and status.reachable) else STATUS_UNREACHABLE
        options.append(
            Option(
                id=desc.provider_id,
                label=desc.display_name,
                aliases=desc.aliases,
                status=ui_status,
                hint=desc.notes,
            )
        )
    return options


def _build_notes(
    catalog: ProviderCatalog, statuses: dict[str, ProviderStatus]
) -> dict[str, dict[str, str]]:
    notes: dict[str, dict[str, str]] = {}
    for desc in catalog.list_providers():
        status = statuses.get(desc.provider_id)
        snippets: list[str] = []
        if desc.supports_local:
            snippets.append(f"local @ {desc.default_base_url}")
        elif desc.credential_env_var:
            snippets.append(f"env: {desc.credential_env_var}")
        if status and status.error:
            snippets.append(f"err: {status.error[:40]}")
        notes[desc.provider_id] = {"note": " · ".join(snippets)}
    return notes


def _default_choice(options: Iterable[Option]) -> str:
    # Prefer a local-ready provider; else the first non-unreachable cloud.
    ordered = list(options)
    for opt in ordered:
        if opt.status == STATUS_READY and "local" in opt.label.lower():
            return opt.id
    for opt in ordered:
        if opt.status == STATUS_READY:
            return opt.id
    for opt in ordered:
        if opt.status == STATUS_NEEDS_KEY:
            return opt.id
    return ordered[0].id if ordered else ""


# ----------------------------------------------------------------------
# Local provider assistance
# ----------------------------------------------------------------------


async def _handle_ollama_unreachable(console) -> None:
    from ..local_providers import OLLAMA_INSTALL_HINT, ollama_cli_installed

    console.print()
    console.print(
        "[yellow]Ollama isn't responding at http://localhost:11434.[/]"
        if _RICH_AVAILABLE else
        "Ollama isn't responding at http://localhost:11434."
    )
    if not ollama_cli_installed():
        for line in OLLAMA_INSTALL_HINT.splitlines():
            console.print(f"  {line}")
        return
    console.print("  `ollama` is on your PATH but the server isn't serving.")
    console.print("  Start it with: ollama serve")
    console.print("  Then re-run `feral setup` to continue.")


async def _handle_ollama_no_models(catalog, console) -> None:
    from ..helpers import ask_text, confirm
    from ..local_providers import STARTER_OLLAMA_MODELS, ollama_pull_model

    console.print("  Ollama is running but no models are installed yet.")
    if not confirm("  Pull a starter model now?", default=True):
        return
    options_text = ", ".join(STARTER_OLLAMA_MODELS)
    console.print(f"  Suggested: {options_text}")
    choice = ask_text(
        "  Model to pull",
        default=STARTER_OLLAMA_MODELS[0],
        allow_empty=False,
    )
    console.print(f"  Running `ollama pull {choice}` — streaming progress...")
    try:
        code = await ollama_pull_model(choice, on_line=lambda line: console.print(f"    {line}"))
    except Exception as exc:
        console.print(f"  [red]pull failed:[/] {exc}" if _RICH_AVAILABLE else f"  pull failed: {exc}")
        return
    if code == 0:
        console.print(f"  [green]✓[/] pulled {choice}" if _RICH_AVAILABLE else f"  pulled {choice}")
        # Refresh cache so the model step sees it.
        try:
            await catalog.list_models("ollama", live=True, force=True)
        except Exception:
            pass
    else:
        console.print(f"  [red]ollama pull exited with code {code}[/]" if _RICH_AVAILABLE else
                      f"  ollama pull exited with code {code}")


def _show_lmstudio_instructions(console) -> None:
    from ..local_providers import LMSTUDIO_INSTRUCTIONS

    console.print()
    for line in LMSTUDIO_INSTRUCTIONS.splitlines():
        console.print(f"  {line}")


def _show_lmstudio_no_model(console) -> None:
    console.print("  LM Studio is running but no model is loaded.")
    console.print("  Open LM Studio → pick a model → Start the local server.")
    console.print("  Then re-run `feral setup`.")
