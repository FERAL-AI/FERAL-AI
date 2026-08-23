"""One ambient conversation, all the way through a real brain.

The sweep itself lives in ``scripts_audit/ambient_ws_sweep.py`` and is
run here in a SUBPROCESS, for the same two reasons
``tests/test_every_route_answers.py`` does it:

1. A websocket receive that never returns would otherwise hang the whole
   pytest session, and a test that can hang the suite is worse than no
   test.
2. The sweep boots the real app with its own ``FERAL_HOME``, and doing
   that inside a session where ``conftest`` has already initialised
   ``api.state.state`` produced a deadlock on lifespan teardown that had
   nothing to do with anything under test.

It also has a third reason of its own: ``api/server.py`` reads
``NODE_API_KEY`` from the environment at MODULE IMPORT time, so a test
that sets it after the import has set nothing at all.

What this covers that nothing else does
=======================================
``tests/test_ambient_transcript_frame.py`` calls
``_handle_ambient_transcript`` directly, with a hand-written fake socket
and a stubbed ``state``. ``tests/test_ambient_conversation_recall.py``
calls ``_ambient_store`` and ``list_conversations`` as plain functions.
Both are good tests and neither one connects to anything, so neither can
see the seams, and both reported defects on this surface WERE seams:

* the transcripts were stored and the agent could not reach them,
  because the writer (``api/server.py``) and the reader
  (``memory/ambient_conversations.py``) agree about the schema only by
  hand and by comment;
* ``hrv`` / ``skin_temperature`` / ``steps`` were dropped between a
  writer and a reader that disagreed about a key name.

A seam is exactly what a test that mocks one side of it cannot see. So
the sweep authenticates a real bearer against ``/v1/node``, sends a real
frame through ``parse_message`` and ``daemon_session``, waits for the
real detached summariser, and then asks the real skill registry the
question the operator asked the agent: is there any ambient conversation
recorded.

Determinism: the sweep detaches the LLM after boot rather than relying
on an unset ``OPENAI_API_KEY``, because the provider falls back to a
local Ollama when one happens to be running, and on the machine this was
written on one was. It also pins ``started_at`` to a fixed epoch instead
of ``now``, so nothing here depends on the wall clock or the timezone.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


FERAL_CORE = Path(__file__).resolve().parents[1]
SWEEP = FERAL_CORE / "scripts_audit" / "ambient_ws_sweep.py"

#: The sweep boots a brain, drives a socket and waits on a background
#: summariser. Generous, because a cold first boot builds a vault, an
#: embedding queue and an app registry; still bounded, because the whole
#: point is that it can never hang the suite.
#:
#: Measured: ~6s when the chain is intact, ~80s with the chain severed
#: at the first link (every bounded wait runs to its end). The headroom
#: is deliberate. If this ceiling is ever hit, the sweep is reported as
#: timed out and the per-link diagnosis below is lost, which is the
#: worst outcome short of no test at all.
SWEEP_TIMEOUT_S = 300

#: Bearer the sweep's node presents. Any non-empty value works: what is
#: under test is that the gate accepts a matching one, not the secret.
SWEEP_NODE_KEY = "ambient-sweep-key"

#: Every check the sweep is expected to report, and the guard that a
#: check added to the script cannot sit there unasserted. The failure
#: mode this prevents is quiet: a new link in the chain gets a check,
#: nobody writes the test for it, and the sweep reports it as failing
#: while the suite stays green.
EXPECTED_CHECKS = (
    "the_socket_authenticates_and_registers",
    "a_real_frame_is_acked_over_the_real_socket",
    "the_first_send_is_not_reported_as_a_duplicate",
    "a_resend_is_acked_as_a_duplicate_not_stored_twice",
    "the_finished_digest_is_pushed_to_the_node_that_is_still_here",
    "the_digest_becomes_retrievable_over_the_socket",
    "the_digest_summarises_the_words_that_were_sent",
    "the_digest_carries_the_full_transcript_when_asked",
    "the_digest_names_the_speakers_the_phone_reported",
    "the_digest_says_it_ran_without_a_model",
    "an_unstored_id_answers_unknown_with_the_same_key_set",
    "the_recall_tool_is_registered_under_the_name_the_prompt_uses",
    "the_recall_tool_answers_without_erroring",
    "the_skill_the_agent_calls_can_see_the_conversation",
    "the_skill_reports_it_as_ready_not_pending",
    "the_skill_hands_the_model_the_same_summary_the_phone_got",
    "every_column_the_reader_selects_is_one_the_writer_creates",
    "the_reader_and_the_writer_open_the_same_file",
)


def test_the_sweep_script_is_present():
    """A test that shells out to a missing script passes vacuously."""
    assert SWEEP.is_file(), f"ambient websocket sweep missing at {SWEEP}"


@pytest.fixture(scope="module")
def sweep(tmp_path_factory):
    """Run the sweep once and hand every test its own check.

    Module-scoped because booting a brain costs seconds and the sweep is
    a single linear conversation: splitting it into one subprocess per
    assertion would multiply that cost by eighteen and test the same
    chain eighteen times.
    """
    from scripts_audit.ambient_ws_sweep import RESULT_SENTINEL

    home = tmp_path_factory.mktemp("ambient-sweep-home")

    env = dict(os.environ)
    # Never the operator's real ~/.feral. The sweep writes transcripts,
    # episodes and a vault.
    env["FERAL_HOME"] = str(home)
    # Read at import time by api/server.py, which is why this has to be
    # in the child's environment and not set from inside a test.
    env["NODE_API_KEY"] = SWEEP_NODE_KEY
    # Keep it off any real provider; it must not need a network. The
    # sweep additionally detaches the LLM after boot, because unsetting
    # these is not sufficient on a machine running a local Ollama.
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GROQ_API_KEY"):
        env.pop(key, None)

    try:
        proc = subprocess.run(
            [sys.executable, str(SWEEP)],
            cwd=str(FERAL_CORE),
            env=env,
            capture_output=True,
            text=True,
            timeout=SWEEP_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"the ambient websocket sweep did not finish within "
            f"{SWEEP_TIMEOUT_S}s. Something on the chain is wedged; run it "
            "by hand to see where:\n"
            f"    cd feral-core && FERAL_HOME=/tmp/ambient "
            f"NODE_API_KEY=k python "
            f"{SWEEP.relative_to(FERAL_CORE)}"
        )

    if proc.returncode == 2:
        pytest.skip(f"sweep could not run in this environment:\n{proc.stdout[-2000:]}")

    line = ""
    for candidate in (proc.stdout + "\n" + proc.stderr).splitlines():
        if candidate.startswith(RESULT_SENTINEL):
            line = candidate[len(RESULT_SENTINEL):]
    if not line:
        pytest.fail(
            "the sweep produced no result line, so it died before it could "
            "report:\n--- stdout ---\n"
            + proc.stdout[-3000:]
            + "\n--- stderr ---\n"
            + proc.stderr[-3000:]
        )

    return {c["name"]: c for c in json.loads(line)["checks"]}


def _assert(sweep: dict, name: str) -> None:
    """Fail with the sweep's own explanation of what went wrong."""
    check = sweep.get(name)
    assert check is not None, (
        f"the sweep did not report {name!r}; it stopped before reaching it. "
        f"Reported: {sorted(sweep)}"
    )
    assert check["ok"], f"{name}: {check['detail']}"


