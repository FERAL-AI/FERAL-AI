"""Scoped sharing: what crosses between two brains, and what must not.

Replication used to be all-or-nothing. Every row of every table in
``SyncEngine._SYNC_ALLOWED_TABLES`` went to any peer that got past the
handshake, so two operators who wanted to pool robot events had to pool
their whole personal memory. This module pins the boundary that makes
partial sharing expressible, and the fail-closed rules that make a bug
in it produce "shared too little" rather than "shared too much".

Where the assertions live
-------------------------
The headline case (:class:`TestTwoBrainsShareOneScope`) drives the real
``api.server.sync_peer_endpoint`` over a real websocket, from the real
``SyncEngine.sync_with_peer``, and asserts on MATERIALISED TABLE
CONTENTS on the receiving side. Never on the op log. A previous fuzz
suite asserted over the op log and missed three defects for exactly
that reason: both sides converge on the same set of WAL operations no
matter what did or did not reach the user's ``notes`` table, and the
scope gate is precisely a place where those two answers differ.

The unit classes below it pin the pieces the end-to-end test cannot
isolate: the normalisation rules, the WAL column's default for history,
and the receive-side check against a peer that lies.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from memory.hlc import HLCTimestamp
from memory.store import MemoryStore
from memory.sync import SyncEngine, SyncOperation, SyncWAL
from security.peer_roster import PeerRoster
from security.sync_scopes import (
    DENY_ALL,
    INHERIT,
    PRIVATE,
    InvalidScopeError,
    is_shareable,
    normalise_scope,
    normalise_scope_set,
    require_shareable_scope,
)

# Reuse the end-to-end harness rather than building a second one. It is
# the module that already drives the real endpoint over a real socket,
# and a parallel harness would be a second thing to keep honest.
#
# Re-bound as module attributes rather than imported by name: pytest
# collects a fixture from whatever module attribute holds it, and an
# ``from ... import two_nodes`` binding that a test then names as a
# parameter is what ruff reports as F811.
from tests import test_sync_e2e_protocol as _e2e

exchange = _e2e.exchange
notes_table = _e2e.notes_table
share = _e2e.share
write_note = _e2e.write_note
_start_node = _e2e._start_node
_stop_node = _e2e._stop_node

# Fixtures, including the two autouse ones that keep the shared
# passphrase and the outbound-grant vault out of the operator's real
# ``~/.feral``.
_passphrase = _e2e._passphrase
_peer_grants = _e2e._peer_grants
two_nodes = _e2e.two_nodes

pytestmark = pytest.mark.timeout(120)

ROBOT_SCOPE = "robot-events"


# ---------------------------------------------------------------------------
# The headline: two brains, one shared scope, personal notes that stay home
# ---------------------------------------------------------------------------


class TestTwoBrainsShareOneScope:
    """The end state the whole change exists for.

    Two separately-owned brains pool one named feed and nothing else.
    Both write personal notes the ordinary way, with no scope, and
    those notes must not cross in either direction.
    """

    async def test_only_the_granted_scope_crosses(self, two_nodes):
        """A grants B ``robot-events`` and B grants A the same. Each
        brain also keeps a personal note.

        Asserted on ``notes`` on both sides after a real handshake. If
        the scope gate were absent this is the test that fails, and it
        fails by finding the OTHER operator's private note in the local
        table, which is the outcome that cannot be undone in production.
        """
        a, b = two_nodes
        share(a, b, scope=ROBOT_SCOPE)

        await write_note(a, "robot-a", "pallet 41 delivered", scope=ROBOT_SCOPE)
        await write_note(b, "robot-b", "dock 2 blocked", scope=ROBOT_SCOPE)
        await write_note(a, "personal-a", "call the clinic back", scope=None)
        await write_note(b, "personal-b", "renew the lease", scope=None)

        result = await exchange(a, b)
        assert result["success"] is True, result

        on_a = await notes_table(a)
        on_b = await notes_table(b)

        assert on_a == {
            "robot-a": "pallet 41 delivered",
            "robot-b": "dock 2 blocked",
            "personal-a": "call the clinic back",
        }
        assert on_b == {
            "robot-a": "pallet 41 delivered",
            "robot-b": "dock 2 blocked",
            "personal-b": "renew the lease",
        }
        assert "personal-b" not in on_a, "B's private note crossed to A"
        assert "personal-a" not in on_b, "A's private note crossed to B"

    async def test_an_authenticated_peer_with_no_grant_receives_nothing(
        self, tmp_path,
    ):
        """Enrolment is not sharing. A peer that authenticates but holds
        no grant gets zero operations, including ones written into a
        perfectly valid scope.

        This is the default state of every peer after this change, and
        it is why the fixtures in the end-to-end module have to call
        ``share`` explicitly.
        """
        a = await _start_node(tmp_path, "node-a")
        b = await _start_node(tmp_path, "node-b")
        try:
            await write_note(a, "r1", "shared feed", scope=ROBOT_SCOPE)

            result = await exchange(a, b)

            assert result["success"] is True, result
            assert result["sent"] == 0
            assert await notes_table(b) == {}
        finally:
            await _stop_node(a)
            await _stop_node(b)

    async def test_a_one_sided_grant_shares_nothing(self, two_nodes):
        """Pooling takes two grants, and this is why.

        Each brain's roster is that brain's whole policy toward a peer,
        applied to BOTH directions: what it sends and what it is
        willing to hold. So when only B grants, B is willing to send
        but A refuses to hold, and A is unwilling to send at all.
        Nothing moves.

        That asymmetry is deliberate. The alternative, letting a
        one-sided grant push data onto a brain that never agreed to
        take it, would mean an operator's store could grow rows they
        never consented to, decided entirely by somebody else's
        roster.
        """
        a, b = two_nodes
        b.roster.grant_scope(a.node_id, ROBOT_SCOPE)

        await write_note(a, "from-a", "a's feed", scope=ROBOT_SCOPE)
        await write_note(b, "from-b", "b's feed", scope=ROBOT_SCOPE)

        assert (await exchange(a, b))["success"] is True

        assert await notes_table(a) == {"from-a": "a's feed"}, (
            "A never granted B that scope, so A must refuse what B sent"
        )
        assert await notes_table(b) == {"from-b": "b's feed"}, (
            "A never granted B that scope, so A must send nothing"
        )

        # The other half of the grant makes it flow, on the next
        # exchange and only on it.
        a.roster.grant_scope(b.node_id, ROBOT_SCOPE)
        assert (await exchange(a, b))["success"] is True
        assert set(await notes_table(a)) == {"from-a", "from-b"}
        assert set(await notes_table(b)) == {"from-a", "from-b"}

    async def test_revoking_a_scope_stops_later_writes_but_not_earlier_ones(
        self, two_nodes,
    ):
        """Revocation semantics, stated as an assertion rather than as
        a comment.

        What revocation achieves: the note written after it does not
        cross. What it does NOT achieve: the note written before it is
        still on the peer's disk afterwards, and nothing in this
        codebase can reach across and remove it. The second assertion
        is deliberately written as the thing an operator might hope was
        false.
        """
        a, b = two_nodes
        share(a, b, scope=ROBOT_SCOPE)
        await write_note(a, "before", "crossed already", scope=ROBOT_SCOPE)
        assert (await exchange(a, b))["success"] is True
        assert "before" in await notes_table(b)

        assert a.roster.revoke_scope(b.node_id, ROBOT_SCOPE) is True
        assert b.roster.revoke_scope(a.node_id, ROBOT_SCOPE) is True

        await write_note(a, "after", "must not cross", scope=ROBOT_SCOPE)
        assert (await exchange(a, b))["success"] is True

        on_b = await notes_table(b)
        assert "after" not in on_b, "revocation did not stop future replication"
        assert on_b == {"before": "crossed already"}, (
            "revocation is not recall: what already crossed stays on the "
            "peer's disk, and this test exists to stop anyone documenting "
            "otherwise"
        )

    async def test_a_delete_inherits_the_scope_of_its_write(self, two_nodes):
        """A delete has to reach exactly the peers the write reached.

        The shared note's delete crosses; the private note's delete has
        nowhere to go and stays home. Asserted on the peer's table,
        because a delete that fails to replicate looks identical to a
        healthy sync from the op log.
        """
        a, b = two_nodes
        share(a, b, scope=ROBOT_SCOPE)
        await write_note(a, "shared", "will be deleted", scope=ROBOT_SCOPE)
        assert (await exchange(a, b))["success"] is True
        assert set(await notes_table(b)) == {"shared"}

        hlc = await a.engine.log_operation_async(
            "notes", "delete", "shared", {"id": "shared"}, INHERIT,
        )
        conn = await a.store._conn()
        try:
            await conn.execute("DELETE FROM notes WHERE id = 'shared'")
            await conn.commit()
        finally:
            await a.store._release(conn)
        await a.store._record_tombstone("notes", "shared", hlc)

        assert (await exchange(a, b))["success"] is True
        assert await notes_table(b) == {}, (
            "the delete inherited the wrong scope and never crossed: the row "
            "the user deleted is still live on the peer"
        )


# ---------------------------------------------------------------------------
# Receive-side enforcement, against a peer that does not play fair
# ---------------------------------------------------------------------------


def _op(row_id: str, scope, *, wall: int = 1_700_000_000_000) -> dict:
    """An operation dict as it would arrive off the wire.

    Built by hand rather than through ``log_operation`` because the
    point of these cases is a sender that does not follow the rules,
    and a local engine will not produce one.
    """
    d = SyncOperation(
        op_id=f"op-{row_id}",
        table="notes",
        op_type="insert",
        row_id=row_id,
        data={
            "id": row_id,
            "content": f"content-{row_id}",
            "tags": "[]",
            "importance": "normal",
            "source": "node-remote",
            "created_at": time.time(),
        },
        hlc=HLCTimestamp(wall_ms=wall, counter=0, node_id="node-remote").to_string(),
        origin_node="node-remote",
    ).to_dict()
    if scope is _MISSING:
        d.pop("scope")
    else:
        d["scope"] = scope
    return d


_MISSING = object()


class TestReceiveSideRejectsUngrantedScopes:
    """The send filter runs on the brain that is sending, and that
    brain belongs to somebody else. These drive the receive check with
    operations a modified peer could construct.
    """

    @pytest.fixture
    def engine(self, tmp_path):
        store = MemoryStore(db_path=str(tmp_path / "memory.db"))
        eng = SyncEngine(
            node_id="node-local",
            memory_store=store,
            db_path=str(tmp_path / "wal.db"),
        )
        store.set_sync_engine(eng)
        roster = PeerRoster(db_path=str(tmp_path / "roster.db"))
        eng.set_peer_roster(roster)
        yield eng, store, roster

    async def _notes(self, store) -> set[str]:
        conn = await store._conn()
        try:
            async with conn.execute("SELECT id FROM notes") as cur:
                return {r["id"] for r in await cur.fetchall()}
        finally:
            await store._release(conn)

    async def test_a_granted_scope_is_applied(self, engine):
        eng, store, roster = engine
        roster.grant_scope("node-remote", ROBOT_SCOPE)

        applied = await eng.apply_remote_changes_from_peer(
            [_op("r1", ROBOT_SCOPE)], peer_node_id="node-remote",
        )

        assert applied == 1
        assert await self._notes(store) == {"r1"}

    @pytest.mark.parametrize(
        "scope, why",
        [
            ("some-other-scope", "a scope the peer was never granted"),
            (PRIVATE, "the reserved never-replicate scope, sent explicitly"),
            ("", "an empty scope"),
            (None, "a null scope"),
            (12345, "a scope of the wrong type"),
            ("Robot Events", "a scope name the grammar refuses"),
            ("x" * 200, "a scope name past the length ceiling"),
            (_MISSING, "no scope field at all, as an older peer would send"),
        ],
    )
    async def test_ungranted_or_malformed_scopes_are_refused(
        self, engine, scope, why,
    ):
        """Every one of these must land nowhere: not in the local notes
        table, and not in the local WAL either.

        The WAL assertion matters as much as the table one. An
        operation recorded locally would make this brain a relay for
        memory it has no permission to hold, and the only thing between
        that row and a third brain would be the next peer's send
        filter.
        """
        eng, store, roster = engine
        roster.grant_scope("node-remote", ROBOT_SCOPE)

        applied = await eng.apply_remote_changes_from_peer(
            [_op("r1", scope)], peer_node_id="node-remote",
        )

        assert applied == 0, f"refused input was applied: {why}"
        assert await self._notes(store) == set(), why
        assert eng._wal.count == 0, f"refused op was recorded in the local WAL: {why}"

    async def test_a_peer_with_no_grants_is_refused_everything(self, engine):
        eng, store, roster = engine

        applied = await eng.apply_remote_changes_from_peer(
            [_op("r1", ROBOT_SCOPE), _op("r2", "another")],
            peer_node_id="node-remote",
        )

        assert applied == 0
        assert await self._notes(store) == set()

    async def test_a_grant_for_one_peer_does_not_admit_another(self, engine):
        """Grants are per node_id. A peer presenting a different
        node_id gets nothing, even for a scope that is granted to
        somebody else."""
        eng, store, roster = engine
        roster.grant_scope("node-remote", ROBOT_SCOPE)

        applied = await eng.apply_remote_changes_from_peer(
            [_op("r1", ROBOT_SCOPE)], peer_node_id="node-imposter",
        )

        assert applied == 0
        assert await self._notes(store) == set()

    async def test_the_good_op_in_a_mixed_batch_still_lands(self, engine):
        """A refused operation must not abort the batch. A peer that
        can drop everything after it by prepending one bad op has a
        denial primitive."""
        eng, store, roster = engine
        roster.grant_scope("node-remote", ROBOT_SCOPE)

        applied = await eng.apply_remote_changes_from_peer(
            [_op("bad", "not-granted"), _op("good", ROBOT_SCOPE)],
            peer_node_id="node-remote",
        )

        assert applied == 1
        assert await self._notes(store) == {"good"}

    async def test_scopes_for_peer_denies_when_the_roster_is_missing(self, tmp_path):
        """No roster is not "no restrictions". It is "nothing is
        proven", and the safe reading of that is the empty set."""
        eng = SyncEngine(node_id="n", db_path=str(tmp_path / "wal.db"))

        class _Broken:
            def granted_scopes(self, node_id):
                raise sqlite3.OperationalError("roster is unreadable")

        eng.set_peer_roster(_Broken())
        assert eng.scopes_for_peer("node-remote") == DENY_ALL


