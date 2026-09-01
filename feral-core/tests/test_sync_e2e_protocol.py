"""End-to-end federated-sync protocol harness.

Everything else in ``tests/`` that claims to cover sync either calls
``apply_remote_changes`` directly or hands one engine's whole WAL to the
other by hand. Neither touches the wire, so three defects shipped that a
single honest round trip would have caught:

  1. ``websockets.connect`` was called with no ``max_size``, so the
     library default of 1 MiB applied to every frame the client read.
     The change set went out as ONE frame. Any peer holding more than a
     megabyte of history was unsyncable, permanently.
  2. Nothing pruned ``sync_wal``, and ``get_changes_since`` pulled the
     whole table into Python before filtering it.
  3. The change set for a peer was cut at ``remote_vc[self.node_id]``:
     the peer's high-water mark for MY writes, applied as the cutoff for
     ops of EVERY origin. A relay node silently stopped forwarding.

So this module drives the real ``api.server.sync_peer_endpoint`` over a
real websocket, from the real ``SyncEngine.sync_with_peer``, and asserts
on MATERIALISED TABLE CONTENTS on the receiving side. Never on the op
log: the op log is what both sides already agree on by construction, so
an assertion against it cannot fail for the reasons that matter.

Harness shape
-------------
``sync_peer_endpoint`` reads the process-global ``api.server.state`` for
its store and engine, so a node's identity is bound at request time by
:func:`_serving_as`, not per app instance. Exchanges in this module are
therefore driven one at a time. That is a limitation of the endpoint's
global, not of the protocol: :func:`exchange` swaps the global for the
duration of one handshake and restores it after.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

import pytest
import uvicorn

import api.server as api_server
import memory.sync as sync_mod
from memory.store import MemoryStore
from memory.sync import SyncEngine

pytestmark = pytest.mark.timeout(120)

_PASSPHRASE = "e2e-harness-passphrase"

# One frame of the old protocol had to carry the entire change set. The
# websockets client default receive limit is this many bytes, and it is
# the exact number defect 1 tripped over.
WS_DEFAULT_MAX_SIZE = 1024 * 1024


# ---------------------------------------------------------------------------
# Node: a store, an engine, and a real HTTP server in front of them
# ---------------------------------------------------------------------------


@dataclass
class _Node:
    node_id: str
    store: MemoryStore
    engine: SyncEngine
    port: int
    server: uvicorn.Server
    task: asyncio.Task
    state: object


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


class _FakeState:
    """The two attributes ``sync_peer_endpoint`` actually reads."""

    def __init__(self, engine: SyncEngine, store: MemoryStore):
        self.sync_engine = engine
        self.memory = store


async def _start_node(
    tmp_path,
    node_id: str,
    *,
    ws_max_size: Optional[int] = None,
) -> _Node:
    """Bring up a store, an engine and a uvicorn server for one node.

    ``lifespan="off"`` because the real app's startup boots the whole
    brain (models, channels, ~/.feral). We want the route and its
    middleware stack, not the daemon.

    ``ws_max_size`` is uvicorn's per-frame receive limit. Left at the
    uvicorn default (16 MiB) unless a test is deliberately pinning the
    behaviour of a peer running the stock ``websockets`` 1 MiB default.
    """
    db = tmp_path / f"{node_id}.db"
    wal = tmp_path / f"{node_id}_wal.db"
    store = MemoryStore(db_path=str(db))
    engine = SyncEngine(node_id=node_id, memory_store=store, db_path=str(wal))
    store.set_sync_engine(engine)

    port = _free_port()
    kwargs = dict(
        host="127.0.0.1",
        port=port,
        log_level="error",
        lifespan="off",
        access_log=False,
    )
    if ws_max_size is not None:
        kwargs["ws_max_size"] = ws_max_size
    config = uvicorn.Config(api_server.app, **kwargs)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError(f"uvicorn for {node_id} did not start")
        await asyncio.sleep(0.02)

    return _Node(
        node_id=node_id,
        store=store,
        engine=engine,
        port=port,
        server=server,
        task=task,
        state=_FakeState(engine, store),
    )


async def _stop_node(node: _Node) -> None:
    node.server.should_exit = True
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(node.task, timeout=10)
    if not node.task.done():
        node.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await node.task
    await node.store.aclose()


@contextlib.contextmanager
def _serving_as(node: _Node):
    """Point ``api.server.state`` at ``node`` for one exchange."""
    original = api_server.state
    api_server.state = node.state
    try:
        yield
    finally:
        api_server.state = original


async def exchange(client: _Node, server: _Node, **kwargs) -> dict:
    """One real handshake: ``client`` dials ``server`` over a websocket.

    Returns whatever ``sync_with_peer`` returns, unmodified, including
    the failure dicts. Tests assert on tables, not on this, but a
    failure dict is what the current code produces for defect 1 and
    reporting it makes the before/after evidence legible.
    """
    client.engine._peers[server.node_id] = {
        "address": "127.0.0.1",
        "port": server.port,
        "discovered_at": time.time(),
        "source": "test",
    }
    kwargs.setdefault("max_attempts", 1)
    kwargs.setdefault("connect_timeout", 10.0)
    kwargs.setdefault("handshake_timeout", 30.0)
    with _serving_as(server):
        return await client.engine.sync_with_peer(server.node_id, **kwargs)


# ---------------------------------------------------------------------------
# Writes and materialised-state assertions
# ---------------------------------------------------------------------------


async def write_note(node: _Node, note_id: str, content: str) -> str:
    """Write a note the way the real store does: WAL op first so the
    HLC lands in the row, then the row.

    ``notes_legacy.save_note`` also enqueues embeddings and writes a
    knowledge triple; neither is under test here and both cost seconds
    at the volumes defect 1 needs.
    """
    now = time.time()
    data = {
        "id": note_id,
        "content": content,
        "tags": "[]",
        "importance": "normal",
        "source": node.node_id,
        "created_at": now,
    }
    hlc = await node.engine.log_operation_async("notes", "insert", note_id, data)
    conn = await node.store._conn()
    try:
        await conn.execute(
            "INSERT INTO notes (id, content, tags, importance, source, "
            "created_at, updated_at, hlc_string) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET content = excluded.content, "
            "hlc_string = excluded.hlc_string",
            (note_id, content, "[]", "normal", node.node_id, now, now, hlc),
        )
        await conn.commit()
    finally:
        await node.store._release(conn)
    return hlc


async def notes_table(node: _Node) -> dict[str, str]:
    """The materialised ``notes`` table as ``{id: content}``.

    This, and only this, is what convergence means. The WAL agreeing is
    not convergence: ``_apply_to_memory`` rejects ops the WAL happily
    holds (any ``op_type`` outside insert/delete, any table outside
    ``_SYNC_ALLOWED_TABLES``), so a WAL-derived assertion passes on data
    that never reached the user's store.
    """
    conn = await node.store._conn()
    try:
        async with conn.execute("SELECT id, content FROM notes") as cur:
            return {r["id"]: r["content"] for r in await cur.fetchall()}
    finally:
        await node.store._release(conn)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _passphrase(monkeypatch):
    """Both sides of the handshake read the shared secret at call time:
    the client from ``memory.sync.SYNC_PASSPHRASE``, the server from the
    env var falling back to that same global. Patching only the global
    exercises the fallback and keeps the suite's env-leak guard quiet.
    """
    monkeypatch.delenv("FERAL_SYNC_PASSPHRASE", raising=False)
    monkeypatch.setattr(sync_mod, "SYNC_PASSPHRASE", _PASSPHRASE)
    # No TLS: build_client_ssl_context() must return None so the client
    # dials ws:// and uvicorn's plain listener answers.
    for var in (
        "FERAL_SYNC_TLS_CERT",
        "FERAL_SYNC_TLS_KEY",
        "FERAL_SYNC_TLS_CA",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(sync_mod, "SYNC_TLS_CERT", "")
    monkeypatch.setattr(sync_mod, "SYNC_TLS_KEY", "")
    monkeypatch.setattr(sync_mod, "SYNC_TLS_CA", "")


@pytest.fixture
async def two_nodes(tmp_path):
    a = await _start_node(tmp_path, "node-a")
    b = await _start_node(tmp_path, "node-b")
    try:
        yield a, b
    finally:
        await _stop_node(a)
        await _stop_node(b)


@pytest.fixture
async def three_nodes(tmp_path):
    """A <-> B <-> C. B relays; A and C never speak directly."""
    a = await _start_node(tmp_path, "node-a")
    b = await _start_node(tmp_path, "node-b")
    c = await _start_node(tmp_path, "node-c")
    try:
        yield a, b, c
    finally:
        await _stop_node(a)
        await _stop_node(b)
        await _stop_node(c)


# ---------------------------------------------------------------------------
# 0. The harness itself reaches the endpoint
# ---------------------------------------------------------------------------


class TestHarnessReachesTheWire:
    async def test_single_note_round_trip(self, two_nodes):
        """The baseline the other tests build on: one note, one real
        handshake, asserted on B's ``notes`` table."""
        a, b = two_nodes
        await write_note(a, "n1", "hello from a")

        result = await exchange(a, b)

        assert result["success"] is True, result
        assert await notes_table(b) == {"n1": "hello from a"}

    async def test_bidirectional_round_trip(self, two_nodes):
        """One handshake carries both directions; both tables converge."""
        a, b = two_nodes
        await write_note(a, "from-a", "a content")
        await write_note(b, "from-b", "b content")

        result = await exchange(a, b)

        assert result["success"] is True, result
        expected = {"from-a": "a content", "from-b": "b content"}
        assert await notes_table(a) == expected
        assert await notes_table(b) == expected

    async def test_wrong_passphrase_is_rejected(self, two_nodes, monkeypatch):
        """Negative control. If a bad secret still synced, nothing else
        in this module would be proving anything.

        Client and server share one process, so they also share
        ``memory.sync.SYNC_PASSPHRASE``. The env var is the only lever
        that separates them, and it is restored inside the test body
        rather than by ``monkeypatch`` because the suite's env-leak
        guard snapshots earlier than monkeypatch unwinds.
        """
        a, b = two_nodes
        await write_note(a, "n1", "should not land")
        import os

        os.environ["FERAL_SYNC_PASSPHRASE"] = "server-side-secret"
        try:
            monkeypatch.setattr(sync_mod, "SYNC_PASSPHRASE", "client-side-secret")
            result = await exchange(a, b)
        finally:
            os.environ.pop("FERAL_SYNC_PASSPHRASE", None)

        assert result["success"] is False
        assert await notes_table(b) == {}

    async def test_unset_passphrase_is_rejected(self, two_nodes, monkeypatch):
        """The zero-auth guard from audit-r12 A2, exercised over the
        wire rather than by reading the branch."""
        a, b = two_nodes
        await write_note(a, "n1", "should not land")
        monkeypatch.setattr(sync_mod, "SYNC_PASSPHRASE", "")

        result = await exchange(a, b)

        assert result["success"] is False
        assert await notes_table(b) == {}


