"""Blood pressure from the Theora glasses, ingestion to answer.

The glasses measure BP on demand (`jwTestBPAction` in the JW SDK, which
calls back with `int high, int low`). Before this, FERAL had no
`blood_pressure` event type, no canonical metric, and no plausible
range -- so a reading of ANY value, including a zeroed decode, would
have been accepted unchallenged had one ever arrived.

These tests pin the path end to end: a device_event carrying a BP pair
reaches the perception frame, the durable store, and the LLM context.
"""
import time

import pytest

import api.server as srv
from agents.baseline_engine import BaselineEngine
from integrations.health_canonical import CANONICAL_METRICS, build_reading
from perception.fusion import PerceptionFrame

NODE = "feral-iphone-bptest"


def test_blood_pressure_is_in_the_dispatcher_vocabulary():
    """The `uv` and `hrv` bugs were both a branch with no filter entry."""
    assert "blood_pressure" in srv._EXTRACTABLE_EVENT_TYPES


def test_both_halves_are_canonical_metrics():
    assert "bp_systolic" in CANONICAL_METRICS
    assert "bp_diastolic" in CANONICAL_METRICS
    assert CANONICAL_METRICS["bp_systolic"].unit == "mmHg"
    assert CANONICAL_METRICS["bp_diastolic"].unit == "mmHg"


def test_a_reading_survives_the_canonical_builder():
    r = build_reading("bp_systolic", 118, source="jw_health_glasses", ts=1.0)
    assert r is not None
    assert r["unit"] == "mmHg"
    assert r["source_name"] == "Theora glasses"


class TestPlausibility:
    """The gate exists to reject a decode fault, not a sick person."""

    @pytest.mark.parametrize("sys_,dia", [(118, 78), (90, 60), (185, 120), (210, 130)])
    def test_real_readings_are_accepted(self, sys_, dia):
        assert BaselineEngine.is_plausible("bp_systolic", sys_)
        assert BaselineEngine.is_plausible("bp_diastolic", dia)

    @pytest.mark.parametrize("value", [0, -5, 400, 1000])
    def test_impossible_values_are_rejected(self, value):
        assert not BaselineEngine.is_plausible("bp_systolic", value)

    def test_a_hypertensive_crisis_is_not_filtered_away(self):
        """The reading someone most needs to see must not be dropped."""
        assert BaselineEngine.is_plausible("bp_systolic", 190)
        assert BaselineEngine.is_plausible("bp_diastolic", 125)


@pytest.fixture()
def captured_sensors(monkeypatch):
    """Capture what the dispatcher hands to the perception frame.

    The dispatcher fans out to the sessions bound to the emitting node,
    so a node with no session silently delivers nothing -- bind one.
    """
    seen: list[dict] = []

    def _capture(session_id, sensors):
        seen.append(dict(sensors))

    monkeypatch.setitem(srv.state._daemon_session_bindings, NODE, {"bp-session"})
    monkeypatch.setattr(srv.state.perception, "update_sensors", _capture)
    return seen


class TestIngestion:
    def test_sdk_high_low_names_are_accepted(self, captured_sensors):
        """The glasses SDK says high/low; HealthKit says systolic/diastolic."""
        srv._handle_biometric_device_event(
            NODE, "blood_pressure",
            {"high": 118, "low": 78, "source": "jw_health_glasses", "ts": time.time()},
        )
        merged = {k: v for d in captured_sensors for k, v in d.items()}
        assert merged.get("bp_systolic") == 118
        assert merged.get("bp_diastolic") == 78
        assert merged.get("blood_pressure_source") == "jw_health_glasses"

    def test_healthkit_systolic_diastolic_names_are_accepted(self, captured_sensors):
        srv._handle_biometric_device_event(
            NODE, "blood_pressure",
            {"systolic": 122, "diastolic": 81, "source": "jw_health_glasses",
             "ts": time.time()},
        )
        merged = {k: v for d in captured_sensors for k, v in d.items()}
        assert merged.get("bp_systolic") == 122
        assert merged.get("bp_diastolic") == 81

    def test_half_a_reading_is_not_a_reading(self, captured_sensors):
        """Storing one half alone would let a query pair it wrongly."""
        srv._handle_biometric_device_event(
            NODE, "blood_pressure",
            {"high": 118, "source": "jw_health_glasses", "ts": time.time()},
        )
        merged = {k: v for d in captured_sensors for k, v in d.items()}
        assert "bp_systolic" not in merged
        assert "bp_diastolic" not in merged


