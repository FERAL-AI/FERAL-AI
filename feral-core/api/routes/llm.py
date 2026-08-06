"""LLM status, provider catalog, model discovery, switching, preset endpoints.

The `providers` + `providers/{id}/models` routes are the contract the
CLI setup wizard and v2 /setup page both read so they can never drift
from the runtime's view of the world (see
`feral-core/providers/catalog.py`).
"""

import logging
import os
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from api.state import state

logger = logging.getLogger("feral.api.llm")

router = APIRouter()


def _require_catalog():
    catalog = getattr(state, "provider_catalog", None)
    if catalog is None:
        raise HTTPException(status_code=503, detail="ProviderCatalog not initialised")
    return catalog


@router.get("/api/llm/status")
async def llm_status():
    """LLM availability status for the client UI.

    ``supported`` surfaces whether the currently-selected provider
    has a runtime adapter in this build. The UI uses it to render a
    clear "provider not supported" badge instead of showing a green
    dot based on ``available`` alone — the old shape made an unknown
    provider with a stale API key look healthy.
    """
    if not state.orchestrator:
        return {
            "available": False, "provider": "none",
            "supported": False, "reason": "Brain not initialized",
        }
    llm = state.orchestrator.llm
    if not llm:
        return {
            "available": False, "provider": "none",
            "supported": False, "reason": "No LLM configured",
        }
    from agents.llm_provider import is_supported_runtime_provider
    provider = getattr(llm, "provider", "unknown")
    supported = is_supported_runtime_provider(provider) or provider in ("local", "hybrid")
    payload: dict[str, Any] = {
        "available": bool(getattr(llm, "available", False)) and supported,
        "provider": provider,
        "model": getattr(llm, "model", "unknown"),
        "supported": supported,
    }
    if not supported:
        payload["reason"] = (
            f"Provider {provider!r} has no runtime adapter in this build. "
            "Select a supported provider via /api/llm/config."
        )
    return payload


@router.post("/api/llm/switch")
async def llm_switch(body: dict):
    """Hot-swap the LLM provider at runtime.

    Validates the request against the catalog (rejects unknown ids
    with a 400) and against the runtime adapter registry (rejects
    catalog-only providers that have no wire in this build unless
    the caller supplies an explicit ``base_url`` override for a
    custom OpenAI-compatible gateway). Previously, an unknown id
    slipped through and ``switch_provider`` silently aliased it to
    OpenAI.
    """
    if not state.orchestrator or not state.orchestrator.llm:
        return {"error": "Brain not initialized"}
    provider = body.get("provider", "")
    model = body.get("model", "")
    api_key = body.get("api_key", "")
    base_url = body.get("base_url", "")
    if not provider:
        return {"error": "provider is required"}

    catalog = getattr(state, "provider_catalog", None)
    resolved = provider
    if catalog is not None:
        alias = catalog.resolve_alias(provider)
        if alias is not None:
            resolved = alias
        if catalog.get_descriptor(resolved) is None and not base_url:
            # Unknown id with no custom base_url escape hatch -> 400.
            # A caller-supplied base_url signals a custom
            # OpenAI-compatible gateway, which is always welcome.
            raise HTTPException(
                status_code=400,
                detail=f"unknown provider {provider!r}; resolve via /api/llm/providers",
            )

    from agents.llm_provider import is_supported_runtime_provider
    if (
        not is_supported_runtime_provider(resolved)
        and resolved not in ("local", "hybrid")
        and not base_url
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"provider {resolved!r} has no runtime adapter; supply an "
                "explicit base_url for a custom OpenAI-compatible gateway or "
                "pick a supported provider via /api/llm/providers"
            ),
        )

    await state.orchestrator.llm.switch_provider(
        resolved, model=model, api_key=api_key, base_url=base_url,
    )
    llm = state.orchestrator.llm
    return {
        "success": True,
        "provider": llm.provider,
        "model": llm.model,
        "available": bool(llm.available),
        "supported": is_supported_runtime_provider(llm.provider)
                     or llm.provider in ("local", "hybrid"),
    }


@router.get("/api/llm/presets")
async def llm_presets():
    if not state.orchestrator or not state.orchestrator.llm:
        return {"presets": []}
    return {"presets": state.orchestrator.llm.list_presets()}


