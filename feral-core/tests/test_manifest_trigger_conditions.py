"""Pin the manifest trigger condition evaluator.

Background (the failure these tests exist to prevent recurring):
manifests have declared ``triggers[].condition`` since the schema was
written, and nothing in the tree ever read those strings.
``skills/registry.py`` copied the condition into a cron payload and
``api/server.py`` dispatched the payload, so a ``JobType.TRIGGERED``
routine polled "every 1m" and ran its action unconditionally. Two such
routines on the operator's install accumulated 4,766 runs each, one of
them a Telegram send gated on
``biometric.heart_rate_bpm > 160 && biometric.inferred_state == 'stressed'``
that was never evaluated.

So the invariants under test are, in order of importance:
  1. a false condition does not fire,
  2. an unparseable condition does not fire, and says so at WARNING,
  3. injection-shaped input is rejected rather than evaluated,
  4. a missing reading is not a satisfied condition,
  5. cooldown_seconds from the manifest is honoured,
  6. a satisfied condition notifies and executes nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agents.proactive_engine import Priority, ProactiveEngine
from agents.trigger_conditions import (
    ConditionParseError,
    build_biometric_namespace,
    describe_namespace,
    evaluate_condition,
    parse_condition,
)
from perception.fusion import PerceptionFrame

# The condition that shipped in skills/manifests/messaging.json and was
# never evaluated. Every "real manifest" test below reads it from disk
# rather than restating it, so an edit to the manifest cannot silently
# drift away from its test.
_MANIFEST_PATH = Path(__file__).resolve().parents[1] / "skills" / "manifests" / "messaging.json"


def _shipped_trigger() -> dict:
    data = json.loads(_MANIFEST_PATH.read_text())
    triggers = data.get("triggers") or []
    assert triggers, "messaging.json lost its trigger definition"
    return triggers[0]


def _all_shipped_conditions() -> list[tuple[str, str, str]]:
    """(manifest file, trigger id, condition) for every shipped manifest.

    Two exist today: messaging.json's high_stress_alert and
    smart_home.json's sleep_detected. Both are now read by a live
    evaluator, so both must at minimum parse.
    """
    out = []
    for path in sorted(_MANIFEST_PATH.parent.glob("*.json")):
        data = json.loads(path.read_text())
        for trigger in data.get("triggers") or []:
            condition = trigger.get("condition")
            if condition:
                out.append((path.name, trigger.get("id", "?"), condition))
    return out


def _frame(**kw) -> PerceptionFrame:
    """A perception frame with fresh, live-wearable defaults."""
    now = kw.pop("now", time.time())
    defaults = dict(
        timestamp=now,
        heart_rate=82,
        heart_rate_sample_ts=now,
        heart_rate_source="jw_health_glasses",
        activity_state="resting",
    )
    defaults.update(kw)
    return PerceptionFrame(**defaults)


# ---------------------------------------------------------------------------
# Grammar
# ---------------------------------------------------------------------------

class TestGrammar:
    def test_shipped_condition_parses(self):
        node = parse_condition(_shipped_trigger()["condition"])
        assert node is not None

    def test_every_shipped_manifest_condition_parses(self):
        """A shipped condition that cannot parse is a permanently dead gate."""
        shipped = _all_shipped_conditions()
        assert shipped, "no shipped manifest declares a trigger any more"
        for filename, trigger_id, condition in shipped:
            try:
                parse_condition(condition)
            except ConditionParseError as exc:  # pragma: no cover - failure path
                pytest.fail(f"{filename}:{trigger_id} does not parse: {exc}")

    def test_sleep_detected_is_evaluable_and_currently_false(self):
        """smart_home.json's trigger is decidable, not invented into truth.

        There is no sleep metric on this install. The condition compares
        the fused activity state, whose documented domain
        (models/protocol.py:80) is resting/walking/running/stressed, so it
        evaluates to a definite False rather than being made up.
        """
        conditions = {
            trigger_id: condition
            for _f, trigger_id, condition in _all_shipped_conditions()
        }
        condition = conditions.get("sleep_detected")
        if condition is None:
            pytest.skip("smart_home.json no longer declares sleep_detected")
        now = time.time()
        ns = build_biometric_namespace(frames=[_frame(now=now)], now=now)
        result = evaluate_condition(condition, ns)
        assert result.parse_error is None
        assert result.satisfied is False
        assert result.type_errors == []

    @pytest.mark.parametrize("text", [
        "biometric.heart_rate_bpm > 160",
        "biometric.heart_rate_bpm >= 160.5",
        "biometric.inferred_state == 'stressed'",
        'biometric.inferred_state != "resting"',
        "biometric.heart_rate_bpm > 160 && biometric.inferred_state == 'stressed'",
        "biometric.heart_rate_bpm > 160 || biometric.spo2_pct < 94",
        "(biometric.heart_rate_bpm > 160 || biometric.spo2_pct < 94) "
        "&& biometric.inferred_state == 'stressed'",
        "biometric.heart_rate_deviation_sigma >= 3",
        "biometric.skin_temperature_c > -1",
        "160 < biometric.heart_rate_bpm",
    ])
    def test_supported_forms(self, text):
        assert parse_condition(text) is not None

    @pytest.mark.parametrize("text", [
        # Bare truthiness: coercing a value to a bool is exactly how a gate
        # becomes "always passes".
        "biometric.heart_rate_bpm",
        "true",
        # Arithmetic, calls, indexing, membership, negation: none of these
        # are comparisons of two values.
        "biometric.heart_rate_bpm + 1 > 2",
        "max(biometric.heart_rate_bpm, 2) > 2",
        "biometric.heart_rate_bpm[0] > 2",
        "biometric.inferred_state in ['stressed']",
        "not biometric.heart_rate_bpm > 2",
        "biometric.heart_rate_bpm > 2 and biometric.spo2_pct < 94",
        # Deeper attribute chains and private names.
        "biometric.vitals.hr > 2",
        "biometric.__class__ == 1",
        # Unknown namespaces.
        "state.secrets == 'x'",
        "heart_rate_bpm > 2",
        # Malformed.
        "",
        "   ",
        "biometric.heart_rate_bpm >",
        "biometric.heart_rate_bpm > 2 &&",
        "(biometric.heart_rate_bpm > 2",
        "biometric.heart_rate_bpm => 2",
        # Backslash escapes in strings are refused rather than guessed at.
        r"biometric.inferred_state == 'stre\'ssed'",
    ])
    def test_refused_forms(self, text):
        with pytest.raises(ConditionParseError):
            parse_condition(text)


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

class TestEvaluation:
    def test_true_condition_is_satisfied(self):
        result = evaluate_condition(
            _shipped_trigger()["condition"],
            {
                "biometric.heart_rate_bpm": 171,
                "biometric.inferred_state": "stressed",
            },
        )
        assert result.satisfied is True
        assert result.resolved["biometric.heart_rate_bpm"] == 171

    def test_false_condition_does_not_fire(self):
        """The real reading on this install: 82 bpm, resting."""
        result = evaluate_condition(
            _shipped_trigger()["condition"],
            {
                "biometric.heart_rate_bpm": 82,
                "biometric.inferred_state": "resting",
            },
        )
        assert result.satisfied is False
        assert result.parse_error is None
        assert result.type_errors == []

    def test_half_true_and_does_not_fire(self):
        result = evaluate_condition(
            _shipped_trigger()["condition"],
            {
                "biometric.heart_rate_bpm": 171,
                "biometric.inferred_state": "resting",
            },
        )
        assert result.satisfied is False

    def test_unparseable_does_not_fire_and_warns(self, caplog):
        with caplog.at_level(logging.WARNING, logger="feral.triggers"):
            result = evaluate_condition(
                "biometric.heart_rate_bpm >>> 160",
                {"biometric.heart_rate_bpm": 200},
                trigger_id="manifest:demo:bad",
            )
        assert result.satisfied is False
        assert result.parse_error
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "an unparseable condition must not fail silently"
        assert "manifest:demo:bad" in warnings[0].getMessage()
        assert "NOT fire" in warnings[0].getMessage()

    def test_missing_field_is_unknown_not_true(self):
        """SpO2 is absent on this install (newest sample 2026-07-07)."""
        result = evaluate_condition(
            "biometric.spo2_pct < 94",
            {"biometric.heart_rate_bpm": 82},
        )
        assert result.satisfied is False
        assert result.unknown is True
        assert result.missing == ["biometric.spo2_pct"]

    def test_definite_false_beats_unknown_in_and(self):
        result = evaluate_condition(
            "biometric.heart_rate_bpm > 160 && biometric.spo2_pct < 94",
            {"biometric.heart_rate_bpm": 82},
        )
        assert result.satisfied is False
        assert result.unknown is False  # the AND is decidably False

    def test_definite_true_beats_unknown_in_or(self):
        result = evaluate_condition(
            "biometric.heart_rate_bpm > 160 || biometric.spo2_pct < 94",
            {"biometric.heart_rate_bpm": 171},
        )
        assert result.satisfied is True

    def test_unknown_or_false_does_not_fire(self):
        result = evaluate_condition(
            "biometric.heart_rate_bpm > 160 || biometric.spo2_pct < 94",
            {"biometric.heart_rate_bpm": 82},
        )
        assert result.satisfied is False

    def test_type_mismatch_does_not_fire_and_warns(self, caplog):
        with caplog.at_level(logging.WARNING, logger="feral.triggers"):
            result = evaluate_condition(
                "biometric.inferred_state > 100",
                {"biometric.inferred_state": "stressed"},
                trigger_id="manifest:demo:typed",
            )
        assert result.satisfied is False
        assert result.type_errors
        assert [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_bool_is_not_a_number(self):
        """True must not compare as 1: silent coercion is the whole hazard."""
        result = evaluate_condition(
            "biometric.heart_rate_bpm > 0",
            {"biometric.heart_rate_bpm": True},
        )
        assert result.satisfied is False
        assert result.type_errors

    def test_string_ordering_is_refused(self):
        result = evaluate_condition(
            "biometric.inferred_state > 'a'",
            {"biometric.inferred_state": "stressed"},
        )
        assert result.satisfied is False
        assert result.type_errors


# ---------------------------------------------------------------------------
# Injection
# ---------------------------------------------------------------------------

class TestInjectionIsRejectedNotEvaluated:
    @pytest.mark.parametrize("text", [
        "__import__('os').system('id')",
        "__import__(\"os\")",
        "os.system('rm -rf /')",
        "eval('1==1')",
        "exec('x=1')",
        "open('/etc/passwd').read() == 'x'",
        "biometric.heart_rate_bpm > 0; import os",
        "biometric.heart_rate_bpm > 0 or __import__('os')",
        "().__class__.__bases__[0] == 1",
        "globals() == 1",
        "1==1 if True else 2==2",
        "lambda: 1",
    ])
    def test_rejected(self, text):
        result = evaluate_condition(text, {"biometric.heart_rate_bpm": 200})
        assert result.satisfied is False
        assert result.parse_error, f"{text!r} must be a parse error, not a lookup"

    def test_nothing_is_executed(self, tmp_path):
        """Proof by side effect: a condition that would touch a file, doesn't.

        The evaluator never hands the condition text to eval/exec, so this
        is not "the sandbox held": there is no interpreter on this path at
        all. The marker file is the only observable that would distinguish
        the two.
        """
        marker = tmp_path / "pwned"
        payloads = [
            f"__import__('os').system('touch {marker}')",
            f"__import__('pathlib').Path('{marker}').touch()",
            f"open('{marker}', 'w') == 1",
        ]
        for text in payloads:
            result = evaluate_condition(text, {})
            assert result.satisfied is False
            assert result.parse_error
        assert not marker.exists()

    def test_namespace_keys_are_not_attribute_lookups(self):
        """A dict namespace is looked up by exact key, never by getattr."""
        result = evaluate_condition(
            "biometric.heart_rate_bpm > 0",
            {"biometric": SimpleNamespace(heart_rate_bpm=200)},
        )
        assert result.satisfied is False
        assert result.missing == ["biometric.heart_rate_bpm"]


# ---------------------------------------------------------------------------
# Namespace builder
# ---------------------------------------------------------------------------

class TestBiometricNamespace:
    def test_fresh_live_frame_publishes_hr(self):
        now = time.time()
        ns = build_biometric_namespace(frames=[_frame(now=now)], now=now)
        assert ns["biometric.heart_rate_bpm"] == 82
        assert ns["biometric.heart_rate_source"] == "jw_health_glasses"
        assert ns["biometric.heart_rate_age_s"] < 1
        assert ns["biometric.activity_state"] == "resting"
        assert ns["biometric.inferred_state"] == "resting"

    def test_stale_sample_is_omitted_not_zeroed(self):
        """Omission matters: a zeroed HR would compare as a real number."""
        now = time.time()
        frame = _frame(now=now, heart_rate_sample_ts=now - 3600)
        ns = build_biometric_namespace(frames=[frame], now=now)
        assert "biometric.heart_rate_bpm" not in ns

    def test_unstamped_sample_is_omitted(self):
        now = time.time()
        frame = _frame(now=now, heart_rate_sample_ts=0.0)
        ns = build_biometric_namespace(frames=[frame], now=now)
        assert "biometric.heart_rate_bpm" not in ns

    def test_cloud_mirror_source_is_omitted(self):
        """Operator report 2026-06-08: HealthKit relabels endDate to 'now'."""
        now = time.time()
        frame = _frame(now=now, heart_rate=115, heart_rate_source="apple_healthkit")
        ns = build_biometric_namespace(frames=[frame], now=now)
        assert "biometric.heart_rate_bpm" not in ns

    def test_freshest_frame_wins(self):
        now = time.time()
        old = _frame(now=now, heart_rate=60, heart_rate_sample_ts=now - 100)
        new = _frame(now=now, heart_rate=95, heart_rate_sample_ts=now - 1)
        ns = build_biometric_namespace(frames=[old, new], now=now)
        assert ns["biometric.heart_rate_bpm"] == 95

    def test_unknown_activity_state_is_omitted(self):
        now = time.time()
        ns = build_biometric_namespace(
            frames=[_frame(now=now, activity_state="unknown")], now=now
        )
        assert "biometric.activity_state" not in ns
        assert "biometric.inferred_state" not in ns

    def test_no_frames_yields_empty_namespace(self):
        assert build_biometric_namespace(frames=[], now=time.time()) == {}

    def test_no_invented_fields(self):
        """There is no hrv and no sleep metric on this install."""
        now = time.time()
        ns = build_biometric_namespace(frames=[_frame(now=now)], now=now)
        published = set(ns) | set(describe_namespace())
        for invented in ("hrv", "sleep", "respiratory", "stress_score", "recovery"):
            assert not any(invented in key for key in published), invented

    def test_documented_fields_cover_everything_published(self):
        now = time.time()
        ns = build_biometric_namespace(
            frames=[_frame(now=now, spo2_pct=97, spo2_sample_ts=now,
                           spo2_source="jw_health_glasses",
                           skin_temperature_c=33.4)],
            now=now,
        )
        assert set(ns) <= set(describe_namespace())


# ---------------------------------------------------------------------------
# Baseline attachment, against a copy of the operator's real database
# ---------------------------------------------------------------------------

def _real_baselines_copy(tmp_path) -> str | None:
    """Copy ~/.feral/baselines.db so the running brain's DB is never opened."""
    src = Path(os.path.expanduser("~/.feral/baselines.db"))
    if not src.exists():
        return None
    dst = tmp_path / "baselines_copy.db"
    dst.write_bytes(src.read_bytes())
    return str(dst)


