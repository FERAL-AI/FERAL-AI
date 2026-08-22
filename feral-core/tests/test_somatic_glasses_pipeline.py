"""The glasses -> somatic engine -> behavioural policy -> proactive path.

Reported from the Theora iOS client on 2026-08-22: the somatic engine
was constructed, wired and injected into the system prompt, but the
vector stayed empty, so BehavioralPolicy never left "normal".

The wiring was in fact present (`_handle_biometric_device_event` calls
`update_from_perception_frame`, which calls `update_biometrics`). What
was broken was narrower and worse: three of the five signals could not
reach the vector at all, including the one that dominates cognitive
load. Measured on a live brain before the fix, feeding one device_event
of each type:

    heart_rate  78    arrived
    spo2        97    arrived
    skin_temp   33.4  DROPPED  (written flat, read only under "vitals")
    steps       4213  DROPPED  (written "steps", read "steps_today")
    hrv         42    DROPPED  (no ingestion path existed at all)

producing "HR:78bpm | SpO2:97%" and a cognitive load computed without
its largest term.

These tests pin each leg so none of it can silently come apart again.
"""
from __future__ import annotations

import time

import pytest

import api.server as srv
from api.state import state
from perception.somatic import (
    HRV_MAX_MS,
    HRV_MIN_MS,
    SomaticEngine,
    plausible_hrv_ms,
)


NODE = "glasses-test"
SESSION = "sess-test"


@pytest.fixture()
def midday(monkeypatch):
    """Pin the local clock to 13:30 so circadian terms are deterministic.

    Both the cognitive-load circadian term and the `hour < 5 or
    hour > 23` branch of get_behavioral_policy read `time.localtime()`,
    so anything asserting a tone or a load figure is otherwise a
    function of when the suite happens to run.

    This is not hypothetical: two tests here passed locally at 16:00
    PDT and failed on a UTC runner at 23:18, where the fractional hour
    is 23.31 and the quiet-hours branch rewrote tone to "calm". A first
    attempt guarded with `time.localtime().tm_hour > 23`, which is the
    INTEGER hour and can never exceed 23, so the guard could not fire
    for the very case that breaks. Freeze the clock instead of trying
    to detect it.

    13:30 is chosen to sit outside the 13:00-15:00 post-lunch dip,
    which carries its own 0.15 circadian load.
    """
    import time as _time

    frozen = _time.struct_time((2026, 8, 22, 13, 30, 0, 4, 234, 1))

    class _FrozenClock:
        """Only localtime is frozen. `time()` must keep advancing:
        freshness, staleness and age_s are all measured with it."""

        @staticmethod
        def localtime(*args, **kwargs):
            return frozen

        @staticmethod
        def time():
            return _time.time()

    monkeypatch.setattr("perception.somatic.time", _FrozenClock)
    return frozen


@pytest.fixture()
def glasses(monkeypatch, midday):
    """A somatic engine bound to one node, driven by real device_events."""
    engine = SomaticEngine()
    monkeypatch.setattr(state, "somatic_engine", engine, raising=False)
    monkeypatch.setitem(state._daemon_session_bindings, NODE, {SESSION})

    def send(event_type: str, payload: dict) -> None:
        srv._handle_biometric_device_event(NODE, event_type, dict(payload))

    return engine, send


def _vector(engine):
    return engine.get_vector(SESSION)


# ── Ask 1: the readings reach the vector ────────────────────────────────


class TestGlassesReachTheVector:

    def test_every_signal_the_glasses_send_arrives(self, glasses):
        engine, send = glasses
        send("heart_rate", {"bpm": 78, "source": "jw_health_glasses", "ts": time.time()})
        send("hrv", {"rmssd_ms": 42, "source": "jw_health_glasses"})
        send("spo2", {"current": 97, "source": "jw_health_glasses"})
        send("skin_temperature", {"celsius": 33.4})
        send("steps", {"count": 4213})

        v = _vector(engine)
        assert v.heart_rate == 78
        assert v.hrv_ms == 42, "hrv had no ingestion path at all"
        assert v.spo2_pct == 97
        assert v.skin_temp_c == 33.4, "written flat, was read only under vitals"
        assert v.steps_today == 4213, "written as 'steps', was read as 'steps_today'"

    def test_hrv_is_in_the_dispatcher_vocabulary(self):
        """The `uv` bug was a branch with no filter entry. Same shape."""
        assert "hrv" in srv._EXTRACTABLE_EVENT_TYPES
        assert "activity" in srv._EXTRACTABLE_EVENT_TYPES

    def test_cognitive_load_uses_hrv_once_it_can_see_it(self, glasses):
        engine, send = glasses
        send("heart_rate", {"bpm": 78, "source": "jw_health_glasses", "ts": time.time()})
        without_hrv = _vector(engine).cognitive_load

        send("hrv", {"rmssd_ms": 8})
        with_crushed_hrv = _vector(engine).cognitive_load

        assert with_crushed_hrv > without_hrv, (
            "hrv carries weight 0.3, the largest single term"
        )