class TestDurableAndContext:
    def test_both_halves_are_mapped_into_the_durable_store(self):
        for key in ("bp_systolic", "bp_diastolic"):
            assert key in srv._HISTORY_METRIC_MAP, f"{key} would never be persisted"
            metric, src_key, ts_key = srv._HISTORY_METRIC_MAP[key]
            assert metric == key
            assert src_key == "blood_pressure_source"
            assert ts_key == "blood_pressure_sample_ts"

    def test_frame_carries_the_pair_and_the_context_names_it(self):
        f = PerceptionFrame()
        f.bp_systolic, f.bp_diastolic = 118, 78
        f.bp_sample_ts = time.time()
        ctx = f.to_system_context()
        assert "BP=118/78mmHg" in ctx

    def test_an_old_reading_is_reported_with_its_age_not_suppressed(self):
        """An hour-old BP is still the wearer's most recent BP.

        HR and SpO2 are suppressed when stale because a stale stream
        value is misleading. A BP measurement describes one moment and
        is never superseded by silence, so it is aged, not hidden.
        """
        f = PerceptionFrame()
        f.bp_systolic, f.bp_diastolic = 118, 78
        f.bp_sample_ts = time.time() - 7200
        ctx = f.to_system_context()
        assert "118/78" in ctx
        assert "2 h ago" in ctx
        assert "not a current reading" in ctx

    def test_a_late_arriving_older_reading_does_not_overwrite_a_newer_one(self):
        """Driven through the real merge, not a re-implementation of it."""
        from perception.fusion import PerceptionEngine

        engine = PerceptionEngine()
        session = "bp-merge"
        now = time.time()
        engine.update_sensors(session, {
            "bp_systolic": 120, "bp_diastolic": 80,
            "blood_pressure_sample_ts": now,
            "blood_pressure_source": "jw_health_glasses",
        })
        engine.update_sensors(session, {
            "bp_systolic": 99, "bp_diastolic": 55,
            "blood_pressure_sample_ts": now - 600,
            "blood_pressure_source": "jw_health_glasses",
        })
        frame = engine.get_frame(session)
        assert (frame.bp_systolic, frame.bp_diastolic) == (120, 80)

    def test_a_newer_reading_does_replace_an_older_one(self):
        from perception.fusion import PerceptionEngine

        engine = PerceptionEngine()
        session = "bp-merge-2"
        now = time.time()
        engine.update_sensors(session, {
            "bp_systolic": 120, "bp_diastolic": 80,
            "blood_pressure_sample_ts": now - 600,
            "blood_pressure_source": "jw_health_glasses",
        })
        engine.update_sensors(session, {
            "bp_systolic": 131, "bp_diastolic": 86,
            "blood_pressure_sample_ts": now,
            "blood_pressure_source": "jw_health_glasses",
        })
        frame = engine.get_frame(session)
        assert (frame.bp_systolic, frame.bp_diastolic) == (131, 86)


class TestReachesTheDurableStoreForReal:
    """Ingestion to query, against a real engine and a real database.

    The maps above pin the wiring; this pins that the wiring carries a
    value all the way to the thing the health tools read.
    """

    def test_a_relayed_reading_is_queryable_afterwards(self, tmp_path, monkeypatch):
        engine = BaselineEngine(db_path=str(tmp_path / "b.db"))
        monkeypatch.setattr(srv.state, "baseline_engine", engine, raising=False)

        now = time.time()
        srv._record_biometrics_to_history(
            {
                "bp_systolic": 118,
                "bp_diastolic": 78,
                "blood_pressure_source": "jw_health_glasses",
                "blood_pressure_sample_ts": now,
            },
            effective_node="feral-iphone-bptest",
        )

        sys_rows = engine.get_samples("bp_systolic", since=now - 60)
        dia_rows = engine.get_samples("bp_diastolic", since=now - 60)
        assert len(sys_rows) == 1, "systolic never reached the durable store"
        assert len(dia_rows) == 1, "diastolic never reached the durable store"
        assert sys_rows[0]["value"] == 118
        assert dia_rows[0]["value"] == 78
        assert sys_rows[0]["source"] == "jw_health_glasses"
        # Both halves must share a timestamp or a reader cannot pair them.
        assert sys_rows[0]["ts"] == dia_rows[0]["ts"]

    def test_an_on_demand_reading_is_not_rejected_for_being_minutes_old(
        self, tmp_path, monkeypatch,
    ):
        """BP is measured on demand, so "old" is its normal state.

        The 120 s freshness window that guards the HR baseline would
        discard every blood pressure ever taken, since the wearer starts
        a measurement and the app relays it on its next poll.
        """
        engine = BaselineEngine(db_path=str(tmp_path / "b2.db"))
        monkeypatch.setattr(srv.state, "baseline_engine", engine, raising=False)

        taken_at = time.time() - 1800  # half an hour ago
        srv._record_biometrics_to_history(
            {
                "bp_systolic": 131,
                "bp_diastolic": 86,
                "blood_pressure_source": "jw_health_glasses",
                "blood_pressure_sample_ts": taken_at,
            },
            effective_node="feral-iphone-bptest",
        )

        rows = engine.get_samples("bp_systolic", since=taken_at - 60)
        assert len(rows) == 1, "a 30-minute-old BP reading was silently dropped"
        assert abs(rows[0]["ts"] - taken_at) < 1.0

    def test_the_health_history_tool_returns_it_with_the_product_name(
        self, tmp_path, monkeypatch,
    ):
        import asyncio

        from integrations.health_platforms import HealthAggregator

        engine = BaselineEngine(db_path=str(tmp_path / "b3.db"))
        now = time.time()
        engine.record_sample("bp_systolic", 118.0, source="jw_health_glasses", ts=now)
        engine.record_sample("bp_diastolic", 78.0, source="jw_health_glasses", ts=now)

        agg = HealthAggregator.__new__(HealthAggregator)
        agg._biometric_history = lambda: engine
        agg._maybe_sync_durable = lambda: asyncio.sleep(0)

        history = asyncio.get_event_loop().run_until_complete(
            agg.get_health_history(days=1)
        )
        assert "bp_systolic" in history["metrics"]
        assert "bp_diastolic" in history["metrics"]
        assert history["sources"] == ["Theora glasses"]
        reading = history["series"]["bp_systolic"][0]
        assert reading["value"] == 118.0
        assert reading["unit"] == "mmHg"