def test_no_check_the_sweep_reports_goes_unasserted(sweep):
    """Keeps the two files from drifting apart.

    A check added to the script and not to ``EXPECTED_CHECKS`` would run
    on every commit, fail on every commit, and be read by nobody.
    """
    unknown = sorted(set(sweep) - set(EXPECTED_CHECKS))
    assert not unknown, (
        f"the sweep reports {unknown}, which no test in this module "
        "asserts. Add a test naming the bug each one catches."
    )


def test_the_sweep_got_all_the_way_through_the_chain(sweep):
    """An absent check is a failure, not a pass.

    The sweep stops recording downstream checks once a link breaks: it
    cannot ask a digest about a transcript that was never acked. So a
    missing name means something upstream went wrong, and the named test
    for that link is where the real explanation is. This one exists so
    that a chain which quietly got shorter cannot read as a green run
    with fewer tests.
    """
    missing = sorted(set(EXPECTED_CHECKS) - set(sweep))
    assert not missing, (
        f"the sweep never reached {missing}. Look at the other failures "
        "in this module first: one of them is the cause."
    )


# ── the socket ──────────────────────────────────────────────────────


class TestTheFrameReachesTheBrainOverARealSocket:
    """Everything below the ack. None of it is reachable by a test that
    calls the handler directly: auth, envelope validation and the
    ``daemon_session`` type dispatch are all upstream of that call."""

    def test_the_bearer_is_accepted_and_the_node_registers(self, sweep):
        """``/v1/node`` refuses an unpaired socket outright when
        ``NODE_API_KEY`` is unset, and closes 4003 on a mismatch. A
        transcript path that works perfectly behind a gate nothing can
        get through is a transcript path nobody can use."""
        _assert(sweep, "the_socket_authenticates_and_registers")

    def test_a_real_ambient_frame_is_acked(self, sweep):
        """The frame goes through ``parse_message`` and the
        ``msg.type == "ambient_transcript"`` branch of
        ``daemon_session``. Registering a payload type in
        ``MESSAGE_TYPES`` without adding that branch validates the frame
        and then drops it into the terminal else, which is exactly how
        ``ambient_digest_request`` shipped inert once already."""
        _assert(sweep, "a_real_frame_is_acked_over_the_real_socket")

    def test_the_first_send_is_not_called_a_duplicate(self, sweep):
        """The phone discards a transcript it believes the brain already
        had. A false duplicate on the first send loses the conversation
        from both sides."""
        _assert(sweep, "the_first_send_is_not_reported_as_a_duplicate")

    def test_a_resend_is_acked_as_a_duplicate(self, sweep):
        """``episode_save`` mints a fresh uuid per call and has no
        dedupe of its own, so without the idempotency gate a phone
        draining a queue over a flaky link writes the same conversation
        into memory twice."""
        _assert(sweep, "a_resend_is_acked_as_a_duplicate_not_stored_twice")