# ---------------------------------------------------------------------------
# 1. The 1 MiB frame cap
# ---------------------------------------------------------------------------


async def _fill(node: _Node, count: int, size: int, prefix: str) -> dict[str, str]:
    expected = {}
    for i in range(count):
        note_id = f"{prefix}{i:05d}"
        content = f"{note_id}-" + ("x" * size)
        await write_note(node, note_id, content)
        expected[note_id] = content
    return expected


class TestOversizeChangeSet:
    """Defect 1. The owner's live WAL is 16,324 ops / 6.24 MiB of
    payload; a first sync against a fresh peer shipped all of it in one
    frame and died on the client's 1 MiB receive limit.
    """

    async def test_client_receives_change_set_over_one_mib(self, two_nodes):
        """B holds ~1.6 MiB of notes. A dials B and must end up with all
        of them.

        Against the pre-fix code the client's ``websockets.connect`` had
        no ``max_size``, so reading B's change set raised
        ``PayloadTooBig`` and A's ``notes`` table stayed empty.
        """
        a, b = two_nodes
        expected = await _fill(b, 320, 5_000, "big")
        payload = len(json.dumps([op.to_dict() for op in b.engine._wal.get_changes_since("0:0:")]))
        assert payload > WS_DEFAULT_MAX_SIZE, (
            f"test data must exceed the 1 MiB default to be meaningful, got {payload}"
        )

        result = await exchange(a, b)

        assert result["success"] is True, result
        assert await notes_table(a) == expected

    async def test_no_single_frame_exceeds_one_mib(self, tmp_path):
        """The mirror direction, and the invariant that makes the fix
        general rather than a bigger constant.

        The server here runs with uvicorn's frame limit pinned to the
        stock ``websockets`` default. That is not what FERAL's own
        uvicorn ships (16 MiB); it stands in for any peer speaking the
        library's defaults, and it pins the property we actually want:
        the sender must bound its frames, whatever the reader allows.
        """
        a = await _start_node(tmp_path, "node-a")
        b = await _start_node(tmp_path, "node-b", ws_max_size=WS_DEFAULT_MAX_SIZE)
        try:
            expected = await _fill(a, 320, 5_000, "big")

            result = await exchange(a, b)

            assert result["success"] is True, result
            assert await notes_table(b) == expected
        finally:
            await _stop_node(a)
            await _stop_node(b)


