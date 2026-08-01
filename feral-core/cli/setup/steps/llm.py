"""Provider + model selection step.

Reads the live ProviderCatalog, renders a side-by-side "ready vs
needs-key vs unreachable" table, accepts fuzzy provider names, lets
the user type any model id even if it's newer than the bundled
catalog.
"""

from __future__ import annotations

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
    existing_provider_key,
    get_console,
    render_provider_table,
    _RICH_AVAILABLE,
)
from ..state import WizardState


# How many times the model prompt may be re-asked after a failed smoke
# test before the step keeps whatever was typed and moves on. A wizard
# prompt must always terminate.
_MAX_MODEL_ATTEMPTS = 3


def _selectable_providers(catalog: ProviderCatalog) -> list:
    """Catalog descriptors the runtime can actually dial.

    ``ProviderCatalog`` lists every descriptor the UI can *render*,
    including ones with no OpenAI-compatible runtime adapter in this
    build (``bedrock``, ``together``, ``fireworks``). Offering those in
    the picker let setup "succeed" and then fail on the operator's very
    first chat turn. ``POST /api/llm/provider`` has refused them since
    v2026.5.x; this is the same gate for the CLI.
    """
    from agents.llm_provider import is_supported_catalog_provider

    return [
        desc for desc in catalog.list_providers()
        if is_supported_catalog_provider(desc.provider_id)
    ]


def _unsupported_provider_ids(catalog: ProviderCatalog) -> list[str]:
    """Catalog ids the picker hides, for the one-line disclosure."""
    from agents.llm_provider import is_supported_catalog_provider

    return [
        desc.provider_id for desc in catalog.list_providers()
        if not is_supported_catalog_provider(desc.provider_id)
    ]


async def run_provider_step(state: WizardState) -> None:
    console = get_console()
    catalog = _catalog(state)

    # Probe every provider so the table shows reachable state. This is
    # parallel under a single refresh_all but we call probe() per-id so
    # the network results can differ (Ollama reachable, OpenAI not).
    statuses = await _probe_all(catalog)
    options = _build_options(catalog, statuses, state)
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
        hidden = _unsupported_provider_ids(catalog)
        if hidden:
            console.print(
                f"[dim]Not listed: {', '.join(hidden)} — in the catalog but "
                f"without a runtime adapter in this build. Point "
                f"`llm.base_url` at an OpenAI-compatible gateway to use "
                f"them.[/]"
            )

    # On the typed-fallback path (off-tty / no InquirerPy) the picker
    # cannot show inline status badges, so the wide Rich table still
    # earns its keep. On the interactive path the picker draws each
    # option with the same status badge inline (see helpers._option_badge)
    # — rendering the table separately would dump the providers twice.
    if not (ui_kit.is_inquirer_available() and ui_kit.is_interactive()):
        render_provider_table("Available providers", options, extra_columns=extra_notes)

    previous_id = state.get_setting("llm", "provider") or ""
    default_id = previous_id or _default_choice(options)
    chosen = ask_choice("Choose a provider", options, default=default_id)
    state.set_setting("llm", "provider", chosen.id)

    desc = catalog.get_descriptor(chosen.id)
    if desc is None:
        return

    _apply_base_url(state, desc, provider_changed=(chosen.id != previous_id))

    # Cloud providers: detect any existing key (labeled vault entry,
    # legacy default-namespace entry, or process env var) so we can
    # show "key already configured — keep / replace / add another /
    # remove" instead of the misleading "needs a key" prompt the
    # operator hit when a labeled key was already stored. The
    # detection consults ``security.vault_keys`` first (post-v2026.5.42
    # source of truth) and falls back to the legacy default-namespace
    # for installs that pre-date the labeled-key migration.
    if desc.requires_api_key:
        env_var = desc.credential_env_var
        await _configure_provider_key(
            state=state,
            catalog=catalog,
            provider_id=chosen.id,
            env_var=env_var,
            display_name=desc.display_name,
            console=console,
        )

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


def _apply_base_url(state: WizardState, desc, *, provider_changed: bool) -> None:
    """Own ``llm.base_url`` for the chosen provider.

    The old rule was "write the descriptor default only when the slot
    is empty", which made the value sticky across provider changes. Pick
    Ollama (base_url becomes localhost:11434/v1), navigate back, pick
    OpenAI, and the OpenAI branch of ``agents/llm_provider`` does
    ``self.base_url = self.base_url or <default>``, so the stale local URL
    survives and every OpenAI request is sent to the Ollama daemon. The
    same thing happens re-running ``feral setup`` on an install that
    previously used a local provider.

    So: when the provider changed, the new provider owns the slot
    outright, and a provider with no descriptor default clears it (an
    empty slot is what lets the runtime fill in its own default).
    When the provider did NOT change we leave any operator-customised
    value alone and only fill an empty slot.
    """
    if provider_changed:
        state.set_setting("llm", "base_url", desc.default_base_url or "")
        return
    if desc.default_base_url and not state.get_setting("llm", "base_url"):
        state.set_setting("llm", "base_url", desc.default_base_url)


