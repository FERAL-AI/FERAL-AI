"""Reasoning-family request-body shaping for LLM providers.

Each provider's reasoning models accept a different param contract on
``/v1/chat/completions`` (and the Anthropic Messages API). These helpers
mutate the outbound body in-place so the dispatcher in
``agents.llm_provider`` stays provider-agnostic. Extracted from
``agents.llm_provider`` (-A15); re-exported from there for import
compatibility.
"""

from __future__ import annotations

import re

# v2026.5.25 — Pro-model family. Used by ``apply_responses_param_fork``
# to clamp the Responses-API param subset OpenAI accepts on these
# specific SKUs (live-verified 2026-05-14 against gpt-5.5-pro):
#   * ``max_output_tokens`` minimum is 16 (not 1).
#   * ``reasoning.effort`` accepts only ``medium`` / ``high`` / ``xhigh``.
# Pattern accepts dated snapshots (``gpt-5-pro-2026-05-08``).
_PRO_MODEL_RX = re.compile(r"^gpt-5(\.[0-9]+)?-pro(-\d{4}-\d{2}-\d{2})?$")


def _apply_openai_reasoning_fork(model: str, body: dict) -> dict:
    """Rewrite *body* for OpenAI's reasoning-family contract.

    gpt-5, gpt-5.4*, gpt-5.5*, o1/o3/o4: Chat Completions rejects
    ``max_tokens`` and non-1 ``temperature`` / ``top_p`` / penalty
    params with a 400 (this is the exact shape of the v2026.5.0 shipped
    400s). The fix: rename to ``max_completion_tokens`` and strip.
    Non-reasoning models (gpt-4o, gpt-4.1) pass through untouched.

    Imported locally to keep module-load order simple — providers.model_classes
    has no side-effects beyond regex compilation.
    """
    from providers.model_classes import classify
    if classify("openai", model) != "reasoning":
        return body
    if "max_tokens" in body and "max_completion_tokens" not in body:
        body["max_completion_tokens"] = body.pop("max_tokens")
    else:
        body.pop("max_tokens", None)
    temp = body.get("temperature")
    if temp is not None and temp != 1 and temp != 1.0:
        body.pop("temperature", None)
    for key in ("top_p", "presence_penalty", "frequency_penalty"):
        body.pop(key, None)
    body.setdefault("reasoning_effort", "medium")
    return body


def _apply_deepseek_reasoning_fork(model: str, body: dict) -> dict:
    """Rewrite *body* for DeepSeek's thinking-mode contract.

    v4-pro / deepseek-reasoner demand ``extra_body.thinking`` enabled
    and reject sampling params in strict mode. v4-flash / deepseek-chat
    are pass-through.
    """
    from providers.model_classes import classify
    if classify("deepseek", model) != "reasoning":
        return body
    extra = body.setdefault("extra_body", {})
    if not isinstance(extra, dict):
        extra = {}
        body["extra_body"] = extra
    extra.setdefault("thinking", {"type": "enabled"})
    body.setdefault("reasoning_effort", "high")
    for key in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
        body.pop(key, None)
    return body


def _apply_gemini_reasoning_fork(model: str, body: dict) -> dict:
    """Rewrite *body* for Gemini's thinkingConfig contract.

    ``-thinking`` variants accept ``generationConfig.thinkingConfig.enabled=true``.
    Non-thinking Gemini 3.x rejects the block with a 400.
    """
    from providers.model_classes import classify
    if classify("gemini", model) != "reasoning":
        return body
    gen_cfg = body.setdefault("generationConfig", {})
    thinking_cfg = gen_cfg.setdefault("thinkingConfig", {})
    thinking_cfg.setdefault("enabled", True)
    return body


def _apply_anthropic_reasoning_fork(model: str, body: dict) -> dict:
    """Rewrite *body* for Anthropic's thinking contract.

    Extended-thinking models (Sonnet 4.6, Haiku 4.5, Sonnet / Opus 4.5)
    accept ``thinking={"type":"enabled","budget_tokens":N}`` and require
    ``temperature=1``. Adaptive-thinking models (Opus 4.7) decline the
    explicit block — the model chooses its own depth.
    """
    from providers.model_classes import classify
    if classify("anthropic", model) != "reasoning":
        return body
    # Import lazily to avoid circular import at module load.
    from providers.anthropic_provider import (
        AnthropicProvider,
        _default_budget_tokens,
    )
    probe = AnthropicProvider(api_key="_probe_")
    if probe.supports_extended_thinking(model):
        budget = _default_budget_tokens(model)
        if budget:
            body.setdefault("thinking", {
                "type": "enabled",
                "budget_tokens": int(budget),
            })
            body.pop("temperature", None)
    elif probe.supports_adaptive_thinking(model):
        body.pop("thinking", None)
        # Live smoke on 2026-04-26 confirmed: claude-opus-4-7 returns
        # 400 ``temperature is deprecated for this model`` when any
        # temperature value is sent. The adaptive-thinking contract
        # says the model chooses its own behaviour; temperature is no
        # longer a caller-controlled knob for this class. Drop it.
        body.pop("temperature", None)
    return body