# ---------------------------------------------------------------------------
# Send-side enforcement, at the WAL query
# ---------------------------------------------------------------------------


class TestSendSideFilter:
    def _wal(self, tmp_path) -> SyncWAL:
        return SyncWAL(str(tmp_path / "wal.db"))

    def _append(self, wal: SyncWAL, row_id: str, scope: str, wall: int) -> None:
        wal.append(SyncOperation(
            op_id=f"op-{row_id}",
            table="notes",
            op_type="insert",
            row_id=row_id,
            data={"id": row_id},
            hlc=HLCTimestamp(wall_ms=wall, counter=0, node_id="n").to_string(),
            origin_node="n",
            scope=scope,
        ))

    def test_only_granted_scopes_are_selected(self, tmp_path):
        wal = self._wal(tmp_path)
        self._append(wal, "r1", ROBOT_SCOPE, 1_700_000_000_000)
        self._append(wal, "r2", "finance", 1_700_000_000_001)
        self._append(wal, "r3", PRIVATE, 1_700_000_000_002)

        got = wal.get_changes_for_peer({}, allowed_scopes={ROBOT_SCOPE})

        assert [op.row_id for op in got] == ["r1"]

    def test_an_empty_grant_set_selects_nothing(self, tmp_path):
        """The bug this shape exists to prevent: an empty ``IN ()`` list
        built as no clause at all, which would send everything to the
        peer that was granted the least."""
        wal = self._wal(tmp_path)
        self._append(wal, "r1", ROBOT_SCOPE, 1_700_000_000_000)
        self._append(wal, "r2", PRIVATE, 1_700_000_000_001)

        assert wal.get_changes_for_peer({}, allowed_scopes=frozenset()) == []
        assert wal.get_changes_for_peer({}, allowed_scopes=DENY_ALL) == []

    def test_private_is_never_selected_even_if_it_is_granted(self, tmp_path):
        """A hand-edited grant row naming ``private`` must not widen
        anything. ``normalise_scope_set`` drops it before it can reach
        the query."""
        wal = self._wal(tmp_path)
        self._append(wal, "r1", PRIVATE, 1_700_000_000_000)

        assert wal.get_changes_for_peer({}, allowed_scopes={PRIVATE}) == []

    def test_allowed_scopes_has_no_default(self, tmp_path):
        """The send filter is peer-facing, so omitting the grant set is
        a TypeError rather than a silently unrestricted query."""
        wal = self._wal(tmp_path)
        with pytest.raises(TypeError):
            wal.get_changes_for_peer({})

    def test_the_per_origin_fallback_path_is_also_filtered(self, tmp_path):
        """``get_changes_for_peer`` has three query shapes: no
        watermarks, the clause form, and the coarse prefilter past
        ``_MAX_VC_CLAUSES``. All three have to carry the scope filter,
        and only the middle one is exercised by the tests above.
        """
        import memory.sync as sync_mod

        wal = self._wal(tmp_path)
        self._append(wal, "r1", ROBOT_SCOPE, 1_700_000_000_000)
        self._append(wal, "r2", PRIVATE, 1_700_000_000_001)

        # Clause form: a watermark the peer already holds for origin n.
        vc = {"n": HLCTimestamp(
            wall_ms=1_600_000_000_000, counter=0, node_id="n",
        ).to_string()}
        assert [op.row_id for op in wal.get_changes_for_peer(
            vc, allowed_scopes={ROBOT_SCOPE},
        )] == ["r1"]

        # Coarse prefilter: force the fallback by lowering the cap.
        original = sync_mod._MAX_VC_CLAUSES
        try:
            sync_mod._MAX_VC_CLAUSES = 0
            assert [op.row_id for op in wal.get_changes_for_peer(
                vc, allowed_scopes={ROBOT_SCOPE},
            )] == ["r1"]
            assert wal.get_changes_for_peer(vc, allowed_scopes=frozenset()) == []
        finally:
            sync_mod._MAX_VC_CLAUSES = original