class TestCircadianPhase:
    """Was only ever set by update_interaction.

    A session fed by a wearable and nothing else kept circadian_phase at
    its 0.0 default, which reads as MIDNIGHT: measured at 14:00, a
    glasses stream produced tone="calm" and suppress_non_urgent=True
    from the `hour < 5` branch, plus a spurious 0.3 circadian term in
    cognitive load.
    """

    def test_biometrics_alone_set_the_clock(self, glasses, midday):
        engine, send = glasses
        send("heart_rate", {"bpm": 70, "source": "jw_health_glasses", "ts": time.time()})

        v = _vector(engine)
        expected = (midday.tm_hour * 60 + midday.tm_min) / 1440.0
        assert v.circadian_phase == pytest.approx(expected, abs=0.001)
        assert v.circadian_phase > 0, "0.0 is the bug: it reads as midnight"

    def test_a_daytime_reading_is_not_treated_as_midnight(self, glasses):
        engine, send = glasses
        send("heart_rate", {"bpm": 70, "source": "jw_health_glasses", "ts": time.time()})
        send("hrv", {"rmssd_ms": 55})

        policy = engine.get_behavioral_policy(SESSION)
        assert policy.suppress_non_urgent is False
        assert policy.tone == "normal"

    def test_the_quiet_hours_branch_still_works(self, glasses, monkeypatch):
        """The daytime fix must not disable the behaviour it sits next to."""
        engine, send = glasses
        send("heart_rate", {"bpm": 70, "source": "jw_health_glasses", "ts": time.time()})
        send("hrv", {"rmssd_ms": 55})
        assert engine.get_behavioral_policy(SESSION).suppress_non_urgent is False

        # 02:00. Note the branch compares the FRACTIONAL hour, which is
        # why a guard written against tm_hour cannot express this case.
        _vector(engine).circadian_phase = 2.0 / 24.0
        policy = engine.get_behavioral_policy(SESSION)
        assert policy.tone == "calm"
        assert policy.suppress_non_urgent is True


# ── Ask 1: hrv_ms must be RMSSD in milliseconds ─────────────────────────


class TestHRVScaleGuard:
    """hrv_ms drives cognitive load as `1.0 - hrv_ms/100.0` at weight
    0.3, so a vendor index on an undocumented scale does not degrade the
    policy, it inverts it."""

    @pytest.mark.parametrize("value", [42, 5, 300, 15.5, 99])
    def test_plausible_values_accepted(self, value):
        assert plausible_hrv_ms(value) is True

    @pytest.mark.parametrize("value", [0, 3, 4.9, 301, 900, -20, None, "x"])
    def test_implausible_values_rejected(self, value):
        assert plausible_hrv_ms(value) is False

    def test_bounds_are_the_documented_ones(self):
        assert (HRV_MIN_MS, HRV_MAX_MS) == (5.0, 300.0)

    def test_a_bad_scale_never_reaches_the_vector(self, glasses):
        engine, send = glasses
        send("hrv", {"rmssd_ms": 42})
        assert _vector(engine).hrv_ms == 42

        send("hrv", {"value": 3})       # a 0-10 vendor index
        send("hrv", {"rmssd_ms": 900})  # microseconds, or nonsense
        assert _vector(engine).hrv_ms == 42, (
            "an implausible reading must be dropped, and the last good "
            "one must stand"
        )

    def test_the_drop_is_visible_in_the_log(self, glasses, caplog):
        _engine, send = glasses
        with caplog.at_level("WARNING"):
            send("hrv", {"value": 3})
        assert any("RMSSD" in r.message or "RMSSD" in str(r.msg) for r in caplog.records)


# ── Ask 1: the activity gate the reporter asked us to preserve ──────────


