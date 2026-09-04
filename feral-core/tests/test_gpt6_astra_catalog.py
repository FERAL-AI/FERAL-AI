"""gpt-6-astra is known to the catalog, classified and routed correctly.

OpenAI released GPT-6 Astra on 2026-09-03 (model id ``gpt-6-astra``).
Its model page lists it as a reasoning model with function calling on
both Chat Completions and Responses, 1,050,000 context, no Realtime.

What the 5.6 line taught us, and what these tests pin: a new model id
that falls through every regex classifies as ``unknown``, which skips the
reasoning fork, keeps ``max_tokens`` in the body, and is a guaranteed 400
the first time an operator picks it. And a reasoning model that reaches
``/chat/completions`` with tools plus ``reasoning_effort`` is a 400 too.
So the id has to be a known reasoning model AND routed to Responses
before anyone can select it.

The default stays gpt-5.6-sol on purpose. ``default_model_for`` picks
the top recommended tier, and astra costs twice as much per token; it
must not become the silent default the day it lands on an account.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from providers.model_classes import classify, classify_endpoint, is_responses_only  # noqa: E402
from providers.recommended import recommended_for  # noqa: E402


def test_astra_is_a_reasoning_model():
    assert classify("openai", "gpt-6-astra") == "reasoning"
    # Dated snapshots and future tier suffixes must not fall back to unknown.
    assert classify("openai", "gpt-6-astra-2026-09-03") == "reasoning"
    assert classify("openai", "gpt-6-astra-mini") == "reasoning"
    # An invented gpt-6 id is still unknown: the classifier must never
    # guess that a model it has not seen is a reasoning model.
    assert classify("openai", "gpt-6-hyperthinking-2027-01-01") == "unknown"
    assert classify_endpoint("openai", "gpt-6-hyperthinking-2027-01-01") == "chat_completions"


def test_astra_routes_to_responses_so_tools_and_reasoning_coexist():
    assert is_responses_only("openai", "gpt-6-astra")
    assert classify_endpoint("openai", "gpt-6-astra") == "responses"
    # Through OpenRouter the same upstream rule applies.
    assert classify_endpoint("openrouter", "openai/gpt-6-astra") == "responses"


def test_astra_is_in_the_bundled_catalog_with_the_published_rates():
    catalog = json.loads((ROOT / "providers" / "model_catalog.json").read_text())
    openai = catalog["providers"]["openai"]
    assert "gpt-6-astra" in openai["models"]
    price = openai["pricing"]["gpt-6-astra"]
    # developers.openai.com/api/docs/models/gpt-6-astra, 2026-09-04:
    # $10 in, $50 out, $1 cached, per 1M tokens.
    assert price["input"] == 0.01
    assert price["output"] == 0.05
    assert price["cache_read"] == 0.001
    assert price["context_window"] == 1_050_000


def test_astra_is_recommended_but_not_the_default():
    live = ["babbage-002", "gpt-5.6-sol", "gpt-6-astra", "gpt-5.6-terra", "gpt-4.1"]
    pick = recommended_for("openai", live)
    assert "gpt-6-astra" in pick
    assert pick[0] == "gpt-5.6-sol", (
        "gpt-6-astra costs twice gpt-5.6-sol; it must not become the silent default"
    )
    assert pick.index("gpt-6-astra") == 1


def test_an_account_without_astra_is_unaffected():
    live = ["gpt-5.6-sol", "gpt-5.6-terra"]
    assert recommended_for("openai", live)[0] == "gpt-5.6-sol"
    assert "gpt-6-astra" not in recommended_for("openai", live)