# ---------------------------------------------------------------------------
# The WAL column, and what it does to history
# ---------------------------------------------------------------------------


class TestWalScopeColumn:
    def test_pre_existing_rows_migrate_to_private(self, tmp_path):
        """A WAL written before this column existed. Every row in it is
        private after the migration, permanently, and no backfill tries
        to guess otherwise.

        Defaulting to a shareable scope instead would take an
        operator's entire personal history and mark it poolable because
        of a schema change they never asked for.
        """
        db = str(tmp_path / "legacy_wal.db")
        conn = sqlite3.connect(db)
        try:
            conn.execute("""
                CREATE TABLE sync_wal (
                    op_id TEXT PRIMARY KEY,
                    table_name TEXT NOT NULL,
                    op_type TEXT NOT NULL,
                    row_id TEXT NOT NULL,
                    data TEXT NOT NULL,
                    hlc TEXT NOT NULL,
                    origin_node TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    synced_to TEXT DEFAULT '[]'
                )
            """)
            conn.execute(
                "INSERT INTO sync_wal VALUES (?,?,?,?,?,?,?,?,?)",
                ("legacy-1", "notes", "insert", "n1", '{"id": "n1"}',
                 "1700000000000:0:old-node", "old-node", time.time(), "[]"),
            )
            conn.commit()
        finally:
            conn.close()

        wal = SyncWAL(db)

        rows = wal.get_changes_since("0:0:")
        assert len(rows) == 1
        assert rows[0].scope == PRIVATE
        assert wal.get_changes_for_peer({}, allowed_scopes={ROBOT_SCOPE}) == []
        assert wal.get_changes_for_peer({}, allowed_scopes={PRIVATE}) == []

    def test_an_unscoped_write_is_private(self, tmp_path):
        eng = SyncEngine(node_id="n", db_path=str(tmp_path / "wal.db"))
        eng.log_operation("notes", "insert", "n1", {"id": "n1"})

        assert eng._wal.get_changes_since("0:0:")[0].scope == PRIVATE

    def test_a_junk_scope_is_stored_as_private(self, tmp_path):
        """Normalisation happens on the way in as well as on the way
        out, so a name the grammar refuses never becomes durable."""
        eng = SyncEngine(node_id="n", db_path=str(tmp_path / "wal.db"))
        eng.log_operation("notes", "insert", "n1", {"id": "n1"}, "Not A Scope")

        assert eng._wal.get_changes_since("0:0:")[0].scope == PRIVATE

    def test_a_row_read_back_with_a_corrupt_scope_is_private(self, tmp_path):
        """The WAL is a file an operator, a restore, or a half-applied
        migration can leave holding anything."""
        eng = SyncEngine(node_id="n", db_path=str(tmp_path / "wal.db"))
        eng.log_operation("notes", "insert", "n1", {"id": "n1"}, ROBOT_SCOPE)
        conn = sqlite3.connect(eng._wal._db_path)
        try:
            conn.execute("UPDATE sync_wal SET scope = 'NOT VALID'")
            conn.commit()
        finally:
            conn.close()

        assert eng._wal.get_changes_since("0:0:")[0].scope == PRIVATE

    def test_scope_for_row_returns_private_for_an_unknown_row(self, tmp_path):
        wal = SyncWAL(str(tmp_path / "wal.db"))
        assert wal.scope_for_row("notes", "never-written") == PRIVATE

    def test_inherit_takes_the_newest_operation(self, tmp_path):
        eng = SyncEngine(node_id="n", db_path=str(tmp_path / "wal.db"))
        eng.log_operation("notes", "insert", "n1", {"id": "n1"}, ROBOT_SCOPE)
        eng.log_operation("notes", "delete", "n1", {"id": "n1"}, INHERIT)

        ops = {op.op_type: op.scope for op in eng._wal.get_changes_since("0:0:")}
        assert ops == {"insert": ROBOT_SCOPE, "delete": ROBOT_SCOPE}

    def test_inherit_on_an_unknown_row_is_private(self, tmp_path):
        eng = SyncEngine(node_id="n", db_path=str(tmp_path / "wal.db"))
        eng.log_operation("notes", "delete", "gone", {"id": "gone"}, INHERIT)

        assert eng._wal.get_changes_since("0:0:")[0].scope == PRIVATE


# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------


class TestScopeVocabulary:
    @pytest.mark.parametrize("value", [
        None, "", "   ", 0, 1, 3.4, [], {}, object(),
        "Robot Events", "robot events", "robot/events", "robot\nevents",
        "-leading", "trailing-", ".dot", "UPPER!", "x" * 65,
        "__inherit__", PRIVATE,
    ])
    def test_everything_ambiguous_normalises_to_private(self, value):
        assert normalise_scope(value) == PRIVATE
        assert is_shareable(value) is False

    @pytest.mark.parametrize("raw, expected", [
        ("robot-events", "robot-events"),
        ("Robot-Events", "robot-events"),
        ("  robot-events  ", "robot-events"),
        ("robot_events", "robot_events"),
        ("robot.events.v2", "robot.events.v2"),
        ("a", "a"),
        ("x" * 64, "x" * 64),
    ])
    def test_valid_names_survive_normalisation(self, raw, expected):
        assert normalise_scope(raw) == expected
        assert is_shareable(raw) is True

    def test_require_shareable_scope_refuses_private_by_name(self):
        with pytest.raises(InvalidScopeError) as exc:
            require_shareable_scope(PRIVATE)
        assert PRIVATE in str(exc.value)

    def test_require_shareable_scope_refuses_junk(self):
        with pytest.raises(InvalidScopeError):
            require_shareable_scope("Robot Events")

    def test_normalise_scope_set_drops_private_and_junk(self):
        assert normalise_scope_set(
            [ROBOT_SCOPE, PRIVATE, "", None, "Bad Name", "FINANCE"]
        ) == frozenset({ROBOT_SCOPE, "finance"})

    def test_normalise_scope_set_of_nothing_is_empty(self):
        assert normalise_scope_set(None) == frozenset()
        assert normalise_scope_set([]) == frozenset()

    def test_a_bare_string_is_one_scope_not_twelve_characters(self):
        """``allowed_scopes="robot-events"`` must not iterate into
        single-character scope names, every one of which passes the
        grammar. That would be a widening bug, and widening is the one
        direction this module must never fail in."""
        assert normalise_scope_set("robot-events") == frozenset({"robot-events"})
        assert "r" not in normalise_scope_set("robot-events")

    def test_the_inherit_sentinel_is_not_a_valid_scope_name(self):
        """It must fail the grammar, so a sentinel that leaks into
        storage or onto the wire reads as private like any other junk
        rather than as a scope some peer might have granted."""
        assert normalise_scope(INHERIT) == PRIVATE


