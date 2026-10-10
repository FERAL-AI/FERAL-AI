"""F-16 regression: vision binding must not construct or switch LLMProvider.

The initialized shared runtime owns inference, budget and wire adaptation.
Missing credentials and broken configuration have distinct redacted refusals.
"""
from __future__ import annotations

import inspect
from unittest.mock import Mock

from agents.llm_provider import LLMProvider
from tests.test_agentic_cu_selected_vision import wired as _wired_fixture

wired = _wired_fixture


def test_llmprovider_takes_no_configuration_kwargs():
    assert set(inspect.signature(LLMProvider.__init__).parameters) == {"self"}


async def test_a_vlm_is_bound_when_a_matching_key_is_present(wired):
    vlm = await wired.skill._get_vlm({"OPENAI_API_KEY": "inert-test-key"})
    assert vlm.owner is wired.llm and vlm.api_key == "inert-test-key"
    assert wired.calls == wired.clients == []
    wired.constructor.assert_not_called()
    wired.switch.assert_not_awaited()


async def test_env_overrides_bind_exact_provider_without_switch(wired, monkeypatch):
    monkeypatch.setenv("FERAL_VLM_PROVIDER", "anthropic")
    monkeypatch.setenv("FERAL_VLM_MODEL", "claude-opus-4-7")
    vlm = await wired.skill._get_vlm({"ANTHROPIC_API_KEY": "inert-ant-key"})
    assert (vlm.provider, vlm.model, vlm.api_key) == ("anthropic", "claude-opus-4-7", "inert-ant-key")
    assert wired.calls == wired.clients == []


async def test_no_key_is_explicit_missing_credentials_without_warning(wired, caplog):
    wired.llm.api_key = ""
    result = await wired.skill._get_vlm({})
    assert result["error_code"] == "vision_credentials_unavailable"
    assert not caplog.text and wired.calls == []


async def test_broken_configuration_is_distinct_and_redacted(wired, monkeypatch, caplog):
    monkeypatch.setattr(wired.config, "get", Mock(side_effect=RuntimeError("private-config-canary")))
    result = await wired.skill._get_vlm({"OPENAI_API_KEY": "inert-key"})
    assert result["error_code"] == "vision_binding_unavailable"
    assert "RuntimeError" in caplog.text and "private-config-canary" not in repr(result) + caplog.text
    assert wired.calls == wired.clients == []


async def test_wrong_internal_signature_is_not_missing_credentials(wired, monkeypatch, caplog):
    monkeypatch.setattr(wired.config, "get", Mock(side_effect=TypeError("private-wrong-signature")))
    result = await wired.skill._get_vlm({"OPENAI_API_KEY": "inert-key"})
    assert result["error_code"] == "vision_binding_unavailable"
    assert "TypeError" in caplog.text and "private-wrong-signature" not in repr(result) + caplog.text