def _demote_hosted_local_models(provider_id: str, models: list[str]) -> list[str]:
    """Sink Ollama Cloud (``:cloud``) model ids below the local ones.

    ``ollama list`` reports Ollama Cloud models alongside genuinely
    local ones, and the picker defaults to the first entry. On a
    machine where a ``:cloud`` id sorted first, the wizard's own
    default selected a model that needs an ollama.com key: the LLM step
    never prompts for one (the provider is marked
    ``requires_api_key=False``) and the smoke test exempts local
    providers, so setup completed clean and the first chat turn came
    back 403. Local models are what "Ollama (local)" promises, so they
    lead. Order within each group is preserved.
    """
    if provider_id != "ollama":
        return list(models)
    local = [m for m in models if not str(m).endswith(":cloud")]
    hosted = [m for m in models if str(m).endswith(":cloud")]
    return local + hosted


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
    models = _demote_hosted_local_models(provider_id, chat_models)

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
        await _smoke_test_model(catalog, provider_id, picked, console, state)
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

    # Bounded. This loop used to be ``while True``: when the smoke test
    # failed for a reason retyping the model id can never fix (no API
    # key configured for the provider), "Pick a different model?"
    # defaulted to yes and the enter-through-defaults path re-asked
    # forever until stdin ran out. Setup must always terminate.
    for attempt in range(_MAX_MODEL_ATTEMPTS):
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
        if await _smoke_test_model(catalog, provider_id, raw, console, state):
            return
        if attempt == _MAX_MODEL_ATTEMPTS - 1:
            console.print(
                "  [yellow]Keeping the model as typed.[/] Re-run it later "
                "with `feral setup --from-step llm_model`."
                if _RICH_AVAILABLE else
                "  Keeping the model as typed. Re-run it later with "
                "`feral setup --from-step llm_model`."
            )
            return
        if not confirm("  Pick a different model?", default=True):
            return


async def _smoke_test_model(
    catalog,
    provider_id: str,
    model: str,
    console,
    state: "WizardState | None" = None,
) -> bool:
    """Send one throwaway token through provider+model. Returns ok.

    A mistyped or newer-than-catalog model id used to be accepted in
    silence — the wizard printed "You're set up" and the operator's
    first chat turn died on a 404 model_not_found. This is the same
    round-trip ``feral models test`` performs, run once at the point
    the id is chosen.

    Local providers are exempt: pulling a model into Ollama can be a
    multi-GB download that the LLM step already walked the operator
    through, and a cold model here would stall the wizard.

    A key-requiring provider with NO key configured is exempt too, and
    reports as passing. The test can only distinguish a good model id
    from a bad one when it can authenticate; without a key every id
    fails identically with "provider has no api_key configured". The
    caller treats a failure as "retype the model id", which cannot fix
    a missing key, so reporting failure here re-asked the operator a
    question no answer of theirs could satisfy.
    """
    desc = catalog.get_descriptor(provider_id)
    if desc is not None and getattr(desc, "supports_local", False):
        return True

    if desc is not None and getattr(desc, "requires_api_key", False):
        secret, _source, _labels = existing_provider_key(
            provider_id, getattr(desc, "credential_env_var", "") or "", state,
        )
        if not secret:
            console.print(
                f"  [dim]No {provider_id} key configured, so the model id "
                f"cannot be verified from here. Add one with `feral key add "
                f"--provider {provider_id}`.[/]" if _RICH_AVAILABLE else
                f"  No {provider_id} key configured, so the model id cannot "
                f"be verified from here."
            )
            return True

    adapter = catalog.get_adapter(provider_id)
    chat = getattr(adapter, "chat", None) if adapter is not None else None
    if chat is None:
        console.print(
            f"  [dim]No chat adapter for {provider_id} — skipping the "
            f"model smoke test.[/]" if _RICH_AVAILABLE else
            f"  No chat adapter for {provider_id} — skipping the model "
            f"smoke test."
        )
        return True

    console.print(f"  Testing {provider_id}/{model} with a one-token request…")
    try:
        result = await chat(
            model=model,
            messages=[{"role": "user", "content": "Reply with the single word 'ok'."}],
            max_tokens=4,
            temperature=0.0,
        )
    except Exception as exc:
        console.print(
            f"  [red]✘[/] {provider_id}/{model} failed: {exc}"
            if _RICH_AVAILABLE else
            f"  ✘ {provider_id}/{model} failed: {exc}"
        )
        return False

    text = ""
    if isinstance(result, dict):
        text = result.get("content") or result.get("text") or ""
    console.print(
        f"  [green]✓[/] {provider_id}/{model} responded: {text.strip()!r}"
        if _RICH_AVAILABLE else
        f"  ✓ {provider_id}/{model} responded: {text.strip()!r}"
    )
    return True