# ---------------------------------------------------------------------------
# Grants on the roster
# ---------------------------------------------------------------------------


class TestRosterScopeGrants:
    @pytest.fixture
    def roster(self, tmp_path):
        return PeerRoster(db_path=str(tmp_path / "roster.db"))

    def test_nothing_is_granted_by_default(self, roster):
        assert roster.granted_scopes("node-b") == frozenset()

    def test_grant_then_read_back(self, roster):
        roster.grant_scope("node-b", "Robot-Events")
        assert roster.granted_scopes("node-b") == frozenset({"robot-events"})

    def test_grants_do_not_leak_between_peers(self, roster):
        roster.grant_scope("node-b", ROBOT_SCOPE)
        assert roster.granted_scopes("node-c") == frozenset()

    def test_granting_private_is_refused(self, roster):
        with pytest.raises(InvalidScopeError):
            roster.grant_scope("node-b", PRIVATE)
        assert roster.granted_scopes("node-b") == frozenset()

    def test_granting_a_malformed_name_is_refused(self, roster):
        with pytest.raises(InvalidScopeError):
            roster.grant_scope("node-b", "Robot Events")
        assert roster.granted_scopes("node-b") == frozenset()

    def test_a_blank_node_id_is_refused(self, roster):
        with pytest.raises(ValueError):
            roster.grant_scope("", ROBOT_SCOPE)

    def test_revoke_removes_exactly_one_grant(self, roster):
        roster.grant_scope("node-b", ROBOT_SCOPE)
        roster.grant_scope("node-b", "finance")

        assert roster.revoke_scope("node-b", ROBOT_SCOPE) is True
        assert roster.granted_scopes("node-b") == frozenset({"finance"})

    def test_revoking_something_never_granted_reports_false(self, roster):
        assert roster.revoke_scope("node-b", ROBOT_SCOPE) is False

    def test_granting_twice_is_idempotent(self, roster):
        roster.grant_scope("node-b", ROBOT_SCOPE)
        roster.grant_scope("node-b", ROBOT_SCOPE, note="second time")
        assert roster.granted_scopes("node-b") == frozenset({ROBOT_SCOPE})
        assert len(roster.list_scope_grants()) == 1

    def test_a_hand_edited_private_grant_cannot_widen_anything(self, roster):
        """Defence in depth against the storage layer itself. Even a row
        inserted straight into SQLite naming the reserved scope is
        dropped on read."""
        conn = sqlite3.connect(roster.db_path)
        try:
            conn.execute(
                "INSERT INTO peer_scope_grants (node_id, scope, granted_at) "
                "VALUES (?, ?, ?)",
                ("node-b", PRIVATE, time.time()),
            )
            conn.commit()
        finally:
            conn.close()

        assert roster.granted_scopes("node-b") == frozenset()

    def test_list_peers_reports_what_a_peer_actually_receives(self, roster):
        invite = roster.invite_peer("laptop")
        roster.verify_peer(invite["secret"], node_id="node-b")
        roster.grant_scope("node-b", ROBOT_SCOPE)

        rows = roster.list_peers()
        assert len(rows) == 1
        assert rows[0]["node_id"] == "node-b"
        assert rows[0]["scopes"] == [ROBOT_SCOPE]

    def test_an_enrolled_peer_starts_with_no_scopes(self, roster):
        invite = roster.invite_peer("laptop")
        roster.verify_peer(invite["secret"], node_id="node-b")

        assert roster.list_peers()[0]["scopes"] == [], (
            "enrolling a brain must not hand it any memory"
        )


