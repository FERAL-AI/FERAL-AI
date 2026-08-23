"""Drive one ambient conversation through a live brain over a real socket.

Run by hand:

    cd feral-core
    FERAL_HOME=/tmp/ambient NODE_API_KEY=sweep-key \\
        python scripts_audit/ambient_ws_sweep.py

Prints one JSON object describing every check and exits non-zero if any
of them failed. ``tests/test_ambient_websocket_chain.py`` runs this in a
subprocess and turns each named check into its own test.

Why this exists
===============
The ambient path already has two test modules and neither one connects
to anything. ``tests/test_ambient_transcript_frame.py`` calls
``_handle_ambient_transcript`` directly with a hand-written fake socket
and a stubbed ``state``; ``tests/test_ambient_conversation_recall.py``
calls ``_ambient_store`` and ``list_conversations`` as functions. Both
are worth having and neither can see the seam that actually broke.

The two reported defects both lived in a seam:

* the transcripts were stored, and the agent could not reach them,
  because the write side and the read side were two modules that agreed
  about the schema only by hand (``memory/ambient_conversations.py``
  says so in its own module docstring);
* ``hrv`` / ``skin_temperature`` / ``steps`` were dropped between a
  writer and a reader that disagreed about a key name.

A seam is invisible to a test that mocks one side of it. So this boots
the real app on a throwaway ``FERAL_HOME``, authenticates a real
websocket against ``/v1/node`` with a real bearer, sends a real
``ambient_transcript`` frame through ``parse_message`` and
``daemon_session``, waits for the real background summariser, and then
asks the REAL skill registry the question an operator asks the agent:
"is there any ambient conversation recorded?".

Subprocess, not in-process, for the same two reasons as
``scripts_audit/route_sweep.py``: a wedged websocket receive would
otherwise hang the whole pytest session, and booting a second brain
inside a session where ``conftest`` has already initialised
``api.state.state`` deadlocked on lifespan teardown.

Nothing here needs a network and nothing spends a token: the LLM is
detached after boot (see ``_detach_llm``) so the summariser takes its
documented no-model path, which is both offline and deterministic.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

# Boot the way the brain really boots: the real entrypoint is
# `python -m api.server` run FROM feral-core, so sys.path[0] is
# feral-core and top-level packages like `migrations` resolve. See the
# same note in route_sweep.py.
_FERAL_CORE = Path(__file__).resolve().parents[1]
if str(_FERAL_CORE) not in sys.path:
    sys.path.insert(0, str(_FERAL_CORE))


#: A single websocket receive may not exceed this.
#:
#: Deliberately short. There is no network here: the app runs in this
#: process behind a TestClient, so a frame that is coming arrives in
#: milliseconds and a frame that has not arrived in seconds is not
#: coming. Sized against the FAILING path, not the passing one: a
#: severed chain makes every one of these waits run to the end, and at
#: 45s each that summed to 229s, which was close enough to the pytest
#: wrapper's ceiling that a broken chain would have been reported as
#: "the sweep timed out" instead of naming the link that broke. A
#: diagnosis is worth more than the last few seconds of patience.
#:
#: Waiting for the background summariser is NOT bounded by this; it has
#: its own poll loop and DIGEST_WAIT_S below.
STEP_TIMEOUT_S = 15

#: How long to keep re-asking for a digest before calling it lost. The
#: summariser is a detached background task, so "not yet" is a normal
#: intermediate answer and only its persistence is a failure.
DIGEST_WAIT_S = 40

#: Frames to read past while looking for one particular type. The brain
#: pushes unsolicited traffic on this socket (digests, brain events), so
#: a receive loop that expected the next frame to be the interesting one
#: would be flaky by construction.
MAX_FRAMES_SCANNED = 40

#: Prefix on the one machine-readable line of output. Read by
#: tests/test_ambient_websocket_chain.py; do not change one without the
#: other.
RESULT_SENTINEL = "AMBIENT_SWEEP_RESULT "

NODE_ID = "glasses-ambient-sweep"

#: Distinctive enough that finding it in a summary cannot be a
#: coincidence, and shaped like something a person would really say so
#: the commitment extractor has something true to work on.
TRANSCRIPT_TEXT = (
    "Morning Priya. I'll send you the pairing repro before Friday, "
    "and I still owe Noah the investor deck from last week. "
    "Zarquon-7 is the codename we agreed on for the glasses refresh."
)
SPEAKERS = ["Priya", "Noah"]


class _Timeout(Exception):
    pass


def _alarm(_signum, _frame):
    raise _Timeout()


class Report:
    """Named checks, so the pytest wrapper can fail one bug at a time.

    A single pass/fail for the whole chain would tell an operator that
    "ambient is broken" and nothing about which link. Each check here
    names a specific defect that has actually happened.
    """

    def __init__(self):
        self.checks: list[dict] = []

    def record(self, name: str, ok: bool, detail: str = "") -> bool:
        # Detail is kept only on the failing side. On the passing side it
        # is noise at best and actively misleading at worst: a message
        # written to explain a failure ("none of them was the transcript
        # this sweep sent") reads as a contradiction next to ok=true.
        ok = bool(ok)
        self.checks.append({
            "name": name, "ok": ok, "detail": "" if ok else str(detail)[:900],
        })
        return ok

    def failed(self) -> list[str]:
        return [c["name"] for c in self.checks if not c["ok"]]

    def emit(self) -> None:
        # Twice, for two different readers. Indented for a person running
        # this by hand, then one compact line behind a sentinel for the
        # pytest wrapper. Booting a brain writes a few hundred lines of
        # log around this, and "find the last balanced brace" is a
        # parser waiting to break on a log line that happens to contain
        # one.
        print(json.dumps({"checks": self.checks}, indent=2))
        print(RESULT_SENTINEL + json.dumps({"checks": self.checks}))


def _frame(msg_type: str, payload: dict) -> dict:
    from models.protocol import HUP_VERSION

    return {
        "hup_version": HUP_VERSION,
        "type": msg_type,
        "hop": "daemon",
        "payload": payload,
    }


class _Wire:
    """The socket, plus a pushback buffer for frames not asked for yet.

    This brain does not answer one frame per frame. It pushes an
    unsolicited ``ambient_digest`` the moment summarisation finishes, and
    that push interleaves with whatever the sweep is waiting on. A
    reader that took "the next frame of this type" and threw the rest
    away would silently consume the push while looking for an ack, and
    then time out waiting for a digest that had already been delivered
    and discarded. Every frame read is kept, so the order the brain
    happens to send in cannot make this flaky.

    Bounded twice over: by ``budget`` frames and by SIGALRM on every
    individual receive. A websocket read with neither is the thing that
    hangs a suite.
    """

    def __init__(self, ws):
        self._ws = ws
        self._held: list[dict] = []

    def send(self, frame: dict) -> None:
        self._ws.send_json(frame)

    def take(self, wanted: str, match=None, budget: int = MAX_FRAMES_SCANNED):
        """The first frame of type ``wanted`` satisfying ``match``, or None."""
        def _hit(frame: dict) -> bool:
            if frame.get("type") != wanted:
                return False
            return match is None or match(frame.get("payload") or {})

        for i, frame in enumerate(self._held):
            if _hit(frame):
                return self._held.pop(i)

        for _ in range(budget):
            signal.alarm(STEP_TIMEOUT_S)
            try:
                frame = self._ws.receive_json()
            except _Timeout:
                return None
            finally:
                signal.alarm(0)
            if _hit(frame):
                return frame
            self._held.append(frame)
        return None


def _detach_llm() -> None:
    """Take the model out of the loop, on purpose and explicitly.

    ``summarize_transcript`` degrades to a heuristic when handed no LLM,
    and that is not a special test mode: it is the documented path a
    brain with no provider configured takes every day. Pinning the sweep
    to it buys two things this test must have. It cannot reach a network
    (an unset OPENAI_API_KEY is not sufficient on its own, because the
    provider falls back to a local Ollama if one happens to be running
    on the machine), and the digest it produces is a pure function of
    the transcript, so the assertions downstream can be exact instead of
    hedged.
    """
    from api.state import state

    orchestrator = getattr(state, "orchestrator", None)
    if orchestrator is not None:
        orchestrator.llm = None


def _writer_schema_columns() -> set[str]:
    """The columns the brain's own writer really created."""
    import sqlite3

    import api.server as srv

    with sqlite3.connect(str(srv._ambient_db_path())) as conn:
        return {row[1] for row in conn.execute(
            "PRAGMA table_info(ambient_transcripts)"
        )}