# ----------------------------------------------------------------------
# Provider-key UX (Bug 2 — "key already exists" detection)
# ----------------------------------------------------------------------


def _warn_key_skipped(display_name: str, provider_id: str, console) -> None:
    """Tell the operator what skipping the key prompt costs them.

    Setup still completes, which is the point: an unfinishable wizard is
    worse than one that finishes with a provider that needs a key added
    later. The message names the command that adds it.
    """
    console.print(
        f"  [yellow]No {display_name} key stored.[/] Chat will not work "
        f"until you add one: [bold]feral key add --provider {provider_id}[/]"
        if _RICH_AVAILABLE else
        f"  No {display_name} key stored. Chat will not work until you run "
        f"`feral key add --provider {provider_id}`."
    )


async def _configure_provider_key(
    *,
    state: WizardState,
    catalog,
    provider_id: str,
    env_var: str,
    display_name: str,
    console,
) -> None:
    """Drive the "you already have a key / enter a new one" UX.

    When a key is already stored anywhere — labeled-key vault, the
    legacy default-namespace vault entry, or the process env — we
    show the masked value plus an action menu (Keep / Replace / Add
    another labeled key / Remove). We only fall through to the
    free-text "enter your API key" prompt when no key is found OR
    the operator explicitly chose Replace / Add.

    The messaging never says "needs a key" when one is present;
    that was the contradiction the operator hit (table said "needs
    API key", subsequent prompt asked "keep existing or add new?").
    """
    import os as _os
    from security.vault_keys import mask_key as _mask_key

    secret, source, labels = existing_provider_key(provider_id, env_var, state)

    if not secret:
        # No key anywhere → free-text prompt.
        #
        # This MUST accept an empty answer. It used to be
        # ``allow_empty=False``, and both ``helpers._ask_secret`` and
        # ``ui_kit.password`` loop forever on an empty value, so an
        # operator who reached this prompt with no key to hand had no
        # way out but Ctrl+C. On a keyless machine the picker's own
        # default provider lands here, which means pressing enter
        # through the wizard could never finish it.
        key = ask_text(
            f"  Enter your {display_name} API key (leave blank to skip)",
            allow_empty=True,
            secret=True,
        )
        if not (key or "").strip():
            _warn_key_skipped(display_name, provider_id, console)
            return
        _persist_new_key(
            state=state, catalog=catalog,
            provider_id=provider_id, env_var=env_var, key=key,
        )
        return

    # We have a key. Surface the masked value + source so the
    # operator sees what FERAL already has on file.
    masked = _mask_key(secret)
    if _RICH_AVAILABLE:
        source_label = {
            "vault_keys": "labeled key",
            "vault_legacy": "vault (default)",
            "env": f"env var {env_var}",
        }.get(source, source or "stored")
        if labels:
            active_label = next((e.label for e in labels if e.is_active), labels[0].label)
            console.print(
                f"  [green]✓[/] {display_name} key already configured: "
                f"[bold]{masked}[/]  [dim]· label: {active_label} · source: {source_label}[/]"
            )
        else:
            console.print(
                f"  [green]✓[/] {display_name} key already configured: "
                f"[bold]{masked}[/]  [dim]· source: {source_label}[/]"
            )
    else:
        console.print(
            f"  ✓ {display_name} key already configured: {masked}  (source: {source})"
        )

    action_opts = [
        Option(id="keep", label="Keep current key", status="ready"),
        Option(id="replace", label="Replace with a new key"),
        Option(id="add", label="Add another labeled key (prod/dev/…)"),
    ]
    if labels:
        action_opts.append(Option(id="remove", label="Remove an existing labeled key"))

    chosen_action = ask_choice(
        "  What would you like to do?",
        action_opts,
        default="keep",
    )

    if chosen_action.id == "keep":
        # Ensure env var + state.credentials are populated so downstream
        # steps (audio, voice_preflight) can read the key without a
        # second vault round-trip.
        if env_var:
            _os.environ[env_var] = secret
            state.set_credential(env_var, secret)
        try:
            catalog.configure(provider_id, api_key=secret)
        except Exception:
            pass
        return

    if chosen_action.id == "remove":
        await _remove_labeled_key(provider_id=provider_id, labels=labels, console=console)
        # Recurse — show the (now-updated) menu so the operator can
        # add a fresh key or pick a different stored label.
        await _configure_provider_key(
            state=state, catalog=catalog, provider_id=provider_id,
            env_var=env_var, display_name=display_name, console=console,
        )
        return

    # replace OR add → free-text prompt for a fresh key.
    label = "default" if chosen_action.id == "replace" else ""
    if chosen_action.id == "add":
        label = ask_text(
            "  Label for the new key (e.g. prod, dev, team-a)",
            default="prod",
            allow_empty=False,
        )
    new_key = ask_text(
        f"  Enter your {display_name} API key (leave blank to skip)",
        allow_empty=True,
        secret=True,
    )
    if not (new_key or "").strip():
        _warn_key_skipped(display_name, provider_id, console)
        return
    _persist_new_key(
        state=state, catalog=catalog,
        provider_id=provider_id, env_var=env_var,
        key=new_key, label=label or "default",
        set_active=(chosen_action.id == "replace"),
    )


