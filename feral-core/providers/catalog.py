"""ProviderCatalog — single source of truth for FERAL's LLM provider + model inventory.

Before this module existed the codebase had *three* parallel registries:

* ``providers/*.py`` adapters — rich ``refresh_models`` / pricing /
  capability code, but only imported by tests.
* ``agents/llm_provider.py::_PROVIDER_REGISTRY`` — a tuple keyed on
  provider id, used by the runtime ``LLMProvider`` for chat.
* ``cli/setup_wizard.py::PROVIDERS`` — a hardcoded dict duplicating
  model lists, used by the interactive setup flow.

The end result was that newer provider models (GPT-5, Claude 4.5, any
2026-released name) were rejected by the wizard while the adapters
already knew how to list them. This module collapses all three
registries into one catalog so the wizard, REST API, v2 `/setup`
page, and runtime ``LLMProvider`` read from the same source.

Responsibilities
----------------
* Keep one adapter instance per provider id. API keys / base URLs are
  re-bound through :meth:`configure` when the caller supplies fresh
  credentials — no process restart required.
* Serve cached model lists from ``~/.feral/.cache/model_catalog.json``
  with a 24-hour TTL so an offline ``feral setup`` still produces
  sensible defaults, and avoid hammering provider APIs on every boot.
* ``list_models(id, live=True)`` is the ergonomic call: returns the
  latest models, refreshing from the provider's ``/v1/models`` (or
  equivalent) when the cache is stale and the network is reachable.
* ``probe(id)`` is the "is it ready?" check used by the wizard's
  side-by-side table — returns ``{reachable, detected_models, error}``
  in one call so both CLI and v2 Setup can render identical status.

Non-goals
---------
* No LLM inference. The existing chat code paths are unchanged;
  ``LLMProvider.chat`` continues to own the hot path. The catalog is
  strictly about *metadata* (what providers exist, what models they
  support, whether they're configured).
* No credential persistence beyond BlindVault / settings.json. The
  catalog never writes keys to disk itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from .base import BaseProvider, Provider

logger = logging.getLogger("feral.providers.catalog")


# ----------------------------------------------------------------------
# Provider metadata + descriptor
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderDescriptor:
    """Static metadata the catalog needs to advertise a provider.

    Runtime properties that change (API key presence, current base URL)
    are stored on the adapter instance itself; this descriptor is the
    "type" info the wizard + settings UI render unchanged.
    """

    provider_id: str
    display_name: str
    supports_local: bool
    requires_api_key: bool
    default_base_url: str
    default_model: str
    credential_env_var: str = ""
    aliases: tuple[str, ...] = ()
    notes: str = ""
    # Truthfulness signal — whether the adapter's ``chat()`` path is
    # production-wired for this provider. Defaults to ``True`` so every
    # existing descriptor + community-installed provider keeps
    # advertising itself as chat-ready. Set to ``False`` for adapters
    # that are discovery-visible (they participate in the picker +
    # model refresh loop) but whose ``chat()`` method still raises or
    # otherwise can't carry a user turn — e.g. the bedrock adapter
    # stubs ``chat()`` until the AWS runtime path is wired. The v2
    # Settings UI reads this through :class:`ProviderStatus` to render
    # a "preview / not chat-ready" chip instead of presenting these
    # providers as equivalently ready next to OpenAI / Anthropic.
    chat_ready: bool = True
    # One-line human-readable reason shown alongside ``chat_ready``
    # when the adapter is not production-wired. Empty when chat_ready
    # is True so the UI can collapse the chip entirely.
    stub_reason: str = ""


@dataclass
class ProviderStatus:
    """Live snapshot the wizard renders in its side-by-side table."""

    provider_id: str
    display_name: str
    supports_local: bool
    requires_api_key: bool
    configured: bool
    reachable: Optional[bool] = None
    default_base_url: str = ""
    default_model: str = ""
    last_refresh: float = 0.0
    error: str = ""
    # Runtime truthfulness for the v2 Settings / Setup UI. When the
    # descriptor (or its adapter) declares the chat path is at stub
    # level, the status surfaces it here so the picker can render a
    # distinct "preview / not chat-ready" chip. Kept optional with a
    # safe default so legacy consumers that never look at this field
    # keep working unchanged, and so new adapters default to
    # production-ready unless they opt out.
    chat_ready: bool = True
    stub_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.provider_id,
            "display_name": self.display_name,
            "supports_local": self.supports_local,
            "requires_api_key": self.requires_api_key,
            "configured": self.configured,
            "reachable": self.reachable,
            "default_base_url": self.default_base_url,
            "default_model": self.default_model,
            "last_refresh": self.last_refresh,
            "error": self.error,
            "chat_ready": self.chat_ready,
            "stub_reason": self.stub_reason,
        }


# ----------------------------------------------------------------------
# Built-in descriptors
# ----------------------------------------------------------------------


# Cloud frontier providers no longer carry a hardcoded ``default_model``
# in their descriptor. The catalog resolves the default lazily through
# :meth:`ProviderCatalog.default_model_for` so the dropdown follows the
# provider's live model list rather than a literal that drifts every
# few months. (Roadmap §3.5 P0.)
#
# Local-runtime providers (ollama, lmstudio) keep an empty default too —
# the live ``/api/tags`` (Ollama) or ``/v1/models`` (LM Studio) call is
# the source of truth and the picker should reflect what's actually
# loaded on the host, not a guess like "llama3.3".
BUILT_IN_DESCRIPTORS: tuple[ProviderDescriptor, ...] = (
    ProviderDescriptor(
        provider_id="openai",
        display_name="OpenAI",
        supports_local=False,
        requires_api_key=True,
        default_base_url="https://api.openai.com/v1",
        default_model="",
        credential_env_var="OPENAI_API_KEY",
        aliases=("open ai", "openai api", "gpt", "chatgpt"),
    ),
    ProviderDescriptor(
        provider_id="codex",
        display_name="Codex (ChatGPT sign-in)",
        supports_local=False,
        requires_api_key=False,
        default_base_url="",
        default_model="",
        aliases=("codex", "codex oauth", "chatgpt sign-in"),
        notes="Requires the Codex CLI and an existing `codex login` session.",
    ),
    ProviderDescriptor(
        provider_id="anthropic",
        display_name="Anthropic",
        supports_local=False,
        requires_api_key=True,
        default_base_url="https://api.anthropic.com/v1",
        default_model="",
        credential_env_var="ANTHROPIC_API_KEY",
        aliases=("claude", "anthropic api"),
        notes="No public /v1/models endpoint — models curated from the bundled catalog.",
    ),
    ProviderDescriptor(
        provider_id="gemini",
        display_name="Google Gemini",
        supports_local=False,
        requires_api_key=True,
        default_base_url="https://generativelanguage.googleapis.com/v1beta",
        default_model="",
        credential_env_var="GOOGLE_API_KEY",
        aliases=("google", "google gemini", "gemini api"),
    ),
    ProviderDescriptor(
        provider_id="groq",
        display_name="Groq",
        supports_local=False,
        requires_api_key=True,
        default_base_url="https://api.groq.com/openai/v1",
        default_model="",
        credential_env_var="GROQ_API_KEY",
        aliases=("groq cloud",),
    ),
    ProviderDescriptor(
        provider_id="deepseek",
        display_name="DeepSeek",
        supports_local=False,
        requires_api_key=True,
        default_base_url="https://api.deepseek.com/v1",
        default_model="",
        credential_env_var="DEEPSEEK_API_KEY",
    ),
    ProviderDescriptor(
        provider_id="openrouter",
        display_name="OpenRouter",
        supports_local=False,
        requires_api_key=True,
        default_base_url="https://openrouter.ai/api/v1",
        default_model="",
        credential_env_var="OPENROUTER_API_KEY",
        aliases=("open router", "router"),
    ),
    ProviderDescriptor(
        provider_id="together",
        display_name="Together AI",
        supports_local=False,
        requires_api_key=True,
        default_base_url="https://api.together.xyz/v1",
        default_model="",
        credential_env_var="TOGETHER_API_KEY",
        aliases=("together ai",),
    ),
    ProviderDescriptor(
        provider_id="fireworks",
        display_name="Fireworks AI",
        supports_local=False,
        requires_api_key=True,
        default_base_url="https://api.fireworks.ai/inference/v1",
        default_model="",
        credential_env_var="FIREWORKS_API_KEY",
        aliases=("fireworks ai",),
    ),
    ProviderDescriptor(
        provider_id="bedrock",
        display_name="Amazon Bedrock",
        supports_local=False,
        requires_api_key=True,
        default_base_url="",
        default_model="",
        credential_env_var="AWS_ACCESS_KEY_ID",
        aliases=("aws bedrock", "amazon"),
        notes=(
            "Auth via AWS IAM (AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY). "
            "Reached through the Bedrock Converse API; the FERAL "
            "LLMProvider runtime currently routes Bedrock through the "
            "catalog adapter rather than the httpx OpenAI-compat path, "
            "so it ships as catalog-ready (chat available via the "
            "catalog adapter / route_call) but does not appear in "
            "SUPPORTED_RUNTIME_PROVIDERS — see findings/13-llm-core.md."
        ),
        # The Bedrock adapter (BedrockProvider.chat /
        # BedrockProvider.stream_chat) is live as of audit-r12 D8 — it
        # speaks bedrock-runtime.converse via boto3 and returns the
        # canonical ChatResponse shape.  this descriptor still
        # advertised ``chat_ready=False``, which made the UI render a
        # "Bedrock not chat-ready" chip even though the adapter ran a
        # real chat turn. ``_resolve_chat_readiness`` lets the adapter
        # downgrade but never upgrade; aligning the descriptor here is
        # the only honest fix. See findings/13-llm-core.md fix #2.
        chat_ready=True,
        stub_reason="",
    ),
    # ── Providers that had a runtime binding but no descriptor ──────
    #
    # ``kimi`` and ``qwen`` were dispatchable by
    # ``agents/llm_provider.py`` for their entire lifetime yet had no
    # descriptor here, so ``ProviderCatalog`` never advertised them:
    # the setup wizard could not offer them and the v2 picker could not
    # render them. ``xai`` was the mirror-image bug — catalog data with
    # no runtime binding. ``tests/test_provider_registry_consistency.py``
    # is what stops all three classes of drift recurring.
    #
    # NOTE the id: the catalog speaks ``moonshot``; the runtime
    # historically says ``kimi``. ``_CATALOG_PROVIDER_MAP`` in
    # agents/llm_provider.py is the (one-entry) translation table.
    ProviderDescriptor(
        provider_id="moonshot",
        display_name="Moonshot (Kimi)",
        supports_local=False,
        requires_api_key=True,
        default_base_url="https://api.moonshot.ai/v1",
        default_model="",
        credential_env_var="MOONSHOT_API_KEY",
        aliases=("kimi", "moonshot ai"),
        notes=(
            "API host is api.moonshot.ai — the docs host moved to "
            "platform.kimi.ai but the API host did not. Model ids use "
            "dots (kimi-k2.7-code)."
        ),
    ),
    ProviderDescriptor(
        provider_id="qwen",
        display_name="Qwen (Alibaba)",
        supports_local=False,
        requires_api_key=True,
        default_base_url=(
            "https://[{WorkspaceId}].ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"
        ),
        default_model="",
        credential_env_var="DASHSCOPE_API_KEY",
        aliases=("alibaba", "dashscope", "tongyi"),
        notes=(
            "Base URL is workspace-scoped: {WorkspaceId} must be substituted "
            "from DASHSCOPE_WORKSPACE_ID before the client can dial."
        ),
    ),
    ProviderDescriptor(
        provider_id="xai",
        display_name="xAI (Grok)",
        supports_local=False,
        requires_api_key=True,
        default_base_url="https://api.x.ai/v1",
        default_model="",
        credential_env_var="XAI_API_KEY",
        aliases=("grok", "x.ai", "x ai"),
        notes="OpenAI-compatible chat; model index is hand-curated (not pollable).",
    ),
    ProviderDescriptor(
        provider_id="zai",
        display_name="Z.ai (GLM)",
        supports_local=False,
        requires_api_key=True,
        default_base_url="https://api.z.ai/api/paas/v4/",
        default_model="",
        credential_env_var="ZAI_API_KEY",
        aliases=("z.ai", "z ai", "glm", "zhipu"),
        notes="glm-5.2 is text-only — no vision.",
    ),
    ProviderDescriptor(
        provider_id="minimax",
        display_name="MiniMax",
        supports_local=False,
        requires_api_key=True,
        default_base_url="https://api.minimax.io/v1",
        default_model="",
        credential_env_var="MINIMAX_API_KEY",
        aliases=("mini max",),
        notes="Model id is CamelCase — MiniMax-M3.",
    ),
    ProviderDescriptor(
        provider_id="mistral",
        display_name="Mistral AI",
        supports_local=False,
        requires_api_key=True,
        default_base_url="https://api.mistral.ai/v1",
        default_model="",
        credential_env_var="MISTRAL_API_KEY",
        aliases=("mistral ai", "le chat"),
        notes="Model ids are date-coded (mistral-medium-2604); no rolling aliases.",
    ),
    ProviderDescriptor(
        provider_id="ollama",
        display_name="Ollama (local)",
        supports_local=True,
        requires_api_key=False,
        # MUST carry the ``/v1`` OpenAI-compat suffix. The runtime posts
        # the relative path ``/chat/completions`` against this base
        # (agents/llm_provider.py), and Ollama only serves that under
        # ``/v1``. The bare root 404s on every chat turn. The wizard
        # copies this value straight into ``llm.base_url``, and
        # ``llm_provider`` only fills the slot when it is empty, so a
        # bare URL here can never be repaired downstream. Matches
        # ``config.runtime.ollama_openai_base_url()``.
        default_base_url="http://localhost:11434/v1",
        default_model="",
        aliases=("local-ollama",),
    ),
    ProviderDescriptor(
        provider_id="lmstudio",
        display_name="LM Studio (local)",
        supports_local=True,
        requires_api_key=False,
        default_base_url="http://localhost:1234/v1",
        default_model="",
        aliases=("lm studio", "lm-studio", "local-lmstudio"),
        notes="LM Studio must be running with a model loaded.",
    ),
)


# ----------------------------------------------------------------------
# Catalog
# ----------------------------------------------------------------------


# 6-hour TTL — short enough that a stale picker dies within a single
# working day, long enough that the first model open after a reboot is
# instant. Live fetch always wins over cache; `force=True` skips the
# cache entirely (that's what the Refresh button hits).
DEFAULT_CACHE_TTL_SECONDS = 6 * 3600


@dataclass
class CachedModelList:
    models: list[str]
    last_refresh: float
    source: str  # "live" | "cache" | "fallback"
    # Populated when a live refresh was attempted but failed (401, 5xx,
    # network error, etc). Surfaced through the REST API so the v2
    # picker can render a warning chip ("key rejected — showing last
    # known good list") instead of silently lying about models.
    warning: str = ""


class ProviderCatalog:
    """Holds one adapter instance per provider id + a disk-backed model cache."""

    def __init__(
        self,
        *,
        cache_path: Optional[Path] = None,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
        descriptors: Optional[Iterable[ProviderDescriptor]] = None,
    ) -> None:
        self._descriptors: dict[str, ProviderDescriptor] = {}
        for d in descriptors or BUILT_IN_DESCRIPTORS:
            self._descriptors[d.provider_id] = d

        self._adapters: dict[str, Provider] = {}
        self._models: dict[str, CachedModelList] = {}
        # Per-provider warning from the most recent refresh attempt. Set
        # when the live call failed (e.g. 401 after the user pasted the
        # wrong key) so the API layer can surface it alongside the
        # fallback list.
        self._warnings: dict[str, str] = {}
        self._cache_path = cache_path
        self._cache_ttl = cache_ttl_seconds
        self._lock = asyncio.Lock()
        self._load_cache()
        self._bind_builtin_adapters()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_providers(self) -> list[ProviderDescriptor]:
        return [self._descriptors[pid] for pid in sorted(self._descriptors)]

    def get_descriptor(self, provider_id: str) -> Optional[ProviderDescriptor]:
        return self._descriptors.get(provider_id)

    def resolve_alias(self, text: str) -> Optional[str]:
        """Map a user-typed string to a known provider id.

        Accepts the id itself, the display name (case-insensitive), any
        declared alias, and finally a substring match. Returns ``None``
        if the input is empty or no provider matches.
        """
        norm = (text or "").strip().lower()
        if not norm:
            return None
        for pid, desc in self._descriptors.items():
            if norm == pid:
                return pid
            if norm == desc.display_name.lower():
                return pid
            if norm in (a.lower() for a in desc.aliases):
                return pid
        # Substring fallback — match only when unambiguous so we don't
        # send "o" to openai vs openrouter vs ollama silently.
        hits = []
        for pid, desc in self._descriptors.items():
            needles = [pid, desc.display_name.lower(), *[a.lower() for a in desc.aliases]]
            if any(norm in n for n in needles):
                hits.append(pid)
        if len(hits) == 1:
            return hits[0]
        return None

    def get_adapter(self, provider_id: str) -> Optional[Provider]:
        return self._adapters.get(provider_id)

    def register_adapter(self, adapter: Provider) -> None:
        """Register or replace the adapter for an already-described provider.

        Community-installed providers can also register a new descriptor
        via :meth:`register_descriptor` first, then this method.
        """
        pid = getattr(adapter, "provider_id", "")
        if not pid:
            raise ValueError("adapter missing provider_id")
        self._adapters[pid] = adapter

    def register_descriptor(self, descriptor: ProviderDescriptor) -> None:
        self._descriptors[descriptor.provider_id] = descriptor

    def configure(
        self,
        provider_id: str,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        **extra: Any,
    ) -> None:
        """Re-bind an adapter with fresh credentials / base URL.

        Called by :class:`LLMProvider` when the user switches providers
        and by the REST config route when settings change at runtime.

        Invalidates the cached model list for this provider so the next
        ``list_models()`` call does a live fetch with the new credentials
        — without this, the v2 Settings picker would keep rendering the
        pre-key model list after the user pasted a working key.
        """
        desc = self._descriptors.get(provider_id)
        if desc is None:
            raise KeyError(f"unknown provider_id: {provider_id!r}")
        adapter = self._build_adapter(desc, api_key=api_key, base_url=base_url, **extra)
        if adapter is not None:
            self._adapters[provider_id] = adapter
        # Drop the stale cache — next list_models() will refetch live.
        self._models.pop(provider_id, None)
        self._warnings.pop(provider_id, None)

    def invalidate_models(self, provider_id: str) -> None:
        """Force the next ``list_models(provider_id)`` call to go live."""
        self._models.pop(provider_id, None)
        self._warnings.pop(provider_id, None)

    async def list_models(
        self,
        provider_id: str,
        *,
        live: bool = True,
        force: bool = False,
        model_class: Optional[str] = None,
        recommended: bool = False,
    ) -> CachedModelList:
        """Return models for *provider_id*.

        ``live=False`` returns the cached value without touching the
        network. ``force=True`` ignores the TTL and always refreshes —
        that's what the "Refresh models" button in v2 Settings hits.
        When a live attempt fails the cached / fallback list is still
        returned, but ``CachedModelList.warning`` carries the error so
        the client can render a visible "key rejected" chip instead of
        silently lying about models.

        ``model_class`` and ``recommended`` are projection-only filters
        layered on top of the canonical cached raw list. They never
        mutate :attr:`_models` — a filtered response returns a new
        :class:`CachedModelList` whose ``models`` are the filtered view
        while the catalog's internal cache keeps the full list from
        the provider, so a subsequent unfiltered call still sees every
        id the provider advertises.
        """
        if provider_id not in self._descriptors:
            raise KeyError(f"unknown provider_id: {provider_id!r}")
        base = await self._resolve_cached(provider_id, live=live, force=force)
        return self._project(provider_id, base, model_class=model_class, recommended=recommended)

    async def _resolve_cached(
        self,
        provider_id: str,
        *,
        live: bool,
        force: bool,
    ) -> CachedModelList:
        """Return the canonical raw ``CachedModelList`` honouring TTL +
        force/live semantics, before any projection-only filter is
        layered on top."""
        cached = self._models.get(provider_id)
        now = time.time()
        if cached and not force and (now - cached.last_refresh) < self._cache_ttl:
            # Still serve whatever warning rode along with the previous
            # attempt — if the last live call 401'd, keep flagging it.
            warning = self._warnings.get(provider_id, "")
            if warning and not cached.warning:
                cached.warning = warning
            return cached
        if not live and cached:
            warning = self._warnings.get(provider_id, "")
            if warning and not cached.warning:
                cached.warning = warning
            return cached
        async with self._lock:
            fresh = await self._refresh_models(provider_id)
            if fresh is not None:
                fresh.warning = ""
                self._warnings.pop(provider_id, None)
                self._models[provider_id] = fresh
                self._save_cache()
                return fresh
            warning = self._warnings.get(provider_id, "")
        if cached:
            cached.warning = warning
            return cached
        fallback = self._fallback_models(provider_id)
        fallback.warning = warning
        return fallback

    def _project(
        self,
        provider_id: str,
        base: CachedModelList,
        *,
        model_class: Optional[str],
        recommended: bool,
    ) -> CachedModelList:
        """Return a filtered view of *base* without mutating the cache.

        When no filter is requested the canonical cached instance is
        returned untouched (preserves existing identity-based test
        semantics). When a filter is requested, a new
        :class:`CachedModelList` is constructed so the catalog's
        in-memory cache keeps the full raw list for subsequent
        unfiltered reads.
        """
        if not model_class and not recommended:
            return base
        # Local imports keep this module's import graph flat at module
        # load time — filter_models and recommended_for pull in the
        # classifier + shortlist tables which aren't needed until a
        # caller actually asks for the filtered view.
        from .model_classes import filter_models
        from .recommended import recommended_for

        models = list(base.models)
        if model_class:
            models = filter_models(provider_id, models, model_class=model_class)
        if recommended:
            models = recommended_for(provider_id, models)
        return CachedModelList(
            models=models,
            last_refresh=base.last_refresh,
            source=base.source,
            warning=base.warning,
        )

    async def probe(self, provider_id: str) -> ProviderStatus:
        """Try to reach the provider; return a ready/not-ready snapshot.

        Uses the adapter's ``refresh_models()`` directly so a network
        failure is distinguishable from a successful-but-empty response.
        The catalog's ``list_models()`` helper would paper over the
        failure with its fallback list, which is the wrong answer for
        a "can we reach the provider right now?" check.
        """
        desc = self._descriptors.get(provider_id)
        if desc is None:
            return ProviderStatus(
                provider_id=provider_id,
                display_name=provider_id,
                supports_local=False,
                requires_api_key=False,
                configured=False,
                reachable=False,
                error=f"unknown provider_id: {provider_id!r}",
            )
        status = self.status_for(provider_id)
        adapter = self._adapters.get(provider_id)
        if adapter is None:
            status.reachable = False
            status.error = "provider adapter unavailable"
            return status
        try:
            models = await adapter.refresh_models()
            cleaned = [m for m in (models or []) if m]
            status.reachable = bool(cleaned)
            status.last_refresh = time.time()
            if cleaned:
                # Persist the hit so list_models can return it without
                # re-reaching out a second time.
                self._models[provider_id] = CachedModelList(
                    models=cleaned, last_refresh=status.last_refresh, source="live"
                )
                self._save_cache()
            else:
                status.error = "provider returned no models"
        except Exception as exc:
            status.reachable = False
            status.error = str(exc)
        return status

    def status_for(self, provider_id: str) -> ProviderStatus:
        desc = self._descriptors.get(provider_id)
        if desc is None:
            raise KeyError(f"unknown provider_id: {provider_id!r}")
        configured = self._is_configured(desc)
        cached = self._models.get(provider_id)
        # ``descriptor.default_model`` is now empty for cloud providers
        # (see Roadmap §3.5 P0); resolve lazily so the v2 picker, the
        # CLI wizard, and the REST API all see the freshest model id
        # the catalog knows about instead of a stale literal that
        # drifts every quarter.
        default_model = desc.default_model or self.default_model_for(provider_id)
        chat_ready, stub_reason = self._resolve_chat_readiness(desc)
        return ProviderStatus(
            provider_id=desc.provider_id,
            display_name=desc.display_name,
            supports_local=desc.supports_local,
            requires_api_key=desc.requires_api_key,
            configured=configured,
            default_base_url=desc.default_base_url,
            default_model=default_model,
            last_refresh=cached.last_refresh if cached else 0.0,
            chat_ready=chat_ready,
            stub_reason=stub_reason,
        )

    def _resolve_chat_readiness(
        self, descriptor: ProviderDescriptor
    ) -> tuple[bool, str]:
        """Combine descriptor + adapter hints into a single readiness verdict.

        The descriptor carries the authoritative, static verdict for
        built-in providers (see the bedrock entry). Concrete adapters
        may also expose ``chat_ready`` / ``stub_reason`` class-level
        attributes so community-installed providers can opt out of the
        "chat-ready" default without editing this module. Adapter
        values take precedence when they explicitly say "not ready" —
        a descriptor-level True never upgrades an adapter-level False
        because the adapter is closer to the truth (it's what actually
        runs ``chat()``).
        """
        chat_ready = descriptor.chat_ready
        stub_reason = descriptor.stub_reason
        adapter = self._adapters.get(descriptor.provider_id)
        if adapter is not None:
            adapter_ready = getattr(adapter, "chat_ready", True)
            if adapter_ready is False:
                chat_ready = False
                adapter_reason = getattr(adapter, "stub_reason", "") or ""
                if adapter_reason:
                    stub_reason = adapter_reason
        if chat_ready:
            stub_reason = ""
        return chat_ready, stub_reason

    async def refresh_all(self) -> dict[str, CachedModelList]:
        out: dict[str, CachedModelList] = {}
        for pid in self._descriptors:
            try:
                out[pid] = await self.list_models(pid, live=True, force=True)
            except Exception as exc:
                logger.debug("refresh_all: %s failed: %s", pid, exc)
        return out

    def default_model_for(self, provider_id: str) -> str:
        """Resolve the default model id for *provider_id* lazily.

        The descriptor no longer carries a hardcoded ``default_model``
        literal — those drift the moment a provider ships a new
        frontier name (Roadmap §3.5 P0). Instead, the catalog reads
        the first entry of the most recent cached / fallback model
        list, which traces back to either a live ``refresh_models``
        call or the bundled ``model_catalog.json`` (and the adapter's
        ``_models`` for Anthropic, which has no live discovery).

        Returns ``""`` when the provider id is unknown so callers
        (LLMProvider, the wizard, the v2 picker) can fall back to
        whatever default the user typed in the UI rather than crash.
        """
        if provider_id not in self._descriptors:
            return ""
        # A5 (audit-v2026.5.5): refuse to resolve a raw sorted ``[0]``
        # from ``/v1/models`` as the chat default. Sorted alphabetically,
        # OpenAI's live list starts with ``babbage-002`` (completion-only)
        # and OpenRouter's starts with ``ai21/jamba-large-1.7``; shipping
        # either as the chat default produced the v2026.5.0 "chat + tools
        # returns 404 / 400 after a live refresh" regression. We now
        # walk the list through (a) recommended shortlist (first tier),
        # (b) chat-class filter, (c) unfiltered raw list — in that order
        # — and only fall back to ``[0]`` when every filter is empty.
        from .model_classes import filter_models
        from .recommended import recommended_for

        cached = self._models.get(provider_id)
        base_models: list[str] = []
        if cached and cached.models:
            base_models = list(cached.models)
        else:
            adapter = self._adapters.get(provider_id)
            if adapter is not None:
                try:
                    base_models = list(adapter.list_models() or [])
                except Exception:
                    base_models = []
        if not base_models:
            return ""

        pick = recommended_for(provider_id, base_models)
        if pick:
            return pick[0]
        chat_only = filter_models(provider_id, base_models, model_class="chat")
        if chat_only:
            return chat_only[0]
        return base_models[0]

    async def refresh_async(
        self,
        *,
        max_concurrency: int = 4,
    ) -> dict[str, CachedModelList]:
        """Refresh every configured provider in the background.

        Wired into the Brain's startup task list (``api/server.py``)
        so the catalog rolls forward without a manual click on the
        ``Refresh models`` button. Skips providers that have no key
        in either the environment or the vault — those would just
        return cached fallback data and burn HTTP for nothing.

        ``max_concurrency`` caps the parallel refresh fan-out so we
        don't slam every provider at boot. Default keeps four in
        flight which is plenty for the ~10 providers we ship.
        """
        candidates = [
            pid
            for pid, desc in self._descriptors.items()
            if not desc.requires_api_key or self._is_configured(desc)
        ]
        if not candidates:
            logger.info(
                "ProviderCatalog.refresh_async: no provider has credentials — skipped"
            )
            return {}
        sem = asyncio.Semaphore(max(1, max_concurrency))

        async def _one(pid: str) -> tuple[str, Optional[CachedModelList]]:
            async with sem:
                try:
                    return pid, await self.list_models(pid, live=True, force=True)
                except Exception as exc:
                    logger.debug("refresh_async: %s failed: %s", pid, exc)
                    return pid, None

        out: dict[str, CachedModelList] = {}
        results = await asyncio.gather(*(_one(pid) for pid in candidates))
        for pid, cached in results:
            if cached is not None:
                out[pid] = cached
        logger.info(
            "ProviderCatalog.refresh_async: refreshed %d/%d providers",
            len(out),
            len(candidates),
        )
        return out

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _bind_builtin_adapters(self) -> None:
        for desc in self._descriptors.values():
            adapter = self._build_adapter(desc)
            if adapter is not None:
                self._adapters[desc.provider_id] = adapter

    def _build_adapter(
        self,
        descriptor: ProviderDescriptor,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        **extra: Any,
    ) -> Optional[Provider]:
        """Instantiate the concrete adapter class for *descriptor*.

        Returns ``None`` and logs a debug line if the adapter module is
        missing — the catalog still advertises the provider via its
        descriptor so the wizard can surface an honest "adapter
        unavailable" status instead of silently dropping it.
        """
        pid = descriptor.provider_id
        api_key = api_key if api_key is not None else self._env_api_key(descriptor)
        base_url = base_url or descriptor.default_base_url or None
        # Imported here (not at module scope) to keep this module's
        # import graph flat — every other adapter import below is
        # function-local for the same reason.
        from .openai_compat import (
            MiniMaxProvider,
            MistralProvider,
            MoonshotProvider,
            QwenProvider,
            XAIProvider,
            ZaiProvider,
        )

        _OPENAI_COMPAT_ADAPTERS = {
            "moonshot": MoonshotProvider,
            "qwen": QwenProvider,
            "xai": XAIProvider,
            "zai": ZaiProvider,
            "minimax": MiniMaxProvider,
            "mistral": MistralProvider,
        }
        try:
            if pid == "openai":
                from .openai_provider import OpenAIProvider
                return OpenAIProvider(api_key=api_key, base_url=base_url)
            if pid == "codex":
                from .codex_provider import CodexProvider
                return CodexProvider()
            if pid == "anthropic":
                from .anthropic_provider import AnthropicProvider
                return AnthropicProvider(api_key=api_key, base_url=base_url)
            if pid == "gemini":
                from .gemini_provider import GeminiProvider
                return GeminiProvider(api_key=api_key, base_url=base_url)
            if pid == "groq":
                from .groq_provider import GroqProvider
                return GroqProvider(api_key=api_key, base_url=base_url)
            if pid == "deepseek":
                from .deepseek_provider import DeepSeekProvider
                return DeepSeekProvider(api_key=api_key, base_url=base_url)
            if pid == "openrouter":
                from .openrouter_provider import OpenRouterProvider
                return OpenRouterProvider(api_key=api_key, base_url=base_url)
            if pid == "together":
                from .together_provider import TogetherProvider
                return TogetherProvider(api_key=api_key, base_url=base_url)
            if pid == "fireworks":
                from .fireworks_provider import FireworksProvider
                return FireworksProvider(api_key=api_key, base_url=base_url)
            if pid == "bedrock":
                from .bedrock_provider import BedrockProvider
                return BedrockProvider(
                    region=extra.get("region") or os.environ.get("AWS_REGION"),
                    aws_access_key_id=extra.get("aws_access_key_id") or api_key,
                    aws_secret_access_key=extra.get("aws_secret_access_key")
                    or os.environ.get("AWS_SECRET_ACCESS_KEY"),
                    aws_session_token=extra.get("aws_session_token")
                    or os.environ.get("AWS_SESSION_TOKEN"),
                )
            # OpenAI-wire-format providers share one parameterised
            # adapter (providers/openai_compat.py) — no per-provider
            # copy of the same 80 lines, and no model/price literals.
            if pid in _OPENAI_COMPAT_ADAPTERS:
                return _OPENAI_COMPAT_ADAPTERS[pid](api_key=api_key, base_url=base_url)
            if pid == "ollama":
                from .ollama_provider import OllamaProvider
                return OllamaProvider(base_url=base_url)
            if pid == "lmstudio":
                from .lmstudio_provider import LMStudioProvider
                return LMStudioProvider(base_url=base_url)
        except ImportError as exc:
            logger.debug("adapter import for %s failed: %s", pid, exc)
            return None
        logger.debug("no known adapter class for provider_id=%s", pid)
        return None

    async def _refresh_models(self, provider_id: str) -> Optional[CachedModelList]:
        adapter = self._adapters.get(provider_id)
        if adapter is None:
            return None
        try:
            models = await adapter.refresh_models()
        except Exception as exc:
            # Capture the failure so list_models() can surface it as a
            # warning on the cached / fallback list. Without this the
            # picker silently falls back to a hardcoded list and looks
            # stale to the user — the exact bug we're fixing.
            logger.debug("refresh_models(%s) raised: %s", provider_id, exc)
            self._warnings[provider_id] = self._format_refresh_error(exc)
            return None
        cleaned = [m for m in (models or []) if m]
        if not cleaned:
            # Distinguish "provider returned nothing" from "network error"
            # by falling back to the adapter's synchronous cache — still
            # better than an empty list.
            try:
                cleaned = list(adapter.list_models() or [])
            except Exception:
                cleaned = []
        if not cleaned:
            return None
        return CachedModelList(
            models=cleaned, last_refresh=time.time(), source="live"
        )

    @staticmethod
    def _format_refresh_error(exc: Exception) -> str:
        """Render *exc* as a single short line for the v2 picker chip."""
        # httpx exposes status codes via response.status_code; we don't
        # import httpx here to avoid a hard dependency at module load.
        status = None
        response = getattr(exc, "response", None)
        if response is not None:
            status = getattr(response, "status_code", None)
        if status == 401 or status == 403:
            return f"provider rejected the API key (HTTP {status})"
        if status == 429:
            return "provider rate-limited the request (HTTP 429)"
        if isinstance(status, int) and status >= 500:
            return f"provider returned HTTP {status}"
        if isinstance(status, int):
            return f"provider returned HTTP {status}"
        msg = str(exc).strip() or exc.__class__.__name__
        if len(msg) > 200:
            msg = msg[:197] + "..."
        return f"refresh failed: {msg}"

    def _fallback_models(self, provider_id: str) -> CachedModelList:
        adapter = self._adapters.get(provider_id)
        models: list[str] = []
        if adapter is not None:
            try:
                models = list(adapter.list_models() or [])
            except Exception:
                models = []
        return CachedModelList(models=models, last_refresh=0.0, source="fallback")

    def _env_api_key(self, descriptor: ProviderDescriptor) -> Optional[str]:
        if not descriptor.credential_env_var:
            return None
        return os.environ.get(descriptor.credential_env_var) or None

    def _is_configured(self, descriptor: ProviderDescriptor) -> bool:
        """Decide whether *descriptor* is "configured" for the picker.

         this returned ``True`` whenever the descriptor's
        credential env var was set, regardless of whether the value
        actually authenticated upstream. Wave 1's
        :mod:`security.probe` helper runs cheap HTTP probes on a 60s
        cache; we consult that cache here so the picker can show a
        configured-but-not-authorising provider as "configured=False"
        instead of greenlighting a 401.

        Three-state semantics: when the probe cache is cold (no probe
        has run yet, e.g. a fresh boot before ``probe_all``) we keep
        the legacy env-var verdict so the picker doesn't flicker
        empty between boot and first probe. Once probes start landing
        the verdict upgrades automatically.
        """
        if not descriptor.requires_api_key:
            return True
        if not self._env_api_key(descriptor):
            return False
        try:
            from security.probe import probe_ok_or_unknown

            verdict = probe_ok_or_unknown(descriptor.provider_id)
        except Exception as exc:
            logger.debug("probe lookup failed for %s: %s", descriptor.provider_id, exc)
            verdict = None
        if verdict is None:
            # Cold cache — defer to env-var presence (legacy behaviour).
            return True
        return bool(verdict)

    def _load_cache(self) -> None:
        if self._cache_path is None:
            return
        try:
            if not self._cache_path.is_file():
                return
            raw = json.loads(self._cache_path.read_text())
        except Exception as exc:
            logger.debug("catalog cache read failed: %s", exc)
            return
        for pid, blob in (raw.get("providers") or {}).items():
            try:
                self._models[pid] = CachedModelList(
                    models=list(blob.get("models") or []),
                    last_refresh=float(blob.get("last_refresh") or 0),
                    source=str(blob.get("source") or "cache"),
                    warning=str(blob.get("warning") or ""),
                )
            except Exception:
                continue

    def _save_cache(self) -> None:
        if self._cache_path is None:
            return
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "providers": {
                pid: {
                    "models": cached.models,
                    "last_refresh": cached.last_refresh,
                    "source": cached.source,
                }
                for pid, cached in self._models.items()
            },
        }
        try:
            self._cache_path.write_text(json.dumps(payload, indent=2))
        except OSError as exc:
            logger.debug("catalog cache write failed: %s", exc)


# ----------------------------------------------------------------------
# Module-level factory used by api.state + cli.setup
# ----------------------------------------------------------------------


_SHARED: Optional[ProviderCatalog] = None


def default_cache_path() -> Path:
    """~/.feral/.cache/model_catalog.json."""
    try:
        from config.loader import feral_home
        home = feral_home()
    except Exception:
        home = Path.home() / ".feral"
    return home / ".cache" / "model_catalog.json"


def get_shared_catalog() -> ProviderCatalog:
    """Return the process-wide :class:`ProviderCatalog` singleton.

    The brain's :mod:`api.state` wires one on boot; CLI tools that
    don't go through the brain (offline ``feral setup``) use this
    factory so the wizard and brain boot the same adapter inventory.
    """
    global _SHARED
    if _SHARED is None:
        _SHARED = ProviderCatalog(cache_path=default_cache_path())
    return _SHARED


def set_shared_catalog(catalog: ProviderCatalog) -> None:
    """Register the brain's boot-time catalog as the process singleton.

    Operator report 2026-05-09 (audit-r8 brief #07): the brain's
    `LLMProvider._default_model_for` calls `get_shared_catalog()`
    which lazily creates a SECOND, empty `ProviderCatalog` if no one
    has registered the boot-time instance. The two catalogs then
    drift — `state.provider_catalog` gets refreshed by
    `refresh_async`, but the singleton consulted by failover /
    `_get_provider_config` stays bare and returns `""` for
    `default_model_for`, falling back to whatever stale
    `FERAL_LLM_MODEL` env / settings.json value is in scope. That's
    how non-chat models leaked into chat completions despite all
    self-heal layers reporting clean.

    `api/state.BrainState.init` calls this immediately after
    constructing `self.provider_catalog`, BEFORE `LLMProvider()` is
    instantiated, so every downstream `_default_model_for` consults
    the live catalog.
    """
    global _SHARED
    _SHARED = catalog


def reset_shared_catalog() -> None:
    """Test helper: drop the singleton so a new instance loads on next get."""
    global _SHARED
    _SHARED = None


# Silence unused-import warnings in static analysis
_ = contextlib, BaseProvider


def provider_base_url_mismatch(provider_id: str, base_url: str | None) -> str | None:
    """Return a complaint if ``base_url`` belongs to a different provider.

    Returns ``None`` when the override is absent, matches the provider's
    own endpoint, or points somewhere this catalog does not recognise.
    That last case is deliberate: proxies, gateways, Ollama and
    self-hosted endpoints are the reason the field exists, and a check
    that flagged them would just teach people to ignore it.

    What it does catch is one provider's adapter aimed at another
    provider's API, which authenticates with the wrong key and fails on
    every call. A real install ran `provider: openrouter` with
    `base_url: https://api.anthropic.com/v1` and logged 610 consecutive
    401s while every status surface reported the provider healthy.
    """
    if not base_url:
        return None

    from urllib.parse import urlparse

    try:
        host = (urlparse(base_url).hostname or "").lower()
    except Exception:
        return None
    if not host:
        return None

    # Only second-guess a provider this catalog actually knows. An
    # unrecognised provider id means an adapter we cannot reason about,
    # and guessing at it would produce confident nonsense.
    known = {d.provider_id for d in BUILT_IN_DESCRIPTORS}
    if provider_id not in known:
        return None

    # Loopback and private addresses are shared ground: Ollama, LM Studio,
    # a dev proxy and a corporate gateway all live there legitimately.
    # Attributing localhost to whichever descriptor happens to default to
    # it would flag every local setup, and a check that cries wolf on the
    # normal case is a check people learn to ignore.
    if host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".local"):
        return None
    try:
        import ipaddress

        if ipaddress.ip_address(host).is_private:
            return None
    except ValueError:
        pass

    owners: dict[str, str] = {}
    for desc in BUILT_IN_DESCRIPTORS:
        own = getattr(desc, "default_base_url", "") or ""
        try:
            own_host = (urlparse(own).hostname or "").lower()
        except Exception:
            continue
        if own_host and own_host not in {"localhost", "127.0.0.1", "::1"}:
            owners[own_host] = desc.provider_id

    owner = owners.get(host)
    if owner is None or owner == provider_id:
        return None
    return (
        f"llm.provider is {provider_id!r} but llm.base_url points at "
        f"{host}, which is {owner!r}'s endpoint. The {provider_id} adapter "
        f"will send a {provider_id} key to {owner}, and every call will "
        f"fail authentication. Remove the base_url override, or set "
        f"provider to {owner!r}."
    )
