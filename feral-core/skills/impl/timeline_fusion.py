"""Fused-timeline builder for the S1 thesis scenario.

Closes the v1.0 cut-list item #8: when the LLM (or heuristic router)
decides the user asked a temporal-recall question — "what did I do
yesterday?", "summarize my morning", "last Tuesday afternoon" — the
orchestrator dispatches ``notes_memory__fused_timeline`` and this
module fans out across every configured memory/context source,
filters by the parsed temporal window, dedupes, caps per-source at
``per_source_limit`` (default 50), and returns a single structured
result.

The shape is the WS-frame contract from ``models/protocol.py``::

    {
        "entries": [
            {"type", "timestamp", "title", "content",
             "source", "metadata"},
            ...
        ],
        "summary": "",                    # filled by LLM, not here
        "window": {"from": iso, "to": iso, "label": "yesterday"},
        "sources_queried": ["episode", "note", "knowledge", ...],
        "degraded_sources": [
            {"source": "calendar", "reason": "no_token"},
            {"source": "health",   "reason": "no_provider"},
        ],
    }

The orchestrator forwards the result verbatim into a ``timeline``
``FeralMessage`` so the WebUI can mount a ``TimelineCard`` while the
LLM continues to stream prose in parallel.

Design notes for v1.0:

- **Episodes** + **notes** are always queried (free — same SQLite
  pool the rest of memory uses, with the same forgotten/decay
  filters).
- **Knowledge graph** triples are best-effort (some store backends
  don't expose ``recent_triples``; the source is degraded with
  ``no_kg_query`` rather than failing the whole fusion).
- **Calendar** is degraded with ``no_token`` when the integration
  isn't wired (no Google Calendar OAuth + no ICS URL). The shipped
  ``CalendarIntegration.list_events`` is forward-only
  (``days_ahead``) — past-window queries return [] today; the
  fusion still attempts the call so a wired calendar with same-day
  events does surface. A backward-looking variant is a follow-up.
- **Health** (Whoop/Oura via ``HealthAggregator``) is degraded
  with ``no_provider`` when no provider is configured. The sleep
  trend is window-filtered same as everything else.
- **ScreenLoop** frames are degraded with ``no_query_api`` — the
  current ``perception/screen_loop.py`` buffer doesn't expose a
  range query (only "latest N"). Listing the degraded source keeps
  the WebUI's "Screen activity unavailable" chip honest until the
  query API lands.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, time as dtime, timedelta
from typing import Any, Optional

logger = logging.getLogger("feral.skill.timeline_fusion")

# Per-source entry cap — keeps the TimelineCard renderable even if
# the user asks "what did I do this year?" and 10k episodes match.
DEFAULT_PER_SOURCE_LIMIT = 50

# How many episodes we pull from memory before window-filtering. The
# brain's episode count can be 10k+ on long-running installs; reading
# *all* of them and filtering in Python would be expensive. We pull
# a generous 1000 newest and rely on the chronological window doing
# the heavy lifting. For "yesterday" / "today" / "last week" this is
# always enough headroom; longer windows ("last month") will silently
# clip the oldest entries, which we accept for v1.0.
EPISODE_PULL_LIMIT = 1000
NOTE_PULL_LIMIT = 500


# ─────────────────────────────────────────────────────────────────────
# Window parsing — natural-language → (from_ts, to_ts, label)
# ─────────────────────────────────────────────────────────────────────


def _local_midnight(d: datetime) -> datetime:
    return datetime.combine(d.date(), dtime.min)


def parse_window(
    label: Optional[str] = None,
    *,
    from_ts: Optional[float] = None,
    to_ts: Optional[float] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Resolve a natural-language window label into an absolute range.

    Explicit ``from_ts`` / ``to_ts`` always win; ``label`` is only
    consulted when both are missing. Unknown labels collapse to
    "yesterday" — the thesis-default — so a typo in the LLM's tool
    call still returns a sensible card instead of an error.

    Returns ``{"from": iso, "to": iso, "from_ts": float, "to_ts":
    float, "label": str}``. The ISO strings are local time without
    timezone suffix because the TimelineCard renders to the user's
    locale anyway.
    """
    now = now or datetime.now()

    if from_ts is not None and to_ts is not None:
        f = datetime.fromtimestamp(float(from_ts))
        t = datetime.fromtimestamp(float(to_ts))
        return {
            "from": f.isoformat(timespec="seconds"),
            "to": t.isoformat(timespec="seconds"),
            "from_ts": float(from_ts),
            "to_ts": float(to_ts),
            "label": label or "custom",
        }

    label_norm = (label or "yesterday").strip().lower().replace(" ", "_")

    today_midnight = _local_midnight(now)
    yesterday_midnight = today_midnight - timedelta(days=1)

    if label_norm in ("today",):
        f, t = today_midnight, now
    elif label_norm in ("morning", "this_morning"):
        f = today_midnight + timedelta(hours=6)
        t = today_midnight + timedelta(hours=12)
    elif label_norm in ("afternoon", "this_afternoon"):
        f = today_midnight + timedelta(hours=12)
        t = today_midnight + timedelta(hours=18)
    elif label_norm in ("evening", "tonight", "this_evening"):
        f = today_midnight + timedelta(hours=18)
        t = today_midnight + timedelta(hours=23, minutes=59)
    elif label_norm in ("last_week", "past_week", "this_week"):
        f = today_midnight - timedelta(days=7)
        t = now
    elif label_norm in ("last_month", "past_month"):
        f = today_midnight - timedelta(days=30)
        t = now
    elif label_norm.startswith("last_"):
        # last_monday, last_tuesday, … resolved to the most recent
        # occurrence strictly before today.
        weekday_names = [
            "monday", "tuesday", "wednesday", "thursday",
            "friday", "saturday", "sunday",
        ]
        target = label_norm[len("last_"):]
        if target in weekday_names:
            target_idx = weekday_names.index(target)
            delta_days = (now.weekday() - target_idx) % 7
            if delta_days == 0:
                delta_days = 7
            f = today_midnight - timedelta(days=delta_days)
            t = f + timedelta(days=1)
        else:
            f, t = yesterday_midnight, today_midnight
            label_norm = "yesterday"
    else:
        # default = yesterday (covers "yesterday" + every unknown label)
        f, t = yesterday_midnight, today_midnight
        label_norm = "yesterday"

    return {
        "from": f.isoformat(timespec="seconds"),
        "to": t.isoformat(timespec="seconds"),
        "from_ts": f.timestamp(),
        "to_ts": t.timestamp(),
        "label": label_norm,
    }


