"""
FERAL LLM Provider — Pluggable AI Backend
=====================================================
Supports: OpenAI API, Ollama (local), LM Studio (local),
and any OpenAI-compatible endpoint.
Now with streaming support for real-time token delivery.
"""

from __future__ import annotations
import asyncio
import os
import json
import logging
import time
import uuid
import httpx
from typing import Any, Optional, AsyncGenerator

from config.loader import feral_data_home
from config.runtime import ollama_base_url, ollama_openai_base_url
from agents.chat_sanitizer import sanitize_assistant_display_text

# -A15: failover/retry, reasoning request-body shaping, and Anthropic
# transcript shaping live in focused sibling modules. They are imported
# (and re-exported below) so the public API of ``agents.llm_provider``
# is unchanged for existing callers and tests.
from agents.llm_failover import (
    MAX_RETRIES,
    RETRY_DELAYS,
    RETRY_AFTER_MAX_INLINE_SLEEP,
    RETRY_AFTER_MAX_COOLDOWN,
    _RETRIABLE_CODES,
    _SSE_KEEPALIVE_PREFIXES,
    _retry_llm_call,
    parse_retry_after,
    FailoverReason,
    classify_error,
    _describe_http_status_error,
    _describe_error,
    _chat_completions_model_guard,
    ProviderCooldownTracker,
)

# When the failover loop has more than one viable candidate, we cap
# same-provider retries to a single fast attempt. The historical
# 3 × [1, 2, 4]s policy meant up to 7s of dead air on a transient 5xx
# before *any* fallback got tried. With multiple candidates available,
# spending that budget on a known-bad provider is the wrong trade.
_FAILOVER_FAST_MAX_RETRIES = 2
_FAILOVER_FAST_DELAYS: list[float] = [0.5]

# Stable for the life of this process, sent as ``prompt_cache_key`` on
# every /v1/responses request. See ``_build_responses_body``.
_PROMPT_CACHE_KEY = f"feral-{uuid.uuid4().hex[:16]}"
from agents.llm_reasoning import (
    _apply_openai_reasoning_fork,
    _apply_deepseek_reasoning_fork,
    _apply_gemini_reasoning_fork,
    _apply_anthropic_reasoning_fork,
    _apply_groq_reasoning_fork,
    apply_reasoning_fork,
)
from agents.llm_anthropic_shape import (
    _ANTHROPIC_THINKING_RESPONSE_ROOM,
    _convert_messages_for_anthropic,
    _enforce_anthropic_thinking_max_tokens,
)
from agents.multimodal_blocks import (
    to_provider_tool_choice,
    tool_list_contains,
)
from agents.tool_list import OPENAI_TOOL_HARD_LIMIT, cap_tools_with_pins
from agents.token_estimate import estimate_message_tokens
from agents.context_manager import configured_context_window_tokens

# Cost-budget surface (Wave 1 Lane 04). The runtime gate lives on the
# public chat entry points — see ``_budget_check`` /
# ``_budget_record`` / ``_budget_exceeded_response`` below.
try:
    from cost.budget import BudgetExceeded
except Exception:  # pragma: no cover - defensive
    class BudgetExceeded(Exception):  # type: ignore[no-redef]
        """Stand-in when the cost module is unavailable in stripped builds."""

logger = logging.getLogger("feral.llm")


# Endpoints (provider, base_url) known to reject
# ``stream_options: {"include_usage": true}`` on /chat/completions.
#
# The key is opt-IN on OpenAI's chat-completions API: without it the
# provider sends no usage chunk at all, so every streamed turn billed
# at ZERO tokens (see ``chat_stream``). We therefore send it by
# default. But FERAL points that same code at openrouter, deepseek,
# groq, kimi, qwen and local servers (Ollama / LM Studio / vLLM /
# llama.cpp), and strict OpenAI-compatible shims reject unknown body
# keys with a 400 instead of ignoring them. When that happens we retry
# the SAME turn once without the key and remember the endpoint here,
# so the degradation is "no usage data for that provider" and never
# "the turn fails". Entries are added only after the retry SUCCEEDS,
# so a 400 that was really about something else (bad model, bad tool
# schema) does not permanently disable usage capture.
#
# Process-lifetime cache: cleared on restart, which is also when an
# operator would have upgraded the local server that gained support.
_STREAM_OPTIONS_UNSUPPORTED: set[tuple[str, str]] = set()


def _usage_event_payload(raw: Any) -> Optional[dict]:
    """Normalise a provider usage block into the ``done`` event shape.

    Accepts both the OpenAI chat-completions naming
    (``prompt_tokens`` / ``completion_tokens``) and the Anthropic /
    Responses naming (``input_tokens`` / ``output_tokens``) and emits
    the latter, matching what ``_responses_stream`` already puts on its
    terminal event so the UI has one shape to read.

    Returns ``None`` when the block carries no usable numbers, so
    callers can distinguish "provider sent no usage" from "provider
    sent zeros" and avoid inventing spend.
    """
    if not isinstance(raw, dict):
        return None
    _in = raw.get("input_tokens")
    if _in is None:
        _in = raw.get("prompt_tokens")
    _out = raw.get("output_tokens")
    if _out is None:
        _out = raw.get("completion_tokens")
    if _in is None and _out is None:
        return None
    try:
        _in = int(_in or 0)
        _out = int(_out or 0)
        _total = int(raw.get("total_tokens") or (_in + _out))
    except (TypeError, ValueError):
        return None
    return {"input_tokens": _in, "output_tokens": _out, "total_tokens": _total}


def _gemini_api_key() -> str | None:
    """Return Gemini API key. Prefers GEMINI_API_KEY; falls back to GOOGLE_API_KEY."""
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def _cap_openai_chat_tools(clean_tools: list[dict]) -> list[dict]:
    """Pin essential automation tools before applying OpenAI's 128 cap."""
    return cap_tools_with_pins(clean_tools, max_tools=OPENAI_TOOL_HARD_LIMIT)


def _resolve_tool_choice(
    provider: str,
    tools: Optional[list[dict]],
    force_tool: Optional[str],
) -> Any:
    """Return the ``tool_choice`` wire value for a chat-completions body.

    Centralises the v2026.5.48 grounded-memory closure: when the
    orchestrator classifies a temporal-recall turn it asks the LLM
    layer to FORCE the model to call ``notes_memory__fused_timeline``
    (so the prose answer is grounded in the retrieved tool result
    rather than narrated from working-memory context). The per-provider
    wire shape lives in :mod:`agents.multimodal_blocks`.

    Guards against forcing a tool the model wasn't given — when
    ``force_tool`` is not present in ``tools`` (typo, routing skipped
    the right skill, the tool was filtered out by the 128-tool cap),
    silently degrade to ``"auto"`` so the call still succeeds.

    Returns ``"auto"`` for the default (legacy) path so callers can
    do a single unconditional ``body["tool_choice"] = <result>`` at
    every site without branching.
    """
    if force_tool and tool_list_contains(tools, force_tool):
        translated = to_provider_tool_choice(provider, force_tool)
        if translated is not None:
            return translated
    return "auto"


def _resolve_api_key(provider: str) -> str:
    """Hot-path credential resolver — labeled vault → legacy vault → env.

    Thin wrapper around :func:`security.vault_keys.get_active_key` so
    every call site in this module shares the same resolution order
    (Cross-cut #1 of v2026.5.42 wave). Returns ``""`` when nothing is
    configured so the caller can flip ``available=False`` instead of
    sending a bare ``Authorization: Bearer`` header upstream.

    Import is lazy so a stripped build without the security overlay
    (e.g. early bootstrap, unit tests that monkeypatch the vault) can
    still construct an ``LLMProvider`` with env-only credentials.
    """
    try:
        from security.vault_keys import get_active_key
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("_resolve_api_key(%s): vault_keys import failed (%s)", provider, exc)
        return ""
    try:
        return get_active_key(provider) or ""
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("_resolve_api_key(%s): get_active_key raised (%s)", provider, exc)
        return ""


VISION_READY_OLLAMA_MODELS = (
    "llava",
    "moondream",
    "qwen2-vl",
    "minicpm-v",
    "bakllava",
    "gemma3",
)




def _openrouter_route_supports_vision(model: str) -> tuple[bool, str]:
    """Check whether the selected OpenRouter route handles vision.

    When the router catalog has a capability snapshot for *model* we
    consult it. Otherwise we trust the router-level superset (which
    now includes vision) and return True — sending an image to a
    non-vision route will still 400 upstream, but that's a targeted
    error instead of our old "openrouter does not support vision"
    blanket ban.
    """
    try:
        from providers.catalog import get_shared_catalog
        catalog = get_shared_catalog()
        adapter = catalog.get_adapter("openrouter")
    except Exception:
        adapter = None
    if adapter is None:
        return True, ""
    caps_for = getattr(adapter, "_capabilities_for_model", None)
    if callable(caps_for) and model:
        caps = set(caps_for(model) or ())
        if caps and "vision" not in caps:
            return False, (
                f"Selected OpenRouter route {model!r} does not accept image "
                "inputs. Pick a vision-capable route (e.g. "
                "'anthropic/claude-opus-4-7', 'openai/gpt-5.5', "
                "'google/gemini-3.1-pro')."
            )
    return True, ""

# Empty ``model`` strings tell ``apply_preset`` → ``switch_provider``
# to resolve the default via the shared catalog (Roadmap §3.5 P0).
# Hardcoding a frontier name here drifts every quarter — the catalog
# does not.
LLM_PRESETS = {
    "ollama_text": {
        "provider": "ollama",
        "model": "",
        "description": "Local text path on Ollama (uses first installed text model)",
        "vision_supported": False,
    },
    "ollama_vision": {
        "provider": "ollama",
        "model": "llava",
        "description": "Local vision path on Ollama VLM",
        "vision_supported": True,
    },
    "openai_default": {
        "provider": "openai",
        "model": "",
        "description": "Cloud default for balanced latency/quality",
        "vision_supported": True,
    },
}



