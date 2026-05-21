"""
AboutMe — structured self-model of the user, layered on top of IDENTITY / SOUL / MEMORY.

Where this fits in the memory hierarchy
---------------------------------------
IDENTITY.yaml    → the *agent's* personality, rules, communication style
SOUL.md          → deeper behavioural notes for the agent
USER.md          → free-form "about me" the user wrote by hand
MEMORY.md        → long-term pinned notes
AboutMeStore     → *structured*, queryable facts about the user, with provenance
                   and confidence, so the orchestrator can surface "why do you
                   know this?" on every fact and the user can confirm, reject,
                   or delete each one individually.

Fact kinds
----------
preference     - "I don't drink coffee after 4pm"         (diet, sleep)
relationship   - "My sister Amy lives in Boston"          (family)
place          - "Home is 123 X street"                   (location)
routine        - "Morning run Mon/Wed/Fri at 6:30am"      (fitness, schedule)
context        - "I work as a founder, currently in Y"    (work, identity)
goal           - "Launch Theora wristband in Q3"          (goal)
taboo          - "Never mention Z in suggestions"         (privacy)

Source provenance
-----------------
user_stated             - typed via /api/about-me POST or chat "remember that..."
inferred_from_chat      - regex-level extractor on episode summaries (confidence 0.5)
inferred_from_baseline  - proposed by BaselineEngine / IdeasEngine patterns
imported                - migrated from USER.md or an older knowledge-graph row

Confidence ladder
-----------------
0.5  - inferred, unconfirmed
0.75 - inferred but recurred multiple times
1.0  - user confirmed (or typed directly)

Expiry
------
`expires_at` is optional. Used for time-bounded facts such as "traveling to
SF this week" — the sweep() helper drops expired rows on schedule.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from typing import Iterable, Literal, Optional
from uuid import uuid4

logger = logging.getLogger("feral.about_me")

FactKind = Literal[
    "preference",
    "relationship",
    "place",
    "routine",
    "context",
    "goal",
    "taboo",
]

FactSource = Literal[
    "user_stated",
    "inferred_from_chat",
    "inferred_from_baseline",
    "imported",
]

FACT_KINDS: tuple[FactKind, ...] = (
    "preference",
    "relationship",
    "place",
    "routine",
    "context",
    "goal",
    "taboo",
)

FACT_SOURCES: tuple[FactSource, ...] = (
    "user_stated",
    "inferred_from_chat",
    "inferred_from_baseline",
    "imported",
)

CONFIDENCE_UNCONFIRMED = 0.5
CONFIDENCE_RECURRED = 0.75
CONFIDENCE_CONFIRMED = 1.0


@dataclass
class AboutMeFact:
    id: str
    kind: FactKind
    text: str
    tags: list[str] = field(default_factory=list)
    source: FactSource = "user_stated"
    confidence: float = CONFIDENCE_CONFIRMED
    created_at: float = 0.0
    updated_at: float = 0.0
    expires_at: Optional[float] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


class AboutMeStore:
    """SQLite-backed store of the user's self-model.

    Uses the same on-disk table shape as BaselineEngine so the install-smoke
    workflow can back it up with a single sqlite dump.
    """

    _DDL = """
    CREATE TABLE IF NOT EXISTS about_me_facts (
        id          TEXT PRIMARY KEY,
        kind        TEXT NOT NULL,
        text        TEXT NOT NULL,
        tags_json   TEXT NOT NULL DEFAULT '[]',
        source      TEXT NOT NULL DEFAULT 'user_stated',
        confidence  REAL NOT NULL DEFAULT 1.0,
        created_at  REAL NOT NULL,
        updated_at  REAL NOT NULL,
        expires_at  REAL
    );
    CREATE INDEX IF NOT EXISTS ix_about_me_kind ON about_me_facts(kind);
    CREATE INDEX IF NOT EXISTS ix_about_me_source ON about_me_facts(source);
    """

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            db_path = ":memory:"
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(self._DDL)

    @property
    def db_path(self) -> str:
        return self._db_path

    def upsert(
        self,
        *,
        kind: FactKind,
        text: str,
        tags: Iterable[str] | None = None,
        source: FactSource = "user_stated",
        confidence: float = CONFIDENCE_CONFIRMED,
        expires_at: float | None = None,
        fact_id: str | None = None,
    ) -> AboutMeFact:
        if kind not in FACT_KINDS:
            raise ValueError(
                f"Unknown about-me fact kind {kind!r}; must be one of {FACT_KINDS}"
            )
        if source not in FACT_SOURCES:
            raise ValueError(
                f"Unknown about-me source {source!r}; must be one of {FACT_SOURCES}"
            )
        text = (text or "").strip()
        if not text:
            raise ValueError("AboutMe fact text cannot be empty")
        confidence = max(0.0, min(1.0, float(confidence)))
        tags_list = sorted({t.strip().lower() for t in (tags or []) if t and t.strip()})

        now = time.time()
        existing = self._find_equivalent(kind, text)
        if existing is not None:
            existing_tags = set(existing.tags)
            existing_tags.update(tags_list)
            new_conf = max(existing.confidence, confidence)
            self._conn.execute(
                """UPDATE about_me_facts
                      SET tags_json = ?,
                          source    = ?,
                          confidence= ?,
                          updated_at= ?,
                          expires_at= ?
                    WHERE id = ?""",
                (
                    json.dumps(sorted(existing_tags)),
                    source,
                    new_conf,
                    now,
                    expires_at,
                    existing.id,
                ),
            )
            self._conn.commit()
            return self.get(existing.id)  # type: ignore[return-value]

        fid = fact_id or str(uuid4())
        self._conn.execute(
            """INSERT INTO about_me_facts
                (id, kind, text, tags_json, source, confidence,
                 created_at, updated_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fid,
                kind,
                text,
                json.dumps(tags_list),
                source,
                confidence,
                now,
                now,
                expires_at,
            ),
        )
        self._conn.commit()
        fact = self.get(fid)
        assert fact is not None
        return fact

    def get(self, fact_id: str) -> Optional[AboutMeFact]:
        row = self._conn.execute(
            "SELECT * FROM about_me_facts WHERE id = ?", (fact_id,)
        ).fetchone()
        return self._row_to_fact(row) if row else None

    def list(
        self,
        *,
        kind: FactKind | None = None,
        tag: str | None = None,
        include_expired: bool = False,
    ) -> list[AboutMeFact]:
        sql = "SELECT * FROM about_me_facts"
        params: list[object] = []
        where: list[str] = []
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if not include_expired:
            where.append("(expires_at IS NULL OR expires_at > ?)")
            params.append(time.time())
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC"
        rows = self._conn.execute(sql, params).fetchall()
        facts = [self._row_to_fact(r) for r in rows]
        if tag:
            t = tag.strip().lower()
            facts = [f for f in facts if t in f.tags]
        return facts

    def delete(self, fact_id: str) -> bool:
        cur = self._conn.execute(
            "DELETE FROM about_me_facts WHERE id = ?", (fact_id,)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def confirm(self, fact_id: str) -> Optional[AboutMeFact]:
        """User confirms an inferred fact; bump confidence to 1.0."""
        fact = self.get(fact_id)
        if fact is None:
            return None
        self._conn.execute(
            """UPDATE about_me_facts
                  SET confidence = ?, source = ?, updated_at = ?
                WHERE id = ?""",
            (CONFIDENCE_CONFIRMED, "user_stated", time.time(), fact_id),
        )
        self._conn.commit()
        return self.get(fact_id)

    def reject(self, fact_id: str) -> Optional[AboutMeFact]:
        """User rejects an inferred fact.

        Converts the original fact into a kind='taboo' row with its text
        negated ("Never assume: <original>") so the orchestrator stops
        proposing it again. The id is preserved for traceability.
        """
        fact = self.get(fact_id)
        if fact is None:
            return None
        negated_text = f"Never assume: {fact.text}"
        now = time.time()
        tags = sorted(set(fact.tags) | {"privacy", "user_rejected"})
        self._conn.execute(
            """UPDATE about_me_facts
                  SET kind = ?, text = ?, tags_json = ?,
                      source = ?, confidence = ?, updated_at = ?,
                      expires_at = NULL
                WHERE id = ?""",
            (
                "taboo",
                negated_text,
                json.dumps(tags),
                "user_stated",
                CONFIDENCE_CONFIRMED,
                now,
                fact_id,
            ),
        )
        self._conn.commit()
        return self.get(fact_id)

    def sweep_expired(self, now: float | None = None) -> int:
        cur = self._conn.execute(
            "DELETE FROM about_me_facts WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (now if now is not None else time.time(),),
        )
        self._conn.commit()
        return cur.rowcount

    def summary(self) -> dict:
        rows = self._conn.execute(
            """SELECT kind, COUNT(*) AS c FROM about_me_facts
                WHERE expires_at IS NULL OR expires_at > ?
                GROUP BY kind""",
            (time.time(),),
        ).fetchall()
        per_kind = {r["kind"]: r["c"] for r in rows}
        total = sum(per_kind.values())
        return {
            "total_facts": total,
            "per_kind": per_kind,
            "kinds_supported": list(FACT_KINDS),
        }

    def system_prompt_chunk(self, *, token_budget_chars: int = 2400) -> str:
        """Build a short natural-language block for the system prompt.

        The chunk is capped at *token_budget_chars* (≈600 tokens) and orders
        facts by kind so the LLM sees identity/context first, then concrete
        preferences, then taboos. Returns an empty string when there are no
        facts so the system prompt isn't padded with "About you" noise.
        """
        if token_budget_chars <= 0:
            return ""

        facts = self.list()
        if not facts:
            return ""

        kind_order = ("context", "goal", "routine", "place", "relationship", "preference", "taboo")
        grouped: dict[str, list[AboutMeFact]] = {k: [] for k in kind_order}
        for f in facts:
            grouped.setdefault(f.kind, []).append(f)

        lines: list[str] = ["## About the user (structured self-model)"]
        for kind in kind_order:
            rows = grouped.get(kind) or []
            if not rows:
                continue
            label = {
                "context": "Context",
                "goal": "Goals",
                "routine": "Routines",
                "place": "Places",
                "relationship": "Relationships",
                "preference": "Preferences",
                "taboo": "Taboos (do not suggest)",
            }[kind]
            lines.append(f"### {label}")
            for f in rows:
                conf = "confirmed" if f.confidence >= CONFIDENCE_CONFIRMED else f"~{int(f.confidence * 100)}%"
                src = "user" if f.source == "user_stated" else f.source.replace("_", " ")
                lines.append(f"- {f.text} (source: {src}, {conf})")
        chunk = "\n".join(lines)
        if len(chunk) > token_budget_chars:
            chunk = chunk[: token_budget_chars - 3].rstrip() + "..."
        return chunk

    # ------------------------------------------------------------------
    # Chat extractor — regex-level auto-suggested facts
    # ------------------------------------------------------------------

    _EXTRACTOR_PATTERNS: tuple[tuple[str, FactKind, tuple[str, ...]], ...] = (
        # "I prefer X" / "I like X" / "I love X"
        (r"\bI (?:prefer|like|love|enjoy) (.{4,120})", "preference", ()),
        # "I don't/never X" - negative preference
        (r"\bI (?:don't|do not|never) (.{4,120})", "preference", ("negative",)),
        # "My <relation> <name>" — capture with name
        (r"\bMy (wife|husband|partner|sister|brother|mom|dad|mother|father|son|daughter|kid|child) (?:is )?([A-Z][a-zA-Z]{1,20})", "relationship", ("family",)),
        # "I live in X" / "I'm based in X"
        (r"\bI (?:live|am based|'m based) in ([A-Z][\w\s,.-]{2,80})", "place", ("location",)),
        # "I work as X" / "I'm a X"
        (r"\bI (?:work as|'m a|am a) (.{3,60})", "context", ("work",)),
        # "I usually X at <time>" - routine
        (r"\bI (?:usually|typically|normally) (.{4,120})", "routine", ()),
        # "My goal is to X" / "I want to X" / "I'm trying to X"
        (r"\b(?:My goal is to|I want to|I'm trying to|I am trying to) (.{4,120})", "goal", ()),
        # "Don't mention X" / "Never mention X" — taboo
        (r"\b(?:Don't|Never|Do not) mention (.{3,80})", "taboo", ("privacy",)),
    )

    def extract_from_text(
        self,
        text: str,
        *,
        confidence: float = CONFIDENCE_UNCONFIRMED,
        source: FactSource = "inferred_from_chat",
    ) -> list[AboutMeFact]:
        """Scan a chunk of text for self-referential patterns.

        Returns the list of facts that were stored (new + promoted). This is
        intentionally conservative — we prefer missed extractions over noisy
        false positives, since every extracted fact shows up in Settings →
        Self → About Me for the user to confirm or reject.
        """
        import re

        if not text or not text.strip():
            return []

        saved: list[AboutMeFact] = []
        for pat, kind, extra_tags in self._EXTRACTOR_PATTERNS:
            for match in re.finditer(pat, text):
                # Take the last non-empty group as the payload; patterns with
                # multiple groups put the name/payload last. Trim trailing
                # punctuation + whitespace so "coffee." → "coffee".
                payload = next(
                    (g for g in reversed(match.groups()) if g),
                    "",
                ).strip(" \t\n\r.,;:!?")
                if not payload:
                    continue

                # Rebuild the canonical fact text so UI reads cleanly:
                templates = {
                    "preference": lambda p: f"Prefers: {p}",
                    "relationship": lambda p: f"Family: {p}",
                    "place": lambda p: f"Lives in {p}",
                    "context": lambda p: f"Works as {p}",
                    "routine": lambda p: f"Routine: {p}",
                    "goal": lambda p: f"Goal: {p}",
                    "taboo": lambda p: f"Do not mention: {p}",
                }
                fact_text = templates[kind](payload)

                tags = list(extra_tags)
                try:
                    fact = self.upsert(
                        kind=kind,
                        text=fact_text,
                        tags=tags,
                        source=source,
                        confidence=confidence,
                    )
                    saved.append(fact)
                except ValueError as e:
                    logger.debug("About-me extractor skipped match: %s", e)
        return saved

    async def extract_from_text_async(
        self,
        text: str,
        *,
        confidence: float = CONFIDENCE_UNCONFIRMED,
        source: FactSource = "inferred_from_chat",
    ) -> list[AboutMeFact]:
        """Async-offloaded variant of :meth:`extract_from_text`.

        Regex matching plus N sqlite3 ``INSERT`` round-trips per
        match are run on a worker thread so the chat / voice hot
        paths can ``await`` extraction without blocking the event
        loop. AUDIT-r14 finding 14 traced sync ``extract_from_text``
        calls in ``MemoryStore.episode_save`` as a measurable
        slow-callback on every chat turn.
        """
        return await asyncio.to_thread(
            self.extract_from_text,
            text,
            confidence=confidence,
            source=source,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _find_equivalent(self, kind: FactKind, text: str) -> Optional[AboutMeFact]:
        row = self._conn.execute(
            "SELECT * FROM about_me_facts WHERE kind = ? AND LOWER(text) = LOWER(?)",
            (kind, text),
        ).fetchone()
        return self._row_to_fact(row) if row else None

    @staticmethod
    def _row_to_fact(row: sqlite3.Row) -> AboutMeFact:
        return AboutMeFact(
            id=row["id"],
            kind=row["kind"],
            text=row["text"],
            tags=json.loads(row["tags_json"]) if row["tags_json"] else [],
            source=row["source"],
            confidence=row["confidence"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expires_at=row["expires_at"],
        )
