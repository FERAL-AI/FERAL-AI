"""What a voice session would REALLY put on the wire.

``tests/test_voice_capability_parity.py`` proves ``cap_tools_with_pins``
picks the right tools and that ``truncation_notice`` writes the right
sentence. Both are unit tests of helpers. The one test in that file that
tries to check the helpers are actually WIRED reads the source of
``voice/realtime_proxy.py`` and greps it for ``truncation_notice(`` and
``instructions=instructions``. That is a test of the text of the file,
not of the behaviour of the program, and it passes for a program in
which the notice is computed, is genuinely assigned to ``instructions``,
and comes out empty every single time.

Which is what was happening. ``RealtimeProxy._get_tools()`` capped the
registry to 128 and handed THAT to ``RealtimeSession``, so by the time
``configure()`` computed ``truncation_notice(self._tools, capped)`` it
was comparing a 128-item list with itself, ``len(capped) >= len(tools)``
was true, and the notice was "". Both greps still matched. The model was
never told its list had been cut, which is the exact silence the notice
exists to break.

So this module does not read the source and does not call the helpers.
It boots a real ``RealtimeProxy`` over the real ``SkillRegistry``, lets
the real ``session.created`` event drive the real ``configure()``, and
inspects the JSON frame that the session handed to the transport. The
only thing replaced is the socket to OpenAI: nothing here needs a
network and nothing here spends a token.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from agents.tool_list import (
    OPENAI_TOOL_HARD_LIMIT,
    skill_id_from_tool_name,
    tool_name_from_def,
)
from skills import availability
from skills.availability import filter_unavailable_tools


#: Every await in this module is against an in-memory fake, so anything
#: that does not settle almost immediately is wedged, not slow. Bounded
#: because a voice test that hangs takes the whole session with it, the
#: way the first in-process route sweep did (see
#: tests/test_every_route_answers.py).
WIRE_TIMEOUT_S = 10.0


class _FakeOpenAIRealtimeEndpoint:
    """Stands in for the OpenAI Realtime websocket, and nothing else.

    ``RealtimeSession`` touches its transport in exactly three ways:
    ``send`` (from ``_send``), async iteration (from ``_receive_loop``)
    and ``close`` (from ``disconnect``). Implementing those three is
    enough to run the real session end to end, so the cap, the GA tool
    reshaping, the tool_choice resolution, the truncation notice and the
    JSON serialisation are all the production code paths.

    Iteration yields the scripted inbound frames and then parks on
    ``_closed`` rather than raising. Raising would look to
    ``_receive_loop`` like the socket died, which flips ``_connected``
    False and routes into the error/fallback path: a session that has
    torn itself down is not the session under test.
    """

    def __init__(self, inbound: list[dict] | None = None):
        self.sent: list[dict] = []
        self._inbound = list(inbound or [])
        # Set once the receive loop has consumed every scripted frame,
        # which by definition is after the last handler returned. That
        # is the signal that ``configure()`` has finished, without a
        # sleep and without polling.
        self.drained = asyncio.Event()
        self._closed = asyncio.Event()

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def close(self) -> None:
        self._closed.set()

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if self._inbound:
            return json.dumps(self._inbound.pop(0))
        self.drained.set()
        await self._closed.wait()
        raise StopAsyncIteration

    def frames_of_type(self, wire_type: str) -> list[dict]:
        return [f for f in self.sent if f.get("type") == wire_type]

    async def wait_drained(self) -> None:
        await asyncio.wait_for(self.drained.wait(), timeout=WIRE_TIMEOUT_S)


@pytest.fixture(scope="module")
def live_tools() -> list[dict]:
    """The real registry. The point of the whole exercise.

    A synthetic tool list cannot reproduce this bug: the bug is that the
    real registry produces more tools than the cap allows and that the
    difference was being hidden. A fixture that hands over 12 fake tools
    is under the cap and proves nothing.
    """
    from skills.registry import SkillRegistry

    registry = SkillRegistry()
    registry.load_builtin_skills()
    tools = registry.get_all_tools()
    if not tools:
        pytest.skip("no skills registered in this environment")
    return tools


class _ToolsOnlyRegistry:
    """The registry surface ``RealtimeProxy`` actually consumes.

    ``RealtimeProxy`` calls exactly one method on its skill registry,
    ``get_all_tools()``. Wrapping the real tool list rather than passing
    the real ``SkillRegistry`` keeps the module-scoped load from being
    repeated per test while leaving the data genuinely real.
    """

    def __init__(self, tools: list[dict]):
        self._tools = tools

    def get_all_tools(self, *, offerable_only: bool = False) -> list[dict]:
        # ``offerable_only`` is accepted and ignored: the availability
        # gate (skills/availability.py) is exercised in
        # tests/test_tool_availability_gate.py, and this file is about
        # what reaches the wire, so it wants the whole list either way.
        return list(self._tools)


async def _configured_session(live_tools, monkeypatch, *, inbound=None):
    """Boot a real proxy, open a real session, return (session, endpoint).

    The inbound script defaults to a single ``session.created``, because
    that is what OpenAI sends first and it is what makes the session
    configure itself: ``_handle_event`` calls ``configure()`` on it. So
    the frame under test is produced by the same trigger production uses,
    not by a test reaching in and calling ``configure`` by hand.

    The caller must ``disconnect()`` the session. The fixture below does.
    """
    from voice.realtime_proxy import RealtimeProxy, RealtimeSession

    endpoint = _FakeOpenAIRealtimeEndpoint(
        inbound if inbound is not None else [{"type": "session.created"}]
    )

    async def _fake_connect(url, **kwargs):
        return endpoint

    monkeypatch.setattr(
        RealtimeSession, "_connect_with_retry", staticmethod(_fake_connect)
    )

    proxy = RealtimeProxy(skill_registry=_ToolsOnlyRegistry(live_tools))
    # The only credential this test needs is a non-empty one: `connect`
    # refuses to open a socket without a key, and the fake endpoint
    # never looks at it.
    proxy._api_key = "sk-not-a-real-key"

    session = await asyncio.wait_for(
        proxy.start_session("sess-wire", "node-wire"), timeout=WIRE_TIMEOUT_S
    )
    assert session is not None, "start_session refused to open a session"
    await endpoint.wait_drained()
    return session, endpoint


@pytest.fixture()
async def wire(live_tools, monkeypatch):
    """A configured session plus the frames it put on the wire."""
    session, endpoint = await _configured_session(live_tools, monkeypatch)
    try:
        updates = endpoint.frames_of_type("session.update")
        assert updates, "the session never configured itself"
        yield session, endpoint, updates[0]["session"]
    finally:
        await asyncio.wait_for(session.disconnect(), timeout=WIRE_TIMEOUT_S)


# ── the frame itself ────────────────────────────────────────────────


class TestTheFrameAVoiceSessionReallySends:
    """Assertions about the wire payload, not about the helpers that
    build it. Each one is something the OpenAI Realtime API would
    enforce on a live call and that no other test in this repo can see,
    because every other test stops at the helper's return value."""

    async def test_the_session_configures_itself_on_session_created(self, wire):
        """The trigger, not the call.

        Nothing in production calls ``configure()`` directly; it is
        reached only from ``_handle_event`` on ``session.created``. A
        test that calls it by hand would still pass if that dispatch
        were deleted.
        """
        _session, _endpoint, sess = wire
        assert sess["type"] == "realtime"

    async def test_the_frame_survives_a_round_trip_through_json(self, wire):
        """The transport serialises with ``json.dumps``. A tool schema
        carrying anything non-serialisable raises inside ``_send``,
        which swallows it as a send error and kills the session with no
        model output at all."""
        _session, _endpoint, sess = wire
        assert json.loads(json.dumps(sess)) == sess

    async def test_the_wire_never_carries_more_than_the_cap(self, wire):
        """128 is the API's number, not ours. Over it, the whole
        session.update is rejected and the session is left unconfigured
        with no tools rather than with too many."""
        _session, _endpoint, sess = wire
        assert len(sess["tools"]) <= OPENAI_TOOL_HARD_LIMIT

    async def test_every_tool_on_the_wire_is_the_ga_flat_shape(self, wire):
        """Realtime GA wants ``{"type","name","description","parameters"}``
        flat. The registry produces the chat/completions nested shape
        with the name under ``function``. Sending the nested shape is
        accepted-looking and yields a session whose tools are all
        unnamed."""
        _session, _endpoint, sess = wire
        for tool in sess["tools"]:
            assert tool["type"] == "function"
            assert tool["name"], f"tool with no name on the wire: {tool}"
            assert "function" not in tool
            assert isinstance(tool["parameters"], dict)

    async def test_the_spoken_sentence_has_a_tool_on_the_wire(self, wire):
        """The original report: "open <url> in chrome" spoken did
        nothing while typed it worked. This is that sentence's landing
        site, checked on the payload rather than on the cap helper."""
        _session, _endpoint, sess = wire
        names = {t["name"] for t in sess["tools"]}
        assert "desktop_control__open_url" in names

    async def test_no_available_skill_is_invisible_on_the_wire(
        self, wire, live_tools
    ):
        """The wider bug. A skill with zero tools in the frame cannot be
        reached and cannot be reasoned about, so the model reports the
        BRAIN as incapable of something it does daily in chat.

        The property is about skills that CAN work. Since
        ``skills/availability.py``, a skill whose prerequisite is absent
        (no key, not connected, no Docker, no device) is withheld on
        purpose, and it is withheld in chat too, so its absence here is
        not the voice/text divergence this test was written for. The
        model saying "I cannot reach email" about an unconnected mailbox
        is true. The next test is what keeps it from being the only thing
        the model knows.
        """
        _session, _endpoint, sess = wire
        on_wire = {skill_id_from_tool_name(t["name"]) for t in sess["tools"]}
        offerable = {
            skill_id_from_tool_name(tool_name_from_def(t))
            for t in filter_unavailable_tools(live_tools)
        }
        missing = sorted(offerable - on_wire)
        assert not missing, f"these usable skills are invisible on voice: {missing}"

    async def test_a_withheld_skill_is_named_in_the_instructions(
        self, live_tools, monkeypatch
    ):
        """Withholding without explaining is the same bug in a new place.

        A capability that vanishes from the tool list with no reason
        attached is one the model will report as missing from the BRAIN
        rather than from the setup, which is the exact operator-facing
        failure this module exists to prevent. So every skill the gate
        withheld has to be named, with why, in the prompt that actually
        went over the wire.

        The verdicts are pinned rather than read from the ambient
        environment. Whether this machine has a Docker binary or a
        connected mailbox decides what the real gate withholds, and a
        test that skips itself on the developer's laptop and only bites
        in CI is not a test of anything.
        """
        withheld = {
            "email": "Email: not connected",
            "cutebot": "CuteBot: not plugged in",
        }
        monkeypatch.setattr(
            availability, "unavailable_skills", lambda **_kw: dict(withheld),
        )

        session, endpoint = await _configured_session(live_tools, monkeypatch)
        try:
            sess = endpoint.frames_of_type("session.update")[-1]["session"]
            on_wire = {skill_id_from_tool_name(t["name"]) for t in sess["tools"]}
            instructions = sess["instructions"]

            for skill_id, reason in withheld.items():
                assert skill_id not in on_wire, (
                    f"{skill_id} was withheld yet still reached the wire"
                )
                assert reason in instructions, (
                    f"{skill_id} was withheld with no explanation in the prompt"
                )
            assert (
                "do not claim feral lacks the capability" in instructions.lower()
            )
        finally:
            await asyncio.wait_for(session.disconnect(), timeout=WIRE_TIMEOUT_S)

    async def test_tool_choice_is_a_value_the_api_accepts(self, wire):
        """``auto`` for an unforced session. A forced turn sends the GA
        object form, covered below."""
        _session, _endpoint, sess = wire
        assert sess["tool_choice"] == "auto"

    async def test_the_frame_carries_no_field_ga_rejects(self, wire):
        """``session.temperature`` was removed in GA and a live call
        answered "Unknown parameter: 'session.temperature'", which
        silently broke configuration so the model never spoke. There is
        a comment about it in the source; this is the assertion."""
        _session, _endpoint, sess = wire
        assert "temperature" not in sess


