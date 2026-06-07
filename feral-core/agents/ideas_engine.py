"""IdeasEngine — deterministic suggestion generator, LLM-optional.

Where it fits
-------------
ProactiveEngine  → fires *alerts* (HR elevated, meeting in 10 min, etc.).
                   These are event-style nudges that interrupt.
IdeasEngine      → fires *ideas* — "Hey, you mentioned tea yesterday, save
                   that to About Me?" — that render on a Home pane the
                   user can pull when they're ready. Low-interruption.

Triggers
--------
1. Daily "morning brief" tick at 07:30 local.
2. Every BaselineEngine alert that fires.
3. Every ConsciousnessStore entity in ``waiting_user`` status.

The engine never calls an LLM unless ``settings.ideas_llm_polish`` is
enabled; by default it composes suggestions from deterministic templates
keyed on the signal bundle (``metric_id``, ``deviation_sigma``, paused
consciousness kinds, calendar gaps, latest-inferred AboutMe fact).
Everything runs without network, so every ``pip install feral-ai``
works out of the box on Day 1.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Literal, Optional

logger = logging.getLogger("feral.ideas_engine")


IdeaKind = Literal[
    "morning",   # "Here's what's on deck today"
    "health",    # HRV / sleep / activity anomaly
    "work",      # paused consciousness / intent / TaskFlow
    "about",     # confirm an inferred AboutMe fact
    "focus",     # you haven't finished X in 3 days
]

IdeaSeverity = Literal["info", "warning", "critical"]


@dataclass
class IdeaAction:
    """Optional deep-link the UI can fire on accept."""
    kind: str                # e.g. "route" | "install_routine" | "confirm_about_me_fact" | "resume_consciousness"
    route: str = ""          # v2 UI route if kind="route"
    verb: str = ""           # free-form action hint, e.g. "install"
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Idea:
    id: str
    kind: IdeaKind
    text: str
    source_signals: list[str] = field(default_factory=list)
    action: Optional[IdeaAction] = None
    severity: IdeaSeverity = "info"
    created_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.action is None:
            d.pop("action", None)
        return d


class IdeasStore:
    """SQLite persistence for generated + user-interacted ideas.

    Separate from BaselineEngine / AboutMe so we can TTL-expire stale
    ideas independently and track per-user dismiss weighting.
    """

    _DDL = """
    CREATE TABLE IF NOT EXISTS ideas (
        id              TEXT PRIMARY KEY,
        kind            TEXT NOT NULL,
        text            TEXT NOT NULL,
        source_signals  TEXT NOT NULL DEFAULT '[]',
        action_json     TEXT,
        severity        TEXT NOT NULL DEFAULT 'info',
        created_at      REAL NOT NULL,
        expires_at      REAL,
        accepted_at     REAL,
        dismissed_at    REAL
    );
    CREATE INDEX IF NOT EXISTS ix_ideas_created ON ideas(created_at DESC);
    CREATE TABLE IF NOT EXISTS idea_dismiss_weights (
        signal_key      TEXT PRIMARY KEY,
        dismiss_count   INTEGER NOT NULL DEFAULT 0,
        updated_at      REAL NOT NULL
    );
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

    def insert(self, idea: Idea) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO ideas
                (id, kind, text, source_signals, action_json, severity,
                 created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                idea.id,
                idea.kind,
                idea.text,
                json.dumps(idea.source_signals),
                json.dumps(idea.action.to_dict()) if idea.action else None,
                idea.severity,
                idea.created_at,
                idea.expires_at,
            ),
        )
        self._conn.commit()

    def list_today(self, *, limit: int = 20, now: float | None = None) -> list[Idea]:
        """Ideas created since 04:00 local today (so the morning card is stable)."""
        _now = now if now is not None else time.time()
        dt = datetime.fromtimestamp(_now).astimezone()
        day_start = dt.replace(hour=4, minute=0, second=0, microsecond=0)
        if dt.hour < 4:
            from datetime import timedelta
            day_start = day_start.replace(day=dt.day) - timedelta(days=1)
        cutoff = day_start.timestamp()
        rows = self._conn.execute(
            """SELECT * FROM ideas
                WHERE created_at >= ?
                  AND accepted_at IS NULL
                  AND dismissed_at IS NULL
                  AND (expires_at IS NULL OR expires_at > ?)
                ORDER BY created_at DESC LIMIT ?""",
            (cutoff, _now, limit),
        ).fetchall()
        return [self._row_to_idea(r) for r in rows]

    def record_accept(self, idea_id: str) -> Optional[Idea]:
        row = self._conn.execute(
            "SELECT * FROM ideas WHERE id = ?", (idea_id,)
        ).fetchone()
        if not row:
            return None
        self._conn.execute(
            "UPDATE ideas SET accepted_at = ? WHERE id = ?",
            (time.time(), idea_id),
        )
        self._conn.commit()
        return self._row_to_idea(
            self._conn.execute("SELECT * FROM ideas WHERE id = ?", (idea_id,)).fetchone()
        )

    def record_dismiss(self, idea_id: str) -> Optional[Idea]:
        row = self._conn.execute(
            "SELECT * FROM ideas WHERE id = ?", (idea_id,)
        ).fetchone()
        if not row:
            return None
        now = time.time()
        self._conn.execute(
            "UPDATE ideas SET dismissed_at = ? WHERE id = ?", (now, idea_id)
        )
        signals: list[str] = json.loads(row["source_signals"] or "[]")
        for sig in signals:
            self._conn.execute(
                """INSERT INTO idea_dismiss_weights (signal_key, dismiss_count, updated_at)
                   VALUES (?, 1, ?)
                   ON CONFLICT(signal_key) DO UPDATE SET
                     dismiss_count = dismiss_count + 1,
                     updated_at = excluded.updated_at""",
                (sig, now),
            )
        self._conn.commit()
        return self._row_to_idea(
            self._conn.execute("SELECT * FROM ideas WHERE id = ?", (idea_id,)).fetchone()
        )

    def has_active_signal(self, signal_key: str, now: float | None = None) -> bool:
        """True if a live (un-accepted, un-dismissed, un-expired) idea
        already exists for *signal_key*.

        Dedup guard (operator report 2026-06-07): a sustained baseline
        anomaly fanned out one idea per proactive tick, stacking ~20
        byte-identical "resting HR 51 / baseline 100" cards on the Home
        pane. Each idea has a unique uuid so INSERT OR REPLACE never
        collapsed them; we instead refuse to compose a second card while
        the first is still showing. Matches on the JSON-encoded signal
        token so we don't depend on per-row JSON parsing.
        """
        _now = now if now is not None else time.time()
        row = self._conn.execute(
            """SELECT 1 FROM ideas
                WHERE accepted_at IS NULL
                  AND dismissed_at IS NULL
                  AND (expires_at IS NULL OR expires_at > ?)
                  AND source_signals LIKE ?
                LIMIT 1""",
            (_now, f'%"{signal_key}"%'),
        ).fetchone()
        return row is not None

    def dismiss_weight(self, signal_key: str) -> int:
        row = self._conn.execute(
            "SELECT dismiss_count FROM idea_dismiss_weights WHERE signal_key = ?",
            (signal_key,),
        ).fetchone()
        return int(row["dismiss_count"]) if row else 0

    def sweep_expired(self, now: float | None = None) -> int:
        cur = self._conn.execute(
            "DELETE FROM ideas WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (now if now is not None else time.time(),),
        )
        self._conn.commit()
        return cur.rowcount

    @staticmethod
    def _row_to_idea(row: sqlite3.Row | None) -> Idea:
        assert row is not None
        action: Optional[IdeaAction] = None
        raw = row["action_json"]
        if raw:
            try:
                data = json.loads(raw)
                action = IdeaAction(**data)
            except Exception:
                action = None
        return Idea(
            id=row["id"],
            kind=row["kind"],
            text=row["text"],
            source_signals=json.loads(row["source_signals"] or "[]"),
            action=action,
            severity=row["severity"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )


# ----------------------------------------------------------------------
# Template composition
# ----------------------------------------------------------------------


def _signal_key(prefix: str, *parts: str) -> str:
    return ":".join([prefix, *parts])


def _one_day_ttl(now: float | None = None) -> float:
    return (now or time.time()) + 24 * 3600


def compose_morning_brief(
    *,
    pending_consciousness_count: int = 0,
    inferred_about_me_count: int = 0,
    now: float | None = None,
) -> Optional[Idea]:
    """Single morning-brief idea, stable across today unless dismissed."""
    pieces: list[str] = []
    if pending_consciousness_count:
        pieces.append(
            f"{pending_consciousness_count} paused item{'s' if pending_consciousness_count != 1 else ''} from yesterday"
        )
    if inferred_about_me_count:
        pieces.append(
            f"{inferred_about_me_count} new About-Me fact{'s' if inferred_about_me_count != 1 else ''} waiting on your confirm"
        )
    if not pieces:
        return None
    dt = datetime.fromtimestamp(now or time.time()).astimezone()
    header = (
        "Good morning. Quick brief: "
        if 5 <= dt.hour < 12
        else "Here's a quick brief: "
    )
    text = header + "; ".join(pieces) + "."
    return Idea(
        id=str(uuid.uuid4()),
        kind="morning",
        text=text,
        source_signals=[_signal_key("morning", "brief")],
        action=IdeaAction(kind="route", route="/", verb="open_home"),
        severity="info",
        expires_at=_one_day_ttl(now),
    )


def compose_baseline_alert_idea(alert: Any, now: float | None = None) -> Optional[Idea]:
    """Turn a BaselineAlert into a contextual idea with concrete next-step."""
    metric = getattr(alert, "metric_id", "metric")
    deviation = float(getattr(alert, "deviation_sigma", 0.0) or 0.0)
    mean = float(getattr(alert, "baseline_mean", 0.0) or 0.0)
    current = float(getattr(alert, "current_value", 0.0) or 0.0)
    alert_type = getattr(alert, "alert_type", "anomaly")

    severity: IdeaSeverity
    if deviation >= 3.0:
        severity = "critical"
    elif deviation >= 2.0:
        severity = "warning"
    else:
        severity = "info"

    direction = "above" if current > mean else "below"
    signal = _signal_key("baseline", str(metric), str(alert_type), direction)

    templates: dict[str, str] = {
        "hr_resting": (
            f"Your resting heart rate is {current:.0f} bpm today — {deviation:.1f}σ {direction} "
            f"your baseline of {mean:.0f}. Log how you're feeling to help me catch patterns earlier?"
        ),
        "sleep_hours": (
            f"Sleep dropped to {current:.1f}h last night ({deviation:.1f}σ below your {mean:.1f}h baseline). "
            "Want me to push the 10pm wind-down routine tonight?"
        ),
        "hrv": (
            f"HRV is {current:.0f} — {deviation:.1f}σ {direction} your average. "
            "Could be stress or a late night. Want a 5-minute breathing session?"
        ),
        "steps": (
            f"You're at {current:.0f} steps — {deviation:.1f}σ {direction} your baseline of {mean:.0f}. "
            "Short walk between meetings?"
        ),
        "spo2": (
            f"SpO₂ reading: {current:.0f}%. That's {deviation:.1f}σ {direction} your baseline — worth a deep-breath check."
        ),
    }
    text = templates.get(
        metric,
        f"'{metric}' is {current:.1f}, {deviation:.1f}σ {direction} your usual {mean:.1f}. "
        "Noticed worth investigating?",
    )

    action: Optional[IdeaAction] = None
    if metric == "sleep_hours":
        action = IdeaAction(
            kind="install_routine",
            route="/flows/routines/wind_down",
            verb="install",
            payload={"routine_id": "wind_down"},
        )
    elif metric in ("hr_resting", "hrv"):
        action = IdeaAction(
            kind="route",
            route="/health",
            verb="view_health",
        )

    return Idea(
        id=str(uuid.uuid4()),
        kind="health",
        text=text,
        source_signals=[signal],
        action=action,
        severity=severity,
        expires_at=_one_day_ttl(now),
    )


def compose_consciousness_waiting_idea(entity: Any, now: float | None = None) -> Optional[Idea]:
    """Nudge the user about a Consciousness entity in waiting_user status."""
    kind = getattr(entity, "kind", "thought")
    summary = (getattr(entity, "summary", "") or "").strip() or "a half-done task"
    eid = getattr(entity, "id", "")
    age_hours = max(0.0, (time.time() - float(getattr(entity, "updated_at", time.time()))) / 3600.0)
    signal = _signal_key("consciousness", str(kind), str(eid))

    if age_hours < 1:
        age_str = "just now"
    elif age_hours < 24:
        age_str = f"{int(age_hours)}h ago"
    else:
        days = int(age_hours / 24)
        age_str = f"{days}d ago"

    kind_labels = {
        "intent":        "intent",
        "flow":          "TaskFlow",
        "thought":       "conversation",
        "device_stream": "device stream",
        "turn":          "chat turn",
    }
    label = kind_labels.get(kind, "task")
    text = (
        f"You paused the {label} \"{summary}\" {age_str}. "
        "Want to resume where you left off?"
    )
    return Idea(
        id=str(uuid.uuid4()),
        kind="work",
        text=text,
        source_signals=[signal],
        action=IdeaAction(
            kind="resume_consciousness",
            route="/",
            verb="resume",
            payload={"consciousness_id": eid, "consciousness_kind": kind},
        ),
        severity="info",
        expires_at=_one_day_ttl(now),
    )


def compose_about_me_confirm_idea(fact: Any, now: float | None = None) -> Optional[Idea]:
    """Inferred AboutMe fact at 0.5 confidence → ask the user to confirm."""
    text_field = (getattr(fact, "text", "") or "").strip()
    if not text_field:
        return None
    fid = getattr(fact, "id", "")
    signal = _signal_key("about_me", str(fid))
    return Idea(
        id=str(uuid.uuid4()),
        kind="about",
        text=(
            f"I noticed: \"{text_field}\". "
            "Save this to About Me so I can remember it? (Yes confirms; No marks it as not-for-suggestions.)"
        ),
        source_signals=[signal],
        action=IdeaAction(
            kind="confirm_about_me_fact",
            route="/settings",
            verb="review_about_me",
            payload={"fact_id": fid},
        ),
        severity="info",
        expires_at=_one_day_ttl(now),
    )


# ----------------------------------------------------------------------
# Engine
# ----------------------------------------------------------------------


class IdeasEngine:
    """Composes + stores Idea rows and notifies subscribers.

    Wiring (done in ``state.init``):
        store = IdeasStore(db_path=...)
        engine = IdeasEngine(
            store=store,
            consciousness=state.consciousness,
            baseline=state.baseline_engine,
            about_me=state.about_me,
            on_ideas_updated=lambda ideas: state.broadcast_event(
                "ideas_updated", {"count": len(ideas)}
            ),
        )

    The engine exposes three public hooks:
        * ``morning_brief()``          - called by the scheduler at 07:30 local
        * ``handle_baseline_alert()``  - called from BaselineEngine fan-out
        * ``refresh_waiting_user()``   - called when ConsciousnessStore emits a
                                         ``waiting_user`` event
    """

    def __init__(
        self,
        *,
        store: IdeasStore,
        consciousness: Any = None,
        baseline: Any = None,
        about_me: Any = None,
        on_ideas_updated: Optional[Callable[[list[Idea]], None]] = None,
        llm_polish_enabled: bool = False,
        llm_polish_fn: Optional[Callable[[str], str]] = None,
    ):
        self._store = store
        self._consciousness = consciousness
        self._baseline = baseline
        self._about_me = about_me
        self._on_updated = on_ideas_updated
        self._llm_polish_enabled = bool(llm_polish_enabled)
        self._llm_polish_fn = llm_polish_fn

    @property
    def store(self) -> IdeasStore:
        return self._store

    def set_llm_polish(self, enabled: bool, fn: Optional[Callable[[str], str]] = None) -> None:
        self._llm_polish_enabled = bool(enabled)
        if fn is not None:
            self._llm_polish_fn = fn

    def _maybe_polish(self, idea: Idea) -> Idea:
        """Opt-in LLM pass. Silent no-op when disabled or callable missing."""
        if not self._llm_polish_enabled or self._llm_polish_fn is None:
            return idea
        try:
            polished = self._llm_polish_fn(idea.text)
            if polished and polished.strip():
                idea.text = polished.strip()
        except Exception as exc:
            logger.debug("Ideas LLM polish failed: %s", exc)
        return idea

    def _record(self, idea: Optional[Idea]) -> Optional[Idea]:
        if idea is None:
            return None
        for sig in idea.source_signals:
            weight = self._store.dismiss_weight(sig)
            if weight >= 3:
                logger.debug(
                    "Idea suppressed — signal %s dismissed %dx before",
                    sig,
                    weight,
                )
                return None
        # Dedup: don't stack a second active card for a signal that
        # already has a live one (root cause of the 20 identical HR
        # cards). The user dismisses/accepts to clear it; until then one
        # card per signal is enough.
        for sig in idea.source_signals:
            if self._store.has_active_signal(sig):
                logger.debug(
                    "Idea suppressed — active card already exists for signal %s", sig
                )
                return None
        idea = self._maybe_polish(idea)
        self._store.insert(idea)
        return idea

    def _notify(self) -> None:
        if self._on_updated is None:
            return
        try:
            self._on_updated(self._store.list_today())
        except Exception as exc:
            logger.debug("Ideas on_updated callback raised: %s", exc)

    # ------------------------------------------------------------------
    # Triggers
    # ------------------------------------------------------------------

    def morning_brief(self, *, now: float | None = None) -> list[Idea]:
        pending = 0
        if self._consciousness is not None:
            try:
                pending = len(self._consciousness.list_active() or [])
            except Exception as exc:
                logger.debug("Consciousness.list_active failed: %s", exc)

        inferred = 0
        inferred_fact_candidates: list[Any] = []
        if self._about_me is not None:
            try:
                all_facts = self._about_me.list() or []
                inferred_fact_candidates = [
                    f for f in all_facts
                    if getattr(f, "source", "") == "inferred_from_chat"
                    and getattr(f, "confidence", 1.0) < 1.0
                ]
                inferred = len(inferred_fact_candidates)
            except Exception as exc:
                logger.debug("AboutMeStore.list failed: %s", exc)

        stored: list[Idea] = []

        brief = compose_morning_brief(
            pending_consciousness_count=pending,
            inferred_about_me_count=inferred,
            now=now,
        )
        recorded = self._record(brief)
        if recorded is not None:
            stored.append(recorded)

        for fact in inferred_fact_candidates[:3]:
            idea = compose_about_me_confirm_idea(fact, now=now)
            recorded = self._record(idea)
            if recorded is not None:
                stored.append(recorded)

        if stored:
            self._notify()
        return stored

    def handle_baseline_alert(self, alert: Any, *, now: float | None = None) -> Optional[Idea]:
        idea = compose_baseline_alert_idea(alert, now=now)
        recorded = self._record(idea)
        if recorded is not None:
            self._notify()
        return recorded

    def refresh_waiting_user(self, *, now: float | None = None) -> list[Idea]:
        """Generate ideas for every Consciousness entity in waiting_user state."""
        if self._consciousness is None:
            return []
        try:
            waiting = [
                e for e in (self._consciousness.list_active() or [])
                if getattr(e, "status", "") == "waiting_user"
            ]
        except Exception as exc:
            logger.debug("Consciousness.list_active failed in refresh_waiting_user: %s", exc)
            return []

        stored: list[Idea] = []
        for entity in waiting:
            idea = compose_consciousness_waiting_idea(entity, now=now)
            recorded = self._record(idea)
            if recorded is not None:
                stored.append(recorded)
        if stored:
            self._notify()
        return stored

    # Convenience ------------------------------------------------------

    def list_today(self, *, limit: int = 20) -> list[Idea]:
        return self._store.list_today(limit=limit)

    def accept(self, idea_id: str) -> Optional[Idea]:
        return self._store.record_accept(idea_id)

    def dismiss(self, idea_id: str) -> Optional[Idea]:
        return self._store.record_dismiss(idea_id)