def run(report: Report) -> None:
    from fastapi.testclient import TestClient

    import api.server as srv

    node_key = os.environ["NODE_API_KEY"]

    client = TestClient(srv.app, raise_server_exceptions=False)
    with client:
        _detach_llm()

        # ── connect and identify ────────────────────────────────────
        #
        # A real bearer against the real auth gate. `daemon_session`
        # refuses an unpaired node when NODE_API_KEY is unset and
        # refuses a wrong key with a 4003 close, so reaching the frame
        # loop at all is itself a check.
        with client.websocket_connect(
            "/v1/node", headers={"Authorization": f"Bearer {node_key}"},
        ) as raw_ws:
            wire = _Wire(raw_ws)
            wire.send(_frame("node_register", {
                "node_id": NODE_ID,
                "node_type": "glasses",
                "platform": "ios",
                "capabilities": ["ambient_audio"],
            }))
            ack = wire.take("node_ack")
            if not report.record(
                "the_socket_authenticates_and_registers",
                ack is not None,
                "no node_ack came back; the bearer was refused or the "
                "register frame did not reach the handler",
            ):
                return

            transcript_id = f"sweep-{int(time.time() * 1000)}"
            started_at = 1_700_000_000.0  # fixed: never "now", never local

            # ── the frame a phone really sends ──────────────────────
            #
            # Physiology included deliberately. It is optional on the
            # wire, and optional fields are exactly where a writer and a
            # reader drift apart without anybody noticing.
            wire.send(_frame("ambient_transcript", {
                "transcript_id": transcript_id,
                "text": TRANSCRIPT_TEXT,
                "session_id": "sweep-session",
                "device_id": "phone-sweep",
                "started_at": started_at,
                "ended_at": started_at + 300.0,
                "source": "glasses_mic",
                "speakers": SPEAKERS,
                "baseline_hr": 62.0,
                "respiratory_bpm": 14.0,
                "moments": [{
                    "at_char": 10,
                    "hr": 91.0,
                    "delta_bpm": 29.0,
                    "confidence": 0.9,
                }],
            }))

            ack = wire.take("ambient_transcript_ack")
            report.record(
                "a_real_frame_is_acked_over_the_real_socket",
                ack is not None
                and ack["payload"]["transcript_id"] == transcript_id
                and ack["payload"]["accepted"] is True,
                f"ack={ack}",
            )
            report.record(
                "the_first_send_is_not_reported_as_a_duplicate",
                bool(ack) and ack["payload"]["duplicate"] is False,
                f"ack={ack}",
            )

            # ── the phone resends what it has not yet dropped ───────
            wire.send(_frame("ambient_transcript", {
                "transcript_id": transcript_id,
                "text": TRANSCRIPT_TEXT,
                "session_id": "sweep-session",
                "source": "glasses_mic",
                "speakers": SPEAKERS,
            }))
            resend = wire.take("ambient_transcript_ack")
            report.record(
                "a_resend_is_acked_as_a_duplicate_not_stored_twice",
                resend is not None and resend["payload"]["duplicate"] is True,
                f"ack={resend}",
            )

            # ── the push leg ───────────────────────────────────────
            #
            # Summarisation finishes after the ack, so the brain sends
            # an unsolicited digest to the node that recorded it. That
            # is best-effort by nature because the phone is usually
            # gone by then, but this phone is still here, which is the
            # one case the push exists to cover: a recording made while
            # both ends are up would otherwise show no summary until
            # the next reconnect.
            def mine(payload: dict) -> bool:
                return payload.get("transcript_id") == transcript_id

            pushed = wire.take("ambient_digest", match=mine)
            report.record(
                "the_finished_digest_is_pushed_to_the_node_that_is_still_here",
                pushed is not None and pushed["payload"].get("status") == "ready",
                f"pushed={pushed}",
            )

            # ── the pull leg ───────────────────────────────────────
            #
            # What makes a digest reachable at all for a phone that was
            # gone when the summary landed. Asked separately from the
            # push above, and matched on the id, so one leg cannot pass
            # by consuming the other leg's frame.
            deadline = time.time() + DIGEST_WAIT_S
            digest = None
            while True:
                wire.send(_frame("ambient_digest_request", {
                    "transcript_ids": [transcript_id],
                    "include_detail": True,
                }))
                frame = wire.take("ambient_digest", match=mine)
                if frame is None:
                    break
                digest = frame["payload"]
                if digest.get("status") == "ready":
                    break
                if time.time() >= deadline:
                    break
                time.sleep(0.25)

            ready = bool(digest) and digest.get("status") == "ready"
            report.record(
                "the_digest_becomes_retrievable_over_the_socket",
                ready,
                f"last digest frame: {digest}",
            )

            if ready:
                report.record(
                    "the_digest_summarises_the_words_that_were_sent",
                    "Zarquon-7" in digest.get("summary", ""),
                    f"summary={digest.get('summary')!r}",
                )
                report.record(
                    "the_digest_carries_the_full_transcript_when_asked",
                    digest.get("detail", "") == TRANSCRIPT_TEXT,
                    f"detail={digest.get('detail')!r}",
                )
                report.record(
                    "the_digest_names_the_speakers_the_phone_reported",
                    set(SPEAKERS).issubset(set(digest.get("people") or [])),
                    f"people={digest.get('people')}",
                )
                report.record(
                    "the_digest_says_it_ran_without_a_model",
                    "no_llm" in (digest.get("degraded") or []),
                    "the sweep detaches the LLM, so anything else means "
                    f"a model was reached: degraded={digest.get('degraded')}",
                )

            # ── an id this socket does not own ─────────────────────
            stranger = "sweep-id-that-was-never-stored"
            wire.send(_frame("ambient_digest_request", {
                "transcript_ids": [stranger],
            }))
            unknown = wire.take(
                "ambient_digest",
                match=lambda p: p.get("transcript_id") == stranger,
            )
            report.record(
                "an_unstored_id_answers_unknown_with_the_same_key_set",
                unknown is not None
                and unknown["payload"]["status"] == "unknown"
                and set(unknown["payload"]) == set(digest or unknown["payload"]),
                f"frame={unknown}",
            )

        # ── the question the agent could not answer ─────────────────
        #
        # Out of the socket now and into the skill the model calls. This
        # is the seam the second defect lived in: everything above can
        # be perfect and the agent still answers "I don't have any
        # ambient recording" if the read side cannot see the write side.
        listing = _skill_listing(report)
        if listing is not None:
            found = [
                c for c in listing.get("conversations", [])
                if c.get("transcript_id") == transcript_id
            ]
            report.record(
                "the_skill_the_agent_calls_can_see_the_conversation",
                bool(found),
                "notes_memory__list_conversations returned "
                f"{listing.get('total')} total and none of them was the "
                f"transcript this sweep just sent: {listing}",
            )
            if found:
                conv = found[0]
                report.record(
                    "the_skill_reports_it_as_ready_not_pending",
                    conv.get("status") == "ready",
                    f"conversation={conv}",
                )
                report.record(
                    "the_skill_hands_the_model_the_same_summary_the_phone_got",
                    "Zarquon-7" in (conv.get("summary") or ""),
                    f"summary={conv.get('summary')!r}",
                )

        # ── the seam itself, stated as a check ─────────────────────
        #
        # api/server.py owns every write and memory/ambient_conversations
        # owns the read, and the two agree about column names only by
        # hand. That is the shape of the hrv / skin_temperature / steps
        # defect: a reader naming a key the writer does not write.
        from memory.ambient_conversations import _COLUMNS, ambient_db_path

        written = _writer_schema_columns()
        missing = sorted(set(_COLUMNS) - written)
        report.record(
            "every_column_the_reader_selects_is_one_the_writer_creates",
            not missing,
            f"memory/ambient_conversations._COLUMNS names {missing}, which "
            f"api/server.py does not create. Columns present: {sorted(written)}",
        )
        report.record(
            "the_reader_and_the_writer_open_the_same_file",
            str(ambient_db_path()) == str(_server_db_path()),
            f"reader={ambient_db_path()} writer={_server_db_path()}",
        )