# Per-provider HTTP base URL + credential env var. Default model used to
# live in this tuple — it has been stripped because hardcoded model
# literals here drift the moment a provider ships a new frontier name
# (Roadmap §3.5 P0). The runtime now resolves the default model lazily
# through ``get_shared_catalog().default_model_for(pid)`` (see
# ``_default_model_for``) so the catalog's bundled / live model list is
# the single source of truth.
_PROVIDER_REGISTRY: dict[str, tuple[str, str]] = {
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "anthropic": ("https://api.anthropic.com/v1", "ANTHROPIC_API_KEY"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    # DeepSeek's documented OpenAI-compat base URL is ``/v1``. 
    # this entry was missing the ``/v1`` suffix while the adapter and
    # ``__init__`` defaulted to ``/v1`` — the divergence meant
    # failover candidates resolved through ``_get_provider_config``
    # hit ``api.deepseek.com/chat/completions`` (404) while the
    # primary path worked. See findings/13-llm-core.md fix #3.
    "deepseek": ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    # The Moonshot API host is api.moonshot.**ai**. The DOCS host moved
    # to platform.kimi.ai but the API host did NOT — and the previous
    # value here (api.moonshot.**cn**) was a different, older estate.
    "kimi": ("https://api.moonshot.ai/v1", "MOONSHOT_API_KEY"),
    # Qwen's OpenAI-compatible host is now WORKSPACE-SCOPED. The literal
    # below is the documented template, not a dialable host —
    # ``_resolve_workspace_base_url`` substitutes {WorkspaceId} from
    # DASHSCOPE_WORKSPACE_ID at connect time. The old
    # dashscope.aliyuncs.com/compatible-mode/v1 endpoint this replaced
    # is no longer the documented path.
    "qwen": (
        "https://[{WorkspaceId}].ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
        "DASHSCOPE_API_KEY",
    ),
    # xai had catalog data but no runtime binding — the catalog
    # advertised Grok models the brain could not actually dispatch to.
    # Its API is OpenAI-compatible, so the binding is one line.
    "xai": ("https://api.x.ai/v1", "XAI_API_KEY"),
    "zai": ("https://api.z.ai/api/paas/v4", "ZAI_API_KEY"),
    "minimax": ("https://api.minimax.io/v1", "MINIMAX_API_KEY"),
    "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    "lmstudio": ("http://localhost:1234/v1", ""),
}


def _resolve_workspace_base_url(base_url: str) -> str:
    """Substitute ``{WorkspaceId}`` in a workspace-scoped base URL.

    Alibaba moved Qwen's OpenAI-compatible endpoint to a per-workspace
    host. The registry stores the documented template; the operator's
    workspace id comes from ``DASHSCOPE_WORKSPACE_ID`` (or
    ``QWEN_WORKSPACE_ID``). When neither is set the template is returned
    unchanged so the caller surfaces an honest "unresolved base URL"
    error instead of dialling a host literally named ``{WorkspaceId}``.
    """
    if "{WorkspaceId}" not in (base_url or ""):
        return base_url
    workspace = (
        os.environ.get("DASHSCOPE_WORKSPACE_ID")
        or os.environ.get("QWEN_WORKSPACE_ID")
        or ""
    ).strip()
    if not workspace:
        return base_url
    return base_url.replace("[{WorkspaceId}]", workspace).replace(
        "{WorkspaceId}", workspace
    )


# Catalog id ↔ legacy llm_provider id. The catalog only knows
# "anthropic", "moonshot" etc. but llm_provider historically used
# "anthropic", "kimi" — keep this map small and explicit so no caller
# has to remember the translation.
_CATALOG_PROVIDER_MAP: dict[str, str] = {
    "kimi": "moonshot",
}

# Reverse of the above: catalog id -> runtime id. Needed by any caller that
# starts from a ``ProviderDescriptor`` (the setup wizard picker, the v2
# /setup page) and wants to know whether the runtime can dial it.
_RUNTIME_PROVIDER_MAP: dict[str, str] = {
    catalog_id: runtime_id for runtime_id, catalog_id in _CATALOG_PROVIDER_MAP.items()
}


def is_supported_catalog_provider(catalog_id: str) -> bool:
    """``is_supported_runtime_provider`` keyed by a *catalog* provider id.

    The catalog and the runtime do not always agree on a provider's key:
    the catalog calls Moonshot ``moonshot`` while the runtime binding is
    ``kimi``. Calling ``is_supported_runtime_provider("moonshot")`` therefore
    returns False for a provider that works perfectly, which made the setup
    wizard hide Kimi from the picker entirely. Resolve through the map first.
    """
    return is_supported_runtime_provider(
        _RUNTIME_PROVIDER_MAP.get(catalog_id, catalog_id)
    )


# Canonical set of provider ids the runtime can actually execute chat
# calls against. This is the single source of truth consulted by
# ``_get_provider_config``, ``switch_provider``, ``__init__``,
# ``health_snapshot`` and ``is_available`` so unknown provider ids
# (catalog-registered descriptors without a runtime binding, user
# typos, deprecated aliases) can never silently masquerade as OpenAI
# at request time. Previously every one of those call sites had its
# own implicit ``... or OPENAI defaults`` branch — removing that
# fallback is the whole point of this module-level constant.
SUPPORTED_RUNTIME_PROVIDERS: frozenset[str] = frozenset({
    *_PROVIDER_REGISTRY.keys(),  # cloud + lmstudio from the registry
    "codex",                     # ChatGPT sign-in via Codex app-server
    "ollama",                    # local, base url derived dynamically
    "local",                     # on-device inference engine
    "hybrid",                    # local + cloud splitter
})


def is_supported_runtime_provider(provider_name: str) -> bool:
    """True when *provider_name* has a runtime binding in this module.

    The check is intentionally narrower than ``ProviderCatalog`` —
    the catalog exposes every descriptor the UI can render (e.g.
    ``bedrock``, ``together``, ``fireworks``), but those providers
    have no OpenAI-compatible runtime adapter here yet. Returning
    False keeps the runtime from silently dialling OpenAI for them.
    """
    return (provider_name or "") in SUPPORTED_RUNTIME_PROVIDERS


# Model classes that cannot serve a chat-completions call. ``chat``,
# ``reasoning`` and ``vision`` obviously can; ``unknown`` is deliberately
# treated as chat-capable, matching the documented policy on
# ``providers.model_classes.classify`` -- a newly released frontier id that
# no rule matches yet must not be silently excluded until the next catalog
# refresh. ``realtime`` is excluded because the Realtime API is a separate
# WebSocket surface, not /v1/chat/completions.
_NON_CHAT_MODEL_CLASSES: frozenset[str] = frozenset({
    "embedding", "audio", "image", "completion-only", "realtime", "video",
})


def _chat_capability_of(provider_name: str, model_id: str) -> tuple[bool, str]:
    """Return ``(is_chat_capable, model_class)`` for a failover candidate.

    Pure and cheap: ``classify`` does no IO. Used to drop a candidate
    before the wire call rather than paying a round trip to learn the same
    fact from a 404. Failures to classify resolve to chat-capable, because
    refusing to dial on an inconclusive signal would be worse than the 404
    it is trying to avoid.

    Imported lazily to match the existing ``classify_endpoint`` call sites
    in this module.
    """
    if not model_id:
        return True, "unknown"
    try:
        from providers.model_classes import classify
        model_class = str(classify(provider_name, model_id))
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("model-class lookup failed for %s/%s: %s", provider_name, model_id, exc)
        return True, "unknown"
    return model_class not in _NON_CHAT_MODEL_CLASSES, model_class


def _default_model_for(provider_name: str) -> str:
    """Return the catalog's current default model id for *provider_name*.

    Returns ``""`` when the catalog is unavailable or the provider is
    unknown. Callers MUST NOT substitute a hardcoded literal — surface
    the empty string back to the user / settings UI so the picker
    renders an honest "no model selected" state instead of a stale
    guess like ``gpt-4o-mini``. The roadmap §3.5 P0 ban on hardcoded
    defaults exists because those literals went stale every quarter
    and shipped to production unnoticed.
    """
    pid = _CATALOG_PROVIDER_MAP.get(provider_name, provider_name)
    try:
        from providers.catalog import get_shared_catalog
        return get_shared_catalog().default_model_for(pid) or ""
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("_default_model_for(%s) failed: %s", provider_name, exc)
        return ""


def _cooldown_state_path() -> str:
    """Path used to persist provider cooldown circuit state."""
    override = os.environ.get("FERAL_LLM_COOLDOWN_STATE_PATH", "").strip()
    if override:
        return override
    try:
        base = feral_data_home()
        base.mkdir(parents=True, exist_ok=True)
        return str(base / "llm_provider_cooldowns.json")
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("cooldown state path unavailable: %s", exc)
        return ""


def _responses_endpoint_for(provider_name: str, model: str) -> bool:
    """True when ``(provider, model)`` must be served by ``/v1/responses``.

    Thin wrapper over ``providers.model_classes.classify_endpoint`` that
    never raises: ``_call_provider`` runs inside the failover loop, and
    a classifier exception there would be recorded as a provider
    failure and cool the candidate down for nothing.
    """
    if not provider_name or not model:
        return False
    try:
        from providers.model_classes import classify_endpoint
        return classify_endpoint(provider_name, model) == "responses"
    except Exception:
        return False


def llm_response_error(data: Any) -> Optional[str]:
    """Return the provider failure carried by a ``chat()`` result, or ``None``.

    Every ``LLMProvider.chat`` path reports failure in-band as
    ``{"error": <detail>, "choices": []}``. ``extract_response`` used to
    hand that detail back in the TEXT slot, so the orchestrator rendered
    an HTTP 400 as an assistant bubble, stored it in
    ``conversation_history`` and fed it back to the model as its own
    prior turn. Consumers that need the failure now ask this helper and
    route it as an error frame; ``extract_response`` returns no text.

    A pure function over the dict (not a method) so the orchestrator,
    multi-agent workers and subagent loop can call it regardless of how
    the ``llm`` object is stubbed in tests.

    Only an explicit, non-empty ``error`` counts. A payload with no
    ``choices`` is an EMPTY answer, not a provider failure: the
    orchestrator has a never-stall retry for that case, and a failure
    frame for it would be wrong. Non-dict input is left alone for the
    same reason (test doubles hand back MagicMocks).
    """
    if not isinstance(data, dict):
        return None
    err = data.get("error")
    if err:
        return err if isinstance(err, str) else str(err)
    return None


def parse_tool_arguments(raw: Any, tool_name: str = "") -> tuple[dict, str]:
    """Parse a model's tool-call ``arguments`` string.

    Returns ``(args, error)``. ``error`` is ``""`` on success and a short
    human-readable reason otherwise.

    Why this exists instead of ``json.loads(...) except: args = {}``:
    the four sites that did exactly that turned a truncated or malformed
    arguments blob into an empty-but-valid call, with no log line. The
    tool then ran with no arguments and returned whatever it says when
    required fields are missing, which reads to the model as a tool
    problem rather than its own output being lost.

    Measured in the live store: 61 ``web_search__web_search`` rows on
    2026-05-15, all with ``args = {}``, all answered "Missing search
    query. Provide 'query' or 'q' parameter.", and the anti-loop guard
    firing at streaks of 5, 6 and 7. 61 executions, 61 failures, zero
    successes, and nothing anywhere said the arguments had failed to
    parse. ``computer_use__bash`` shows the same shape (7 of 8 calls with
    empty args on 2026-05-12), so it is not one bad model reply.
    """
    if isinstance(raw, dict):
        return dict(raw), ""
    raw_str = raw if isinstance(raw, str) else ""
    if not raw_str.strip():
        # A genuinely no-argument tool call. Common and correct.
        return {}, ""
    try:
        parsed = json.loads(raw_str)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning(
            "tool-call arguments for %s did not parse (%s); the call would "
            "otherwise run with no arguments. raw=%r",
            tool_name or "<unnamed tool>", exc, raw_str[:300],
        )
        return {}, f"arguments were not valid JSON ({exc})"
    if not isinstance(parsed, dict):
        logger.warning(
            "tool-call arguments for %s parsed to %s, not an object; the "
            "call would otherwise run with no arguments. raw=%r",
            tool_name or "<unnamed tool>", type(parsed).__name__, raw_str[:300],
        )
        return {}, f"arguments were a JSON {type(parsed).__name__}, not an object"
    return parsed, ""


def _finalise_tool_call(entry: dict) -> dict:
    """v2026.5.27 — convert an in-progress Responses-API tool-call
    accumulator into the shape the orchestrator / tool_runner expects.

    Input shape (built up across multiple SSE events):
        {item_id: "fc_…", call_id: "call_…", name: "...", arguments: "{...}"}

    Output shape (chat-completions-equivalent tool_call_delta):
        {id: "call_…", name: "...", arguments: "{...}", args: {...}}

    The orchestrator's stream loop merges these into the assistant
    message's ``tool_calls`` list using ``id`` as the tool-call id and
    ``args`` as the parsed function arguments. ``id`` MUST be the
    model-facing ``call_id`` because that's what the next turn echoes
    back as ``function_call_output.call_id`` to thread the response.
    """
    args_str = entry.get("arguments", "") or "{}"
    args, args_error = parse_tool_arguments(args_str, entry.get("name", ""))
    return {
        "id": entry.get("call_id") or entry.get("item_id", ""),
        "name": entry.get("name", ""),
        "arguments": args_str,
        "args": args,
        "args_error": args_error,
    }


class LLMProvider:
    """
    Pluggable LLM interface.
    
    Supports:
    - OpenAI API (GPT-4o, GPT-4o-mini)
    - Ollama local (llama3, mistral, etc.)
    - Any OpenAI-compatible endpoint (Groq, Together, etc.)
    - Local on-device inference (MLX on Apple Silicon, llama.cpp elsewhere)
    - Hybrid mode (local for routing, cloud for reasoning)
    """

    def __init__(self):
        self.provider = os.getenv("FERAL_LLM_PROVIDER", "openai")
        # Resolve the default model lazily from the shared
        # ``ProviderCatalog`` rather than burning a literal here. The
        # catalog reads ``model_catalog.json`` + each adapter's bundled
        # list, so this picks up frontier IDs (gpt-5.5, claude-opus-4-7,
        # gemini-3.1-pro-preview) without an llm_provider.py edit. If
        # the catalog hasn't booted yet (offline ``feral setup``,
        # tests), fall back to the env override or empty so the picker
        # surfaces an honest "choose a model" state.
        self.model = os.getenv("FERAL_LLM_MODEL", "") or _default_model_for(self.provider)
        self.api_key = os.getenv("OPENAI_API_KEY", "")
        self.base_url = os.getenv("FERAL_LLM_BASE_URL", "")
        self.available = True
        self._config: dict = {}
        self._cooldown = ProviderCooldownTracker(storage_path=_cooldown_state_path())
        self._last_budget_routing: dict[str, Any] = {}
        # Per-call cross-provider failover record. ``None`` means the
        # primary answered on its first hop (steady state). Populated
        # by ``chat_with_failover`` and read by ``health_snapshot`` /
        # the WebUI fallback chip. See findings/13-llm-core.md fix #5.
        self._last_failover: Optional[dict] = None
        # Optional ``CostBudget`` (Wave 1 Lane 04). When set, every
        # chat / chat_stream / chat_with_failover call is gated through
        # ``check_and_reserve`` AND records actual token usage via
        # ``record_usage``. ``BudgetExceeded`` surfaces as structured
        # response shape (``{error, budget_exceeded: {...}}``) so the
        # orchestrator can render a banner instead of an error toast.
        # See findings/13-llm-core.md fix #5 + audit-r13
        # 05-token-billing-leakage.md.
        self._cost_budget: Any = None

        # When `chat()` (the direct path, not chat_with_failover) sees a
        # permanent auth failure for a provider+key combination, we
        # remember it so subsequent calls short-circuit instead of
        # hammering the API and spamming ERROR-level logs every minute.
        # Cleared by `switch_provider()` (which is what /api/config/credentials
        # calls when the user updates their key in Settings).
        self._auth_permanent_until: dict[str, float] = {}
        self._auth_permanent_logged: set[str] = set()

        # Local inference engine (for provider=local or hybrid)
        self._local_engine = None
        self._hybrid_cloud_provider = None
        self._codex_adapter = None

        if self.provider in ("local", "hybrid"):
            self._init_local_engine()
            if self.provider == "hybrid":
                self._init_hybrid_cloud()
            if self._local_engine:
                logger.info(f"LLM Provider: {self.provider} | Local Model: {self._local_engine.model_id}")
                return
            else:
                logger.warning("Local engine init failed, falling back to cloud")
                self.provider = "openai"

        # Set defaults based on provider. Model defaults always come
        # from the catalog (`_default_model_for`) so frontier IDs land
        # without code edits.
        if self.provider == "ollama":
            self.base_url = self.base_url or ollama_openai_base_url()
            # Ollama exposes the loaded model list via /api/tags; the
            # detected name in __init__ is preferred. Fall back only if
            # the user shipped no model.
            self.model = self.model or _default_model_for("ollama")
            self.api_key = "ollama"
        elif self.provider == "groq":
            self.base_url = self.base_url or "https://api.groq.com/openai/v1"
            self.api_key = os.getenv("GROQ_API_KEY", self.api_key)
            self.model = self.model or _default_model_for("groq")
        elif self.provider == "anthropic":
            self.base_url = self.base_url or "https://api.anthropic.com/v1"
            self.api_key = os.getenv("ANTHROPIC_API_KEY", self.api_key)
            self.model = self.model or _default_model_for("anthropic")
        elif self.provider == "gemini":
            self.base_url = self.base_url or "https://generativelanguage.googleapis.com/v1beta/openai"
            self.api_key = _gemini_api_key() or self.api_key
            self.model = self.model or _default_model_for("gemini")
        elif self.provider == "openrouter":
            self.base_url = self.base_url or "https://openrouter.ai/api/v1"
            self.api_key = os.getenv("OPENROUTER_API_KEY", self.api_key)
            self.model = self.model or _default_model_for("openrouter")
        elif self.provider == "deepseek":
            # Keep aligned with ``_PROVIDER_REGISTRY`` — both must end
            # in ``/v1`` so the failover candidate path through
            # ``_get_provider_config`` and the primary boot path can
            # never disagree on the URL shape.
            self.base_url = self.base_url or "https://api.deepseek.com/v1"
            self.api_key = os.getenv("DEEPSEEK_API_KEY", self.api_key)
            self.model = self.model or _default_model_for("deepseek")
        elif self.provider == "lmstudio":
            self.base_url = self.base_url or "http://localhost:1234/v1"
            self.api_key = "lm-studio"
            self.model = self.model or _default_model_for("lmstudio")
        elif self.provider == "codex":
            adapter = self._get_codex_adapter()
            self.base_url = "app-server://stdio"
            # Non-secret marker used by the legacy health surface,
            # which still calls every authentication mode "has_key".
            self.api_key = "codex-managed-auth"
            self.model = self.model or _default_model_for("codex")
            self.available = adapter.cli_available()
        elif self.provider == "openai":
            # Default path. Previously this case rode the else branch
            # that also served as the silent fallback for unknown
            # provider ids — that's the conflation  A3 is untangling.
            # Splitting ``openai`` into its own branch lets the else
            # below report unknown provider ids truthfully.
            self.base_url = self.base_url or "https://api.openai.com/v1"
            self.api_key = os.getenv("OPENAI_API_KEY", self.api_key)
            self.model = self.model or _default_model_for("openai")
        elif self.provider in _PROVIDER_REGISTRY:
            # Registry-driven branch for every remaining OpenAI-compatible
            # provider (kimi, qwen, xai, zai, minimax, mistral). These used
            # to need a hand-written ``elif`` apiece, which is how kimi and
            # qwen ended up with base URLs that had drifted from the
            # registry tuple sitting a few hundred lines above. Reading the
            # registry directly makes that drift structurally impossible:
            # adding a provider is now a one-line registry edit.
            registry_base, registry_env = _PROVIDER_REGISTRY[self.provider]
            self.base_url = self.base_url or _resolve_workspace_base_url(registry_base)
            if registry_env:
                self.api_key = os.getenv(registry_env, self.api_key)
            self.model = self.model or _default_model_for(self.provider)
            if "{" in (self.base_url or ""):
                # Workspace-scoped host with no workspace id supplied.
                # Fail visibly rather than dial a literal "{WorkspaceId}".
                logger.warning(
                    "%s base URL is still a template (%s) — set "
                    "DASHSCOPE_WORKSPACE_ID. Marking provider unavailable.",
                    self.provider,
                    self.base_url,
                )
                self.available = False
        else:
            # Unknown provider id. Previously this branch silently
            # defaulted to ``https://api.openai.com/v1`` with the
            # inherited ``OPENAI_API_KEY`` — which meant a typo'd or
            # not-yet-supported provider id (e.g. catalog-only entries
            # like ``bedrock`` / ``together`` / ``fireworks``) would
            # masquerade as OpenAI at request time and leak the user's
            # OpenAI key to the wrong endpoint name in logs / metrics.
            # The new contract: keep the unknown provider name visible
            # to the caller, clear the inherited OpenAI key, and mark
            # the runtime unavailable unless the operator explicitly
            # set FERAL_LLM_BASE_URL for a custom OpenAI-compatible
            # gateway. Local-fallback detection below still runs.
            logger.warning(
                "Unknown LLM provider %r — no runtime adapter. "
                "Supported providers: %s. Set FERAL_LLM_PROVIDER to a "
                "supported id or supply FERAL_LLM_BASE_URL for a "
                "custom OpenAI-compatible endpoint.",
                self.provider,
                sorted(SUPPORTED_RUNTIME_PROVIDERS),
            )
            if self.base_url:
                # Operator explicitly pointed us at a custom gateway.
                # Trust it, keep the explicit api_key (if any), and
                # resolve the default model best-effort.
                self.model = self.model or _default_model_for(self.provider)
            else:
                self.base_url = ""
                self.api_key = ""
                self.model = self.model or _default_model_for(self.provider)
                self.available = False

        # Cross-cut #1 (v2026.5.42): if env / per-provider branch left
        # ``self.api_key`` empty, consult the labeled-keys vault overlay
        # before declaring the slot unconfigured. Prior to this the
        # operator could ``feral key add --provider X --set-active``
        # successfully and the brain would still refuse to boot with a
        # usable key because ``vault_keys`` was never read on the hot
        # path. ``ollama`` / ``lmstudio`` keep their literal stubs.
        if not self.api_key and self.provider not in ("ollama", "lmstudio", "codex"):
            resolved = _resolve_api_key(self.provider)
            if resolved:
                self.api_key = resolved

        # Check if API key is available — if not, try local fallbacks
        if not self.api_key and self.provider not in ("ollama", "lmstudio", "codex"):
            logger.warning(f"No API key for provider '{self.provider}'. Trying local fallbacks...")
            ollama_model = self._detect_ollama()
            if ollama_model:
                self.provider = "ollama"
                self.base_url = ollama_openai_base_url()
                self.model = ollama_model
                self.api_key = "ollama"
                logger.info(f"Ollama detected — using model '{ollama_model}'")
            else:
                lmstudio_model = self._detect_lmstudio()
                if lmstudio_model:
                    self.provider = "lmstudio"
                    self.base_url = "http://localhost:1234/v1"
                    self.model = lmstudio_model
                    self.api_key = "lm-studio"
                    logger.info(f"LM Studio detected — using model '{lmstudio_model}'")
                else:
                    logger.warning(
                        "No LLM available. Set OPENAI_API_KEY or run Ollama (`ollama serve`) "
                        "or LM Studio. Brain will operate in direct-execution mode "
                        "(no reasoning, skill matching only)."
                    )
                    self.available = False
                    self.api_key = "none"

        self.client = self._build_client()

        status = "READY" if self.available else "DIRECT-EXECUTION MODE (no LLM)"
        logger.info(f"LLM Provider: {self.provider} | Model: {self.model} | Status: {status}")

    @staticmethod
    def list_presets() -> list[dict]:
        return [{"id": k, **v} for k, v in LLM_PRESETS.items()]

    def _build_client(self) -> httpx.AsyncClient:
        if self.provider == "codex":
            # Codex owns its own authenticated transport. Keep a plain
            # client only to preserve the lifecycle expected by callers
            # that unconditionally close ``LLMProvider.client``.
            return httpx.AsyncClient(timeout=60.0)
        # Cross-cut #1 (v2026.5.42): if the slot is empty, late-bind
        # from the labeled-keys vault overlay just before the headers
        # are baked into the httpx client. Explicit ``api_key`` writes
        # from ``switch_provider`` / ``reconfigure`` are preserved —
        # we only fill the slot when the caller left it blank.
        if not self.api_key and self.provider not in ("ollama", "lmstudio", "codex"):
            resolved = _resolve_api_key(self.provider)
            if resolved:
                self.api_key = resolved
        headers = {"Content-Type": "application/json"}
        if self.provider == "anthropic":
            headers["x-api-key"] = self.api_key
            headers["anthropic-version"] = "2023-06-01"
        elif self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=60.0)

    def _get_codex_adapter(self):
        adapter = getattr(self, "_codex_adapter", None)
        if adapter is None:
            from providers.codex_provider import CodexProvider

            adapter = CodexProvider()
            self._codex_adapter = adapter
        return adapter

    @staticmethod
    def _codex_messages(messages: list[dict]):
        from providers.base import ChatMessage

        converted = []
        for message in messages:
            content = message.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=True)
            converted.append(
                ChatMessage(
                    role=str(message.get("role") or "user"),
                    content=content,
                    name=message.get("name"),
                    tool_calls=list(message.get("tool_calls") or []),
                )
            )
        return converted

    @staticmethod
    def _codex_response_dict(response) -> dict:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": response.text,
        }
        if response.tool_calls:
            message["tool_calls"] = response.tool_calls
        return {
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": response.finish_reason,
                }
            ],
            "usage": response.usage,
            "model": response.model,
        }

    @staticmethod
    def _detect_ollama() -> Optional[str]:
        """Probe Ollama for running models. Returns best model name or None."""
        preferred = ["llama3.1", "llama3", "mistral", "gemma2", "phi3", "qwen2"]
        try:
            import urllib.request
            resp = urllib.request.urlopen(f"{ollama_base_url().rstrip('/')}/api/tags", timeout=3)
            data = json.loads(resp.read())
            models = [m.get("name", "").split(":")[0] for m in data.get("models", [])]
            if not models:
                logger.info("Ollama running but no models pulled. Try: ollama pull llama3.1")
                return None
            for pref in preferred:
                if pref in models:
                    return pref
            return models[0]
        except Exception:
            return None

    @staticmethod
    def _detect_lmstudio() -> Optional[str]:
        """Probe LM Studio for loaded models. Returns model id or None."""
        try:
            import httpx
            r = httpx.get("http://localhost:1234/v1/models", timeout=2)
            if r.status_code == 200:
                models = r.json().get("data", [])
                if models:
                    return models[0].get("id", "local-model")
        except Exception:
            pass
        return None

    def _init_local_engine(self):
        try:
            from agents.local_inference import create_local_engine
            self._local_engine = create_local_engine()
            self.available = True
        except Exception as e:
            logger.warning(f"Local LLM engine init failed: {e}")
            self._local_engine = None

    @property
    def context_window_tokens(self) -> int:
        """Usable context window of the model this provider talks to.

        The router had no way to answer this, so every caller that
        needed to size a prompt invented a character cap instead. It
        answers now, and it answers CONSERVATIVELY on purpose.

        When a local engine is attached it wins, even in ``hybrid`` mode
        where a given turn may go to the cloud instead. Hybrid routes by
        turn, this property is read before the routing decision exists,
        and the two errors are not symmetric: sizing a cloud prompt to
        the local engine's 4096-token window under-fills a large window
        and costs some context, while sizing a local prompt to a
        128000-token window overflows the engine and the request fails
        outright. ``agents/token_estimate.py`` states the same asymmetry
        for the same reason.

        Engines that do not know their own window report 0, and the
        answer falls back to what the operator configured.
        """
        # ``getattr`` on self too: the attribute is assigned partway
        # through __init__, and this must not raise for a provider that
        # is still being built.
        engine = getattr(self, "_local_engine", None)
        engine_window = getattr(engine, "context_window_tokens", 0)
        try:
            engine_window = int(engine_window or 0)
        except (TypeError, ValueError):
            engine_window = 0
        if engine_window > 0:
            return engine_window
        return configured_context_window_tokens()

    def _init_hybrid_cloud(self):
        """In hybrid mode, cloud is used for complex reasoning."""
        cloud_key = os.getenv("OPENAI_API_KEY", "")
        if cloud_key:
            self._hybrid_cloud_provider = httpx.AsyncClient(
                base_url="https://api.openai.com/v1",
                headers={"Authorization": f"Bearer {cloud_key}", "Content-Type": "application/json"},
                timeout=30.0,
            )

    async def chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        *,
        call_site: str = "chat",
        force_tool: Optional[str] = None,
    ) -> dict:
        """Send a chat completion request and return the full response dict.

        v2026.5.23: when the current model is classified as
        ``responses``-only (gpt-5-pro / gpt-5.5-pro / o3-pro /
        deep-research / gpt-5-codex / computer-use-preview), route
        through ``_responses_chat`` against OpenAI's ``/v1/responses``
        endpoint. Everything else stays on the existing
        ``/v1/chat/completions`` path. The returned dict normalises to
        the same shape callers already consume (``choices[0].message``
        + optional ``tool_calls``).

        When ``fallback_providers`` is configured in ``self._config``,
        transparently delegates to :meth:`chat_with_failover` so every
        caller (digital twin, proactive, ideas engine, wherever) gains
        cross-provider failover without knowing about the distinction.
        """
        # Permanent-auth short-circuit. If a previous call established
        # that the current key is invalid (HTTP 401 + "invalid_api_key"),
        # don't keep poking the wire every 60s -- return the cached
        # error so the user gets a clear reason and the log stays quiet.
        # `switch_provider` clears the entry, so the moment the user
        # updates their key in Settings the brain starts trying again.
        # Defensive getattr because some test stubs subclass LLMProvider
        # without invoking __init__ (the cache + provider/model attrs
        # may not exist).
        auth_block_map = getattr(self, "_auth_permanent_until", None)
        if auth_block_map:
            auth_key = f"{getattr(self, 'provider', '?')}:{getattr(self, 'model', '?')}"
            auth_block = auth_block_map.get(auth_key)
            if auth_block and time.time() < auth_block:
                return {
                    "error": (
                        f"{getattr(self, 'provider', 'LLM').upper()} API key "
                        "invalid (HTTP 401). Update the key in Settings to retry."
                    ),
                    "choices": [],
                    "auth_permanent": True,
                }

        # Cost-budget pre-flight (Wave 2 Lane 09). Runs once at the
        # top of the public ``chat`` entry; downstream paths
        # (chat_with_failover, _chat_anthropic, etc.) are invoked from
        # here and get gated implicitly. We use ``call_site`` (default
        # ``"chat"``) so background loops can opt into their own caps
        # (``screen_loop``, ``proactive``, ``learner``) without
        # spending the user's chat budget. ``getattr`` for ``model``
        # because tests routinely build LLMProvider via ``__new__``
        # without populating every attribute.
        budget_block = await self._budget_check(
            call_site, getattr(self, "model", ""), max_tokens,
        )
        if budget_block is not None:
            return budget_block

        if self.provider == "codex":
            if self._messages_contain_vision(messages):
                return {
                    "error": (
                        "Codex provider image translation is not available yet; "
                        "send a text-only turn or choose a vision provider."
                    ),
                    "choices": [],
                }
            fallbacks = (
                self._config.get("fallback_providers")
                if isinstance(self._config, dict)
                else None
            )
            if fallbacks:
                try:
                    return await self.chat_with_failover(
                        messages,
                        tools,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        call_site=call_site,
                        force_tool=force_tool,
                    )
                except BudgetExceeded as exc:
                    return self._budget_exceeded_response(exc)
                except Exception as exc:
                    logger.warning("Codex failover chain exhausted: %s", exc)
                    return {"error": str(exc), "choices": []}
            try:
                result = await self._call_provider(
                    "codex",
                    {
                        "base_url": self.base_url,
                        "api_key": self.api_key,
                        "model": self.model,
                        "supported": True,
                    },
                    messages,
                    tools,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    force_tool=force_tool,
                )
                await self._budget_record(call_site, self.model, result)
                return result
            except Exception as exc:
                detail = _describe_error(exc)
                logger.error("Codex app-server call failed: %s", detail)
                return {"error": detail, "choices": []}

        fallbacks = self._config.get("fallback_providers") if isinstance(self._config, dict) else None
        use_failover = bool(fallbacks) and not (
            self._local_engine and self.provider in ("local", "hybrid")
        )

        # v2026.5.23 — Responses-API route for OpenAI Pro / o-Pro /
        # deep-research / Codex / computer-use / gpt-5.6 models.
        #
        # Only taken here when NO failover chain is configured. With a
        # chain, ``chat_with_failover`` -> ``_call_provider`` routes the
        # primary candidate through /v1/responses itself, so calling
        # ``_responses_chat`` first would just try the primary twice on
        # every failure. Before this rewrite the failover loop did not
        # know about the Responses endpoint, which is why the direct
        # attempt had to run first.
        #
        # On failure this RETURNS the error. It used to fall through to
        # the /chat/completions body below, where ``apply_reasoning_fork``
        # adds ``reasoning_effort`` and OpenAI answers 400 "Function tools
        # with reasoning_effort are not supported for gpt-5.6-sol in
        # /v1/chat/completions" (brain.err, 2026-08-01 through 2026-09-02).
        # A responses-only model has no working chat-completions fallback
        # by definition, so the second request could only ever add a
        # second, misleading error.
        try:
            from providers.model_classes import classify_endpoint
            primary_is_responses = (
                classify_endpoint(self.provider, self.model) == "responses"
            )
        except Exception as resp_exc:
            logger.warning("Responses-API route classification raised: %s", resp_exc)
            primary_is_responses = False
        if primary_is_responses and not use_failover:
            result = await self._responses_chat(
                messages, tools, temperature, max_tokens,
                force_tool=force_tool,
            )
            if result and not result.get("error"):
                await self._budget_record(call_site, self.model, result)
                return result
            if result and result.get("error"):
                logger.warning(
                    "Responses-API primary failed with no fallback chain: %s",
                    result["error"],
                )
                return result
            return {"error": "responses: empty payload", "choices": []}

        if use_failover:
            try:
                # Forward the same ``call_site`` so the failover path
                # bills against the right cap. ``chat_with_failover``
                # also runs its own ``_budget_check`` — the second
                # check is a no-op when the first reserved nothing
                # (we don't double-bill until ``record_usage`` lands).
                return await self.chat_with_failover(
                    messages, tools,
                    temperature=temperature, max_tokens=max_tokens,
                    call_site=call_site,
                    force_tool=force_tool,
                )
            except BudgetExceeded as exc:
                return self._budget_exceeded_response(exc)
            except Exception as exc:
                logger.warning("chat_with_failover exhausted: %s", exc)
                return {"error": str(exc), "choices": []}

        if self._messages_contain_vision(messages):
            ok, reason = self._vision_support_status()
            if not ok:
                logger.warning(reason)
                return {"error": reason, "choices": []}

        # Guard against unsupported provider before any wire work.
        # Without this the body is assembled and POSTed against
        # whatever ``base_url`` happens to be set — which for the
        # old unknown-provider path was ``https://api.openai.com/v1``.
        if not is_supported_runtime_provider(self.provider) and self.provider not in ("local", "hybrid"):
            reason = (
                f"Selected LLM provider {self.provider!r} is not supported by this "
                f"runtime. Supported: {sorted(SUPPORTED_RUNTIME_PROVIDERS)}."
            )
            logger.warning(reason)
            return {"error": reason, "choices": []}

        model_guard_error = _chat_completions_model_guard(self.provider, self.model)
        if model_guard_error:
            logger.warning(model_guard_error)
            return {"error": model_guard_error, "choices": []}

        # Local inference path
        if self._local_engine and self.provider in ("local", "hybrid"):
            use_local = self.provider == "local" or not self._hybrid_cloud_provider
            if self.provider == "hybrid" and tools:
                use_local = False

            if use_local:
                return await self._chat_local(messages, tools, temperature, max_tokens)

        if self.provider == "anthropic":
            return await self._chat_anthropic(
                messages, tools, temperature, max_tokens,
                force_tool=force_tool,
            )

        # NOTE: a previous "runtime model-class guard" lived here as a
        # belt-and-suspenders defense against the dated-transcribe-id
        # leak. Removed in 2026-05-09 audit-r8 round-2 once the actual
        # root cause was fixed at boot: `api/state.BrainState.init` now
        # calls `providers.catalog.set_shared_catalog(self.provider_catalog)`
        # so every `_default_model_for(...)` consults the live catalog
        # instead of a lazily-created empty singleton. The boot
        # self-heal + classifier are sufficient once the catalog
        # singleton is correctly wired — no per-call patching needed.

        body: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        if tools:
            clean_tools = []
            for tool in tools:
                clean = {k: v for k, v in tool.items() if k != "_feral_meta"}
                clean_tools.append(clean)
            # OpenAI's /v1/chat/completions rejects payloads with more
            # than 128 tools (array_above_max_length). FERAL ships
            # 26 skills + ~23 browser endpoints + subagents + etc,
            # which overflows on some installs. We slice to 128 here
            # with a one-time warning rather than crashing the call.
            # Prioritise retention of the first tools since skills
            # register alphabetically and the brain-auto skills that
            # appear first are the hottest path.
            if self.provider in ("openai",):
                clean_tools = _cap_openai_chat_tools(clean_tools)
            body["tools"] = clean_tools
            body["tool_choice"] = _resolve_tool_choice(
                self.provider, clean_tools, force_tool,
            )

        # Reasoning-family param fork: ``/v1/chat/completions`` rejects
        # ``max_tokens`` + free-form ``temperature`` on gpt-5* / o1 /
        # DeepSeek v4-pro / thinking-capable Claude / Gemini -thinking.
        # This is the exact shape of the v2026.5.0 400s in the shipped
        # terminal log (§A5 of docs/WAVE5_HARDENING_PROMPT.md).
        apply_reasoning_fork(self.provider, self.model, body)

        from observability.metrics import increment, measure
        increment("feral.llm.calls_total", attributes={"provider": self.provider, "model": self.model})
        try:
            async def _do_chat():
                resp = await self.client.post("/chat/completions", json=body)
                resp.raise_for_status()
                return resp.json()

            with measure("feral.llm.latency", {"provider": self.provider, "model": self.model}):
                result = await _retry_llm_call(_do_chat)
            try:
                await self._budget_record(call_site, self.model, result)
            except BudgetExceeded as bx:
                # ``record_usage`` raises when this very call tipped us
                # over a cap. Surface it in the response so the
                # orchestrator can render the banner; the result data
                # is already complete so no token spend is wasted.
                if isinstance(result, dict):
                    result["budget_exceeded"] = self._budget_exceeded_response(bx)["budget_exceeded"]
            return result
        except httpx.HTTPStatusError as e:
            increment("feral.llm.errors_total", attributes={"provider": self.provider, "model": self.model})
            detail = _describe_http_status_error(e)
            # Classify so we can short-circuit the next call instead of
            # hitting the wire every 60s when the key is dead.
            try:
                reason = classify_error(e)
            except Exception:
                reason = None
            auth_key = f"{self.provider}:{self.model}"
            if reason == FailoverReason.AUTH_PERMANENT:
                # 24h block; user updating the key in Settings clears it
                # immediately via switch_provider.
                self._auth_permanent_until[auth_key] = time.time() + 24 * 3600
                if auth_key not in self._auth_permanent_logged:
                    self._auth_permanent_logged.add(auth_key)
                    logger.error(
                        "LLM API error: %s — disabling provider until key is updated. "
                        "Open Settings and refresh the %s API key.",
                        detail, self.provider,
                    )
                else:
                    logger.debug("LLM API error (suppressed, key still invalid): %s", detail)
            else:
                logger.error("LLM API error: %s", detail)
            return {"error": detail, "choices": []}
        except Exception as e:
            increment("feral.llm.errors_total", attributes={"provider": self.provider, "model": self.model})
            detail = _describe_error(e)
            logger.error("LLM call failed: %s", detail)
            return {"error": detail, "choices": []}

    def extract_response(self, data: dict) -> tuple[Optional[str], list[dict]]:
        """
        Extract the text response and tool calls from an LLM response.
        Returns: (text_content, tool_calls)

        A provider failure (``{"error": ..., "choices": []}``) yields
        ``(None, [])``. The failure detail is NOT returned as text: it
        used to be, and the orchestrator then delivered "HTTP 400 ..."
        as an assistant bubble and stored it in the transcript. Callers
        that need the detail read it with ``llm_response_error(data)``.
        An empty payload (no ``choices``) also yields ``(None, [])``; it
        used to yield the literal text "No response from LLM".
        """
        if (
            not isinstance(data, dict)
            or llm_response_error(data) is not None
            or not data.get("choices")
        ):
            return None, []

        choice = data["choices"][0]
        message = choice.get("message", {})
        text = message.get("content", "")
        tool_calls = message.get("tool_calls", [])

        parsed_tools = []
        for tc in tool_calls:
            func = tc.get("function", {})
            args, args_error = parse_tool_arguments(
                func.get("arguments", ""), func.get("name", ""),
            )
            parsed_tools.append({
                "id": tc.get("id", ""),
                "name": func.get("name", ""),
                "args": args,
                "args_error": args_error,
            })

        return text, parsed_tools

    @staticmethod
    def _messages_contain_vision(messages: list[dict]) -> bool:
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        block_type = str(block.get("type", ""))
                        if block_type in ("image_url", "input_image", "image", "image_base64"):
                            return True
                        if "image_url" in block:
                            return True
            elif isinstance(content, dict):
                block_type = str(content.get("type", ""))
                if block_type in ("image_url", "input_image", "image", "image_base64"):
                    return True
                if "image_url" in content:
                    return True
        return False

    def _vision_support_status(self) -> tuple[bool, str]:
        """Vision capability of the LIVE (primary) provider/model."""
        return self._vision_support_for(self.provider, self.model)

    def _vision_support_for(
        self, provider: str, model: str = "",
    ) -> tuple[bool, str]:
        """Vision capability of an ARBITRARY candidate provider/model.

        Split out of ``_vision_support_status`` so ``chat_with_failover``
        can ask the question per hop: capability belongs to the
        candidate the request is about to be sent to, not to
        ``self.provider``. ``_vision_support_status`` keeps its old
        signature and meaning (the primary) for every existing caller.
        """
        model = model or self.model
        if provider in ("openai", "gemini"):
            return True, ""

        # OpenRouter is a router — vision capability is per-route, not
        # per-provider. The v2026.5.0 terminal log showed this call
        # early-returning "does not support vision" on every image send
        # because the adapter's _capabilities omitted "vision". The
        # adapter fix adds vision to the superset; here we consult the
        # narrower ``_capabilities_for_model`` when the catalog knows
        # the route's modality, and otherwise trust the superset.
        if provider == "openrouter":
            ok, narrow_reason = _openrouter_route_supports_vision(model)
            if ok:
                return True, ""
            return False, narrow_reason

        # Anthropic and Groq support vision on their frontier chat models;
        # the provider registry carries that signal in the bundled
        # ``_capabilities`` set. If we ever ship a text-only Anthropic
        # build the per-model hook ``_capabilities_for_model`` narrows it.
        if provider in ("anthropic", "groq"):
            return True, ""

        # DeepSeek was listed above on the assumption that every frontier
        # chat model takes images. It does not: the chat-completions API
        # rejects an ``image_url`` content block outright with
        # HTTP 400 ``invalid_request_error`` --
        #   "Failed to deserialize the JSON body into the target type:
        #    messages[0]: unknown variant `image_url`, expected `text`"
        # -- observed live on 2026-07-30 against ``deepseek-v4-pro``. Because
        # this returned True, the vision guard let the request through, so a
        # DeepSeek hop in the failover chain 400'd on any turn carrying a
        # screen frame or an attached image instead of degrading to text.
        #
        # Returning False here makes the caller strip the image blocks and
        # send the text, which is a usable answer rather than an exhausted
        # chain. That claim used to be false: the only caller,
        # ``chat_with_failover``, answered ``{"error": ...}`` instead of
        # stripping anything. The stripping now genuinely happens, in
        # ``_strip_vision_for_text_only_hop`` below, which
        # ``chat_with_failover`` applies per candidate.
        if provider == "deepseek":
            return (
                False,
                "DeepSeek chat models are text-only and reject image content "
                "blocks. Images will be dropped for this hop.",
            )

        if provider == "ollama":
            model_lower = (model or "").lower()
            if any(hint in model_lower for hint in VISION_READY_OLLAMA_MODELS):
                return True, ""
            return (
                False,
                "Current Ollama model does not appear vision-capable. "
                "Use a VLM model such as 'llava' or apply preset 'ollama_vision'.",
            )

        if provider in ("local", "hybrid") and self._local_engine:
            if getattr(self._local_engine, "supports_vision", False):
                return True, ""
            return (
                False,
                "Local inference engine is text-only and cannot process images. "
                "Use Ollama VLM for local vision (`provider=ollama`, model `llava`).",
            )

        return False, f"Provider '{provider}' does not support vision input."

    # Wording for an image that could not ride along on this hop. Mirrors
    # the convention established in ``agents/multimodal_blocks.py``
    # (``_UNDELIVERED_NOTE`` / ``_PRUNED_NOTE``): when an image cannot
    # reach the model, the model is told IN WORDS that one existed and
    # that it has not seen it. Silently dropping the block would leave
    # the model answering a question about a picture it does not know is
    # missing.
    VISION_STRIPPED_NOTE = (
        "[{count} image(s) were removed from this message before it was "
        "sent. The model answering this turn is {provider!r}, which does "
        "not accept image input. Reason: {reason} You have NOT seen the "
        "image(s). Do not describe or guess at their contents. Say plainly "
        "that the image could not be processed by the current model, and "
        "either ask the operator to switch to a vision-capable model or "
        "use a tool that returns a text description.]"
    )

    _VISION_BLOCK_TYPES = ("image_url", "input_image", "image", "image_base64")

    @classmethod
    def _is_vision_block(cls, block: Any) -> bool:
        """Mirror of ``_messages_contain_vision``'s per-block predicate."""
        if not isinstance(block, dict):
            return False
        if str(block.get("type", "")) in cls._VISION_BLOCK_TYPES:
            return True
        return "image_url" in block

    @classmethod
    def _strip_vision_for_text_only_hop(
        cls, messages: list[dict], *, provider: str = "", reason: str = "",
    ) -> tuple[list[dict], int]:
        """Return ``(messages_without_images, images_removed)``.

        Used when a failover hop lands on a provider that cannot take
        image input. Degrading to text is strictly better than the
        ``{"error": ...}`` this used to produce: the turn still gets an
        answer, and the answer is honest about the missing image because
        every stripped message gains a note saying so.

        The input list is never mutated: only the messages that actually
        carried an image are rebuilt.
        """
        out: list[dict] = []
        removed_total = 0
        for msg in messages:
            content = msg.get("content") if isinstance(msg, dict) else None
            blocks: list[Any]
            if isinstance(content, list):
                blocks = content
            elif isinstance(content, dict) and cls._is_vision_block(content):
                blocks = [content]
            else:
                out.append(msg)
                continue

            kept = [b for b in blocks if not cls._is_vision_block(b)]
            removed = len(blocks) - len(kept)
            if not removed:
                out.append(msg)
                continue
            removed_total += removed

            note = cls.VISION_STRIPPED_NOTE.format(
                count=removed,
                provider=provider or "the fallback provider",
                reason=(reason or "").strip() or "no reason reported.",
            )
            new_msg = dict(msg)
            if msg.get("role") == "tool":
                # An OpenAI-compatible ``role: "tool"`` message wants a
                # plain string body; a list survives only because the
                # Anthropic translator understands it. Collapse to text
                # so the stripped result is legal on every wire shape.
                texts = [
                    str(b.get("text", "")) for b in kept
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                texts = [t for t in texts if t]
                new_msg["content"] = "\n".join(texts + [note])
            else:
                new_msg["content"] = list(kept) + [{"type": "text", "text": note}]
            out.append(new_msg)
        return out, removed_total

    async def _chat_anthropic(
        self, messages: list[dict], tools: Optional[list[dict]],
        temperature: float, max_tokens: int,
        *,
        force_tool: Optional[str] = None,
    ) -> dict:
        """Anthropic Messages API → normalized to OpenAI format."""
        # A5: route OpenAI-shape transcripts through the conversion
        # helper so ``role: "tool"`` and assistant ``tool_calls`` are
        # lifted into Anthropic's content-block shape before the wire
        # request.
        system_text, conv_messages = _convert_messages_for_anthropic(messages)

        body: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": conv_messages,
        }
        if system_text.strip():
            body["system"] = system_text.strip()

        if tools:
            anthropic_tools = []
            for t in tools:
                if t.get("type") == "function":
                    fn = t["function"]
                    anthropic_tools.append({
                        "name": fn["name"],
                        "description": fn.get("description", ""),
                        "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
                    })
            if anthropic_tools:
                body["tools"] = anthropic_tools
                if force_tool and any(
                    t.get("name") == force_tool for t in anthropic_tools
                ):
                    translated = to_provider_tool_choice("anthropic", force_tool)
                    if translated is not None:
                        body["tool_choice"] = translated

        # Reasoning-family fork for Claude thinking-capable models.
        apply_reasoning_fork("anthropic", self.model, body)
        _enforce_anthropic_thinking_max_tokens(body)
        # Same breakpoints as ``_build_anthropic_body``: this method is
        # the OTHER place an Anthropic body is assembled (the direct
        # ``chat()`` path with no fallback chain), and it reports
        # ``cache_creation_input_tokens`` / ``cache_read_input_tokens``
        # below, which stayed permanently zero while nothing on the
        # request asked for a cache.
        if self._anthropic_cache_enabled():
            self._apply_anthropic_cache_breakpoints(body)

        try:
            async def _do_anthropic():
                resp = await self.client.post("/messages", json=body)
                resp.raise_for_status()
                return resp.json()

            data = await _retry_llm_call(_do_anthropic)

            text_parts = []
            tool_calls = []
            for block in data.get("content", []):
                if block.get("type") == "text":
                    text_parts.append(block["text"])
                elif block.get("type") == "tool_use":
                    tool_calls.append({
                        "id": block["id"],
                        "type": "function",
                        "function": {
                            "name": block["name"],
                            "arguments": json.dumps(block.get("input", {})),
                        },
                    })

            msg: dict = {"role": "assistant", "content": "\n".join(text_parts)}
            if tool_calls:
                msg["tool_calls"] = tool_calls

            out: dict = {
                "choices": [
                    {"message": msg, "finish_reason": data.get("stop_reason", "end_turn")},
                ],
            }
            # Carry ``usage`` (input/output AND the two prompt-cache
            # counters) so ``_budget_record`` has something to bill.
            # Dropping it here is why non-streamed Anthropic turns used
            # to move the cap by exactly $0.
            usage = self._anthropic_usage_block(data)
            if usage is not None:
                out["usage"] = usage
            return out
        except httpx.HTTPStatusError as e:
            detail = _describe_http_status_error(e)
            logger.error("Anthropic API error: %s", detail)
            return {"error": detail, "choices": []}
        except Exception as e:
            detail = _describe_error(e)
            logger.error("Anthropic call failed: %s", detail)
            return {"error": detail, "choices": []}

    async def _chat_local(
        self, messages: list[dict], tools: Optional[list[dict]],
        temperature: float, max_tokens: int,
    ) -> dict:
        """Run inference through the local engine."""
        try:
            if not self._local_engine.loaded:
                await self._local_engine.load_model()

            prompt = self._local_engine.format_chat(messages, tools)
            text = await self._local_engine.generate(prompt, max_tokens=max_tokens, temperature=temperature)

            clean_text, tool_calls = self._local_engine.parse_tool_calls(text)
            response_msg: dict = {"role": "assistant", "content": clean_text}

            if tool_calls:
                response_msg["tool_calls"] = [
                    {"id": tc["id"], "type": "function",
                     "function": {"name": tc["name"], "arguments": json.dumps(tc["args"])}}
                    for tc in tool_calls
                ]

            return {"choices": [{"message": response_msg, "finish_reason": "stop"}]}
        except Exception as e:
            logger.error(f"Local inference failed: {e}")
            return {"error": str(e), "choices": []}

    # ─────────────────────────────────────────────────────────────
    # v2026.5.23 — real round-trip availability probe
    # ─────────────────────────────────────────────────────────────
    #
    # Called from ``switch_provider`` and (via ``api/state.py``)
    # boot self-heal. Sends the smallest possible request through
    # the right endpoint for the current model and reports a
    # one-line reason on failure so the operator sees WHY a model
    # was rejected (404 / 401 / 429 / DNS) instead of a silent
    # ``available=False`` flip with no explanation.

    async def _probe_chat_availability(self) -> tuple[bool, str]:
        """Probe the live endpoint with a minimal request.

        Returns ``(ok, reason)``. On success, ``reason`` is empty.
        On failure, ``reason`` is a short string suitable for the
        log + the CLI / web UI to surface to the user.

        v2026.5.25 — uses ``max_tokens=16`` (not 1). gpt-5.5-pro
        rejects ``max_output_tokens<16`` with a 400 ("integer below
        minimum value"), which my v2026.5.24 probe (max_tokens=1)
        tripped on every boot for operators on Pro models, flipping
        `available=False` falsely. 16 is the live-verified minimum.

        v2026.5.25 — also tolerates the ``incomplete`` response
        status. Pro models often burn 16+ reasoning tokens before
        producing visible output; the response is technically
        ``status=incomplete`` but the model IS reachable + the key
        IS valid, which is exactly what the probe was supposed to
        confirm. Anything other than 200 + ``failed`` / outright
        rejection is a soft success.

        Soft failures (DNS, connection reset) are reported as
        failures here but the surrounding code may still leave
        ``available=True`` and rely on the existing cooldown ladder —
        the probe is advisory at boot time, not a circuit breaker
        for in-flight traffic.
        """
        try:
            from providers.model_classes import classify_endpoint
            endpoint_class = classify_endpoint(self.provider, self.model)
        except Exception:
            endpoint_class = "chat_completions"

        try:
            if endpoint_class == "responses":
                body = self._build_responses_body(
                    [{"role": "user", "content": "."}],
                    tools=None,
                    temperature=1,
                    max_tokens=16,  # gpt-5.5-pro hard minimum for max_output_tokens
                    stream=False,
                )
                resp = await self.client.post("/responses", json=body, timeout=15.0)
            else:
                body = {
                    "model": self.model,
                    "messages": [{"role": "user", "content": "."}],
                    "max_tokens": 16,
                    "temperature": 1,
                }
                # Run the reasoning fork so reasoning models get
                # max_completion_tokens / reasoning_effort.
                from agents.llm_reasoning import apply_reasoning_fork
                apply_reasoning_fork(self.provider, self.model, body)
                resp = await self.client.post("/chat/completions", json=body, timeout=15.0)
        except httpx.HTTPStatusError as exc:
            return False, _describe_http_status_error(exc)
        except Exception as exc:
            return False, f"probe transport error: {exc}"

        if resp.status_code == 200:
            # v2026.5.25 — Responses-API may legitimately return 200
            # with ``status: "incomplete"`` when reasoning tokens fill
            # the small budget before any visible output. That still
            # proves the model is reachable + the key is valid, which
            # is the probe's job. Treat as success.
            try:
                payload = resp.json()
            except Exception:
                return True, ""
            status_value = payload.get("status") if isinstance(payload, dict) else None
            if status_value == "failed":
                err = payload.get("error", {}) if isinstance(payload, dict) else {}
                msg = err.get("message", "") if isinstance(err, dict) else str(err)
                return False, f"probe response.status=failed{(' — ' + msg) if msg else ''}"
            return True, ""
        # Surface the structured server message — that's what the
        # operator needs (e.g. "This is not a chat model").
        try:
            err = resp.json().get("error", {})
            msg = err.get("message", "") if isinstance(err, dict) else str(err)
        except Exception:
            msg = resp.text[:200] if resp.text else ""
        return False, f"HTTP {resp.status_code}{(' — ' + msg) if msg else ''}"

    # ─────────────────────────────────────────────────────────────
    # v2026.5.23 — OpenAI /v1/responses adapter
    # ─────────────────────────────────────────────────────────────
    #
    # Pro models (gpt-5-pro / gpt-5.5-pro / o3-pro / deep-research /
    # gpt-5-codex / computer-use-preview) are flagged
    # ``ResponsesOnlyModel`` by OpenAI and either reject chat
    # completions outright or only accept the non-streaming subset.
    # FERAL's UI runtime depends on token streaming + multi-step tool
    # loops, so we wire a dedicated Responses-API path here.
    #
    # Wire shape — verified against the 2026-05 Responses API
    # reference + the function-call cookbook:
    #
    #   POST https://api.openai.com/v1/responses
    #     model            <required string>
    #     input            <string OR array of items>
    #     instructions     <optional string — system prompt>
    #     tools            <optional array — function schemas>
    #     tool_choice      <optional string|object>
    #     reasoning        { effort: none|minimal|low|medium|high|xhigh }
    #     max_output_tokens <int>
    #     stream           <bool>
    #     background       <bool — for jobs that may take minutes>
    #     previous_response_id <stateful continuation>
    #
    # SSE events the adapter consumes (terminal event names from the
    # docs dump + the externally-confirmed function-call deltas):
    #   response.created
    #   response.in_progress
    #   response.output_item.added
    #   response.output_text.delta              -> yield text_delta
    #   response.output_text.done
    #   response.function_call_arguments.delta  -> accumulate
    #   response.function_call_arguments.done   -> finalise tool call
    #   response.output_item.done
    #   response.completed                       -> yield done
    #   response.failed                          -> yield error
    #
    # Stateful mode: we omit ``previous_response_id`` and resend the
    # full message thread as ``input`` each turn. Simpler, matches the
    # chat-completions semantics callers already expect, no extra
    # bookkeeping on tool-call result resubmission. Switch to stateful
    # later if latency becomes a real issue.

    # v2026.5.25 — Responses-API content-part schema. The supported
    # part types per OpenAI docs (verified live against gpt-5.5-pro
    # on 2026-05-14) are EXACTLY:
    #
    #   input_text       — text in a user / system / tool input item
    #   input_image      — image in a user input item (url or b64)
    #   input_file       — file reference (file_id or url)
    #   output_text      — text in an assistant output item
    #   refusal          — refusal text in an assistant output item
    #   summary_text     — reasoning summary in an assistant item
    #   computer_screenshot — computer-use response
    #
    # Chat-Completions multimodal content uses the legacy types
    # `text` / `image_url`. FERAL's perception/fusion.py emits these
    # for every vision-enabled chat turn, so the Responses adapter
    # MUST translate them before POSTing or the API returns 400 with
    # "Invalid value: 'text'" (operator's exact production error).
    _RESPONSES_PART_TYPES_NATIVE = frozenset({
        "input_text", "input_image", "input_file",
        "output_text", "refusal", "summary_text",
        "computer_screenshot",
    })

    @staticmethod
    def _translate_content_part(role: str, part: dict) -> dict:
        """Translate one Chat-Completions content part to its
        Responses-API equivalent. Roles ``user`` / ``system`` / ``tool``
        accept input-side types; ``assistant`` accepts output-side
        types. Already-native Responses types pass through unchanged.

        Unknown types are passed through as-is so the OpenAI server
        is the source of truth for shape validation — we don't want
        to silently drop a new content type that lands at OpenAI
        before this code knows about it.
        """
        if not isinstance(part, dict):
            return part
        ptype = part.get("type", "")
        if ptype in LLMProvider._RESPONSES_PART_TYPES_NATIVE:
            return part

        # The two common Chat-Completions content-part types FERAL's
        # perception/fusion.py and the multimodal chat path emit.
        if ptype == "text":
            text = part.get("text", "")
            if role == "assistant":
                return {"type": "output_text", "text": text}
            return {"type": "input_text", "text": text}

        if ptype == "image_url":
            # Chat-Completions vision shape:
            #   {"type": "image_url", "image_url": {"url": "...", "detail": "low"}}
            # Responses-API vision shape:
            #   {"type": "input_image", "image_url": "https://..."}
            # OpenAI's Responses API takes `image_url` as a STRING (the
            # URL itself or a data: URL). Coerce the nested object.
            img = part.get("image_url")
            url = ""
            if isinstance(img, dict):
                url = img.get("url", "")
            elif isinstance(img, str):
                url = img
            translated: dict = {"type": "input_image", "image_url": url}
            # Preserve the `detail` hint when present so token-cost
            # behaviour matches the Chat-Completions caller.
            if isinstance(img, dict) and img.get("detail"):
                translated["detail"] = img["detail"]
            return translated

        # Unknown content-part type — pass through unchanged. OpenAI's
        # server is the source of truth for validation; logging a
        # warning here would spam for every legitimate new part type
        # we haven't catalogued yet.
        return part

    @staticmethod
    def _normalize_message_content(
        role: str, content
    ) -> "str | list[dict]":
        """Return content shaped for the Responses-API ``input`` array.

        * ``str`` is returned as-is — Responses accepts plain strings
          in role-bearing items (verified live; the API auto-wraps).
        * ``list[dict]`` has each part run through
          ``_translate_content_part`` so Chat-Completions
          ``text`` / ``image_url`` become Responses
          ``input_text`` / ``input_image`` (or ``output_text`` for
          assistant turns).
        * Anything else (e.g. None) falls back to an empty string so
          the API rejects on the body shape we control, not on a
          surprise content value.
        """
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return [LLMProvider._translate_content_part(role, p) for p in content]
        if content is None:
            return ""
        return str(content)

    @staticmethod
    def _messages_to_responses_input(messages: list[dict]) -> tuple[str, list[dict]]:
        """Translate OpenAI Chat-Completions ``messages`` into a
        Responses-API ``(instructions, input)`` pair.

        v2026.5.25 — content parts are now translated per role through
        ``_translate_content_part`` so multimodal turns from
        ``perception/fusion.py:to_llm_user_content`` (which emits
        ``[{type:"text"}, {type:"image_url"}]``) no longer 400 on
        ``/v1/responses``.

        * The first ``system`` message becomes ``instructions``.
        * Every remaining message becomes an ``input`` item with
          ``role`` + translated ``content``.
        * Assistant tool-call replays become ``function_call`` items.
        * ``tool`` role messages (results) become
          ``function_call_output`` items linked by ``call_id``.

        Returns ``(instructions, input_items)``. ``instructions`` may
        be the empty string when no system role was present.

        v2026.5.29 — defensive pairing guard. The Responses API rejects
        a request with ``400 No tool call found for function call output``
        whenever a ``function_call_output`` references a ``call_id``
        that has no preceding ``function_call`` in the same ``input``
        list. That can happen if upstream tail-truncation drops the
        assistant turn that announced a tool but keeps the following
        ``role:"tool"`` row, or if a stale snapshot replays orphan
        tool rows. We pre-scan the message list, collect every
        ``call_id`` that *will* be emitted (from assistant
        ``tool_calls``), and skip any ``tool`` row whose
        ``tool_call_id`` isn't in that set. This makes the translator
        the last line of defence regardless of how the history was
        produced.
        """
        announced_call_ids: set[str] = set()
        for msg in messages:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    if not isinstance(tc, dict):
                        continue
                    cid = tc.get("id")
                    if isinstance(cid, str) and cid:
                        announced_call_ids.add(cid)

        instructions = ""
        input_items: list[dict] = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                # First system wins as instructions; later ones become
                # input items so multi-step prompts (e.g. PromptRefiner
                # injecting a refined system note) aren't silently lost.
                if not instructions:
                    if isinstance(content, str):
                        instructions = content
                    elif isinstance(content, list):
                        # Extract text from content parts. Responses-API
                        # `instructions` is a plain string; flatten any
                        # text-bearing parts.
                        text_chunks: list[str] = []
                        for p in content:
                            if not isinstance(p, dict):
                                continue
                            t = p.get("text", "")
                            if t:
                                text_chunks.append(t)
                        instructions = "".join(text_chunks)
                    else:
                        instructions = str(content)
                    continue
                input_items.append({
                    "role": "system",
                    "content": LLMProvider._normalize_message_content("system", content),
                })
                continue
            if role == "tool":
                call_id = msg.get("tool_call_id") or msg.get("call_id") or ""
                # Pairing guard (v2026.5.29): drop tool results whose
                # matching assistant function_call is missing from this
                # request. OpenAI's Responses API returns
                # ``400 No tool call found for function call output``
                # the moment we send an orphan, so skip silently with a
                # single WARN per drop. The orphan can only come from
                # upstream history corruption (tail-slice truncation,
                # stale snapshot, branch/restore) — we never legitimately
                # want to send one.
                if not call_id or call_id not in announced_call_ids:
                    logger.warning(
                        "llm: dropping orphan tool result with call_id=%r "
                        "(no matching function_call in request input)",
                        call_id,
                    )
                    continue
                # `function_call_output.output` accepts a plain string
                # per OpenAI's Responses API. Tool results from FERAL's
                # tool_runner are already stringified before reaching
                # here (see agents/orchestrator.py history append paths),
                # so a defensive `str()` is enough.
                if isinstance(content, str):
                    output = content
                elif isinstance(content, list):
                    # Flatten content-parts back to a single string.
                    output = "".join(
                        p.get("text", "")
                        for p in content
                        if isinstance(p, dict)
                    )
                else:
                    output = str(content or "")
                input_items.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output,
                })
                continue
            if role == "assistant" and msg.get("tool_calls"):
                # Assistant message with a tool call request — replay
                # as function_call items so the model sees the same
                # history it produced.
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    args = fn.get("arguments")
                    if isinstance(args, dict):
                        args = json.dumps(args)
                    input_items.append({
                        "type": "function_call",
                        "call_id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "arguments": args or "{}",
                    })
                # If there's an accompanying assistant text body (some
                # models emit a short pre-tool narration), preserve it
                # as a translated assistant input item.
                if content:
                    normalized = LLMProvider._normalize_message_content("assistant", content)
                    if normalized:
                        input_items.append({"role": "assistant", "content": normalized})
                continue
            # Plain user / assistant content — translate parts.
            input_items.append({
                "role": role,
                "content": LLMProvider._normalize_message_content(role, content),
            })
        return instructions, input_items

    @staticmethod
    def _chat_tools_to_responses_tools(tools: Optional[list[dict]]) -> Optional[list[dict]]:
        """Convert Chat-Completions ``tools`` schemas to the
        Responses-API shape (flat ``{type, name, description, parameters}``)
        from the nested ``{type:"function", function:{...}}``."""
        if not tools:
            return None
        out: list[dict] = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
                fn = tool["function"]
                out.append({
                    "type": "function",
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
                })
            else:
                # Pass-through for already-flat tool schemas or
                # built-in tools (web_search, code_interpreter, etc).
                out.append({k: v for k, v in tool.items() if k != "_feral_meta"})
        return out

    def _build_responses_body(
        self,
        messages: list[dict],
        tools: Optional[list[dict]],
        temperature: float,
        max_tokens: int,
        *,
        stream: bool,
        force_tool: Optional[str] = None,
        model: Optional[str] = None,
    ) -> dict:
        """Canonical ``/v1/responses`` request body.

        ``model`` defaults to the live primary model. The failover loop
        passes the CANDIDATE's model instead, because a fallback hop to
        ``openai/gpt-5.6-sol`` must build a body for that model, not for
        whatever ``self.model`` happens to be on the misconfigured
        primary.
        """
        from agents.llm_reasoning import apply_responses_param_fork

        model = model or self.model
        instructions, input_items = self._messages_to_responses_input(messages)
        body: dict = {
            "model": model,
            "input": input_items,
            "max_tokens": max_tokens,           # apply_responses_param_fork renames
            "temperature": temperature,         # apply_responses_param_fork drops if !=1
            "stream": bool(stream),
        }
        if instructions:
            body["instructions"] = instructions
        # Routing hint for OpenAI's prompt cache. Requests carrying the
        # same key are steered to the same machine, which is what lets a
        # long shared prefix (the identity header, then the tool schemas)
        # actually hit the cache instead of being re-read at the full
        # input rate on every turn. There is no session id in scope at
        # this layer, and threading one down would touch every caller of
        # ``chat``, so this is a per-PROCESS key: every turn a brain
        # serves shares it, which is the grouping that matters, and a
        # restart starts a new one. Cached tokens are billed at a tenth
        # of the input rate for gpt-5.6-sol.
        body["prompt_cache_key"] = _PROMPT_CACHE_KEY
        # The 128-tool cap lived only on the chat/completions paths, and
        # this builder is the one gpt-5.6-sol actually uses
        # (``providers/model_classes.classify_endpoint`` routes it to
        # /v1/responses). So the model measured on the operator's brain
        # was receiving all 266 schemas: 32,391 tokens, 97% of every
        # request, before a single word of the conversation. Cap here,
        # BEFORE converting to the flat Responses shape, because
        # ``cap_tools_with_pins`` reads pin names off either shape but
        # the pin list and the coverage floor were written against the
        # chat-shape list and stay honest applied to the same input.
        #
        # The cap and the cache key work together rather than against
        # each other: caching pays off on a prefix that is identical
        # turn to turn, and a capped list is both shorter AND more
        # stable than one that varies with whatever the router picked.
        clean_tools = self._chat_tools_to_responses_tools(
            _cap_openai_chat_tools(tools) if tools else tools
        )
        if clean_tools:
            body["tools"] = clean_tools
            # Responses-API tool_choice uses the flat ``{"type":"function","name":<n>}``
            # shape (no nested ``function`` wrapper, unlike chat/completions).
            # Other providers don't reach this builder, so handle it inline.
            if force_tool and any(
                isinstance(t, dict) and t.get("name") == force_tool
                for t in clean_tools
            ):
                body["tool_choice"] = {"type": "function", "name": force_tool}
            else:
                body["tool_choice"] = "auto"
        apply_responses_param_fork(model, body)
        return body

    async def _post_responses(
        self,
        client: Any,
        model: str,
        messages: list[dict],
        tools: Optional[list[dict]],
        temperature: float,
        max_tokens: int,
        *,
        force_tool: Optional[str] = None,
        retry_max: Optional[int] = None,
        retry_delays: Optional[list[float]] = None,
    ) -> dict:
        """Non-streaming POST to ``/v1/responses`` on an explicit client
        and model. Raises on HTTP / transport failure (so the failover
        loop can classify it) and returns the payload normalised to the
        chat-completions shape.

        Shared by ``_responses_chat`` (primary, swallows errors into the
        ``{"error": ...}`` dict) and ``_call_provider`` (primary AND
        fallback candidates, propagates the exception). The fallback
        branch is why ``client`` and ``model`` are parameters: a
        fallback hop runs on a temporary ``httpx.AsyncClient`` built
        for the candidate's ``base_url`` + ``api_key``, never on
        ``self.client``.
        """
        body = self._build_responses_body(
            messages, tools, temperature, max_tokens, stream=False,
            force_tool=force_tool, model=model,
        )

        async def _do_responses():
            resp = await client.post("/responses", json=body)
            resp.raise_for_status()
            return resp.json()

        payload = await _retry_llm_call(
            _do_responses, max_retries=retry_max, delays=retry_delays,
        )
        return self._responses_payload_to_chat_dict(payload)

    async def _responses_chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]],
        temperature: float,
        max_tokens: int,
        *,
        force_tool: Optional[str] = None,
    ) -> dict:
        """Non-streaming POST to ``/v1/responses``. Returns a dict
        normalised to the chat-completions shape callers consume
        (``choices[0].message.content`` + optional ``tool_calls``).
        On error returns ``{"error": str, "choices": []}``.
        """
        from observability.metrics import increment, measure
        increment("feral.llm.calls_total", attributes={
            "provider": self.provider, "model": self.model, "endpoint": "responses",
        })
        try:
            with measure("feral.llm.latency", {
                "provider": self.provider, "model": self.model, "endpoint": "responses",
            }):
                return await self._post_responses(
                    self.client, self.model, messages, tools, temperature,
                    max_tokens, force_tool=force_tool,
                )
        except httpx.HTTPStatusError as e:
            increment("feral.llm.errors_total", attributes={
                "provider": self.provider, "model": self.model, "endpoint": "responses",
            })
            detail = _describe_http_status_error(e)
            logger.error("Responses API error: %s", detail)
            return {"error": detail, "choices": []}
        except Exception as e:
            increment("feral.llm.errors_total", attributes={
                "provider": self.provider, "model": self.model, "endpoint": "responses",
            })
            detail = _describe_error(e)
            logger.error("Responses API failed: %s", detail)
            return {"error": detail, "choices": []}

    async def _call_provider_responses(
        self,
        client: Any,
        provider_name: str,
        model: str,
        messages: list[dict],
        tools: Optional[list[dict]],
        temperature: float,
        max_tokens: int,
        *,
        force_tool: Optional[str] = None,
        retry_max: Optional[int] = None,
        retry_delays: Optional[list[float]] = None,
    ) -> dict:
        """``_call_provider`` leg for responses-only candidates.

        Keeps ``_call_provider``'s contract ("raises on error"): a
        payload that carries an ``error`` object is raised too, so the
        failover loop classifies it, cools the candidate down and moves
        on instead of handing the orchestrator an error dict that looks
        like a successful hop.
        """
        result = await self._post_responses(
            client, model, messages, tools, temperature, max_tokens,
            force_tool=force_tool, retry_max=retry_max, retry_delays=retry_delays,
        )
        if isinstance(result, dict) and result.get("error"):
            raise RuntimeError(
                f"{provider_name}/{model} responses: {result['error']}"
            )
        return result

    @staticmethod
    def _responses_payload_to_chat_dict(payload: dict) -> dict:
        """Walk the Responses API output and synthesise the same
        ``{choices:[{message:{content, tool_calls}}]}`` shape the
        chat-completions adapter returns. Keeps every existing caller
        (``extract_response``, the orchestrator, the digital twin)
        unchanged.

        v2026.5.25 — handles every output-item type Responses API
        emits that carries text or tool calls:

        * ``message`` items with ``content`` parts of type
          ``output_text`` (the standard reply text), ``text`` (legacy
          alias accepted server-side), and ``refusal`` (surfaces as
          ``[refusal] <text>`` so callers don't render an empty
          assistant turn when the model declined).
        * ``function_call`` items → chat-completions ``tool_calls``.
        * ``reasoning`` items with ``summary`` content parts of type
          ``summary_text`` — when the model exposes a brief reasoning
          summary, we surface it as a hidden ``_reasoning_summary``
          key so the orchestrator can show / log it without polluting
          the assistant message.

        Empty visible text + no tool calls is reported as an explicit
        ``finish_reason="incomplete"`` so the orchestrator's stream
        loop can yield a placeholder rather than appearing frozen.
        """
        if not isinstance(payload, dict):
            return {"error": "responses: empty payload", "choices": []}
        if payload.get("error"):
            err = payload["error"]
            return {"error": err.get("message", str(err)), "choices": []}
        text_chunks: list[str] = []
        refusal_chunks: list[str] = []
        reasoning_summary_chunks: list[str] = []
        tool_calls: list[dict] = []
        for item in payload.get("output", []) or []:
            if not isinstance(item, dict):
                continue
            it = item.get("type", "")
            if it == "message":
                for part in item.get("content", []) or []:
                    if not isinstance(part, dict):
                        continue
                    ptype = part.get("type", "")
                    if ptype in ("output_text", "text"):
                        t = part.get("text", "")
                        if t:
                            text_chunks.append(t)
                    elif ptype == "refusal":
                        refusal_chunks.append(part.get("refusal", "") or part.get("text", ""))
            elif it == "function_call":
                args = item.get("arguments", "{}")
                if not isinstance(args, str):
                    args = json.dumps(args)
                tool_calls.append({
                    "id": item.get("call_id", ""),
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": args,
                    },
                })
            elif it == "reasoning":
                # Reasoning items may carry a ``summary`` array with
                # ``summary_text`` parts. The model's actual reasoning
                # tokens stay opaque; the summary is a short rationale
                # the server explicitly exposes.
                summary = item.get("summary") or []
                if isinstance(summary, list):
                    for part in summary:
                        if isinstance(part, dict) and part.get("type") == "summary_text":
                            t = part.get("text", "")
                            if t:
                                reasoning_summary_chunks.append(t)

        visible_text = "".join(text_chunks)
        if refusal_chunks:
            # Make the refusal visible to the orchestrator + the user.
            # Prefix tagged so the chat surface knows this is a refusal
            # not a normal reply (Phase 7b chat rendering can style it).
            refusal_blob = "\n".join(r for r in refusal_chunks if r)
            visible_text = (
                f"{visible_text}\n\n[refusal] {refusal_blob}".strip()
                if visible_text
                else f"[refusal] {refusal_blob}"
            )

        message: dict = {"role": "assistant", "content": visible_text}
        if tool_calls:
            message["tool_calls"] = tool_calls

        status = payload.get("status", "")
        finish_reason = status if status else "stop"
        # If the model returned literally nothing user-visible and made
        # no tool call, mark the finish so the orchestrator can emit
        # a placeholder instead of a silent frozen turn. The most
        # common cause is `max_output_tokens` exhausted by reasoning.
        if not visible_text and not tool_calls and status != "failed":
            finish_reason = "incomplete"

        result: dict = {
            "id": payload.get("id", ""),
            "object": "chat.completion",
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": payload.get("usage", {}),
            "_responses_id": payload.get("id"),
        }
        if reasoning_summary_chunks:
            result["_reasoning_summary"] = "".join(reasoning_summary_chunks)
        return result

    async def _responses_stream(
        self,
        messages: list[dict],
        tools: Optional[list[dict]],
        temperature: float,
        max_tokens: int,
        *,
        force_tool: Optional[str] = None,
    ) -> AsyncGenerator[dict, None]:
        """Stream from ``/v1/responses`` using SSE. Yields the same
        event vocabulary the chat-completions stream yields
        (``text_delta`` / ``tool_call_delta`` / ``done`` / ``error``)
        so the orchestrator's stream loop stays unchanged.
        """
        from observability.metrics import increment
        body = self._build_responses_body(
            messages, tools, temperature, max_tokens, stream=True,
            force_tool=force_tool,
        )
        increment("feral.llm.calls_total", attributes={
            "provider": self.provider, "model": self.model, "endpoint": "responses_stream",
        })

        # v2026.5.27 — accumulator MUST be keyed by ``item_id`` (the
        # ``fc_…`` opaque id), NOT ``call_id`` (the ``call_…`` id sent
        # back to the model on tool_result). The Responses API uses
        # ``item_id`` in every streaming delta event
        # (``response.function_call_arguments.delta/.done``) while
        # ``response.output_item.added`` carries BOTH ``item.id``
        # (== item_id) and ``item.call_id``. v2026.5.25 keyed by
        # ``call_id`` in ``output_item.added``, then the delta events
        # set up a SECOND entry under ``item_id`` — so the entry with
        # the function NAME never accumulated args, and the entry
        # with the accumulated args had no name. Result: every Pro-
        # model streaming tool call landed at the orchestrator as
        # ``<name>({})`` (operator's demo hit the
        # ``web_search__web_search({})`` failure mode every turn).
        #
        # Fix: key the accumulator by ``item_id``, stash the
        # ``call_id`` inside the entry, and emit
        # ``{id: call_id, name, args}`` on completion so the
        # orchestrator + tool_runner see the correct tool-call shape.
        tool_calls: dict[str, dict] = {}
        stream_cm = None
        try:
            for _attempt in range(MAX_RETRIES):
                try:
                    stream_cm = self.client.stream("POST", "/responses", json=body)
                    resp = await stream_cm.__aenter__()
                    # v2026.5.28 — pull the body BEFORE raise_for_status so
                    # ``_describe_http_status_error`` can read the OpenAI
                    # error JSON (type / code / param / message) for
                    # operator-visible diagnostics. In streaming mode the
                    # body is lazy by default; without ``aread()`` the
                    # subsequent ``response.json()`` call returns ``{}``
                    # and the user sees only the bare httpx string
                    # ("Client error '400 Bad Request' for url ..."),
                    # which hides the actual cause of the 400.
                    #
                    # ``isinstance`` guard is for the unit-test fakes that
                    # back ``resp`` with a ``MagicMock`` — comparing a
                    # MagicMock to 400 raises ``TypeError``.
                    _status = getattr(resp, "status_code", None)
                    if isinstance(_status, int) and _status >= 400:
                        try:
                            await resp.aread()
                        except Exception:
                            pass
                    resp.raise_for_status()
                    break
                except Exception as e:
                    if stream_cm:
                        try:
                            await stream_cm.__aexit__(type(e), e, e.__traceback__)
                        except Exception:
                            pass
                        stream_cm = None
                    err_str = str(e).lower()
                    retriable = any(c in err_str for c in _RETRIABLE_CODES)
                    if not retriable or _attempt == MAX_RETRIES - 1:
                        raise
                    logger.warning("Responses stream connect failed (attempt %d/%d)",
                                   _attempt + 1, MAX_RETRIES)
                    await asyncio.sleep(RETRY_DELAYS[_attempt])

            current_event: Optional[str] = None
            async for line in resp.aiter_lines():
                if line is None or line == "":
                    current_event = None
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("event: "):
                    current_event = line[7:].strip()
                    continue
                if not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    for tc in tool_calls.values():
                        yield {"type": "tool_call_delta", "tool_call": _finalise_tool_call(tc)}
                    yield {"type": "done"}
                    return
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                event_type = current_event or chunk.get("type", "")

                if event_type == "response.output_text.delta":
                    delta_txt = chunk.get("delta", "") or chunk.get("text", "")
                    if delta_txt:
                        clean = sanitize_assistant_display_text(delta_txt)
                        if clean:
                            yield {"type": "text_delta", "content": clean}
                elif event_type == "response.function_call_arguments.delta":
                    item_id = chunk.get("item_id", "") or chunk.get("id", "")
                    entry = tool_calls.setdefault(item_id, {
                        "item_id": item_id, "call_id": "", "name": "", "arguments": "",
                    })
                    delta_args = chunk.get("delta", "") or chunk.get("arguments", "")
                    if isinstance(delta_args, str):
                        entry["arguments"] += delta_args
                elif event_type == "response.output_item.added":
                    item = chunk.get("item", {}) or {}
                    if item.get("type") == "function_call":
                        # Key by item.id (the "fc_…" opaque id) so the
                        # subsequent arguments-delta events hit the SAME
                        # entry. The model-facing tool-call identifier
                        # is item.call_id ("call_…"); stash it on the
                        # entry so we can emit it on completion.
                        item_id = item.get("id", "") or item.get("call_id", "")
                        entry = tool_calls.setdefault(item_id, {
                            "item_id": item_id, "call_id": "", "name": "", "arguments": "",
                        })
                        if item.get("call_id") and not entry["call_id"]:
                            entry["call_id"] = item["call_id"]
                        if item.get("name"):
                            entry["name"] = item["name"]
                        if item.get("arguments") and not entry["arguments"]:
                            entry["arguments"] = item["arguments"]
                elif event_type == "response.function_call_arguments.done":
                    item_id = chunk.get("item_id", "") or chunk.get("id", "")
                    entry = tool_calls.get(item_id)
                    if entry is not None:
                        # The ``done`` event carries the final
                        # arguments string. Use it as the source of
                        # truth in case any delta was lost.
                        final_args = chunk.get("arguments")
                        if isinstance(final_args, str) and final_args:
                            entry["arguments"] = final_args
                        name = chunk.get("name")
                        if name and not entry.get("name"):
                            entry["name"] = name
                elif event_type == "response.output_item.done":
                    # Final consistent payload — the item.added event
                    # may have emitted before arguments accumulated;
                    # done has them. Use it to backfill anything we
                    # might have missed (defensive).
                    item = chunk.get("item", {}) or {}
                    if item.get("type") == "function_call":
                        item_id = item.get("id", "") or item.get("call_id", "")
                        entry = tool_calls.setdefault(item_id, {
                            "item_id": item_id, "call_id": "", "name": "", "arguments": "",
                        })
                        if item.get("call_id") and not entry.get("call_id"):
                            entry["call_id"] = item["call_id"]
                        if item.get("name"):
                            entry["name"] = item["name"]
                        if item.get("arguments"):
                            entry["arguments"] = item["arguments"]
                elif event_type == "response.completed":
                    for tc in tool_calls.values():
                        yield {"type": "tool_call_delta", "tool_call": _finalise_tool_call(tc)}
                    # The Responses API reports token usage inside the
                    # terminal event's ``response`` object, with no opt-in
                    # required (unlike chat-completions, where the usage
                    # chunk only arrives if the request asked for
                    # ``stream_options.include_usage``, which
                    # ``chat_stream`` now does). It was arriving here and being
                    # discarded, which had two consequences: the UI could
                    # never show per-turn tokens, and ``_budget_record``
                    # billed every streamed turn at ZERO tokens, so the
                    # operator's cost caps silently under-counted the
                    # default code path.
                    _resp = chunk.get("response") or {}
                    _done: dict = {"type": "done"}
                    _u = _resp.get("usage")
                    if isinstance(_u, dict):
                        # Responses names these input_/output_tokens; keep the
                        # provider's own keys AND the normalised pair so
                        # downstream readers don't need to know the shape.
                        _in = _u.get("input_tokens") or _u.get("prompt_tokens") or 0
                        _out = _u.get("output_tokens") or _u.get("completion_tokens") or 0
                        _done["usage"] = {
                            "input_tokens": int(_in),
                            "output_tokens": int(_out),
                            "total_tokens": int(_u.get("total_tokens") or (_in + _out)),
                        }
                    if _resp.get("model"):
                        # The model that actually answered, which can differ
                        # from the configured one after failover.
                        _done["model"] = str(_resp["model"])
                    yield _done
                    return
                elif event_type == "response.failed":
                    err = chunk.get("response", {}).get("error") or chunk.get("error")
                    msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    yield {"type": "error", "content": f"responses failed: {msg}"}
                    return
                # Unhandled event types (audio.*, code_interpreter.*,
                # reasoning_text.*, etc) are intentionally ignored;
                # they don't carry user-facing text or tool calls in
                # the FERAL chat surface today.

            # Stream ended without an explicit completed/failed marker —
            # treat as success so the orchestrator commits whatever it
            # got and the user sees a reply.
            for tc in tool_calls.values():
                yield {"type": "tool_call_delta", "tool_call": _finalise_tool_call(tc)}
            yield {"type": "done"}
        except httpx.HTTPStatusError as e:
            detail = _describe_http_status_error(e)
            logger.error("Responses stream HTTP error: %s", detail)
            yield {"type": "error", "content": detail}
        except Exception as e:
            detail = _describe_error(e)
            logger.error("Responses stream failed: %s", detail)
            yield {"type": "error", "content": detail}
        finally:
            if stream_cm:
                try:
                    await stream_cm.__aexit__(None, None, None)
                except Exception:
                    pass

    async def _stream_via_nonstream_failover(
        self,
        messages: list[dict],
        tools: Optional[list[dict]],
        temperature: float,
        max_tokens: int,
        *,
        primary_error: Exception,
        force_tool: Optional[str] = None,
    ) -> Optional[list[dict]]:
        """Fallback path for streaming failures.

        For providers that support cross-provider failover in non-stream
        mode, run one failover attempt and convert the response into
        stream-shaped events.
        """
        fallbacks = self._config.get("fallback_providers") if isinstance(self._config, dict) else None
        if not fallbacks:
            return None
        if self._local_engine and self.provider in ("local", "hybrid"):
            return None

        reason = classify_error(primary_error)
        # Don't hide context overflow; the caller should surface the
        # explicit model/context failure.
        if reason == FailoverReason.CONTEXT_OVERFLOW:
            return None

        try:
            self._cooldown.record_failure(self.provider, reason)
            # Prevent immediate re-probe of the same failing primary in
            # chat_with_failover's candidate loop.
            self._cooldown._last_probe[self.provider] = time.time()
        except Exception:
            pass

        logger.warning(
            "Stream primary %s/%s failed (%s); attempting non-stream failover",
            self.provider,
            self.model,
            reason.value,
        )
        try:
            result = await self.chat_with_failover(
                messages,
                tools,
                temperature=temperature,
                max_tokens=max_tokens,
                force_tool=force_tool,
            )
        except Exception as exc:
            primary_detail = _describe_error(primary_error)
            failover_detail = _describe_error(exc)
            logger.warning(
                "Non-stream failover attempt after stream failure exhausted: %s",
                failover_detail,
            )
            return [{
                "type": "error",
                "content": (
                    f"{primary_detail} | failover exhausted: {failover_detail}"
                ),
            }]

        if not isinstance(result, dict):
            return None
        if result.get("error"):
            return None
        if not result.get("choices"):
            return None

        text, tool_calls = self.extract_response(result)
        events: list[dict] = []
        if text:
            clean = sanitize_assistant_display_text(text)
            if clean:
                events.append({"type": "text_delta", "content": clean})
        for tc in tool_calls:
            events.append({"type": "tool_call_delta", "tool_call": tc})
        events.append({"type": "done"})
        return events

    async def chat_stream(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        *,
        call_site: str = "chat",
        force_tool: Optional[str] = None,
    ) -> AsyncGenerator[dict, None]:
        """
        Stream a chat completion. Yields delta dicts:
          {"type": "text_delta", "content": "..."}
          {"type": "tool_call_delta", "tool_call": {...}}
          {"type": "done"}
          {"type": "error", "content": "..."}
          {"type": "budget_exceeded", "payload": {...}}  #  Lane 09

        ``call_site`` defaults to ``"chat"``; background loops opt
        into their own caps by passing ``call_site="screen_loop"``,
        ``"learner"``, etc. (Wave 2 Lane 09).
        """
        if self._messages_contain_vision(messages):
            ok, reason = self._vision_support_status()
            if not ok:
                yield {"type": "error", "content": reason}
                return

        # Cost-budget pre-flight (Wave 2 Lane 09). We can't stream a
        # call we already know will exceed the cap; the caller sees
        # ``budget_exceeded`` and stops.
        budget_block = await self._budget_check(call_site, self.model, max_tokens)
        if budget_block is not None:
            yield {
                "type": "budget_exceeded",
                "payload": budget_block.get("budget_exceeded", {}),
                "content": budget_block.get("error", "budget exceeded"),
            }
            return

        if not is_supported_runtime_provider(self.provider) and self.provider not in ("local", "hybrid"):
            yield {
                "type": "error",
                "content": (
                    f"Selected LLM provider {self.provider!r} is not supported by this "
                    f"runtime. Supported: {sorted(SUPPORTED_RUNTIME_PROVIDERS)}."
                ),
            }
            return

        if self.provider == "codex":
            if self._messages_contain_vision(messages):
                yield {
                    "type": "error",
                    "content": (
                        "Codex provider image translation is not available yet; "
                        "send a text-only turn or choose a vision provider."
                    ),
                }
                return
            streamed_text = False
            try:
                adapter = self._get_codex_adapter()
                converted = self._codex_messages(messages)
                async for event in adapter.stream_events(
                    converted, model=self.model, tools=tools
                ):
                    if event.get("type") == "text_delta":
                        streamed_text = True
                        yield {
                            "type": "text_delta",
                            "content": event.get("content") or "",
                        }
                    elif event.get("type") == "done":
                        result = {
                            "usage": event.get("usage") or {},
                            "choices": [],
                        }
                        await self._budget_record(call_site, self.model, result)
                        yield {"type": "done"}
                return
            except Exception as exc:
                detail = _describe_error(exc)
                logger.error("Codex app-server stream failed: %s", detail)
                if not streamed_text:
                    failover_events = await self._stream_via_nonstream_failover(
                        messages,
                        tools,
                        temperature,
                        max_tokens,
                        primary_error=exc,
                        force_tool=force_tool,
                    )
                    if failover_events:
                        for event in failover_events:
                            yield event
                        return
                yield {"type": "error", "content": detail}
                return

        # v2026.5.23 — Responses-API stream route for OpenAI Pro / o-Pro /
        # deep-research / Codex / computer-use models. Yields the same
        # event vocabulary (text_delta / tool_call_delta / done / error)
        # so orchestrator.handle_command_stream stays unchanged.
        try:
            from providers.model_classes import classify_endpoint
            stream_is_responses = (
                classify_endpoint(self.provider, self.model) == "responses"
            )
        except Exception as resp_exc:
            logger.warning("Responses-API stream classification raised: %s", resp_exc)
            stream_is_responses = False
        if stream_is_responses:
            try:
                streamed_anything = False
                async for event in self._responses_stream(
                    messages, tools, temperature, max_tokens,
                    force_tool=force_tool,
                ):
                    if event.get("type") in ("text_delta", "tool_call_delta"):
                        streamed_anything = True
                    if event.get("type") == "error" and not streamed_anything:
                        # Pre-token failure. Same treatment as the SSE
                        # and Anthropic stream branches: try the
                        # non-stream failover chain (which now routes
                        # responses-only candidates correctly) before
                        # surfacing the error. Guarded because
                        # ``_stream_via_nonstream_failover`` reads
                        # ``self._config``, which test doubles built via
                        # ``__new__`` may not have.
                        failover_events = None
                        try:
                            failover_events = await self._stream_via_nonstream_failover(
                                messages, tools, temperature, max_tokens,
                                primary_error=RuntimeError(
                                    str(event.get("content") or "responses stream failed")
                                ),
                                force_tool=force_tool,
                            )
                        except Exception as fo_exc:
                            logger.debug(
                                "Responses stream failover skipped: %s", fo_exc,
                            )
                        if failover_events:
                            for fo_event in failover_events:
                                yield fo_event
                            return
                        yield event
                        return
                    if event.get("type") == "done" and event.get("usage"):
                        # Record the turn against the operator's cost caps.
                        # ``_budget_record`` is otherwise only reached from
                        # the NON-streaming paths (``chat`` at :806/:932 and
                        # ``chat_with_failover`` at :4102), so every streamed
                        # turn was being billed at ZERO tokens and the
                        # per-call-site caps in settings.json never moved for
                        # it. Both paths have to record, and now both do.
                        #
                        # Scope note (v2026.7): ``features.streaming``
                        # now defaults to True (config/loader.py
                        # ``DEFAULT_STREAMING``), so a fresh profile
                        # serves chat from ``handle_command_stream``.
                        # All THREE stream routes now record: this
                        # Responses one, the chat-completions SSE
                        # branch below (which asks for
                        # ``stream_options.include_usage`` and bills
                        # off the final usage chunk) and
                        # ``_chat_stream_anthropic`` (which bills off
                        # ``message_start`` + ``message_delta``). Each
                        # records exactly once per turn on completion.
                        #
                        # ``_extract_usage`` already accepts the
                        # ``{usage: {input_tokens, output_tokens}}`` shape the
                        # Responses terminal event carries, so the dict goes
                        # straight in. Bill against the model that ANSWERED
                        # (present on the event after failover), not the one
                        # configured. ``_budget_record`` is best-effort and
                        # never raises.
                        await self._budget_record(
                            call_site,
                            str(event.get("model") or self.model),
                            event,
                        )
                    yield event
                # Done, whether or not anything streamed. This used to
                # fall through to the chat-completions SSE body below
                # when the Responses stream ended without content,
                # "so failover / OR still gets a chance". For a
                # responses-only model that second request can only
                # 400 (tools + reasoning_effort on /chat/completions,
                # the brain.err signature from 2026-08-01 to 2026-09-02)
                # and the orchestrator's empty-response retry already
                # covers the no-content case.
                return
            except Exception as resp_exc:
                detail = _describe_error(resp_exc)
                logger.warning("Responses-API stream route raised: %s", detail)
                yield {"type": "error", "content": detail}
                return

        model_guard_error = _chat_completions_model_guard(self.provider, self.model)
        if model_guard_error:
            yield {"type": "error", "content": model_guard_error}
            return

        # Local streaming path
        if self._local_engine and self.provider in ("local", "hybrid"):
            use_local = self.provider == "local" or not self._hybrid_cloud_provider
            if self.provider == "hybrid" and tools:
                use_local = False
            if use_local:
                try:
                    if not self._local_engine.loaded:
                        await self._local_engine.load_model()
                    prompt = self._local_engine.format_chat(messages, tools)
                    async for token in self._local_engine.generate_stream(prompt, max_tokens=max_tokens, temperature=temperature):
                        yield {"type": "text_delta", "content": token}
                    yield {"type": "done"}
                except Exception as e:
                    yield {"type": "error", "content": str(e)}
                return

        # Anthropic native streaming (Messages API with SSE).
        # The Anthropic branch tracks first-token state internally and,
        # on a pre-token failure, hands off to
        # ``_stream_via_nonstream_failover`` so cross-provider failover
        # has the same parity as the OpenAI-compat path below. 
        # this branch returned ``error`` events with no failover at
        # all — see findings/13-llm-core.md fix #1 + #5.
        if self.provider == "anthropic":
            # ``call_site`` is threaded through so the Anthropic stream
            # can bill this turn against the SAME per-call-site cap the
            # non-stream path uses. Without it the branch would have to
            # assume "chat" and background loops (screen_loop, learner)
            # would bill to the wrong bucket.
            async for delta in self._chat_stream_anthropic(
                messages, tools, temperature, max_tokens,
                force_tool=force_tool,
                call_site=call_site,
            ):
                yield delta
            return

        body: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }

        if tools:
            clean_tools = [{k: v for k, v in t.items() if k != "_feral_meta"} for t in tools]
            if self.provider in ("openai",):
                clean_tools = _cap_openai_chat_tools(clean_tools)
            body["tools"] = clean_tools
            body["tool_choice"] = _resolve_tool_choice(
                self.provider, clean_tools, force_tool,
            )

        apply_reasoning_fork(self.provider, self.model, body)

        # Ask for the usage chunk. On chat-completions this is opt-in:
        # without ``stream_options.include_usage`` the provider closes
        # the stream after the last content delta and NEVER reports
        # tokens, so ``_budget_record`` below has nothing to bill and
        # the operator's caps sit at $0 forever. That mattered less
        # when streaming was opt-in; ``features.streaming`` now
        # defaults to True (config/loader.py ``DEFAULT_STREAMING``),
        # making this the primary chat path.
        #
        # ``_STREAM_OPTIONS_UNSUPPORTED`` (module level) records the
        # endpoints that 400 on the key so we only pay the extra
        # round-trip once per process.
        _usage_endpoint = (self.provider, str(self.base_url or ""))
        _want_usage = _usage_endpoint not in _STREAM_OPTIONS_UNSUPPORTED
        if _want_usage:
            body["stream_options"] = {"include_usage": True}

        streamed_text = False
        stream_cm = None
        usage_raw: Optional[dict] = None
        answering_model = ""
        billed = False

        async def _record_stream_usage() -> None:
            """Bill this turn exactly once, on stream completion.

            Guarded by ``billed`` because the SSE loop has two exits
            ([DONE] and stream-closed) and double-billing is worse than
            not billing. When the provider sent no usage block we
            record NOTHING: there is no documented estimator here, and
            a fabricated number is worse than a visible gap.
            """
            nonlocal billed
            if billed or usage_raw is None:
                return
            billed = True
            await self._budget_record(
                call_site,
                answering_model or self.model,
                {"usage": usage_raw},
            )

        async def _open_stream(req_body: dict):
            """Open the SSE stream with the existing retry policy.

            Extracted from the inline loop so the
            ``stream_options``-rejected retry below can re-run it with
            a stripped body without duplicating the retry semantics.
            """
            nonlocal stream_cm
            for _attempt in range(MAX_RETRIES):
                try:
                    stream_cm = self.client.stream("POST", "/chat/completions", json=req_body)
                    _resp = await stream_cm.__aenter__()
                    # v2026.5.28 — see ``_responses_stream`` for the
                    # rationale: pull the body before raise_for_status
                    # so the OpenAI / Anthropic / DeepSeek error JSON
                    # survives into ``_describe_http_status_error``.
                    # ``isinstance`` guard for MagicMock-backed unit
                    # tests (``>= 400`` raises TypeError on MagicMock).
                    _status = getattr(_resp, "status_code", None)
                    if isinstance(_status, int) and _status >= 400:
                        try:
                            await _resp.aread()
                        except Exception:
                            pass
                    _resp.raise_for_status()
                    return _resp
                except Exception as e:
                    if stream_cm:
                        try:
                            await stream_cm.__aexit__(type(e), e, e.__traceback__)
                        except Exception:
                            pass
                        stream_cm = None
                    err_str = str(e).lower()
                    retriable = any(c in err_str for c in _RETRIABLE_CODES)
                    if not retriable or _attempt == MAX_RETRIES - 1:
                        raise
                    logger.warning("LLM stream connect failed (attempt %d/%d) — retrying",
                                   _attempt + 1, MAX_RETRIES)
                    await asyncio.sleep(RETRY_DELAYS[_attempt])
            raise RuntimeError("stream retry loop exhausted without a response")

        try:
            try:
                resp = await _open_stream(body)
            except Exception as first_exc:
                # A 4xx while we are asking for usage may be the strict
                # shims (some vLLM / llama.cpp / gateway builds) that
                # reject unknown body keys outright. Retry the same turn
                # once without ``stream_options`` so losing usage data
                # never costs the user their answer. Anything else, or a
                # second failure, falls through to the existing
                # failover/error handling untouched.
                _exc_status = getattr(getattr(first_exc, "response", None), "status_code", None)
                if not (_want_usage and isinstance(_exc_status, int) and _exc_status in (400, 422)):
                    raise
                body.pop("stream_options", None)
                _want_usage = False
                resp = await _open_stream(body)
                # Only now is it proven the key was the problem: the
                # same request minus ``stream_options`` succeeded.
                _STREAM_OPTIONS_UNSUPPORTED.add(_usage_endpoint)
                logger.warning(
                    "%s at %s rejected stream_options.include_usage (HTTP %s); "
                    "retried without it. Streamed turns for this endpoint will "
                    "not be billed against cost caps.",
                    self.provider, self.base_url, _exc_status,
                )

            accumulated_tool_calls: dict[int, dict] = {}
            async for line in resp.aiter_lines():
                # Tolerate SSE keep-alive comment lines ("keep-alive"
                # ``: ...`` comments and empty lines). DeepSeek's
                # thinking-mode stream, OpenRouter's queue, and some
                # Anthropic variants send these during long reasoning
                # windows; treating them as termination kills the
                # stream prematurely. Per DeepSeek's 2026-04-26 docs
                # the keep-alive can run up to 10 minutes.
                if line is None or line == "" or line.startswith(":"):
                    continue
                if not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    for _, tc in sorted(accumulated_tool_calls.items()):
                        tc["args"], tc["args_error"] = parse_tool_arguments(
                            tc.get("arguments", ""), tc.get("name", ""),
                        )
                        yield {"type": "tool_call_delta", "tool_call": tc}
                    await _record_stream_usage()
                    _done_event: dict = {"type": "done"}
                    _usage_payload = _usage_event_payload(usage_raw)
                    if _usage_payload:
                        _done_event["usage"] = _usage_payload
                    yield _done_event
                    return

                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                # The usage chunk. OpenAI sends it as the LAST chunk
                # before ``[DONE]`` with an EMPTY ``choices`` array;
                # OpenRouter and DeepSeek instead attach ``usage`` to
                # the final content chunk. Read it off any chunk that
                # carries one and keep the last, so both shapes work.
                _chunk_usage = chunk.get("usage")
                if isinstance(_chunk_usage, dict) and _chunk_usage:
                    usage_raw = _chunk_usage
                if chunk.get("model"):
                    # Bill against the model that ANSWERED. OpenRouter
                    # substitutes models transparently, so the
                    # configured id is not always the billed one.
                    answering_model = str(chunk["model"])

                # ``choices`` is empty on the usage chunk. The old
                # ``chunk.get("choices", [{}])[0]`` raised IndexError
                # there, which the outer handler turned into an
                # ``error`` event at the very end of an otherwise
                # successful turn. That never fired before because we
                # never asked for usage; it would fire on every turn now.
                _choices = chunk.get("choices") or []
                if not _choices:
                    continue
                delta = _choices[0].get("delta") or {}

                if delta.get("content"):
                    piece = sanitize_assistant_display_text(delta["content"])
                    if piece:
                        streamed_text = True
                        yield {"type": "text_delta", "content": piece}

                for tc_delta in delta.get("tool_calls", []):
                    idx = tc_delta.get("index", 0)
                    if idx not in accumulated_tool_calls:
                        accumulated_tool_calls[idx] = {
                            "id": tc_delta.get("id", ""),
                            "name": "",
                            "arguments": "",
                        }
                    entry = accumulated_tool_calls[idx]
                    func = tc_delta.get("function", {})
                    if func.get("name"):
                        entry["name"] = func["name"]
                    if func.get("arguments"):
                        entry["arguments"] += func["arguments"]
                    if tc_delta.get("id"):
                        entry["id"] = tc_delta["id"]

            # Stream closed without an explicit ``[DONE]`` sentinel.
            # Some OpenAI-compatible servers (and proxies that drop the
            # trailing sentinel) end this way. The turn already
            # delivered its text, so bill whatever usage arrived rather
            # than losing the turn's cost. ``_record_stream_usage`` is
            # idempotent, so the ``[DONE]`` path above cannot double-bill.
            await _record_stream_usage()

        except httpx.HTTPStatusError as e:
            detail = _describe_http_status_error(e)
            logger.error("LLM stream error: %s", detail)
            if not streamed_text:
                failover_events = await self._stream_via_nonstream_failover(
                    messages,
                    tools,
                    temperature,
                    max_tokens,
                    primary_error=e,
                    force_tool=force_tool,
                )
                if failover_events:
                    for event in failover_events:
                        yield event
                    return
            yield {"type": "error", "content": detail}
        except Exception as e:
            detail = _describe_error(e)
            logger.error("LLM stream failed: %s", detail)
            if not streamed_text:
                failover_events = await self._stream_via_nonstream_failover(
                    messages,
                    tools,
                    temperature,
                    max_tokens,
                    primary_error=e,
                    force_tool=force_tool,
                )
                if failover_events:
                    for event in failover_events:
                        yield event
                    return
            yield {"type": "error", "content": detail}
        finally:
            if stream_cm:
                try:
                    await stream_cm.__aexit__(None, None, None)
                except Exception:
                    pass

    async def _chat_stream_anthropic(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        *,
        force_tool: Optional[str] = None,
        call_site: str = "chat",
    ) -> AsyncGenerator[dict, None]:
        """Native Anthropic Messages API streaming via SSE.

        Three structural changes vs. the  implementation:

        1. **base_url propagation.** The POST URL is now derived from
           ``self.base_url`` (which is in turn driven by the
           ``FERAL_LLM_BASE_URL`` override / ``switch_provider`` /
           ``reconfigure`` plumbing). The previous hard-coded literal
           silently ignored every operator override — including the
           override we use to test failover by pointing the primary
           at a deliberately-broken host. See findings/13-llm-core.md
           fix #1 + #3.

        2. **Pre-token failover parity with OpenAI.** When the request
           fails before any text/tool-call delta has been emitted, we
           hand off to ``_stream_via_nonstream_failover`` (the same
           helper the OpenAI-compat branch uses ~line 1984). 
           this branch yielded a single ``error`` event and returned —
           there was no cross-provider failover for Anthropic streams
           at all, even when ``fallback_providers`` had viable
           candidates. The handoff respects ``streamed_text`` so a
           mid-stream failure (after the user has already seen tokens)
           is not silently restarted.

        3. **Usage capture.** The ``message_delta`` event was matched
           and then dropped (``pass``), which is exactly where
           Anthropic reports ``usage.output_tokens``. With it discarded
           there was nothing to bill, so streamed Anthropic turns never
           reached ``cost_events`` and the operator's caps never moved
           for them. Input tokens arrive earlier, on ``message_start``,
           so both events have to be read to bill a turn.
        """
        # ``self.api_key`` is the source of truth — it's what
        # ``switch_provider`` and ``reconfigure`` write. When empty,
        # Cross-cut #1 (v2026.5.42) routes the lookup through the
        # labeled-keys vault overlay first, env second (kept here as
        # the final fallback ``_resolve_api_key`` covers internally).
        api_key = self.api_key or _resolve_api_key("anthropic")
        # ``self.base_url`` already includes ``/v1`` for Anthropic
        # (see ``__init__``); the messages endpoint is path-relative.
        base = (self.base_url or "https://api.anthropic.com/v1").rstrip("/")
        url = f"{base}/messages"

        # A5: same OpenAI → Anthropic conversion as the non-stream path.
        # Streaming previously forwarded ``role: "tool"`` as-is and
        # produced the same 400 on tool-using transcripts.
        system_prompt, anthropic_messages = _convert_messages_for_anthropic(messages)

        body: dict = {
            "model": self.model,
            "messages": anthropic_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        if system_prompt.strip():
            body["system"] = system_prompt.strip()
        # Thinking-capable Claude models demand ``thinking`` + drop
        # ``temperature`` when streaming; adaptive (Opus 4.7) passes
        # through unchanged.
        apply_reasoning_fork("anthropic", self.model, body)
        _enforce_anthropic_thinking_max_tokens(body)
        if tools:
            anth_tools = [
                {
                    "name": t.get("function", {}).get("name", t.get("name", "")),
                    "description": t.get("function", {}).get("description", ""),
                    "input_schema": t.get("function", {}).get("parameters", {}),
                }
                for t in tools if t.get("type") == "function" or "function" in t
            ]
            body["tools"] = anth_tools
            if force_tool and any(t.get("name") == force_tool for t in anth_tools):
                translated = to_provider_tool_choice("anthropic", force_tool)
                if translated is not None:
                    body["tool_choice"] = translated

        accumulated_tool_calls: dict[str, dict] = {}
        streamed_text = False
        primary_error: Optional[Exception] = None
        # Usage is split across two events on the Anthropic wire:
        # ``message_start`` carries ``usage.input_tokens`` (the prompt
        # is already counted when the model starts) and ``message_delta``
        # carries the running ``usage.output_tokens``, updated as the
        # response grows. Neither event alone can bill a turn.
        usage_input = 0
        usage_output = 0
        # Prompt-cache tokens ride the SAME ``message_start`` usage
        # block as ``input_tokens``, as two sibling fields, and are
        # NOT included in it. They are billed at their own rates now
        # that ``cost/pricing.py`` carries them; previously they were
        # dropped, so a cache-heavy streamed turn under-counted spend.
        usage_cache_write = 0
        usage_cache_read = 0
        saw_usage = False
        billed = False
        answering_model = ""

        async def _record_anthropic_usage() -> None:
            """Bill this turn exactly once, on stream completion.

            ``billed`` guards the two completion exits (``message_stop``
            and the stream closing without one). When the stream carried
            no usage at all we record nothing rather than invent a
            number: there is no estimator here to reuse.

            The usage dict handed to ``_budget_record`` is the same
            shape the non-stream Anthropic path now returns, cache
            fields included, so both routes go through one billing
            implementation and a turn costs the same either way.
            """
            nonlocal billed
            if billed or not saw_usage:
                return
            billed = True
            await self._budget_record(
                call_site,
                answering_model or self.model,
                {"usage": {
                    "input_tokens": usage_input,
                    "output_tokens": usage_output,
                    "cache_creation_input_tokens": usage_cache_write,
                    "cache_read_input_tokens": usage_cache_read,
                }},
            )

        def _absorb_cache_usage(block: dict) -> None:
            """Take the cache counters off an Anthropic usage block.

            Both ``message_start`` and ``message_delta`` are fed
            through here: the prompt-cache counters are settled when
            the prompt is processed, so ``message_start`` normally
            carries them, but a ``message_delta`` that repeats them
            must not be allowed to reset them to 0 either. Hence the
            LAST non-zero value wins rather than the last value.

            Reading these does not set ``saw_usage``: cache counters
            alone are not enough to bill a turn, and a stream that
            reported nothing else must stay unrecorded rather than be
            billed as a zero-input turn.
            """
            nonlocal usage_cache_write, usage_cache_read
            for key, is_write in (
                ("cache_creation_input_tokens", True),
                ("cache_read_input_tokens", False),
            ):
                raw = block.get(key)
                if raw is None:
                    continue
                try:
                    value = max(0, int(raw))
                except (TypeError, ValueError):
                    continue
                if not value:
                    continue
                if is_write:
                    usage_cache_write = value
                else:
                    usage_cache_read = value

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream(
                    "POST", url,
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json=body,
                ) as resp:
                    # Pull the body for non-2xx so the upstream JSON
                    # error message survives into
                    # ``_describe_http_status_error`` instead of being
                    # collapsed to a bare status line.
                    _status = getattr(resp, "status_code", None)
                    if isinstance(_status, int) and _status >= 400:
                        try:
                            await resp.aread()
                        except Exception:
                            pass
                    resp.raise_for_status()
                    current_tool_id = ""
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:].strip()
                        if not data_str:
                            continue
                        try:
                            event = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        event_type = event.get("type", "")

                        if event_type == "message_start":
                            _msg = event.get("message") or {}
                            _u = _msg.get("usage")
                            if isinstance(_u, dict):
                                _in = _u.get("input_tokens")
                                if _in is not None:
                                    try:
                                        usage_input = int(_in)
                                        saw_usage = True
                                    except (TypeError, ValueError):
                                        pass
                                # ``message_start`` also carries an
                                # opening ``output_tokens`` (usually 1);
                                # ``message_delta`` supersedes it below.
                                _out = _u.get("output_tokens")
                                if _out is not None:
                                    try:
                                        usage_output = int(_out)
                                        saw_usage = True
                                    except (TypeError, ValueError):
                                        pass
                                # Prompt-cache counters land here, as
                                # siblings of ``input_tokens``.
                                _absorb_cache_usage(_u)
                            if _msg.get("model"):
                                # Bill the model that ANSWERED, which
                                # can differ from the configured alias
                                # (e.g. a dated snapshot id).
                                answering_model = str(_msg["model"])

                        elif event_type == "content_block_start":
                            block = event.get("content_block", {})
                            if block.get("type") == "tool_use":
                                current_tool_id = block.get("id", "")
                                accumulated_tool_calls[current_tool_id] = {
                                    "id": current_tool_id,
                                    "name": block.get("name", ""),
                                    "arguments": "",
                                }

                        elif event_type == "content_block_delta":
                            delta = event.get("delta", {})
                            if delta.get("type") == "text_delta":
                                piece = sanitize_assistant_display_text(delta.get("text", ""))
                                if piece:
                                    streamed_text = True
                                    yield {"type": "text_delta", "content": piece}
                            elif delta.get("type") == "input_json_delta":
                                if current_tool_id in accumulated_tool_calls:
                                    accumulated_tool_calls[current_tool_id]["arguments"] += delta.get("partial_json", "")

                        elif event_type == "message_delta":
                            # This event is where Anthropic reports
                            # ``usage.output_tokens`` (cumulative, so
                            # the LAST one wins rather than summing).
                            # It used to be ``pass``, which is why
                            # streamed Anthropic turns billed nothing.
                            _u = event.get("usage")
                            if isinstance(_u, dict):
                                _out = _u.get("output_tokens")
                                if _out is not None:
                                    try:
                                        usage_output = int(_out)
                                        saw_usage = True
                                    except (TypeError, ValueError):
                                        pass
                                # Newer API versions echo input_tokens
                                # here too; take it only if
                                # ``message_start`` did not supply one.
                                _in = _u.get("input_tokens")
                                if _in is not None and not usage_input:
                                    try:
                                        usage_input = int(_in)
                                        saw_usage = True
                                    except (TypeError, ValueError):
                                        pass
                                # Same for the cache counters when a
                                # deployment reports them here instead.
                                _absorb_cache_usage(_u)

                        elif event_type == "message_stop":
                            await _record_anthropic_usage()
                            for tc in accumulated_tool_calls.values():
                                tc["args"], tc["args_error"] = parse_tool_arguments(
                                    tc.get("arguments", ""), tc.get("name", ""),
                                )
                                # A tool-call counts as forward
                                # progress for the failover guard:
                                # we've already committed to this
                                # response, restarting it on a
                                # different provider would re-fire
                                # the tool.
                                streamed_text = True
                                yield {"type": "tool_call_delta", "tool_call": tc}
                            _stop_event: dict = {"type": "done"}
                            if saw_usage:
                                _stop_event["usage"] = {
                                    "input_tokens": usage_input,
                                    "output_tokens": usage_output,
                                    "total_tokens": usage_input + usage_output,
                                }
                            yield _stop_event
                            return

            # Stream closed without a ``message_stop``. The turn still
            # delivered its text, so bill whatever usage arrived.
            # ``_record_anthropic_usage`` is idempotent, so the
            # ``message_stop`` path above cannot double-bill.
            await _record_anthropic_usage()
            _end_event: dict = {"type": "done"}
            if saw_usage:
                _end_event["usage"] = {
                    "input_tokens": usage_input,
                    "output_tokens": usage_output,
                    "total_tokens": usage_input + usage_output,
                }
            yield _end_event
            return
        except httpx.HTTPStatusError as e:
            primary_error = e
            detail = _describe_http_status_error(e)
            logger.error("Anthropic stream error: %s", detail)
        except Exception as e:
            primary_error = e
            detail = _describe_error(e)
            logger.error("Anthropic stream failed: %s", detail)

        # Pre-token failure → try cross-provider failover (parity with
        # the OpenAI-compat branch). When tokens already streamed we
        # can't safely restart on another provider, so surface the
        # error so the orchestrator can show a degraded-output banner.
        if not streamed_text and primary_error is not None:
            failover_events = await self._stream_via_nonstream_failover(
                messages,
                tools,
                temperature,
                max_tokens,
                primary_error=primary_error,
                force_tool=force_tool,
            )
            if failover_events:
                for event in failover_events:
                    yield event
                return
        yield {"type": "error", "content": detail}

    async def switch_provider(
        self,
        provider: str,
        model: str = "",
        api_key: str = "",
        base_url: str = "",
    ):
        """Hot-swap the LLM provider at runtime.

        ``base_url`` is an optional override; when empty, the adapter
        looks up the default base URL from :data:`_PROVIDER_REGISTRY`
        for cloud providers or the local helper for
        ``ollama`` / ``lmstudio``. The override path is what lets the
        v2 Settings page's Save-&-switch endpoint point a user at a
        self-hosted inference URL (lmstudio, ollama, a custom
        OpenAI-compatible gateway) without shipping that literal in
        the adapter's defaults. Unknown provider ids without an
        explicit ``base_url`` no longer silently alias to OpenAI —
         A3 removed that fallback because it was a recurring
        footgun (see the ``unknown`` branch below).

        NOTE: the ``base_url`` kwarg itself was added after  in
        response to the shipped v2026.5.0 crash
        (``api/routes/config.py::update_config`` was already passing
        ``base_url=`` but ``switch_provider`` did not accept it ->
        TypeError -> every Save-&-switch 500'd). The regression test
        lives in ``tests/test_switch_provider_base_url.py``.
        """
        client = getattr(self, "client", None)
        if client is not None:
            await client.aclose()

        # Reset the permanent-auth short-circuit. Whatever was wrong
        # before, the user just supplied fresh credentials -- start
        # trying again immediately. Same on switching providers.
        self._auth_permanent_until = {}
        self._auth_permanent_logged = set()

        self.provider = provider
        if model:
            self.model = model

        # Honor the explicit override before the lookup. An empty
        # string is treated as "no override" so legacy callers that
        # always omitted the kwarg keep the auto-resolved default.
        _base_url_override = base_url or ""

        if provider == "codex":
            self._codex_adapter = None
            adapter = self._get_codex_adapter()
            self.base_url = "app-server://stdio"
            self.api_key = "codex-managed-auth"
            try:
                models = await adapter.refresh_models()
                if not model:
                    self.model = models[0]
                self.available = True
            except Exception as exc:
                self.available = False
                logger.warning("Codex provider probe failed: %s", exc)
            self.client = self._build_client()
            logger.info(
                "Switched LLM to %s/%s (available=%s)",
                provider,
                self.model,
                self.available,
            )
            return
        if provider == "lmstudio":
            self.base_url = _base_url_override or "http://localhost:1234/v1"
            self.api_key = "lm-studio"
            if not model:
                # Blocking probe (sync httpx.get, timeout=2). Off the loop:
                # this is an async method and the probe stalls every other
                # request for its whole timeout when the server is down.
                detected = await asyncio.to_thread(self._detect_lmstudio)
                self.model = detected or _default_model_for("lmstudio")
        elif provider == "ollama":
            self.base_url = _base_url_override or ollama_openai_base_url()
            self.api_key = "ollama"
            if not model:
                # Blocking probe (urllib.request.urlopen, timeout=3), same
                # reason as the LM Studio branch above.
                detected = await asyncio.to_thread(self._detect_ollama)
                self.model = detected or _default_model_for("ollama")
        elif provider == "local":
            self._init_local_engine()
            if self._local_engine:
                self.available = True
                logger.info(f"Switched to local inference: {self._local_engine.model_id}")
                return
            else:
                logger.warning("Local engine unavailable")
                self.available = False
                return
        elif provider in _PROVIDER_REGISTRY:
            # Runtime-registered provider. Resolve the default base URL
            # + credential env var from the single registry source so
            # openrouter / deepseek / kimi / qwen stay reachable
            # (before  A3 these were missing from the local
            # PROVIDER_BASES dict and silently fell through to OpenAI).
            base, env_key = _PROVIDER_REGISTRY[provider]
            self.base_url = _base_url_override or base
            # Cross-cut #1 (v2026.5.42): when the caller did not
            # supply an explicit ``api_key``, consult the labeled vault
            # overlay before falling back to env. ``get_active_key``
            # already covers env, so the OR chain here is the gemini
            # GOOGLE_API_KEY tail.
            if provider == "gemini":
                self.api_key = api_key or _resolve_api_key("gemini") or _gemini_api_key() or ""
            elif env_key:
                self.api_key = api_key or _resolve_api_key(provider) or os.getenv(env_key, "")
            else:
                self.api_key = api_key
            if not model:
                self.model = _default_model_for(provider)
        else:
            # Unknown / unsupported provider id. Previously we
            # silently defaulted to ``https://api.openai.com/v1`` and
            # reused whatever ``api_key`` the caller passed — which
            # meant a catalog-only descriptor (``bedrock``,
            # ``together``, ``fireworks``) or a typo would send a
            # valid OpenAI-shaped request against OpenAI's endpoint
            # while the UI believed it was on the selected provider.
            # The new contract:
            #   * if the caller supplied ``base_url``, trust it as an
            #     operator-controlled custom OpenAI-compatible gateway
            #     (keeps Save-&-switch working for on-prem setups);
            #   * otherwise refuse the swap — mark the adapter
            #     unavailable and keep the unknown id visible so the
            #     REST / UI layer can report it truthfully.
            logger.warning(
                "switch_provider(%r): provider is not in the runtime "
                "registry. Supported: %s. %s",
                provider,
                sorted(SUPPORTED_RUNTIME_PROVIDERS),
                "Honouring explicit base_url override."
                if _base_url_override
                else "No base_url override supplied — leaving adapter "
                     "unavailable.",
            )
            if _base_url_override:
                self.base_url = _base_url_override
                self.api_key = api_key
                if not model:
                    self.model = _default_model_for(provider)
            else:
                self.base_url = ""
                self.api_key = ""
                if not model:
                    self.model = _default_model_for(provider)
                self.client = self._build_client()
                self.available = False
                logger.info(
                    "Switched LLM to %s/%s (available=False, reason=unsupported_provider)",
                    provider, self.model,
                )
                return

        self.client = self._build_client()
        # Availability requires BOTH a working base_url and a usable
        # credential. Previously ``bool(self.api_key)`` alone said
        # True even when base_url was empty — masking the failure
        # until the next chat call 404'd.
        self.available = bool(self.api_key) and bool(self.base_url)
        logger.info(f"Switched LLM to {provider}/{self.model} (available={self.available})")

        # v2026.5.23 — Real round-trip availability probe.
        #
        # Pre-fix, ``available=True`` was set purely on key + base_url
        # presence. That let the picker land the operator on models
        # that DO appear in /v1/models but DON'T actually answer at
        # /v1/chat/completions (e.g. gpt-5.5-pro returns 404 "not a
        # chat model" because it routes through /v1/responses). The
        # log said "available=True" but every subsequent chat 404'd.
        #
        # The probe sends a 1-token throwaway request through the
        # endpoint the model is classified to use (chat-completions
        # OR responses) and flips ``available=False`` on hard failure
        # so the picker / orchestrator / health surface tell the
        # truth from the first turn onwards.
        #
        # We deliberately ONLY probe known runtime providers — for
        # custom-base_url gateways (operator-supplied OpenAI-compat
        # endpoints behind their own DNS), the network may be slow
        # / firewalled / unresolvable at boot time and the probe's
        # DNS failure shouldn't disable the provider. Operators
        # debugging connectivity get the existing cooldown-on-error
        # ladder instead.
        _probe_eligible = (
            self.available
            and provider in ("openai", "anthropic", "openrouter", "deepseek", "gemini", "groq", "kimi", "qwen")
        )
        if _probe_eligible:
            probe_ok, probe_reason = await self._probe_chat_availability()
            if not probe_ok:
                self.available = False
                logger.warning(
                    "Probe failed for %s/%s: %s — `available` set to False. "
                    "The model is unreachable from this endpoint; pick a "
                    "different model or check the API key.",
                    provider, self.model, probe_reason,
                )

    async def reconfigure(
        self,
        *,
        provider: str,
        model: str = "",
        api_key: str = "",
        base_url: str = "",
    ) -> dict:
        """Hot-swap provider / model / key / base_url in one call.

        Same wire as ``switch_provider`` but:
          * accepts ``base_url`` so local providers (LM Studio, custom
            Ollama port) can land end-to-end from the Settings form;
          * returns a structured result the REST layer can surface to
            the UI (``{provider, model, available, reason}``);
          * emits a supervisor event so the swap lands in the audit
            log right alongside user commands.
        """
        if base_url:
            os.environ["FERAL_LLM_BASE_URL"] = base_url
        previous_provider = self.provider
        try:
            # ``base_url`` is now threaded through to ``switch_provider``
            # so local providers (LM Studio, custom Ollama port,
            # OpenAI-compat gateway) actually land.  the kwarg
            # was set on ``os.environ`` but never passed to
            # ``switch_provider``, so the override only took effect
            # at the next ``__init__`` boot — see findings/13-llm-core.md
            # fix #3.
            await self.switch_provider(
                provider=provider,
                model=model,
                api_key=api_key,
                base_url=base_url,
            )
        except Exception as exc:
            logger.warning("reconfigure(%s) failed: %s", provider, exc)
            return {
                "ok": False,
                "provider": provider,
                "model": model,
                "available": False,
                "reason": str(exc),
            }

        reason = "ok" if self.available else "no_api_key"
        try:
            from api.state import state as _state
            sup = getattr(_state, "supervisor", None)
            if sup is not None:
                sup.record(
                    source="config",
                    kind="llm_reconfigure",
                    actor="user",
                    payload={
                        "from": previous_provider,
                        "to": self.provider,
                        "model": self.model,
                        "has_key": bool(self.api_key),
                    },
                    decision="allowed" if self.available else "queued",
                    detail={"reason": reason, "base_url": self.base_url},
                )
        except Exception as exc:
            logger.debug("supervisor.record(llm_reconfigure) failed: %s", exc)

        return {
            "ok": True,
            "provider": self.provider,
            "model": self.model,
            "available": bool(self.available),
            "base_url": self.base_url,
            "reason": reason,
        }

    # ── Per-call-site tier picker ( Lane 09) ────────────────────
    #
    # Wave 3 Lane 08 (orchestrator) builds against this contract.
    # ``route_call(call_site, prompt)`` returns a :class:`ProviderRef`
    # naming WHICH provider + model + tier the caller should use for
    # THIS turn — the orchestrator then calls
    # ``chat_with_failover(messages, model=ref.model, ...)``.
    #
    # Tier mapping (cheap / balanced / premium):
    #
    #   * routing  — the "should I call any tools?" pre-flight. Defaults
    #                to *cheap* because routing burns tokens on every
    #                turn (see audit-r13 04-llm-router-and-picker.md).
    #   * chat     — the user-facing reasoning turn. Defaults to
    #                *balanced* — premium frontier models when the
    #                operator opts in.
    #   * vision   — Image / screen-loop perception. Defaults to *cheap*
    #                because vision LLM calls fire on every frame
    #                (see audit-r13 05-token-billing-leakage.md S1).
    #   * embedding — Memory / search vectorisation. Always *cheap*
    #                because the embedding endpoint is orthogonal to
    #                quality tiers.
    #
    # The mapping is configurable via
    # ``settings.llm.tier_map[call_site][tier]`` so an operator can
    # pin chat→cheap (Sonnet/mini) or vision→balanced (full vision
    # frontier) without code edits. Defaults below are conservative.

    CALL_SITES: tuple[str, ...] = ("routing", "chat", "vision", "embedding")
    TIERS: tuple[str, ...] = ("cheap", "balanced", "premium")

    # Default tier per call_site. Operators override via
    # ``settings.llm.call_site_tiers``. Vision LLM stays cheap because
    # the screen loop hits it ~ once per second; routing stays cheap
    # because every chat turn pays for it.
    _DEFAULT_TIER_PER_CALL_SITE: dict[str, str] = {
        "routing": "cheap",
        "chat": "balanced",
        "vision": "cheap",
        "embedding": "cheap",
    }

    # Conservative provider+model slate per (call_site, tier). The
    # operator can override every entry via
    # ``settings.llm.tier_map[call_site][tier] = {"provider": ...,
    # "model": ...}``. Defaults bias toward providers that have a
    # runtime adapter in this build AND a curated catalog entry — so
    # ``route_call`` never returns a reference the runtime can't
    # actually call.
    _DEFAULT_TIER_MAP: dict[str, dict[str, dict[str, str]]] = {
        "routing": {
            "cheap": {"provider": "openai", "model": "gpt-5-mini"},
            "balanced": {"provider": "openai", "model": "gpt-5-mini"},
            "premium": {"provider": "anthropic", "model": "claude-haiku-4-5"},
        },
        "chat": {
            "cheap": {"provider": "openai", "model": "gpt-5-mini"},
            "balanced": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
            "premium": {"provider": "anthropic", "model": "claude-opus-4-7"},
        },
        "vision": {
            "cheap": {"provider": "ollama", "model": "llava"},
            "balanced": {"provider": "openai", "model": "gpt-5"},
            "premium": {"provider": "anthropic", "model": "claude-opus-4-7"},
        },
        "embedding": {
            "cheap": {"provider": "openai", "model": "text-embedding-3-small"},
            "balanced": {"provider": "openai", "model": "text-embedding-3-large"},
            "premium": {"provider": "openai", "model": "text-embedding-3-large"},
        },
    }

    def _resolve_call_site_tier(self, call_site: str) -> str:
        """Pick the tier for *call_site* from settings, with default."""
        cfg = self._config if isinstance(self._config, dict) else {}
        tiers = cfg.get("call_site_tiers") if isinstance(cfg.get("call_site_tiers"), dict) else {}
        tier = tiers.get(call_site) if isinstance(tiers, dict) else None
        if isinstance(tier, str) and tier in self.TIERS:
            return tier
        return self._DEFAULT_TIER_PER_CALL_SITE.get(call_site, "balanced")

    # Cheaper-but-same-provider sibling for the auto (non-override) cheap
    # tier. Keyed by runtime provider id; the value is a fast/cheap model
    # the same vendor exposes. This is what keeps adaptive routing SAFE to
    # enable by default — the cheap tier never leaves the provider the
    # operator already has a key for; it only swaps to a cheaper model.
    # Operators wanting cross-provider tiers set ``settings.llm.tier_map``.
    # Refreshed 2026-07-30 against the catalog. Entries whose cheap SKU
    # could not be verified were REMOVED rather than left pointing at a
    # retired id: ``_cheap_sibling_model`` returning None simply disables
    # cheap-tier routing for that provider, whereas a stale literal
    # routes real traffic at a model that 404s. ``xai`` lost its
    # ``grok-3-mini`` entry for exactly that reason — grok-3-mini is
    # retired and the current line (grok-4.5 / grok-4.3 / grok-build-0.1)
    # has no confirmed cheap tier.
    _CHEAP_SIBLING: dict[str, str] = {
        "openai": "gpt-5-nano",
        "anthropic": "claude-haiku-4-5",
        "google": "gemini-3.5-flash-lite",
        "gemini": "gemini-3.5-flash-lite",
        "deepseek": "deepseek-v4-flash",
        "openrouter": "openai/gpt-5-nano",
        "groq": "llama-3.1-8b-instant",
        "together": "meta-llama/Llama-3.1-8B-Instruct-Turbo",
        "fireworks": "accounts/fireworks/models/llama-v3p1-8b-instruct",
        "mistral": "mistral-small-2603",
        "kimi": "kimi-k2.5",
    }

    def _cheap_sibling_model(self, provider: str, current_model: str) -> Optional[str]:
        """Return a cheaper same-provider model, or ``None`` when there
        isn't a known one (or the operator is already on it)."""
        sibling = self._CHEAP_SIBLING.get(provider)
        if not sibling:
            return None
        if current_model and current_model.strip() == sibling:
            return None
        return sibling

    def _local_first_target(self, call_site: str) -> Optional[dict[str, str]]:
        """When ``settings.llm.local_first`` is on, resolve the cheap tier
        to a local model (privacy + zero-cost), or ``None`` to fall through.

        Honoured only for the cheap tier on ``chat`` / ``routing`` /
        ``vision``. Uses an explicit ``settings.llm.local_model`` target
        when configured; otherwise, if the primary itself is already a
        local engine, keeps it. Never invents an unsupported provider.
        """
        cfg = self._config if isinstance(self._config, dict) else {}
        if not cfg.get("local_first"):
            return None
        if call_site not in ("chat", "routing", "vision"):
            return None
        lm = cfg.get("local_model")
        if isinstance(lm, dict):
            provider = str(lm.get("provider") or "").strip()
            model = str(lm.get("model") or "").strip()
            if provider and is_supported_runtime_provider(provider):
                return {"provider": provider, "model": model or _default_model_for(provider)}
        if self.provider in ("ollama", "lmstudio", "local", "hybrid"):
            return {"provider": self.provider, "model": self.model}
        return None

    def _resolve_tier_target(self, call_site: str, tier: str) -> dict[str, str]:
        """Resolve ``(call_site, tier)`` to a ``{provider, model}`` dict.

        Order: operator ``tier_map`` override → same-provider tiering for
        chat/routing (so the auto path never switches providers) →
        bundled purpose-built target for vision/embedding → primary.
        """
        cfg = self._config if isinstance(self._config, dict) else {}
        overrides = cfg.get("tier_map") if isinstance(cfg.get("tier_map"), dict) else {}
        site_overrides = overrides.get(call_site) if isinstance(overrides, dict) else {}
        if isinstance(site_overrides, dict):
            tier_override = site_overrides.get(tier)
            if isinstance(tier_override, dict):
                provider = str(tier_override.get("provider") or "").strip()
                model = str(tier_override.get("model") or "").strip()
                if provider:
                    return {
                        "provider": provider,
                        "model": model or _default_model_for(provider),
                    }
        # Auto path: keep tiering WITHIN the operator's configured provider
        # for the interactive/routing call sites. Only the model changes
        # (cheap → cheaper sibling); balanced/premium stay on the model the
        # operator chose, which is their quality ceiling unless they opt
        # into a cross-provider ``tier_map``.
        if call_site in ("chat", "routing"):
            if tier == "cheap":
                sibling = self._cheap_sibling_model(self.provider, self.model)
                return {"provider": self.provider, "model": sibling or self.model}
            return {"provider": self.provider, "model": self.model}
        bundled = self._DEFAULT_TIER_MAP.get(call_site, {}).get(tier)
        if bundled:
            return dict(bundled)
        # Final fallback: whatever's currently primary.
        return {"provider": self.provider, "model": self.model}

    def route_call(
        self,
        call_site: str,
        prompt: Any = None,
        *,
        tier: Optional[str] = None,
        adaptive: bool = False,
    ) -> dict[str, Any]:
        """Pick a (provider, model) for *call_site* via the tier picker.

        **Public API contract** (Wave 3 Lane 08 builds against this):

        Args:
            call_site: One of ``"routing"`` / ``"chat"`` / ``"vision"``
                / ``"embedding"``. Other values raise
                :class:`ValueError` so a typo can't silently swing the
                whole call onto the wrong tier.
            prompt: Optional opaque payload. Currently unused at the
                router level (the orchestrator already estimates
                length via ``_estimate_tokens_for_budget``); reserved
                for future per-prompt heuristics
                (e.g. "this prompt has an image attached, force a
                vision-capable target"). Passing ``None`` is fine.
            tier: Optional override of the operator's settings tier
                ("cheap" / "balanced" / "premium"). When omitted the
                tier comes from ``settings.llm.call_site_tiers`` with
                a conservative default per :attr:`_DEFAULT_TIER_PER_CALL_SITE`.

        Returns:
            ``ProviderRef`` shape: ::

                {
                    "call_site":  str,        # echoed
                    "tier":       str,        # one of TIERS
                    "provider":   str,        # runtime provider id
                    "model":      str,        # model id (catalog-resolved)
                    "supported":  bool,       # True iff runtime has
                                              # an adapter for `provider`
                    "fallback_providers": list[str],  # ordered chain
                    "source":     str,        # "settings" | "default"
                }

            The orchestrator then dispatches via:

                ref = llm.route_call("chat", prompt=user_prompt)
                result = await llm.chat_with_failover(
                    messages,
                    model=ref["model"],
                    # provider override applied by the orchestrator if
                    # `ref["provider"] != llm.provider`
                )

            Lane 08 owns the orchestrator wiring; this contract MUST
            stay stable across  → .

        Raises:
            ValueError: when ``call_site`` is not one of
                :attr:`CALL_SITES` or ``tier`` is not one of
                :attr:`TIERS`.
        """
        if call_site not in self.CALL_SITES:
            raise ValueError(
                f"unknown call_site {call_site!r}; "
                f"must be one of {sorted(self.CALL_SITES)}"
            )
        if tier is None:
            resolved_tier = self._resolve_call_site_tier(call_site)
            source = "settings" if (
                isinstance(self._config, dict)
                and isinstance(self._config.get("call_site_tiers"), dict)
                and call_site in self._config["call_site_tiers"]
            ) else "default"
        else:
            if tier not in self.TIERS:
                raise ValueError(
                    f"unknown tier {tier!r}; must be one of {sorted(self.TIERS)}"
                )
            resolved_tier = tier
            source = "explicit"
        # Adaptive callers (the interactive chat path) let budget pressure
        # downshift the tier and let local-first policy claim the cheap
        # tier. Non-adaptive callers (API preview, tests, explicit picks)
        # get a pure (call_site, tier) → target lookup with no surprises.
        if adaptive:
            from agents import llm_router

            snap = self._budget_snapshot()
            downshifted = llm_router.apply_cost_downshift(
                resolved_tier,
                headroom_ratio=float(snap.get("headroom_ratio", 1.0)),
                tight_ratio=float(snap.get("tight_ratio", 0.25)),
            )
            if downshifted != resolved_tier:
                resolved_tier = downshifted
                source = "budget_downshift"
        target = self._resolve_tier_target(call_site, resolved_tier)
        if adaptive and resolved_tier == "cheap":
            local_target = self._local_first_target(call_site)
            if local_target:
                target = local_target
                source = "local_first"
        provider = target["provider"]
        model = target["model"]
        supported = is_supported_runtime_provider(provider) or provider in ("local", "hybrid")
        fallbacks: list[str] = []
        if isinstance(self._config, dict):
            fb = self._config.get("fallback_providers") or []
            if isinstance(fb, list):
                fallbacks = [str(p) for p in fb if isinstance(p, str)]
        ref = {
            "call_site": call_site,
            "tier": resolved_tier,
            "provider": provider,
            "model": model,
            "supported": supported,
            "fallback_providers": fallbacks,
            "source": source,
        }
        logger.debug(
            "route_call(%s, tier=%s) -> %s/%s (supported=%s)",
            call_site, resolved_tier, provider, model, supported,
        )
        return ref

    async def apply_preset(self, preset_id: str) -> dict:
        preset = LLM_PRESETS.get(preset_id)
        if not preset:
            return {"ok": False, "error": f"Unknown preset: {preset_id}"}
        requested_model = preset.get("model", "") or ""
        # Ollama presets that hardcode a model name (``ollama_vision`` ->
        # ``llava``) are a guaranteed 404 on the first chat turn when
        # that model isn't pulled locally. Probe ``/api/tags`` and fall
        # through to switch_provider's auto-detect path when the request
        # doesn't match an installed model — the caller still gets an
        # ``ok=True`` response but with a ``warning`` describing the
        # substitution so the UI can prompt the operator to pull the
        # preferred model. When Ollama itself is unreachable we leave
        # the request unchanged: the running brain's own probe ladder
        # will surface that failure.
        fallback_warning = ""
        if preset.get("provider") == "ollama" and requested_model:
            pulled = await self._ollama_pulled_models()
            if pulled is not None:
                base = requested_model.split(":", 1)[0]
                if requested_model not in pulled and base not in pulled:
                    fallback_warning = (
                        f"Ollama model {requested_model!r} is not pulled; "
                        "auto-detecting an installed model instead. "
                        f"Run `ollama pull {requested_model}` to use this "
                        "preset directly."
                    )
                    requested_model = ""
        await self.switch_provider(
            provider=preset["provider"],
            model=requested_model,
            api_key="",
        )
        payload: dict = {
            "ok": True,
            "preset": preset_id,
            "provider": self.provider,
            "model": self.model,
            "vision_supported": bool(preset.get("vision_supported", False)),
        }
        if fallback_warning:
            payload["warning"] = fallback_warning
        return payload

    @staticmethod
    async def _ollama_pulled_models() -> Optional[set[str]]:
        """Return the set of model names + base ids pulled on the local
        Ollama server, or ``None`` when the server is unreachable.

        Used by ``apply_preset`` to avoid handing switch_provider a
        guaranteed-404 model literal. The return shape includes both
        the full ``name:tag`` form and the bare ``name`` so callers can
        match either style of configured id.
        """
        try:
            base = ollama_base_url().rstrip("/")
            async with httpx.AsyncClient(timeout=3.0) as c:
                r = await c.get(f"{base}/api/tags")
                r.raise_for_status()
            payload = r.json() or {}
        except Exception:
            return None
        models = payload.get("models") or []
        out: set[str] = set()
        for entry in models:
            name = (entry.get("name") or "").strip()
            if not name:
                continue
            out.add(name)
            out.add(name.split(":", 1)[0])
        return out

    # ── Failover ───────────────────────────────────────────

    def set_config(self, config: dict):
        """Accept external config (e.g. from ConfigLoader) for fallback routing."""
        self._config = config

    def set_cost_budget(self, budget: Any) -> None:
        """Wire a Wave 1 ``CostBudget`` instance into this LLMProvider.

        After this call every ``chat`` / ``chat_stream`` /
        ``chat_with_failover`` invocation:

        * pre-flights ``check_and_reserve`` — if the projected cost
          would breach a cap, the call short-circuits with a
          structured ``{error, budget_exceeded: {...}}`` response
          shape (no upstream HTTP traffic, no token spend).
        * records actual token usage via
          ``record_usage`` on success. Reasoning tokens
          (``usage.completion_tokens_details.reasoning_tokens``) are
          billed at the output rate per OpenAI's documented policy.

        Pass ``None`` to disable the budget gate (e.g. tests). The
        gate is also a no-op when the budget instance has
        ``enabled=False``.
        """
        self._cost_budget = budget

    @staticmethod
    def _extract_usage(result: Any) -> tuple[int, int, int]:
        """Pull (prompt_tokens, completion_tokens, reasoning_tokens) from
        whatever response shape the provider returned.

        Tolerant of the three shapes we see in the wild:

        * OpenAI: ``{usage: {prompt_tokens, completion_tokens,
          completion_tokens_details: {reasoning_tokens}}}``
        * Anthropic (post-normalisation): ``{usage: {input_tokens,
          output_tokens}}``
        * Bare ``{usage: {input_tokens, output_tokens}}`` (already
          normalised at adapter level)

        Returns ``(0, 0, 0)`` when the response carries no usage data;
        the budget then records the call without billing tokens. The
        stream routes avoid feeding that case in at all: each calls
        ``_budget_record`` only once it actually has a usage block
        (chat-completions asks for ``stream_options.include_usage``,
        Anthropic reads ``message_start`` + ``message_delta``), so a
        provider that reports nothing stays unrecorded rather than
        being billed as a zero-token turn.
        """
        if not isinstance(result, dict):
            return (0, 0, 0)
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        prompt = (
            usage.get("prompt_tokens")
            if usage.get("prompt_tokens") is not None
            else usage.get("input_tokens")
        ) or 0
        completion = (
            usage.get("completion_tokens")
            if usage.get("completion_tokens") is not None
            else usage.get("output_tokens")
        ) or 0
        reasoning = 0
        details = usage.get("completion_tokens_details")
        if isinstance(details, dict):
            reasoning = details.get("reasoning_tokens") or 0
        # Anthropic reports thinking tokens at the top level when
        # extended-thinking is enabled — pick those up too so the
        # billing surface is symmetric across providers.
        if not reasoning:
            reasoning = usage.get("reasoning_tokens") or usage.get("thinking_tokens") or 0
        try:
            return int(prompt), int(completion), int(reasoning)
        except (TypeError, ValueError):
            return (0, 0, 0)

    @staticmethod
    def _extract_cache_usage(result: Any) -> tuple[int, int]:
        """Pull ``(cache_write_tokens, cache_read_tokens)`` from a response.

        These are Anthropic's ``usage.cache_creation_input_tokens`` and
        ``usage.cache_read_input_tokens``. They are reported ALONGSIDE
        ``input_tokens``, never inside it: a turn that reads 20k tokens
        from the cache and sends 500 fresh ones reports
        ``input_tokens=500``. So ``_extract_usage`` alone under-counts a
        cache-heavy turn, which is why this exists as a separate read
        rather than being folded into the prompt count there.

        Kept OUT of ``_extract_usage`` on purpose: that helper's
        3-tuple shape is pinned by tests and read by several call
        sites, and cache tokens must not be billed at the plain input
        rate anyway (see ``cost/pricing.py``).

        Returns ``(0, 0)`` for every provider that does not report these
        fields, which is every provider except Anthropic today.
        """
        if not isinstance(result, dict):
            return (0, 0)
        usage = result.get("usage")
        if not isinstance(usage, dict):
            return (0, 0)
        try:
            write = int(usage.get("cache_creation_input_tokens") or 0)
            read = int(usage.get("cache_read_input_tokens") or 0)
        except (TypeError, ValueError):
            return (0, 0)
        return (max(0, write), max(0, read))

    @staticmethod
    def _extract_inclusive_cached_tokens(result: Any) -> int:
        """OpenAI's prompt-cache read count, which is INSIDE the input total.

        OpenAI reports it as ``usage.prompt_tokens_details.cached_tokens``
        on /chat/completions and ``usage.input_tokens_details.cached_tokens``
        on /v1/responses, and in both cases the number is a subset of
        ``prompt_tokens`` / ``input_tokens`` rather than an addition to
        it. That is the opposite of Anthropic's contract (see
        ``_extract_cache_usage``), which is why this is a separate read
        and why the caller SUBTRACTS before re-adding at the cache rate.

        Both spellings are checked because the Responses adapter
        (``_responses_payload_to_chat_dict``) passes the provider's usage
        block through untouched, so whichever shape arrived is the shape
        stored. Neither was read at all before this, so every cached
        token on the audited install was billed at the full input rate:
        ``model_catalog.json`` prices ``gpt-5.6-sol`` cache reads at
        0.0005 against an input rate of 0.005, a tenth, so a cache-heavy
        turn charged the hourly cap roughly ten times what it cost.
        """
        if not isinstance(result, dict):
            return 0
        usage = result.get("usage")
        if not isinstance(usage, dict):
            return 0
        for key in ("input_tokens_details", "prompt_tokens_details"):
            details = usage.get(key)
            if not isinstance(details, dict):
                continue
            try:
                cached = int(details.get("cached_tokens") or 0)
            except (TypeError, ValueError):
                continue
            if cached > 0:
                return cached
        return 0

    def _budget_exceeded_response(self, exc: Any) -> dict:
        """Build the structured ``BudgetExceeded`` response shape.

        Orchestrator + WebUI consume this directly — see
        findings/13-llm-core.md fix #5. The shape carries enough
        information for the WebUI banner ("Chat budget exceeded —
        $0.10/hour cap. Resets at 14:00 UTC.") without any further
        introspection.
        """
        return {
            "error": str(exc),
            "choices": [],
            "budget_exceeded": {
                "call_site": getattr(exc, "call_site", "chat"),
                "cap_dollars": float(getattr(exc, "cap_dollars", 0.0)),
                "current_dollars": float(getattr(exc, "current_dollars", 0.0)),
                "window": getattr(exc, "window", "hour"),
                "reset_at": float(getattr(exc, "reset_at", 0.0)),
            },
        }

    async def _budget_check(
        self,
        call_site: str,
        model: str,
        max_tokens: int,
    ) -> Optional[dict]:
        """Return a structured response if the budget would be exceeded,
        else ``None``. Used as a pre-flight in chat / chat_stream /
        chat_with_failover.
        """
        # ``getattr`` rather than direct attribute access — tests
        # routinely instantiate ``LLMProvider`` via ``__new__`` (skipping
        # ``__init__``) to mock candidate lists, and the attribute
        # may not be present.
        budget = getattr(self, "_cost_budget", None)
        if budget is None:
            return None
        try:
            await budget.ensure_ready()
        except Exception as exc:
            logger.debug("CostBudget.ensure_ready failed (non-fatal): %s", exc)
            return None
        try:
            ok = budget.check_and_reserve(call_site, model, int(max_tokens or 0))
        except Exception as exc:
            logger.debug("CostBudget.check_and_reserve raised (non-fatal): %s", exc)
            return None
        if ok:
            return None
        # Build a synthetic BudgetExceeded so the response shape is
        # uniform whether the cap is detected pre- or post-call.
        try:
            from cost.budget import BudgetExceeded, window_reset_at
            cap = (
                budget._cap_for(call_site, "hour")
                or budget._cap_for("__global__", "hour")
                or 0.0
            )
            current = budget.current_spend(call_site, "hour")
            exc = BudgetExceeded(
                call_site=call_site,
                cap_dollars=float(cap),
                current_dollars=float(current),
                window="hour",
                reset_at=window_reset_at("hour"),
            )
            return self._budget_exceeded_response(exc)
        except Exception as exc:
            logger.debug("Failed to build BudgetExceeded response: %s", exc)
            return {
                "error": "budget exceeded",
                "choices": [],
                "budget_exceeded": {"call_site": call_site, "window": "hour"},
            }

    async def _budget_record(
        self,
        call_site: str,
        model: str,
        result: Any,
    ) -> None:
        """Best-effort post-call usage recording. Never raises.

        Prompt-cache tokens
        -------------------
        Anthropic reports ``cache_creation_input_tokens`` (written) and
        ``cache_read_input_tokens`` (served) separately from
        ``input_tokens``, and bills them at their own rates: a write
        costs 1.25x the base input rate, a read 0.1x (see
        ``cost/pricing.py`` for the source URL). They used to be dropped
        entirely, so a cache-heavy turn under-counted spend and a
        configured cap tripped later than it should.

        They are converted here into the number of base-input-rate
        tokens that costs the same money
        (``cost.pricing.cache_equivalent_prompt_tokens``) and added to
        the prompt count, because ``CostBudget.record_usage`` bills off
        a plain prompt/completion/reasoning triple and has no cache
        columns. The dollars land exactly right; the caveat is that
        ``cost_events.prompt_tokens`` is then a BILLING-EQUIVALENT
        count, not the raw wire number. Adding the raw cache tokens
        instead would bill a cache read at 10x its real price, which is
        the error this replaced, in the other direction.

        The conversion is a pure pricing lookup and returns 0 for any
        model with no published cache rate, so a catalog miss degrades
        to the old behaviour rather than to a wrong number, and the
        whole body stays inside the existing try so it can never take
        down a chat turn.
        """
        budget = getattr(self, "_cost_budget", None)
        if budget is None:
            return
        try:
            prompt, completion, reasoning = self._extract_usage(result)
            cache_write, cache_read = self._extract_cache_usage(result)
            # OpenAI's cached tokens are already counted inside the input
            # total, so they have to come OUT of the prompt count before
            # they go back in at the cache rate. Anthropic reports its
            # cache tokens alongside the input total, so nothing is
            # subtracted for it and this is a no-op there.
            inclusive_cached = self._extract_inclusive_cached_tokens(result)
            if inclusive_cached:
                inclusive_cached = min(inclusive_cached, prompt)
                prompt -= inclusive_cached
                cache_read += inclusive_cached
            if cache_write or cache_read:
                # Own try: a pricing failure must cost us the cache
                # SURCHARGE only, not the whole turn's billing. Falling
                # through to record_usage with the unmodified prompt
                # count reproduces the old behaviour, which is the right
                # degradation target.
                try:
                    from cost.pricing import cache_equivalent_prompt_tokens
                    prompt += cache_equivalent_prompt_tokens(
                        model, cache_write, cache_read,
                    )
                except Exception as exc:
                    logger.debug(
                        "cache-token pricing failed for %s (non-fatal): %s",
                        model, exc,
                    )
            await budget.record_usage(
                call_site=call_site,
                model=model,
                prompt_tokens=prompt,
                completion_tokens=completion,
                reasoning_tokens=reasoning,
            )
        except Exception as exc:
            # Non-fatal: a billing failure must never take down a chat
            # turn. The cap-exceeded path is exercised through
            # ``_budget_check`` instead.
            logger.debug(
                "CostBudget.record_usage(%s, %s) failed (non-fatal): %s",
                call_site, model, exc,
            )

    @staticmethod
    def _float_or_default(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return float(default)

    def _budget_snapshot(self) -> dict[str, Any]:
        raw_cfg = getattr(self, "_config", {})
        cfg = raw_cfg if isinstance(raw_cfg, dict) else {}
        raw_budget = os.environ.get(
            "FERAL_LLM_DAILY_BUDGET_USD",
            cfg.get("daily_budget_usd", 0.0),
        )
        raw_spend = os.environ.get(
            "FERAL_LLM_DAILY_SPEND_USD",
            cfg.get("daily_spend_usd", 0.0),
        )
        budget = max(0.0, self._float_or_default(raw_budget, 0.0))
        spend = max(0.0, self._float_or_default(raw_spend, 0.0))
        tight_ratio = self._float_or_default(
            os.environ.get(
                "FERAL_LLM_BUDGET_TIGHT_RATIO",
                cfg.get("budget_tight_ratio", 0.25),
            ),
            0.25,
        )
        tight_ratio = min(1.0, max(0.0, tight_ratio))
        remaining = budget - spend
        headroom_ratio = (remaining / budget) if budget > 0 else 1.0
        return {
            "enabled": bool(budget > 0.0),
            "daily_budget_usd": budget,
            "daily_spend_usd": spend,
            "remaining_usd": remaining,
            "headroom_ratio": headroom_ratio,
            "tight_ratio": tight_ratio,
        }

    @staticmethod
    def _message_char_count(messages: list[dict]) -> int:
        total = 0
        for msg in messages or []:
            content = msg.get("content") if isinstance(msg, dict) else ""
            if isinstance(content, str):
                total += len(content)
                continue
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        total += len(str(part.get("text", "") or ""))
                    else:
                        total += len(str(part))
        return total

    def _estimate_tokens_for_budget(
        self,
        messages: list[dict],
        kwargs: dict[str, Any],
    ) -> tuple[int, int]:
        # Coarse estimate used for candidate ordering only, but it prices the
        # call against a USD budget, so under-counting overshoots the budget.
        # `prompt_chars / 4` did that by up to 11x: measured against
        # cl100k/o200k, emoji ran 0.09x and Chinese 0.21x of the real count.
        # See agents/token_estimate.py.
        prompt_tokens = max(1, estimate_message_tokens(messages))
        max_tokens = int(kwargs.get("max_tokens", 1024) or 1024)
        completion_tokens = max(1, min(max_tokens, 4096))
        return prompt_tokens, completion_tokens

    def _pricing_for_model(self, provider_name: str, model: str) -> dict[str, float]:
        if not model:
            return {"input": 0.0, "output": 0.0}
        pid = _CATALOG_PROVIDER_MAP.get(provider_name, provider_name)
        try:
            from providers.catalog import get_shared_catalog
            adapter = get_shared_catalog().get_adapter(pid)
        except Exception:
            adapter = None
        if adapter is None:
            return {"input": 0.0, "output": 0.0}
        try:
            pricing = adapter.pricing_per_1k(model) or {}
        except Exception:
            pricing = {}
        input_cost = max(0.0, self._float_or_default(pricing.get("input", 0.0), 0.0))
        output_cost = max(0.0, self._float_or_default(pricing.get("output", 0.0), 0.0))
        return {"input": input_cost, "output": output_cost}

    def _estimate_candidate_cost_usd(
        self,
        provider_name: str,
        config: dict,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> float:
        model = str(config.get("model", "") or "")
        if not model and provider_name == self.provider:
            model = str(self.model or "")
        pricing = self._pricing_for_model(provider_name, model)
        if pricing["input"] <= 0.0 and pricing["output"] <= 0.0:
            return 0.0
        in_cost = (float(prompt_tokens) / 1000.0) * pricing["input"]
        out_cost = (float(completion_tokens) / 1000.0) * pricing["output"]
        return max(0.0, in_cost + out_cost)

    def _route_candidates_with_budget(
        self,
        candidates: list[tuple[str, dict]],
        messages: list[dict],
        kwargs: dict[str, Any],
    ) -> tuple[list[tuple[str, dict]], dict[str, Any]]:
        snapshot = self._budget_snapshot()
        if not snapshot["enabled"]:
            return candidates, snapshot

        prompt_tokens, completion_tokens = self._estimate_tokens_for_budget(messages, kwargs)
        remaining = float(snapshot.get("remaining_usd", 0.0))
        annotated: list[dict[str, Any]] = []
        for idx, (provider_name, config) in enumerate(candidates):
            estimated = self._estimate_candidate_cost_usd(
                provider_name,
                config,
                prompt_tokens,
                completion_tokens,
            )
            affordable = estimated <= 0.0 or remaining >= estimated
            annotated.append({
                "idx": idx,
                "provider": provider_name,
                "config": config,
                "estimated_usd": estimated,
                "affordable": affordable,
            })

        affordable = [row for row in annotated if row["affordable"]]
        over_budget = [row for row in annotated if not row["affordable"]]

        headroom_ratio = float(snapshot.get("headroom_ratio", 1.0))
        tight_ratio = float(snapshot.get("tight_ratio", 0.25))
        if affordable and headroom_ratio <= tight_ratio:
            # When budget headroom is low, prefer the cheapest affordable
            # provider first (ties preserve initial candidate order).
            affordable.sort(key=lambda row: (row["estimated_usd"], row["idx"]))
            ordered = affordable + over_budget
        elif affordable:
            # Normal mode: preserve configured provider priority, but defer
            # over-budget candidates to the back of the queue.
            ordered = affordable + over_budget
        else:
            # If every candidate is over budget, keep the system available
            # by trying the cheapest option first instead of hard failing.
            ordered = sorted(over_budget, key=lambda row: (row["estimated_usd"], row["idx"]))

        routed = [(row["provider"], row["config"]) for row in ordered]
        snapshot["prompt_tokens_estimate"] = prompt_tokens
        snapshot["completion_tokens_estimate"] = completion_tokens
        snapshot["candidate_costs"] = [
            {
                "provider": row["provider"],
                "estimated_usd": row["estimated_usd"],
                "affordable": row["affordable"],
            }
            for row in ordered
        ]
        snapshot["over_budget_providers"] = [row["provider"] for row in over_budget]
        return routed, snapshot

    def set_catalog(self, catalog) -> None:
        """Attach the shared :class:`ProviderCatalog` for metadata lookups.

        Commit 1 only stores the reference; the runtime keeps reading
        its primary config from env vars exactly as before so this is
        backward-compatible. Commit 3 flips the primary source over to
        the catalog once every adapter has been reviewed.
        """
        self._catalog = catalog

    @staticmethod
    def _anthropic_usage_block(data: Any) -> Optional[dict]:
        """Lift ``usage`` off a raw Anthropic Messages response.

        The non-stream Anthropic normalisers used to return only
        ``{"choices": [...]}``, dropping ``usage`` on the floor, so
        ``_budget_record`` saw ``(0, 0, 0)`` and a NON-streamed
        Anthropic turn billed nothing at all, not just its cache
        tokens, its input and output tokens too. Carrying the block
        through is what makes "a turn costs the same whether or not it
        streamed" true, since the stream route already bills off the
        same four fields.

        Returns ``None`` when the response carries no usable usage, so
        the caller can leave the key off entirely and ``_extract_usage``
        keeps distinguishing "provider sent nothing" from "provider sent
        zeros".
        """
        if not isinstance(data, dict):
            return None
        raw = data.get("usage")
        if not isinstance(raw, dict):
            return None
        block: dict = {}
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ):
            value = raw.get(key)
            if value is None:
                continue
            try:
                block[key] = max(0, int(value))
            except (TypeError, ValueError):
                continue
        return block or None

    @staticmethod
    def _normalize_anthropic_response(data: dict) -> dict:
        """Convert raw Anthropic Messages API response to OpenAI-shaped dict."""
        text_parts: list[str] = []
        tool_calls: list[dict] = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block["text"])
            elif block.get("type") == "tool_use":
                tool_calls.append({
                    "id": block["id"],
                    "type": "function",
                    "function": {
                        "name": block["name"],
                        "arguments": json.dumps(block.get("input", {})),
                    },
                })
        msg: dict = {"role": "assistant", "content": "\n".join(text_parts)}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        out: dict = {
            "choices": [
                {"message": msg, "finish_reason": data.get("stop_reason", "end_turn")},
            ],
        }
        usage = LLMProvider._anthropic_usage_block(data)
        if usage is not None:
            out["usage"] = usage
        return out

    def _get_provider_config(self, provider_name: str) -> dict:
        """Resolve base_url / api_key / model for a named provider.

        The model is always resolved through the shared catalog so the
        failover candidate list never contains a stale literal.

        For provider ids that have no runtime binding in this module,
        returns a shape-compatible dict with ``supported=False`` and
        empty URL / key / model. Callers (``_build_candidate_list``,
        ``health_snapshot``, ``is_available``, ``_call_provider``)
        must treat these as unreachable instead of silently
        substituting OpenAI defaults — that substitution was the
        exact footgun this method used to hide behind its two-arg
        ``dict.get`` fallback.
        """
        # For local providers we honour the operator-configured base
        # URL (``self.base_url`` when the primary IS the local
        # provider, otherwise ``FERAL_LLM_BASE_URL`` /
        # ``FERAL_LMSTUDIO_BASE_URL`` env overrides).  these
        # branches hard-coded ``http://localhost:1234/v1`` /
        # ``ollama_openai_base_url()`` even when the primary was
        # already pointed at a non-default port — the failover loop
        # then silently retried against the wrong URL. See
        # findings/13-llm-core.md fix #3.
        if provider_name == "codex":
            return {
                "base_url": "app-server://stdio",
                "api_key": "codex-managed-auth",
                "model": _default_model_for("codex"),
                "supported": True,
            }
        if provider_name == "ollama":
            configured = (
                self.base_url
                if self.provider == "ollama" and self.base_url
                else os.getenv("FERAL_OLLAMA_BASE_URL", "")
            )
            return {
                "base_url": configured or ollama_openai_base_url(),
                "api_key": "ollama",
                "model": _default_model_for("ollama") or self._detect_ollama() or "",
                "supported": True,
            }
        if provider_name == "lmstudio":
            detected = self._detect_lmstudio()
            configured = (
                self.base_url
                if self.provider == "lmstudio" and self.base_url
                else os.getenv("FERAL_LMSTUDIO_BASE_URL", "")
            )
            return {
                "base_url": configured or "http://localhost:1234/v1",
                "api_key": "lm-studio",
                "model": detected or _default_model_for("lmstudio"),
                "supported": True,
            }
        reg = _PROVIDER_REGISTRY.get(provider_name)
        if reg is None:
            # Unknown provider id — return an explicitly unsupported
            # config so downstream code can report it honestly
            # instead of silently hitting api.openai.com.
            return {
                "base_url": "",
                "api_key": "",
                "model": _default_model_for(provider_name),
                "supported": False,
            }
        base_url, env_key = reg
        # Cross-cut #1 (v2026.5.42): prefer the labeled-vault active
        # key for failover candidates too. Pre-fix the failover loop
        # only honoured ``os.getenv(env_key)`` so an operator who only
        # ever set the secret via ``feral key add`` lost cross-provider
        # failover for the candidate.
        if provider_name == "gemini":
            api_key = _resolve_api_key("gemini") or _gemini_api_key() or ""
        else:
            api_key = _resolve_api_key(provider_name) or (
                os.getenv(env_key, "") if env_key else ""
            )
        return {
            "base_url": base_url,
            "api_key": api_key,
            "model": _default_model_for(provider_name),
            "supported": True,
        }

    def _build_candidate_list(self) -> list[tuple[str, dict]]:
        """Ordered list of (provider_name, config) — primary first, then fallbacks.

        Every config dict carries a ``supported`` bool so the failover
        loop, health snapshot and availability check can tell runtime
        candidates apart from catalog-only descriptors whose runtime
        adapter hasn't shipped yet.
        """
        candidates: list[tuple[str, dict]] = [
            (self.provider, {
                "base_url": self.base_url,
                "api_key": self.api_key,
                "model": self.model,
                "supported": is_supported_runtime_provider(self.provider),
            }),
        ]
        for fb in self._config.get("fallback_providers", []):
            if fb != self.provider:
                candidates.append((fb, self._get_provider_config(fb)))
        return candidates

    def _candidates_for_route(
        self, route_provider: str, route_model: str,
    ) -> list[tuple[str, dict]]:
        """Candidate chain that puts the adaptive router's (provider,
        model) first, then preserves the operator's primary and the
        configured fallback chain (deduped).

        When the route stays on the primary provider (the common case —
        a cheaper same-provider model) this is just the primary candidate
        with the model swapped, so the persistent client is reused. When
        the route hops providers, the primary is kept as the first
        fallback so a missing key on the routed provider degrades cleanly
        back to the operator's model instead of failing the turn.
        """
        candidates: list[tuple[str, dict]] = []
        seen: set[str] = set()
        if route_provider == self.provider:
            candidates.append((self.provider, {
                "base_url": self.base_url,
                "api_key": self.api_key,
                "model": route_model,
                "supported": is_supported_runtime_provider(self.provider),
            }))
        else:
            cfg = dict(self._get_provider_config(route_provider))
            cfg["model"] = route_model
            candidates.append((route_provider, cfg))
        seen.add(route_provider)
        if self.provider not in seen:
            candidates.append((self.provider, {
                "base_url": self.base_url,
                "api_key": self.api_key,
                "model": self.model,
                "supported": is_supported_runtime_provider(self.provider),
            }))
            seen.add(self.provider)
        for fb in self._config.get("fallback_providers", []):
            if fb not in seen:
                candidates.append((fb, self._get_provider_config(fb)))
                seen.add(fb)
        return candidates

    @staticmethod
    def _anthropic_cache_enabled() -> bool:
        """Kill switch for Anthropic prompt-cache breakpoints.

        On by default. Set ``FERAL_ANTHROPIC_PROMPT_CACHE=0`` to send
        bodies with no ``cache_control`` at all, useful when pointing
        ``base_url`` at a proxy or gateway that does not understand the
        field.
        """
        raw = os.environ.get("FERAL_ANTHROPIC_PROMPT_CACHE", "1").strip().lower()
        return raw not in ("0", "false", "no", "off")

    @staticmethod
    def _apply_anthropic_cache_breakpoints(body: dict) -> dict:
        """Place Anthropic prompt-cache breakpoints on ``body`` in place.

        Source: platform.claude.com computer-use-tool docs, "Manage
        screenshot history for prompt caching" (fetched 2026-08-19):

          "Place one ``cache_control`` breakpoint after the system
           prompt and tool definitions, and up to three more on the most
           recent ``tool_result`` blocks, advancing them each turn."
           "Prune old screenshots in *batches*, not one each turn ...
           keep the last three screenshots and prune every 25 turns, so
           the prefix stays byte-identical between prune events."

        and platform.claude.com prompt-caching docs (same date) for the
        mechanics: render order is ``tools`` -> ``system`` ->
        ``messages``, so a breakpoint on the LAST system block caches
        tools and system together; the hard ceiling is 4 breakpoints per
        request (a 5th is a 400); the lookback window when matching a
        prior write is 20 blocks.

        1 (system+tools) + 3 (recent tool_results) = exactly 4, which is
        why no other breakpoint may be added here.

        This is the missing other half of the batch screenshot-pruning
        the tool-result-image lane already implements
        (``multimodal_blocks.should_prune_images``): that scheduler
        exists solely to keep the prefix byte-stable between prune
        events, which is worth nothing without a breakpoint to cache
        against.

        Below-minimum prefixes (512-4096 tokens depending on model) are
        silently not cached by the API rather than erroring, so there is
        no size check to make here.
        """
        marker = {"type": "ephemeral"}

        # Breakpoint 1: end of the tools+system prefix.
        #
        # ``system`` is emitted as a plain string elsewhere in this
        # builder because Anthropic accepts both forms; the block-list
        # form is required to carry ``cache_control``, so promote it.
        # Falling back to the last tool definition covers a tools-only
        # request (no system prompt), where the tools ARE the prefix.
        system = body.get("system")
        if isinstance(system, str) and system:
            body["system"] = [
                {"type": "text", "text": system, "cache_control": dict(marker)}
            ]
        elif isinstance(system, list) and system:
            for block in reversed(system):
                if isinstance(block, dict):
                    block["cache_control"] = dict(marker)
                    break
        else:
            tools = body.get("tools")
            if isinstance(tools, list) and tools and isinstance(tools[-1], dict):
                tools[-1]["cache_control"] = dict(marker)

        # Breakpoints 2-4: the three most recent ``tool_result`` blocks,
        # advancing each turn. In a growing agentic conversation the
        # newest one writes a fresh entry and the older ones stay valid
        # read points, which is what keeps a hit reachable inside the
        # 20-block lookback when a single turn appends many blocks.
        remaining = 3
        for msg in reversed(body.get("messages") or []):
            if remaining <= 0:
                break
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for block in reversed(content):
                if remaining <= 0:
                    break
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    block["cache_control"] = dict(marker)
                    remaining -= 1
        return body

    @staticmethod
    def _build_anthropic_body(
        model: str, messages: list[dict], tools: Optional[list[dict]],
        temperature: float, max_tokens: int,
        *,
        force_tool: Optional[str] = None,
    ) -> dict:
        """Build Anthropic Messages API request body."""
        system_text, conv = _convert_messages_for_anthropic(messages)
        body: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": conv,
        }
        if system_text.strip():
            body["system"] = system_text.strip()
        if tools:
            anthropic_tools = []
            for t in tools:
                if t.get("type") == "function":
                    fn = t["function"]
                    anthropic_tools.append({
                        "name": fn["name"],
                        "description": fn.get("description", ""),
                        "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
                    })
            if anthropic_tools:
                body["tools"] = anthropic_tools
                if force_tool and any(
                    t.get("name") == force_tool for t in anthropic_tools
                ):
                    translated = to_provider_tool_choice("anthropic", force_tool)
                    if translated is not None:
                        body["tool_choice"] = translated
        apply_reasoning_fork("anthropic", model, body)
        _enforce_anthropic_thinking_max_tokens(body)
        # Last, so the breakpoint lands on the final shape of ``system``
        # and ``tools`` (the reasoning fork can still add top-level keys,
        # but those render outside the cached prefix).
        if LLMProvider._anthropic_cache_enabled():
            LLMProvider._apply_anthropic_cache_breakpoints(body)
        return body

    async def _call_provider(
        self,
        provider_name: str,
        config: dict,
        messages: list[dict],
        tools: Optional[list[dict]],
        **kwargs,
    ) -> dict:
        """Make a chat request to a specific provider. Raises on error.

        ``_retry_max`` / ``_retry_delays`` (popped from ``kwargs``) let
        the failover orchestrator dial down same-provider retries when
        a healthy fallback is configured — avoids spending the full
        ``RETRY_DELAYS`` budget on a known-bad provider before routing.
        Defaults preserve historical behaviour for direct callers.
        """
        retry_max = kwargs.pop("_retry_max", None)
        retry_delays = kwargs.pop("_retry_delays", None)
        force_tool = kwargs.pop("force_tool", None)
        # Refuse up front for provider ids that have no runtime
        # adapter. Previously the fallback path built an httpx client
        # against whatever default ``_get_provider_config`` handed
        # back — which was OpenAI for any unknown id. That silently
        # turned a user-selected ``bedrock`` fallback into an OpenAI
        # call. Raise a clear error so the failover loop records a
        # cooldown against the right provider name.
        if config.get("supported") is False or not is_supported_runtime_provider(provider_name):
            raise RuntimeError(
                f"Provider {provider_name!r} has no runtime adapter — "
                f"supported: {sorted(SUPPORTED_RUNTIME_PROVIDERS)}"
            )
        # On the primary provider, honour a per-call model override carried
        # in ``config["model"]`` (the adaptive router's tier pick) — falling
        # back to ``self.model`` when the candidate didn't specify one. This
        # is what lets the chat path downshift to a cheaper same-provider
        # model without mutating the shared provider's global ``self.model``.
        if provider_name == self.provider:
            selected_model = str(config.get("model") or self.model)
        else:
            selected_model = str(config.get("model", "") or "")
        temperature = kwargs.get("temperature", 0.7)
        max_tokens = kwargs.get("max_tokens", 1024)

        if provider_name == "codex":
            adapter = self._get_codex_adapter()
            response = await adapter.chat(
                self._codex_messages(messages),
                model=selected_model,
                max_tokens=max_tokens,
                temperature=temperature,
                tools=tools,
            )
            return self._codex_response_dict(response)

        model_guard_error = _chat_completions_model_guard(provider_name, selected_model)
        if model_guard_error:
            raise RuntimeError(model_guard_error)

        # Primary provider — reuse existing client
        if provider_name == self.provider:
            if provider_name == "anthropic":
                body = self._build_anthropic_body(
                    selected_model, messages, tools, temperature, max_tokens,
                    force_tool=force_tool,
                )

                async def _do_primary_anthropic():
                    resp = await self.client.post("/messages", json=body)
                    resp.raise_for_status()
                    return resp.json()

                data = await _retry_llm_call(
                    _do_primary_anthropic,
                    max_retries=retry_max,
                    delays=retry_delays,
                )
                return self._normalize_anthropic_response(data)

            # Responses-only models (gpt-5.6-sol, gpt-5.5-pro, o3-pro, ...)
            # never go to /chat/completions from here. Before this check
            # the branch below built a chat-completions body, and
            # ``apply_reasoning_fork`` added ``reasoning_effort`` to it;
            # with tools attached OpenAI answers 400 "Function tools with
            # reasoning_effort are not supported for gpt-5.6-sol in
            # /v1/chat/completions. To use function tools, use
            # /v1/responses or set reasoning_effort to 'none'". Every
            # candidate in the failover chain passes through here, so
            # this is the one place the route has to be right.
            if _responses_endpoint_for(provider_name, selected_model):
                return await self._call_provider_responses(
                    self.client, provider_name, selected_model, messages,
                    tools, temperature, max_tokens, force_tool=force_tool,
                    retry_max=retry_max, retry_delays=retry_delays,
                )

            body = {
                "model": selected_model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if tools:
                clean_tools = [{k: v for k, v in t.items() if k != "_feral_meta"} for t in tools]
                if self.provider in ("openai",):
                    clean_tools = _cap_openai_chat_tools(clean_tools)
                body["tools"] = clean_tools
                body["tool_choice"] = _resolve_tool_choice(
                    self.provider, clean_tools, force_tool,
                )

            apply_reasoning_fork(self.provider, selected_model, body)

            async def _do_primary():
                resp = await self.client.post("/chat/completions", json=body)
                resp.raise_for_status()
                return resp.json()

            return await _retry_llm_call(
                _do_primary,
                max_retries=retry_max,
                delays=retry_delays,
            )

        # Fallback provider — build a temporary client
        base_url = config["base_url"]
        api_key = config["api_key"]
        model = config["model"]
        if not api_key:
            raise RuntimeError(f"No API key configured for fallback provider '{provider_name}'")

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if provider_name == "anthropic":
            headers["x-api-key"] = api_key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {api_key}"

        async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=60.0) as tmp:
            if provider_name == "anthropic":
                body = self._build_anthropic_body(
                    model, messages, tools, temperature, max_tokens,
                    force_tool=force_tool,
                )

                async def _do_fb_anthropic():
                    resp = await tmp.post("/messages", json=body)
                    resp.raise_for_status()
                    return resp.json()

                data = await _retry_llm_call(
                    _do_fb_anthropic,
                    max_retries=retry_max,
                    delays=retry_delays,
                )
                return self._normalize_anthropic_response(data)

            # Same routing rule as the primary branch, on the candidate's
            # temporary client. This is the exact path the 2026-09-02
            # brain.err hits took: primary misconfigured as
            # deepseek | anthropic/claude-sonnet-5, so openai/gpt-5.6-sol
            # was a FALLBACK candidate and was posted to /chat/completions
            # with tools + reasoning_effort, four times in a row.
            if _responses_endpoint_for(provider_name, model):
                return await self._call_provider_responses(
                    tmp, provider_name, model, messages, tools, temperature,
                    max_tokens, force_tool=force_tool,
                    retry_max=retry_max, retry_delays=retry_delays,
                )

            body = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if tools:
                clean_tools = [{k: v for k, v in t.items() if k != "_feral_meta"} for t in tools]
                if provider_name in ("openai",):
                    clean_tools = _cap_openai_chat_tools(clean_tools)
                body["tools"] = clean_tools
                body["tool_choice"] = _resolve_tool_choice(
                    provider_name, clean_tools, force_tool,
                )

            apply_reasoning_fork(provider_name, model, body)

            async def _do_fb():
                resp = await tmp.post("/chat/completions", json=body)
                resp.raise_for_status()
                return resp.json()

            return await _retry_llm_call(
                _do_fb,
                max_retries=retry_max,
                delays=retry_delays,
            )

    async def chat_with_failover(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        **kwargs,
    ) -> dict:
        """Call chat() with automatic failover across configured providers.

        Same-provider transient retries are handled by ``_retry_llm_call``.
        Cross-provider routing is handled here based on error classification.

        On a successful failover hop the response carries a
        ``last_failover`` metadata block:

        ``{"from": "<primary>", "to": "<provider that answered>",
           "reason": "<FailoverReason.value>",
           "candidates_tried": [{"provider": str, "reason": str}, ...]}``

        Callers (orchestrator, WebUI) use this to render the
        "fallback active" chip / banner without parsing log lines.
        ``last_failover`` is omitted entirely when the primary
        succeeded on its first attempt.

        ``force_tool`` (kw-only, forwarded via ``**kwargs``) — when set
        to a tool name, EVERY candidate provider this call hops to
        will receive a per-provider ``tool_choice`` that forces the
        model to call that tool. Used by the orchestrator's grounded-
        memory closure to require ``notes_memory__fused_timeline`` on
        temporal-recall turns. Silently degrades to ``"auto"`` on a
        candidate that doesn't have the tool in its allowed set, or
        on providers (Gemini) that can't name a single tool on the
        wire shape we drive.
        """
        # Adaptive route (kw-only ``route`` = a ``route_call`` ProviderRef).
        # Popped FIRST so it never leaks into ``self.chat(**kwargs)`` on the
        # local-engine short-circuit below. When present and concrete it
        # makes the routed (provider, model) the first failover candidate.
        route = kwargs.pop("route", None)
        route_provider = ""
        route_model = ""
        if isinstance(route, dict):
            route_provider = str(route.get("provider") or "").strip()
            route_model = str(route.get("model") or "").strip()

        # Vision is resolved PER HOP further down, not here.
        #
        # This used to be a hard gate on ``self._vision_support_status()``
        # (the PRIMARY provider's capability) that answered
        # ``{"error": ...}`` and never tried the chain. Two ways that was
        # wrong: a text-only primary with a vision-capable fallback never
        # reached the fallback that could actually see the image, and a
        # vision-capable primary that failed over to a text-only hop
        # turned a recoverable degradation into a dead turn. Both now
        # degrade: the candidate loop strips the images for any hop that
        # cannot take them and tells the model, in words, that an image
        # was dropped.
        messages_have_vision = self._messages_contain_vision(messages)

        if self._local_engine and self.provider in ("local", "hybrid"):
            return await self.chat(messages, tools, **kwargs)

        # Cost-budget pre-flight (Wave 2 Lane 09). The orchestrator
        # passes ``call_site="chat"`` for user-facing turns;
        # background loops (screen_loop, proactive, learner) pass
        # their own call_site name. Defaults to "chat" so a missing
        # kwarg does the safe thing.
        call_site = str(kwargs.pop("call_site", "chat") or "chat")
        max_tokens_kw = int(kwargs.get("max_tokens", 1024) or 1024)
        budget_model = route_model or self.model
        budget_block = await self._budget_check(call_site, budget_model, max_tokens_kw)
        if budget_block is not None:
            return budget_block

        from observability.metrics import increment, measure

        if route_provider and route_model:
            candidates = self._candidates_for_route(route_provider, route_model)
        else:
            candidates = self._build_candidate_list()
        candidates, budget_ctx = self._route_candidates_with_budget(
            candidates,
            messages,
            kwargs,
        )
        self._last_budget_routing = budget_ctx
        last_error: Optional[Exception] = None
        # Track per-call cross-provider failover history so the
        # response surfaces it (and ``health_snapshot`` can report
        # the most recent hop). The first candidate is the primary,
        # so ``failed_candidates`` collects each candidate that was
        # attempted-and-failed BEFORE the eventual success.
        failed_candidates: list[dict] = []
        primary_provider = candidates[0][0] if candidates else self.provider

        # When at least one supported fallback exists beyond the
        # primary, use the fast-fail retry profile so a transient 5xx
        # on the primary doesn't burn the whole RETRY_DELAYS budget
        # before we even try the fallback. With a single candidate
        # there's nowhere else to go, so keep the historical
        # 3 × [1, 2, 4]s policy.
        viable = [
            name for name, cfg in candidates
            if cfg.get("supported", True)
        ]
        use_fast_retry = len(viable) > 1
        retry_kwargs: dict[str, Any] = {}
        if use_fast_retry:
            retry_kwargs["_retry_max"] = _FAILOVER_FAST_MAX_RETRIES
            retry_kwargs["_retry_delays"] = _FAILOVER_FAST_DELAYS

        for provider_name, config in candidates:
            candidate_model = config.get("model") or self.model
            chat_capable, candidate_class = _chat_capability_of(provider_name, candidate_model)
            if not chat_capable:
                # The model exists but is not a chat model, so the hop can
                # only ever 404. Observed live on 2026-07-30: the ``openai``
                # fallback resolved to a non-chat id and every turn burned a
                # hop on
                #   HTTP 404 invalid_request_error, param=model: "This is not
                #   a chat model and thus not supported in the
                #   v1/chat/completions endpoint. Did you mean to use
                #   v1/completions?"
                # before falling through to the next candidate. Classifying
                # it here costs nothing and keeps the chain honest; the wire
                # call is the expensive way to learn the same fact.
                logger.info(
                    "Skipping %r in failover chain: model %r is not chat-capable (%s)",
                    provider_name, candidate_model, candidate_class,
                )
                last_error = last_error or RuntimeError(
                    f"Model {candidate_model!r} on provider {provider_name!r} "
                    "is not a chat model"
                )
                failed_candidates.append({
                    "provider": provider_name,
                    "reason": FailoverReason.MODEL_NOT_FOUND.value,
                })
                continue
            if not config.get("supported", True):
                # Skip catalog-only providers with no runtime adapter.
                # No cooldown — the problem isn't transient, it's that
                # this module has no wire for them. Logged once per
                # attempt so the ops log shows *why* the candidate was
                # passed over rather than silently dropping it.
                logger.info(
                    "Skipping unsupported provider %r in failover chain",
                    provider_name,
                )
                last_error = last_error or RuntimeError(
                    f"Provider {provider_name!r} has no runtime adapter"
                )
                failed_candidates.append({
                    "provider": provider_name,
                    "reason": FailoverReason.NOT_SUPPORTED.value,
                })
                continue
            if not self._cooldown.should_probe(provider_name):
                failed_candidates.append({
                    "provider": provider_name,
                    "reason": FailoverReason.COOLDOWN.value,
                })
                continue
            # Per-hop vision resolution. Capability belongs to the
            # candidate we are about to call, not to ``self.provider``.
            hop_messages = messages
            vision_degraded: Optional[dict] = None
            if messages_have_vision:
                vision_ok, vision_reason = self._vision_support_for(
                    provider_name, candidate_model,
                )
                if not vision_ok:
                    hop_messages, dropped = self._strip_vision_for_text_only_hop(
                        messages, provider=provider_name, reason=vision_reason,
                    )
                    if dropped:
                        vision_degraded = {
                            "provider": provider_name,
                            "model": candidate_model,
                            "images_dropped": dropped,
                            "reason": vision_reason,
                        }
                        logger.warning(
                            "Vision degradation on hop to %s (%s): stripped %d "
                            "image block(s) and continued as text. %s",
                            provider_name, candidate_model, dropped, vision_reason,
                        )
                        increment(
                            "feral.llm.vision_stripped_total",
                            attributes={"provider": provider_name},
                        )
                    else:
                        hop_messages = messages

            increment("feral.llm.calls_total", attributes={"provider": provider_name, "model": config.get("model", self.model)})
            try:
                with measure("feral.llm.latency", {"provider": provider_name, "model": config.get("model", self.model)}):
                    result = await self._call_provider(
                        provider_name, config, hop_messages, tools,
                        **retry_kwargs, **kwargs,
                    )
                self._cooldown.record_success(provider_name)
                if failed_candidates and provider_name != primary_provider:
                    last_failover = {
                        "from": primary_provider,
                        "to": provider_name,
                        "reason": failed_candidates[-1].get("reason", "unknown"),
                        "candidates_tried": list(failed_candidates),
                    }
                    if isinstance(result, dict):
                        result.setdefault("metadata", {})
                        if isinstance(result["metadata"], dict):
                            result["metadata"]["last_failover"] = last_failover
                        result["last_failover"] = last_failover
                    self._last_failover = last_failover
                else:
                    # Primary won on first hop — clear stale state so
                    # the next call's health snapshot doesn't keep
                    # advertising an out-of-date fallback chip.
                    self._last_failover = None
                # Tell the caller (orchestrator / WebUI) that the answer
                # it is about to render was produced WITHOUT the image.
                # The model was told in words too, but a machine-readable
                # flag lets the UI say so without parsing prose.
                if vision_degraded and isinstance(result, dict):
                    result.setdefault("metadata", {})
                    if isinstance(result["metadata"], dict):
                        result["metadata"]["vision_degraded"] = vision_degraded
                    result["vision_degraded"] = vision_degraded
                # Bill actual token usage. ``record_usage`` itself
                # checks the per-call-site / global caps after the
                # fact and raises ``BudgetExceeded`` if THIS call
                # tipped us over — the caller sees the response
                # normally and the NEXT pre-flight short-circuits.
                try:
                    await self._budget_record(
                        call_site,
                        config.get("model") or self.model,
                        result,
                    )
                except Exception as exc:
                    # Already swallowed inside _budget_record; this
                    # belt is in case it ever propagates.
                    logger.debug(
                        "_budget_record raised after success (non-fatal): %s",
                        exc,
                    )
                return result
            except Exception as e:
                increment("feral.llm.errors_total", attributes={"provider": provider_name})
                reason = classify_error(e)
                # Honour upstream Retry-After hint when present so the
                # cooldown reflects the provider's actual recovery
                # window instead of our static 60s default.
                retry_after = parse_retry_after(e)
                self._cooldown.record_failure(
                    provider_name, reason, retry_after=retry_after,
                )
                # A5: surface the upstream HTTP body (status + JSON
                # ``error.message`` / ``type`` / ``code`` / ``param``)
                # instead of opaque ``str(e)`` which for
                # ``httpx.HTTPStatusError`` is just the status line. The
                # structured ``extra`` fields make the full body
                # searchable in the ops log / metrics backend; the
                # primary log line stays human-readable.
                detail = _describe_error(e)
                http_status: Any = ""
                error_type = ""
                error_code = ""
                error_param = ""
                body_snippet = ""
                if isinstance(e, httpx.HTTPStatusError):
                    http_status = getattr(e.response, "status_code", "")
                    try:
                        payload = e.response.json()
                    except Exception:
                        payload = {}
                    if isinstance(payload, dict):
                        err_obj = payload.get("error", payload)
                        if isinstance(err_obj, dict):
                            error_type = str(err_obj.get("type", "") or "")
                            error_code = str(err_obj.get("code", "") or "")
                            error_param = str(err_obj.get("param", "") or "")
                    try:
                        body_snippet = (e.response.text or "")[:2048]
                    except Exception:
                        body_snippet = ""
                logger.warning(
                    "Provider %s failed (%s): %s",
                    provider_name, reason.value, detail,
                    extra={
                        "provider": provider_name,
                        "failover_reason": reason.value,
                        "http_status": http_status,
                        "error_type": error_type,
                        "error_code": error_code,
                        "error_param": error_param,
                        "body_snippet": body_snippet,
                    },
                )
                last_error = e
                failed_candidates.append({
                    "provider": provider_name,
                    "reason": reason.value,
                    "http_status": http_status if http_status != "" else None,
                    "error_type": error_type or None,
                    "error_code": error_code or None,
                })
                if reason == FailoverReason.CONTEXT_OVERFLOW:
                    raise
                continue

        # Every candidate failed. Record the chain so health snapshot
        # / clients can show what was attempted.
        if failed_candidates:
            self._last_failover = {
                "from": primary_provider,
                "to": None,
                "reason": "exhausted",
                "candidates_tried": list(failed_candidates),
            }
        if last_error:
            raise last_error
        raise RuntimeError("All LLM providers exhausted")

    def health_snapshot(self) -> dict:
        """Return a snapshot of every candidate provider's availability.

        Used by `GET /api/llm/health` to power the v2 "Fallbacks" card —
        the user can see which providers are live, which are in cooldown,
        and why, without having to dig through server logs.
        """
        now = time.time()
        primary_supported = is_supported_runtime_provider(self.provider)
        primary = {
            "provider": self.provider,
            "model": self.model,
            "has_key": bool(self.api_key) and self.api_key not in ("none", ""),
            "available": bool(self.available) and primary_supported,
            "base_url": self.base_url,
            "supported": primary_supported,
        }
        candidates = []
        try:
            candidate_list = self._build_candidate_list()
        except Exception:
            candidate_list = [(self.provider, {
                "base_url": self.base_url,
                "api_key": self.api_key,
                "model": self.model,
                "supported": primary_supported,
            })]
        for name, cfg in candidate_list:
            until = self._cooldown._cooldowns.get(name, 0.0)
            in_cooldown = until > now
            supported = bool(cfg.get("supported", is_supported_runtime_provider(name)))
            has_key = bool(cfg.get("api_key")) and cfg.get("api_key") not in ("none", "")
            candidates.append({
                "provider": name,
                "model": cfg.get("model") or "",
                "base_url": cfg.get("base_url") or "",
                "has_key": has_key,
                "in_cooldown": in_cooldown,
                "cooldown_until": until if in_cooldown else None,
                "cooldown_remaining": max(0.0, until - now) if in_cooldown else 0.0,
                "supported": supported,
            })
        fallbacks = list(self._config.get("fallback_providers", [])) if isinstance(self._config, dict) else []
        budget = self._budget_snapshot()
        last_budget = getattr(self, "_last_budget_routing", {})
        if isinstance(last_budget, dict) and last_budget:
            budget["last_routing"] = {
                "remaining_usd": last_budget.get("remaining_usd"),
                "candidate_costs": last_budget.get("candidate_costs", []),
                "over_budget_providers": last_budget.get("over_budget_providers", []),
                "prompt_tokens_estimate": last_budget.get("prompt_tokens_estimate", 0),
                "completion_tokens_estimate": last_budget.get("completion_tokens_estimate", 0),
            }
        return {
            "active": primary,
            "candidates": candidates,
            "fallback_providers": fallbacks,
            "budget": budget,
            # ``last_failover`` is None during steady state (primary
            # answered on first hop). The WebUI fallback chip reads
            # this directly — see findings/13-llm-core.md fix #5.
            "last_failover": getattr(self, "_last_failover", None),
            # Total ready-to-serve = supported AND has key AND not in cooldown.
            # Unsupported candidates were counted as "available" before  A3
            # whenever a lookalike env var happened to be set, inflating the
            # fallbacks card with providers the runtime could never actually
            # call.
            "total_available": sum(
                1 for c in candidates
                if c["has_key"] and not c["in_cooldown"] and c["supported"]
            ),
        }

    def is_available(self) -> bool:
        """True if at least one provider has a valid key and is not in cooldown.

        A primary or fallback that has no runtime adapter
        (``is_supported_runtime_provider`` False) never counts — even
        if the corresponding credential env var happens to be set.
        """
        if not self.available:
            return False
        if self._local_engine and self.provider in ("local", "hybrid"):
            return True
        if self.provider in ("ollama", "lmstudio"):
            return True
        if (
            is_supported_runtime_provider(self.provider)
            and self.api_key and self.api_key not in ("none", "")
            and self.base_url
            and self._cooldown.is_available(self.provider)
        ):
            return True
        for fb in self._config.get("fallback_providers", []):
            if fb == self.provider:
                continue
            cfg = self._get_provider_config(fb)
            if not cfg.get("supported"):
                continue
            if cfg.get("api_key") and self._cooldown.is_available(fb):
                return True
        return False

    async def close(self):
        client = getattr(self, "client", None)
        if client is not None:
            await client.aclose()
