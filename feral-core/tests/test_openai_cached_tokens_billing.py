"""OpenAI's cached prompt tokens must not be billed at the full input rate.

Anthropic reports cache tokens ALONGSIDE ``input_tokens``; OpenAI
reports them INSIDE it, as ``usage.prompt_tokens_details.cached_tokens``
on /chat/completions and ``usage.input_tokens_details.cached_tokens`` on
/v1/responses. ``_extract_usage`` read neither, and the Responses
adapter passes the provider's usage block through untouched, so on the
audited install (``llm.provider=openai``, ``llm.model=gpt-5.6-sol``)
every cached token was charged as a fresh input token.

``model_catalog.json`` prices gpt-5.6-sol cache reads at 0.0005 against
an input rate of 0.005, a tenth. A turn that reads 8k tokens from the
cache was therefore billing the hourly cap for 8k input tokens instead
of 800 cache-equivalent ones, which is how a $10/hour cap arrived early.

Because OpenAI's count is a SUBSET of the input total, the fix has to
subtract before it re-adds. Getting that wrong in the other direction
double-counts.
"""

from __future__ import annotations

import pytest

from agents.llm_provider import LLMProvider
from cost.pricing import cache_equivalent_prompt_tokens

MODEL = "gpt-5.6-sol"


class _Budget:
    """Captures what ``_budget_record`` hands CostBudget."""

    def __init__(self):
        self.calls = []

    async def record_usage(
        self, *, call_site, model, prompt_tokens, completion_tokens,
        reasoning_tokens=0,
    ):
        self.calls.append({
            "call_site": call_site,
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "reasoning_tokens": reasoning_tokens,
        })


def _provider(budget):
    llm = LLMProvider.__new__(LLMProvider)
    llm._cost_budget = budget
    return llm


RESPONSES_USAGE = {
    "usage": {
        "input_tokens": 10_000,
        "output_tokens": 200,
        "input_tokens_details": {"cached_tokens": 8_000},
    }
}

CHAT_USAGE = {
    "usage": {
        "prompt_tokens": 10_000,
        "completion_tokens": 200,
        "prompt_tokens_details": {"cached_tokens": 8_000},
    }
}

ANTHROPIC_USAGE = {
    "usage": {
        "input_tokens": 1_000,
        "output_tokens": 400,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 20_000,
    }
}


class TestExtraction:
    @pytest.mark.parametrize("payload", [RESPONSES_USAGE, CHAT_USAGE])
    def test_both_openai_spellings_are_read(self, payload):
        assert LLMProvider._extract_inclusive_cached_tokens(payload) == 8_000

    def test_anthropics_shape_reports_nothing_inclusive(self):
        """Anthropic's cache tokens are siblings of the input total.
        Treating them as inclusive would subtract them twice."""
        assert LLMProvider._extract_inclusive_cached_tokens(ANTHROPIC_USAGE) == 0
        assert LLMProvider._extract_cache_usage(ANTHROPIC_USAGE) == (0, 20_000)

    @pytest.mark.parametrize("payload", [
        {}, {"usage": None}, {"usage": {}},
        {"usage": {"input_tokens_details": None}},
        {"usage": {"input_tokens_details": {"cached_tokens": "lots"}}},
        "not a dict",
    ])
    def test_junk_reports_zero(self, payload):
        assert LLMProvider._extract_inclusive_cached_tokens(payload) == 0


class TestBilling:
    # 8_000 cached at a tenth of the input rate = 800 base-rate tokens.
    EXPECTED_EQUIVALENT = 800

    def test_the_catalog_still_prices_a_cache_read_at_a_tenth(self):
        assert cache_equivalent_prompt_tokens(MODEL, 0, 8_000) == self.EXPECTED_EQUIVALENT

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [RESPONSES_USAGE, CHAT_USAGE])
    async def test_cached_tokens_are_subtracted_then_rebilled_at_cache_rate(
        self, payload,
    ):
        budget = _Budget()
        await _provider(budget)._budget_record("chat", MODEL, payload)

        assert len(budget.calls) == 1
        billed = budget.calls[0]["prompt_tokens"]
        # 10_000 total - 8_000 cached + 800 cache-equivalent.
        assert billed == 2_000 + self.EXPECTED_EQUIVALENT
        # The defect billed the raw input total.
        assert billed < 10_000
        assert budget.calls[0]["completion_tokens"] == 200

    @pytest.mark.asyncio
    async def test_a_turn_with_no_cache_is_billed_exactly_as_before(self):
        budget = _Budget()
        await _provider(budget)._budget_record("chat", MODEL, {
            "usage": {"input_tokens": 500, "output_tokens": 60},
        })
        assert budget.calls[0]["prompt_tokens"] == 500

    @pytest.mark.asyncio
    async def test_anthropic_billing_is_unchanged(self):
        """Its cache tokens are added, never subtracted."""
        budget = _Budget()
        await _provider(budget)._budget_record("chat", "claude-opus-4-8", ANTHROPIC_USAGE)
        expected = 1_000 + cache_equivalent_prompt_tokens("claude-opus-4-8", 0, 20_000)
        assert budget.calls[0]["prompt_tokens"] == expected

    @pytest.mark.asyncio
    async def test_a_cached_count_larger_than_the_input_total_cannot_go_negative(self):
        budget = _Budget()
        await _provider(budget)._budget_record("chat", MODEL, {
            "usage": {
                "input_tokens": 100,
                "output_tokens": 10,
                "input_tokens_details": {"cached_tokens": 5_000},
            },
        })
        assert budget.calls[0]["prompt_tokens"] >= 0


class TestPromptCacheKey:
    def test_responses_bodies_carry_a_stable_routing_key(self):
        """Without it OpenAI is free to route consecutive turns to
        different machines, and a shared prefix never hits the cache."""
        from agents.llm_provider import _PROMPT_CACHE_KEY

        llm = LLMProvider.__new__(LLMProvider)
        llm.model = MODEL
        first = llm._build_responses_body(
            [{"role": "user", "content": "one"}], None, 1.0, 256, stream=False,
        )
        second = llm._build_responses_body(
            [{"role": "user", "content": "two"}], None, 1.0, 256, stream=False,
        )
        assert first["prompt_cache_key"] == _PROMPT_CACHE_KEY
        assert second["prompt_cache_key"] == first["prompt_cache_key"]
        assert first["prompt_cache_key"]