class TestBaselineAttachment:
    def test_baseline_stats_are_read_only(self, tmp_path):
        """Building the namespace must not persist a baseline_alert row.

        ``check_anomaly`` writes a row and fans out to the IdeasEngine on
        every call; operator report 2026-06-07 is 78 duplicate alerts from
        exactly that. A namespace builder runs every 15s, so it uses
        ``get_baseline`` and does the arithmetic itself.
        """
        from agents.baseline_engine import BaselineEngine

        db = tmp_path / "baselines.db"
        engine = BaselineEngine(db_path=str(db))
        for value in (80, 81, 82, 80, 83, 81, 82):
            engine.record("hr_resting:jw_health_glasses", float(value), category="health")
        before = engine._conn.execute(
            "SELECT count(*) FROM baseline_alerts"
        ).fetchone()[0]

        now = time.time()
        ns = build_biometric_namespace(
            frames=[_frame(now=now, heart_rate=171)],
            baseline_engine=engine,
            now=now,
        )
        after = engine._conn.execute(
            "SELECT count(*) FROM baseline_alerts"
        ).fetchone()[0]

        assert after == before
        assert ns["biometric.heart_rate_baseline_mean"] > 0
        assert ns["biometric.heart_rate_deviation_sigma"] > 3

    def test_against_real_install_data(self, tmp_path):
        """The shipped condition must be False on the operator's real data."""
        db_path = _real_baselines_copy(tmp_path)
        if db_path is None:
            pytest.skip("no ~/.feral/baselines.db on this machine")

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT ts, source, value FROM biometric_samples "
            "WHERE metric = 'hr' ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row is None:
            pytest.skip("no hr samples recorded")
        _ts, source, value = row

        now = time.time()
        frame = _frame(
            now=now,
            heart_rate=int(value),
            heart_rate_sample_ts=now,
            heart_rate_source=source,
        )
        ns = build_biometric_namespace(frames=[frame], now=now)
        result = evaluate_condition(_shipped_trigger()["condition"], ns)
        # 82 bpm resting. If this ever asserts the other way the manifest
        # trigger is firing on a genuinely elevated real reading.
        assert result.satisfied is False
        assert ns["biometric.heart_rate_bpm"] == int(value)