def _persist_new_key(
    *,
    state: WizardState,
    catalog,
    provider_id: str,
    env_var: str,
    key: str,
    label: str = "default",
    set_active: bool = True,
) -> None:
    """Write a freshly-typed key to the labeled-key vault + legacy
    surfaces so every code path (chat, voice, REST) can resolve it."""
    import os as _os

    try:
        from security import vault_keys

        vault_keys.add_provider_key(
            provider_id, label, key, set_active=set_active,
        )
    except Exception:
        # The labeled-key write is best-effort; the legacy default
        # vault entry below is what unbreaks the wizard on systems
        # where the labeled-key namespace can't be written.
        pass
    if env_var:
        state.set_credential(env_var, key)
        _os.environ[env_var] = key
    try:
        catalog.configure(provider_id, api_key=key)
    except Exception:
        pass


async def _remove_labeled_key(*, provider_id: str, labels, console) -> None:
    """Render a picker over the operator's labeled keys + delete the
    chosen one. No-op when the labeled-key namespace is empty."""
    if not labels:
        console.print("  (no labeled keys to remove)")
        return
    opts = [
        Option(
            id=e.label,
            label=f"{e.label}  · {e.fingerprint}" + (" (active)" if e.is_active else ""),
        )
        for e in labels
    ]
    opts.append(Option(id="__cancel__", label="(cancel)"))
    pick = ask_choice("  Which labeled key should be removed?", opts, default=labels[0].label)
    if pick.id == "__cancel__":
        return
    try:
        from security import vault_keys

        vault_keys.remove_provider_key(provider_id, pick.id)
        console.print(f"  Removed {provider_id}:{pick.id}")
    except Exception as exc:
        console.print(f"  [yellow]Could not remove key:[/] {exc}")


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
    for desc in _selectable_providers(catalog):
        try:
            out[desc.provider_id] = await catalog.probe(desc.provider_id)
        except Exception:
            out[desc.provider_id] = catalog.status_for(desc.provider_id)
    return out


def _build_options(
    catalog: ProviderCatalog,
    statuses: dict[str, ProviderStatus],
    state: WizardState | None = None,
) -> list[Option]:
    options: list[Option] = []
    for desc in _selectable_providers(catalog):
        status = statuses.get(desc.provider_id)
        # Bug 2 — consult EVERY credential surface the wizard knows
        # about before deciding "needs API key". The pre-fix wizard
        # only checked ``vault_keys.get_active_provider_key`` (labeled
        # keys), so an install that stored its OpenAI/Anthropic key in
        # the legacy default-namespace vault (``source: vault
        # (default)``) saw the contradictory pair "needs API key" in
        # the picker badge but "✓ key already configured" the moment
        # they selected the row (because the key step's
        # ``_configure_provider_key`` consults the same
        # ``existing_provider_key`` helper that also reads the legacy
        # vault + env). The badge MUST reflect the same resolution as
        # the next step so the operator never sees that contradiction.
        key_present = False
        if desc.requires_api_key and not desc.supports_local:
            try:
                secret, _src, _lbls = existing_provider_key(
                    desc.provider_id,
                    getattr(desc, "credential_env_var", "") or "",
                    state,
                )
                if secret:
                    key_present = True
            except Exception:
                key_present = False
        if desc.supports_local:
            if status and status.reachable:
                ui_status = STATUS_READY
            else:
                ui_status = STATUS_UNREACHABLE
        else:
            if status and status.configured and status.reachable:
                ui_status = STATUS_READY
            elif key_present:
                # "configured" — refine to UNREACHABLE only if the
                # live probe actively said the network can't be
                # reached. Otherwise show READY so the badge never
                # contradicts the post-select "✓ key already
                # configured" message.
                if status and status.configured and not status.reachable:
                    ui_status = STATUS_UNREACHABLE
                else:
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
    for desc in _selectable_providers(catalog):
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