@router.post("/api/llm/presets/apply")
async def llm_apply_preset(body: dict):
    if not state.orchestrator or not state.orchestrator.llm:
        return {"error": "Brain not initialized"}
    preset_id = body.get("preset", "")
    if not preset_id:
        return {"error": "preset is required"}
    result = await state.orchestrator.llm.apply_preset(preset_id)
    if result.get("ok"):
        state.config.update_settings("llm", "provider", result.get("provider"))
        state.config.update_settings("llm", "model", result.get("model"))
        if result.get("preset") == "ollama_vision":
            state.config.update_settings("vision", "enabled", True)
            state.config.update_settings("vision", "provider", "ollama")
            state.config.update_settings("vision", "model", result.get("model", "llava"))
    return result


@router.get("/api/voice/status")
async def voice_status():
    """Voice subsystem status."""
    realtime_available = state.realtime_proxy.available if state.realtime_proxy else False
    audio_available = state.audio.available if state.audio else False
    active_sessions = len(state.realtime_proxy._sessions) if state.realtime_proxy else 0
    return {
        "realtime_available": realtime_available,
        "audio_available": audio_available,
        "active_realtime_sessions": active_sessions,
        "wake_word_enabled": bool(state.wake_word and state.wake_word.enabled),
        "tts_voice": os.getenv("FERAL_TTS_VOICE", "nova"),
    }


# ----------------------------------------------------------------------
# Provider + model discovery (ProviderCatalog-backed)
# ----------------------------------------------------------------------


@router.get("/api/llm/providers")
async def list_llm_providers():
    """Return every registered provider with its static metadata + live status.

    Renders the side-by-side table in the CLI + v2 Setup flow. ``configured``
    is True when the provider either doesn't need a key or has one in env +
    vault; ``reachable`` stays null until the client calls ``probe`` so this
    route is cheap enough to call on every page load.
    """
    catalog = _require_catalog()
    descriptors = catalog.list_providers()
    payload = []
    for desc in descriptors:
        status = catalog.status_for(desc.provider_id)
        payload.append({
            **status.to_dict(),
            "credential_env_var": desc.credential_env_var,
            "aliases": list(desc.aliases),
            "notes": desc.notes,
        })
    return {"providers": payload, "count": len(payload)}


