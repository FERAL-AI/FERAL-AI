"""Setup, configuration, identity, and credential endpoints."""

import logging
import os
import re

from fastapi import APIRouter, HTTPException

from api.state import state
from config.loader import feral_home
from config.runtime import ollama_base_url

logger = logging.getLogger("feral.api.config")

router = APIRouter()


# ── Setup ──

@router.get("/api/setup/status")
async def setup_status():
    """Check if initial setup has been completed."""
    home = feral_home()
    user_md = home / "USER.md"
    has_identity = False
    if user_md.exists():
        content = user_md.read_text().strip()
        has_identity = (
            bool(content)
            and "Tell your agent about yourself" not in content
            and ("My name is" in content or len(content) > 50)
        )
    return {
        "setup_complete": state.config.setup_complete,
        "has_identity": has_identity,
        "settings": state.config.to_client_safe_dict(),
    }


@router.post("/api/setup/complete")
async def complete_setup(body: dict):
    """Mark setup as complete and apply settings."""
    settings = body.get("settings", {})
    credentials = body.get("credentials", {})
    identity = body.get("identity", {})

    if settings:
        state.config.save_user_settings(settings)
    if credentials:
        if credentials.get("GEMINI_API_KEY") and not credentials.get("GOOGLE_API_KEY"):
            credentials["GOOGLE_API_KEY"] = credentials["GEMINI_API_KEY"]
        if credentials.get("GOOGLE_API_KEY") and not credentials.get("GEMINI_API_KEY"):
            credentials["GEMINI_API_KEY"] = credentials["GOOGLE_API_KEY"]
        for key in ("OPENAI_API_KEY", "GROQ_API_KEY", "ANTHROPIC_API_KEY",
                     "GOOGLE_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY",
                     "OPENROUTER_API_KEY", "MOONSHOT_API_KEY", "DASHSCOPE_API_KEY"):
            if credentials.get(key):
                os.environ[key] = credentials[key]
        state.config.save_credentials(credentials)

    if identity:
        _write_identity_files(identity)

    state.config.mark_setup_complete()
    state.config.discover()

    return {"ok": True, "setup_complete": True}


# ── Config ──

@router.get("/api/config")
async def get_config():
    """Get current configuration (safe for client, no secrets)."""
    return state.config.to_client_safe_dict()


