"""The provenance tool must not delete anything unless asked.

`scripts_audit/about_me_provenance.py` reconstructs which About-Me
facts were extracted from speech the operator merely overheard, so they
can be removed from a profile that was polluted before the fix landed.

It can delete rows from a real person's profile, so the default has to
be read-only and the deletion path has to take a backup. That is what
these tests pin. A reporting tool that quietly mutates is the worst
shape this could take.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


FERAL_CORE = Path(__file__).resolve().parents[1]
TOOL = FERAL_CORE / "scripts_audit" / "about_me_provenance.py"


def _seed(home: Path, *, fact_at: float, transcript_at: float) -> None:
    """A profile with one operator-stated fact and one inferred one,
    plus an ambient transcript processed at ``transcript_at``."""
    home.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(str(home / "about_me.db")) as conn:
        conn.execute("""
            CREATE TABLE about_me_facts (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, text TEXT NOT NULL,
                tags_json TEXT NOT NULL DEFAULT '[]',
                source TEXT NOT NULL DEFAULT 'user_stated',
                confidence REAL NOT NULL DEFAULT 1.0,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                expires_at REAL
            )
        """)
        conn.executemany(
            "INSERT INTO about_me_facts "
            "(id, kind, text, source, confidence, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                ("mine", "goal", "Goal: ship the demo", "user_stated",
                 1.0, fact_at, fact_at),
                ("theirs", "preference", "Prefers: tea", "inferred_from_chat",
                 0.5, fact_at, fact_at),
            ],
        )
        conn.commit()

    with sqlite3.connect(str(home / "ambient_transcripts.db")) as conn:
        conn.execute("""
            CREATE TABLE ambient_transcripts (
                transcript_id TEXT PRIMARY KEY, received_at REAL NOT NULL,
                node_id TEXT NOT NULL, device_id TEXT NOT NULL,
                session_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                processed_at REAL, episode_id TEXT
            )
        """)
        conn.execute(
            "INSERT INTO ambient_transcripts VALUES (?,?,?,?,?,?,?,?)",
            ("t1", transcript_at, "n", "d", "s",
             json.dumps({"text": "two people talking about tea"}),
             transcript_at, "ep1"),
        )
        conn.commit()


def _run(home: Path, *args: str) -> subprocess.CompletedProcess:
    import os

    env = dict(os.environ)
    env["FERAL_HOME"] = str(home)
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        cwd=str(FERAL_CORE), env=env, capture_output=True, text=True,
        timeout=120,
    )


def _fact_ids(home: Path) -> set[str]:
    with sqlite3.connect(str(home / "about_me.db")) as conn:
        return {r[0] for r in conn.execute("SELECT id FROM about_me_facts")}


def test_the_tool_exists():
    assert TOOL.is_file()


def test_it_reports_without_deleting(tmp_path):
    """The default must be read-only. This is somebody's profile."""
    home = tmp_path / "home"
    now = time.time()
    _seed(home, fact_at=now, transcript_at=now)

    result = _run(home)
    assert result.returncode == 0, result.stderr
    assert "attributable to overheard speech: 1" in result.stdout
    assert _fact_ids(home) == {"mine", "theirs"}, "read-only run deleted rows"


def test_an_operator_stated_fact_is_never_attributed(tmp_path):
    """`user_stated` means they said it themselves, whatever else was
    being recorded at the time."""
    home = tmp_path / "home"
    now = time.time()
    _seed(home, fact_at=now, transcript_at=now)

    result = _run(home)
    assert "Goal: ship the demo" not in result.stdout


def test_a_fact_far_from_any_transcript_is_left_alone(tmp_path):
    """Correlation is the only evidence available, so the window has to
    actually bound it."""
    home = tmp_path / "home"
    now = time.time()
    _seed(home, fact_at=now, transcript_at=now - 3600)

    result = _run(home)
    assert "attributable to overheard speech: 0" in result.stdout


def test_delete_removes_only_the_attributed_rows_and_backs_up(tmp_path):
    home = tmp_path / "home"
    now = time.time()
    _seed(home, fact_at=now, transcript_at=now)

    result = _run(home, "--delete")
    assert result.returncode == 0, result.stderr
    assert _fact_ids(home) == {"mine"}, "deleted the wrong rows"

    backups = list(home.glob("about_me.db.bak.*"))
    assert backups, "deletion must take a backup first"
    with sqlite3.connect(str(backups[0])) as conn:
        restored = {r[0] for r in conn.execute("SELECT id FROM about_me_facts")}
    assert restored == {"mine", "theirs"}, "the backup is not the pre-delete state"


def test_a_profile_with_no_facts_is_not_an_error(tmp_path):
    home = tmp_path / "empty"
    home.mkdir()
    result = _run(home)
    assert result.returncode == 0
    assert "No About-Me facts" in result.stdout