def _server_db_path():
    import api.server as srv

    return srv._ambient_db_path()


def _skill_listing(report: Report):
    """Ask through the registry, not through the read module.

    ``list_conversations`` is already called directly by
    ``tests/test_ambient_conversation_recall.py``. What was never
    exercised is the path the model actually takes: a tool name, the
    skill registry's dispatch, and the async wrapper in
    ``skills/impl/notes_memory.py``. A skill registered under the wrong
    endpoint id fails there and nowhere else.
    """
    import asyncio

    from skills.registry import SkillRegistry

    registry = SkillRegistry()
    registry.load_builtin_skills()

    names = {
        (t.get("function") or {}).get("name") or t.get("name")
        for t in registry.get_all_tools()
    }
    if not report.record(
        "the_recall_tool_is_registered_under_the_name_the_prompt_uses",
        "notes_memory__list_conversations" in names,
        "agents/identity_loader.py tells the model to call "
        "`notes_memory__list_conversations` by name; a tool under any "
        "other name is a tool the model will never find",
    ):
        return None

    from skills.impl import SKILL_IMPLEMENTATIONS

    skill = SKILL_IMPLEMENTATIONS.get("notes_memory")
    if skill is None:
        report.record(
            "the_recall_tool_has_a_backing_implementation", False,
            "notes_memory is in the manifest but has no implementation "
            "registered, so every call 404s",
        )
        return None

    result = asyncio.run(skill.execute("list_conversations", {"limit": 20}, {}))
    if not report.record(
        "the_recall_tool_answers_without_erroring",
        bool(result.get("success")),
        f"result={result}",
    ):
        return None
    return result.get("data") or {}


def main() -> int:
    if not os.environ.get("FERAL_HOME"):
        print("refusing to run without FERAL_HOME set to a throwaway directory")
        return 2
    if not os.environ.get("NODE_API_KEY"):
        print("refusing to run without NODE_API_KEY; /v1/node closes an "
              "unpaired socket when it is unset, which is correct and is "
              "not what this sweep is measuring")
        return 2

    signal.signal(signal.SIGALRM, _alarm)

    report = Report()
    try:
        run(report)
    except _Timeout:
        report.record(
            "the_sweep_completed", False,
            f"a single step blocked for over {STEP_TIMEOUT_S}s",
        )
    except Exception as exc:  # noqa: BLE001 - report it, never hang on it
        import traceback

        report.record(
            "the_sweep_completed", False,
            f"{exc.__class__.__name__}: {exc}\n{traceback.format_exc()[-1500:]}",
        )
    finally:
        signal.alarm(0)

    report.emit()
    return 1 if report.failed() else 0


if __name__ == "__main__":
    sys.exit(main())
