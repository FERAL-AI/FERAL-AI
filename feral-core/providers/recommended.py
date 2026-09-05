"""Per-provider recommended-model shortlists ("latest relevant").

A provider's ``/v1/models`` endpoint returns everything the provider
ever published — legacy chat releases, embedding heads, audio codecs,
image models, fine-tune snapshots, and preview branches. Surfacing all
132 OpenAI IDs (or all 355 OpenRouter routes) to the end user is noise;
they want to pick from the 6-10 models that actually earn their $$ in
2026-Q2.

This module is the curated overlay. It is the second filter that
follows :func:`feral_core.providers.model_classes.classify` — first
``classify()`` drops non-chat classes (embeddings, audio, image,
completion-only), then this module's :func:`is_recommended` keeps only
the 2026-04-26 latest-relevant picks per provider.

The list is maintained by the conductor based on live
``/v1/models`` output plus the upstream provider's current
"recommended for new projects" guidance. When a provider ships a newer
model, the entry rolls forward and previous-gen entries move to a
``_LEGACY_OK`` set so existing users with saved picks keep working
without the picker surfacing them to new users.

The v2 Settings picker defaults to ``recommended=True``; a "Show all"
toggle flips to the full chat-class list.
"""

from __future__ import annotations

from typing import FrozenSet


# ─────────────────────────────────────────────────────────────────────
# Curated shortlists per provider (2026-04-26)
# ─────────────────────────────────────────────────────────────────────
# Update criteria:
#   1. Upstream provider page lists it as "Recommended for production"
#      or equivalent (the provider's own editorial pick).
#   2. Still-receiving-updates (not marked deprecated in /v1/models).
#   3. Covers the tier spread the operator cares about: a flagship,
#      a fast/cheap tier, and a thinking/reasoning tier where the
#      provider has one.

_RECOMMENDED_OPENAI: FrozenSet[str] = frozenset({
    # GPT-6 Astra (2026-09-03). Recommended, and ranked one below the
    # 5.6 flagship in ``_TIER_RANK`` so it never becomes the silent
    # default (it costs twice as much per token).
    "gpt-6-astra",
    # Flagship (2026-07 generation). The 5.6 line names its tiers
    # sol / terra / luna; ``gpt-5.6`` is an alias of ``gpt-5.6-sol``.
    "gpt-5.6-sol",
    "gpt-5.6",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    # Cheapest SKU — the cheap-tier target in
    # ``LLMProvider._CHEAP_SIBLING``.
    "gpt-5-nano",
    # Previous flagship generation (still current, still recommended)
    "gpt-5.5",
    "gpt-5.5-pro",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.4-nano",
    "gpt-5",
    "gpt-5-mini",
    # Reasoning tier
    "o4-mini",
    "o3",
    "o3-mini",
    # Vision-capable chat
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-4.1-nano",
})

_RECOMMENDED_ANTHROPIC: FrozenSet[str] = frozenset({
    # Claude 5 generation (2026-07). Ids are DATELESS — the bare id is
    # itself a pinned snapshot; appending a date 404s.
    "claude-opus-5",
    "claude-fable-5",
    "claude-sonnet-5",
    # Claude 4.x, still current
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
    "claude-haiku-4-5",
})

_RECOMMENDED_DEEPSEEK: FrozenSet[str] = frozenset({
    # These two are the only chat models DeepSeek exposes on
    # /v1/models as of 2026-04-26. The legacy deepseek-chat /
    # deepseek-reasoner aliases deprecate 2026-07-24 per upstream.
    "deepseek-v4-pro",
    "deepseek-v4-flash",
})

_RECOMMENDED_GEMINI: FrozenSet[str] = frozenset({
    # 3.5 / 3.6 flash tiers (2026-07)
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    # 3.1 tier (current). NOTE: the ``-preview`` suffix is part of the
    # model id — there is no stable ``gemini-3.1-pro``.
    "gemini-3.1-pro-preview",
    "gemini-3.1-flash-lite-preview",
    "gemini-3.1-flash-image-preview",
    # 3.0 tier (still widely deployed)
    "gemini-3-pro-preview",
    "gemini-3-flash-preview",
    "gemini-3-pro-image-preview",
    # 2.5 tier (stable, cost-effective)
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    # Rolling aliases that always point at the latest
    "gemini-pro-latest",
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
})

