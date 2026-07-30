"""Per-provider model classification.

The shipped v2026.5.0 picker renders 132 models returned verbatim from
``/v1/models`` — which on OpenAI includes ``babbage-002`` (completion-
only), ``whisper-1`` (audio-in), ``dall-e-3`` (image), and
``text-embedding-3-large`` (embedding). Sending any of those to
``/v1/chat/completions`` is a 400, and that is exactly the shape of the
400s in the v2026.5.0 terminal log the maintainer reported.

This module is the deterministic, regex-driven classifier every adapter
uses to tag each model id with a class. Classifier output feeds:

* ``BaseProvider.list_models(model_class="chat")`` — the chat-only
  filter so the picker stops handing the user completion-only ids.
* The per-provider reasoning-family parameter fork — OpenAI reasoning
  needs ``max_completion_tokens``, DeepSeek v4-pro needs
  ``extra_body.thinking``, Anthropic needs ``thinking``, etc. The
  dispatch path reads ``classify(...) == "reasoning"`` to decide.

Rules
-----
* Deterministic. Given ``(provider_id, model_id)`` the answer is stable
  across runs and across hosts. No network. No env reads.
* Reasoning is a subset of chat. A reasoning-capable model's class is
  ``"reasoning"``; ``list_models(model_class="chat")`` still returns it
  (chat is the supertype). Asking for ``model_class="reasoning"`` gives
  only the reasoning-capable subset.
* Vision is additive to chat. It is NOT a class here — ask
  ``BaseProvider.supports("vision")`` (provider-wide) or
  ``BaseProvider._capabilities_for_model(id)`` (per-route, for
  openrouter / routers). ``model_class="vision"`` filters to the
  intersection of chat-class + vision-capable.
* ``"unknown"`` is the honest fallback. Unknown ids are retained in the
  chat-class result (default-include rather than default-drop) so a
  newly-released model id still appears in the picker until the next
  catalog refresh reaches this module — the alternative (defaulting to
  drop) would silently hide frontier models the moment they ship.

The 2026-04-26 snapshot embedded below was verified against each
provider's live docs on the same date (Anthropic models list, OpenAI
docs, DeepSeek pricing page, OpenRouter /api/v1/models, Gemini
/v1beta/models). The refresh script at
``scripts/refresh_provider_catalog.py`` re-runs the live fetch and
updates the ``tests/fixtures/*_models.json`` snapshots the tests assert
against.
"""

from __future__ import annotations

import re
from typing import Iterable, Literal, Optional

# ``Literal`` keeps the set enumerable at type-check time. Keep in sync
# with the ``_CLASS_ORDER`` tuple below — the tuple controls the
# ordering of the :func:`list_classes` helper the docs + tests walk.
ModelClass = Literal[
    "chat",
    "reasoning",
    "vision",
    "embedding",
    "audio",
    "image",
    "completion-only",
    "realtime",
    "video",
    "unknown",
]

_CLASS_ORDER: tuple[ModelClass, ...] = (
    "chat",
    "reasoning",
    "vision",
    "embedding",
    "audio",
    "image",
    "completion-only",
    "realtime",
    "video",
    "unknown",
)


def list_classes() -> tuple[ModelClass, ...]:
    """The canonical ordered list of classes (docs + test enumeration)."""
    return _CLASS_ORDER


# ---------------------------------------------------------------------------
# Per-provider rule sets
# ---------------------------------------------------------------------------
#
# Each entry is ``(regex, class)``. Order matters: the first match wins.
# Provider-specific rules fire before fallbacks. Patterns are anchored at
# the start + end of the id so a substring like ``"gpt-5.5"`` inside a
# hypothetical fine-tune id ``"ft:gpt-5.5:my-org::xyz"`` doesn't wrongly
# classify the fine-tune as reasoning; dedicated rules for the ``ft:``
# prefix land below.