class TestFraming:
    """The chunking contract itself, away from the wire.

    The end-to-end tests above prove a large change set arrives. These
    pin WHY, so a future change that quietly raises the budget back to
    one frame per exchange fails here rather than on someone's laptop.
    """

    def test_frames_stay_inside_the_budget(self):
        ops = [
            sync_mod.SyncOperation(
                op_id=f"op{i}", table="notes", op_type="insert", row_id=f"r{i}",
                data={"id": f"r{i}", "content": "x" * 5_000},
                hlc=f"{1000 + i}:0:n", origin_node="n",
            )
            for i in range(320)
        ]
        frames = sync_mod.sync_data_frames(ops)

        sizes = [len(json.dumps(f)) for f in frames]
        assert max(sizes) <= sync_mod.SYNC_MAX_FRAME_BYTES, max(sizes)
        assert len(frames) > 1, "1.6 MiB of ops must not fit in one frame"
        assert sum(len(f["changes"]) for f in frames) == len(ops), "ops lost in framing"
        assert [f["more"] for f in frames] == [True] * (len(frames) - 1) + [False]

    def test_empty_change_set_is_still_a_terminal_frame(self):
        """"I have nothing for you" has to be said out loud, or the
        reader waits for a frame that never comes."""
        frames = sync_mod.sync_data_frames([])
        assert len(frames) == 1
        assert frames[0]["changes"] == []
        assert frames[0]["more"] is False

    def test_single_op_over_the_budget_gets_its_own_frame(self):
        """Operations are indivisible. The alternative to shipping one
        oversize frame is dropping the op, which is why the reader's
        ceiling is well above the sender's budget."""
        huge = sync_mod.SyncOperation(
            op_id="op", table="notes", op_type="insert", row_id="r",
            data={"id": "r", "content": "x" * (sync_mod.SYNC_MAX_FRAME_BYTES * 2)},
            hlc="1000:0:n", origin_node="n",
        )
        frames = sync_mod.sync_data_frames([huge])
        assert len(frames) == 1
        assert len(frames[0]["changes"]) == 1
        assert len(json.dumps(frames[0])) < sync_mod.SYNC_MAX_RECV_BYTES

    async def test_reader_accepts_a_pre_chunking_peer(self):
        """Backward compatibility, stated as a test.

        A peer on the old build sends ONE ``sync_data`` message with no
        ``more`` key at all. That reads as falsy, so the loop stops
        after one frame instead of blocking forever on a second.
        """
        old_style = [{
            "type": "sync_data",
            "changes": [{"op_id": "a", "table": "notes", "op_type": "insert",
                         "row_id": "r", "data": {}, "hlc": "1:0:n",
                         "origin_node": "n", "timestamp": 0.0}],
        }]
        pending = iter(old_style)

        async def _recv():
            return next(pending)

        got = await sync_mod.recv_sync_data(_recv)
        assert len(got) == 1

    async def test_reader_reassembles_a_chunked_stream(self):
        ops = [
            sync_mod.SyncOperation(
                op_id=f"op{i}", table="notes", op_type="insert", row_id=f"r{i}",
                data={"id": f"r{i}", "content": "x" * 5_000},
                hlc=f"{1000 + i}:0:n", origin_node="n",
            )
            for i in range(320)
        ]
        pending = iter(sync_mod.sync_data_frames(ops))

        async def _recv():
            return next(pending)

        got = await sync_mod.recv_sync_data(_recv)
        assert [c["op_id"] for c in got] == [op.op_id for op in ops]


