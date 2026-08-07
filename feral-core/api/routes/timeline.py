"""
FERAL Timeline API — Chronological life view
"""
from __future__ import annotations

import logging
import os
import time
from fastapi import APIRouter, HTTPException, Query

from api.state import state

router = APIRouter()

_push_logger = logging.getLogger("feral.api.push")


# RC polish: the WebUI shipped a filter dropdown whose values
# (``memory``, ``calendar``, ``chat``) didn't match the gates this
# route checks (``memories``, ``events``, ``health``). Non-"All"
# picks therefore returned an empty feed. The canonical filter names
# are ``all`` / ``memories`` / ``events`` / ``health`` / ``chat``;
# the legacy WebUI vocabulary still works through this alias map so
# operators running mismatched client/brain versions don't see a
# silently-empty timeline.
_TYPE_ALIASES = {
    "memory": "memories",
    "calendar": "events",
}


@router.get("/api/timeline")
async def get_timeline(
    days: int = Query(7, ge=1, le=90),
    type: str = Query("all"),
):
    """Return a chronological feed of memories, health, screen, and events.

    Canonical ``type`` values: ``all`` | ``memories`` | ``events`` |
    ``health`` | ``chat`` (``memory``/``calendar`` are accepted as
    legacy aliases for ``memories``/``events``).
    """
    entries = []
    since_ts = time.time() - (days * 86400)
    type = _TYPE_ALIASES.get(type, type)

    # ``chat`` is a UI-friendly label for the episode source — the
    # brain's chat turns are stored as episodes — so route it through
    # the same gate as ``memories``.
    if type in ("all", "memories", "chat"):
        # AUDIT-r14 round2 wave3-followup-001: pre-fix this called
        # ``state.memory.search("", limit=100)`` which is the legacy
        # *notes* search API. With 0 notes (the canonical case for a
        # brain whose user hasn't run ``feral memory save`` explicitly)
        # the timeline reported 0 entries even though the brain held
        # thousands of *episodes* — exactly the scenario the v2026.5.40
        # live verify caught (4601 episodes on disk, /api/timeline
        # returned []). The chronological life view's canonical source
        # is ``episode_recent`` (memory/store.py:1318), which orders
        # by ``created_at DESC`` and honors the same forgotten/decay
        # filter as the rest of the episode read paths.
        try:
            episodes = await state.memory.episode_recent(limit=200)
            for ep in episodes:
                ts = ep.get("created_at") or ep.get("timestamp", 0)
                if isinstance(ts, str):
                    try:
                        from datetime import datetime
                        ts = datetime.fromisoformat(ts).timestamp()
                    except Exception:
                        ts = 0
                if ts >= since_ts:
                    # Episode rows expose ``user_message`` /
                    # ``assistant_message`` / ``summary`` depending on
                    # how they were written. The timeline card needs
                    # one short title + a slightly longer body; pick
                    # the most informative non-empty pair without
                    # loading raw fields the UI doesn't render.
                    summary = ep.get("summary") or ""
                    user_msg = ep.get("user_message") or ""
                    assistant_msg = ep.get("assistant_message") or ""
                    title = (
                        summary.strip()
                        or (user_msg[:80].strip() if user_msg else "")
                        or "Episode"
                    )
                    content = assistant_msg or user_msg or summary or ""
                    entries.append({
                        "type": "memory",
                        "timestamp": ts,
                        "title": title,
                        "content": content,
                        "metadata": {
                            "id": ep.get("id"),
                            "session_id": ep.get("session_id"),
                            "tags": ep.get("tags", []),
                        },
                    })
        except Exception as exc:
            # Never let the read fail silently — surface a single
            # error entry into the response so the timeline UI can
            # render an honest "memory unavailable" row instead of
            # an empty page.
            entries.append({
                "type": "memory_error",
                "timestamp": time.time(),
                "title": "Memory unavailable",
                "content": f"{exc.__class__.__name__}: {exc}",
                "metadata": {},
            })

    if type in ("all", "health") and state.health_aggregator:
        try:
            trend = await state.health_aggregator.get_sleep_trend(days=days)
            for entry in (trend if isinstance(trend, list) else []):
                entries.append({
                    "type": "health",
                    "timestamp": entry.get("timestamp", time.time()),
                    "title": "Sleep",
                    "content": f"{entry.get('hours', '?')}h sleep, quality {entry.get('quality', '?')}",
                    "metadata": entry,
                })
        except Exception:
            pass

    if type in ("all", "events") and state.calendar:
        try:
            raw = await state.calendar.execute("list_events", {"days_ahead": days})
            # Audit-r9: `CalendarIntegration.list_events` returns
            # `{"success": True, "data": {"events": [...]}}`. The
            # original `events.get("events", [])` was reading the
            # outer dict's `events` key (which doesn't exist) and
            # silently returning [], so the timeline UI's "events"
            # filter was always empty even with a valid calendar.
            data = raw.get("data") if isinstance(raw, dict) else None
            events_list = (
                data.get("events", []) if isinstance(data, dict)
                else (raw.get("events", []) if isinstance(raw, dict) else [])
            )
            for ev in events_list:
                entries.append({
                    "type": "event",
                    "timestamp": ev.get("start_epoch", time.time()),
                    "title": ev.get("summary") or ev.get("title", "Event"),
                    "content": ev.get("description", ""),
                    "metadata": ev,
                })
        except Exception as exc:
            # Surface the failure into the entries list so the UI can
            # show "Calendar unavailable" instead of silently empty.
            # `type` is shadowed by the route's filter parameter, so
            # explicit `__class__.__name__` for the exception class.
            entries.append({
                "type": "event_error",
                "timestamp": time.time(),
                "title": "Calendar unavailable",
                "content": f"{exc.__class__.__name__}: {exc}",
                "metadata": {},
            })

    entries.sort(key=lambda e: e.get("timestamp", 0))
    return {"entries": entries, "count": len(entries), "days": days}