class TestActivityGate:
    """`_recompute_cognitive_load` uses heart rate only when
    activity_level < 0.3, which is what stops a walk upstairs reading as
    stress. Nothing populated activity_level from this path, so the
    guard never engaged."""

    def test_activity_arrives(self, glasses):
        engine, send = glasses
        send("activity", {"state": "running"})
        assert _vector(engine).activity_level == 1.0

    def test_exertion_does_not_read_as_load(self, glasses):
        engine, send = glasses
        send("hrv", {"rmssd_ms": 60})

        send("activity", {"state": "sedentary"})
        send("heart_rate", {"bpm": 128, "source": "jw_health_glasses", "ts": time.time()})
        sitting = _vector(engine).cognitive_load

        send("activity", {"state": "running"})
        send("heart_rate", {"bpm": 128, "source": "jw_health_glasses", "ts": time.time()})
        running = _vector(engine).cognitive_load

        assert running < sitting, "the same heart rate must mean less when moving"

    def test_activity_survives_later_frames(self, glasses):
        """A device_event carries ONE reading.

        Defaulting activity to 0.0 for a frame that says nothing about
        it made every subsequent reading overwrite a known "walking"
        back to "sedentary", so the guard was undone by the next heart
        rate that arrived.
        """
        engine, send = glasses
        send("activity", {"state": "walking"})
        assert _vector(engine).activity_level == 0.5

        send("heart_rate", {"bpm": 110, "source": "jw_health_glasses", "ts": time.time()})
        send("steps", {"count": 5000})
        assert _vector(engine).activity_level == 0.5


# ── Ask 2: the policy has to be observable ──────────────────────────────


class TestPolicyIsObservable:

    def test_no_biometrics_means_no_claim(self, glasses):
        """None, not an empty object.

        "The agent is not adapting" and "the agent is adapting to a
        neutral state" are different claims.
        """
        assert srv._somatic_state_for_turn(SESSION) is None

    def test_frame_carries_input_and_output(self, glasses):
        engine, send = glasses
        send("heart_rate", {"bpm": 78, "source": "jw_health_glasses", "ts": time.time()})
        send("hrv", {"rmssd_ms": 45})

        frame = engine.state_frame(SESSION, reason="chat_turn")
        # Input.
        for key in ("cognitive_load", "stress_level", "heart_rate", "hrv_ms"):
            assert key in frame
        # Output: what the agent will actually do about it.
        for key in ("tone", "suppress_non_urgent", "tool_restrictions", "proactive_level"):
            assert key in frame
        assert frame["reason"] == "chat_turn"
        assert frame["has_biometrics"] is True

    def test_strain_is_visible_as_a_changed_policy(self, glasses):
        engine, send = glasses
        send("heart_rate", {"bpm": 118, "source": "jw_health_glasses", "ts": time.time()})
        send("hrv", {"rmssd_ms": 8})

        frame = srv._somatic_state_for_turn(SESSION)
        assert frame is not None
        assert frame["cognitive_load"] > 0.7
        assert frame["tone"] == "concise"
        assert frame["suppress_non_urgent"] is True
        assert frame["max_response_tokens"] == 150
        assert frame["tool_restrictions"], "high load restricts tools"

    def test_the_frame_reports_the_policy_actually_applied(self, glasses):
        """`BehavioralPolicy.from_vector` is a SECOND derivation with no
        production caller, and it disagrees: it answers "calm" where the
        live one answers "concise". Reporting it would show a client a
        policy the agent is not applying."""
        engine, send = glasses
        send("heart_rate", {"bpm": 118, "source": "jw_health_glasses", "ts": time.time()})
        send("hrv", {"rmssd_ms": 8})

        frame = engine.state_frame(SESSION)
        assert frame["tone"] == engine.get_behavioral_policy(SESSION).tone

    def test_a_stale_vector_is_marked_stale(self, glasses):
        """A vector outlives the wearable that fed it. A body-state
        display must never present an old reading as current."""
        engine, send = glasses
        send("heart_rate", {"bpm": 78, "source": "jw_health_glasses", "ts": time.time()})
        assert engine.state_frame(SESSION)["stale"] is False

        engine.get_vector(SESSION).timestamp = time.time() - 3600
        frame = engine.state_frame(SESSION)
        assert frame["stale"] is True
        assert frame["age_s"] > 120

    def test_the_wire_model_accepts_the_frame(self, glasses):
        from models.protocol import MESSAGE_TYPES, SomaticStatePayload

        engine, send = glasses
        send("heart_rate", {"bpm": 78, "source": "jw_health_glasses", "ts": time.time()})
        SomaticStatePayload(**engine.state_frame(SESSION))
        assert MESSAGE_TYPES["somatic_state"] is SomaticStatePayload

    def test_chat_response_can_carry_it(self):
        from models.protocol import ChatResponsePayload, SomaticStatePayload

        reply = ChatResponsePayload(
            session_id="s", text="ok",
            somatic=SomaticStatePayload(cognitive_load=0.8, tone="concise"),
        )
        assert reply.somatic.tone == "concise"
        assert ChatResponsePayload(session_id="s", text="ok").somatic is None

    def test_a_non_dict_never_reaches_the_wire(self, monkeypatch):
        """The turn field is serialised onto the reply the user is
        waiting for, so a value that cannot be serialised takes the
        whole chat_response with it.

        Not hypothetical, and the second time this pattern has bitten
        in this codebase: a MagicMock has every attribute and every call
        returns another MagicMock, so a test double standing in for
        BrainState satisfies the engine lookup, `.state_frame(...)` and
        `.get("has_biometrics")` all truthily, and put a MagicMock on
        the socket. Two chat tests died on it.
        """
        from unittest.mock import MagicMock

        monkeypatch.setattr(state, "somatic_engine", MagicMock(), raising=False)
        assert srv._somatic_state_for_turn(SESSION) is None

    def test_a_raising_engine_does_not_cost_the_reply(self, monkeypatch):
        engine = SomaticEngine()

        def _boom(*_a, **_kw):
            raise RuntimeError("somatic exploded")

        engine.state_frame = _boom
        monkeypatch.setattr(state, "somatic_engine", engine, raising=False)
        assert srv._somatic_state_for_turn(SESSION) is None

    def test_publishing_is_suppressed_when_the_policy_has_not_moved(self, glasses):
        engine, send = glasses
        send("heart_rate", {"bpm": 78, "source": "jw_health_glasses", "ts": time.time()})
        frame = engine.state_frame(SESSION)
        signature = srv._somatic_policy_signature(frame)
        assert srv._somatic_policy_signature(engine.state_frame(SESSION)) == signature

        send("hrv", {"rmssd_ms": 8})
        send("heart_rate", {"bpm": 125, "source": "jw_health_glasses", "ts": time.time()})
        assert srv._somatic_policy_signature(engine.state_frame(SESSION)) != signature