def _apply_groq_reasoning_fork(model: str, body: dict) -> dict:
    """Mirror the OpenAI fork for Groq-hosted reasoning models."""
    from providers.model_classes import classify
    if classify("groq", model) != "reasoning":
        return body
    if "max_tokens" in body and "max_completion_tokens" not in body:
        body["max_completion_tokens"] = body.pop("max_tokens")
    temp = body.get("temperature")
    if temp is not None and temp != 1 and temp != 1.0:
        body.pop("temperature", None)
    for key in ("top_p", "presence_penalty", "frequency_penalty"):
        body.pop(key, None)
    body.setdefault("reasoning_effort", "medium")
    return body


def apply_reasoning_fork(provider: str, model: str, body: dict) -> dict:
    """Provider-router wrapper around the per-provider reasoning forks.

    Called at every chat-body assembly site in the dispatcher so the
    outbound wire-shape matches the selected model's reasoning contract.
    Non-reasoning models pass through untouched. ``body`` is mutated
    in-place AND returned for caller convenience. See
    ``docs/mintlify/providers/reasoning-models.mdx`` for the full
    per-provider param table.
    """
    pid = (provider or "").lower()
    if pid == "openai":
        return _apply_openai_reasoning_fork(model, body)
    if pid == "deepseek":
        return _apply_deepseek_reasoning_fork(model, body)
    if pid == "gemini":
        return _apply_gemini_reasoning_fork(model, body)
    if pid == "anthropic":
        return _apply_anthropic_reasoning_fork(model, body)
    if pid == "groq":
        return _apply_groq_reasoning_fork(model, body)
    return body


def apply_responses_param_fork(model: str, body: dict) -> dict:
    """Rewrite *body* in-place for OpenAI's ``/v1/responses`` API.

    v2026.5.23 initial. v2026.5.25 — tightens for live-verified
    gpt-5.5-pro constraints (operator's launch-blocking bug):

    * ``max_output_tokens`` minimum is **16** for ``gpt-5-pro`` /
      ``gpt-5.5-pro`` families. Anything below trips 400 "integer
      below minimum value". We clamp here so callers passing low
      caps (e.g. the availability probe) don't hit the 400.
    * ``reasoning.effort`` valid values for ``gpt-5.5-pro`` are
      **only** ``medium``, ``high``, ``xhigh``. ``low`` / ``none`` /
      ``minimal`` trip 400 "Unsupported value". For other Pro models
      OpenAI's accepted set may include ``low`` etc; we clamp the
      ``gpt-5(.x)?-pro`` family to the valid set and pass through
      for everything else.

    Differences from the Chat-Completions reasoning fork:

    * Token cap is ``max_output_tokens`` (counts reasoning + visible
      output), NOT ``max_completion_tokens``.
    * Reasoning controls live under a nested ``reasoning`` object,
      not the top-level ``reasoning_effort`` string.
    * The chat-shaped ``messages`` / ``temperature`` / ``top_p`` /
      penalty params are deliberately left for the dispatcher to
      translate; this fork only normalises the keys that already
      live on the body.
    """
    if "max_tokens" in body and "max_output_tokens" not in body:
        body["max_output_tokens"] = body.pop("max_tokens")
    elif "max_completion_tokens" in body and "max_output_tokens" not in body:
        # Caller already ran the chat-completions reasoning fork and is
        # now switching adapters; honour the renamed cap.
        body["max_output_tokens"] = body.pop("max_completion_tokens")
    else:
        body.pop("max_tokens", None)
        body.pop("max_completion_tokens", None)

    # v2026.5.25 — clamp max_output_tokens to the per-model floor.
    # Pro families enforce a 16-token minimum. Non-Pro Responses
    # callers (none in FERAL today, but future-proof) get a lower
    # floor of 1.
    is_pro = bool(_PRO_MODEL_RX.match(model or ""))
    floor = 16 if is_pro else 1
    cap = body.get("max_output_tokens")
    if isinstance(cap, int) and cap < floor:
        body["max_output_tokens"] = floor

    # Nest reasoning under an object. Honour an existing
    # ``reasoning_effort`` string the chat-completions fork may have
    # set so the operator's configured effort survives the endpoint
    # switch.
    legacy_effort = body.pop("reasoning_effort", None)
    reasoning = body.get("reasoning")
    if not isinstance(reasoning, dict):
        reasoning = {}
        body["reasoning"] = reasoning
    if "effort" not in reasoning:
        reasoning["effort"] = legacy_effort or "medium"

    # v2026.5.25 — Pro models only accept medium / high / xhigh.
    # Live-verified against gpt-5.5-pro 2026-05-14. Clamp.
    if is_pro:
        effort = str(reasoning.get("effort") or "medium").lower()
        if effort not in ("medium", "high", "xhigh"):
            reasoning["effort"] = "medium"

    # Strip chat-shaped sampling params that Responses API doesn't honour
    # for reasoning models. Temperature is supported for non-reasoning
    # Responses models but Pro / o-series reject anything other than 1
    # — and our caller only hits this fork for responses-class models,
    # which are all reasoning. Drop temperature to avoid the 400.
    temp = body.get("temperature")
    if temp is not None and temp != 1 and temp != 1.0:
        body.pop("temperature", None)
    for key in ("top_p", "presence_penalty", "frequency_penalty"):
        body.pop(key, None)
    return body


__all__ = [
    "_apply_openai_reasoning_fork",
    "_apply_deepseek_reasoning_fork",
    "_apply_gemini_reasoning_fork",
    "_apply_anthropic_reasoning_fork",
    "_apply_groq_reasoning_fork",
    "apply_reasoning_fork",
    "apply_responses_param_fork",
]