@router.get("/api/digital-twin/ask")
async def digital_twin_ask(question: str = Query(...)):
    """Ask the digital twin a question."""
    if not state.digital_twin:
        return {"error": "Digital twin not initialized", "answer": ""}
    try:
        answer = await state.digital_twin.ask(question)
        return {"answer": answer, "question": question}
    except Exception as e:
        return {"error": str(e), "answer": ""}


@router.get("/api/automations")
async def list_automations():
    """List user-created automations."""
    try:
        jobs = state.scheduler.list_automations()
        return {"automations": [
            {
                "id": j.id,
                "description": j.description,
                "cron": j.cron_expr,
                "enabled": j.enabled,
                "next_run": j.next_run,
                "run_count": j.run_count,
            }
            for j in jobs
        ]}
    except Exception as e:
        return {"automations": [], "error": str(e)}


@router.post("/api/automations")
async def create_automation(body: dict):
    """Create an automation from natural language."""
    text = body.get("text", "")
    session_id = body.get("session_id", "web")
    if not text:
        return {"error": "text is required"}
    try:
        job = state.scheduler.create_from_natural_language(text, session_id)
        return {"success": True, "job_id": job.id, "cron": job.cron_expr, "description": job.description}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.delete("/api/automations/{job_id}")
async def delete_automation(job_id: int):
    """Delete a user automation."""
    try:
        state.scheduler.delete_automation(job_id)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/api/health-summary")
async def health_summary():
    """Aggregated health data from all connected platforms."""
    if not state.health_aggregator:
        return {"error": "No health platforms connected", "data": {}}
    try:
        data = await state.health_aggregator.get_health_summary()
        return {"data": data}
    except Exception as e:
        return {"error": str(e), "data": {}}


@router.post("/api/location/update")
async def update_location(body: dict):
    """Update the user's location.

    Source can be anything (``browser``, ``phone``, ``wristband`` …). We
    no longer default to ``"phone"`` — honest source labels or
    ``"unknown"`` when the client doesn't say.
    """
    if not state.location_engine:
        return {"error": "Location engine not initialized"}
    lat = body.get("lat")
    lon = body.get("lon")
    if lon is None:
        lon = body.get("lng")
    if lat is None or lon is None:
        return {"error": "lat and lon (or lng) required"}
    try:
        triggered = state.location_engine.update_location(
            lat, lon, source=body.get("source", "unknown"),
        )
        return {"success": True, "triggered_fences": triggered}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/api/geofences")
