"""F-13 — token budgets must not under-count non-Latin and code-heavy content.

Every budget in the brain used ``len(str(content)) // 4``, which is calibrated
for English prose and nothing else. Measured against the real tokenizers
(``cl100k_base`` and ``o200k_base``, worse of the two, because the router talks
to 16 providers):

    Chinese   1080 chars = 1260 real tokens,  //4 said  270   (0.21x)
    emoji      800 chars = 2200 real tokens,  //4 said  200   (0.09x)
    Hebrew    1600 chars = 1651 real tokens,  //4 said  400   (0.24x)
    JSON      1180 chars =  540 real tokens,  //4 said  295   (0.55x)

So a Chinese conversation was measured at a fifth of its real size and an
emoji-heavy one at a ninth, and the pruner concluded it was comfortably inside
a window it was five times over.

**Scope: nine sites in five files, not the one line the audit cites.**

    agents/context_engine.py:188,192,197   context window budget  (cited: 197)
    agents/llm_provider.py:3951            USD budget, candidate routing
    agents/learner.py:171,229,230          cost ledger via _cost_guard.record
    agents/proactive_engine.py:612         cost ledger via _cost_guard.record
    memory/context_builder.py:42           DEFERRED, another lane owns the file

The four cost-ledger sites are the same defect with a different consequence:
spend against a USD budget is under-reported by the same factor rather than a
request being refused.

The corpus lives in ``tests/fixtures/token_estimate_corpus.json`` with its real
token counts recorded, so this file proves the property without needing
tiktoken. ``tiktoken`` is not in ``requirements.lock`` (it arrives only as a
transitive of ``langchain-openai`` on some dev machines), so the live
cross-check below skips in CI rather than pretending to run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.token_estimate import estimate_message_tokens, estimate_tokens


FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "token_estimate_corpus.json"
)
CORPUS = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
CASES = [(c["name"], c) for c in CORPUS["cases"]]


def _text(case: dict) -> str:
    return case["unit"] * case["repeat"]


@pytest.mark.parametrize("name,case", CASES, ids=[n for n, _ in CASES])
def test_estimate_never_falls_below_the_real_token_count(name: str, case: dict):
    """The property that matters. Under-counting is a refused request."""
    estimate = estimate_tokens(_text(case))
    assert estimate >= case["real_tokens"], (
        f"{name}: estimated {estimate} for {case['real_tokens']} real tokens "
        f"({estimate / case['real_tokens']:.2f}x). {case['why']}"
    )


@pytest.mark.parametrize("name,case", CASES, ids=[n for n, _ in CASES])
def test_the_old_estimator_is_recorded_as_wrong(name: str, case: dict):
    """Pins the defect itself, so the corpus cannot quietly become trivial."""
    assert case["chars_div_4"] == len(_text(case)) // 4


def test_the_corpus_actually_contains_the_hard_cases():
    """A corpus of English prose would let any estimator pass."""
    by_name = {c["name"]: c for _, c in CASES}
    for required in ("chinese", "emoji", "json_blob", "hebrew", "base64"):
        assert required in by_name, f"corpus lost its {required} case"
    # Every one of these was under-counted by more than 2x before the fix.
    for name in ("chinese", "emoji", "hebrew"):
        case = by_name[name]
        assert case["chars_div_4"] * 2 < case["real_tokens"], (
            f"{name} no longer demonstrates the defect"
        )


def test_estimate_does_not_over_count_english_absurdly():
    """Over-counting is the safe direction but it is not free.

    An estimator that returned a huge number would pass every test above and
    summarise every conversation on its second turn.
    """
    by_name = {c["name"]: c for _, c in CASES}
    for name in ("english_prose", "python_code", "json_blob"):
        case = by_name[name]
        estimate = estimate_tokens(_text(case))
        assert estimate <= case["real_tokens"] * 2.5, (
            f"{name}: {estimate} is {estimate / case['real_tokens']:.1f}x the "
            f"real count; that prunes context that did not need pruning"
        )


def test_edge_cases():
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) > 0  # str(None) is "None", four characters
    assert estimate_tokens(1234) > 0
    assert estimate_tokens([{"type": "text", "text": "hi"}]) > 0
    assert estimate_message_tokens([]) == 0
    assert estimate_message_tokens([{"content": "hello"}]) > 0
    assert estimate_message_tokens([{"role": "user"}]) == 0


def test_recorded_token_counts_match_live_tiktoken():
    """Keeps the fixture honest wherever a real tokenizer is available.

    Skips in CI: tiktoken is not in requirements.lock. Saying that out loud is
    better than a corpus nobody ever re-derives.
    """
    tiktoken = pytest.importorskip("tiktoken")
    for name, case in CASES:
        text = _text(case)
        for encoding_name, recorded in case["tokens"].items():
            live = len(tiktoken.get_encoding(encoding_name).encode(text))
            assert live == recorded, (
                f"{name}/{encoding_name}: recorded {recorded}, tiktoken now "
                f"says {live}. Regenerate the corpus and re-check the weights "
                f"in agents/token_estimate.py."
            )


# ── the call sites ───────────────────────────────────────────────


def test_context_engine_uses_the_shared_estimator():
    """The cited defect: the context-window budget."""
    source = (
        Path(__file__).resolve().parents[1] / "agents" / "context_engine.py"
    ).read_text(encoding="utf-8")
    # Comments are skipped: the fix quotes the old expression to explain itself.
    offenders = [
        line.strip()
        for line in source.splitlines()
        if "// 4" in line and not line.strip().startswith("#")
    ]
    assert not offenders, (
        "agents/context_engine.py still divides characters by 4 for a token "
        f"budget: {offenders}"
    )
    assert "token_estimate" in source, (
        "agents/context_engine.py does not use the shared estimator"
    )


def test_cost_budget_sites_use_the_shared_estimator():
    """Same estimator, money instead of a context window."""
    root = Path(__file__).resolve().parents[1]
    for relative in (
        "agents/llm_provider.py",
        "agents/learner.py",
        "agents/proactive_engine.py",
    ):
        source = (root / relative).read_text(encoding="utf-8")
        assert "token_estimate" in source, (
            f"{relative} prices calls with its own character heuristic; "
            f"spend is under-reported by the same factor the corpus measures"
        )


def test_context_engine_prunes_a_chinese_conversation_it_used_to_keep():
    """End to end, on the real class: `_prune_to_budget`.

    Twelve messages of 200 Chinese characters is about 2800 real tokens. The
    old estimator made that 600, so a 1000-token budget looked satisfied and
    nothing was pruned.
    """
    from agents.context_engine import DefaultContextEngine

    messages = [{"role": "user", "content": "这是一个关于人工智能的技术文档。" * 13}
                for _ in range(12)]
    characters = sum(len(m["content"]) for m in messages)
    assert characters // 4 < 1000, "the fixture no longer reproduces the defect"

    kept = DefaultContextEngine._prune_to_budget(messages, 1000)
    assert len(kept) < len(messages), (
        "nothing was pruned for a 1000-token budget that the conversation is "
        "several times over; the old estimator saw "
        f"{characters // 4} tokens where there are "
        f"{estimate_message_tokens(messages)}"
    )
    assert estimate_message_tokens(kept) <= 1000 or len(kept) == 2, (
        "pruning stopped above budget without hitting the two-message floor"
    )
