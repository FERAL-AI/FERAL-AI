"""Anthropic prompt-cache tokens have to reach the cost budget.

The hole this covers: Anthropic reports ``cache_creation_input_tokens``
(written to the cache) and ``cache_read_input_tokens`` (served from it)
as SIBLINGS of ``input_tokens``, never inside it. Both were dropped, so
a cache-heavy turn under-counted spend and a configured cost cap tripped
later than it should. They could not simply be added to the prompt count
either: a cache read costs 0.1x the base input rate, so that would have
over-charged by 10x.

What is pinned here:

1. ``cost/pricing.py`` carries a cache_write / cache_read rate for every
   priced Anthropic model, and each one matches Anthropic's published
   multipliers (5m write = 1.25x base input, read = 0.1x).
2. The token -> dollar arithmetic is exact and hand-checkable.
3. The Anthropic NON-stream route bills cache tokens.
4. The Anthropic STREAM route bills cache tokens.
5. (3) and (4) produce the SAME dollar figure for the same usage block.
6. A model with no published cache rate still bills input/output
   normally, leaves its cache tokens unbilled, and does not crash.
7. ``_budget_record`` stays best-effort: a pricing blow-up loses the
   cache surcharge, not the chat turn.

Scaffolding (SSE mock, ``_RecordSpy``, the Anthropic provider factory)
is imported from ``test_stream_usage_billing`` rather than re-declared.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio

from agents.llm_provider import LLMProvider
from cost.pricing import (
    ModelPricing,
    cache_equivalent_prompt_tokens,
    compute_token_cost,
    get_shared_pricing,
)

from tests.test_stream_usage_billing import (  # reuse, do not duplicate
    _RecordSpy,
    _make_anthropic_provider,
    _patch_anthropic_httpx,
)


# ─────────────────────────────────────────────────────────────────────
# One usage block, used by every billing assertion below so the
# stream / non-stream comparison is genuinely the same input.
#
# Hand arithmetic for claude-opus-4-8 ($/1k: in .005, out .025,
# cache_write .00625, cache_read .0005):
#
#     input        1_000 / 1000 * 0.005    = $0.005
#     output         400 / 1000 * 0.025    = $0.010
#     cache write  8_000 / 1000 * 0.00625  = $0.050
#     cache read  20_000 / 1000 * 0.0005   = $0.010
#                                           ─────────
#     total                                  $0.075
#
# Without the cache tokens the same turn bills $0.015, so the fix is
# worth 5x on this shape.
# ─────────────────────────────────────────────────────────────────────

CACHE_MODEL = "claude-opus-4-8"
USAGE = {
    "input_tokens": 1000,
    "output_tokens": 400,
    "cache_creation_input_tokens": 8000,
    "cache_read_input_tokens": 20000,
}
EXPECTED_DOLLARS = 0.075
BASE_ONLY_DOLLARS = 0.015          # what the old code billed
# 8_000 * 1.25 + 20_000 * 0.1 = 12_000 base-input-rate tokens.
EXPECTED_EQUIVALENT_TOKENS = 12000

ANTHROPIC_CACHE_SSE = [
    'data: {"type":"message_start","message":{"model":"claude-opus-4-8",'
    '"usage":{"input_tokens":1000,"output_tokens":1,'
    '"cache_creation_input_tokens":8000,"cache_read_input_tokens":20000}}}',
    'data: {"type":"content_block_start","content_block":{"type":"text"}}',
    'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"ok"}}',
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
    '"usage":{"output_tokens":400}}',
    'data: {"type":"message_stop"}',
]


@pytest_asyncio.fixture
async def budget(tmp_path):
    """Real ``CostBudget`` on an isolated SQLite file. The cap is set well
    above every figure asserted here so ``record_usage`` records rather
    than raising ``BudgetExceeded`` mid-assertion."""
    from cost.budget import CostBudget

    b = CostBudget(
        settings={"cost": {"enabled": True, "chat": {"per_hour_usd": 5.0}}},
        db_path=str(tmp_path / "cost.db"),
    )
    try:
        await b.ensure_ready()
        yield b
    finally:
        await b.close()


def _anthropic_post(payload: dict) -> AsyncMock:
    resp = MagicMock(spec=httpx.Response)
    resp.raise_for_status = MagicMock()
    resp.json.return_value = payload
    return AsyncMock(return_value=resp)


# ─────────────────────────────────────────────────────────────────────
# 1. The rates themselves
# ─────────────────────────────────────────────────────────────────────


def test_every_priced_anthropic_model_has_published_cache_rates():
    """Anthropic's published multipliers, applied to the base input rate,
    must reproduce the catalog's cache_write / cache_read exactly. This
    is what makes the numbers verifiable rather than plausible.

    Source: https://platform.claude.com/docs/en/about-claude/pricing
    5-minute cache write = 1.25x base input, cache read = 0.1x.
    (The 1h write rate, 2x, is deliberately not carried: FERAL never
    sends ``cache_control.ttl``, so it cannot incur a 1h write.)
    """
    from cost.pricing import _CATALOG_PATH

    catalog = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    priced = catalog["providers"]["anthropic"]["pricing"]
    models = [m for m in priced if not m.startswith("_")]
    assert models, "no Anthropic pricing entries in the catalog"

    for model in models:
        rates = priced[model]
        base = rates["input"]
        assert rates["cache_write"] == pytest.approx(base * 1.25, rel=1e-9), model
        assert rates["cache_read"] == pytest.approx(base * 0.10, rel=1e-9), model


def test_lookup_surfaces_cache_rates_and_fallback_omits_them():
    pricing = get_shared_pricing()
    rates = pricing.lookup(CACHE_MODEL)
    assert rates["input"] == 0.005
    assert rates["output"] == 0.025
    assert rates["cache_write"] == 0.00625
    assert rates["cache_read"] == 0.0005

    # A model with no published cache rate must expose NO cache keys, so
    # "absent" stays distinguishable from "free".
    plain = pricing.lookup("gpt-5.4")
    assert "cache_write" not in plain
    assert "cache_read" not in plain

    # Same for a model the catalog has never heard of.
    unknown = pricing.lookup("no-such-model-anywhere")
    assert "cache_write" not in unknown
    assert "cache_read" not in unknown


# ─────────────────────────────────────────────────────────────────────
# 2. The arithmetic
# ─────────────────────────────────────────────────────────────────────


def test_compute_token_cost_matches_hand_arithmetic():
    dollars, rates = compute_token_cost(
        get_shared_pricing(),
        CACHE_MODEL,
        prompt_tokens=USAGE["input_tokens"],
        completion_tokens=USAGE["output_tokens"],
        cache_write_tokens=USAGE["cache_creation_input_tokens"],
        cache_read_tokens=USAGE["cache_read_input_tokens"],
    )
    # 0.005 + 0.010 + 0.050 + 0.010
    assert dollars == pytest.approx(EXPECTED_DOLLARS, rel=1e-9)
    assert rates["cache_write"] == 0.00625

    # Drop the cache tokens and you get the old, wrong figure.
    base_only, _ = compute_token_cost(
        get_shared_pricing(),
        CACHE_MODEL,
        prompt_tokens=USAGE["input_tokens"],
        completion_tokens=USAGE["output_tokens"],
    )
    assert base_only == pytest.approx(BASE_ONLY_DOLLARS, rel=1e-9)


def test_cache_equivalent_prompt_tokens_is_dollar_exact():
    """The equivalence is what lets a prompt-token-only ledger bill cache
    tokens correctly: 8k writes @1.25x + 20k reads @0.1x cost the same
    as 12k plain input tokens."""
    equivalent = cache_equivalent_prompt_tokens(
        CACHE_MODEL,
        USAGE["cache_creation_input_tokens"],
        USAGE["cache_read_input_tokens"],
    )
    assert equivalent == EXPECTED_EQUIVALENT_TOKENS

    folded, _ = compute_token_cost(
        get_shared_pricing(),
        CACHE_MODEL,
        prompt_tokens=USAGE["input_tokens"] + equivalent,
        completion_tokens=USAGE["output_tokens"],
    )
    assert folded == pytest.approx(EXPECTED_DOLLARS, rel=1e-9)


def test_cache_equivalent_is_zero_without_published_rates():
    # Priced model, no cache rates published.
    assert cache_equivalent_prompt_tokens("gpt-5.4", 8000, 20000) == 0
    # Unknown model (fallback rates, no cache rates).
    assert cache_equivalent_prompt_tokens("no-such-model-anywhere", 8000, 20000) == 0
    # No cache tokens at all.
    assert cache_equivalent_prompt_tokens(CACHE_MODEL, 0, 0) == 0


def test_cache_equivalent_never_raises_on_a_broken_pricing_table():
    class _Exploding(ModelPricing):
        def lookup(self, model):  # type: ignore[override]
            raise RuntimeError("catalog on fire")

    assert cache_equivalent_prompt_tokens(
        CACHE_MODEL, 8000, 20000, pricing=_Exploding(),
    ) == 0


def test_extract_cache_usage_reads_both_fields():
    assert LLMProvider._extract_cache_usage({"usage": USAGE}) == (8000, 20000)
    # Providers that do not report them, and junk, degrade to zero.
    assert LLMProvider._extract_cache_usage({"usage": {"input_tokens": 5}}) == (0, 0)
    assert LLMProvider._extract_cache_usage({}) == (0, 0)
    assert LLMProvider._extract_cache_usage(None) == (0, 0)
    assert LLMProvider._extract_cache_usage(
        {"usage": {"cache_read_input_tokens": "junk"}},
    ) == (0, 0)


# ─────────────────────────────────────────────────────────────────────
# 3 + 4 + 5. Both routes, and their agreement
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_non_stream_anthropic_bills_cache_tokens(budget):
    llm = _make_anthropic_provider()
    llm.model = CACHE_MODEL
    llm.client = MagicMock()
    llm.client.post = _anthropic_post({
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": USAGE,
    })
    llm.set_cost_budget(budget)

    result = await llm._chat_anthropic(
        [{"role": "user", "content": "x"}],
        tools=None, temperature=0.5, max_tokens=128,
    )
    # The usage block now survives normalisation (it used to be dropped,
    # which is why a non-streamed Anthropic turn billed nothing at all).
    assert result["usage"] == USAGE
    assert LLMProvider._extract_usage(result) == (1000, 400, 0)
    assert LLMProvider._extract_cache_usage(result) == (8000, 20000)

    await llm._budget_record("chat", CACHE_MODEL, result)
    assert budget.current_spend("chat", "hour") == pytest.approx(
        EXPECTED_DOLLARS, rel=1e-9,
    )


@pytest.mark.asyncio
async def test_stream_anthropic_bills_cache_tokens(budget):
    llm = _make_anthropic_provider()
    llm.model = CACHE_MODEL
    llm.set_cost_budget(budget)

    with _patch_anthropic_httpx(ANTHROPIC_CACHE_SSE):
        async for _ in llm._chat_stream_anthropic(
            [{"role": "user", "content": "x"}], call_site="chat",
        ):
            pass

    assert budget.current_spend("chat", "hour") == pytest.approx(
        EXPECTED_DOLLARS, rel=1e-9,
    )


@pytest.mark.asyncio
async def test_stream_and_non_stream_agree_on_the_same_usage_block():
    """A turn must cost the same whether or not it streamed. Both routes
    are driven with the identical four usage numbers and their
    ``_budget_record`` payloads are compared field by field."""
    stream_spy = _RecordSpy()
    llm_stream = _make_anthropic_provider()
    llm_stream.model = CACHE_MODEL
    llm_stream._budget_record = stream_spy  # type: ignore[method-assign]

    with _patch_anthropic_httpx(ANTHROPIC_CACHE_SSE):
        async for _ in llm_stream._chat_stream_anthropic(
            [{"role": "user", "content": "x"}], call_site="chat",
        ):
            pass

    nonstream_spy = _RecordSpy()
    llm_plain = _make_anthropic_provider()
    llm_plain.model = CACHE_MODEL
    llm_plain.client = MagicMock()
    llm_plain.client.post = _anthropic_post({
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": USAGE,
    })
    llm_plain._budget_record = nonstream_spy  # type: ignore[method-assign]
    plain_result = await llm_plain._chat_anthropic(
        [{"role": "user", "content": "x"}],
        tools=None, temperature=0.5, max_tokens=128,
    )
    await nonstream_spy("chat", CACHE_MODEL, plain_result)

    assert len(stream_spy.calls) == 1
    streamed = stream_spy.calls[0][2]
    plain = nonstream_spy.calls[0][2]

    assert LLMProvider._extract_usage(streamed) == LLMProvider._extract_usage(plain)
    assert (
        LLMProvider._extract_cache_usage(streamed)
        == LLMProvider._extract_cache_usage(plain)
        == (8000, 20000)
    )
    # And therefore the same money, computed the same way.
    for payload in (streamed, plain):
        prompt, completion, reasoning = LLMProvider._extract_usage(payload)
        write, read = LLMProvider._extract_cache_usage(payload)
        dollars, _ = compute_token_cost(
            get_shared_pricing(), CACHE_MODEL,
            prompt, completion, reasoning,
            cache_write_tokens=write, cache_read_tokens=read,
        )
        assert dollars == pytest.approx(EXPECTED_DOLLARS, rel=1e-9)


# ─────────────────────────────────────────────────────────────────────
# 6 + 7. Degradation
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_model_without_cache_rates_still_bills_input_and_output(budget):
    """gpt-5.4 is priced ($0.0025 / $0.015 per 1k) but has no published
    cache rate. Its cache tokens must be left unbilled rather than
    guessed at, and the turn must not crash."""
    llm = _make_anthropic_provider()
    llm.set_cost_budget(budget)

    await llm._budget_record("chat", "gpt-5.4", {"usage": USAGE})

    # 1000/1000*0.0025 + 400/1000*0.015 = 0.0025 + 0.006 = 0.0085
    assert budget.current_spend("chat", "hour") == pytest.approx(0.0085, rel=1e-9)


@pytest.mark.asyncio
async def test_budget_record_survives_a_pricing_lookup_blowing_up(budget):
    """Best-effort contract: if the cache conversion raises, the turn
    still bills its base tokens and never propagates."""
    llm = _make_anthropic_provider()
    llm.set_cost_budget(budget)

    with patch(
        "cost.pricing.cache_equivalent_prompt_tokens",
        side_effect=RuntimeError("pricing exploded"),
    ):
        await llm._budget_record("chat", CACHE_MODEL, {"usage": USAGE})

    # The surcharge is lost, the turn is not: base tokens still landed.
    assert budget.current_spend("chat", "hour") == pytest.approx(
        BASE_ONLY_DOLLARS, rel=1e-9,
    )