async def add_geofence(body: dict):
    """Add a geofence.

    Accepts either the canonical ``{lat, lon, radius_m}`` body or the
    v2 client shape ``{lat, lng, radius}`` — the two-letter field names
    drifted between the UI and the engine and this route normalises both.
    """
    if not state.location_engine:
        raise HTTPException(status_code=503, detail="Location engine not initialized")
    try:
        name = body["name"]
        lat = body["lat"]
        lon = body.get("lon", body.get("lng"))
        if lon is None:
            raise HTTPException(status_code=400, detail="lat and lon (or lng) required")
        radius_m = body.get("radius_m", body.get("radius", 200))
        state.location_engine.add_geofence(
            name=name,
            lat=lat,
            lon=lon,
            radius_m=radius_m,
            on_enter=body.get("on_enter", ""),
            on_exit=body.get("on_exit", ""),
        )
        return {
            "success": True,
            "geofence": {
                "id": name,
                "name": name,
                "lat": lat,
                "lng": lon,
                "lon": lon,
                "radius": radius_m,
                "radius_m": radius_m,
            },
        }
    except HTTPException:
        raise
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=f"missing field: {exc}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/geofences")
async def list_geofences():
    """List all geofences. Returns both lat/lon and lat/lng for client parity."""
    if not state.location_engine:
        return {"geofences": [], "fences": []}
    try:
        fences = state.location_engine.list_geofences()
        rows = [
            {
                "id": f.name,
                "name": f.name,
                "lat": f.center.lat,
                "lng": f.center.lon,
                "lon": f.center.lon,
                "radius": f.radius_m,
                "radius_m": f.radius_m,
                "on_enter": getattr(f, "on_enter", "") or "",
                "on_exit": getattr(f, "on_exit", "") or "",
            }
            for f in fences
        ]
        # Keep the legacy `fences` key so any old client (and the server-side
        # snapshot at test_perception_deep.py) doesn't break.
        return {"geofences": rows, "fences": rows}
    except Exception as e:
        return {"geofences": [], "fences": [], "error": str(e)}


