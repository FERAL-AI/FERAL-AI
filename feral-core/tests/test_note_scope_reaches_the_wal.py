"""A named scope must survive the whole write path into the sync WAL.

``save_note`` has accepted a ``scope`` since scoped replication landed,
and ``SyncEngine`` has enforced grants against it the whole time. But
every note in the product is written through ``MemoryStore.save``, and
that wrapper did not forward the argument. So the producer half of
federation was never wired: measured on the operator's brain on
2026-09-07, all 8,898 rows in ``sync_wal`` carried scope ``private``,
and granting a peer a scope replicated nothing at all.

That is the failure this file exists to stop, and it is a quiet one.
Nothing errors, no test goes red, the CLI cheerfully reports the grant,
and the operator concludes the feature works. These tests assert on the
WAL row itself rather than on the return value of the call, because the
call succeeded the whole time it was broken.

The default stays private in every case. A note leaves this machine
only if the caller names a scope AND a peer holds a grant for it.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from memory.store import MemoryStore
from memory.sync import SyncEngine


@pytest.fixture
async def store(tmp_path):
    st = MemoryStore(db_path=str(tmp_path / "mem.db"))
    eng = SyncEngine(
        node_id="node-local",
        memory_store=st,
        db_path=str(tmp_path / "wal.db"),
    )
    st.set_sync_engine(eng)
    st._wal_path = str(tmp_path / "wal.db")
    return st


def _wal_scopes(store) -> list[tuple[str, str]]:
    """(row_id, scope) for every note op the WAL holds."""
    conn = sqlite3.connect(f"file:{store._wal_path}?mode=ro", uri=True)
    try:
        return [
            (r[0], r[1])
            for r in conn.execute(
                "SELECT row_id, scope FROM sync_wal WHERE table_name = 'notes' "
                "ORDER BY rowid"
            )
        ]
    finally:
        conn.close()


class TestTheChokepoint:
    """``MemoryStore.save`` is the one place every note passes through."""

    async def test_a_named_scope_reaches_the_wal(self, store):
        await store.save("shared with the team", tags=["t"], scope="work")
        rows = _wal_scopes(store)
        assert rows, "the note produced no WAL operation at all"
        assert rows[-1][1] == "work", (
            f"scope was dropped between MemoryStore.save and the WAL: {rows[-1]}"
        )

    async def test_omitting_the_scope_stays_private(self, store):
        await store.save("personal", tags=[])
        assert _wal_scopes(store)[-1][1] == "private", (
            "a note written without a scope must never become shareable"
        )

    async def test_an_explicit_none_stays_private(self, store):
        await store.save("personal", tags=[], scope=None)
        assert _wal_scopes(store)[-1][1] == "private"

    async def test_private_is_not_launderable_into_a_grant(self, store):
        """Naming the reserved scope must not make a row replicable."""
        await store.save("still personal", tags=[], scope="private")
        assert _wal_scopes(store)[-1][1] == "private"

    async def test_two_notes_keep_their_own_scopes(self, store):
        """The demo case: one note crosses, the next one does not."""
        await store.save("goes to the peer", tags=[], scope="work")
        await store.save("stays home", tags=[])
        scopes = [s for _, s in _wal_scopes(store)]
        assert scopes[-2:] == ["work", "private"]


class TestTheSurfacesAboveIt:
    """The wrapper is useless if the callers above it drop the argument."""

    def test_the_http_route_forwards_scope(self):
        import inspect
        from api.routes import memory as memory_routes

        src = inspect.getsource(memory_routes.memory_save)
        assert 'body.get("scope")' in src, "the save route ignores scope"
        assert "scope=scope" in src, "the save route reads scope but drops it"

    def test_the_skill_forwards_scope(self):
        import inspect
        from skills.impl.notes_memory import NotesMemorySkill

        src = inspect.getsource(NotesMemorySkill._save_note)
        assert 'args.get("scope")' in src, "the notes skill ignores scope"
        assert "scope=" in src

    def test_the_manifest_declares_scope(self):
        """Undeclared, the model can never choose to share anything."""
        from pathlib import Path

        manifest = json.loads(
            (Path(__file__).resolve().parent.parent
             / "skills" / "manifests" / "notes.json").read_text()
        )
        save = next(e for e in manifest["endpoints"] if e["id"] == "save_note")
        param = next((p for p in save["params"] if p["name"] == "scope"), None)
        assert param is not None, "save_note does not declare a scope parameter"
        assert param.get("required") is False, "sharing must never be mandatory"
        text = param["description"].lower()
        assert "private" in text, "the description must state the default"