# ── Ask 3: proactive fires on load, not on a raw threshold ──────────────


class TestProactiveUsesLoad:

    @staticmethod
    def _engine(somatic):
        from agents.proactive_engine import ProactiveEngine
        from perception.fusion import PerceptionEngine

        engine = ProactiveEngine(
            perception=PerceptionEngine(), somatic_engine=somatic,
        )
        engine._first_interaction_today = False
        return engine

    def test_threshold_matches_the_policy_boundary(self):
        from agents.proactive_engine import SOMATIC_LOAD_ALERT

        assert SOMATIC_LOAD_ALERT == 0.7, (
            "the same 0.7 get_behavioral_policy treats as high load, so "
            "the agent alerts exactly when it also changes its behaviour"
        )

    def test_exertion_does_not_alert(self, glasses):
        somatic, send = glasses
        send("activity", {"state": "running"})
        send("hrv", {"rmssd_ms": 60})
        send("heart_rate", {"bpm": 128, "source": "jw_health_glasses", "ts": time.time()})

        from agents.proactive_engine import SOMATIC_LOAD_ALERT
        load = self._engine(somatic)._cognitive_load_for(SESSION)
        assert load is not None
        assert load < SOMATIC_LOAD_ALERT, "128 bpm while running is not an alert"

    def test_strain_alerts(self, glasses):
        somatic, send = glasses
        send("activity", {"state": "sedentary"})
        send("hrv", {"rmssd_ms": 8})
        send("heart_rate", {"bpm": 118, "source": "jw_health_glasses", "ts": time.time()})

        from agents.proactive_engine import SOMATIC_LOAD_ALERT
        load = self._engine(somatic)._cognitive_load_for(SESSION)
        assert load >= SOMATIC_LOAD_ALERT

    def test_without_a_somatic_engine_the_raw_path_is_kept(self):
        """None routes the caller back to `heart_rate > 100`.

        A brain with no wearable HRV must not silently lose the alert.
        """
        assert self._engine(None)._cognitive_load_for(SESSION) is None

    def test_none_not_zero_when_unknown(self, glasses):
        """0.0 is a real value meaning "this person is fine" and would
        silence a genuine alert."""
        somatic, _send = glasses
        assert self._engine(somatic)._cognitive_load_for("never-seen") is None

    def test_a_stale_body_does_not_decide(self, glasses):
        somatic, send = glasses
        send("hrv", {"rmssd_ms": 8})
        send("heart_rate", {"bpm": 118, "source": "jw_health_glasses", "ts": time.time()})
        assert self._engine(somatic)._cognitive_load_for(SESSION) is not None

        somatic.get_vector(SESSION).timestamp = time.time() - 3600
        assert self._engine(somatic)._cognitive_load_for(SESSION) is None

    def test_circadian_alone_never_decides(self, glasses):
        """An all-zero vector still produces a load figure from
        circadian phase. That is not a statement about anyone's body and
        must not be allowed to interrupt them."""
        somatic, send = glasses
        somatic.update_interaction(SESSION, text_length=10)
        assert somatic.get_vector(SESSION).timestamp > 0
        assert self._engine(somatic)._cognitive_load_for(SESSION) is None
