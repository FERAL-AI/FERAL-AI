"""OpenRouter vision-capability regression tests.

The v2026.5.0 terminal log showed the log line
``Provider 'openrouter' does not support vision input`` firing 10+ times
per session. Root cause: ``OpenRouterProvider._capabilities`` omitted
``"vision"`` even though OpenRouter is a router whose underlying model
can be vision-capable. These tests pin the  fix:

* The adapter's superset capability advertises vision (router default).
* ``_capabilities_for_model(model_id)`` narrows per-route using the
  live catalog's modality data (from ``/api/v1/models``).
* The ``LLMProvider`` dispatcher does NOT early-return the vision
  error for openrouter; it trusts the router-level default, and when
  a route-specific capability lookup exists and narrows the answer, it
  emits a targeted "this route doesn't accept images" error instead.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from providers.catalog import get_shared_catalog, reset_shared_catalog
from providers.openrouter_provider import OpenRouterProvider


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _reset_catalog() -> None:
    reset_shared_catalog()
    yield
    reset_shared_catalog()


def _load_openrouter_fixture() -> dict:
    return json.loads((FIXTURES / "openrouter_models.json").read_text())


# ---------------------------------------------------------------------------
# Adapter-level
# ---------------------------------------------------------------------------


def test_openrouter_adapter_advertises_vision_in_superset() -> None:
    """The router-level ``_capabilities`` MUST include ``"vision"``.

    This is the exact regression the v2026.5.0 log showed: the old
    adapter returned False from ``supports("vision")`` for every
    request, which triggered the dispatcher's early return.
    """
    adapter = OpenRouterProvider(api_key="or-test")
    assert adapter.supports("vision") is True
    assert "vision" in adapter._capabilities


def test_openrouter_capabilities_for_model_defaults_to_superset() -> None:
    """Before a live refresh lands, per-model queries return the superset.

    Without a snapshot we have to trust the router (vision yes, tools
    yes, streaming yes). Otherwise we'd regress on first-boot hosts.
    """
    adapter = OpenRouterProvider(api_key="or-test")
    caps = adapter._capabilities_for_model("anthropic/claude-opus-4-7")
    assert "vision" in caps
    assert "tool_calling" in caps


def test_openrouter_capabilities_for_model_narrows_from_fixture() -> None:
    adapter = OpenRouterProvider(api_key="or-test")
    data = _load_openrouter_fixture()
    # Populate the per-model capability bag the same way refresh_models
    # does, so we can assert the narrowing logic without hitting the
    # network.
    from providers.openrouter_provider import _extract_capabilities
    adapter._model_caps = {
        entry["id"]: _extract_capabilities(entry) for entry in data["data"]
    }

    vision_ok = adapter._capabilities_for_model("anthropic/claude-opus-4-7")
    assert "vision" in vision_ok

    text_only = adapter._capabilities_for_model("deepseek/deepseek-v4-pro")
    assert "vision" not in text_only, (
        "DeepSeek V4 Pro is text-only on OR; per-route narrowing must "
        "drop the vision capability so the orchestrator can surface a "
        "targeted 'try a vision route' error"
    )


def test_openrouter_vision_modality_parsing_variants() -> None:
    """Both legacy ``architecture.modality`` + new ``input_modalities`` parse."""
    from providers.openrouter_provider import _extract_capabilities
    legacy = _extract_capabilities(
        {"architecture": {"modality": "text+image"}, "supported_parameters": ["tools"]}
    )
    assert legacy["vision"] is True
    modern = _extract_capabilities(
        {"architecture": {"input_modalities": ["text", "image"]}, "supported_parameters": []}
    )
    assert modern["vision"] is True
    text_only = _extract_capabilities(
        {"architecture": {"modality": "text"}, "supported_parameters": []}
    )
    assert text_only["vision"] is False


# ---------------------------------------------------------------------------
# Dispatcher-level — no more "does not support vision" early-return for OR.
# ---------------------------------------------------------------------------


def _make_llm_provider(model: str):
    # Import here so the env is set up before the module loads.
    import importlib

    mod = importlib.import_module("agents.llm_provider")
    provider = mod.LLMProvider.__new__(mod.LLMProvider)
    provider.provider = "openrouter"
    provider.model = model
    provider.base_url = "https://openrouter.ai/api/v1"
    provider.api_key = "or-test"
    provider.client = None
    provider.available = True
    provider._hybrid_cloud_provider = None
    provider._local_engine = None
    provider._config = {}
    provider._cooldown = type("C", (), {"should_probe": lambda _s, _p: True,
                                         "record_success": lambda *_a, **_k: None,
                                         "record_failure": lambda *_a, **_k: None,
                                         "is_available": lambda *_a, **_k: True,
                                         "_cooldowns": {}})()
    return provider


def test_dispatcher_does_not_early_return_vision_for_openrouter() -> None:
    """When no per-model snapshot exists, vision is allowed to pass.

    Before  the dispatcher's ``_vision_support_status`` returned
    ``(False, "Provider 'openrouter' does not support vision input.")``
    on every openrouter call; that blocked legitimate image requests.
    After  the default is ``(True, "")``.
    """
    provider = _make_llm_provider("anthropic/claude-opus-4-7")
    ok, reason = provider._vision_support_status()
    assert ok is True, reason


def test_dispatcher_narrows_to_route_when_catalog_knows_modality() -> None:
    """When the shared catalog has a narrowing snapshot, the dispatcher
    returns a targeted error pointing at vision-capable alternatives."""
    catalog = get_shared_catalog()
    adapter = catalog.get_adapter("openrouter")
    assert adapter is not None
    # Seed the per-model cap bag the way refresh_models would.
    from providers.openrouter_provider import _extract_capabilities
    data = _load_openrouter_fixture()
    adapter._model_caps = {
        e["id"]: _extract_capabilities(e) for e in data["data"]
    }

    text_only = _make_llm_provider("deepseek/deepseek-v4-pro")
    ok, reason = text_only._vision_support_status()
    assert ok is False
    assert "vision" in reason.lower()
    # The error nudges toward vision-capable routes so the user can fix
    # the mistake without reading logs.
    assert (
        "claude-opus-4-7" in reason
        or "gemini" in reason
        or "gpt-5.5" in reason
    )

    vision_ok = _make_llm_provider("anthropic/claude-opus-4-7")
    ok, _ = vision_ok._vision_support_status()
    assert ok is True