# ── the digest ──────────────────────────────────────────────────────


class TestTheSummaryComesBackToThePhone:

    def test_the_finished_digest_is_pushed_while_the_phone_is_here(self, sweep):
        """The push leg. A recording made while both ends are up would
        otherwise show no summary until the next reconnect."""
        _assert(sweep, "the_finished_digest_is_pushed_to_the_node_that_is_still_here")

    def test_the_digest_can_be_pulled_back_over_the_socket(self, sweep):
        """The pull leg, which is the reliable one: summarisation
        finishes after the ack, by which time the phone is usually gone.
        This is a real background task on the real event loop, not a
        coroutine a test awaited itself."""
        _assert(sweep, "the_digest_becomes_retrievable_over_the_socket")

    def test_the_digest_is_about_the_words_that_were_sent(self, sweep):
        """A summary that arrives and describes nothing is the same
        failure as no summary. Asserted on a nonsense token that appears
        in the transcript and could not be there by coincidence."""
        _assert(sweep, "the_digest_summarises_the_words_that_were_sent")

    def test_the_full_transcript_survives_the_round_trip(self, sweep):
        """``include_detail`` is off by default and the phone asks for it
        when a card is opened. Compared byte for byte against what was
        sent, because this is the only remaining copy once the phone has
        dropped its own."""
        _assert(sweep, "the_digest_carries_the_full_transcript_when_asked")

    def test_the_speakers_the_phone_reported_come_back(self, sweep):
        """``speakers`` is an optional field on the wire, and optional
        fields are where a writer and a reader drift apart without
        anybody noticing. This is the ``hrv`` / ``skin_temperature`` /
        ``steps`` shape of defect, on this surface."""
        _assert(sweep, "the_digest_names_the_speakers_the_phone_reported")

    def test_the_digest_proves_no_model_was_reached(self, sweep):
        """Guards the determinism of every assertion above it.

        If a model were reached, the summary would be whatever that
        model said and these tests would pass or fail on the mood of a
        local Ollama. ``degraded: ["no_llm"]`` is the brain stating that
        it took the heuristic path.
        """
        _assert(sweep, "the_digest_says_it_ran_without_a_model")

    def test_an_id_nobody_stored_answers_unknown_in_the_same_shape(self, sweep):
        """Digest reads are scoped to the authenticated owner, and the
        unknown frame carries every key the ready frame does. A sparse
        frame would let a caller tell "another device owns this" from
        "nobody owns this" by which keys came back, which is the fact
        the scoping exists to withhold."""
        _assert(sweep, "an_unstored_id_answers_unknown_with_the_same_key_set")


