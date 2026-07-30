"""Adaptive prompt → tier routing (agents/llm_router.py + LLMProvider).

Pins the intelligence layer added on top of the existing error-based
failover:

1. The difficulty classifier grades turns conservatively (trivial →
   cheap, substantive/agentic → balanced, hard reasoning → premium).
2. ``escalate`` / ``apply_cost_downshift`` move tiers the right way.
3. ``route_call(adaptive=True)`` applies the budget downshift and the
   local-first policy; the auto path NEVER switches providers.
4. ``_candidates_for_route`` puts the routed model first.
5. ``_call_provider`` honours a per-call model override on the primary
   provider (the core fix that makes routing actually take effect).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents import llm_router
from agents.llm_provider import LLMProvider


pytestmark = pytest.mark.no_auto_feral_home


# ─────────────────────────────────────────────────────────────────────
# Difficulty classifier
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", ["hi", "hello there", "thanks!", "ok", "yo"])
def test_trivial_turns_route_cheap(text):
    assert llm_router.classify_difficulty(text) == llm_router.CHEAP


@pytest.mark.parametrize("text", [
    "Can you refactor this function and explain why it's slow?",
    "def foo(x):\n    return x*2\nWhy does this raise?",
    "Prove that the algorithm is O(n log n).",
    "Walk me through the trade-offs of these two architectures.",
])
def test_hard_reasoning_routes_premium(text):
    assert llm_router.classify_difficulty(text) == llm_router.PREMIUM


def test_ordinary_question_routes_balanced():
    out = llm_router.classify_difficulty("What's the capital of France and its population?")
    assert out == llm_router.BALANCED


def test_tool_turns_never_cheap():
    # Even a short prompt is balanced when tools are offered (agentic).
    assert llm_router.classify_difficulty("do it", has_tools=True) == llm_router.BALANCED


def test_vision_with_reasoning_is_premium():
    out = llm_router.classify_difficulty(
        "Analyze this screenshot and debug the stack trace", has_vision=True,
    )
    assert out == llm_router.PREMIUM


def test_escalate_clamps_at_premium():
    assert llm_router.escalate(llm_router.CHEAP) == llm_router.BALANCED
    assert llm_router.escalate(llm_router.BALANCED) == llm_router.PREMIUM
    assert llm_router.escalate(llm_router.PREMIUM) == llm_router.PREMIUM


def test_cost_downshift_only_when_tight():
    # Healthy headroom → unchanged.
    assert llm_router.apply_cost_downshift(
        llm_router.PREMIUM, headroom_ratio=0.9, tight_ratio=0.25,
    ) == llm_router.PREMIUM
    # Tight headroom → drop one tier.
    assert llm_router.apply_cost_downshift(
        llm_router.PREMIUM, headroom_ratio=0.1, tight_ratio=0.25,
    ) == llm_router.BALANCED
    assert llm_router.apply_cost_downshift(
        llm_router.BALANCED, headroom_ratio=0.1, tight_ratio=0.25,
    ) == llm_router.CHEAP
    # Already cheapest → stays.
    assert llm_router.apply_cost_downshift(
        llm_router.CHEAP, headroom_ratio=0.0, tight_ratio=0.25,
    ) == llm_router.CHEAP


# ─────────────────────────────────────────────────────────────────────
# route_call — same-provider tiering + adaptive policy
# ─────────────────────────────────────────────────────────────────────


def _llm(config: dict | None = None, *, provider="openai", model="gpt-5"):
    llm = LLMProvider.__new__(LLMProvider)
    llm.provider = provider
    llm.model = model
    llm.base_url = "https://api.openai.com/v1"
    llm.api_key = "sk-x"
    llm._config = config or {}
    return llm


def test_cheap_tier_downshifts_model_within_same_provider():
    # Asserted against ``_CHEAP_SIBLING["openai"]``, refreshed 2026-07-30
    # from ``gpt-4o-mini`` (deprecated — see ``_DEPRECATED_OPENAI_IDS`` in
    # tests/test_provider_catalog.py) to ``gpt-5-nano``, the cheapest SKU
    # OpenAI currently publishes. What this test actually pins is the
    # invariant, not the id: the cheap tier stays on the SAME provider
    # and swaps only the model.
    from agents.llm_provider import LLMProvider as _LP

    ref = _llm().route_call("chat", tier="cheap")
    assert ref["provider"] == "openai"
    assert ref["model"] == _LP._CHEAP_SIBLING["openai"]
    assert ref["model"] != "gpt-5"  # actually downshifted from the primary


def test_balanced_and_premium_keep_operator_model():
    llm = _llm()
    assert llm.route_call("chat", tier="balanced")["model"] == "gpt-5"
    assert llm.route_call("chat", tier="premium")["model"] == "gpt-5"
    # ...and never leave the configured provider on the auto path.
    assert llm.route_call("chat", tier="premium")["provider"] == "openai"


def test_adaptive_budget_downshift(monkeypatch):
    monkeypatch.setenv("FERAL_LLM_DAILY_BUDGET_USD", "1.0")
    monkeypatch.setenv("FERAL_LLM_DAILY_SPEND_USD", "0.9")  # headroom 0.1 < 0.25
    ref = _llm().route_call("chat", tier="premium", adaptive=True)
    assert ref["tier"] == "balanced"
    assert ref["source"] == "budget_downshift"


def test_adaptive_local_first_claims_cheap_tier():
    cfg = {"local_first": True, "local_model": {"provider": "ollama", "model": "llama3.1"}}
    ref = _llm(cfg).route_call("chat", tier="cheap", adaptive=True)
    assert ref["provider"] == "ollama"
    assert ref["model"] == "llama3.1"
    assert ref["source"] == "local_first"


def test_operator_tier_map_override_still_wins():
    cfg = {"tier_map": {"chat": {"cheap": {"provider": "openrouter", "model": "z/cheap"}}}}
    ref = _llm(cfg).route_call("chat", tier="cheap")
    assert ref["provider"] == "openrouter"
    assert ref["model"] == "z/cheap"


# ─────────────────────────────────────────────────────────────────────
# _candidates_for_route — routed model goes first
# ─────────────────────────────────────────────────────────────────────


def test_candidates_for_route_same_provider_swaps_model_first():
    llm = _llm({"fallback_providers": ["anthropic"]})
    cands = llm._candidates_for_route("openai", "gpt-4o-mini")
    assert cands[0][0] == "openai"
    assert cands[0][1]["model"] == "gpt-4o-mini"
    # configured fallback preserved
    assert any(name == "anthropic" for name, _ in cands)


def test_candidates_for_route_cross_provider_keeps_primary_as_fallback():
    llm = _llm({"fallback_providers": []})
    cands = llm._candidates_for_route("anthropic", "claude-haiku-4-5")
    assert cands[0][0] == "anthropic"
    assert cands[0][1]["model"] == "claude-haiku-4-5"
    # primary openai is retained so a missing anthropic key degrades cleanly
    assert any(name == "openai" and cfg["model"] == "gpt-5" for name, cfg in cands)


# ─────────────────────────────────────────────────────────────────────
# _call_provider — primary path honours the routed model
# ─────────────────────────────────────────────────────────────────────


def test_call_provider_honors_routed_model_on_primary():
    llm = _llm()
    captured: dict = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    async def _post(path, json=None):
        captured["path"] = path
        captured["body"] = json
        return _Resp()

    llm.client = MagicMock()
    llm.client.post = AsyncMock(side_effect=_post)

    out = asyncio.run(llm._call_provider(
        "openai",
        {"model": "gpt-4o-mini", "supported": True},
        [{"role": "user", "content": "hi"}],
        None,
    ))
    assert out["choices"][0]["message"]["content"] == "ok"
    # The routed cheap sibling — NOT self.model ("gpt-5") — hit the wire.
    assert captured["body"]["model"] == "gpt-4o-mini"