@router.post("/api/config/update")
async def update_config(body: dict):
    """Update a setting. Body: {section, key, value}"""
    section = body.get("section", "")
    key = body.get("key", "")
    value = body.get("value")
    if not section or not key:
        return {"error": "section and key are required"}

    # ── Access mode is not a free-form setting ────────────────────────
    # This route is a generic, unvalidated setter. Letting it write
    # ``access.pairing_mode`` is what produced the reachable-broken
    # state: the web "Same WiFi" button posted the mode here, nothing
    # co-wrote ``network.bind_host``, and the brain then advertised a
    # LAN pair URL while still bound to loopback. The UI reported
    # success. ``POST /api/access/mode`` writes both keys together and
    # reports whether a restart is needed.
    if section == "access" and key == "pairing_mode":
        raise HTTPException(
            status_code=400,
            detail={
                "code": "use_access_mode_endpoint",
                "message": (
                    "access.pairing_mode cannot be set directly — it must stay "
                    "consistent with network.bind_host."
                ),
                "remediation": "POST /api/access/mode with {\"mode\": \"...\"} instead.",
            },
        )
    if section == "network" and key == "bind_host":
        raise HTTPException(
            status_code=400,
            detail={
                "code": "bind_host_is_derived",
                "message": (
                    "network.bind_host is derived from the access mode and is "
                    "not independently writable."
                ),
                "remediation": "POST /api/access/mode with {\"mode\": \"...\"} instead.",
            },
        )

    state.config.update_settings(section, key, value)

    if section == "agents" and state.orchestrator:
        # v2026.6.11 — live-apply the tool-loop budget so the operator's
        # setting takes effect on the next turn, not the next restart.
        # Invalid values are persisted as-is but ignored at apply time
        # (the boot-time resolver also degrades them to the defaults).
        if key == "max_tool_iterations":
            try:
                state.orchestrator._max_iterations = max(0, int(value or 0))
            except (TypeError, ValueError):
                pass
        elif key == "tool_loop_max_seconds":
            try:
                state.orchestrator._tool_loop_max_seconds = max(0.0, float(value or 0))
            except (TypeError, ValueError):
                pass

    elif section == "features" and key == "multi_agent" and state.orchestrator:
        enabled = value if isinstance(value, bool) else str(value).lower() in ("true", "1", "yes", "on")
        os.environ["FERAL_MULTI_AGENT"] = str(enabled).lower()
        state.orchestrator._multi_agent_enabled = enabled
        if enabled and state.orchestrator._multi_agent is None:
            state.orchestrator._init_multi_agent()
        if not enabled:
            state.orchestrator._multi_agent = None

    elif section == "llm" and state.orchestrator and state.orchestrator.llm:
        llm_config = state.config._merged.get("llm", {})
        new_provider = llm_config.get("provider", state.orchestrator.llm.provider)
        new_model = llm_config.get("model", "")
        new_base = llm_config.get("base_url", "")
        new_key = os.environ.get(f"{new_provider.upper()}_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
        # Audit-r8 brief #07: `switch_provider` is async — the prior
        # call site fired-and-forgot the coroutine, leaving persisted
        # settings.json drifted from in-memory `LLMProvider` state.
        # That drift was one of the paths the dated-transcribe model
        # id leaked into chat completions despite a clean settings
        # write. Await the swap so the in-memory provider always
        # matches what we just persisted.
        await state.orchestrator.llm.switch_provider(
            new_provider, model=new_model, base_url=new_base, api_key=new_key,
        )

    elif section == "features":
        enabled = str(value).lower() in ("true", "1", "yes", "on")
        if key == "streaming" and state.orchestrator:
            state.orchestrator._streaming_enabled = enabled
            os.environ["FERAL_STREAMING"] = str(enabled).lower()
        elif key == "proactive":
            if state.orchestrator:
                state.orchestrator._proactive_enabled = enabled
            os.environ["FERAL_PROACTIVE"] = str(enabled).lower()
            if hasattr(state, 'proactive') and state.proactive:
                if enabled:
                    # ``ProactiveEngine.start`` schedules its internal
                    # ``_run_loop`` task on ``self._task`` and returns
                    # fast, so awaiting it here is non-blocking. The
                    # inner task is forwarded into the central registry
                    # so shutdown can cancel it cleanly.
                    await state.proactive.start()
                    if getattr(state.proactive, "_task", None) is not None:
                        state.register_background_task(state.proactive._task)
                else:
                    # A6 — ``ProactiveEngine.stop`` is ``async def``
                    # (it awaits task cancellation). It MUST be
                    # awaited here: the pre-A6 route called it without
                    # await, which was silently discarding the
                    # coroutine and leaving the loop running for a
                    # full interval afterwards.
                    await state.proactive.stop()
        elif key == "vision":
            os.environ["FERAL_VISION_ENABLED"] = str(enabled).lower()
            if state.orchestrator:
                state.orchestrator._vision_enabled = enabled
            if hasattr(state, 'screen_loop') and state.screen_loop:
                if enabled:
                    # ``ScreenLoop.start`` schedules its own capture
                    # task on ``self._task`` and returns fast, so
                    # awaiting it is non-blocking. We forward that
                    # inner task into the central registry.
                    await state.screen_loop.start()
                    if getattr(state.screen_loop, "_task", None) is not None:
                        state.register_background_task(state.screen_loop._task)
                else:
                    # A6 — ``ScreenLoop.stop`` is ``async def`` and
                    # cancels the capture task. Previously the route
                    # called ``state.screen_loop.stop()`` without
                    # awaiting it, producing a "coroutine was never
                    # awaited" warning while the loop kept burning
                    # vision-model API quota every interval.
                    await state.screen_loop.stop()
        elif key == "self_learning":
            os.environ["FERAL_SELF_LEARNING"] = str(enabled).lower()

    elif section == "security" and key == "autonomy_mode":
        if state.orchestrator and hasattr(state.orchestrator, 'tool_runner'):
            state.orchestrator.tool_runner.set_autonomy_mode(str(value))

    elif section == "cost":
        # v2026.5.44 — operator-typed cost caps must take effect
        # without a brain restart. ``CostBudget`` is constructed
        # once at boot in ``BrainState`` with the settings snapshot
        # at that moment, so without an explicit reload the freshly
        # persisted ``cost.<site>.per_hour_usd`` value never reaches
        # ``CostBudget._settings`` and ScreenLoop / ProactiveEngine
        # keep tripping the yellow ``cost_cap_hit`` banner against
        # the factory default of $0.10. Re-read settings into the
        # in-memory budget and clear any per-subsystem
        # ``BudgetLoopGuard`` pause windows so a raised cap recovers
        # an actively-paused subsystem on the next loop tick.
        budget = getattr(state, "cost_budget", None)
        if budget is not None and hasattr(budget, "reload_from_settings"):
            try:
                budget.reload_from_settings()
            except Exception as exc:
                logger.warning("cost_budget reload after settings update failed: %s", exc)
        for guard_attr in (
            "screen_loop_cost_guard",
            "proactive_cost_guard",
            "learner_cost_guard",
            "cron_cost_guard",
            "email_cost_guard",
            "mqtt_cost_guard",
        ):
            guard = getattr(state, guard_attr, None)
            if guard is not None and hasattr(guard, "reset"):
                try:
                    guard.reset()
                except Exception as exc:
                    # This route returns ok=True regardless; a guard that did not
                    # reset stays tripped and keeps blocking work.
                    logger.warning("loop_guard reset (%s) failed: %s", guard_attr, exc)

    return {"ok": True, "section": section, "key": key, "value": value}


# -A13 — Reduce global env mutation blast radius. Only legacy SDKs that
# read API keys directly from ``os.environ`` (openai, anthropic, boto3, …)
# need their credentials exported. Everything else (channel bot tokens,
# webhook secrets, FERAL_KEY_<skill>) flows through the in-process
# ``ConfigLoader._credentials`` / vault path so two sequential test cases
# or two sequential ``/api/config/credentials`` writes can't leak state
# at one another via process-global env vars.
_LEGACY_ENV_EXPORT_KEYS: frozenset[str] = frozenset({
    # LLM SDKs that read keys from os.environ at import / client time
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "DEEPSEEK_API_KEY",
    "MOONSHOT_API_KEY",
    "DASHSCOPE_API_KEY",
    "TOGETHER_API_KEY",
    "FIREWORKS_API_KEY",
    "PERPLEXITY_API_KEY",
    "MISTRAL_API_KEY",
    "COHERE_API_KEY",
    "XAI_API_KEY",
    # boto3 reads these from env if no profile/explicit creds passed
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    # Web-search SDKs/clients that historically read from env
    "TAVILY_API_KEY",
    "BRAVE_API_KEY",
    "EXA_API_KEY",
    "SERPER_API_KEY",
})


def _should_export_to_env(key: str) -> bool:
    """Return True only for keys legacy SDKs explicitly read from env."""
    return key in _LEGACY_ENV_EXPORT_KEYS


_KEY_ENV_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*_API_KEY$")

# Extra env vars providers use that DON'T end in _API_KEY — we still want
# them persisted when Settings → Providers saves them.
_EXTRA_PROVIDER_ENV_VARS: frozenset[str] = frozenset({
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "MOONSHOT_API_KEY",
    "DASHSCOPE_API_KEY",
    "FERAL_TELEGRAM_BOT_TOKEN",
    "FERAL_DISCORD_BOT_TOKEN",
    "FERAL_SLACK_BOT_TOKEN",
    "FERAL_SLACK_APP_TOKEN",
    # WhatsApp Cloud API (Meta) — none end in _API_KEY, so they would be
    # silently rejected by ``_is_accepted_env_key`` without an explicit
    # allowlist entry here.
    "FERAL_WHATSAPP_ACCESS_TOKEN",
    "FERAL_WHATSAPP_PHONE_NUMBER_ID",
    "FERAL_WHATSAPP_APP_SECRET",
    "FERAL_WHATSAPP_VERIFY_TOKEN",
})


def _catalog_env_vars() -> set[str]:
    """Gather every credential_env_var the ProviderCatalog knows about."""
    catalog = getattr(state, "provider_catalog", None)
    if catalog is None:
        return set()
    try:
        descriptors = catalog.list_providers()
    except Exception:
        return set()
    return {d.credential_env_var for d in descriptors if getattr(d, "credential_env_var", "")}


def _is_accepted_env_key(key: str) -> bool:
    if not isinstance(key, str) or not key:
        return False
    if key in _EXTRA_PROVIDER_ENV_VARS:
        return True
    if key in _catalog_env_vars():
        return True
    return bool(_KEY_ENV_PATTERN.match(key))


def _apply_skill_keys(skill_keys: dict) -> tuple[list[str], list[str]]:
    """Write skill API keys through to the live SkillExecutor.

    Returns ``(saved_skill_ids, rejected_skill_ids)``. Values are never
    echoed back. Rejection means the id or the value was empty, or the
    executor is not up yet, in either case the caller must not report
    the key as saved.
    """
    saved: list[str] = []
    rejected: list[str] = []
    executor = getattr(state, "skill_executor", None)
    for skill_id, secret in (skill_keys or {}).items():
        if not isinstance(skill_id, str) or not isinstance(secret, str):
            rejected.append(str(skill_id))
            continue
        if executor is None:
            rejected.append(skill_id)
            continue
        try:
            executor.store_key(skill_id, secret)
        except Exception as exc:
            logger.warning("skill key store failed for %r: %s", skill_id, exc)
            rejected.append(skill_id)
            continue
        saved.append(skill_id)
    if rejected and executor is None:
        logger.warning(
            "skill_keys posted but no SkillExecutor is running; %d key(s) "
            "dropped rather than written somewhere nothing reads.",
            len(rejected),
        )
    return saved, rejected


@router.post("/api/config/credentials")
async def save_credentials(body: dict):
    """Save API credentials.

    Accepts any env var the provider catalog declares, any key matching
    ``^[A-Z][A-Z0-9_]*_API_KEY$``, and a fixed set of provider-specific
    non-``_API_KEY`` env vars (e.g. ``GOOGLE_API_KEY``, AWS creds,
    channel bot tokens). Previously whitelisted only 3 providers and
    silently dropped every other key typed in the UI.

    Body shape::

        { "OPENAI_API_KEY": "...", "GEMINI_API_KEY": "...",
          "skill_keys": { "<skill_id>": "..." } }

    ``skill_keys`` entries are written through to the running
    ``SkillExecutor`` (vault ``skill_keys`` namespace plus its in-process
    cache), so the key is usable on the next tool call with no restart.
    They are reported back under ``skill_keys_saved`` /
    ``skill_keys_rejected`` rather than ``keys_saved``.
    """
    creds: dict = {}
    rejected: list[str] = []
    skill_keys_saved: list[str] = []
    skill_keys_rejected: list[str] = []
    for key, value in (body or {}).items():
        if key == "skill_keys" and isinstance(value, dict):
            creds["skill_keys"] = value
            # Skill keys go straight to the SkillExecutor, which owns the
            # vault namespace the execution path reads. They are NOT
            # pushed through ``os.environ`` because that leaked test-case
            # state across the entire process.
            #
            # This write-through is the fix for the dead round trip: the
            # route used to hand ``skill_keys`` to
            # ``ConfigLoader.save_credentials`` and stop there. That
            # landed the value in ``ConfigLoader._credentials`` where the
            # only reader was ``ConfigLoader.get_skill_key``, which no
            # production code calls. A key typed into Settings therefore
            # never reached ``SkillExecutor._get_key``, and the only
            # working path was ``FERAL_KEY_<SKILL_ID>`` plus a restart.
            saved, rejected_ids = _apply_skill_keys(value)
            skill_keys_saved.extend(saved)
            skill_keys_rejected.extend(rejected_ids)
            continue
        if not _is_accepted_env_key(key):
            rejected.append(key)
            continue
        if not isinstance(value, str) or not value:
            continue
        creds[key] = value
        # -A13 — only mutate the global env for keys legacy SDKs
        # actually read from ``os.environ``. Channel tokens, webhook
        # secrets, etc. are passed explicitly to their consumers below
        # (channel_manager.start_channel) instead of relying on a
        # process-global side channel.
        if _should_export_to_env(key):
            os.environ[key] = value

    persisted_to_creds = False
    try:
        if creds:
            state.config.save_credentials(creds)
            persisted_to_creds = True
    except Exception as exc:  # pragma: no cover — disk write failure
        logger.warning("save_credentials failed to write credentials.json: %s", exc)

    persisted_to_vault: list[str] = []
    if state.vault is not None:
        for env_var, secret in creds.items():
            if env_var == "skill_keys":
                continue
            try:
                state.vault.store(env_var, secret, stored_by="settings_credentials")
                persisted_to_vault.append(env_var)
            except Exception as exc:
                logger.warning("vault.store failed for %s: %s", env_var, exc)

    if state.orchestrator and state.orchestrator.llm:
        for key_name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
                         "GROQ_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY",
                         "TOGETHER_API_KEY", "FIREWORKS_API_KEY"):
            if key_name in creds and creds[key_name]:
                provider = state.orchestrator.llm.provider
                try:
                    await state.orchestrator.llm.switch_provider(provider, api_key=creds[key_name])
                except Exception as exc:
                    # The key was saved but the live provider never picked it up,
                    # so the user sees "saved" and the next call still fails.
                    logger.warning("switch_provider after save_credentials failed: %s", exc)
                break

    if state.channel_manager:
        import asyncio

        def _existing_cfg(channel_type: str) -> dict:
            existing = state.channel_manager._channels.get(channel_type)
            cfg = getattr(existing, "config", {}) if existing else {}
            return cfg if isinstance(cfg, dict) else {}

        def _start_channel_bg(channel_type: str, cfg: dict) -> None:
            """Start a messaging channel in the background, referenced.

            AUDIT-FIXES F-06. These three call sites used a bare
            ``asyncio.create_task(...)``. The event loop holds only a weak
            reference to a task, so once this request handler returned and
            dropped its frame, nothing referenced the start-up coroutine and
            the collector was free to take it mid-flight (verified
            reproducible: a suspended task whose awaited future is reachable
            only through the task itself is destroyed by ``gc.collect()``
            and its remaining steps never run).

            That failure is invisible from the outside: the route has
            already answered ``{"ok": true, "keys_saved": [...]}``, so the
            user is told their Telegram/Slack/WhatsApp token was accepted
            while the channel silently never came up.

            ``state.register_background_task`` is the registry the rest of
            this file already uses (see the proactive/vision toggles above);
            it holds a strong reference until the task finishes and drops it
            on completion. The done-callback additionally *retrieves* the
            exception, which the registry's plain ``set.discard`` does not,
            so a channel that fails to start now says so in the log instead
            of relying on asyncio's never-retrieved warning at collection
            time.
            """
            task = asyncio.create_task(
                state.channel_manager.start_channel(channel_type, cfg),
                name=f"channel-start-{channel_type}",
            )
            state.register_background_task(task)

            def _report(t: "asyncio.Task") -> None:
                if t.cancelled():
                    return
                exc = t.exception()
                if exc is not None:
                    logger.warning(
                        "Channel %s failed to start after credential save: %s",
                        channel_type, exc, exc_info=exc,
                    )

            task.add_done_callback(_report)

        # Telegram / Discord: single bot token.
        for env_key, channel_type in (
            ("FERAL_TELEGRAM_BOT_TOKEN", "telegram"),
            ("FERAL_DISCORD_BOT_TOKEN", "discord"),
        ):
            if env_key not in creds:
                continue
            token = creds.get(env_key) or os.environ.get(env_key, "")
            if not token:
                continue
            existing_cfg = _existing_cfg(channel_type)
            if existing_cfg.get("bot_token", "").strip() != token.strip():
                _start_channel_bg(
                    channel_type,
                    {"bot_token": token, "enabled": True},
                )

        # Slack: either bot/app token update should refresh channel config.
        if "FERAL_SLACK_BOT_TOKEN" in creds or "FERAL_SLACK_APP_TOKEN" in creds:
            existing_cfg = _existing_cfg("slack")
            bot_token = creds.get("FERAL_SLACK_BOT_TOKEN") or existing_cfg.get("bot_token", "")
            app_token = creds.get("FERAL_SLACK_APP_TOKEN") or existing_cfg.get("app_token", "")
            if bot_token:
                changed = (
                    existing_cfg.get("bot_token", "").strip() != bot_token.strip()
                    or existing_cfg.get("app_token", "").strip() != app_token.strip()
                )
                if changed:
                    _start_channel_bg(
                        "slack",
                        {
                            "bot_token": bot_token,
                            "app_token": app_token,
                            "enabled": True,
                        },
                    )

        # WhatsApp: access token + phone id are required; app secret is optional
        # but should trigger restart when changed so signature verification
        # updates without process restart.
        if (
            "FERAL_WHATSAPP_ACCESS_TOKEN" in creds
            or "FERAL_WHATSAPP_PHONE_NUMBER_ID" in creds
            or "FERAL_WHATSAPP_APP_SECRET" in creds
        ):
            existing_cfg = _existing_cfg("whatsapp")
            access_token = creds.get("FERAL_WHATSAPP_ACCESS_TOKEN") or existing_cfg.get("access_token", "")
            phone_number_id = creds.get("FERAL_WHATSAPP_PHONE_NUMBER_ID") or existing_cfg.get("phone_number_id", "")
            app_secret = creds.get("FERAL_WHATSAPP_APP_SECRET") or existing_cfg.get("app_secret", "")
            if access_token and phone_number_id:
                changed = (
                    existing_cfg.get("access_token", "").strip() != str(access_token).strip()
                    or existing_cfg.get("phone_number_id", "").strip() != str(phone_number_id).strip()
                    or existing_cfg.get("app_secret", "").strip() != str(app_secret).strip()
                )
                if changed:
                    _start_channel_bg(
                        "whatsapp",
                        {
                            "access_token": access_token,
                            "phone_number_id": phone_number_id,
                            "app_secret": app_secret,
                            "enabled": True,
                        },
                    )

    return {
        "ok": True,
        "keys_saved": [k for k in creds.keys() if k != "skill_keys"],
        "persisted_to_credentials_json": persisted_to_creds,
        "persisted_to_vault": persisted_to_vault,
        "rejected": rejected,
        # Reported separately from ``keys_saved`` because a skill key is
        # not an env var and lands in a different store. A UI that shows
        # "saved" must key off this list, not off a 200.
        "skill_keys_saved": skill_keys_saved,
        "skill_keys_rejected": skill_keys_rejected,
    }


