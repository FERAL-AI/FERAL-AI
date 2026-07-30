"""Anthropic thinking / sampling capability comes from the catalog.

The bug this pins
-----------------
``AnthropicProvider`` used to carry two hand-maintained literals::

    _ADAPTIVE_THINKING_MODELS = frozenset({"claude-opus-4-7"})
    _EXTENDED_THINKING_MODELS = frozenset({...})

The adaptive set is what decides whether the adapter may send
``temperature``. ``temperature`` / ``top_p`` / ``top_k`` were REMOVED on
Claude 4.7 and every later model — each returns HTTP 400. So adding the
Claude 5 ids to the model list *without also remembering to extend a
frozenset in a different file* would have 400'd every Claude 5 call that
carried a temperature. Nothing in the type system or the test suite
connected the two edits.

Both sets are now derived from the catalog's ``capabilities`` block,
which ``scripts/research_providers.py`` refreshes from Anthropic's live
``GET /v1/models`` (it returns ``thinking.types`` per model). These
tests assert the derivation, the 400-avoidance it exists for, and the
conservative defaults for ids the catalog has never seen.
"""

from __future__ import annotations

import pytest

from agents.llm_reasoning import apply_reasoning_fork
from providers.anthropic_provider import AnthropicProvider

#: Every Claude id that rejects sampling params (4.7 and later).
SAMPLING_REJECTED = [
    "claude-fable-5",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
]

#: Claude ids that still accept them. Opus 4.6 / Sonnet 4.6 are the
#: interesting case: they support adaptive thinking AND sampling params,
#: so "adaptive implies no sampling" is not a safe shorthand for them.
SAMPLING_ACCEPTED = [
    "claude-sonnet-4-6",
    "claude-opus-4-6",
    "claude-haiku-4-5",
    "claude-haiku-4-5-20251001",
]


@pytest.fixture
def provider() -> AnthropicProvider:
    return AnthropicProvider(api_key="sk-test")


# ── Derivation ───────────────────────────────────────────────────────


def test_adaptive_set_is_derived_not_hardcoded() -> None:
    """The adaptive set must contain the whole 4.7+ line, not one entry."""
    adaptive = AnthropicProvider.adaptive_thinking_models()
    assert "claude-opus-4-7" in adaptive
    missing = sorted(m for m in SAMPLING_REJECTED if m not in adaptive)
    assert not missing, (
        f"adaptive-thinking models missing from the catalog-derived set: "
        f"{missing}. If this set were still the hardcoded "
        'frozenset({"claude-opus-4-7"}), every one of these would send '
        "temperature and 400."
    )


def test_extended_set_is_derived_not_hardcoded() -> None:
    extended = AnthropicProvider.extended_thinking_models()
    assert "claude-haiku-4-5" in extended
    assert "claude-sonnet-4-6" in extended
    # Claude 5 uses adaptive thinking only — budget_tokens is gone.
    assert "claude-opus-5" not in extended
    assert "claude-fable-5" not in extended


# ── The 400 this exists to prevent ───────────────────────────────────


@pytest.mark.parametrize("model", SAMPLING_REJECTED)
def test_sampling_params_rejected_models(provider, model) -> None:
    assert provider.supports_sampling_params(model) is False, (
        f"{model} accepts sampling params per the catalog, but "
        "temperature/top_p/top_k return HTTP 400 on Claude 4.7 and later."
    )


@pytest.mark.parametrize("model", SAMPLING_ACCEPTED)
def test_sampling_params_accepted_models(provider, model) -> None:
    assert provider.supports_sampling_params(model) is True, (
        f"{model} should still accept sampling params; stripping them "
        "silently discards a caller's temperature."
    )


@pytest.mark.parametrize("model", SAMPLING_REJECTED)
def test_reasoning_fork_strips_all_three_sampling_params(model) -> None:
    """The wire body must carry none of the three on a 4.7+ model.

    The earlier fork dropped only ``temperature``, so a caller passing
    ``top_p`` still 400'd.
    """
    body = apply_reasoning_fork(
        "anthropic",
        model,
        {"model": model, "temperature": 0.7, "top_p": 0.9, "top_k": 40},
    )
    leaked = sorted(k for k in ("temperature", "top_p", "top_k") if k in body)
    assert not leaked, f"{model} would 400: body still carries {leaked}"


@pytest.mark.parametrize("model", SAMPLING_REJECTED)
def test_no_explicit_thinking_block_for_adaptive_models(model) -> None:
    """Adaptive models 400 on ``thinking={"type": "enabled"}``."""
    body = apply_reasoning_fork("anthropic", model, {"model": model})
    assert body.get("thinking") is None or body["thinking"].get("type") != "enabled"


# ── Conservative defaults for unknown ids ────────────────────────────


def test_unknown_model_keeps_sampling_params(provider) -> None:
    """A model released after the last catalog refresh must not lose
    the caller's temperature — default to the historical behaviour."""
    assert provider.supports_sampling_params("claude-something-unreleased") is True


def test_live_capability_flags_win_over_catalog(provider) -> None:
    """``refresh_models`` populates ``_thinking_caps`` from the live
    ``/v1/models`` response; those flags outrank the bundled catalog so
    a same-day model launch is handled before the catalog refreshes."""
    provider._thinking_caps["claude-haiku-4-5"] = {
        "thinking_enabled": False,
        "thinking_adaptive": True,
    }
    assert provider.supports_adaptive_thinking("claude-haiku-4-5") is True
    assert provider.supports_extended_thinking("claude-haiku-4-5") is False
    # No explicit catalog `sampling_params` record for that override, so
    # the adaptive proxy applies.
    fresh = AnthropicProvider(api_key="sk-test")
    fresh._thinking_caps["claude-brand-new"] = {"thinking_adaptive": True}
    assert fresh.supports_sampling_params("claude-brand-new") is False


# ── Model list is catalog-backed ─────────────────────────────────────


def test_bundled_models_come_from_the_catalog(provider) -> None:
    models = provider.list_models()
    assert "claude-opus-5" in models
    assert "claude-fable-5" in models
    # Retired ids must not linger in the bundled fallback list.
    for retired in (
        "claude-opus-4-20250514",
        "claude-sonnet-4-20250514",
        "claude-3-7-sonnet-20250219",
        "claude-3-5-haiku-20241022",
    ):
        assert retired not in models, f"{retired} is retired but still listed"


def test_claude_5_ids_carry_no_date_suffix(provider) -> None:
    """Since the 4.6 generation the bare id IS the pinned snapshot;
    appending a date 404s."""
    for model in provider.list_models():
        if model.endswith("-5") or "-5-2" in model:
            assert not model.startswith(("claude-opus-5-2", "claude-sonnet-5-2")), (
                f"{model} carries a date suffix — Claude 5 ids are dateless"
            )