# ---------------------------------------------------------------------------
# Structural guard on the two peer-facing call sites
# ---------------------------------------------------------------------------


class TestPeerPathsUseTheScopedEntryPoint:
    """``apply_remote_changes`` still exists without a peer boundary,
    for local bundle import and for the older tests that predate
    scopes. That is a door, so this asserts nothing peer-facing walks
    through it.

    Read as source rather than by calling: the property is "no peer
    path calls the unscoped form", and only the source can answer a
    question phrased as "no".
    """

    def _source(self, relative: str) -> str:
        from pathlib import Path
        return (Path(__file__).resolve().parents[1] / relative).read_text()

    def test_the_sync_endpoint_uses_the_scoped_entry_point(self):
        src = self._source("api/server.py")
        assert "apply_remote_changes_from_peer(" in src
        # The bare form must not appear anywhere in the endpoint file.
        bare = [
            line for line in src.splitlines()
            if "apply_remote_changes(" in line and "_from_peer" not in line
        ]
        assert bare == [], (
            "api/server.py calls the unscoped apply_remote_changes: the "
            f"receive-side scope check is bypassed on that path. {bare}"
        )

    def test_the_dialling_exchange_uses_the_scoped_entry_point(self):
        src = self._source("memory/sync.py")
        start = src.index("async def _handshake_and_exchange")
        end = src.index("def export_to_bundle")
        body = src[start:end]
        assert "apply_remote_changes_from_peer(" in body
        bare = [
            line for line in body.splitlines()
            if "apply_remote_changes(" in line and "_from_peer" not in line
        ]
        assert bare == [], (
            "_handshake_and_exchange calls the unscoped apply_remote_changes: "
            f"the receive-side scope check is bypassed on that path. {bare}"
        )

    def test_no_peer_path_calls_get_changes_for_peer_without_scopes(self):
        """``allowed_scopes`` has no default, so this would be a
        TypeError at runtime. Pinned here anyway because a runtime
        TypeError inside a websocket handler is caught by the
        endpoint's broad handler and logged as a sync failure, which
        reads as a network problem.
        """
        import re

        # A CALL, not a mention. Prose in a docstring names the method
        # in backticks and a comment names it with a trailing period;
        # a call is always followed by ``(`` or, when it is handed to
        # ``asyncio.to_thread`` as a bare reference, by ``,``.
        call = re.compile(r"[\w.]*get_changes_for_peer\s*[(,]")
        for path in ("api/server.py", "memory/sync.py"):
            lines = self._source(path).splitlines()
            checked = 0
            for idx, line in enumerate(lines):
                if not call.search(line) or line.strip().startswith("#"):
                    continue
                if line.strip().startswith("def "):
                    continue
                checked += 1
                window = "\n".join(lines[idx:idx + 8])
                assert "allowed_scopes" in window, (
                    f"{path}:{idx + 1} calls get_changes_for_peer without an "
                    "explicit grant set"
                )
            assert checked, (
                f"{path} no longer calls get_changes_for_peer at all; this "
                "guard is now pinning nothing and the send filter has moved"
            )