_RECOMMENDED_GROQ: FrozenSet[str] = frozenset({
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "qwen/qwen3-32b",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "groq/compound",
    "groq/compound-mini",
})

# OpenRouter's /v1/models returns 355+ routes. The shortlist below is
# biased toward the most-used routes for each upstream provider. The
# v2 picker should still let the user type to filter across all 355
# when they want a specific route.
_RECOMMENDED_OPENROUTER_PREFIXES: FrozenSet[str] = frozenset({
    # Anthropic on OpenRouter
    "anthropic/claude-opus-5",
    "anthropic/claude-fable-5",
    "anthropic/claude-sonnet-5",
    "anthropic/claude-opus-4",
    "anthropic/claude-sonnet-4",
    "anthropic/claude-haiku-4",
    # OpenAI on OpenRouter ("openai/gpt-5" also covers the 5.6 line)
    "openai/gpt-5",
    "openai/gpt-4.1",
    "openai/o3",
    "openai/o4",
    # Google on OpenRouter
    "google/gemini-3",
    "google/gemini-2.5",
    # Meta / Llama
    "meta-llama/llama-4",
    "meta-llama/llama-3.3",
    # DeepSeek on OpenRouter (useful when you don't want to hold a
    # DeepSeek key directly)
    "deepseek/deepseek-v4",
    # xAI
    "x-ai/grok-",
    # Mistral
    "mistralai/mistral-",
    "mistralai/mixtral-",
    # Qwen
    "qwen/qwen3",
    # Moonshot / Kimi
    "moonshotai/kimi-k",
    # Z.ai / GLM
    "z-ai/glm-5",
    # MiniMax
    "minimax/minimax-m",
})

# Locally-hosted backends: there is no upstream catalog — whatever the
# user has loaded IS the list. The recommended overlay is a no-op;
# everything the host advertises is relevant by definition.
_LOCAL_PROVIDERS: FrozenSet[str] = frozenset({"lmstudio", "ollama", "local"})


_RECOMMENDED_BY_PROVIDER: dict[str, FrozenSet[str]] = {
    "openai": _RECOMMENDED_OPENAI,
    "anthropic": _RECOMMENDED_ANTHROPIC,
    "deepseek": _RECOMMENDED_DEEPSEEK,
    "gemini": _RECOMMENDED_GEMINI,
    "groq": _RECOMMENDED_GROQ,
}


def is_recommended(provider_id: str, model_id: str) -> bool:
    """True iff ``model_id`` is on the conductor-curated shortlist.

    Unknown providers and local backends return True — the caller has
    no other source of truth for what's relevant there.
    """
    pid = (provider_id or "").lower()
    mid = (model_id or "").strip()

    if not mid:
        return False

    if pid in _LOCAL_PROVIDERS:
        return True

    if pid == "openrouter":
        return any(mid.startswith(p) for p in _RECOMMENDED_OPENROUTER_PREFIXES)

    shortlist = _RECOMMENDED_BY_PROVIDER.get(pid)
    if shortlist is None:
        # Unknown provider: the operator's own inventory is the
        # authoritative list. Don't second-guess.
        return True
    return mid in shortlist


