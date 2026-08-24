"""Which About-Me facts came from speech the operator merely overheard.

Until 2026-08-23 the About-Me extractor ran on every episode, including
`ambient_conversation` ones. Every one of its patterns is first person
("I prefer X", "My wife <Name>", "I live in X"), so on a recorded
conversation the "I" was whoever happened to be talking. Other people's
preferences, families and home towns were filed under the OPERATOR at
0.5 confidence, and the ideas engine then asked them to confirm it.

The fix stops new pollution. It cannot un-write what is already stored,
and the stored rows do not say where they came from: `source` is
`inferred_from_chat` for chat episodes and ambient ones alike, because
the extractor was called the same way for both.

So provenance is reconstructed by correlation. A fact is attributed to
an ambient transcript when it was created within `WINDOW_S` of that
transcript being processed, which is exact in practice because the
extractor runs inline at the end of processing: on the profile this was
written against, the polluted fact and its transcript shared a
timestamp to the second.

READ ONLY by default. It prints what it found and exits. Deleting
somebody's profile rows is not something a report should do on its own,
so `--delete` exists, requires the explicit flag, and takes a backup
first.

    python scripts_audit/about_me_provenance.py
    python scripts_audit/about_me_provenance.py --delete
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path


#: How close in time a fact must be to a transcript's processing to be
#: attributed to it. The extractor runs inline at the tail of
#: `_process_ambient_transcript`, so the real gap is sub-second; this is
#: generous enough to survive a slow write and tight enough that an
#: unrelated chat turn a minute later is not swept up.
WINDOW_S = 20.0


def feral_home() -> Path:
    import os

    return Path(os.environ.get("FERAL_HOME") or (Path.home() / ".feral"))


def load_ambient_windows(home: Path) -> list[tuple[str, float, str]]:
    """(transcript_id, processed_at, first words) for processed rows."""
    db = home / "ambient_transcripts.db"
    if not db.exists():
        return []
    out = []
    with sqlite3.connect(str(db)) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT transcript_id, processed_at, payload_json "
                "FROM ambient_transcripts WHERE processed_at IS NOT NULL"
            ).fetchall()
        except sqlite3.Error:
            return []
    for row in rows:
        text = ""
        try:
            text = (json.loads(row["payload_json"]) or {}).get("text", "")
        except Exception:
            text = ""
        out.append((row["transcript_id"], float(row["processed_at"]), text[:90]))
    return out


def load_facts(home: Path) -> list[dict]:
    db = home / "about_me.db"
    if not db.exists():
        return []
    with sqlite3.connect(str(db)) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, kind, text, source, confidence, created_at "
                "FROM about_me_facts ORDER BY created_at"
            ).fetchall()
        except sqlite3.Error:
            return []
    return [dict(r) for r in rows]


def attribute(facts: list[dict], windows: list[tuple[str, float, str]]) -> list[dict]:
    suspect = []
    for fact in facts:
        # A fact the operator stated themselves is theirs by
        # definition, whatever else was happening at the time.
        if fact["source"] == "user_stated":
            continue
        created = float(fact["created_at"])
        for transcript_id, processed_at, excerpt in windows:
            if abs(created - processed_at) <= WINDOW_S:
                suspect.append({
                    **fact,
                    "transcript_id": transcript_id,
                    "gap_s": round(created - processed_at, 2),
                    "excerpt": excerpt,
                })
                break
    return suspect


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Report About-Me facts that were extracted from overheard "
            "speech rather than from the operator. Read-only unless "
            "--delete is passed."
        ),
    )
    parser.add_argument(
        "--delete", action="store_true",
        help="Remove the attributed facts. Takes a timestamped backup first.",
    )
    args = parser.parse_args()

    home = feral_home()
    facts = load_facts(home)
    windows = load_ambient_windows(home)

    if not facts:
        print(f"No About-Me facts stored under {home}.")
        return 0

    suspect = attribute(facts, windows)

    print(f"about_me.db      : {home / 'about_me.db'}")
    print(f"facts stored     : {len(facts)}")
    print(f"ambient sessions : {len(windows)} processed transcripts")
    print(f"attributable to overheard speech: {len(suspect)}")

    if not suspect:
        print("\nNothing to clean. Every inferred fact predates or falls "
              "outside an ambient transcript window.")
        return 0

    print()
    for fact in suspect:
        print(f"  [{fact['kind']}] {fact['text']}")
        print(f"      confidence {fact['confidence']}  created "
              f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(fact['created_at']))}")
        print(f"      from transcript {fact['transcript_id']} "
              f"({fact['gap_s']:+}s from processing)")
        print(f"      conversation began: {fact['excerpt']!r}")
        print()

    if not args.delete:
        print("Read-only. Re-run with --delete to remove these "
              "(a backup is taken first).")
        return 0

    db = home / "about_me.db"
    backup = db.with_suffix(f".db.bak.{int(time.time())}")
    shutil.copy2(db, backup)
    print(f"backup written: {backup}")

    with sqlite3.connect(str(db)) as conn:
        conn.executemany(
            "DELETE FROM about_me_facts WHERE id = ?",
            [(f["id"],) for f in suspect],
        )
        conn.commit()
    print(f"deleted {len(suspect)} fact(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