# ── the silence the notice exists to break ──────────────────────────


class TestTheTruncationNoticeReachesTheWire:
    """The bug this module was written for.

    Every part of the notice mechanism was correct in isolation and the
    notice still reached the model as an empty string, because the list
    handed to the session had already been capped by the proxy. The only
    place that is visible is the instructions field of the frame.
    """

    async def test_the_instructions_actually_sent_carry_the_notice(self, wire):
        """The load-bearing assertion.

        Red before the ``_get_tools`` fix with ``assert '## Voice Tool
        Limit' in instructions`` failing on an instructions string that
        ended at the system prompt.
        """
        _session, _endpoint, sess = wire
        assert "## Voice Tool Limit" in sess["instructions"]

    async def test_the_notice_names_the_real_totals_not_the_capped_one(
        self, wire, live_tools
    ):
        """The number that matters is how many tools were available to cap.

        Had the notice been computed from the pre-capped list it would
        have read "128 of the 128 tools available to you right now",
        which is both false and useless. Asserting the real pre-cap total
        appears is what distinguishes a correct notice from a
        self-referential one.

        That total is the OFFERED set, not the installed one. The two
        differ by the skills ``skills/availability.py`` withheld, and
        folding those in here would make the notice actively wrong: its
        remedy is "ask me in chat, where the full set is available", and
        chat cannot reach an unauthorised Notion either. Those are named
        separately, with their reasons, and asserted above.
        """
        _session, _endpoint, sess = wire
        instructions = sess["instructions"]
        offered = filter_unavailable_tools(live_tools)
        assert str(len(offered)) in instructions
        assert str(len(sess["tools"])) in instructions

    async def test_the_notice_tells_the_model_absence_is_not_incapability(
        self, wire
    ):
        """Without this sentence the model answers "I can't do that"
        instead of "I can't reach that from voice, ask me in chat", and
        the operator hears the brain deny a capability it has."""
        _session, _endpoint, sess = wire
        assert "does NOT mean the brain cannot do it" in sess["instructions"]

    async def test_the_notice_is_appended_to_the_prompt_not_swapped_for_it(
        self, wire
    ):
        """``instructions`` is one field carrying both the personality
        block and the notice. Appending to an empty string would have
        satisfied every assertion above while deleting the system
        prompt."""
        _session, _endpoint, sess = wire
        instructions = sess["instructions"]
        assert instructions.index("## Voice Tool Limit") > 0
        head = instructions.split("## Voice Tool Limit")[0]
        assert len(head.strip()) > 100, "the system prompt is missing"

    async def test_the_session_is_given_the_uncapped_list_to_measure_against(
        self, live_tools, monkeypatch
    ):
        """The root cause, stated directly.

        ``configure()`` caps before sending, so handing the session the
        full list costs nothing on the wire and is the only way the
        session can know how much was dropped. Pre-capping in
        ``RealtimeProxy._get_tools`` made the two lists identical and the
        difference unmeasurable.
        """
        session, endpoint = await _configured_session(live_tools, monkeypatch)
        try:
            # Uncapped, but gated: the session measures the cap's damage
            # against what it could have offered, which is the registry
            # minus the skills whose prerequisite is absent. Pre-capping
            # is the bug; pre-gating is the point.
            assert len(session._tools) == len(filter_unavailable_tools(live_tools))
            assert len(session._tools) > OPENAI_TOOL_HARD_LIMIT, (
                "the gated list must still overflow the cap, or this file "
                "is no longer testing truncation at all"
            )
        finally:
            await asyncio.wait_for(session.disconnect(), timeout=WIRE_TIMEOUT_S)