async def _validate_key_for_provider(provider: str, api_key: str, base_url: str = None) -> tuple[bool, str]:
    """Return (is_valid, message). Makes a real HTTP call to the provider."""
    import httpx

    provider = provider.lower().strip()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if provider == "openai":
                r = await client.get(
                    f"{base_url or 'https://api.openai.com'}/v1/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            elif provider == "anthropic":
                r = await client.get(
                    "https://api.anthropic.com/v1/models",
                    headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                )
            elif provider in ("gemini", "google"):
                r = await client.get(
                    f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}",
                )
            elif provider == "groq":
                r = await client.get(
                    "https://api.groq.com/openai/v1/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            elif provider == "deepseek":
                r = await client.get(
                    "https://api.deepseek.com/v1/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            elif provider == "xai":
                r = await client.get(
                    "https://api.x.ai/v1/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            elif provider == "cohere":
                r = await client.get(
                    "https://api.cohere.ai/v1/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            elif provider == "mistral":
                r = await client.get(
                    "https://api.mistral.ai/v1/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            elif provider == "together":
                r = await client.get(
                    "https://api.together.xyz/v1/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            elif provider == "openrouter":
                r = await client.get(
                    "https://openrouter.ai/api/v1/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            elif provider == "ollama":
                url = base_url or ollama_base_url()
                r = await client.get(f"{url}/api/tags")
            elif provider == "lmstudio":
                url = base_url or "http://localhost:1234"
                r = await client.get(f"{url}/v1/models")
            elif provider == "perplexity":
                r = await client.get(
                    "https://api.perplexity.ai/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                if r.status_code in (200, 400, 405):
                    return True, "Key validates (method-not-allowed is OK for perplexity)"
            elif provider in ("kimi", "moonshot"):
                r = await client.get(
                    "https://api.moonshot.ai/v1/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            elif provider in ("qwen", "dashscope"):
                r = await client.get(
                    "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            else:
                return False, f"Unknown provider: {provider}"

            if r.status_code in (200, 201):
                return True, "Key is valid"
            elif r.status_code in (401, 403):
                return False, f"Invalid or unauthorized key (HTTP {r.status_code})"
            else:
                return False, f"Unexpected response HTTP {r.status_code}: {r.text[:200]}"
    except httpx.TimeoutException:
        return False, "Timeout connecting to provider"
    except httpx.ConnectError as e:
        return False, f"Connection error: {e}"
    except Exception as e:
        return False, f"Validation error: {type(e).__name__}: {e}"


@router.post("/api/config/validate-key")
async def validate_key(body: dict):
    """Validate an LLM API key by making a test request."""
    provider = body.get("provider", "openai")
    api_key = body.get("api_key", "")
    base_url = body.get("base_url", "")

    if not api_key and provider not in ("ollama", "lmstudio"):
        return {"valid": False, "error": "No API key provided"}

    valid, message = await _validate_key_for_provider(provider, api_key, base_url or None)
    return {"valid": valid, "provider": provider, "message": message}


# ── Identity ──

@router.get("/api/identity")
async def get_identity():
    """Get the agent identity configuration."""
    identity_path = feral_home() / "IDENTITY.yaml"
    if identity_path.exists():
        try:
            import yaml
            with open(identity_path) as f:
                return yaml.safe_load(f) or {}
        except Exception:
            pass
    return {"name": "FERAL", "personality": "", "rules": [], "greeting_style": "", "voice": {"tts_voice": "nova"}}


@router.post("/api/identity")
async def update_identity(body: dict):
    """Update the agent identity configuration."""
    identity_path = feral_home() / "IDENTITY.yaml"
    try:
        import yaml
        with open(identity_path, "w") as f:
            yaml.dump(body, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}


# ── Helpers ──

def _build_greeting() -> str:
    """Build a contextual greeting based on identity files."""
    home = feral_home()
    agent_name = "FERAL"
    user_name = ""

    identity_path = home / "IDENTITY.yaml"
    if identity_path.exists():
        try:
            import yaml
            with open(identity_path) as f:
                data = yaml.safe_load(f) or {}
            agent_name = data.get("name", "FERAL")
        except Exception:
            pass

    user_md = home / "USER.md"
    if user_md.exists():
        try:
            for line in user_md.read_text().splitlines():
                if line.startswith("My name is "):
                    user_name = line.replace("My name is ", "").rstrip(".")
                    break
        except Exception:
            pass

    if user_name:
        return f"{agent_name} connected. Hey {user_name}, how can I help?"
    return f"{agent_name} connected. How can I help?"


def _write_identity_files(identity: dict):
    """Write USER.md, SOUL.md, and IDENTITY.yaml from the setup wizard identity payload."""
    home = feral_home()
    home.mkdir(parents=True, exist_ok=True)

    user_name = identity.get("userName", "").strip()
    location = identity.get("location", "").strip()
    occupation = identity.get("occupation", "").strip()
    interests = identity.get("interests", "").strip()
    agent_name = identity.get("agentName", "FERAL").strip() or "FERAL"
    personality_id = identity.get("personality", "assistant")

    personality_map = {
        "assistant": (
            "You are a warm, capable personal assistant. You speak naturally, like "
            "a trusted colleague who knows the user well. You're direct — no filler, "
            "no over-explaining — but never cold. You proactively notice patterns in "
            "the user's data and mention things that might be useful."
        ),
        "engineer": (
            "You are a precise technical partner. You prefer concrete answers, code, "
            "and data over vague suggestions. You think step-by-step and explain your "
            "reasoning. You're comfortable with complexity and don't over-simplify."
        ),
        "coach": (
            "You are an encouraging wellness coach. You're proactive about the user's "
            "health and wellbeing, noticing patterns in their data. You celebrate "
            "progress, suggest improvements gently, and keep the tone supportive."
        ),
        "minimal": (
            "You are brief and factual. No small talk. No filler. Answer the question, "
            "report the data, execute the task. If there's nothing to say, say nothing."
        ),
    }
    soul = personality_map.get(personality_id, personality_map["assistant"])

    lines = ["# About Me\n"]
    if user_name:
        lines.append(f"My name is {user_name}.")
    if location:
        lines.append(f"I live in {location}.")
    if occupation:
        lines.append(f"I work as {occupation}.")
    if interests:
        lines.append(f"\n## Interests\n{interests}")
    if any([user_name, location, occupation, interests]):
        (home / "USER.md").write_text("\n".join(lines) + "\n")

    (home / "SOUL.md").write_text(f"# {agent_name}\n\n{soul}\n")

    try:
        import yaml
        identity_data = {
            "name": agent_name,
            "tagline": "Your personal AI operating system — local, private, always learning.",
            "personality": soul,
            "rules": [
                "Never make up sensor data or health readings. Only report what's actually connected.",
                "If a tool call fails, explain what went wrong in plain language.",
                "Keep responses concise — 1-3 sentences for simple questions.",
                "Respect user privacy. Everything runs locally unless they explicitly ask to share.",
            ],
            "greeting_style": (
                "Keep greetings brief and contextual. If you know the user's name, use it. "
                "Don't list all your capabilities unless asked."
            ),
            "voice": {"style": "conversational", "tts_voice": "nova", "speed": 1.0},
        }
        (home / "IDENTITY.yaml").write_text(yaml.dump(identity_data, default_flow_style=False, sort_keys=False))
    except ImportError:
        import json
        (home / "IDENTITY.yaml").write_text(json.dumps({"name": agent_name, "personality": soul}, indent=2))