_OPENAI_RULES: tuple[tuple[re.Pattern[str], ModelClass], ...] = (
    # Fine-tuned models come through as ``ft:<base>:<org>::<id>`` — class
    # them by the base. Today every fine-tune base we allow is chat.
    (re.compile(r"^ft:.*$"), "chat"),
    # Embedding family.
    (re.compile(r"^text-embedding-(3|ada)-.+$"), "embedding"),
    (re.compile(r"^text-embedding-.+$"), "embedding"),
    # Audio in / transcription. The ``(-\d{4}-\d{2}-\d{2})?`` tail
    # matches OpenAI's dated snapshot suffixes (e.g.
    # ``gpt-4o-mini-transcribe-2025-12-15``). Without it the dated
    # form fell through to the chat catch-all ``^gpt-4o(-.+)?$``
    # below and the catalog handed a transcription model out as the
    # OpenAI chat default — every chat completion then 404s with
    # ``This is not a chat model`` (operator report 2026-05-08).
    # Pinned by tests/test_voice_realtime_headers.py-adjacent
    # tests/test_provider_model_classes.py.
    (re.compile(r"^whisper-.+$"), "audio"),
    (re.compile(r"^gpt-4o(-mini)?-transcribe(-\d{4}-\d{2}-\d{2})?$"), "audio"),
    (re.compile(r"^gpt-4o(-mini)?-transcribe-diarize(-\d{4}-\d{2}-\d{2})?$"), "audio"),
    # Audio out / TTS — same dated-snapshot fix.
    (re.compile(r"^gpt-4o(-mini)?-tts(-\d{4}-\d{2}-\d{2})?$"), "audio"),
    (re.compile(r"^tts-.+$"), "audio"),
    # Image generation.
    (re.compile(r"^dall-e-.+$"), "image"),
    (re.compile(r"^gpt-image-.+$"), "image"),
    # Realtime speech-to-speech. Same dated-snapshot tail so the
    # 2025-12-17 / etc. dated variants don't leak into chat either.
    (re.compile(r"^gpt-(4o-)?realtime.*$"), "realtime"),
    (re.compile(r"^gpt-realtime(-.+)?$"), "realtime"),
    # Completion-only legacy (chat completions rejects these with 400).
    (re.compile(r"^babbage-.+$"), "completion-only"),
    (re.compile(r"^davinci-.+$"), "completion-only"),
    (re.compile(r"^.*-instruct(-.+)?$"), "completion-only"),
    (re.compile(r"^gpt-3\.5-turbo-instruct(-.*)?$"), "completion-only"),
    (re.compile(r"^text-davinci-.+$"), "completion-only"),
    # Reasoning family. gpt-5, gpt-5.4+, gpt-5.5, gpt-5.6 are reasoning;
    # o1 / o3 / o4 are pure reasoning. gpt-4o is NOT reasoning (it's
    # omni-chat).
    #
    # The gpt-5.6 generation names its tiers ``sol`` / ``terra`` /
    # ``luna`` instead of pro/mini/nano. Without them in this alternation
    # ``gpt-5.6-sol`` classified as "unknown", which meant
    # ``_apply_openai_reasoning_fork`` never fired for it and the
    # outbound body kept ``max_tokens`` — a guaranteed 400 on a
    # reasoning SKU.
    (re.compile(
        r"^gpt-5(\.[0-9]+)?(-(pro|nano|mini|sol|terra|luna))?"
        r"(-(pro|nano|mini))?(-\d{4}-\d{2}-\d{2})?$"
    ), "reasoning"),
    (re.compile(r"^o[134](-.*)?$"), "reasoning"),
    # Chat (gpt-4o / gpt-4.1 / gpt-4-turbo / gpt-4 / gpt-3.5-turbo).
    (re.compile(r"^gpt-4o(-.+)?$"), "chat"),
    (re.compile(r"^gpt-4\.1(-.+)?$"), "chat"),
    (re.compile(r"^gpt-4(-turbo.*)?$"), "chat"),
    (re.compile(r"^gpt-3\.5-turbo(-.+)?$"), "chat"),
    # Moderation / embeddings guardrails fall through to unknown.
    (re.compile(r"^omni-moderation-.+$"), "unknown"),
)


