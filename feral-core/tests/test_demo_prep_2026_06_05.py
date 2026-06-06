"""Demo prep 2026-06-05 — pins for the three live-debug fixes.

* ``ProviderCooldownTracker.clear`` drops the in-memory + on-disk
  cooldown ledger so a re-credited provider gets retried on the
  very next chat turn.
* ``perception/fusion.update_sensors`` prefers fresh wearable HR /
  SpO2 over a stale Apple HealthKit reading so the brain context
  matches the WebUI dashboard.

Both tests run standalone — they avoid importing ``feral-core``'s
heavy ``conftest.py`` (which boots the full brain in fixtures and
hangs in CI). To keep the runner fast we monkeypatch the persist
hook in the cooldown test instead of writing to a tempfile, and
in the fusion test we instantiate ``PerceptionEngine`` directly.
"""
from __future__ import annotations

import time


def test_cooldown_tracker_clear_forgets_parked_provider(tmp_path):
    from agents.llm_failover import FailoverReason, ProviderCooldownTracker

    state_path = tmp_path / "cooldowns.json"
    tracker = ProviderCooldownTracker(storage_path=str(state_path))

    tracker.record_failure("anthropic", FailoverReason.BILLING)
    tracker.record_failure("openrouter", FailoverReason.AUTH)
    assert "anthropic" in tracker.cooldown_state()
    assert "openrouter" in tracker.cooldown_state()

    cleared = tracker.clear(provider="anthropic")
    assert cleared == ["anthropic"]
    assert "anthropic" not in tracker.cooldown_state()
    assert "openrouter" in tracker.cooldown_state()

    cleared_all = tracker.clear()
    assert "openrouter" in cleared_all
    assert tracker.cooldown_state() == {}


def test_fresh_wearable_hr_wins_over_stale_healthkit():
    """Live wearable HR ingested *after* a stale HealthKit sample
    must take precedence — that's the contract that BUG 2's fix
    relied on. Regression here would put the iOS Context tab back
    on a 19 871 572 s-old HealthKit reading.
    """
    from perception.fusion import PerceptionEngine

    engine = PerceptionEngine()
    now = time.time()
    stale_ts = now - 60_000  # ~17 h old

    engine.update_sensors("session_a", {
        "ppg_heart_rate": 115,
        "heart_rate_source": "apple_healthkit",
        "heart_rate_sample_ts": stale_ts,
    })

    engine.update_sensors("session_a", {
        "ppg_heart_rate": 72,
        "heart_rate_source": "veepoo_wristband",
        "heart_rate_sample_ts": now,
    })

    frame = engine.get_frame("session_a")
    assert frame is not None
    assert frame.heart_rate == 72
    assert frame.heart_rate_source == "veepoo_wristband"
    # Sample_ts must come from the fresh ingest, not the stale one.
    assert abs(frame.heart_rate_sample_ts - now) < 1.0


def test_stale_healthkit_does_not_clobber_fresh_wearable():
    """Reverse direction: fresh wearable arrives first, then a
    stale HealthKit ping. The wearable must remain authoritative.
    """
    from perception.fusion import PerceptionEngine

    engine = PerceptionEngine()
    now = time.time()
    stale_ts = now - 60_000

    engine.update_sensors("session_b", {
        "ppg_heart_rate": 72,
        "heart_rate_source": "veepoo_wristband",
        "heart_rate_sample_ts": now,
    })

    engine.update_sensors("session_b", {
        "ppg_heart_rate": 115,
        "heart_rate_source": "apple_healthkit",
        "heart_rate_sample_ts": stale_ts,
    })

    frame = engine.get_frame("session_b")
    assert frame is not None
    assert frame.heart_rate == 72
    assert frame.heart_rate_source == "veepoo_wristband"
