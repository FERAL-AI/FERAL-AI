"""
FERAL Proactive Scheduler
SQLite-backed job scheduler for reminders, health checks, data sync, and proactive insights.

Hardened: file lock for multi-process safety, cron macro support (@daily etc.),
missed-job catch-up within 1-day window.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

from config.loader import feral_data_home, local_timezone_name

logger = logging.getLogger("feral.scheduler")

_ONE_DAY_SECONDS = 86400


class FileLock:
    """Advisory file lock using fcntl.flock for multi-process SQLite safety."""

    def __init__(self, lock_path: str):
        self._lock_path = lock_path
        self._fd: int | None = None

    def acquire(self) -> bool:
        try:
            self._fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR)
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (OSError, IOError):
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            return False

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError(f"Could not acquire lock: {self._lock_path}")
        return self

    def __exit__(self, *exc):
        self.release()

    def __del__(self):
        self.release()


class JobType(str, Enum):
    REMINDER = "reminder"
    HEALTH_CHECK = "health_check"
    DATA_SYNC = "data_sync"
    PROACTIVE_INSIGHT = "proactive_insight"
    CUSTOM = "custom"
    SCHEDULED = "scheduled"
    TRIGGERED = "triggered"
    CHAIN = "chain"
    WATCHER = "watcher"


@dataclass
class ScheduledJob:
    id: int
    job_type: JobType
    cron_expr: str
    description: str
    payload: dict[str, Any]
    session_id: str
    created_at: float
    last_run: Optional[float]
    next_run: float
    enabled: bool
    run_count: int
    recurring: bool = True
    priority: int = 1
    tz_name: str = "UTC"


def _default_db_path() -> str:
    base = feral_data_home()
    base.mkdir(parents=True, exist_ok=True)
    return str(base / "scheduled_jobs.db")


_SUPPORTED_CRON_FORMS = (
    "'every Nm' (e.g. 'every 30m'), 'every Nh' (e.g. 'every 2h'), "
    "'daily HH:MM' (e.g. 'daily 21:00'), a 5-field cron subset "
    "('*/N * * * *', '0 */N * * *', 'M H * * *'), or a macro "
    "(@hourly, @daily, @midnight, @weekly, @monthly, @yearly, @annually)"
)


class UnparseableCronExpression(ValueError):
    """Raised when a schedule string matches none of the supported forms.

    Deliberately a hard error rather than a default. ``_compute_next_run``
    used to end in ``return from_time + 60.0``, so any expression the parser
    did not recognise silently became "run every 60 seconds". Two routines
    written as ``"nightly at 9pm"`` matched no pattern and therefore fired
    4,170 and 4,130 times, each driving a full multi-agent orchestrator turn
    at ~20k prompt tokens, which pinned ``chat`` at $9.99 in a single hour
    against a $10 cap. A schedule we cannot parse is a schedule we must not
    guess at.
    """

    def __init__(self, cron_expr: str):
        self.cron_expr = cron_expr
        super().__init__(
            f"Unrecognised schedule {cron_expr!r}. Supported forms: "
            f"{_SUPPORTED_CRON_FORMS}."
        )


def _cron_field_values(spec: str, vmin: int, vmax: int) -> Optional[list[int]]:
    """Expand one cron field to the values it matches.

    Supports ``*``, ``*/N``, a plain integer, and comma lists of integers.
    Returns None (not an empty list) when the field is unparseable or any
    value is out of range, so ``60 25 * * *`` is rejected rather than
    quietly matching nothing.
    """
    if spec == "*":
        return list(range(vmin, vmax + 1))
    if spec.startswith("*/"):
        step_raw = spec[2:]
        if not step_raw.isdigit() or int(step_raw) < 1:
            return None
        return list(range(vmin, vmax + 1, int(step_raw)))
    values: list[int] = []
    for part in spec.split(","):
        if not part.isdigit():
            return None
        value = int(part)
        if not (vmin <= value <= vmax):
            return None
        values.append(value)
    return sorted(set(values)) or None


# A yearly schedule needs at most ~366 day-steps; the extra margin covers
# leap-day-only expressions such as `0 0 29 2 *`.
_CRON_SCAN_DAYS = 366 * 5


def _scan_5field_cron(
    minute: str, hour: str, dom: str, month: str, dow: str,
    from_time: float, tz: timezone | ZoneInfo,
) -> Optional[float]:
    """Next matching minute strictly after *from_time*, or None if the
    expression is unparseable or matches nothing within the scan window.

    Day-by-day scan: date fields are checked once per day and the
    hour/minute lists are walked only on days that match, so a yearly
    expression costs ~365 cheap iterations.
    """
    minutes = _cron_field_values(minute, 0, 59)
    hours = _cron_field_values(hour, 0, 23)
    doms = _cron_field_values(dom, 1, 31)
    months = _cron_field_values(month, 1, 12)
    dows = _cron_field_values(dow, 0, 7)
    if minutes is None or hours is None or doms is None or months is None or dows is None:
        return None

    # cron day-of-week: 0 and 7 both mean Sunday.
    dow_set = {d % 7 for d in dows}
    dom_restricted = dom != "*"
    dow_restricted = dow != "*"

    day = datetime.fromtimestamp(from_time, tz=tz).date()
    for _ in range(_CRON_SCAN_DAYS):
        midnight = datetime(day.year, day.month, day.day, tzinfo=tz)
        if midnight.month in months:
            # Standard cron: when BOTH day-of-month and day-of-week are
            # restricted, a day matches if EITHER does.
            cron_dow = (midnight.weekday() + 1) % 7
            if dom_restricted and dow_restricted:
                day_matches = midnight.day in doms or cron_dow in dow_set
            elif dom_restricted:
                day_matches = midnight.day in doms
            elif dow_restricted:
                day_matches = cron_dow in dow_set
            else:
                day_matches = True
            if day_matches:
                for hh in hours:
                    for mm in minutes:
                        candidate = midnight.replace(hour=hh, minute=mm)
                        if candidate.timestamp() > from_time:
                            return candidate.timestamp()
        day = day + timedelta(days=1)
    return None


def _compute_next_run(cron_expr: str, from_time: float, tz: timezone | ZoneInfo | None = None) -> float:
    """
    Compute the next run timestamp (epoch seconds) strictly after *from_time*.

    *tz* is used for wall-clock anchored schedules (``daily HH:MM`` and
    cron fields with specific hour/minute).  Interval-only schedules
    (``every Nm``, ``every Nh``, ``*/N * * * *``) are timezone-agnostic.

    Supported forms:
    - "every Nm" / "every N m" — every N minutes
    - "every Nh" / "every N h" — every N hours
    - "daily HH:MM" — once per day at HH:MM
    - 5-field cron (subset): */N * * * *, M H * * *, etc.

    Raises:
        UnparseableCronExpression: the expression matches none of the above.
            Callers must disable the job — never re-arm on a guess.
    """
    if tz is None:
        tz = timezone.utc

    raw = cron_expr.strip()
    if not raw:
        raise UnparseableCronExpression(cron_expr)

    # Cron macros
    _macros = {
        "@yearly":  "0 0 1 1 *",
        "@annually": "0 0 1 1 *",
        "@monthly": "0 0 1 * *",
        "@weekly":  "0 0 * * 0",
        "@daily":   "0 0 * * *",
        "@midnight": "0 0 * * *",
        "@hourly":  "0 * * * *",
    }
    if raw.lower() in _macros:
        raw = _macros[raw.lower()]

    # every N minutes
    m = re.match(r"^every\s+(\d+)\s*m(?:inutes?)?$", raw, re.I)
    if m:
        n = max(1, int(m.group(1)))
        return from_time + n * 60.0

    # every N hours
    m = re.match(r"^every\s+(\d+)\s*h(?:ours?)?$", raw, re.I)
    if m:
        n = max(1, int(m.group(1)))
        return from_time + n * 3600.0

    # daily HH:MM
    m = re.match(r"^daily\s+(\d{1,2}):(\d{2})$", raw, re.I)
    if m:
        hh = int(m.group(1)) % 24
        mm = int(m.group(2)) % 60
        dt = datetime.fromtimestamp(from_time, tz=tz)
        target = dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target.timestamp() <= from_time:
            target = target + timedelta(days=1)
        return target.timestamp()

    # 5-field cron (limited)
    parts = raw.split()
    if len(parts) == 5:
        minute, hour, dom, month, dow = parts

        def _parse_field(spec: str, vmin: int, vmax: int) -> Optional[int]:
            if spec == "*":
                return None
            if spec.startswith("*/"):
                return int(spec[2:])
            if spec.isdigit():
                v = int(spec)
                if vmin <= v <= vmax:
                    return v
            return None

        # */N * * * *  -> every N minutes (interval from last boundary)
        if minute.startswith("*/") and hour == dom == month == dow == "*":
            step = max(1, int(minute[2:]))
            return float(from_time + step * 60.0)

        # 0 */N * * * -> every N hours
        if (
            minute == "0"
            and hour.startswith("*/")
            and dom == month == dow == "*"
        ):
            step = max(1, int(hour[2:]))
            interval = step * 3600
            nxt = int(from_time // interval) * interval + interval
            if nxt <= from_time:
                nxt += interval
            return float(nxt)

        # M H * * * -> daily at H:M
        if dom == month == dow == "*":
            mm_val = _parse_field(minute, 0, 59)
            hh_val = _parse_field(hour, 0, 23)
            if mm_val is not None and hh_val is not None:
                dt = datetime.fromtimestamp(from_time, tz=tz)
                target = dt.replace(hour=hh_val, minute=mm_val, second=0, microsecond=0)
                if target.timestamp() <= from_time:
                    target = target + timedelta(days=1)
                return target.timestamp()

        # Everything else 5-field: scan forward for the next matching
        # minute. Needed because the special cases above only cover
        # `* * *` in the date fields and a fully-specified H:M — so
        # `0 * * * *` (hourly on the hour), `0 0 * * 0` (weekly),
        # `0 0 1 * *` (monthly) and `0 0 1 1 *` (yearly) all fell through
        # to the old 60-second catch-all and fired once a minute forever.
        # That includes the @hourly/@weekly/@monthly/@yearly macros, which
        # this module has advertised in its docstring the whole time. The
        # old macro tests passed only because both sides of the comparison
        # returned the same wrong +60s.
        scanned = _scan_5field_cron(minute, hour, dom, month, dow, from_time, tz)
        if scanned is not None:
            return scanned

    # No fallback by design. This used to `return from_time + 60.0`, which
    # turned every typo into a once-a-minute job (see
    # UnparseableCronExpression for the incident). Refusing to guess is the
    # only safe answer: callers disable the job and tell the operator.
    raise UnparseableCronExpression(cron_expr)


class CronService:
    """Background-friendly scheduler with a SQLite job store."""

    @staticmethod
    def _compute_next_run(cron_expr: str, from_time: float, tz: timezone | ZoneInfo | None = None) -> float:
        """Delegate to module parser; kept on the class for discovery/testing."""
        return _compute_next_run(cron_expr, from_time, tz=tz)

    def __init__(self, db_path: Optional[str] = None, config: Optional[dict] = None):
        config = config or {}
        self._db_path = db_path or _default_db_path()
        self._lock = threading.Lock()
        self._file_lock = FileLock(self._db_path + ".lock")
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._callback: Optional[Callable[[ScheduledJob], None]] = None
        # Default to the HOST's local timezone (derived, not hardcoded) so
        # "follow the line at 3:01 PM" fires at 15:01 LOCAL, not UTC. An
        # explicit config["timezone"] still overrides; a bad value degrades
        # to the host local tz, then UTC. Per-job ``tz_name`` (stored on
        # each row) is unaffected — already-scheduled jobs keep their tz.
        configured_tz = (config.get("timezone") or "").strip()
        if configured_tz:
            try:
                self._timezone = ZoneInfo(configured_tz)
            except Exception:
                self._timezone = ZoneInfo(local_timezone_name())
        else:
            self._timezone = ZoneInfo(local_timezone_name())
        self._max_concurrent: int = int(config.get("max_concurrent_jobs", 5))
        self._running_jobs: set[int] = set()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        conn = self._conn
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduled_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_type TEXT NOT NULL,
                    cron_expr TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL DEFAULT '{}',
                    session_id TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    last_run REAL,
                    next_run REAL NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    run_count INTEGER NOT NULL DEFAULT 0,
                    recurring INTEGER NOT NULL DEFAULT 1,
                    priority INTEGER NOT NULL DEFAULT 1,
                    tz_name TEXT NOT NULL DEFAULT 'UTC'
                )
                """
            )
            for col, default in [
                ("recurring", "INTEGER NOT NULL DEFAULT 1"),
                ("priority", "INTEGER NOT NULL DEFAULT 1"),
                ("tz_name", "TEXT NOT NULL DEFAULT 'UTC'"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE scheduled_jobs ADD COLUMN {col} {default}")
                except sqlite3.OperationalError:
                    pass
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sched_next ON scheduled_jobs (next_run)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS routine_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL,
                    started_at REAL NOT NULL,
                    finished_at REAL,
                    status TEXT NOT NULL DEFAULT 'running',
                    result TEXT NOT NULL DEFAULT '{}',
                    error TEXT,
                    FOREIGN KEY (job_id) REFERENCES scheduled_jobs(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_job ON routine_runs (job_id, started_at DESC)"
            )

    def close(self) -> None:
        self.stop()
        with self._lock:
            self._conn.close()
        self._file_lock.release()

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> ScheduledJob:
        payload = json.loads(row["payload"] or "{}")
        try:
            recurring = bool(row["recurring"])
        except (IndexError, KeyError):
            recurring = True
        try:
            priority = int(row["priority"])
        except (IndexError, KeyError):
            priority = 1
        try:
            tz_name = row["tz_name"] or "UTC"
        except (IndexError, KeyError):
            tz_name = "UTC"
        return ScheduledJob(
            id=row["id"],
            job_type=JobType(row["job_type"]),
            cron_expr=row["cron_expr"],
            description=row["description"] or "",
            payload=payload,
            session_id=row["session_id"] or "",
            created_at=row["created_at"],
            last_run=row["last_run"],
            next_run=row["next_run"],
            enabled=bool(row["enabled"]),
            run_count=row["run_count"],
            recurring=recurring,
            priority=priority,
            tz_name=tz_name,
        )

    def create_job(
        self,
        job_type: JobType | str,
        cron_expr: str,
        description: str,
        payload: dict[str, Any],
        session_id: str,
        recurring: bool = True,
        priority: int = 1,
        tz_name: str | None = None,
    ) -> ScheduledJob:
        if isinstance(job_type, str):
            job_type = JobType(job_type)
        tz_name = tz_name or str(self._timezone)
        tz = ZoneInfo(tz_name)
        now = time.time()
        # Validate at write time. A bad expression should be rejected when the
        # routine is created — by the operator, in the UI, immediately — not
        # discovered later from the bill. Propagates
        # UnparseableCronExpression to the caller.
        nxt = CronService._compute_next_run(cron_expr, now, tz=tz)
        payload_json = json.dumps(payload)
        with self._lock:
            conn = self._conn
            cur = conn.execute(
                """
                INSERT INTO scheduled_jobs
                (job_type, cron_expr, description, payload, session_id,
                 created_at, last_run, next_run, enabled, run_count, recurring,
                 priority, tz_name)
                VALUES (?, ?, ?, ?, ?, ?, NULL, ?, 1, 0, ?, ?, ?)
                """,
                (
                    job_type.value,
                    cron_expr,
                    description,
                    payload_json,
                    session_id,
                    now,
                    nxt,
                    int(recurring),
                    priority,
                    tz_name,
                ),
            )
            jid = cur.lastrowid
            conn.commit()
            row = conn.execute(
                "SELECT * FROM scheduled_jobs WHERE id = ?", (jid,)
            ).fetchone()
        assert row is not None
        return self._row_to_job(row)

    def delete_job(self, job_id: int) -> bool:
        with self._lock:
            conn = self._conn
            cur = conn.execute(
                "DELETE FROM scheduled_jobs WHERE id = ?", (job_id,)
            )
            conn.commit()
            return cur.rowcount > 0

    def list_jobs(self, session_id: Optional[str] = None) -> list[ScheduledJob]:
        with self._lock:
            conn = self._conn
            if session_id is None:
                rows = conn.execute(
                    "SELECT * FROM scheduled_jobs ORDER BY id ASC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM scheduled_jobs WHERE session_id = ? ORDER BY id ASC",
                    (session_id,),
                ).fetchall()
        return [self._row_to_job(r) for r in rows]

    def get_due_jobs(self) -> list[ScheduledJob]:
        now = time.time()
        with self._lock:
            conn = self._conn
            rows = conn.execute(
                """
                SELECT * FROM scheduled_jobs
                WHERE enabled = 1 AND next_run <= ?
                ORDER BY priority DESC, next_run ASC
                """,
                (now,),
            ).fetchall()
        return [self._row_to_job(r) for r in rows]

    def mark_completed(self, job_id: int) -> None:
        now = time.time()
        with self._lock:
            conn = self._conn
            row = conn.execute(
                "SELECT * FROM scheduled_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return

            try:
                is_recurring = bool(row["recurring"])
            except (IndexError, KeyError):
                is_recurring = True

            if is_recurring:
                cron = row["cron_expr"]
                try:
                    tz = ZoneInfo(row["tz_name"] or "UTC")
                except (KeyError, IndexError):
                    tz = self._timezone
                try:
                    nxt = CronService._compute_next_run(cron, now, tz=tz)
                except UnparseableCronExpression:
                    # The runaway path. Re-arming on a guess here is what
                    # turned "nightly at 9pm" into 4,170 orchestrator turns.
                    # Disable the job instead so it costs nothing more, and
                    # shout — the operator has to fix the expression.
                    conn.execute(
                        """
                        UPDATE scheduled_jobs
                        SET last_run = ?, run_count = run_count + 1, enabled = 0
                        WHERE id = ?
                        """,
                        (now, job_id),
                    )
                    conn.commit()
                    logger.critical(
                        "feral.scheduler.disabled_unparseable_cron: job %d "
                        "(%r) has schedule %r, which matches no supported "
                        "form. The job has been DISABLED after this run so it "
                        "cannot re-fire every 60s. Edit the routine to use "
                        "%s, then re-enable it.",
                        job_id, row["description"], cron, _SUPPORTED_CRON_FORMS,
                    )
                    return
                conn.execute(
                    """
                    UPDATE scheduled_jobs
                    SET last_run = ?, next_run = ?, run_count = run_count + 1
                    WHERE id = ?
                    """,
                    (now, nxt, job_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE scheduled_jobs
                    SET last_run = ?, run_count = run_count + 1, enabled = 0
                    WHERE id = ?
                    """,
                    (now, job_id),
                )
                logger.info(f"Non-recurring job {job_id} completed and disabled")
            conn.commit()

    def pause_job(self, job_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE scheduled_jobs SET enabled = 0 WHERE id = ?", (job_id,)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def resume_job(self, job_id: int) -> bool:
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT cron_expr, tz_name FROM scheduled_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if not row:
                return False
            try:
                tz = ZoneInfo(row["tz_name"] or "UTC")
            except (KeyError, IndexError):
                tz = self._timezone
            try:
                nxt = CronService._compute_next_run(row["cron_expr"], now, tz=tz)
            except UnparseableCronExpression:
                # Refuse to resume rather than resume-at-60s. The job stays
                # disabled until the expression is fixed.
                logger.error(
                    "feral.scheduler.resume_refused_unparseable_cron: job %d "
                    "has schedule %r, which matches no supported form. It "
                    "stays disabled. Edit the routine to use %s.",
                    job_id, row["cron_expr"], _SUPPORTED_CRON_FORMS,
                )
                return False
            self._conn.execute(
                "UPDATE scheduled_jobs SET enabled = 1, next_run = ? WHERE id = ?",
                (nxt, job_id),
            )
            self._conn.commit()
            return True

    def get_job(self, job_id: int) -> Optional[ScheduledJob]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM scheduled_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_job(row)

    def record_run_start(self, job_id: int) -> int:
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO routine_runs (job_id, started_at, status) VALUES (?, ?, 'running')",
                (job_id, now),
            )
            self._conn.commit()
            return cur.lastrowid

    def record_run_finish(self, run_id: int, status: str, result: dict, error: str | None = None) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE routine_runs SET finished_at = ?, status = ?, result = ?, error = ? WHERE id = ?",
                (now, status, json.dumps(result), error, run_id),
            )
            self._conn.commit()

    def get_runs(self, job_id: int, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM routine_runs WHERE job_id = ? ORDER BY started_at DESC LIMIT ?",
                (job_id, limit),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "job_id": r["job_id"],
                "started_at": r["started_at"],
                "finished_at": r["finished_at"],
                "status": r["status"],
                "result": json.loads(r["result"] or "{}"),
                "error": r["error"],
            }
            for r in rows
        ]

    # ─── Natural Language Automations ───

    _NL_PATTERNS: list[tuple[str, str]] = [
        (r"every\s+morning", "daily 07:00"),
        (r"every\s+evening", "daily 19:00"),
        (r"every\s+night", "daily 22:00"),
        (r"every\s+afternoon", "daily 14:00"),
        (r"every\s+day\s+at\s+(\d{1,2})\s*(am|pm)", "_daily_ampm"),
        (r"every\s+day\s+at\s+(\d{1,2}):(\d{2})\s*(am|pm)?", "_daily_hhmm"),
        (r"every\s+(\d+)\s*h(?:ours?)?", "_every_hours"),
        (r"every\s+(\d+)\s*m(?:in(?:ute)?s?)?", "_every_minutes"),
        (r"every\s+hour", "every 1h"),
        (r"weekly\s+(?:on\s+)?(\w+)(?:\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?)?", "_weekly"),
        (r"daily\s+(\d{1,2}):(\d{2})", "_daily_hhmm_bare"),
    ]

    @staticmethod
    def _resolve_ampm(hour: int, ampm: Optional[str]) -> int:
        if ampm is None:
            return hour % 24
        ampm = ampm.lower()
        if ampm == "pm" and hour != 12:
            return (hour + 12) % 24
        if ampm == "am" and hour == 12:
            return 0
        return hour % 24

    @classmethod
    def _parse_nl_to_cron(cls, text: str) -> Optional[str]:
        """Try regex-based natural language → cron_expr conversion."""
        t = text.strip().lower()

        for pattern, action in cls._NL_PATTERNS:
            m = re.search(pattern, t, re.I)
            if not m:
                continue

            if action == "_daily_ampm":
                hh = cls._resolve_ampm(int(m.group(1)), m.group(2))
                return f"daily {hh:02d}:00"

            if action == "_daily_hhmm":
                hh = int(m.group(1))
                mm = int(m.group(2))
                ampm = m.group(3) if m.lastindex and m.lastindex >= 3 else None
                hh = cls._resolve_ampm(hh, ampm)
                return f"daily {hh:02d}:{mm:02d}"

            if action == "_daily_hhmm_bare":
                return f"daily {int(m.group(1)):02d}:{int(m.group(2)):02d}"

            if action == "_every_hours":
                return f"every {m.group(1)}h"

            if action == "_every_minutes":
                return f"every {m.group(1)}m"

            if action == "_weekly":
                day_map = {
                    "mon": "1", "monday": "1", "tue": "2", "tuesday": "2",
                    "wed": "3", "wednesday": "3", "thu": "4", "thursday": "4",
                    "fri": "5", "friday": "5", "sat": "6", "saturday": "6",
                    "sun": "0", "sunday": "0",
                }
                day_name = m.group(1).lower()
                dow = day_map.get(day_name, "1")
                hour_raw = int(m.group(2)) if m.group(2) else 9
                ampm = m.group(4) if m.lastindex and m.lastindex >= 4 else None
                hh = cls._resolve_ampm(hour_raw, ampm)
                mm = int(m.group(3)) if m.group(3) else 0
                return f"{mm} {hh} * * {dow}"

            return action

        return None

    def create_from_natural_language(
        self,
        text: str,
        session_id: str,
        llm: Optional[Any] = None,
    ) -> ScheduledJob:
        """
        Parse a natural-language automation request and create a ScheduledJob.
        Uses LLM if provided, otherwise falls back to regex.
        """
        cron_expr: Optional[str] = None
        description = text
        action_text = text

        if llm is not None:
            try:
                cron_expr, description, action_text = self._parse_with_llm(text, llm)
            except Exception as exc:
                logger.warning(f"LLM parsing failed, falling back to regex: {exc}")
                cron_expr = None

        if cron_expr is None:
            cron_expr = self._parse_nl_to_cron(text)

        if cron_expr is None:
            logger.warning(f"Could not parse schedule from: {text!r} — defaulting to every 1h")
            cron_expr = "every 1h"

        payload = {"action_text": action_text, "source": "natural_language", "original_text": text}
        job = self.create_job(
            job_type=JobType.CUSTOM,
            cron_expr=cron_expr,
            description=description,
            payload=payload,
            session_id=session_id,
            recurring=True,
        )
        logger.info(f"NL automation created: id={job.id} cron={cron_expr!r} desc={description!r}")
        return job

    @staticmethod
    def _parse_with_llm(text: str, llm: Any) -> tuple[str, str, str]:
        """
        Send text to the LLM and extract structured schedule info.
        Expects llm to have a synchronous `complete(prompt)` or async `chat(...)`.
        Returns (cron_expr, description, action_text).
        """
        prompt = (
            "Extract scheduling info from the following user request. "
            "Return ONLY valid JSON with keys: cron_expr, description, action_text.\n"
            "cron_expr should be one of: 'every Nm', 'every Nh', 'daily HH:MM', "
            "or a 5-field cron expression.\n"
            "description is a short human summary.\n"
            "action_text is the command/action to perform.\n\n"
            f"User request: \"{text}\"\n\nJSON:"
        )

        response_text: str = ""
        if hasattr(llm, "complete"):
            response_text = str(llm.complete(prompt))
        elif hasattr(llm, "complete_sync"):
            response_text = str(llm.complete_sync(prompt))
        else:
            raise ValueError("LLM object has no suitable synchronous completion method")

        cleaned = response_text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```\w*\n?", "", cleaned)
            cleaned = re.sub(r"\n?```$", "", cleaned)

        data = json.loads(cleaned)
        return (
            data.get("cron_expr", "every 1h"),
            data.get("description", text),
            data.get("action_text", text),
        )

    def list_automations(self, session_id: Optional[str] = None) -> list[ScheduledJob]:
        """Return user-created automations (CUSTOM jobs), optionally filtered by session."""
        with self._lock:
            conn = self._conn
            if session_id is None:
                rows = conn.execute(
                    "SELECT * FROM scheduled_jobs WHERE job_type = ? ORDER BY id ASC",
                    (JobType.CUSTOM.value,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM scheduled_jobs WHERE job_type = ? AND session_id = ? ORDER BY id ASC",
                    (JobType.CUSTOM.value, session_id),
                ).fetchall()
        return [self._row_to_job(r) for r in rows]

    def delete_automation(self, job_id: int) -> bool:
        """Remove a user automation by ID (only deletes CUSTOM jobs)."""
        with self._lock:
            conn = self._conn
            cur = conn.execute(
                "DELETE FROM scheduled_jobs WHERE id = ? AND job_type = ?",
                (job_id, JobType.CUSTOM.value),
            )
            conn.commit()
            deleted = cur.rowcount > 0
        if deleted:
            logger.info(f"Deleted automation job_id={job_id}")
        return deleted

    def _catchup_missed_jobs(self) -> None:
        """On boot, fire jobs whose next_run passed while the brain was down.

        Only catches up jobs missed within the last 24 hours to avoid
        avalanche-firing very old jobs after a long outage.
        """
        now = time.time()
        cutoff = now - _ONE_DAY_SECONDS
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, description, next_run, cron_expr FROM scheduled_jobs "
                "WHERE enabled = 1 AND next_run < ? AND next_run >= ?",
                (now, cutoff),
            ).fetchall()
        caught_up = 0
        for row in rows:
            job_id, name, next_run, _cron = row["id"], row["description"], row["next_run"], row["cron_expr"]
            logger.info("Missed job '%s' (id=%d, was due %.0fs ago) — catching up", name, job_id, now - next_run)
            job = self.get_job(job_id)
            if job and self._callback:
                try:
                    self._callback(job)
                    caught_up += 1
                finally:
                    self.mark_completed(job_id)
        if caught_up:
            logger.info("Caught up %d missed jobs", caught_up)

    _MAX_POLL_SECONDS = 30.0

    def _poll_interval(self) -> float:
        """Adaptive sleep before the next due-job scan.

        Historically the loop slept a flat 30s, so a job due at 15:01:00
        could fire as late as 15:01:30. We keep the 30s ceiling when no
        job is near, but shrink the wait to ~1s granularity as the
        soonest ``next_run`` approaches so wall-clock routines fire on
        time. One cheap indexed MIN() query per tick — no scheduler
        rewrite. Any failure falls back to the flat 30s.
        """
        try:
            now = time.time()
            with self._lock:
                row = self._conn.execute(
                    "SELECT MIN(next_run) AS soonest FROM scheduled_jobs WHERE enabled = 1"
                ).fetchone()
            soonest = row["soonest"] if row is not None else None
            if soonest is None:
                return self._MAX_POLL_SECONDS
            delta = float(soonest) - now
            if delta <= 0:
                return 1.0
            return max(1.0, min(self._MAX_POLL_SECONDS, delta))
        except Exception:
            return self._MAX_POLL_SECONDS

    def _loop(self) -> None:
        self._catchup_missed_jobs()
        while not self._stop.wait(self._poll_interval()):
            if self._callback is None:
                continue
            due = self.get_due_jobs()
            for job in due:
                if len(self._running_jobs) >= self._max_concurrent:
                    logger.warning(
                        "Max concurrent jobs (%d) reached, deferring job %d",
                        self._max_concurrent,
                        job.id,
                    )
                    break
                self._running_jobs.add(job.id)
                try:
                    self._callback(job)
                finally:
                    self._running_jobs.discard(job.id)
                    self.mark_completed(job.id)

    def start(self, callback: Callable[[ScheduledJob], None]) -> None:
        """Poll every 30s for due jobs and invoke callback, then reschedule."""
        self._callback = callback
        self._stop.clear()
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=35.0)
            self._thread = None


def get_scheduler_skill_manifest() -> dict[str, Any]:
    """Manifest for agent tool use: create/list/delete scheduled jobs."""
    return {
        "name": "feral_scheduler",
        "version": "1.0.0",
        "description": "Create and manage proactive scheduled jobs (SQLite-backed).",
        "endpoints": [
            {
                "name": "create_job",
                "method": "POST",
                "path": "/scheduler/jobs",
                "body": {
                    "job_type": "reminder | health_check | data_sync | proactive_insight | custom",
                    "cron_expr": "string (e.g. every 15m, daily 09:30, */5 * * * *)",
                    "description": "string",
                    "payload": "object",
                    "session_id": "string",
                },
            },
            {
                "name": "list_jobs",
                "method": "GET",
                "path": "/scheduler/jobs",
                "query": {"session_id": "optional string"},
            },
            {
                "name": "delete_job",
                "method": "DELETE",
                "path": "/scheduler/jobs/{job_id}",
            },
        ],
    }