# ---------------------------------------------------------------------------
# ProactiveEngine wiring
# ---------------------------------------------------------------------------

def _registry_with(skill_id: str, condition: str, cooldown: int = 1800,
                   trigger_id: str = "high_stress_alert"):
    manifest = SimpleNamespace(
        skill_id=skill_id,
        triggers=[SimpleNamespace(
            id=trigger_id,
            condition=condition,
            action_flow_id="send_and_confirm",
            action_endpoint_id=None,
            cooldown_seconds=cooldown,
        )],
    )
    return SimpleNamespace(skills={skill_id: manifest})


def _engine_with(registry, frame: PerceptionFrame | None) -> ProactiveEngine:
    perception = MagicMock()
    perception._frames = {"s": frame} if frame is not None else {}
    perception.get_frame = lambda _sid: frame
    engine = ProactiveEngine(perception=perception, skill_registry=registry)
    engine._first_interaction_today = False
    return engine


class TestProactiveWiring:
    def test_trusted_manifest_trigger_fires_on_true_condition(self):
        now = time.time()
        registry = _registry_with("messaging_sms", _shipped_trigger()["condition"])
        frame = _frame(now=now, heart_rate=171, activity_state="stressed")
        engine = _engine_with(registry, frame)

        messages: list = []
        engine._evaluate_manifest_triggers([frame], messages, now)

        assert len(messages) == 1
        msg = messages[0]
        assert msg.trigger_id == "manifest:messaging_sms:high_stress_alert"
        assert msg.priority is Priority.IMPORTANT
        # Notification only: an empty action_payload is what keeps
        # ``_deliver`` from calling ``_execute_automation``.
        assert msg.action_payload == {}
        assert "NOT run" in msg.body
        assert "send_and_confirm" in msg.body

    def test_false_condition_produces_no_message(self):
        now = time.time()
        registry = _registry_with("messaging_sms", _shipped_trigger()["condition"])
        frame = _frame(now=now, heart_rate=82, activity_state="resting")
        engine = _engine_with(registry, frame)

        messages: list = []
        engine._evaluate_manifest_triggers([frame], messages, now)
        assert messages == []

    def test_unparseable_condition_produces_no_message_and_warns(self, caplog):
        now = time.time()
        registry = _registry_with("messaging_sms", "biometric.heart_rate_bpm !!! 1")
        frame = _frame(now=now, heart_rate=171, activity_state="stressed")
        engine = _engine_with(registry, frame)

        messages: list = []
        with caplog.at_level(logging.WARNING, logger="feral.triggers"):
            engine._evaluate_manifest_triggers([frame], messages, now)
        assert messages == []
        assert [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_untrusted_manifest_is_ignored(self):
        """Only manifests shipping in skills/manifests/ are honoured."""
        now = time.time()
        registry = _registry_with("some_marketplace_skill", "biometric.heart_rate_bpm > 0")
        frame = _frame(now=now, heart_rate=171)
        engine = _engine_with(registry, frame)

        messages: list = []
        engine._evaluate_manifest_triggers([frame], messages, now)
        assert messages == []

    def test_cooldown_seconds_is_honoured(self):
        now = time.time()
        registry = _registry_with(
            "messaging_sms", _shipped_trigger()["condition"], cooldown=1800
        )
        frame = _frame(now=now, heart_rate=171, activity_state="stressed")
        engine = _engine_with(registry, frame)
        tid = "manifest:messaging_sms:high_stress_alert"

        first: list = []
        engine._evaluate_manifest_triggers([frame], first, now)
        assert len(first) == 1
        assert engine._trigger_states[tid].cooldown_s == 1800

        engine._record_fire(tid)

        second: list = []
        engine._evaluate_manifest_triggers([frame], second, now)
        assert second == [], "manifest cooldown_seconds was not honoured"

        # ...and it fires again once the manifest's own window has passed.
        engine._trigger_states[tid].last_fired = time.time() - 1801
        third: list = []
        engine._evaluate_manifest_triggers([frame], third, now)
        assert len(third) == 1

    def test_cooldown_does_not_override_dismiss_backoff(self):
        now = time.time()
        registry = _registry_with(
            "messaging_sms", _shipped_trigger()["condition"], cooldown=60
        )
        frame = _frame(now=now, heart_rate=171, activity_state="stressed")
        engine = _engine_with(registry, frame)
        tid = "manifest:messaging_sms:high_stress_alert"

        engine._evaluate_manifest_triggers([frame], [], now)
        engine.record_dismiss(tid)
        backed_off = engine._trigger_states[tid].cooldown_s
        assert backed_off > 60

        engine._evaluate_manifest_triggers([frame], [], now)
        assert engine._trigger_states[tid].cooldown_s == backed_off

    def test_no_registry_is_a_noop(self):
        engine = _engine_with(None, _frame())
        messages: list = []
        engine._evaluate_manifest_triggers([], messages, time.time())
        assert messages == []

    def test_stale_reading_does_not_fire(self):
        """An hours-old 171 bpm is history, not an event."""
        now = time.time()
        registry = _registry_with("messaging_sms", _shipped_trigger()["condition"])
        frame = _frame(
            now=now, heart_rate=171, activity_state="stressed",
            heart_rate_sample_ts=now - 7200,
        )
        engine = _engine_with(registry, frame)
        messages: list = []
        engine._evaluate_manifest_triggers([frame], messages, now)
        assert messages == []

    def test_full_evaluate_tick_delivers_without_executing(self):
        """End to end through ``_evaluate``: notify, never actuate."""
        now = time.time()
        registry = _registry_with("messaging_sms", _shipped_trigger()["condition"])
        frame = _frame(now=now, heart_rate=171, activity_state="stressed")
        engine = _engine_with(registry, frame)

        delivered: list = []
        executed: list = []

        async def _capture(msg):
            delivered.append(msg)

        async def _automation(msg):  # pragma: no cover - must never run
            executed.append(msg)

        engine.on_message(_capture)
        engine._execute_automation = _automation  # type: ignore[assignment]

        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            engine._evaluate()
        )

        ids = [m.trigger_id for m in delivered]
        assert "manifest:messaging_sms:high_stress_alert" in ids
        # The hardcoded `hr_elevated` trigger also fires at 171 bpm and it
        # does carry an action_payload (scene.calming), so the assertion is
        # scoped: nothing with a manifest: prefix may reach the actuator.
        assert not [
            m for m in executed
            if m.trigger_id.startswith(ProactiveEngine.MANIFEST_TRIGGER_PREFIX + ":")
        ], "manifest triggers must not reach an actuator"

    def test_bad_manifest_does_not_break_the_tick(self, caplog):
        """A registry that raises is logged loudly, not swallowed."""
        class Exploding:
            @property
            def skills(self):
                raise RuntimeError("registry is on fire")

        engine = _engine_with(Exploding(), None)
        with caplog.at_level(logging.WARNING, logger="feral.proactive"):
            asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
                engine._evaluate()
            )
        assert any(
            "Manifest trigger evaluation failed" in r.getMessage()
            for r in caplog.records
        )