# ── the agent's own reach ───────────────────────────────────────────


class TestTheAgentCanFindTheConversationAfterwards:
    """The reported bug, end to end.

    Asked "is there any ambient conversation recorded?", the agent
    answered that it had no ambient recording active while the brain
    held four transcripts with digests and commitments. It did not
    search and come up empty; there was no path from the store to the
    model, so it answered from its self-model.
    """

    def test_the_tool_exists_under_the_name_the_prompt_tells_it_to_call(
        self, sweep
    ):
        """``agents/identity_loader.py`` instructs the model, by name, to
        call ``notes_memory__list_conversations`` instead of
        introspecting. A tool registered under any other name is a tool
        that instruction sends the model to look for and not find."""
        _assert(sweep, "the_recall_tool_is_registered_under_the_name_the_prompt_uses")

    def test_the_tool_answers_through_the_registry(self, sweep):
        """Called the way the model calls it: an endpoint id through the
        skill's dispatch table, not the read function imported directly.
        An endpoint missing from that table returns a 404 envelope that
        no direct-call test can see."""
        _assert(sweep, "the_recall_tool_answers_without_erroring")

    def test_the_tool_can_see_the_conversation_the_socket_delivered(self, sweep):
        """The seam itself. Everything upstream can be correct and the
        agent still says it has no recordings, because the write side
        and the read side are different modules that agree only by
        hand."""
        _assert(sweep, "the_skill_the_agent_calls_can_see_the_conversation")

    def test_the_tool_reports_it_ready_rather_than_pending(self, sweep):
        """"Stored but not yet summarised" is not "no conversation".
        Reporting pending as absent would reproduce the original bug one
        layer down."""
        _assert(sweep, "the_skill_reports_it_as_ready_not_pending")

    def test_the_model_and_the_phone_are_told_the_same_thing(self, sweep):
        """Two readers over one row. They diverge the moment one of them
        starts deriving what the other stores."""
        _assert(sweep, "the_skill_hands_the_model_the_same_summary_the_phone_got")


# ── the seam, named ─────────────────────────────────────────────────


class TestTheWriterAndTheReaderStillAgree:
    """``memory/ambient_conversations.py`` says in its own docstring that
    it deliberately does not import from ``api.server``, and that the
    cost is the db path and the column names being stated in two places.
    This is the check that the cost stays paid."""

    def test_every_column_the_reader_names_is_one_the_writer_creates(
        self, sweep
    ):
        """Compared against the schema of a database the running brain
        actually created in this sweep, not against a CREATE TABLE the
        test wrote itself. A reader naming a column the writer does not
        write is precisely how ``hrv``, ``skin_temperature`` and
        ``steps`` went missing between two halves of one feature."""
        _assert(sweep, "every_column_the_reader_selects_is_one_the_writer_creates")

    def test_they_open_the_same_file(self, sweep):
        """Two ``feral_home() / "ambient_transcripts.db"`` expressions in
        two modules. Agreeing on every column name and disagreeing on
        the path gives a reader that is permanently, silently empty."""
        _assert(sweep, "the_reader_and_the_writer_open_the_same_file")
