"""The return leg: getting a conversation's digest back to the phone.

The transcript path worked and the structured outcome was thrown away.
`_process_ambient_transcript` built a full `TranscriptOutcome` (summary,
detail, people, commitments, topics, degraded, injection_flags) and kept
only the prose, inside an episode, plus `processed_at` and `episode_id`.
So the phone could show a recording's raw transcript and nothing else,
and recovering the summary meant parsing prose back out of the episode's
headline and lead.

These tests cover the three properties that are easy to get wrong and
expensive to get wrong:

  * a digest is readable only by the device that recorded it
  * `unknown` means "resend", so it must never be said about a
    transcript the brain actually holds
  * the reply to a week's backlog is bounded, and says how much is left
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict
from unittest.mock import patch

import pytest

from agents.ambient_transcript import TranscriptOutcome
from models.protocol import (
    MAX_DIGEST_REQUEST_ITEMS,
    MESSAGE_TYPES,
    AmbientDigestPayload,
    AmbientDigestRequestPayload,
)


@pytest.fixture
def ambient_db(tmp_path, monkeypatch):
    """An isolated ambient database, and server helpers pointed at it."""
    import api.server as srv

    path = tmp_path / "ambient_transcripts.db"
    monkeypatch.setattr(srv, "_ambient_db_path", lambda: path)
    return srv, path


def _store(srv, tid, *, owner_key, node_id="node-a", session="s"):
    return srv._ambient_store(
        tid, node_id=node_id, device_id="dev", session_id=session,
        payload={"text": "hello"}, owner_key=owner_key,
    )


def _outcome():
    return TranscriptOutcome(
        summary="Noah will send the SDK build.",
        detail="A long transcript. " * 200,
        people=["Noah"],
        commitments=[{"text": "Send the SDK build", "due_iso": "2026-08-28"}],
        topics=["sdk"],
        degraded=[],
        injection_flags=["ignore_previous_instructions"],
    )


class TestTheFramesAreRegistered:
    def test_both_directions_are_in_message_types(self):
        """Registration is what makes a frame parseable at all."""
        assert MESSAGE_TYPES["ambient_digest_request"] is AmbientDigestRequestPayload
        assert MESSAGE_TYPES["ambient_digest"] is AmbientDigestPayload

    def test_the_request_is_capped_well_below_the_generic_list_bound(self):
        """512 ids x 20k of detail is a ~10MB burst on reconnect."""
        assert MAX_DIGEST_REQUEST_ITEMS == 64
        too_many = [f"t{i}" for i in range(MAX_DIGEST_REQUEST_ITEMS + 1)]
        with pytest.raises(Exception):
            AmbientDigestRequestPayload(transcript_ids=too_many)

    def test_detail_is_off_by_default(self):
        assert AmbientDigestRequestPayload().include_detail is False


class TestTheOutcomeSurvives:
    def test_the_digest_is_stored_and_read_back_whole(self, ambient_db):
        srv, _ = ambient_db
        _store(srv, "t1", owner_key="phone-1")
        srv._ambient_mark_processed(
            "t1", "ep-1", json.dumps(asdict(_outcome())),
        )
        rows = srv._ambient_digest_rows(["t1"], owner_key="phone-1", node_id="node-a")
        frame = srv._ambient_digest_frame("t1", rows["t1"], include_detail=True)

        assert frame["status"] == "ready"
        assert frame["summary"] == "Noah will send the SDK build."
        assert frame["people"] == ["Noah"]
        assert frame["commitments"][0]["due_iso"] == "2026-08-28"
        assert frame["episode_id"] == "ep-1"
        # It validates as the real frame, which is what the phone parses.
        AmbientDigestPayload(**frame)

    def test_injection_flags_are_kept_on_disk_and_never_sent(self, ambient_db):
        """A signal about the transcript, not about the people in it.

        Useful in the brain's own logs; putting it on a phone card
        invites a scare banner over something a colleague said.
        """
        srv, path = ambient_db
        _store(srv, "t1", owner_key="phone-1")
        srv._ambient_mark_processed("t1", "ep-1", json.dumps(asdict(_outcome())))

        with sqlite3.connect(str(path)) as conn:
            stored = conn.execute(
                "SELECT digest_json FROM ambient_transcripts WHERE transcript_id = 't1'"
            ).fetchone()[0]
        assert "ignore_previous_instructions" in stored

        rows = srv._ambient_digest_rows(["t1"], owner_key="phone-1", node_id="node-a")
        frame = srv._ambient_digest_frame("t1", rows["t1"], include_detail=True)
        assert "injection_flags" not in frame
        assert "ignore_previous_instructions" not in json.dumps(frame)

    def test_detail_is_withheld_unless_asked_for(self, ambient_db):
        srv, _ = ambient_db
        _store(srv, "t1", owner_key="phone-1")
        srv._ambient_mark_processed("t1", "ep-1", json.dumps(asdict(_outcome())))
        rows = srv._ambient_digest_rows(["t1"], owner_key="phone-1", node_id="node-a")

        bulk = srv._ambient_digest_frame("t1", rows["t1"], include_detail=False)
        opened = srv._ambient_digest_frame("t1", rows["t1"], include_detail=True)
        assert bulk["detail"] == ""
        assert len(opened["detail"]) > 1000
        # The summary is what makes the card readable, so it is always there.
        assert bulk["summary"] == opened["summary"]


class TestOneDeviceCannotReadAnothers:
    """transcript_id is CLIENT-SUPPLIED (`payload.get(...) or uuid4()`).

    So a lookup keyed on the id alone is not protected by the usual
    "ids are unguessable" reasoning, and the caller here is another
    paired device on the same brain rather than a stranger on the
    internet. Without scoping, any paired node could read back the
    summary, people and commitments of a conversation recorded by a
    different device: the contents of someone else's conversation.
    """

    def test_another_device_gets_unknown_not_the_digest(self, ambient_db):
        srv, _ = ambient_db
        _store(srv, "t1", owner_key="phone-1", node_id="node-a")
        srv._ambient_mark_processed("t1", "ep-1", json.dumps(asdict(_outcome())))

        rows = srv._ambient_digest_rows(
            ["t1"], owner_key="phone-2", node_id="node-b",
        )
        assert rows == {}, "another device read back a digest it does not own"

        frame = srv._ambient_digest_frame("t1", rows.get("t1"), include_detail=True)
        assert frame["status"] == "unknown"
        assert frame["summary"] == ""
        assert "Noah" not in json.dumps(frame)

    def test_a_stolen_id_is_indistinguishable_from_a_missing_one(self, ambient_db):
        """Both answer `unknown`, deliberately.

        Saying "someone else owns this" would confirm the existence of
        another device's recording to anyone who asked for it.
        """
        srv, _ = ambient_db
        _store(srv, "t1", owner_key="phone-1", node_id="node-a")
        srv._ambient_mark_processed("t1", "ep-1", json.dumps(asdict(_outcome())))

        theirs = srv._ambient_digest_frame(
            "t1",
            srv._ambient_digest_rows(["t1"], owner_key="p2", node_id="n2").get("t1"),
            include_detail=False,
        )
        nobodys = srv._ambient_digest_frame(
            "nope",
            srv._ambient_digest_rows(["nope"], owner_key="p2", node_id="n2").get("nope"),
            include_detail=False,
        )
        assert theirs["status"] == nobodys["status"] == "unknown"
        assert set(theirs) == set(nobodys)

    def test_the_owning_device_still_reads_its_own(self, ambient_db):
        srv, _ = ambient_db
        _store(srv, "t1", owner_key="phone-1", node_id="node-a")
        srv._ambient_mark_processed("t1", "ep-1", json.dumps(asdict(_outcome())))
        rows = srv._ambient_digest_rows(["t1"], owner_key="phone-1", node_id="node-a")
        assert rows["t1"]["episode_id"] == "ep-1"

    def test_rows_from_before_owner_key_existed_still_belong_to_their_node(
        self, ambient_db,
    ):
        """Without this fallback, upgrading looks exactly like data loss.

        Every transcript stored before this change carries a NULL
        owner_key. If those answered `unknown`, the phone would read
        that as "the brain lost it" and re-upload every recording it
        has, on the first connect after the upgrade.
        """
        srv, path = ambient_db
        _store(srv, "t1", owner_key="phone-1", node_id="node-a")
        srv._ambient_mark_processed("t1", "ep-1", json.dumps(asdict(_outcome())))
        with sqlite3.connect(str(path)) as conn:
            conn.execute("UPDATE ambient_transcripts SET owner_key = NULL")
            conn.commit()

        mine = srv._ambient_digest_rows(["t1"], owner_key="phone-1", node_id="node-a")
        assert "t1" in mine, "a pre-upgrade row stopped belonging to its own node"

        # And the fallback does not become a hole for a different node.
        theirs = srv._ambient_digest_rows(["t1"], owner_key="phone-2", node_id="node-b")
        assert theirs == {}


class TestTheThreeStatuses:
    def test_stored_but_not_summarized_is_pending_not_unknown(self, ambient_db):
        """`unknown` tells the phone to resend. Saying it here would
        make it re-upload a transcript we are holding and still working
        on, every single connect until the summary lands."""
        srv, _ = ambient_db
        _store(srv, "t1", owner_key="phone-1")
        rows = srv._ambient_digest_rows(["t1"], owner_key="phone-1", node_id="node-a")
        frame = srv._ambient_digest_frame("t1", rows["t1"], include_detail=False)
        assert frame["status"] == "pending"

    def test_a_failed_summary_reads_pending_so_the_sweep_can_retry(self, ambient_db):
        """Processing leaves processed_at NULL on failure, on purpose."""
        srv, _ = ambient_db
        _store(srv, "t1", owner_key="phone-1")
        assert srv._ambient_pending()  # the boot sweep will pick it up
        rows = srv._ambient_digest_rows(["t1"], owner_key="phone-1", node_id="node-a")
        assert srv._ambient_digest_frame(
            "t1", rows["t1"], include_detail=False,
        )["status"] == "pending"

    def test_a_row_we_never_had_is_unknown(self, ambient_db):
        srv, _ = ambient_db
        rows = srv._ambient_digest_rows(["ghost"], owner_key="p", node_id="n")
        assert srv._ambient_digest_frame(
            "ghost", rows.get("ghost"), include_detail=False,
        )["status"] == "unknown"

    def test_an_unreadable_digest_is_ready_but_empty_not_a_crash(self, ambient_db):
        """A corrupt blob must not take down the drain of every other id."""
        srv, path = ambient_db
        _store(srv, "t1", owner_key="phone-1")
        srv._ambient_mark_processed("t1", "ep-1", "{not json")
        rows = srv._ambient_digest_rows(["t1"], owner_key="phone-1", node_id="node-a")
        frame = srv._ambient_digest_frame("t1", rows["t1"], include_detail=True)
        assert frame["status"] == "ready"
        assert frame["summary"] == ""
        AmbientDigestPayload(**frame)


class TestNothingPrunes:
    def test_no_deletion_of_ambient_transcripts_anywhere(self):
        """The invariant `unknown` rests on.

        `unknown` means "the brain lost it, resend". That is only true
        while nothing removes rows. The day retention is added, an
        aged-out recording starts answering `unknown` and the phone
        re-uploads it forever. If this test fails, the fix is a new
        status, not a wider `unknown`.
        """
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parent.parent
        offenders = []
        for path in root.rglob("*.py"):
            rel = path.relative_to(root).as_posix()
            if rel.startswith(("build/", "tests/", "dist/")):
                continue
            text = path.read_text(errors="ignore")
            if "ambient_transcripts" not in text:
                continue
            for m in re.finditer(
                r"(DELETE\s+FROM|DROP\s+TABLE)\s+ambient_transcripts", text, re.I,
            ):
                offenders.append(f"{rel}: {m.group(0)}")
        assert offenders == [], (
            "something now removes ambient transcripts, which breaks the "
            f"meaning of the `unknown` digest status: {offenders}"
        )


class TestTheReconnectBurstIsBoundedAndReportsProgress:
    @pytest.mark.asyncio
    async def test_remaining_counts_down_to_zero(self, ambient_db):
        """A phone away for a week has something to wait for.

        Without `remaining` it cannot tell "your last digest" from "the
        first of forty" until the frames simply stop, so it can only
        appear to hang. With it, it can say it is fetching and show how
        far along it is.
        """
        srv, _ = ambient_db
        ids = [f"t{i}" for i in range(5)]
        for tid in ids:
            _store(srv, tid, owner_key="phone-1")
            srv._ambient_mark_processed(tid, f"ep-{tid}", json.dumps(asdict(_outcome())))

        sent = []

        class _WS:
            async def send_json(self, obj):
                sent.append(obj)

        await srv._handle_ambient_digest_request(
            _WS(), "node-a", "phone-1",
            {"payload": {"transcript_ids": ids}},
        )

        assert [f["payload"]["transcript_id"] for f in sent] == ids
        assert [f["payload"]["remaining"] for f in sent] == [4, 3, 2, 1, 0]
        assert all(f["type"] == "ambient_digest" for f in sent)

    @pytest.mark.asyncio
    async def test_a_repeated_id_cannot_eat_the_budget(self, ambient_db):
        srv, _ = ambient_db
        _store(srv, "t1", owner_key="phone-1")
        sent = []

        class _WS:
            async def send_json(self, obj):
                sent.append(obj)

        await srv._handle_ambient_digest_request(
            _WS(), "node-a", "phone-1",
            {"payload": {"transcript_ids": ["t1"] * 200}},
        )
        assert len(sent) == 1

    @pytest.mark.asyncio
    async def test_an_oversized_list_is_truncated_not_refused(self, ambient_db):
        """A phone draining a long backlog must not lose the connection
        over it; it asks again for the rest."""
        srv, _ = ambient_db
        sent = []

        class _WS:
            async def send_json(self, obj):
                sent.append(obj)

        await srv._handle_ambient_digest_request(
            _WS(), "node-a", "phone-1",
            {"payload": {"transcript_ids": [f"t{i}" for i in range(500)]}},
        )
        assert len(sent) == MAX_DIGEST_REQUEST_ITEMS

    @pytest.mark.asyncio
    async def test_a_socket_that_drops_mid_drain_does_not_raise(self, ambient_db):
        srv, _ = ambient_db
        for i in range(4):
            _store(srv, f"t{i}", owner_key="phone-1")

        class _WS:
            def __init__(self):
                self.n = 0

            async def send_json(self, obj):
                self.n += 1
                if self.n == 2:
                    raise RuntimeError("socket closed")

        await srv._handle_ambient_digest_request(
            _WS(), "node-a", "phone-1",
            {"payload": {"transcript_ids": [f"t{i}" for i in range(4)]}},
        )


class TestTheSchemaMigratesInPlace:
    def test_an_existing_table_gains_the_new_columns(self, ambient_db):
        """CREATE TABLE IF NOT EXISTS is a no-op on an existing table, so
        a column added to the DDL reaches new installs only."""
        srv, path = ambient_db
        with sqlite3.connect(str(path)) as conn:
            conn.execute(
                """
                CREATE TABLE ambient_transcripts (
                    transcript_id TEXT PRIMARY KEY,
                    received_at REAL NOT NULL,
                    node_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    processed_at REAL,
                    episode_id TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO ambient_transcripts VALUES (?,?,?,?,?,?,?,?)",
                ("old", time.time(), "node-a", "dev", "s", "{}", None, None),
            )
            conn.commit()

        _store(srv, "new", owner_key="phone-1")

        with sqlite3.connect(str(path)) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(ambient_transcripts)")}
            kept = conn.execute(
                "SELECT COUNT(*) FROM ambient_transcripts WHERE transcript_id = 'old'"
            ).fetchone()[0]
        assert {"digest_json", "owner_key"} <= cols
        assert kept == 1, "the additive migration dropped an existing row"

    def test_running_it_twice_is_safe(self, ambient_db):
        srv, path = ambient_db
        with sqlite3.connect(str(path)) as conn:
            srv._ambient_ensure_schema(conn)
            srv._ambient_ensure_schema(conn)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(ambient_transcripts)")}
        assert {"digest_json", "owner_key"} <= cols