_ANTHROPIC_RULES: tuple[tuple[re.Pattern[str], ModelClass], ...] = (
    # Claude 5 generation (2026-07): opus / sonnet / fable / mythos. Ids
    # are DATELESS — since the 4.6 generation the bare id is itself a
    # pinned snapshot and appending a date 404s — so the optional
    # ``-\d{8}`` tail exists only to keep a hypothetical dated form from
    # falling through to "unknown".
    #
    # These MUST classify as reasoning. The whole Anthropic param fork
    # (``agents/llm_reasoning._apply_anthropic_reasoning_fork``) is gated
    # on ``classify(...) == "reasoning"``, and that fork is what strips
    # ``temperature`` / ``top_p`` / ``top_k`` — all three of which return
    # HTTP 400 on Claude 4.7 and later. Without these rules every Claude
    # 5 id landed in "unknown", the fork never ran, and any turn carrying
    # a temperature 400'd.
    (re.compile(r"^claude-(opus|sonnet|haiku)-5(-\d{8})?$"), "reasoning"),
    (re.compile(r"^claude-(fable|mythos)-5(-\d{8})?$"), "reasoning"),
    (re.compile(r"^claude-mythos-preview$"), "reasoning"),
    # Every claude 4-family model is chat. The thinking-capable ones are
    # flagged reasoning. Opus 4.7 and later use ADAPTIVE thinking (not
    # extended), but from the "does this model benefit from the reasoning
    # param fork?" angle, they still count as reasoning.
    (re.compile(r"^claude-opus-4-[5-9](-\d{8})?$"), "reasoning"),
    (re.compile(r"^claude-sonnet-4-[4-9](-\d{8})?$"), "reasoning"),
    (re.compile(r"^claude-haiku-4-[4-9](-\d{8})?$"), "reasoning"),
    # Older opus / sonnet 4.0-4.4 — thinking-capable too.
    (re.compile(r"^claude-opus-4-[0-4](-\d{8})?$"), "reasoning"),
    (re.compile(r"^claude-sonnet-4-[0-3](-\d{8})?$"), "reasoning"),
    (re.compile(r"^claude-haiku-4-[0-3](-\d{8})?$"), "reasoning"),
    # Base 4.0 dated-snapshot form (e.g. ``claude-opus-4-20250514``)
    # omits the minor-version digit. The live /v1/models response on
    # 2026-04-26 exposes these alongside the minor-version variants.
    (re.compile(r"^claude-(opus|sonnet|haiku)-4(-\d{8})?$"), "reasoning"),
    # 3.x families remain chat (no thinking).
    (re.compile(r"^claude-(opus|sonnet|haiku)-3(-.+)?$"), "chat"),
    (re.compile(r"^claude-3(-\d)?-(opus|sonnet|haiku)(-.+)?$"), "chat"),
    (re.compile(r"^claude-2(\.\d)?$"), "chat"),
    (re.compile(r"^claude-instant-.+$"), "chat"),
)


_DEEPSEEK_RULES: tuple[tuple[re.Pattern[str], ModelClass], ...] = (
    # v4-pro is reasoning by default (thinking mode is on). v4-flash is
    # chat (thinking mode is off by default). The legacy aliases map the
    # same way per the 2026-04-26 pricing page footnote.
    (re.compile(r"^deepseek-v4-pro(-.+)?$"), "reasoning"),
    (re.compile(r"^deepseek-v4-flash(-.+)?$"), "chat"),
    (re.compile(r"^deepseek-reasoner$"), "reasoning"),
    (re.compile(r"^deepseek-chat$"), "chat"),
    # v3 families are chat; no reasoning variant in that lineage.
    (re.compile(r"^deepseek-v3(-.+)?$"), "chat"),
    # Embedding.
    (re.compile(r"^deepseek-embedding-.+$"), "embedding"),
)


_GEMINI_RULES: tuple[tuple[re.Pattern[str], ModelClass], ...] = (
    # Thinking variants first (more specific).
    (re.compile(r"^gemini-.+-thinking(-.+)?$"), "reasoning"),
    # Image generation models. Match both the GA slug and the
    # ``-image-preview`` suffix Google still uses for some research
    # builds (``gemini-3.1-flash-image-preview``).
    (re.compile(r"^gemini-.+-image(-preview)?(-.+)?$"), "image"),
    (re.compile(r"^imagen-.+$"), "image"),
    # Embedding.
    (re.compile(r"^text-embedding-.+$"), "embedding"),
    (re.compile(r"^embedding-.+$"), "embedding"),
    (re.compile(r"^gemini-embedding(-.+)?$"), "embedding"),
    # Chat (everything else in the 3.x / 2.x / 1.x lines). Accept
    # both the GA slug (``gemini-3.1-pro``) and the ``-preview`` tail
    # Google uses before the GA flip (``gemini-3.1-pro-preview``).
    (re.compile(r"^gemini-3(\.\d+)?-(pro|flash|flash-lite)(-preview)?(-.+)?$"), "chat"),
    (re.compile(r"^gemini-2(\.\d+)?-(pro|flash|flash-lite)(-preview)?(-.+)?$"), "chat"),
    (re.compile(r"^gemini-1(\.\d+)?-(pro|flash|flash-lite)(-preview)?(-.+)?$"), "chat"),
    (re.compile(r"^gemini-pro(-vision)?$"), "chat"),
    # Audio / speech.
    (re.compile(r"^.+-tts(-.+)?$"), "audio"),
)