# ---------------------------------------------------------------------------
# 2. WAL retention and the query that reads it
# ---------------------------------------------------------------------------


class _RecordingConnect:
    """Wrap ``sqlite3.connect`` and record every statement executed.

    Used to prove that ``get_changes_since`` filters in SQL. A timing
    assertion would say the same thing far less reliably.
    """

    def __init__(self, real):
        self._real = real
        self.statements: list[str] = []

    def __call__(self, *args, **kwargs):
        conn = self._real(*args, **kwargs)
        conn.set_trace_callback(self.statements.append)
        return conn


class TestWalRetention:
    """Defect 2. Measured on the owner's live WAL at the time of the
    fix: 16,324 ops, 12 MB on disk, 6.24 MiB of ``data`` payload,
    132 days of history, every row still carrying ``synced_to = '[]'``,
    and no ``DELETE FROM sync_wal`` anywhere in the tree.
    """

    async def test_get_changes_since_filters_in_sql(self, tmp_path):
        """The watermark must reach SQLite, not a Python list
        comprehension over ``fetchall()``."""
        a = await _start_node(tmp_path, "node-a")
        try:
            await _fill(a, 200, 50, "op")
            wal = a.engine._wal
            top = wal.get_changes_since("0:0:")[-1].hlc

            recorder = _RecordingConnect(sqlite3.connect)
            original = sqlite3.connect
            try:
                sync_mod.sqlite3.connect = recorder
                assert wal.get_changes_since(top) == []
            finally:
                sync_mod.sqlite3.connect = original

            selects = [s for s in recorder.statements if "sync_wal" in s and "SELECT" in s.upper()]
            assert selects, "expected a SELECT against sync_wal"
            assert all("hlc" in s and ">" in s for s in selects), (
                "get_changes_since must push the HLC watermark into SQL; "
                f"statements were: {selects}"
            )
        finally:
            await _stop_node(a)

    async def test_prune_exists_and_bounds_the_wal(self, tmp_path):
        """A retention policy has to exist at all, and applying it must
        drop history older than the horizon."""
        a = await _start_node(tmp_path, "node-a")
        try:
            await _fill(a, 30, 50, "old")
            wal = a.engine._wal
            # Backdate everything by a year.
            conn = sqlite3.connect(wal._db_path)
            try:
                conn.execute(
                    "UPDATE sync_wal SET timestamp = ?", (time.time() - 365 * 86400,)
                )
                conn.commit()
            finally:
                conn.close()
            await _fill(a, 5, 50, "new")

            assert wal.count == 35
            removed = wal.prune()
            assert removed == 30, f"expected the 30 backdated ops to go, removed={removed}"
            assert wal.count == 5
        finally:
            await _stop_node(a)

    async def test_prune_keeps_history_inside_the_horizon(self, tmp_path):
        """The other half of the policy: recent ops are NOT dropped, and
        a peer that syncs after a prune still converges."""
        a = await _start_node(tmp_path, "node-a")
        b = await _start_node(tmp_path, "node-b")
        try:
            expected = await _fill(a, 10, 50, "recent")
            assert a.engine._wal.prune() == 0

            result = await exchange(a, b)

            assert result["success"] is True, result
            assert await notes_table(b) == expected
        finally:
            await _stop_node(a)
            await _stop_node(b)


