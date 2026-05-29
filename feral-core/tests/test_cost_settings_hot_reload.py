"""v2026.5.44 / v2026.5.47 — operator-typed cost caps must take effect
without a brain restart, the flat ``cost.<site>.per_hour_usd`` schema
the Settings UI writes must be honoured by ``CostBudget``, AND when
no operator value exists the call_site is UNLIMITED (the v2026.5.47
"open by default" product change).

These tests pin three regressions:

1. ``cost.screen_loop.per_hour_usd: 20.0`` in settings (flat schema)
   raises the in-memory cap to $20 — without this, the live operator
   who set "$20/hour" in Settings → Cost kept tripping the yellow
   ScreenLoop banner against an unintended cap.
2. When the operator has set nothing, every per-call-site cap is
   ``None`` (unlimited). v2026.5.47 removed the hardcoded factory
   dollar defaults (``screen_loop: $0.10/hr``, ``chat: $5/hr``,
   ``global_per_hour_usd: $5``) that were starving subsystems even
   when the operator never asked for a limit.
3. ``CostBudget.reload_from_settings()`` re-reads the settings file
   in place so a POST to ``/api/config/update`` raises caps without
   a restart.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cost.budget import CostBudget


def _write_settings(home: Path, payload: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(json.dumps(payload), encoding="utf-8")


def test_unset_caps_are_unlimited(tmp_path, monkeypatch):
    """v2026.5.47 — no operator override ⇒ every cap is ``None``
    (unlimited). Prior to v2026.5.47 the factory shipped
    ``screen_loop: $0.10/hr``, ``chat: $5/hr``,
    ``global_per_hour_usd: $5`` so the loop guard would trip the
    yellow banner against a cap the operator never set. The product
    change: the budget is open by default and the operator opts in
    to a number."""
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    bud = CostBudget(db_path=tmp_path / "cost.db")
    for site in (
        "screen_loop", "proactive", "routing", "chat",
        "vision", "embedding", "learner", "compaction",
    ):
        assert bud._cap_for(site, "hour") is None, (
            f"{site} has a factory cap; expected unlimited"
        )
    # Globals are likewise unset by default.
    assert bud._cap_for("__global__", "hour") is None
    assert bud._cap_for("__global__", "day") is None


def test_screen_loop_cap_honors_flat_settings_override(tmp_path, monkeypatch):
    """Flat schema ``cost.screen_loop.per_hour_usd: 20.0`` is honoured.

    Pins the bug fix: prior to v2026.5.44 the loader merged the flat
    key as a sibling of ``per_call_site_caps`` and ``_cap_for`` only
    consulted the nested path, so the operator's $20 cap was silently
    overridden by the factory $0.10 default.
    """
    settings = {
        "cost": {
            "enabled": True,
            "screen_loop": {"per_hour_usd": 20.0},
        }
    }
    bud = CostBudget(settings=settings, db_path=tmp_path / "cost.db")
    assert bud._cap_for("screen_loop", "hour") == pytest.approx(20.0)
    # The 4990-token claude-sonnet estimate that was tripping the
    # default cap (~ $0.075) must now be allowed.
    pricing = bud.pricing
    rates = pricing.lookup("claude-sonnet-4-5")
    est = (5000 / 1000.0) * rates["output"]
    assert est < 20.0
    assert bud.check_and_reserve("screen_loop", "claude-sonnet-4-5", 5000) is True


def test_legacy_nested_schema_still_honoured(tmp_path):
    """Legacy ``cost.per_call_site_caps.<site>.per_hour_usd`` still
    works — existing test fixtures and any installs that wrote
    via the prior UI shape must keep functioning."""
    settings = {
        "cost": {
            "per_call_site_caps": {
                "screen_loop": {"per_hour_usd": 7.5},
            }
        }
    }
    bud = CostBudget(settings=settings, db_path=tmp_path / "cost.db")
    assert bud._cap_for("screen_loop", "hour") == pytest.approx(7.5)


def test_flat_schema_wins_over_legacy_nested(tmp_path):
    """If both shapes are present (mixed-history settings.json),
    the flat operator-facing value wins so the Settings UI is the
    source of truth."""
    settings = {
        "cost": {
            "screen_loop": {"per_hour_usd": 20.0},
            "per_call_site_caps": {
                "screen_loop": {"per_hour_usd": 0.10},
            },
        }
    }
    bud = CostBudget(settings=settings, db_path=tmp_path / "cost.db")
    assert bud._cap_for("screen_loop", "hour") == pytest.approx(20.0)


def test_cost_budget_reloads_after_settings_update(tmp_path, monkeypatch):
    """``reload_from_settings()`` picks up disk-resident settings
    changes without re-instantiating the budget — this is what the
    ``/api/config/update`` route invokes after persisting a
    ``cost.*`` write so a raised cap takes effect mid-process."""
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    _write_settings(
        tmp_path,
        {"cost": {"screen_loop": {"per_hour_usd": 0.10}}},
    )
    bud = CostBudget(
        settings={"cost": {"screen_loop": {"per_hour_usd": 0.10}}},
        db_path=tmp_path / "cost.db",
    )
    assert bud._cap_for("screen_loop", "hour") == pytest.approx(0.10)

    _write_settings(
        tmp_path,
        {"cost": {"screen_loop": {"per_hour_usd": 20.0}}},
    )
    bud.reload_from_settings()
    assert bud._cap_for("screen_loop", "hour") == pytest.approx(20.0)


def test_reload_preserves_in_memory_overrides(tmp_path, monkeypatch):
    """``set_cap`` overrides are operator-authoritative in-memory
    handles (used by tests + admin-CLI). ``reload_from_settings``
    must not erase them."""
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    _write_settings(tmp_path, {"cost": {}})
    bud = CostBudget(db_path=tmp_path / "cost.db")
    bud.set_cap("screen_loop", "hour", 99.0)
    bud.reload_from_settings()
    assert bud._cap_for("screen_loop", "hour") == pytest.approx(99.0)