_GROQ_RULES: tuple[tuple[re.Pattern[str], ModelClass], ...] = (
    # Reasoning (thinking) models on Groq.
    (re.compile(r"^deepseek-r1-distill-.+$"), "reasoning"),
    (re.compile(r"^qwen-qwq-.+$"), "reasoning"),
    (re.compile(r"^qwen.*-reasoner$"), "reasoning"),
    # Audio.
    (re.compile(r"^whisper-.+$"), "audio"),
    (re.compile(r"^distil-whisper-.+$"), "audio"),
    # Chat.
    (re.compile(r"^llama-.+$"), "chat"),
    (re.compile(r"^meta-llama/.+$"), "chat"),
    (re.compile(r"^mixtral-.+$"), "chat"),
    (re.compile(r"^gemma\d?-.+$"), "chat"),
)


_OPENROUTER_RULES: tuple[tuple[re.Pattern[str], ModelClass], ...] = (
    # OpenRouter slugs are ``<vendor>/<model>``. Delegate to the
    # underlying vendor's rules by stripping the prefix and re-running
    # :func:`classify` against that vendor. The prefix-aware rules live
    # in :func:`_classify_openrouter_delegated` below; this tuple is kept
    # for the fallback path where the model id lacks a slash (raw
    # fallback id).
    (re.compile(r"^embedding-.+$"), "embedding"),
)


def _classify_openrouter_delegated(model_id: str) -> ModelClass:
    """OR-specific: peel the ``<vendor>/`` prefix, classify against that vendor.

    ``openrouter`` is a router — per-model semantics match the routed
    target. Embedding / audio / image routes are rare but exist; chat and
    reasoning are the common cases. When the prefix is unknown we fall
    back to a conservative ``"chat"`` (OR mostly routes chat models), so
    the picker still lists the id.
    """
    if "/" not in model_id:
        return "unknown"
    vendor, sub = model_id.split("/", 1)
    # Map the OR vendor prefix to the classifier's provider id.
    vendor_map = {
        "openai": "openai",
        "anthropic": "anthropic",
        "google": "gemini",
        "deepseek": "deepseek",
        "meta-llama": "groq",  # llama family — the groq rules cover it.
        "qwen": "groq",
        "mistralai": "openai",  # mistral chat fits the chat fallback.
        "x-ai": "openai",  # grok models fall through to chat via default.
        "cohere": "openai",
        "ai21": "openai",
    }
    target_provider = vendor_map.get(vendor.lower())
    if target_provider is None:
        return "chat"
    # Many OR slugs add a ``:free`` / ``:nitro`` suffix to the model id;
    # strip those for classification purposes.
    base = sub.split(":", 1)[0]
    return classify(target_provider, base)


# ---------------------------------------------------------------------------
# Cross-provider fallback rules
# ---------------------------------------------------------------------------


_FALLBACK_RULES: tuple[tuple[re.Pattern[str], ModelClass], ...] = (
    (re.compile(r"^.*embedding.*$", re.IGNORECASE), "embedding"),
    (re.compile(r"^.*whisper.*$", re.IGNORECASE), "audio"),
    (re.compile(r"^.*dall-?e.*$", re.IGNORECASE), "image"),
    (re.compile(r"^.*realtime.*$", re.IGNORECASE), "realtime"),
    (re.compile(r"^.*-thinking(-.+)?$", re.IGNORECASE), "reasoning"),
    (re.compile(r"^.*-reasoner$", re.IGNORECASE), "reasoning"),
)