# ---------------------------------------------------------------------------
# 3. The N-node watermark
# ---------------------------------------------------------------------------


class TestRelayWatermark:
    """Defect 3. ``peer_has = remote_vc.get(self.node_id, "0:0:")`` is
    the peer's high-water mark for MY OWN writes, used as the cutoff for
    ops of every origin. A relay that also writes locally stops
    forwarding anything older than its own last write.
    """

    async def test_relay_forwards_third_party_ops(self, three_nodes):
        """A -> B -> C, with B's own write ordered AFTER A's.

        Ordering is what makes this deterministic:
          1. A writes ``from-a``   (HLC t1)
          2. B writes ``from-b``   (HLC t2 > t1)
          3. B <-> C, so C now has ``from-b`` and C's clock for B is t2
          4. A <-> B, so B now holds ``from-a`` at t1
          5. B <-> C, and B must send ``from-a``

        At step 5 the old cutoff is C's mark for B's writes, t2. A's op
        is older than that, so it is filtered out and C never sees it.
        Nothing errors; the data is simply gone, forever.
        """
        a, b, c = three_nodes

        await write_note(a, "from-a", "a content")
        await write_note(b, "from-b", "b content")

        assert (await exchange(b, c))["success"] is True
        assert set(await notes_table(c)) == {"from-b"}

        assert (await exchange(a, b))["success"] is True
        assert set(await notes_table(b)) == {"from-a", "from-b"}

        assert (await exchange(b, c))["success"] is True

        assert await notes_table(c) == {
            "from-a": "a content",
            "from-b": "b content",
        }, "relay dropped the third-party op: C converged without A's write"

    async def test_quiet_node_does_not_resend_its_whole_wal(self, three_nodes):
        """The same bug's client-side face, and the reason it burns the
        owner's bandwidth every 30 seconds.

        A never writes anything of its own; it only relays C's op. So
        ``remote_vc[A.node_id]`` is absent on B and the cutoff falls
        back to ``"0:0:"``, so A ships its ENTIRE WAL on every handshake,
        forever, no matter how much of it B already has.

        A two-node version of this cannot fail: with only A and B in
        play the peer's mark for A's writes happens to be the right
        cutoff for the only ops that exist. It takes a third origin to
        separate "what the peer has of mine" from "what the peer has".
        """
        a, b, c = three_nodes
        await write_note(c, "from-c", "c content")

        assert (await exchange(c, a))["success"] is True
        assert set(await notes_table(a)) == {"from-c"}

        first = await exchange(a, b)
        assert first["success"] is True, first
        assert first["sent"] == 1
        assert set(await notes_table(b)) == {"from-c"}

        second = await exchange(a, b)
        assert second["success"] is True, second
        assert second["sent"] == 0, "relay re-sent an op the peer already had"

    async def test_three_node_line_converges(self, three_nodes):
        """The full A <-> B <-> C topology, which is the shape the
        owner actually runs (laptop, desktop, phone, only some pairs
        ever in range of each other at once).

        A and C never speak. Everything either of them learns about the
        other has to cross B. Two rounds is what a line of three needs:
        the first moves each end's writes into B, the second moves them
        out to the far end.

        Stated plainly: this one PASSES against the pre-fix code, and it
        is kept as a regression guard rather than as evidence. Symmetric
        rounds over three fresh nodes do not order B's own write after
        the op it has to relay, which is the condition the watermark
        defect needs. ``test_relay_forwards_third_party_ops`` and
        ``test_three_node_delete_propagates_transitively`` construct
        that ordering deliberately and are the ones that fail.
        """
        a, b, c = three_nodes
        await write_note(a, "from-a", "a content")
        await write_note(b, "from-b", "b content")
        await write_note(c, "from-c", "c content")

        for _ in range(2):
            assert (await exchange(a, b))["success"] is True
            assert (await exchange(b, c))["success"] is True

        expected = {
            "from-a": "a content",
            "from-b": "b content",
            "from-c": "c content",
        }
        assert await notes_table(a) == expected
        assert await notes_table(b) == expected
        assert await notes_table(c) == expected

    async def test_three_node_delete_propagates_transitively(self, three_nodes):
        """The watermark defect applied to a delete, which is its worst
        face.

        A dropped insert looks like missing data and a user might
        notice. A dropped delete looks like the row is fine, so the
        note the user deleted on their laptop is still on their phone
        and nothing anywhere reports a problem.

        Same ordering trick as ``test_relay_forwards_third_party_ops``:
        B's own write lands AFTER A's delete, so under the old single
        cutoff the delete is older than C's mark for B and never
        crosses the relay.
        """
        a, b, c = three_nodes
        await write_note(a, "doomed", "delete me")
        for _ in range(2):
            await exchange(a, b)
            await exchange(b, c)
        assert set(await notes_table(c)) == {"doomed"}

        # Delete on A, recording the tombstone the way a real local
        # delete records it (``MemoryStore._log_sync_async``).
        hlc = await a.engine.log_operation_async(
            "notes", "delete", "doomed", {"id": "doomed"}
        )
        conn = await a.store._conn()
        try:
            await conn.execute("DELETE FROM notes WHERE id = 'doomed'")
            await conn.commit()
        finally:
            await a.store._release(conn)
        await a.store._record_tombstone("notes", "doomed", hlc)

        # B writes AFTER the delete, and gets that write to C first.
        await write_note(b, "b-marker", "b content")
        assert (await exchange(b, c))["success"] is True
        assert set(await notes_table(c)) == {"doomed", "b-marker"}

        assert (await exchange(a, b))["success"] is True
        assert set(await notes_table(b)) == {"b-marker"}

        assert (await exchange(b, c))["success"] is True
        assert await notes_table(c) == {"b-marker": "b content"}, (
            "the delete never crossed the relay: the row the user deleted "
            "is still live on the far node"
        )

        # A further round must not resurrect it out of anyone's WAL.
        assert (await exchange(b, c))["success"] is True
        assert (await exchange(a, b))["success"] is True
        assert "doomed" not in await notes_table(c)
        assert "doomed" not in await notes_table(a)

    async def test_static_peer_ops_are_not_echoed(self, two_nodes):
        """``_load_static_peers`` mints ``peer_id = f"static-{host}:{port}"``,
        which matches no ``origin_node``, so ``exclude_node=peer_id`` is a
        no-op and the peer's own ops are echoed straight back at it.

        The handshake response carries the peer's real ``node_id``. That,
        not the local dictionary key, is what the filter has to use.
        """
        a, b = two_nodes
        await write_note(b, "b-op", "b content")
        await exchange(a, b)

        static_id = f"static-127.0.0.1:{b.port}"
        a.engine._peers[static_id] = {
            "address": "127.0.0.1",
            "port": b.port,
            "discovered_at": time.time(),
            "source": "static",
        }
        with _serving_as(b):
            result = await a.engine.sync_with_peer(
                static_id, max_attempts=1, connect_timeout=10.0, handshake_timeout=30.0
            )

        assert result["success"] is True, result
        assert result["sent"] == 0, (
            "A echoed B's own operations back to B: exclude_node did not "
            "resolve to the peer's real node_id"
        )
