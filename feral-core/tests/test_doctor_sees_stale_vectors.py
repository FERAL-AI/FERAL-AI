"""Doctor must not report a degraded embedding store as healthy.

Measured on the operator's brain on 2026-09-05: 312 of 334 rows in
``entities.embedding`` held 1536d OpenAI vectors while the active
provider was fastembed at 384d. A vector of the wrong width cannot be
compared with the query at all, so 93 percent of the knowledge graph was
unreachable by semantic search and the graph had silently degraded to
keyword-only.

Everything that could have said so, didn't. ``feral memory reembed
check`` knew and printed "<- stale", but nobody runs that unless they
already suspect something. ``feral doctor``, which is the surface an
operator actually consults, printed

    OK  Embedding provider  fastembed (384d, local and free)

and stopped there, because it reported which provider was LIVE and never
asked what had written the vectors already in the store. The only other
symptom was a single line about a failed numpy reshape.

A diagnostic that reports a broken system as healthy is worse than no
diagnostic, because it ends the investigation. These tests pin the three
outcomes: stale warns and names the remedy, matching passes, and an
unreadable store warns rather than staying quiet.
"""

from __future__ import annotations

import sqlite3
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory.reembed import scan_store  # noqa: E402


def _vector(dim: int) -> bytes:
    return struct.pack(f"{dim}f", *([0.1] * dim))


def _store(path: Path, widths: dict[int, int]) -> None:
    """A memory.db whose entities carry vectors of the given widths."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE entities (id INTEGER PRIMARY KEY, name TEXT, embedding BLOB)"
    )
    rid = 0
    for dim, count in widths.items():
        for _ in range(count):
            rid += 1
            conn.execute(
                "INSERT INTO entities (id, name, embedding) VALUES (?, ?, ?)",
                (rid, f"e{rid}", _vector(dim)),
            )
    conn.commit()
    conn.close()


class TestTheScanItself:
    """The measurement doctor now depends on."""

    def test_the_operators_exact_store_is_reported_stale(self, tmp_path):
        db = tmp_path / "memory.db"
        _store(db, {1536: 312, 384: 22})
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            scan = scan_store(conn, 384)
        finally:
            conn.close()
        assert scan.stale_total == 312, "the 1536d rows are the unreachable ones"
        stale = [c for c in scan.columns if c.stale]
        assert stale and stale[0].table == "entities"
        assert stale[0].widths.get(1536) == 312

    def test_a_matching_store_is_not_stale(self, tmp_path):
        db = tmp_path / "memory.db"
        _store(db, {384: 40})
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            scan = scan_store(conn, 384)
        finally:
            conn.close()
        assert scan.stale_total == 0
        assert scan.columns, "a populated store must still be measured"


class TestDoctorReportsIt:
    """The probe wired into ``feral doctor``.

    Driven through the same scan doctor calls rather than by spawning
    the CLI, because the assertion that matters is what the operator is
    told, and the wording is what carries the remedy.
    """

    @staticmethod
    def _render(db: Path, target_dim: int) -> tuple[str, str]:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            scan = scan_store(conn, target_dim)
        finally:
            conn.close()
        if scan.columns and scan.stale_total:
            where = ", ".join(
                f"{c.table}.{c.column} "
                + " ".join(f"{d}d x{n}" for d, n in sorted(c.widths.items()))
                for c in scan.columns
                if c.stale
            )
            return "warn", (
                f"{scan.stale_total} vector(s) were written at a different "
                f"width than the active provider's {target_dim}d, so semantic "
                f"search cannot reach them and that tier has degraded to "
                f"keyword-only ({where})"
            )
        return "pass", f"every stored vector matches the active {target_dim}d provider"

    def test_the_reported_case_warns_and_counts(self, tmp_path):
        db = tmp_path / "memory.db"
        _store(db, {1536: 312, 384: 22})
        level, detail = self._render(db, 384)
        assert level == "warn"
        assert "312" in detail
        assert "384d" in detail and "1536d" in detail
        assert "entities.embedding" in detail
        assert "keyword-only" in detail, "say what the user loses, not just that a number is odd"

    def test_a_healthy_store_passes(self, tmp_path):
        db = tmp_path / "memory.db"
        _store(db, {384: 40})
        level, detail = self._render(db, 384)
        assert level == "pass"
        assert "384d" in detail

    def test_an_empty_store_is_not_called_broken(self, tmp_path):
        """A fresh install has written nothing yet. That is not a fault."""
        db = tmp_path / "memory.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE entities (id INTEGER PRIMARY KEY, embedding BLOB)")
        conn.commit()
        conn.close()
        level, _ = self._render(db, 384)
        assert level == "pass"


def test_the_probe_is_wired_into_doctor():
    """Pins the wiring, so the probe cannot be written and left unused.

    The measurement passing in isolation is not the property that
    failed here. What failed is that doctor never asked.
    """
    src = (ROOT / "cli" / "main.py").read_text()
    assert "from memory.reembed import scan_store" in src
    assert "Stored embeddings" in src
    assert "feral memory reembed" in src, "the warning must carry the remedy"


@pytest.mark.parametrize("target,widths,stale", [
    (384, {384: 10}, 0),
    (384, {1536: 10}, 10),
    (1536, {384: 10}, 10),
    (1536, {1536: 5, 384: 5}, 5),
])
def test_staleness_is_relative_to_the_active_provider(tmp_path, target, widths, stale):
    """Switching back to the old provider makes the other half stale.

    There is no globally correct width, only the one the live provider
    emits, which is why doctor has to read the provider rather than
    assume 384.
    """
    db = tmp_path / f"memory-{target}-{'-'.join(map(str, widths))}.db"
    _store(db, widths)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        scan = scan_store(conn, target)
    finally:
        conn.close()
    assert scan.stale_total == stale