@router.delete("/api/geofences/{fence_id}")
async def delete_geofence(fence_id: str):
    """Delete a geofence by its id (aka its unique name).

    The v2 Geofences page calls this with ``f.id`` — we route that through
    ``LocationEngine.remove_geofence(name)`` which is the canonical
    delete primitive.
    """
    if not state.location_engine:
        raise HTTPException(status_code=503, detail="Location engine not initialized")
    try:
        removed = state.location_engine.remove_geofence(fence_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not removed:
        raise HTTPException(status_code=404, detail=f"geofence {fence_id!r} not found")
    return {"success": True, "id": fence_id}


@router.post("/api/push/register")
async def register_push_device(body: dict):
    """Register a device for push notifications."""
    if not state.push_channel:
        return {"error": "Push channel not initialized"}
    try:
        registered = state.push_channel.register_device(
            user_id=body.get("user_id", "default"),
            token=body["token"],
            platform=body.get("platform", "fcm"),
        )
        # Echo the stored platform back. The channel normalizes aliases
        # ("ios" -> "apns"), and a client that posted an unrecognized value
        # was silently filed under fcm with no way to notice.
        return {"success": True, "platform": registered.get("platform")}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/api/push/send")
async def send_push(body: dict):
    """Send a push notification to every device registered for a user.

    Returns ``{success, sent, failed, degraded, results}``. ``degraded`` is
    a list of human-readable reasons the send did not fully land, following
    the same convention as /api/ambient/briefing and /api/ideas/refresh.

    This endpoint has never once succeeded: it awaited ``PushChannel
    .broadcast``, which was a plain ``def`` returning ``list[dict]``, so
    every call raised ``TypeError: object list can't be used in 'await'
    expression`` and was swallowed by the except below into a generic
    ``{"success": False}``. ``broadcast`` is now a coroutine that offloads
    its blocking httpx calls to a worker thread. The old response also
    returned the raw per-device list, in which "no devices registered" and
    "delivered to 3 devices" were both just a JSON array, so a caller could
    not tell delivery from silence.
    """
    if not state.push_channel:
        return {
            "success": False,
            "sent": 0,
            "failed": 0,
            "degraded": ["push channel not initialized"],
            "results": [],
            "error": "Push channel not initialized",
        }

    user_id = body.get("user_id", "default")
    degraded: list[str] = []

    creds = state.push_channel.credentials_status()
    if not creds.get("any"):
        # Say it up front. Without credentials every per-device result would
        # come back with its own opaque error and the operator would be left
        # guessing whether the tokens or the config were at fault.
        degraded.append(
            "no push credentials configured "
            "(set FERAL_FIREBASE_CREDENTIALS for Android and/or FERAL_APNS_KEY_PATH for iOS)"
        )

    try:
        results = await state.push_channel.broadcast(
            user_id=user_id,
            title=body.get("title", "FERAL"),
            body=body.get("body", ""),
            data=body.get("data"),
        )
    except Exception as e:
        _push_logger.warning("push broadcast for user=%s raised: %s", user_id, e)
        return {
            "success": False,
            "sent": 0,
            "failed": 0,
            "degraded": degraded + [f"{type(e).__name__}: {e}"],
            "results": [],
            "error": str(e),
        }

    sent = sum(1 for r in results if r.get("success"))
    failed = len(results) - sent

    if not results:
        degraded.append(f"no devices registered for user_id={user_id!r}")
    for r in results:
        if not r.get("success"):
            degraded.append(
                f"{r.get('platform', 'unknown')} device "
                f"…{r.get('token_suffix', '??????')}: {r.get('error', 'unknown error')}"
            )

    if degraded:
        _push_logger.warning(
            "push send for user=%s delivered %d/%d: %s",
            user_id, sent, len(results), "; ".join(degraded),
        )

    return {
        # Nothing delivered is not a success, whatever the reason was.
        "success": sent > 0,
        "sent": sent,
        "failed": failed,
        "degraded": degraded,
        "results": results,
    }


@router.get("/api/autonomy")
async def get_autonomy():
    """Get current autonomy mode."""
    orch = state.orchestrator
    if orch and hasattr(orch, "tool_runner"):
        return {"mode": orch.tool_runner.autonomy_mode}
    return {"mode": "hybrid"}


@router.post("/api/autonomy")
async def set_autonomy(body: dict):
    """Set autonomy mode (strict/hybrid/loose).

    v2026.5.26 also persists the choice to ``~/.feral/settings.json``
    under ``security.autonomy_mode`` so the chosen tier survives brain
    restart. Pre-fix this endpoint only mutated the in-memory
    ``ToolRunner._autonomy_mode``; on next ``feral start`` the value
    reverted to ``hybrid`` (or whatever ``FERAL_AUTONOMY`` env var
    pinned). Operator's WebUI Settings -> Autonomy pick was effectively
    a no-op across sessions.

    The tier gates two independent things and they used to move apart.
    ``ToolRunner`` holds its own ``_autonomy_mode`` and gates tool
    approvals from it. ``security/exec_mode.current_autonomy_mode()``
    gates the shell path and reads ``FERAL_AUTONOMY`` and nothing else.
    Flipping only the first meant an operator who moved to ``strict``
    got a stricter approval gate immediately and the old, looser shell
    gate until the next restart re-exported the setting. So the env var
    is rewritten here too, before the disk write, because the shell gate
    must not lag the approval gate even when persistence fails.
    """
    mode = body.get("mode", "hybrid")
    if mode not in ("strict", "hybrid", "loose"):
        return {"error": "mode must be strict, hybrid, or loose"}
    orch = state.orchestrator
    if not (orch and hasattr(orch, "tool_runner")):
        return {"error": "Orchestrator not ready"}

    # Live-update the running process so the next tool call respects
    # the new tier without waiting for a restart.
    orch.tool_runner.set_autonomy_mode(mode)

    # Second gate, same tier. Unconditional and ahead of the persist:
    # ``ConfigLoader.update_settings`` now re-exports this too, but the
    # route must still move it when ``state.config`` is absent or the
    # disk write throws.
    os.environ["FERAL_AUTONOMY"] = mode

    # Persist so restarts honour the operator's choice. Best-effort:
    # the in-memory flip ABOVE already succeeded; a disk-write failure
    # shouldn't roll back the live state, just log truthfully.
    persisted = False
    try:
        if state.config and hasattr(state.config, "update_settings"):
            state.config.update_settings("security", "autonomy_mode", mode)
            persisted = True
    except Exception as exc:
        import logging
        logging.getLogger("feral.api.autonomy").warning(
            "set_autonomy: persist to settings.json failed: %s, "
            "live mode is %s but restart will revert", exc, mode,
        )
    return {"success": True, "mode": mode, "persisted": persisted}