# ── forcing a tool for one turn ─────────────────────────────────────


class TestForcingAToolGoesOverTheSameWire:
    """``force_tool_for_turn`` resolves against the capped list, and it
    has to: naming a tool the session never declared makes OpenAI answer
    with an ``error`` event, which used to collapse the call."""

    async def test_a_tool_the_session_declared_can_be_forced(self, wire):
        session, endpoint, sess = wire
        declared = sess["tools"][0]["name"]
        before = len(endpoint.sent)

        await asyncio.wait_for(
            session.force_tool_for_turn(declared), timeout=WIRE_TIMEOUT_S
        )

        forced = endpoint.frames_of_type("session.update")[-1]["session"]
        assert forced["tool_choice"] == {"type": "function", "name": declared}
        assert len(endpoint.sent) > before

    async def test_a_tool_the_cap_evicted_degrades_to_auto(self, wire):
        """Not an error, and not silence: the turn still gets answered,
        it just is not pinned. A name the session did not declare would
        be rejected by the API instead."""
        session, endpoint, _sess = wire

        await asyncio.wait_for(
            session.force_tool_for_turn("nothing__like_this_is_registered"),
            timeout=WIRE_TIMEOUT_S,
        )

        forced = endpoint.frames_of_type("session.update")[-1]["session"]
        assert forced["tool_choice"] == "auto"

    async def test_the_forced_name_is_always_one_the_frame_declared(self, wire):
        """The invariant behind both cases above, checked against the
        declared list rather than against the helper's opinion of it."""
        session, endpoint, sess = wire
        declared = {t["name"] for t in sess["tools"]}

        for candidate in ("desktop_control__open_url", "not__a_tool"):
            await asyncio.wait_for(
                session.force_tool_for_turn(candidate), timeout=WIRE_TIMEOUT_S
            )
            choice = endpoint.frames_of_type("session.update")[-1]["session"][
                "tool_choice"
            ]
            if isinstance(choice, dict):
                assert choice["name"] in declared
            else:
                assert choice == "auto"
            # force_tool_for_turn is a one-shot and refuses to re-force a
            # tool already run this turn; clear so the loop's second pass
            # exercises the resolution rather than the skip.
            session._turn_tools_executed.clear()