class TestThePushLeg:
    """Push and pull are both needed, and the spec is right about why.

    Push alone loses every digest for a phone that has already gone,
    which is the normal case: summarization finishes seconds to minutes
    after the ack. Pull alone means a recording made with the phone
    sitting connected shows no summary until the next reconnect.
    """

    @pytest.mark.asyncio
    async def test_it_reaches_a_connected_node(self, ambient_db):
        srv, _ = ambient_db
        _store(srv, "t1", owner_key="phone-1", node_id="node-a")
        srv._ambient_mark_processed("t1", "ep-1", json.dumps(asdict(_outcome())))

        sent = []

        async def _send(node_id, msg):
            sent.append((node_id, msg))

        with patch.object(srv.state, "daemons", {"node-a": object()}), \
             patch.object(srv.state, "_send_dict_to_node", _send):
            await srv._ambient_push_digest("t1")

        assert len(sent) == 1
        node_id, msg = sent[0]
        assert node_id == "node-a"
        assert msg["type"] == "ambient_digest"
        assert msg["payload"]["status"] == "ready"
        assert msg["payload"]["remaining"] == 0, "a push is a single digest"
        # The phone is here and this is one frame, so the size argument
        # for withholding detail does not apply.
        assert len(msg["payload"]["detail"]) > 1000
        AmbientDigestPayload(**msg["payload"])

    @pytest.mark.asyncio
    async def test_a_node_that_has_gone_is_not_an_error(self, ambient_db):
        """The usual case. The pull leg covers it."""
        srv, _ = ambient_db
        _store(srv, "t1", owner_key="phone-1", node_id="node-a")
        srv._ambient_mark_processed("t1", "ep-1", json.dumps(asdict(_outcome())))

        sent = []

        async def _send(node_id, msg):
            sent.append(msg)

        with patch.object(srv.state, "daemons", {}), \
             patch.object(srv.state, "_send_dict_to_node", _send):
            await srv._ambient_push_digest("t1")
        assert sent == []

    @pytest.mark.asyncio
    async def test_a_failing_send_never_raises(self, ambient_db):
        """This runs at the tail of a detached task whose contract is
        that failing costs nothing: processed_at is already set."""
        srv, _ = ambient_db
        _store(srv, "t1", owner_key="phone-1", node_id="node-a")
        srv._ambient_mark_processed("t1", "ep-1", json.dumps(asdict(_outcome())))

        async def _boom(node_id, msg):
            raise RuntimeError("socket closed")

        with patch.object(srv.state, "daemons", {"node-a": object()}), \
             patch.object(srv.state, "_send_dict_to_node", _boom):
            await srv._ambient_push_digest("t1")

    @pytest.mark.asyncio
    async def test_a_missing_row_pushes_nothing(self, ambient_db):
        srv, _ = ambient_db
        sent = []

        async def _send(node_id, msg):
            sent.append(msg)

        with patch.object(srv.state, "daemons", {"node-a": object()}), \
             patch.object(srv.state, "_send_dict_to_node", _send):
            await srv._ambient_push_digest("ghost")
        assert sent == []