# Per-provider tier priority: regex prefix → rank (lower = higher
# priority). When ``recommended_for`` returns its shortlist, it sorts
# by (tier_rank, original_index) so the v2 picker's first entry is
# the flagship, not the alphabetically-first listing. Providers without
# a priority entry keep caller order.
_TIER_RANK: dict[str, list[tuple[str, int]]] = {
    "openai": [
        # gpt-5.6-sol stays rank 0 on purpose. ``default_model_for`` takes
        # rank 0, and gpt-6-astra costs twice as much per token ($10/$50
        # against $5/$30). Making it the silent default the day it lands
        # on an account would double every operator's bill without a
        # decision. It is one row below so the picker shows it first
        # after the current default and an operator can choose it.
        ("gpt-5.6-sol", 0),
        ("gpt-6-astra", 1),
        ("gpt-5.6-terra", 2),
        ("gpt-5.6-luna", 3),
        ("gpt-5.6", 4),
        ("gpt-5.5-pro", 5),
        ("gpt-5.5", 6),
        ("gpt-5.4", 7),
        ("gpt-5-mini", 8),
        ("gpt-5-nano", 8),
        ("gpt-5", 8),  # tied with mini/nano; stable order preserved
        ("o4-", 9),
        ("o3", 10),
        ("gpt-4.1", 11),  # previous-gen; still recommended but below current
    ],
    # ``default_model_for`` takes rank 0, so this ladder also chooses
    # the provider's DEFAULT model — not merely the display order.
    # claude-opus-5 leads rather than claude-fable-5 deliberately:
    # Fable is the premium tier at $10/$50 per 1M vs Opus 5's $5/$25,
    # and silently defaulting every operator to the 2x-cost model is
    # not a defensible default. Fable sits immediately below so it is
    # one click away in the picker.
    "anthropic": [
        ("claude-opus-5", 0),
        ("claude-fable-5", 1),
        ("claude-sonnet-5", 2),
        ("claude-opus-4-8", 3),
        ("claude-opus-4-7", 4),
        ("claude-opus-4-6", 5),
        ("claude-sonnet-4-6", 6),
        ("claude-haiku-4-5", 7),
    ],
    "gemini": [
        ("gemini-3.1-pro", 0),
        ("gemini-3.6-flash", 1),
        ("gemini-3.5-flash-lite", 3),  # more specific than 3.5-flash
        ("gemini-3.5-flash", 2),
        ("gemini-3.1-flash", 4),
        ("gemini-3-pro", 5),
        ("gemini-3-flash", 6),
        ("gemini-2.5-pro", 7),
        ("gemini-2.5-flash", 8),
        ("gemini-pro-latest", 9),
        ("gemini-flash-latest", 10),
    ],
    "deepseek": [
        ("deepseek-v4-pro", 0),
        ("deepseek-v4-flash", 1),
    ],
    # OpenRouter's recommended set is a prefix match over 360+ routes
    # which arrive alphabetically sorted, so without a ladder the
    # default lands on whichever ``anthropic/…`` slug sorts first —
    # ``claude-fable-5``, the $10/$50 premium tier. Same reasoning as
    # the anthropic ladder above: lead with the cost-balanced flagship.
    "openrouter": [
        ("anthropic/claude-opus-5", 0),
        ("openai/gpt-5.6", 1),
        ("google/gemini-3.1-pro", 2),
        ("anthropic/claude-sonnet-5", 3),
        ("anthropic/claude-fable-5", 4),
    ],
    "groq": [
        ("llama-3.3-70b", 0),
        ("meta-llama/llama-4", 1),
        ("openai/gpt-oss-120b", 2),
        ("qwen/qwen3-32b", 3),
        ("llama-3.1-8b", 4),
        ("groq/compound", 5),
        ("openai/gpt-oss-20b", 6),
    ],
}


def _tier_rank(provider_id: str, model_id: str) -> int:
    rules = _TIER_RANK.get((provider_id or "").lower())
    if not rules:
        return 999  # providers without a ladder keep caller order
    for prefix, rank in rules:
        if model_id == prefix or model_id.startswith(prefix):
            return rank
    return 100  # recommended but outside the explicit ladder


def recommended_for(provider_id: str, all_models: list[str]) -> list[str]:
    """Filter ``all_models`` down to the recommended shortlist for
    ``provider_id`` and sort by tier priority.

    The first element of the returned list is the conductor's current
    "best pick" for the provider (gpt-5.5-pro for OpenAI,
    claude-opus-4-7 for Anthropic, deepseek-v4-pro for DeepSeek, etc.).
    Ties within a tier keep caller order so live-refresh ordering
    quirks don't churn the picker.

    Callers that want the raw chat-class list (unsorted, full) keep
    using ``BaseProvider.list_models(model_class="chat")`` without
    ``recommended=True``.
    """
    kept = [
        (i, m) for i, m in enumerate(all_models)
        if is_recommended(provider_id, m)
    ]
    kept.sort(key=lambda pair: (_tier_rank(provider_id, pair[1]), pair[0]))
    return [m for _, m in kept]
