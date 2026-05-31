"""``LLMProvider.apply_preset`` validates Ollama model presets against
the local ``/api/tags`` set.

Operator report: applying an Ollama preset that hardcodes a model name
(``ollama_vision`` -> ``llava``) produced ``Switched LLM to ollama/llava
(available=True)`` immediately followed by a 404 on the first chat
turn when ``llava`` was not pulled locally.

Fix: ``apply_preset`` consults ``/api/tags`` and, when the requested
model is not pulled, falls through to ``switch_provider``'s auto-detect
path so the brain lands on an installed model instead. The response
carries a ``warning`` describing the substitution.
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
    assert llm.model == "llava"
    await llm.close()


@pytest.mark.asyncio
async def test_apply_preset_falls_back_when_model_not_pulled():
    llm = _llm_with_openai_env()
    with patch.object(
        LLMProvider, "_ollama_pulled_models",
        new=AsyncMock(return_value={"mistral", "mistral:7b"}),
    ):
        with patch.object(
            LLMProvider, "_detect_ollama", return_value="mistral",
        ):
            result = await llm.apply_preset("ollama_vision")
    assert result["ok"] is True
    assert llm.provider == "ollama"
    # Auto-detect substituted the installed model instead of the
    # guaranteed-404 ``llava`` literal.
    assert llm.model == "mistral"
    assert "warning" in result
    assert "llava" in result["warning"]
    assert "ollama pull llava" in result["warning"]
    await llm.close()


@pytest.mark.asyncio
async def test_apply_preset_unreachable_ollama_preserves_request():
    llm = _llm_with_openai_env()
    with patch.object(
        LLMProvider, "_ollama_pulled_models",
        new=AsyncMock(return_value=None),  # Ollama unreachable
    ):
        result = await llm.apply_preset("ollama_vision")
    # With no signal from the server, apply_preset keeps the requested
    # model so the brain's own probe ladder can surface the outage
    # rather than silently rewriting the operator's intent.
    assert result["ok"] is True
    assert llm.provider == "ollama"
    assert llm.model == "llava"
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