@router.get("/api/llm/providers/{provider_id}")
async def get_llm_provider(provider_id: str):
    catalog = _require_catalog()
    if catalog.get_descriptor(provider_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown provider_id {provider_id!r}")
    status = catalog.status_for(provider_id)
    return status.to_dict()


@router.get("/api/llm/providers/{provider_id}/models")
async def list_llm_provider_models(
    provider_id: str,
    live: bool = True,
    force: bool = False,
    model_class: Optional[str] = None,
    recommended: bool = False,
):
    """Return the model list for a provider.

    ``live=true`` (default) refreshes from the upstream API when the
    6-hour disk cache is stale. ``force=true`` ignores the TTL — that's
    what the "Refresh models" button hits. The response carries
    ``source: "live"|"cache"|"fallback"`` and an optional ``warning``
    string set when the live attempt failed (e.g. wrong API key) so the
    v2 picker can render a chip explaining the stale list.

    ``model_class`` (e.g. ``"chat"``) and ``recommended=true`` are
    projection-only filters applied to the catalog's raw cached list
    so the v2 picker can default to the chat-ready curated subset
    without the catalog forgetting the full inventory — an unfiltered
    request immediately after still sees every model the provider
    advertised. When both filters are absent, behaviour is unchanged
    so legacy callers keep receiving the full list.
    """
    catalog = _require_catalog()
    if catalog.get_descriptor(provider_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown provider_id {provider_id!r}")
    cached = await catalog.list_models(
        provider_id,
        live=live,
        force=force,
        model_class=model_class,
        recommended=recommended,
    )
    return {
        "provider_id": provider_id,
        "models": cached.models,
        "source": cached.source,
        "last_refresh": cached.last_refresh,
        "count": len(cached.models),
        "warning": cached.warning or "",
    }


@router.post("/api/llm/providers/{provider_id}/probe")
async def probe_llm_provider(provider_id: str):
    """Probe a provider: can we reach it right now with the current creds?

    Used by the wizard to turn "needs API key" into "ready" the moment
    the user enters a valid key, and to render a clear unreachable
    state for Ollama / LMStudio when the local server isn't running.
    """
    catalog = _require_catalog()
    if catalog.get_descriptor(provider_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown provider_id {provider_id!r}")
    status = await catalog.probe(provider_id)
    return status.to_dict()


class ConfigureRequest(BaseModel):
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    extra: dict[str, Any] = Field(default_factory=dict)


def _persist_key(env_var: str, api_key: str) -> dict:
    """Write *api_key* through every persistence layer we have.

    Returns ``{ok, vault, credentials_json, env, warnings}`` so the
    caller (REST endpoint) can surface honest success/failure state
    to the UI instead of silently swallowing errors.
    """
    warnings: list[str] = []
    vault_ok = False
    creds_ok = False

    if env_var:
        os.environ[env_var] = api_key

    if state.vault is not None and env_var:
        try:
            state.vault.store(env_var, api_key, stored_by="settings")
            vault_ok = True
        except Exception as exc:
            warnings.append(f"vault.store({env_var}) failed: {exc}")
            logger.warning("vault.store failed for %s: %s", env_var, exc)

    if state.config is not None and env_var:
        try:
            state.config.save_credentials({env_var: api_key})
            creds_ok = True
        except Exception as exc:
            warnings.append(f"save_credentials({env_var}) failed: {exc}")
            logger.warning("save_credentials failed for %s: %s", env_var, exc)

    return {
        "ok": vault_ok or creds_ok,
        "vault": vault_ok,
        "credentials_json": creds_ok,
        "env": bool(env_var),
        "warnings": warnings,
    }


@router.post("/api/llm/providers/{provider_id}/configure")
async def configure_llm_provider(provider_id: str, req: ConfigureRequest):
    """Re-bind an adapter with a fresh key / base URL without restarting.

    The API key is:
      1. written to the BlindVault (primary, encrypted-at-rest store).
      2. routed through ``ConfigLoader.save_credentials`` which, ,
         also writes to the BlindVault (and NEVER to plaintext
         ``credentials.json``) — this second call keeps the in-memory
         ``ConfigLoader._credentials`` dict in sync for boot-time env
         export and is otherwise idempotent with step 1.
      3. exported to ``os.environ`` so the running ``LLMProvider`` sees
         it without waiting for a reboot.

    ``settings.json`` itself never stores the plaintext key — only the
    currently-selected provider + model + base_url.
    """
    catalog = _require_catalog()
    if catalog.get_descriptor(provider_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown provider_id {provider_id!r}")
    try:
        catalog.configure(
            provider_id,
            api_key=req.api_key,
            base_url=req.base_url,
            **req.extra,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    desc = catalog.get_descriptor(provider_id)
    env_var = desc.credential_env_var if desc else ""
    persisted: dict = {"ok": True, "warnings": []}
    if req.api_key:
        persisted = _persist_key(env_var, req.api_key)

    # Provider-scoped configure() must not overwrite the GLOBAL
    # ``llm.base_url`` unless the provider being configured is the one
    # currently active in settings. Otherwise adding a key for a
    # second provider (e.g. anthropic while openai is active) would
    # quietly repoint the active adapter at the wrong endpoint. The
    # per-provider override still lives on the adapter itself (via
    # ``catalog.configure`` above), and is promoted to ``llm.base_url``
    # only when the user explicitly switches via ``/api/llm/config``.
    active_provider = ""
    if state.config is not None:
        try:
            raw_active = state.config.get("llm", "provider", "") or ""
            # Some route-level tests patch ``state.config`` with a bare
            # MagicMock; ``config.get(...)`` then returns a mock object,
            # not a string. Passing that into ``resolve_alias`` can raise
            # TypeError in its substring path. Non-string provider values
            # are treated as unset for this guard.
            active_provider = raw_active if isinstance(raw_active, str) else ""
        except Exception:
            active_provider = ""
    resolved_active = catalog.resolve_alias(active_provider) or active_provider
    is_active_provider = resolved_active == provider_id

    if req.base_url and state.config is not None and is_active_provider:
        try:
            state.config.update_settings("llm", "base_url", req.base_url)
        except Exception as exc:
            # The response below reports success either way, so a failed write
            # here means the caller believes a base_url that was never saved.
            logger.warning("update_settings(base_url) failed: %s", exc)

    return {
        "success": True,
        "status": catalog.status_for(provider_id).to_dict(),
        "persisted": persisted,
        "active_provider": is_active_provider,
    }


# ----------------------------------------------------------------------
# Active LLM config (settings.json-backed)
# ----------------------------------------------------------------------


@router.get("/api/llm/config")
async def get_llm_config():
    """Return the current llm.* settings snapshot.

    Never includes the API key itself — just whether a key is
    configured for the selected provider.
    """
    if state.config is None:
        raise HTTPException(status_code=503, detail="ConfigLoader not initialised")
    provider = state.config.get("llm", "provider", "") or ""
    model = state.config.get("llm", "model", "") or ""
    base_url = state.config.get("llm", "base_url", "") or ""
    fallbacks = state.config.get("llm", "fallback_providers", []) or []
    catalog = getattr(state, "provider_catalog", None)
    configured = False
    if catalog is not None:
        desc = catalog.get_descriptor(provider)
        if desc is not None:
            configured = catalog.status_for(desc.provider_id).configured
    return {
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "fallback_providers": list(fallbacks),
        "configured": configured,
    }


class LLMConfigRequest(BaseModel):
    provider: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1)
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    fallback_providers: Optional[list[str]] = None


@router.get("/api/llm/health")
async def llm_health():
    """Active provider + every fallback + cooldown state.

    Used by the v2 Settings → Providers → Fallbacks card to render
    green/amber/red dots per candidate so the user can see exactly why
    the agent fell over to a different provider this minute.

    The  expansion (Lane 09) adds:

    * ``last_failover`` — ``{from, to, reason, candidates_tried}`` for
      the most recent ``chat_with_failover`` hop, or ``None`` when the
      primary won on first attempt. Drives the "fallback active" chip.
    * ``budget`` — current per-call-site CostBudget remainings + caps.
    * Per-candidate ``probe_ok`` (from ``security.probe`` cache) so the
      Settings card can flag "configured AND authenticates" vs.
      "configured but 401".
    """
    if not state.orchestrator or not state.orchestrator.llm:
        return {"available": False, "active": None, "candidates": [], "fallback_providers": []}
    snapshot = state.orchestrator.llm.health_snapshot()
    # Decorate every candidate with the last cached probe verdict so
    # the picker can render "auth ok" / "401" / "not probed yet" without
    # firing a fresh probe on every page load.
    try:
        from security.probe import cached_probe_result
    except Exception:
        cached_probe_result = None  # type: ignore[assignment]
    if cached_probe_result is not None:
        for cand in snapshot.get("candidates", []) or []:
            pr = cached_probe_result(cand.get("provider", ""))
            if pr is None:
                cand["probe_ok"] = None
                cand["probe_status"] = None
                cand["probe_at"] = None
            else:
                cand["probe_ok"] = bool(pr.ok)
                cand["probe_status"] = pr.status_code
                cand["probe_at"] = pr.probed_at
                cand["probe_reason"] = pr.reason
    return snapshot


@router.post("/api/llm/cooldowns/reset")
async def reset_llm_cooldowns(body: dict | None = None):
    """Drop the in-memory + on-disk LLM provider cooldown state.

    Demo prep 2026-06-05: when an operator tops up a billing account
    or rotates a provider key, the failover loop's cooldown ledger
    still parks that provider for the rest of the BILLING / AUTH
    window (1 h / 5 m respectively). This endpoint forces an
    immediate retry on the next chat turn — the failover loop will
    re-park the provider on its own if the upstream is still broken,
    so it's safe to call freely.

    Body: ``{"provider": "<id>"}`` to clear one provider, omit or
    pass an empty string to clear every parked provider.
    """
    if not state.orchestrator or not state.orchestrator.llm:
        raise HTTPException(status_code=503, detail="LLM not initialised")
    tracker = getattr(state.orchestrator.llm, "_cooldown", None)
    if tracker is None or not hasattr(tracker, "clear"):
        raise HTTPException(status_code=503, detail="cooldown tracker unavailable")
    target = ""
    if isinstance(body, dict):
        target = (body.get("provider") or "").strip().lower()
    cleared = tracker.clear(provider=target or None)
    return {"ok": True, "cleared": cleared}


# ----------------------------------------------------------------------
# Multi-key per provider ( Lane 09)
# ----------------------------------------------------------------------
#
# Operators can stash multiple labeled API keys per provider (a prod
# key + a dev key + a team-shared key) and switch between them without
# re-typing the secret. The labeled-key feature is layered on top of
# BlindVault via security/vault_keys.py — vault.py core is unchanged
# (Lane 03 owns it). See the Lane 09 PR body for the contract Lane 07
# (CLI) wraps as `feral key add/remove/list --provider --label`.


class ProviderKeyRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=64)
    api_key: str = Field(..., min_length=1)
    set_active: bool = False


def _require_vault():
    if state.vault is None:
        raise HTTPException(status_code=503, detail="vault not initialised")
    return state.vault


@router.post("/api/llm/providers/{provider_id}/keys")
async def add_provider_key(provider_id: str, req: ProviderKeyRequest):
    """Add or replace a labeled key for *provider_id*.

    Idempotent: posting the same ``label`` again replaces the secret
    while preserving ``created_at``. Pass ``set_active=true`` to make
    this label the runtime's default selection (the next chat turn
    will use this key).
    """
    catalog = _require_catalog()
    if catalog.get_descriptor(provider_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown provider_id {provider_id!r}")
    vault = _require_vault()
    try:
        from security import vault_keys
        entry = vault_keys.add_provider_key(
            provider_id,
            req.label,
            req.api_key,
            set_active=req.set_active,
            vault=vault,
        )
    except (vault_keys.InvalidProviderId, vault_keys.InvalidLabel) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # When set_active landed the key in the active slot, also push the
    # raw secret through the legacy persistence path (env + default
    # vault namespace + credentials.json) so the running LLMProvider
    # picks it up without a reboot. This mirrors what
    # /api/llm/providers/{id}/configure does for unlabeled writes.
    reconfigure_result: dict | None = None
    if req.set_active:
        desc = catalog.get_descriptor(provider_id)
        env_var = desc.credential_env_var if desc else ""
        if env_var:
            _persist_key(env_var, req.api_key)
            try:
                catalog.configure(provider_id, api_key=req.api_key)
            except Exception as exc:
                logger.warning(
                    "catalog.configure(%s) after add_provider_key failed: %s",
                    provider_id, exc,
                )
        # Cross-cut #1 (v2026.5.42): push the newly-active labeled key
        # into the running LLMProvider so the next chat turn uses it
        # without a brain restart. Pre-fix the vault was updated but
        # ``self.api_key`` / the httpx client stayed stale until the
        # operator hit Settings → Save & switch.
        if state.orchestrator and getattr(state.orchestrator, "llm", None) is not None:
            try:
                from security.vault_keys import get_active_key
                secret = get_active_key(provider_id)
                if secret and provider_id == state.orchestrator.llm.provider:
                    reconfigure_result = await state.orchestrator.llm.reconfigure(
                        provider=provider_id,
                        model=state.orchestrator.llm.model or "",
                        api_key=secret,
                        base_url=state.orchestrator.llm.base_url or "",
                    )
            except Exception as exc:
                logger.warning(
                    "reconfigure after add_provider_key(%s) failed: %s",
                    provider_id, exc,
                )
                reconfigure_result = {"ok": False, "reason": str(exc)}
    payload: dict = {"success": True, "key": entry.to_dict()}
    if reconfigure_result is not None:
        payload["reconfigured"] = reconfigure_result
    return payload


@router.get("/api/llm/providers/{provider_id}/keys")
async def list_provider_keys(provider_id: str):
    """Return every labeled key for *provider_id*. Never includes
    secrets — only label, fingerprint, timestamps, last probe verdict.
    """
    catalog = _require_catalog()
    if catalog.get_descriptor(provider_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown provider_id {provider_id!r}")
    vault = _require_vault()
    from security import vault_keys
    entries = vault_keys.list_provider_keys(provider_id, vault=vault)
    return {
        "provider_id": provider_id,
        "active_label": vault_keys.get_active_label(provider_id, vault=vault),
        "keys": [entry.to_dict() for entry in entries],
        "count": len(entries),
    }


@router.delete("/api/llm/providers/{provider_id}/keys/{label}")
async def delete_provider_key(provider_id: str, label: str):
    """Remove the labeled key. If it was the active selection, the
    active pointer is cleared and the runtime falls back to the legacy
    default-namespace credential.
    """
    catalog = _require_catalog()
    if catalog.get_descriptor(provider_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown provider_id {provider_id!r}")
    vault = _require_vault()
    from security import vault_keys
    try:
        removed = vault_keys.remove_provider_key(provider_id, label, vault=vault)
    except (vault_keys.InvalidProviderId, vault_keys.InvalidLabel) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not removed:
        raise HTTPException(
            status_code=404,
            detail=f"no labeled key {label!r} stored for provider {provider_id!r}",
        )
    return {"success": True, "provider_id": provider_id, "label": label}


class ProviderActiveRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=64)


@router.post("/api/llm/providers/{provider_id}/keys/active")
async def set_provider_active_key(provider_id: str, req: ProviderActiveRequest):
    """Mark *label* as the active selection for *provider_id*.

    Also pushes the secret into the legacy env / default vault
    namespace so the running ``LLMProvider`` picks it up on the next
    chat turn (no reboot needed).
    """
    catalog = _require_catalog()
    desc = catalog.get_descriptor(provider_id)
    if desc is None:
        raise HTTPException(status_code=404, detail=f"unknown provider_id {provider_id!r}")
    vault = _require_vault()
    from security import vault_keys
    try:
        active = vault_keys.set_active_label(provider_id, req.label, vault=vault)
    except (vault_keys.InvalidProviderId, vault_keys.InvalidLabel) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    secret = vault_keys.get_provider_key(provider_id, active, vault=vault, record_use=True)
    env_var = desc.credential_env_var or ""
    if secret and env_var:
        _persist_key(env_var, secret)
        try:
            catalog.configure(provider_id, api_key=secret)
        except Exception as exc:
            logger.warning(
                "catalog.configure(%s) after set_active_label failed: %s",
                provider_id, exc,
            )
    # Cross-cut #1 (v2026.5.42): propagate the active-label swap into
    # the running LLMProvider so the next chat turn uses the new key
    # without a restart. Pre-fix the vault active pointer flipped but
    # ``self.api_key`` / the httpx client stayed pinned to the
    # previously-active secret until full Save & switch.
    reconfigure_result: dict | None = None
    if (
        secret
        and state.orchestrator
        and getattr(state.orchestrator, "llm", None) is not None
        and provider_id == state.orchestrator.llm.provider
    ):
        try:
            reconfigure_result = await state.orchestrator.llm.reconfigure(
                provider=provider_id,
                model=state.orchestrator.llm.model or "",
                api_key=secret,
                base_url=state.orchestrator.llm.base_url or "",
            )
        except Exception as exc:
            logger.warning(
                "reconfigure after set_active_label(%s -> %s) failed: %s",
                provider_id, active, exc,
            )
            reconfigure_result = {"ok": False, "reason": str(exc)}
    payload: dict = {
        "success": True, "provider_id": provider_id, "active_label": active,
    }
    if reconfigure_result is not None:
        payload["reconfigured"] = reconfigure_result
    return payload


@router.get("/api/llm/route")
async def llm_route(call_site: str, tier: Optional[str] = None):
    """Resolve a (provider, model) reference for a given call_site.

    Thin REST wrapper around ``LLMProvider.route_call`` — used by Lane
    08's orchestrator to pre-flight tier decisions and by the v2
    Settings → Tier picker UI to render "what would happen if I
    selected this tier" without actually firing a chat. See
    ``LLMProvider.route_call`` docstring for the full contract.
    """
    if not state.orchestrator or not state.orchestrator.llm:
        raise HTTPException(status_code=503, detail="brain not initialized")
    try:
        ref = state.orchestrator.llm.route_call(call_site, tier=tier)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return ref


@router.post("/api/llm/providers/{provider_id}/keys/{label}/probe")
async def probe_provider_key(provider_id: str, label: str):
    """Probe the key behind ``label``: does it actually authenticate?

    Temporarily exports the labeled secret into the env var the probe
    helper reads, runs the probe, then restores the previous env value.
    The labeled-keys metadata is updated with the verdict so the list
    endpoint can render "Probe: ok 30s ago" without re-issuing.
    """
    catalog = _require_catalog()
    desc = catalog.get_descriptor(provider_id)
    if desc is None:
        raise HTTPException(status_code=404, detail=f"unknown provider_id {provider_id!r}")
    vault = _require_vault()
    from security import vault_keys
    try:
        secret = vault_keys.get_provider_key(provider_id, label, vault=vault)
    except (vault_keys.InvalidProviderId, vault_keys.InvalidLabel) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if secret is None:
        raise HTTPException(
            status_code=404,
            detail=f"no labeled key {label!r} stored for provider {provider_id!r}",
        )

    env_var = desc.credential_env_var or ""
    saved = os.environ.get(env_var) if env_var else None
    if env_var:
        os.environ[env_var] = secret
    try:
        from security.probe import probe as run_probe, clear_probe_cache
        clear_probe_cache()
        result = await run_probe(provider_id, vault=vault, force=True)
    finally:
        if env_var:
            if saved is None:
                os.environ.pop(env_var, None)
            else:
                os.environ[env_var] = saved
    vault_keys.record_probe_result(
        provider_id, label, ok=bool(result.ok), vault=vault,
    )
    payload = {
        "provider_id": provider_id,
        "label": label,
        "ok": bool(result.ok),
        "status_code": result.status_code,
        "reason": result.reason,
        "detail": result.detail,
        "latency_ms": result.latency_ms,
        "probed_at": result.probed_at,
    }
    return payload


@router.post("/api/llm/config")
async def set_llm_config(req: LLMConfigRequest):
    """Persist llm.* settings + route the key into vault + credentials +
    env + hot-swap the running LLMProvider.

    This is the single entry point the v2 Settings → Providers "Save &
    switch" button hits. After this call completes successfully the
    next chat turn uses the new provider — no reboot needed.
    """
    catalog = _require_catalog()
    resolved = catalog.resolve_alias(req.provider) or req.provider
    if catalog.get_descriptor(resolved) is None:
        raise HTTPException(
            status_code=400,
            detail=f"unknown provider {req.provider!r}; resolve via /api/llm/providers",
        )
    if state.config is None:
        raise HTTPException(status_code=503, detail="ConfigLoader not initialised")

    # Gate catalog-only descriptors (e.g. ``bedrock``, ``together``,
    # ``fireworks``) that ship without a runtime adapter in this build.
    # We allow the save to proceed only when the caller supplies an
    # explicit ``base_url`` override — that's the escape hatch for
    # custom OpenAI-compatible gateways. Otherwise we 400 so the UI
    # never lands on a provider the runtime can't actually call.
    from agents.llm_provider import is_supported_runtime_provider
    if (
        not is_supported_runtime_provider(resolved)
        and resolved not in ("local", "hybrid")
        and not req.base_url
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"provider {resolved!r} is listed in the catalog but has no "
                "runtime adapter. Supply an explicit base_url for a custom "
                "OpenAI-compatible gateway, or pick a supported provider."
            ),
        )

    # Auto-prepend the previous primary as a fallback so failover works
    # by default. User can still explicitly pass `fallback_providers`
    # (including []) to override this.
    previous_provider = state.config.get("llm", "provider", "") or ""
    state.config.update_settings("llm", "provider", resolved)
    state.config.update_settings("llm", "model", req.model)
    if req.base_url is not None:
        state.config.update_settings("llm", "base_url", req.base_url)
    if req.fallback_providers is not None:
        fallbacks = list(req.fallback_providers)
    else:
        existing = state.config.get("llm", "fallback_providers", []) or []
        fallbacks = [p for p in existing if p != resolved]
        if previous_provider and previous_provider != resolved and previous_provider not in fallbacks:
            fallbacks.insert(0, previous_provider)
    state.config.update_settings("llm", "fallback_providers", fallbacks)

    desc = catalog.get_descriptor(resolved)
    env_var = desc.credential_env_var if desc else ""
    persisted: dict = {"ok": True, "warnings": []}
    if req.api_key:
        persisted = _persist_key(env_var, req.api_key)
        catalog.configure(resolved, api_key=req.api_key, base_url=req.base_url)

    state.config.update_settings("meta", "setup_complete", True)

    # Hot-swap the running LLMProvider so the next chat turn uses the
    # new config without waiting for a Brain reboot. Happens even when
    # no api_key was supplied (user just switching between already-
    # configured providers).
    reconfigure_result: dict = {"ok": False, "reason": "orchestrator_missing"}
    if state.orchestrator and state.orchestrator.llm:
        try:
            reconfigure_result = await state.orchestrator.llm.reconfigure(
                provider=resolved,
                model=req.model,
                api_key=req.api_key or "",
                base_url=req.base_url or "",
            )
            # Push the new fallback list into the running LLM so
            # chat_with_failover picks it up on the very next turn.
            cur = state.orchestrator.llm._config if isinstance(state.orchestrator.llm._config, dict) else {}
            state.orchestrator.llm.set_config({**cur, "fallback_providers": fallbacks})
        except Exception as exc:
            logger.warning("reconfigure after set_llm_config failed: %s", exc)
            reconfigure_result = {"ok": False, "reason": str(exc)}

    return {
        "success": True,
        "provider": resolved,
        "model": req.model,
        "persisted": persisted,
        "reconfigured": reconfigure_result,
    }
