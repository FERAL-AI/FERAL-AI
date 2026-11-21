"""``LLMProvider.apply_preset`` validates Ollama model presets against
the local ``/api/tags`` set.

Operator report: applying an Ollama preset that hardcodes a model name
(``ollama_vision`` -> ``llava``) produced ``Switched LLM to ollama/llava
(available=True)`` immediately followed by a 404 on the first chat
turn when ``llava`` was not pulled locally.

Vision application requires an installed matching tag and the existing
capability classification before switching. Missing or unknown inventory
refuses without substituting a text model. Text presets retain auto-detection.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

from agents.llm_provider import LLMProvider


def _llm_with_openai_env():
    """Construct an LLMProvider that boots cleanly without touching
    Ollama — the apply_preset tests below explicitly switch into
    ollama. Mirrors the shape of the existing apply_preset tests."""
    env = {
        "FERAL_LLM_PROVIDER": "openai",
        "OPENAI_API_KEY": "sk-test",
    }
    with patch.dict(os.environ, env, clear=False):
        with patch.object(LLMProvider, "_detect_ollama", return_value=None):
            return LLMProvider()


@pytest.mark.asyncio
async def test_apply_preset_keeps_model_when_pulled():
    llm = _llm_with_openai_env()
    with patch.object(
        LLMProvider, "_ollama_pulled_models",
        new=AsyncMock(return_value={"llava", "llava:7b"}),
    ):
        result = await llm.apply_preset("ollama_vision")
    assert result["ok"] is True
    assert "warning" not in result
    assert llm.provider == "ollama"
    assert llm.model == "llava:7b"
    await llm.close()


@pytest.mark.asyncio
async def test_apply_preset_refuses_when_vision_model_not_pulled():
    llm = _llm_with_openai_env()
    original = (llm.provider, llm.model)
    with patch.object(
        LLMProvider, "_ollama_pulled_models",
        new=AsyncMock(return_value={"mistral", "mistral:7b"}),
    ):
        with patch.object(
            LLMProvider, "_detect_ollama", return_value="mistral",
        ):
            result = await llm.apply_preset("ollama_vision")
    assert result["ok"] is False
    assert result["error_code"] == "vision_model_not_installed"
    assert (llm.provider, llm.model) == original
    await llm.close()


@pytest.mark.asyncio
async def test_apply_preset_unreachable_ollama_preserves_current_selection():
    llm = _llm_with_openai_env()
    original = (llm.provider, llm.model)
    with patch.object(
        LLMProvider, "_ollama_pulled_models",
        new=AsyncMock(return_value=None),  # Ollama unreachable
    ):
        result = await llm.apply_preset("ollama_vision")
    assert result["ok"] is False
    assert result["error_code"] == "vision_model_unverified"
    assert (llm.provider, llm.model) == original
    assert "warning" not in result
    await llm.close()


@pytest.mark.asyncio
async def test_apply_preset_text_preset_skips_validation():
    """``ollama_text`` has model='' so switch_provider auto-detects.
    The /api/tags probe should NOT run for that path — there's nothing
    to validate."""
    llm = _llm_with_openai_env()
    sentinel = AsyncMock(return_value=set())
    with patch.object(LLMProvider, "_ollama_pulled_models", new=sentinel):
        with patch.object(
            LLMProvider, "_detect_ollama", return_value="llama3.1",
        ):
            result = await llm.apply_preset("ollama_text")
    assert result["ok"] is True
    sentinel.assert_not_called()
    assert llm.provider == "ollama"
    assert llm.model == "llama3.1"
    await llm.close()