# ─────────────────────────────────────────────────────────────────────
# Per-source fetchers (each is defensive — never raises)
# ─────────────────────────────────────────────────────────────────────


async def _fetch_episodes(memory, from_ts: float, to_ts: float, limit: int) -> tuple[list[dict], Optional[str]]:
    if memory is None or not hasattr(memory, "episode_recent"):
        return [], "no_memory"
    try:
        rows = await memory.episode_recent(limit=EPISODE_PULL_LIMIT)
    except Exception as exc:
        logger.warning("timeline_fusion: episode_recent failed: %s", exc)
        return [], f"episode_query_failed: {exc.__class__.__name__}"

    entries: list[dict] = []
    for ep in rows:
        ts = ep.get("created_at") or ep.get("timestamp") or 0
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts).timestamp()
            except Exception:
                ts = 0
        ts = float(ts or 0)
        if ts < from_ts or ts > to_ts:
            continue
        summary = (ep.get("summary") or "").strip()
        user_msg = (ep.get("user_message") or "").strip()
        assistant_msg = (ep.get("assistant_message") or "").strip()
        title = summary or (user_msg[:80] if user_msg else "Episode")
        content = assistant_msg or user_msg or summary or ""
        entries.append({
            "type": "episode",
            "source": "episode",
            "timestamp": ts,
            "title": title,
            "content": content[:500],
            "metadata": {
                "id": ep.get("id"),
                "session_id": ep.get("session_id"),
                "tags": ep.get("tags") or [],
            },
        })
        if len(entries) >= limit:
            break
    return entries, None