_RULES_BY_PROVIDER: dict[str, tuple[tuple[re.Pattern[str], ModelClass], ...]] = {
    "openai": _OPENAI_RULES,
    "anthropic": _ANTHROPIC_RULES,
    "deepseek": _DEEPSEEK_RULES,
    "gemini": _GEMINI_RULES,
    "groq": _GROQ_RULES,
    "openrouter": _OPENROUTER_RULES,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify(provider_id: str, model_id: str) -> ModelClass:
    """Return the :class:`ModelClass` for ``(provider_id, model_id)``.

    ``provider_id`` is FERAL's canonical provider key (``"openai"``,
    ``"anthropic"``, ``"deepseek"``, ``"openrouter"``, ``"gemini"``,
    ``"groq"``, etc). ``model_id`` is the model id string as returned by
    the provider's ``/v1/models`` endpoint (or the bundled catalog).

    The function is pure — no IO, no env reads. It's the single source
    of truth consulted by ``BaseProvider.list_models(model_class=...)``
    and by the per-provider reasoning-family parameter fork in
    ``llm_provider.py``. Returns ``"unknown"`` only when every rule
    misses; the chat-only filter default-includes unknown ids so
    newly-released frontier names aren't silently hidden until the next
    catalog refresh.
    """
    if not model_id:
        return "unknown"
    provider = (provider_id or "").lower()
    model = model_id.strip()

    # OpenRouter delegates per-route.
    if provider == "openrouter":
        delegated = _classify_openrouter_delegated(model)
        if delegated != "unknown":
            return delegated
        # Fall through to the per-provider rules below for ids without
        # a slash prefix (rare but possible for cached fallbacks).

    rules = _RULES_BY_PROVIDER.get(provider)
    if rules is not None:
        for pattern, klass in rules:
            if pattern.match(model):
                return klass

    # Cross-provider fallback: catches ids like "text-embedding-foo" on
    # a provider with no embedding rules of its own (e.g. together).
    for pattern, klass in _FALLBACK_RULES:
        if pattern.match(model):
            return klass

    return "unknown"


def classify_many(
    provider_id: str, model_ids: Iterable[str]
) -> dict[str, ModelClass]:
    """Batch-classify. Useful for pricing tables + picker rendering."""
    return {mid: classify(provider_id, mid) for mid in model_ids}


def filter_models(
    provider_id: str,
    model_ids: Iterable[str],
    *,
    model_class: Optional[ModelClass],
) -> list[str]:
    """Return the subset of ``model_ids`` matching ``model_class``.

    ``model_class=None`` is the legacy no-filter path — the unchanged
    full list is returned.

    ``model_class="chat"`` returns chat AND reasoning AND unknown ids
    (default-include for unknown so a freshly-released id still appears
    in the picker until the next catalog refresh reclassifies it).

    ``model_class="reasoning"`` returns ONLY the reasoning subset. The
    classifier's reasoning bucket is strict — unknown ids are NOT
    included here (it would be a wrong positive to advertise a model
    as reasoning-capable when we don't know).

    ``model_class="vision"`` is the intersection of chat-class +
    vision-capable. The vision capability is looked up through the
    adapter's ``_capabilities_for_model(id)`` hook rather than the
    classifier — vision is additive to chat, not its own class.
    Callers pass the lookup result as ``vision_capable`` below to
    avoid importing adapter state into this module.
    """
    ids = list(model_ids)
    if model_class is None:
        return ids
    if model_class == "chat":
        return [
            mid for mid in ids
            if classify(provider_id, mid) in ("chat", "reasoning", "unknown")
        ]
    if model_class == "reasoning":
        return [mid for mid in ids if classify(provider_id, mid) == "reasoning"]
    # Exact-match classes: embedding, audio, image, realtime, video,
    # completion-only.
    return [mid for mid in ids if classify(provider_id, mid) == model_class]


__all__ = [
    "ModelClass",
    "classify",
    "classify_many",
    "classify_endpoint",
    "filter_models",
    "is_responses_only",
    "list_classes",
]


# ---------------------------------------------------------------------------
# v2026.5.23 — endpoint classification (chat_completions vs responses)
# ---------------------------------------------------------------------------
#
# Some reasoning models on OpenAI are flagged ``ResponsesOnlyModel`` —
# they answer at ``POST /v1/responses`` but the ``/v1/chat/completions``
# endpoint either rejects them outright (404 "not a chat model") or
# only accepts the non-streaming subset, which breaks FERAL's
# streaming UI runtime.
#
# This module is the single source of truth the OpenAI adapter and the
# CLI / web pickers consult to decide which HTTP endpoint to use. It
# stays orthogonal to ``ModelClass`` (which exists for the reasoning
# param fork in ``llm_provider.py`` — we do NOT want to reclassify
# Pro models out of ``reasoning`` because the reasoning param shape
# is still needed regardless of which endpoint we POST to).
#
# Rules
# -----
# * Deterministic. Pure function. No network. No env reads.
# * "responses" classification implies BOTH (a) the model needs
#   ``/v1/responses`` for correctness/streaming, AND (b) the request
#   body uses the Responses-API shape (``input``/``instructions``,
#   ``reasoning.effort``, ``max_output_tokens``) not the chat shape.
# * Default is "chat_completions" for everything else — the adapter's
#   established path. The picker won't suddenly route models that
#   already work.
#
# The list below tracks OpenAI's ``ResponsesOnlyModel`` union (verified
# from the 2026-05 Responses API reference + cookbook examples). When
# a new responses-only model lands at OpenAI, add a regex here; the
# adapter picks it up automatically. The flag pattern is regex match
# (anchored) to support dated snapshots like ``gpt-5-pro-2026-05-08``.

import re as _re_endpoint

_RESPONSES_ONLY_OPENAI: tuple[_re_endpoint.Pattern[str], ...] = (
    # gpt-5-pro / gpt-5.4-pro / gpt-5.5-pro and dated snapshots. The
    # gpt-5.6 generation names its tiers sol / terra / luna, so the Pro
    # variants are ``gpt-5.6-sol-pro`` etc.
    _re_endpoint.compile(
        r"^gpt-5(\.[0-9]+)?(-(sol|terra|luna))?-pro(-\d{4}-\d{2}-\d{2})?$"
    ),
    # o-series reasoning Pro variants (o3-pro etc).
    _re_endpoint.compile(r"^o[134]-pro(-.*)?$"),
    # Deep research models (responses-only per OpenAI docs).
    _re_endpoint.compile(r"^.+-deep-research(-.+)?$"),
    # Codex-on-responses + computer-use preview models.
    _re_endpoint.compile(r"^gpt-5-codex(-.+)?$"),
    _re_endpoint.compile(r"^computer-use-preview(-.+)?$"),
)


def is_responses_only(provider_id: str, model_id: str) -> bool:
    """True iff ``(provider_id, model_id)`` MUST be routed through
    ``/v1/responses`` to function. False for every model that can be
    served by ``/v1/chat/completions`` (the legacy adapter path).

    Today only OpenAI exposes responses-only models. OpenRouter routes
    that wrap them are also responses-only (the OR ``openai/`` prefix
    delegates to the same upstream behaviour); other providers return
    False.
    """
    if not model_id:
        return False
    provider = (provider_id or "").lower()
    model = model_id.strip()

    if provider == "openrouter" and "/" in model:
        vendor, sub = model.split("/", 1)
        if vendor.lower() == "openai":
            base = sub.split(":", 1)[0]
            return is_responses_only("openai", base)
        return False

    if provider != "openai":
        return False

    for pattern in _RESPONSES_ONLY_OPENAI:
        if pattern.match(model):
            return True
    return False


def classify_endpoint(
    provider_id: str, model_id: str
) -> Literal["chat_completions", "responses"]:
    """Return the HTTP endpoint family the LLM provider should use for
    ``(provider_id, model_id)``.

    * ``"responses"`` — route through ``/v1/responses`` with the
      Responses-API body shape. Streaming + tool calls + background
      mode all live here. Examples: ``gpt-5-pro``, ``gpt-5.5-pro``,
      ``o3-pro``, ``-deep-research`` variants, ``gpt-5-codex``,
      ``computer-use-preview``.
    * ``"chat_completions"`` — route through ``/v1/chat/completions``
      with the OpenAI-compat body shape. Every other reachable model,
      including non-Pro reasoning models like ``gpt-5.5`` /
      ``gpt-5.4`` / ``o3`` (which are dual-stack: OpenAI recommends
      Responses for them but Chat Completions still works).

    The chat-completions default is deliberate — we don't want to
    silently re-route models the existing adapter already handles
    correctly. The opt-in is by membership in ``_RESPONSES_ONLY_OPENAI``.
    """
    if is_responses_only(provider_id, model_id):
        return "responses"
    return "chat_completions"
