"""The realtime default is a cost decision and should stay deliberate.

Realtime audio is billed per audio token in both directions, and the
full model runs roughly 3x the mini on both. Defaulting to the full
model made every voice turn on a fresh install three times more
expensive than it needed to be, and nothing surfaced that to the
operator.

Two constants have to agree: the settings default, and the proxy
fallback used when the setting is unset. If they drift, a fresh install
pays full price until someone opens Settings, which is exactly the
failure this pins.

The model name is checked against providers/model_catalog.json rather
than against documentation. That file is refreshed from the live
provider endpoint, so it is the only source here that cannot be stale
in the way a doc can.
"""
from __future__ import annotations

import json
from pathlib import Path

from config.loader import DEFAULT_SETTINGS
from voice.realtime_proxy import DEFAULT_MODEL

_CATALOG = Path(__file__).resolve().parents[1] / "providers" / "model_catalog.json"


def _openai_models() -> list[str]:
    data = json.loads(_CATALOG.read_text())
    return list(data["providers"]["openai"]["models"])


def test_settings_default_and_proxy_fallback_agree():
    assert DEFAULT_SETTINGS["audio"]["realtime_model"] == DEFAULT_MODEL, (
        "the proxy fallback must match the settings default, or an install "
        "that never opens Settings runs on a different model than the one "
        "the defaults advertise"
    )


def test_the_default_is_the_mini_tier():
    assert "mini" in DEFAULT_MODEL, (
        "defaulting off the mini tier triples the per-turn audio cost; "
        "change this only with a deliberate reason"
    )


def test_the_default_model_exists_in_the_refreshed_catalog():
    """Guards against a plausible-looking name that no provider serves.

    Documentation and the live endpoint have disagreed on realtime model
    naming, so the catalog wins.
    """
    assert DEFAULT_MODEL in _openai_models(), (
        f"{DEFAULT_MODEL!r} is not in model_catalog.json; a default the "
        f"provider does not serve fails every voice session"
    )


def test_the_full_model_is_still_offered():
    """Downgrading the default must not remove the choice."""
    models = _openai_models()
    assert any(
        m.startswith("gpt-realtime") and "mini" not in m for m in models
    ), "an operator who wants the full model must still be able to pick it"