async def _fetch_notes(memory, from_ts: float, to_ts: float, limit: int) -> tuple[list[dict], Optional[str]]:
    if memory is None or not hasattr(memory, "list_recent"):
        return [], "no_memory"
    try:
        rows = await memory.list_recent(limit=NOTE_PULL_LIMIT)
    except Exception as exc:
        logger.warning("timeline_fusion: list_recent failed: %s", exc)
        return [], f"note_query_failed: {exc.__class__.__name__}"

    entries: list[dict] = []
    for note in rows:
        ts = note.get("created_at") or 0
        try:
            ts = float(ts or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts < from_ts or ts > to_ts:
            continue
        content = (note.get("content") or "").strip()
        entries.append({
            "type": "note",
            "source": "note",
            "timestamp": ts,
            "title": content[:80] or "Note",
            "content": content[:500],
            "metadata": {
                "id": note.get("id"),
                "tags": note.get("tags") or [],
                "importance": note.get("importance"),
            },
        })
        if len(entries) >= limit:
            break
    return entries, None


async def _fetch_knowledge(memory, from_ts: float, to_ts: float, limit: int) -> tuple[list[dict], Optional[str]]:
    if memory is None:
        return [], "no_memory"
    # Try the unified-KG recent-triples API first; older stores expose
    # ``knowledge_recent`` and the unified store uses ``recent_triples``.
    fn = (
        getattr(memory, "recent_triples", None)
        or getattr(memory, "knowledge_recent", None)
    )
    if fn is None:
        return [], "no_kg_query"
    try:
        rows = await fn(limit=limit * 2)
    except Exception as exc:
        logger.warning("timeline_fusion: knowledge fetch failed: %s", exc)
        return [], f"knowledge_query_failed: {exc.__class__.__name__}"

    entries: list[dict] = []
    for t in rows or []:
        ts = t.get("created_at") or t.get("timestamp") or 0
        try:
            ts = float(ts or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts < from_ts or ts > to_ts:
            continue
        subj = t.get("subject") or t.get("subj") or ""
        pred = t.get("predicate") or t.get("pred") or ""
        obj = t.get("object") or t.get("obj") or ""
        triple_text = f"{subj} {pred} {obj}".strip()
        entries.append({
            "type": "knowledge",
            "source": "knowledge",
            "timestamp": ts,
            "title": triple_text[:80] or "Triple",
            "content": triple_text[:500],
            "metadata": {
                "subject": subj,
                "predicate": pred,
                "object": obj,
                "source_ref": t.get("source"),
            },
        })
        if len(entries) >= limit:
            break
    return entries, None


async def _fetch_calendar(calendar, from_ts: float, to_ts: float, limit: int) -> tuple[list[dict], Optional[str]]:
    if calendar is None:
        return [], "no_token"
    try:
        # CalendarIntegration.list_events is forward-only today
        # (`days_ahead`). We attempt the call anyway: if the user
        # asked for a today/this-week window, same-day events still
        # land. A backward-looking range query is a follow-up.
        days_ahead = max(1, int((to_ts - time.time()) / 86400) + 1)
        raw = await calendar.execute("list_events", {"days_ahead": days_ahead})
    except Exception as exc:
        logger.warning("timeline_fusion: calendar list_events failed: %s", exc)
        return [], f"calendar_query_failed: {exc.__class__.__name__}"

    if not isinstance(raw, dict):
        return [], "calendar_bad_response"
    if not raw.get("success", True):
        return [], raw.get("error", "calendar_call_failed")[:64]
    data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    events_list = data.get("events") or [] if isinstance(data, dict) else []

    entries: list[dict] = []
    for ev in events_list:
        ts = ev.get("start_epoch")
        if ts is None:
            # CalendarIntegration sometimes returns ISO-strings only.
            start = ev.get("start") or ""
            try:
                ts = datetime.fromisoformat(str(start).replace("Z", "+00:00")).timestamp()
            except Exception:
                ts = 0
        try:
            ts = float(ts or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts and (ts < from_ts or ts > to_ts):
            continue
        entries.append({
            "type": "event",
            "source": "calendar",
            "timestamp": ts,
            "title": ev.get("summary") or ev.get("title") or "Event",
            "content": ev.get("description") or "",
            "metadata": {
                "location": ev.get("location"),
                "id": ev.get("id"),
                "html_link": ev.get("html_link"),
            },
        })
        if len(entries) >= limit:
            break
    return entries, None


async def _fetch_health(health_aggregator, from_ts: float, to_ts: float, limit: int) -> tuple[list[dict], Optional[str]]:
    if health_aggregator is None:
        return [], "no_provider"
    try:
        days = max(1, int((to_ts - from_ts) / 86400) + 1)
        trend = await health_aggregator.get_sleep_trend(days=days)
    except Exception as exc:
        logger.warning("timeline_fusion: health get_sleep_trend failed: %s", exc)
        return [], f"health_query_failed: {exc.__class__.__name__}"

    entries: list[dict] = []
    for row in trend if isinstance(trend, list) else []:
        ts = row.get("timestamp", 0)
        try:
            ts = float(ts or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts and (ts < from_ts or ts > to_ts):
            continue
        hours = row.get("hours")
        quality = row.get("quality")
        entries.append({
            "type": "health",
            "source": "health",
            "timestamp": ts,
            "title": "Sleep",
            "content": f"{hours}h sleep" + (f", quality {quality}" if quality is not None else ""),
            "metadata": row,
        })
        if len(entries) >= limit:
            break
    return entries, None


# ─────────────────────────────────────────────────────────────────────
# Public entry point — used by NotesMemorySkill and tests
# ─────────────────────────────────────────────────────────────────────


async def timeline_fusion(
    *,
    query: str,
    memory=None,
    calendar=None,
    health_aggregator=None,
    window_label: Optional[str] = None,
    from_ts: Optional[float] = None,
    to_ts: Optional[float] = None,
    per_source_limit: int = DEFAULT_PER_SOURCE_LIMIT,
    now: Optional[datetime] = None,
) -> dict:
    """Fuse every available memory/context source into one chronological
    timeline for the given window. See module docstring for the
    contract."""
    window = parse_window(
        window_label, from_ts=from_ts, to_ts=to_ts, now=now,
    )
    f_ts, t_ts = window["from_ts"], window["to_ts"]
    limit = max(1, int(per_source_limit or DEFAULT_PER_SOURCE_LIMIT))

    sources_queried: list[str] = []
    degraded_sources: list[dict] = []
    all_entries: list[dict] = []

    for label, coro in (
        ("episode", _fetch_episodes(memory, f_ts, t_ts, limit)),
        ("note", _fetch_notes(memory, f_ts, t_ts, limit)),
        ("knowledge", _fetch_knowledge(memory, f_ts, t_ts, limit)),
        ("calendar", _fetch_calendar(calendar, f_ts, t_ts, limit)),
        ("health", _fetch_health(health_aggregator, f_ts, t_ts, limit)),
    ):
        sources_queried.append(label)
        entries, reason = await coro
        if reason:
            degraded_sources.append({"source": label, "reason": reason})
        all_entries.extend(entries)

    # ScreenLoop frames don't expose a range-query API today —
    # surface the source as degraded so the UI keeps an honest chip
    # rather than a silently missing section.
    sources_queried.append("screen_loop")
    degraded_sources.append({"source": "screen_loop", "reason": "no_query_api"})

    # Dedup: ``episode`` rows that originate from the same conversation
    # can land twice if the orchestrator wrote both a summary + a
    # raw turn. Keep the first occurrence per (source, id).
    seen: set[tuple[str, Any]] = set()
    deduped: list[dict] = []
    for e in all_entries:
        key = (e.get("source", ""), e.get("metadata", {}).get("id"))
        if key[1] is not None and key in seen:
            continue
        seen.add(key)
        deduped.append(e)

    deduped.sort(key=lambda e: e.get("timestamp", 0))

    return {
        "entries": deduped,
        "summary": "",
        "window": {
            "from": window["from"],
            "to": window["to"],
            "label": window["label"],
        },
        "sources_queried": sources_queried,
        "degraded_sources": degraded_sources,
        "query": query,
    }
