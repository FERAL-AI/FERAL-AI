"""Tests for perception.scene — SceneAnalyzer multi-provider VLM pipeline."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from perception.scene import SceneAnalyzer, SCENE_ANALYSIS_PROMPT, TEXT_EXTRACTION_PROMPT


@pytest.fixture()
def llm():
    m = MagicMock()
    m.available = True
    m.chat = AsyncMock(return_value={"choices": [{"message": {"content": ""}}]})
    m.extract_response = MagicMock(return_value=("", []))
    return m


@pytest.fixture()
def analyzer(llm, monkeypatch):
    monkeypatch.delenv("FERAL_VLM_PROVIDER", raising=False)
    return SceneAnalyzer(llm=llm)


# ── Init with different VLM providers ────────────────────────────

def test_init_default_uses_shared_llm(analyzer, llm):
    assert analyzer._vlm_client is None
    assert analyzer.available is True


def test_init_ollama_provider(monkeypatch):
    monkeypatch.setenv("FERAL_VLM_PROVIDER", "ollama")
    monkeypatch.setenv("FERAL_VLM_MODEL", "llava")
    with patch("httpx.AsyncClient"):
        sa = SceneAnalyzer()
    assert sa._vlm_client is not None
    assert sa._vlm_client["type"] == "ollama"


# ── Mode-based prompt selection ──────────────────────────────────

def test_select_prompt_general(analyzer):
    prompt = analyzer._select_prompt("general", "node-1", "")
    assert prompt == SCENE_ANALYSIS_PROMPT


def test_select_prompt_ocr(analyzer):
    prompt = analyzer._select_prompt("ocr", "node-1", "")
    assert prompt == TEXT_EXTRACTION_PROMPT


def test_select_prompt_query(analyzer):
    prompt = analyzer._select_prompt("query", "node-1", "What color is the car?")
    assert "What color is the car?" in prompt


def test_select_prompt_tracking_uses_cache(analyzer):
    analyzer._cache["node-1"] = {"scene_description": "A park with dogs."}
    prompt = analyzer._select_prompt("tracking", "node-1", "")
    assert "A park with dogs." in prompt


# ── JSON parsing ─────────────────────────────────────────────────

def test_parse_json_with_fences(analyzer):
    raw = '```json\n{"scene_description": "office"}\n```'
    result = analyzer._parse_json(raw)
    assert result == {"scene_description": "office"}


def test_parse_json_plain(analyzer):
    result = analyzer._parse_json('{"people_count": 3}')
    assert result["people_count"] == 3


def test_parse_json_invalid(analyzer):
    assert analyzer._parse_json("not json at all") is None


# ── Prose salvage (regression: vision went silent on ollama/moondream) ──
#
# The install switched ``vision.provider`` to ollama/moondream on
# 2026-08-01. moondream answers SCENE_ANALYSIS_PROMPT with a paragraph
# of prose no matter how firmly the prompt demands JSON, so _parse_json
# returned None, analyze_frame returned None, and ScreenLoop recorded
# nothing. 9,513 screen episodes stopped dead; the only trace was a
# debug-level "VLM returned non-JSON scene description".

MOONDREAM_PROSE = (
    "The image shows a computer screen displaying the terminal window of a "
    "Unix-based operating system, with several commands visible."
)


async def test_prose_reply_is_salvaged_as_scene_description(analyzer, llm):
    llm.extract_response.return_value = (MOONDREAM_PROSE, [])

    result = await analyzer.analyze_frame("AAAA==", node_id="n1", force=True)

    assert result is not None, "a usable caption must not be thrown away"
    assert result["scene_description"] == MOONDREAM_PROSE
    assert result["prose_fallback"] is True
    # Structured extras genuinely aren't available from prose, so they
    # come back empty rather than invented.
    assert result["detected_objects"] == []
    assert result["text_in_scene"] == []


async def test_prose_salvage_warns_once_per_model(analyzer, llm, caplog):
    llm.extract_response.return_value = (MOONDREAM_PROSE, [])

    with caplog.at_level("WARNING", logger="feral.scene"):
        await analyzer.analyze_frame("AAAA==", node_id="n1", force=True)
        await analyzer.analyze_frame("BBBB==", node_id="n2", force=True)

    warnings = [r for r in caplog.records if "does not honour the JSON contract" in r.message]
    assert len(warnings) == 1, "the JSON-contract warning must not spam every cooldown"


async def test_prose_salvage_result_is_cached_and_historied(analyzer, llm):
    llm.extract_response.return_value = (MOONDREAM_PROSE, [])
    await analyzer.analyze_frame("AAAA==", node_id="n1", force=True)

    assert analyzer.get_cached("n1")["scene_description"] == MOONDREAM_PROSE
    assert len(analyzer.get_history("n1")) == 1


async def test_query_mode_prose_populates_answer(analyzer, llm):
    llm.extract_response.return_value = (MOONDREAM_PROSE, [])

    result = await analyzer.analyze_frame(
        "AAAA==", node_id="n1", force=True, mode="query", query="what is on screen?",
    )

    # _analyze_scene_background reads "answer" first in query mode; if it
    # is missing the reply reaches nobody.
    assert result["answer"] == MOONDREAM_PROSE


async def test_malformed_json_is_not_salvaged_as_prose(analyzer, llm):
    # A truncated JSON object was MEANT to be structured. Salvaging it
    # would put raw braces into episodic memory as a "description".
    llm.extract_response.return_value = ('{"scene_description": "office", "detec', [])

    result = await analyzer.analyze_frame("AAAA==", node_id="n1", force=True)

    assert result is None


async def test_tiny_reply_is_not_salvaged_as_prose(analyzer, llm):
    llm.extract_response.return_value = ("ok", [])

    assert await analyzer.analyze_frame("AAAA==", node_id="n1", force=True) is None


# ── Silent-failure visibility ────────────────────────────────────────


async def test_empty_vlm_reply_logs_a_warning(analyzer, llm, caplog):
    # The shared LLM's failover chain exhausting on 401s returns "" and
    # does not raise. That path used to `return None` with no log at all,
    # which is how vision stayed dead while every log line looked fine.
    llm.extract_response.return_value = ("", [])

    with caplog.at_level("WARNING", logger="feral.scene"):
        result = await analyzer.analyze_frame("AAAA==", node_id="n1", force=True)

    assert result is None
    assert any("empty reply" in r.message for r in caplog.records)


async def test_unusable_reply_logs_a_warning(analyzer, llm, caplog):
    llm.extract_response.return_value = ("{{{", [])

    with caplog.at_level("WARNING", logger="feral.scene"):
        result = await analyzer.analyze_frame("AAAA==", node_id="n1", force=True)

    assert result is None
    assert any("neither JSON nor usable prose" in r.message for r in caplog.records)


# ── Cooldown enforcement ─────────────────────────────────────────

async def test_cooldown_returns_cached(analyzer, llm):
    scene_json = '{"scene_description": "desk with monitor"}'
    llm.extract_response.return_value = (scene_json, [])

    result = await analyzer.analyze_frame("AAAA==", node_id="n1", force=True)
    assert result is not None

    llm.chat.reset_mock()
    cached = await analyzer.analyze_frame("BBBB==", node_id="n1", force=False)
    llm.chat.assert_not_awaited()
    assert cached == result


# ── History tracking ─────────────────────────────────────────────

def test_push_history_respects_max(analyzer):
    for i in range(10):
        analyzer._push_history("n1", {"scene_description": f"scene-{i}"})
    assert len(analyzer.get_history("n1")) == analyzer._max_history


# ── analyze_with_history ─────────────────────────────────────────

async def test_analyze_with_history_multi_frame(analyzer, llm):
    llm.extract_response.return_value = ('{"activity_summary":"walking"}', [])
    frames = [{"data_b64": "AAAA==", "encoding": "jpeg"} for _ in range(3)]
    result = await analyzer.analyze_with_history(frames, node_id="n1")
    assert result == {"activity_summary": "walking"}
