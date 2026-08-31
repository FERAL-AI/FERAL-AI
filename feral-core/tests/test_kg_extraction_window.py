"""The KG extraction prompt is sized against the model's context window.

``memory/knowledge_graph.py`` built its extraction prompt with
``f"Text: {text[:2000]}"``. That literal had no derivation behind it and
was wrong in both directions at once.

It threw text away. ``memory/context_builder.py`` segments a transcript
at ``CHUNK_CHARS = 12000`` and hands each segment to
``extract_and_store``, so six sevenths of every carefully sized segment
went into a prompt that read the first 2000 characters. An entity named
late in a segment could not enter the graph at all.

And it did not deliver the safety it looked like it was buying.
Characters are not tokens: measured with ``agents.token_estimate`` over
``tests/fixtures/token_estimate_corpus.json``, 2000 characters is 698
estimated tokens of English prose but 2601 of Chinese and 6000 of
emoji. With the template and the 1024-token reply on top, the emoji case
came to ~7137 tokens against the 4096-token window ``LlamaCppEngine``
pins. The old "safe" cap overflowed a small local model, and raising it
to 8000 or 12000 would have moved eight more scripts into that column.

The bound is a token budget now:

    text budget = window - reply reserve - prompt overhead

These tests hold both halves: text well past character 2000 reaches the
graph, and no script can push the prompt past a 4096-token window.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from agents.token_estimate import estimate_tokens
from memory.context_builder import CHUNK_CHARS
from memory.embeddings import LOCAL_DIM, EmbeddingProvider
from memory.knowledge_graph import (
    MAX_EXTRACTION_CHARS,
    KnowledgeGraph,
    _EXTRACTION_OVERHEAD_TOKENS,
    _EXTRACTION_PROMPT_TEMPLATE,
    _EXTRACTION_REPLY_TOKENS,
    _LOCAL_CHAT_WRAPPER,
    _extraction_text_budget_tokens,
    fit_extraction_text,
)

# The window LlamaCppEngine actually pins. Imported rather than restated
# so a change to the engine reaches this test.
from agents.local_inference import LLAMA_CPP_N_CTX

_CORPUS = json.loads(
    (Path(__file__).parent / "fixtures" / "token_estimate_corpus.json").read_text()
)

# What the extraction prompt cost before this change, for the assertion
# messages below.
_OLD_CAP_CHARS = 2000


def _force_hash_provider(ep: EmbeddingProvider) -> None:
    ep._provider = "hash"
    ep._dim = LOCAL_DIM
    ep._model = None


@pytest.fixture
def kg():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    with patch.object(EmbeddingProvider, "_detect_provider", _force_hash_provider):
        embedder = EmbeddingProvider()
    yield KnowledgeGraph(path, embedder)
    try:
        os.unlink(path)
    except OSError:
        pass


class _RecordingLLM:
    """Captures the prompt and answers with triples for names it can see.

    The point of reading the prompt rather than the original text is
    that it is the only thing the model gets. A name the extractor
    truncated away is a name no real LLM could return either.
    """

    available = True

    def __init__(self, window_tokens: int = LLAMA_CPP_N_CTX, names=()):
        self.context_window_tokens = window_tokens
        self.prompts: list[str] = []
        self._names = tuple(names)

    async def chat(self, messages, tools=None, temperature=0.7, max_tokens=1024):
        prompt = messages[0]["content"]
        self.prompts.append(prompt)
        triples = [
            {
                "subject": name,
                "subject_type": "person",
                "predicate": "mentioned_in",
                "object": "the transcript",
                "object_type": "thing",
            }
            for name in self._names
            if name in prompt
        ]
        return {"choices": [{"message": {"content": json.dumps(triples)}}]}

    def extract_response(self, data):
        return data["choices"][0]["message"]["content"], []


def _prompt_tokens(prompt: str) -> int:
    """Total window cost of one extraction call."""
    return (
        estimate_tokens(prompt)
        + estimate_tokens(_LOCAL_CHAT_WRAPPER)
        + _EXTRACTION_REPLY_TOKENS
    )


def _text_of(case: dict, length: int) -> str:
    unit = case["unit"]
    return (unit * (length // max(len(unit), 1) + 2))[:length]


# ─────────────────────────────────────────────────────────────────────
# The text that used to be dropped now reaches the graph
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_entity_past_the_old_cap_reaches_the_graph(kg):
    """An entity 6000 characters in was invisible to the graph forever.

    Fails against the unfixed extractor: ``text[:2000]`` cuts before
    ``Zanzibar`` is ever mentioned, so the model never sees the name and
    no relation is stored for it.
    """
    filler = "The team discussed the quarterly roadmap at length. "
    text = (
        "Marcus Chen chaired the meeting. "
        + filler * 115
        + "Priya Raman signed off on the Zanzibar launch. "
        + filler * 5
    )
    assert len(text) > 6000, "fixture must reach past the old cap"
    assert len(text) < MAX_EXTRACTION_CHARS, "fixture must not hit the character bound"
    late_index = text.index("Priya Raman")
    assert late_index > _OLD_CAP_CHARS * 2, (
        f"the late entity sits at character {late_index}; it must be well "
        f"past the old {_OLD_CAP_CHARS}-character cap for this test to mean anything"
    )

    llm = _RecordingLLM(names=("Marcus Chen", "Priya Raman"))
    stored = await kg.extract_and_store(text, llm)

    subjects = {rel["source"] for rel in stored}
    assert "Marcus Chen" in subjects, "the entity inside the old cap regressed"
    assert "Priya Raman" in subjects, (
        f"an entity at character {late_index} of a {len(text)}-character "
        f"segment never reached the model. The prompt carried "
        f"{len(llm.prompts[0])} characters; the old [:{_OLD_CAP_CHARS}] cap "
        f"made everything past character {_OLD_CAP_CHARS} permanently "
        f"invisible to the knowledge graph."
    )

    # And it is really in the graph, not just in the return value.
    neighbours = await kg.traverse("Priya Raman", max_depth=1)
    assert neighbours, "the late entity was returned but never stored"


@pytest.mark.asyncio
async def test_a_full_context_builder_segment_is_carried_whole(kg):
    """The largest bounded caller's segment must not be truncated.

    ``context_builder`` sizes segments at ``CHUNK_CHARS`` and calls
    ``extract_and_store`` once per segment. On a large window the
    extractor must carry the whole segment.
    """
    segment = "Ada Lovelace wrote the first algorithm. " * (CHUNK_CHARS // 39)
    segment = segment[:CHUNK_CHARS]
    llm = _RecordingLLM(window_tokens=128_000, names=("Ada Lovelace",))

    await kg.extract_and_store(segment, llm)

    carried = llm.prompts[0]
    assert segment in carried, (
        f"a {len(segment)}-character segment, exactly the size "
        f"context_builder produces, did not survive into the prompt "
        f"({len(carried)} characters total)"
    )


def test_english_prose_gains_text_even_on_the_smallest_window():
    """The 4096-token local model is the tightest real configuration."""
    case = next(c for c in _CORPUS["cases"] if c["name"] == "english_prose")
    raw = _text_of(case, MAX_EXTRACTION_CHARS)

    class _Small:
        context_window_tokens = LLAMA_CPP_N_CTX

    fitted = fit_extraction_text(raw, _Small())
    assert len(fitted) > _OLD_CAP_CHARS * 3, (
        f"on a {LLAMA_CPP_N_CTX}-token window English prose fits "
        f"{len(fitted)} characters; the old cap allowed {_OLD_CAP_CHARS}, "
        "and the budget arithmetic says several times that should fit"
    )


# ─────────────────────────────────────────────────────────────────────
# A small-context model cannot be blown up. This is the risk traded
# against, and the old cap did not cover it either.
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("case", _CORPUS["cases"], ids=lambda c: c["name"])
def test_prompt_fits_a_4096_token_window_for_every_script(case):
    """No script may push the extraction prompt past a 4096 window.

    Fails against the unfixed extractor on the emoji case: 2000
    characters of emoji is ~6000 estimated tokens, so the prompt plus
    its 1024-token reply reserve came to ~7137 against a 4096 window.
    """
    class _Small:
        context_window_tokens = LLAMA_CPP_N_CTX

    # Far more text than any caller sends, so the bound is what holds.
    raw = _text_of(case, 60_000)
    fitted = fit_extraction_text(raw, _Small())
    total = _prompt_tokens(_EXTRACTION_PROMPT_TEMPLATE.format(text=fitted))

    assert total <= LLAMA_CPP_N_CTX, (
        f"{case['name']}: the extraction prompt costs {total} estimated "
        f"tokens against a {LLAMA_CPP_N_CTX}-token window "
        f"({len(fitted)} characters of text, "
        f"{_EXTRACTION_OVERHEAD_TOKENS} overhead, "
        f"{_EXTRACTION_REPLY_TOKENS} reserved for the reply). A local "
        "model would refuse this request."
    )


@pytest.mark.asyncio
async def test_the_real_extraction_call_fits_the_small_window(kg):
    """End to end, not just the helper: the prompt actually sent fits."""
    case = next(c for c in _CORPUS["cases"] if c["name"] == "emoji")
    text = _text_of(case, 60_000)
    llm = _RecordingLLM(window_tokens=LLAMA_CPP_N_CTX)

    await kg.extract_and_store(text, llm)

    total = _prompt_tokens(llm.prompts[0])
    assert total <= LLAMA_CPP_N_CTX, (
        f"extract_and_store sent a prompt costing {total} estimated tokens "
        f"to a {LLAMA_CPP_N_CTX}-token model"
    )


def test_emoji_is_cut_harder_than_the_old_cap_allowed():
    """The safety half of the fix, stated as a regression guard.

    A token budget must give the densest script LESS than the flat
    character cap did, or it is not buying any safety.
    """
    case = next(c for c in _CORPUS["cases"] if c["name"] == "emoji")

    class _Small:
        context_window_tokens = LLAMA_CPP_N_CTX

    fitted = fit_extraction_text(_text_of(case, 60_000), _Small())
    assert len(fitted) < _OLD_CAP_CHARS, (
        f"emoji-dense text still fits {len(fitted)} characters, no tighter "
        f"than the old {_OLD_CAP_CHARS}-character cap that overflowed a "
        f"{LLAMA_CPP_N_CTX}-token window"
    )


def test_budget_shrinks_with_the_configured_window():
    """The bound is derived from the window, not a constant."""
    class _Window:
        def __init__(self, tokens):
            self.context_window_tokens = tokens

    small = _extraction_text_budget_tokens(_Window(LLAMA_CPP_N_CTX))
    large = _extraction_text_budget_tokens(_Window(128_000))
    assert small < large, (
        f"a {LLAMA_CPP_N_CTX}-token model got the same budget ({small}) as "
        f"a 128000-token one ({large}); the bound is not derived from the "
        "window"
    )
    assert small == LLAMA_CPP_N_CTX - _EXTRACTION_REPLY_TOKENS - _EXTRACTION_OVERHEAD_TOKENS


# ``patch.dict`` rather than ``monkeypatch.setenv``: conftest's autouse
# ``restore_process_env`` is torn down BEFORE monkeypatch's undo here (an
# earlier autouse fixture already requested monkeypatch, so monkeypatch
# is set up first and finalised last), and it reports the still-set
# variable as a leak. A ``with`` block restores inside the test body.


def test_budget_falls_back_to_the_configured_window():
    """An llm that reports no window uses FERAL_CONTEXT_WINDOW_TOKENS."""
    with patch.dict(os.environ, {"FERAL_CONTEXT_WINDOW_TOKENS": "8192"}):
        assert _extraction_text_budget_tokens(None) == (
            8192 - _EXTRACTION_REPLY_TOKENS - _EXTRACTION_OVERHEAD_TOKENS
        )


def test_a_tiny_window_still_extracts_something():
    """A misconfigured window must not produce an empty prompt."""
    with patch.dict(os.environ, {"FERAL_CONTEXT_WINDOW_TOKENS": "512"}):
        fitted = fit_extraction_text("Grace Hopper worked at Harvard. " * 400, None)
    assert fitted, "a small window produced an extraction prompt with no text"
    assert "Grace Hopper" in fitted


# ─────────────────────────────────────────────────────────────────────
# The bounds cannot silently drift
# ─────────────────────────────────────────────────────────────────────


def test_character_bound_matches_the_largest_caller():
    """MAX_EXTRACTION_CHARS tracks context_builder.CHUNK_CHARS.

    The character bound is a cost bound, chosen as the largest size any
    caller passes so that nothing a caller sends is truncated by it. If
    ``CHUNK_CHARS`` is raised and this is not, the truncation this
    change removed comes straight back for the segments in between.
    """
    assert MAX_EXTRACTION_CHARS == CHUNK_CHARS, (
        f"context_builder segments at {CHUNK_CHARS} characters but the "
        f"extractor carries at most {MAX_EXTRACTION_CHARS}; "
        f"{CHUNK_CHARS - MAX_EXTRACTION_CHARS} characters of every "
        "segment would be dropped"
    )


def test_prompt_overhead_is_measured_from_the_real_template():
    """The overhead constant must match the strings actually sent."""
    expected = estimate_tokens(
        _EXTRACTION_PROMPT_TEMPLATE.format(text="") + _LOCAL_CHAT_WRAPPER
    )
    assert _EXTRACTION_OVERHEAD_TOKENS == expected


def test_over_budget_text_keeps_its_tail():
    """Leading-N truncation is the one choice this repo rules out.

    ``context_builder.PER_MESSAGE_HARD_CAP`` cites arXiv 2210.16732 for
    it: salience is anti-correlated with position near a leading cut.
    """
    text = "HEADMARKER. " + ("filler sentence about nothing. " * 4000) + " TAILMARKER."

    class _Small:
        context_window_tokens = LLAMA_CPP_N_CTX

    fitted = fit_extraction_text(text, _Small())
    assert "HEADMARKER" in fitted, "the head was dropped"
    assert "TAILMARKER" in fitted, (
        "the tail was dropped; leading-N truncation loses the salient end"
    )


def test_llama_cpp_engine_reports_the_window_it_loads():
    """The router can only derive a bound if the engine publishes one."""
    from agents.local_inference import LlamaCppEngine

    engine = LlamaCppEngine("some-model.gguf")
    assert engine.context_window_tokens == LLAMA_CPP_N_CTX
