"""Supervisor — one seat that sees every input the Brain takes action on.

Today every entry point (web chat, /v1/node HUP, voice, cron, channels,
proactive, ui_event) reaches the orchestrator independently. There is no
single place to audit "everything that happened in the last minute" or
enforce a cross-cutting policy.

``Supervisor`` sits in front of ``Orchestrator.handle_command``,
``handle_command_stream``, and ``handle_ui_event``. It:

  * Records every call as a row in ``supervisor_events`` (SQLite) —
    source, kind, session_id, actor, payload hash, decision, latency.
  * Broadcasts a ``supervisor_event`` WS frame so the v2 /oversight page
    can render a live event river.
  * Exposes a kill-switch (``set_paused(True)``) that blocks every
    dispatch until cleared — the big red "Pause all actions" button the
    digital-twin commit needs.
  * Emits structured decision hooks (``allowed`` / ``denied`` /
    ``queued``) so Commit 7 can plug a policy gate in without another
    rewrite.

Designed to be thin: it WRAPS the orchestrator, it does not replace
it. Subsystems that already hold a reference to ``state.orchestrator``
keep working — ``state.wire_supervisor(orchestrator, ...)`` replaces
the public methods with the wrapped versions in place.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import uuid4

logger = logging.getLogger("feral.supervisor")


@dataclass
class SupervisorEvent:
    """One row in the oversight log."""
    event_id: str
    ts: float
    source: str          # web / node / voice / cron / channel / proactive / ui / twin / …
    kind: str            # command / command_stream / ui_event / proactive / …
    session_id: str
    actor: str           # user / twin / system
    payload_hash: str    # sha256 truncated
    payload_summary: str # first 200 chars of the utterance or JSON snippet
    decision: str        # allowed / denied / queued / error
    latency_ms: int      # time spent inside orchestrator handler
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class SupervisorStore:
    """SQLite-backed audit log.

    WAL-mode; small schema; one call per event. Kept separate from the
    memory store because the retention policy is different — by default
    we keep 90 days of supervisor events and nothing else.
    """

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            import os
            home = os.environ.get("FERAL_HOME", str(Path.home() / ".feral"))
            Path(home).mkdir(parents=True, exist_ok=True)
            db_path = str(Path(home) / "supervisor.db")
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS supervisor_events (
                        event_id       TEXT PRIMARY KEY,
                        ts             REAL NOT NULL,
                        source         TEXT NOT NULL,
                        kind           TEXT NOT NULL,
                        session_id     TEXT NOT NULL,
                        actor          TEXT NOT NULL,
                        payload_hash   TEXT NOT NULL,
                        payload_summary TEXT NOT NULL,
                        decision       TEXT NOT NULL,
                        latency_ms     INTEGER NOT NULL,
                        detail         TEXT NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_sv_ts ON supervisor_events(ts DESC)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_sv_source ON supervisor_events(source)
                """)
                conn.commit()
            finally:
                conn.close()

    def insert(self, event: SupervisorEvent) -> None:
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    """INSERT INTO supervisor_events
                       (event_id, ts, source, kind, session_id, actor,
                        payload_hash, payload_summary, decision, latency_ms, detail)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event.event_id,
                        event.ts,
                        event.source,
                        event.kind,
                        event.session_id,
                        event.actor,
                        event.payload_hash,
                        event.payload_summary,
                        event.decision,
                        event.latency_ms,
                        json.dumps(event.detail),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def recent(
        self,
        *,
        limit: int = 50,
        source: str = "",
        actor: str = "",
        decision: str = "",
    ) -> list[dict]:
        q = "SELECT * FROM supervisor_events WHERE 1=1"
        args: list = []
        if source:
            q += " AND source = ?"
            args.append(source)
        if actor:
            q += " AND actor = ?"
            args.append(actor)
        if decision:
            q += " AND decision = ?"
            args.append(decision)
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(max(1, min(limit, 500)))
        conn = self._conn()
        try:
            rows = conn.execute(q, args).fetchall()
            return [
                {
                    "event_id": r["event_id"],
                    "ts": r["ts"],
                    "source": r["source"],
                    "kind": r["kind"],
                    "session_id": r["session_id"],
                    "actor": r["actor"],
                    "payload_hash": r["payload_hash"],
                    "payload_summary": r["payload_summary"],
                    "decision": r["decision"],
                    "latency_ms": r["latency_ms"],
                    "detail": _safe_json(r["detail"]),
                }
                for r in rows
            ]
        finally:
            conn.close()

    def stats(self) -> dict:
        conn = self._conn()
        try:
            total = conn.execute("SELECT COUNT(*) FROM supervisor_events").fetchone()[0]
            rows = conn.execute(
                "SELECT source, COUNT(*) c FROM supervisor_events GROUP BY source ORDER BY c DESC"
            ).fetchall()
            return {
                "total": total,
                "by_source": {r[0]: r[1] for r in rows},
            }
        finally:
            conn.close()

    def purge(self) -> int:
        """Delete every row — used by tests and by the kill-switch reset."""
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute("DELETE FROM supervisor_events")
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()


def _safe_json(raw: str) -> dict:
    try:
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {"value": data}
    except Exception:
        return {}


def _hash_payload(payload: Any) -> str:
    try:
        blob = json.dumps(payload, sort_keys=True, default=str).encode()
    except Exception:
        blob = str(payload).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def _summarise(payload: Any, limit: int = 200) -> str:
    if isinstance(payload, str):
        return payload[:limit]
    try:
        return json.dumps(payload, default=str)[:limit]
    except Exception:
        return str(payload)[:limit]


PolicyGate = Callable[[SupervisorEvent], str]
EventBroadcaster = Callable[[dict], "asyncio.Future | None"]


class Supervisor:
    """The single oversight seat.

    Usage — wired once at boot::

        supervisor = Supervisor(store=SupervisorStore(), broadcaster=state.broadcast)
        supervisor.wrap(state.orchestrator)

    After ``wrap``, every call to ``orchestrator.handle_command`` /
    ``handle_command_stream`` / ``handle_ui_event`` is audited + subject
    to the policy gate + kill-switch.
    """

    def __init__(
        self,
        *,
        store: Optional[SupervisorStore] = None,
        broadcaster: Optional[EventBroadcaster] = None,
        policy_gate: Optional[PolicyGate] = None,
    ):
        self.store = store or SupervisorStore()
        self.broadcaster = broadcaster
        self.policy_gate = policy_gate
        self._paused = False
        self._recent: list[dict] = []  # in-memory fast cache for /oversight
        self._recent_cap = 200
        self._orchestrator = None
        self._orig: dict[str, Any] = {}

    # ── Kill switch ──────────────────────────────────────────────

    def set_paused(self, paused: bool) -> None:
        self._paused = bool(paused)
        logger.warning("Supervisor %s", "PAUSED" if self._paused else "resumed")

    @property
    def paused(self) -> bool:
        return self._paused

    # ── Wrap / unwrap ────────────────────────────────────────────

    def wrap(self, orchestrator) -> None:
        """Replace the public methods on *orchestrator* with audited versions.

        Idempotent — calling twice is safe because we check ``_orig``.
        """
        if self._orchestrator is orchestrator and self._orig:
            return
        self._orchestrator = orchestrator
        for name in ("handle_command", "handle_command_stream", "handle_ui_event", "handle_daemon_result"):
            if hasattr(orchestrator, name):
                original = getattr(orchestrator, name)
                self._orig[name] = original
                setattr(orchestrator, name, self._wrap_call(name, original))
        logger.info("Supervisor wrapped orchestrator (%s)", ", ".join(self._orig))

    def unwrap(self) -> None:
        if not self._orchestrator:
            return
        for name, original in self._orig.items():
            setattr(self._orchestrator, name, original)
        self._orig.clear()
        self._orchestrator = None

    # ── Core dispatch ────────────────────────────────────────────

    def _wrap_call(self, kind: str, original: Callable):
        async def wrapped(*args, **kwargs):
            session_id = kwargs.get("session_id") or (args[0] if args else "")
            text = kwargs.get("text") or (args[1] if len(args) > 1 else "")
            context = kwargs.get("context") or (args[2] if len(args) > 2 else {}) or {}
            source = context.get("source") if isinstance(context, dict) else ""
            source = source or "web"
            actor = context.get("actor") if isinstance(context, dict) else "user"
            actor = actor or "user"

            event = SupervisorEvent(
                event_id=str(uuid4()),
                ts=time.time(),
                source=source,
                kind=kind,
                session_id=str(session_id or ""),
                actor=actor,
                payload_hash=_hash_payload(text),
                payload_summary=_summarise(text),
                decision="allowed",
                latency_ms=0,
                detail={"args": len(args), "kwargs": sorted(kwargs.keys())},
            )

            if self._paused:
                event.decision = "denied"
                event.detail["reason"] = "supervisor_paused"
                self._record(event)
                # WS6 — surface as a structured ``refusal`` WS frame
                # rather than raising into the WS handler (which used
                # to render as a 500 / opaque error toast).
                await self._emit_refusal(
                    session_id=str(session_id or ""),
                    kind=kind,
                    reason="supervisor_paused",
                    retry_hint=(
                        "FERAL's supervisor is paused. Resume in "
                        "Settings → Oversight, then send the request again."
                    ),
                )
                return None

            if self.policy_gate is not None:
                try:
                    verdict = self.policy_gate(event) or "allowed"
                except Exception as exc:
                    logger.exception("policy_gate raised: %s", exc)
                    verdict = "allowed"
                event.decision = verdict
                if verdict == "denied":
                    event.detail["reason"] = "policy_denied"
                    self._record(event)
                    # WS6 — structured refusal instead of raise.
                    await self._emit_refusal(
                        session_id=str(session_id or ""),
                        kind=kind,
                        reason="policy_denied",
                        retry_hint=(
                            "The current autonomy policy declined this "
                            "action. Lower autonomy_mode requirements in "
                            "Settings → Autonomy if you'd like to allow it."
                        ),
                    )
                    return None
                if verdict == "queued":
                    event.detail["reason"] = "policy_queued"
                    self._record(event)
                    return {"queued": True, "event_id": event.event_id}

            started = time.monotonic()
            try:
                result = await original(*args, **kwargs)
                return result
            except Exception as exc:
                event.decision = "error"
                event.detail["error"] = str(exc)[:200]
                raise
            finally:
                event.latency_ms = int((time.monotonic() - started) * 1000)
                self._record(event)

        return wrapped

    async def _emit_refusal(
        self,
        *,
        session_id: str,
        kind: str,
        reason: str,
        retry_hint: str = "",
    ) -> None:
        """Send a structured ``refusal`` WS frame to the session.

        Lane 08 WS6 — supervisor denials surface as a renderable
        chip in chat, not a 500 from the WS handler. Best-effort:
        we never raise from here, the orchestrator's lifecycle is
        already finished as far as the user is concerned.
        """
        orch = self._orchestrator
        if orch is None or not session_id:
            return
        try:
            # Lazy import — supervisor.py is owned by Lane 08 and
            # ``models.protocol`` is the public wire schema. The
            # local import keeps the module-level imports free of a
            # protocol dependency.
            from models.protocol import FeralMessage, RefusalPayload
        except Exception:
            logger.debug("RefusalPayload import failed", exc_info=True)
            return
        try:
            payload = RefusalPayload(
                reason=reason,
                retry_hint=retry_hint,
                source="supervisor",
                kind=kind,
            ).model_dump()
            msg = FeralMessage(
                session_id=session_id, hop="brain", type="refusal", payload=payload,
            )
            send = getattr(orch, "send", None)
            if send is None:
                return
            await send(session_id, msg)
        except Exception:
            logger.debug("refusal emit failed", exc_info=True)

    def _record(self, event: SupervisorEvent) -> None:
        try:
            self.store.insert(event)
        except Exception as exc:
            logger.exception("Supervisor audit insert failed: %s", exc)

        self._recent.append(event.to_dict())
        if len(self._recent) > self._recent_cap:
            del self._recent[: len(self._recent) - self._recent_cap]

        if self.broadcaster is not None:
            try:
                coro = self.broadcaster({
                    "type": "supervisor_event",
                    "payload": event.to_dict(),
                })
                if asyncio.iscoroutine(coro):
                    asyncio.create_task(coro)
            except Exception as exc:
                logger.debug("Broadcast supervisor_event failed: %s", exc)

    # ── Observability ────────────────────────────────────────────

    def recent(self, limit: int = 50, **filters) -> list[dict]:
        return self.store.recent(limit=limit, **filters)

    def stats(self) -> dict:
        info = self.store.stats()
        info["paused"] = self._paused
        info["wrapped"] = list(self._orig.keys())
        return info

    # ── Explicit event recording (for non-orchestrator sources) ──

    def record(
        self,
        *,
        source: str,
        kind: str,
        session_id: str = "",
        actor: str = "system",
        payload: Any = None,
        decision: str = "allowed",
        detail: Optional[dict] = None,
    ) -> SupervisorEvent:
        """Record an event that doesn't flow through the orchestrator.

        Proactive alerts, twin actions, cron triggers, channel ingress —
        anything that still wants the oversight seat's audit can call
        ``state.supervisor.record(...)``.
        """
        event = SupervisorEvent(
            event_id=str(uuid4()),
            ts=time.time(),
            source=source,
            kind=kind,
            session_id=session_id,
            actor=actor,
            payload_hash=_hash_payload(payload),
            payload_summary=_summarise(payload),
            decision=decision,
            latency_ms=0,
            detail=detail or {},
        )
        self._record(event)
        return event


class SupervisorBlocked(RuntimeError):
    """Raised when the supervisor gates an event off."""
