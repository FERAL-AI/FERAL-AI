"""An important alert must reach a human with no browser open.

Every proactive decision went to broadcast_event, which iterates
self.sessions and nothing else. With no tab open the message was
destroyed: not queued, not retried, not stored. This install generated
2,441 of them, most recently "Time for a Break" at 16:32, and the user
saw only whichever ones he happened to be looking at.

The reason escalation is safe to switch on is the priority gate, and the
threshold comes from the real distribution rather than taste. Of those
2,441 fires:

    focus_break     1783   SUGGESTION
    break_reminder   601   SUGGESTION
    screen_error      18   SUGGESTION
    hr_elevated       17   IMPORTANT
    baseline_hr       15   IMPORTANT

Forwarding everything would have sent 2,384 break nags, which is how an
app teaches someone to mute it. Gating at IMPORTANT forwards the 32 that
concern the user's body. These tests pin that split, because the value of
the feature is entirely in what it refuses to send.
"""

from __future__ import annotations

import asyncio

import pytest

from agents.proactive_engine import Priority


class FakeChannel:
    channel_type = "telegram"

    def __init__(self, chats=("chat-1",)):
        self._chats = list(chats)
        self.sent: list[tuple[str, str]] = []

    def active_chat_ids(self):
        return list(self._chats)

    async def send_direct(self, to, text, reply_to=None):
        self.sent.append((to, text))
        return {"ok": True}


class FakeManager:
    def __init__(self, channel=None):
        self._channel = channel

    def active_channels(self):
        return ["telegram"] if self._channel else []

    def get_channel(self, channel_type):
        return self._channel if channel_type == "telegram" else None


class Msg:
    def __init__(self, trigger_id, priority, title="T", body="B"):
        self.trigger_id = trigger_id
        self.priority = priority
        self.title = title
        self.body = body


@pytest.fixture
def brain():
    """A BrainState with only the attributes escalation touches."""
    from api.state import BrainState

    b = BrainState.__new__(BrainState)
    b.sessions = {}
    b._escalation_last_sent = {}
    b.config = None
    b.channel_manager = None
    return b


def _escalate(brain, msg):
    return asyncio.run(brain._escalate_when_nobody_is_watching(msg))


class TestTheGateRefusesNoise:
    @pytest.mark.parametrize("trigger", ["focus_break", "break_reminder", "screen_error"])
    def test_suggestions_are_never_escalated(self, brain, trigger):
        """2,384 of 2,441 real fires. Forwarding these is the failure mode."""
        ch = FakeChannel()
        brain.channel_manager = FakeManager(ch)

        _escalate(brain, Msg(trigger, Priority.SUGGESTION))

        assert ch.sent == [], f"{trigger} was escalated"

    def test_ambient_is_never_escalated(self, brain):
        ch = FakeChannel()
        brain.channel_manager = FakeManager(ch)
        _escalate(brain, Msg("daily_summary", Priority.AMBIENT))
        assert ch.sent == []


class TestTheGateForwardsWhatMatters:
    @pytest.mark.parametrize("trigger", ["hr_elevated", "baseline_hr"])
    def test_important_health_signals_are_escalated(self, brain, trigger):
        ch = FakeChannel()
        brain.channel_manager = FakeManager(ch)

        _escalate(brain, Msg(trigger, Priority.IMPORTANT, title="Heart rate high"))

        assert len(ch.sent) == 1
        to, text = ch.sent[0]
        assert to == "chat-1"
        assert "Heart rate high" in text

    def test_critical_is_escalated(self, brain):
        ch = FakeChannel()
        brain.channel_manager = FakeManager(ch)
        _escalate(brain, Msg("spo2_low", Priority.CRITICAL))
        assert len(ch.sent) == 1


class TestItDoesNotDuplicateOrRepeat:
    def test_an_open_browser_session_suppresses_escalation(self, brain):
        """The websocket push already delivered it; a second copy on the
        phone is noise, and noise is what gets the feature turned off."""
        ch = FakeChannel()
        brain.channel_manager = FakeManager(ch)
        brain.sessions = {"web-1": object()}

        _escalate(brain, Msg("hr_elevated", Priority.IMPORTANT))

        assert ch.sent == []

    def test_the_same_trigger_is_not_sent_twice_in_the_cooldown(self, brain):
        """hr_elevated re-fires for as long as the rate stays up, and a real
        warning repeated every fifteen seconds reads as spam."""
        ch = FakeChannel()
        brain.channel_manager = FakeManager(ch)

        for _ in range(5):
            _escalate(brain, Msg("hr_elevated", Priority.IMPORTANT))

        assert len(ch.sent) == 1

    def test_a_different_trigger_is_not_blocked_by_the_cooldown(self, brain):
        ch = FakeChannel()
        brain.channel_manager = FakeManager(ch)

        _escalate(brain, Msg("hr_elevated", Priority.IMPORTANT))
        _escalate(brain, Msg("spo2_low", Priority.CRITICAL))

        assert len(ch.sent) == 2


class TestUndeliverableIsLoudNotSilent:
    def test_no_channel_configured_warns_and_names_the_setting(self, brain, caplog):
        """Dropping it quietly here would rebuild the original bug one layer
        further down."""
        brain.channel_manager = FakeManager(None)

        with caplog.at_level("WARNING"):
            _escalate(brain, Msg("hr_elevated", Priority.IMPORTANT))

        assert "nowhere to go" in caplog.text
        assert "escalate_to" in caplog.text

    def test_that_warning_is_also_rate_limited(self, brain, caplog):
        brain.channel_manager = FakeManager(None)
        with caplog.at_level("WARNING"):
            for _ in range(4):
                _escalate(brain, Msg("hr_elevated", Priority.IMPORTANT))

        assert caplog.text.count("nowhere to go") == 1

    def test_a_failing_send_does_not_raise_into_the_engine(self, brain, caplog):
        class Broken(FakeChannel):
            async def send_direct(self, to, text, reply_to=None):
                raise RuntimeError("telegram is down")

        brain.channel_manager = FakeManager(Broken())
        with caplog.at_level("WARNING"):
            _escalate(brain, Msg("hr_elevated", Priority.IMPORTANT))

        assert "telegram is down" in caplog.text

    def test_a_failed_send_does_not_consume_the_cooldown(self, brain):
        """Otherwise one transient outage silences the next half hour."""
        class Broken(FakeChannel):
            async def send_direct(self, to, text, reply_to=None):
                raise RuntimeError("down")

        brain.channel_manager = FakeManager(Broken())
        _escalate(brain, Msg("hr_elevated", Priority.IMPORTANT))
        assert brain._escalation_last_sent.get("hr_elevated") in (None, 0.0)


class TestNoLLMCost:
    def test_escalation_sends_the_already_composed_message(self, brain):
        """The engine composed title and body already. Escalation must not
        reach for a model: cost per alert is the other way to make a
        notification layer something people switch off."""
        ch = FakeChannel()
        brain.channel_manager = FakeManager(ch)

        _escalate(brain, Msg("hr_elevated", Priority.IMPORTANT,
                             title="Heart rate high", body="128 bpm at rest"))

        _, text = ch.sent[0]
        assert text == "Heart rate high\n\n128 bpm at rest"
