"""Dashboard, system info, health, and activity endpoints."""

import json
import logging
import os
import time
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from version import VERSION as __version__
from models.protocol import stamp_hup_envelope
from api.state import state
from config.loader import feral_home

router = APIRouter()
logger = logging.getLogger("feral.dashboard")


@router.get("/api/identity/greeting")
async def identity_greeting():
    """Personalized greeting for the smart empty state."""
    hour = time.localtime().tm_hour
    if hour < 12:
        tod = "Good morning"
    elif hour < 18:
        tod = "Good afternoon"
    else:
        tod = "Good evening"

    name = ""
    user_path = feral_home() / "USER.md"
    try:
        if user_path.exists():
            content = user_path.read_text()
            for line in content.splitlines():
                if line.strip().startswith("- Name:"):
                    name = line.split(":", 1)[1].strip().split()[0]
                    break
    except Exception:
        pass

    greeting = f"{tod}, {name}." if name else f"{tod}."

    health_summary = ""
    try:
        frames = []
        if state.perception:
            for sid in list(getattr(state.perception, '_frames', {}).keys()):
                f = state.perception.get_frame(sid)
                if f and f.heart_rate > 0:
                    frames.append(f)
        if frames:
            f = frames[0]
            health_summary = f"Heart rate {f.heart_rate} bpm, SpO2 {f.spo2_pct}%."
    except Exception:
        pass

    last_memory = ""
    try:
        recent = await state.memory.episode_recent(limit=1, session_id=None)
        if recent:
            last_memory = (recent[0].get("summary", "") or "")[:120]
    except Exception:
        pass

    return {
        "name": name,
        "greeting": greeting,
        "health_summary": health_summary,
        "last_memory": last_memory,
    }


@router.get("/api/context/live")
async def context_live():
    """Return the brain's current perception context for companion apps.

    Structured snapshot of what the brain "knows" right now — sensors,
    vision, audio, somatic state, plus the formatted system-context string
    that the LLM receives. The iOS Context tab polls this instead of
    showing raw metric cards.
    """
    now = time.time()

    perception_text = "No active sessions."
    sensors = {}
    vision = {}
    somatic = {}

    if state.perception and state.sessions:
        for sid in state.sessions:
            frame = state.perception.get_frame(sid)
            if frame:
                perception_text = frame.to_system_context()
                sensors = {
                    "heart_rate": frame.heart_rate or None,
                    "heart_rate_fresh": (
                        frame.heart_rate > 0
                        and frame.heart_rate_sample_ts > 0
                        and (now - frame.heart_rate_sample_ts) <= 120
                    ),
                    "heart_rate_source": frame.heart_rate_source or None,
                    "spo2": frame.spo2_pct or None,
                    "spo2_fresh": (
                        frame.spo2_pct > 0
                        and frame.spo2_sample_ts > 0
                        and (now - frame.spo2_sample_ts) <= 120
                    ),
                    "temperature_c": frame.skin_temperature_c or None,
                    "activity_state": frame.activity_state if frame.activity_state != "unknown" else None,
                    "battery_pct": frame.battery_pct if frame.battery_pct < 100 else None,
                }
                vision = {
                    "active": frame.has_vision,
                    "scene_description": frame.scene_description or None,
                    "objects": frame.detected_objects[:5] if frame.detected_objects else [],
                    "text": frame.text_in_scene[:3] if frame.text_in_scene else [],
                }
                break

    if hasattr(state, 'somatic_engine') and state.somatic_engine and state.sessions:
        for sid in state.sessions:
            vec = state.somatic_engine.get_vector(sid)
            # This used to report the vector alone: cognitive_load,
            # activity, circadian phase. That is the INPUT to the
            # behavioural policy and none of the OUTPUT, so a dashboard
            # could show a load figure while the thing the load actually
            # did (shorter answers, suppressed proactive messages,
            # restricted tools) stayed invisible. state_frame carries
            # both halves and is the same shape the phone gets on the
            # `somatic_state` HUP frame.
            try:
                somatic = state.somatic_engine.state_frame(sid, reason="poll")
            except Exception:
                somatic = {
                    "cognitive_load": vec.cognitive_load,
                    "activity_level": vec.activity_level,
                    "circadian_phase": vec.circadian_phase,
                }
            else:
                somatic["circadian_phase"] = vec.circadian_phase
            break

    hardware_context = ""
    if state.device_registry:
        hardware_context = state.device_registry.to_llm_context()

    return {
        "perception_text": perception_text,
        "sensors": sensors,
        "vision": vision,
        "somatic": somatic,
        "hardware_context": hardware_context,
        "timestamp": now,
    }


@router.get("/health")
async def health():
    """Health check endpoint for Docker HEALTHCHECK and load balancers."""
    boot_data = state._boot_report.to_dict() if hasattr(state, '_boot_report') else {}
    return {"status": "ok", "version": __version__, "boot": boot_data}


@router.get("/api/boot-report")
async def boot_report():
    """Live boot progress — used by `feral start` for subsystem readouts."""
    if hasattr(state, "_boot_report"):
        return state._boot_report.to_dict()
    return {"current": None, "last": None, "subsystems": [], "summary": {}}


@router.get("/api/info")
async def api_info():
    stats = await state.memory.stats()
    return {
        "name": "FERAL Brain",
        "version": __version__,
        "status": "running",
        "sessions": len(state.sessions),
        "daemons": list(state.daemons.keys()),
        "devices": len(state.devices),
        "skills": len(state.skill_registry.skills),
        "memory": stats,
        "audio_available": state.audio.available,
        "realtime_available": state.realtime_proxy.available if state.realtime_proxy else False,
    }


@router.get("/api/system/info")
async def system_info():
    """Full system info for the dashboard."""
    stats = await state.memory.stats()
    hw_stats = state.device_registry.stats if state.device_registry else {}
    mcp_client_stats = state.mcp_client.stats if state.mcp_client else {}
    channel_stats = state.channel_manager.stats if state.channel_manager else {}
    skill_gen_stats = state.skill_gen.stats if state.skill_gen else {}
    return {
        "version": __version__,
        "config": state.config.to_client_safe_dict(),
        "memory": stats,
        "sessions": len(state.sessions),
        "nodes": list(state.daemons.keys()),
        "devices": len(state.devices),
        "skills": [
            {"skill_id": s.skill_id, "name": s.brand.name, "endpoints": len(s.endpoints)}
            for s in state.skill_registry.skills.values()
        ],
        "audio_available": state.audio.available,
        "hardware": hw_stats,
        "mcp": {
            "server_active": state.mcp_server is not None,
            "client": mcp_client_stats,
        },
        "channels": channel_stats,
        "skill_generator": skill_gen_stats,
        "security": {
            "vault_keys": len(state.vault.list_keys()) if state.vault else 0,
            "max_tier": state.sandbox.max_tier if state.sandbox else "active",
            "policy": state.policy._data.get("name", "default") if state.policy else "none",
        },
        "voice": {
            "audio_available": state.audio.available,
            "realtime_available": state.realtime_proxy.available if state.realtime_proxy else False,
            "active_realtime_sessions": len(state.realtime_proxy._sessions) if state.realtime_proxy else 0,
        },
        "taskflows": state.taskflows.stats() if state.taskflows else {},
        "vision": {
            "change_detector": state.change_detector.stats() if state.change_detector else {},
            "scene_available": state.scene.available if state.scene else False,
            # ``scene_available`` alone read as a flat False whether the
            # operator had configured no VLM at all or had one configured
            # and not running. Those need different actions from them, so
            # report the reachability the SceneAnalyzer already tracks.
            "scene_provider": state.scene.provider_health if state.scene else {},
        },
        "integrations": {
            "oauth": state.oauth.status() if state.oauth else {},
            "spotify": state.spotify.connected if state.spotify else False,
            "home_assistant": state.home_assistant.connected if state.home_assistant else False,
            "notion": state.notion.connected if state.notion else False,
            "webhooks": state.event_bus.stats() if state.event_bus else {},
        },
        "marketplace": {
            "installed_skills": len(state.marketplace.list_installed()) if state.marketplace else 0,
        },
        "multi_agent": state.orchestrator._multi_agent.stats if state.orchestrator and state.orchestrator._multi_agent else {},
        "orchestrator": state.orchestrator.runtime_status if state.orchestrator else {},
    }


def _check_llm_available() -> bool:
    """Real LLM availability check: key is configured and not in cooldown."""
    if not state.orchestrator:
        return False
    llm = getattr(state.orchestrator, 'llm', None)
    if not llm:
        return False
    try:
        return llm.is_available()
    except Exception:
        return False


async def _get_dashboard_data() -> dict:
    stats = await state.memory.stats()
    devices_list = []
    latest_health = {}
    online_node_ids: set[str] = set()
    for node_id in state.daemons:
        dev = state.devices.get(node_id, {})
        devices_list.append({"node_id": node_id, "type": dev.get("device_type", dev.get("node_type", "unknown")), "connected": True})
        online_node_ids.add(node_id)

    # Add paired-but-offline devices so the home page can distinguish
    # "no devices have ever been paired" from "you have N paired
    # devices, none of them are talking to the brain right now". The
    # previous behaviour conflated these and looked like pairing had
    # silently failed.
    #
    # Phase-1 validation pass (Item 6 follow-up): a hard failure of
    # `pairing_store.list_devices` used to be swallowed into
    # `paired_rows = []`, which lied about paired_count when the
    # store was unreachable. We now record the failure into
    # `paired_unavailable: <str>` on the return dict so the
    # dashboard can render a real warning instead of silently
    # claiming zero pairings.
    paired_count = 0
    paired_offline = 0
    paired_unavailable: str | None = None
    pairing_store = getattr(state, "device_pairing_store", None)
    paired_rows: list[dict] = []
    if pairing_store is not None and hasattr(pairing_store, "list_devices"):
        try:
            paired_rows = pairing_store.list_devices(include_unclaimed=False) or []
        except Exception as exc:
            logger.warning(
                "device_pairing_store.list_devices failed: %s", exc,
            )
            paired_unavailable = (
                f"{exc.__class__.__name__}: {str(exc)[:200]}"
            )
            paired_rows = []
        paired_count = len(paired_rows)
        for row in paired_rows:
            node_id = row.get("device_id") or row.get("node_id")
            if not node_id or node_id in online_node_ids:
                continue
            paired_offline += 1
            devices_list.append({
                "node_id": node_id,
                "type": row.get("kind") or row.get("type") or "unknown",
                "name": row.get("name"),
                "connected": False,
                "paired_at": row.get("paired_at"),
                "last_seen": row.get("last_seen"),
            })
    # Same freshness gate the iOS Context tab applies (operator
    # report 2026-06-05): a stale HealthKit reading must NOT flow
    # to the WebUI as ``latest_health.heart_rate`` because the home
    # page renders it as "live · NN bpm" without re-checking.
    # ``heart_rate_fresh`` is forwarded so the client can downgrade
    # the dot to grey when the underlying sample is older than the
    # 120s context window even though we still surface the value
    # for diagnostic display.
    #
    # Fix #6 (operator report 2026-06-08): the per-session loop
    # below USED to be "iterate sessions, last iterated wins" with
    # no live-wearable filter, so a HealthKit-mirrored frame in
    # one session could clobber a fresh W300 reading in another
    # session AND `/api/dashboard.latest_health.heart_rate` could
    # disagree with `/api/health/summary.current_hr` (which already
    # called BrainState._latest_live_wearable_snapshot). Now both
    # endpoints share the same canonical snapshot so the home tile
    # and the chat tool see exactly the same bpm + source.
    snap = None
    try:
        snap = state._latest_live_wearable_snapshot()
    except Exception as exc:
        # Falling back quietly here is how the home tile and /api/health/summary
        # start disagreeing about heart rate, which is what this block exists
        # to prevent.
        logger.warning("latest_live_wearable_snapshot failed: %s", exc)
        snap = None
    if snap and snap.get("heart_rate"):
        latest_health["heart_rate"] = snap["heart_rate"]
        latest_health["heart_rate_source"] = snap.get("heart_rate_source", "") or ""
        latest_health["heart_rate_fresh"] = True
    if snap and snap.get("spo2"):
        latest_health["spo2"] = snap["spo2"]
        latest_health["spo2_source"] = snap.get("spo2_source", "") or ""
        latest_health["spo2_fresh"] = True
    if snap and snap.get("skin_temperature_c"):
        latest_health["temperature"] = snap["skin_temperature_c"]
    # Stale fallback: when the canonical snapshot is empty (no fresh
    # live wearable), still surface a non-zero perception value so
    # the diagnostic banner can render "(stale)" rather than "—".
    # Lagging sources land here too, so the client must read
    # `*_fresh` before painting the live dot.
    if "heart_rate" not in latest_health:
        for sid in state.sessions:
            frame = state.perception.get_frame(sid)
            if frame and frame.heart_rate:
                latest_health["heart_rate_stale"] = frame.heart_rate
                latest_health["heart_rate_source"] = frame.heart_rate_source or ""
                latest_health["heart_rate_fresh"] = False
                break
    if "spo2" not in latest_health:
        for sid in state.sessions:
            frame = state.perception.get_frame(sid)
            if frame and frame.spo2_pct:
                latest_health["spo2_stale"] = frame.spo2_pct
                latest_health["spo2_source"] = frame.spo2_source or ""
                latest_health["spo2_fresh"] = False
                break
    if "temperature" not in latest_health:
        for sid in state.sessions:
            frame = state.perception.get_frame(sid)
            if frame and frame.skin_temperature_c:
                latest_health["temperature"] = frame.skin_temperature_c
                break
    boot_data = state._boot_report.to_dict() if hasattr(state, '_boot_report') else {}

    is_demo = getattr(state, "_demo", None) is not None or os.environ.get("FERAL_DEV_DEMO", "").lower() in ("1", "true", "yes")

    somatic_state = {}
    if hasattr(state, 'somatic_engine') and state.somatic_engine and state.sessions:
        for sid in state.sessions:
            vec = state.somatic_engine.get_vector(sid)
            somatic_state = {
                "cognitive_load": vec.cognitive_load,
                "heart_rate": vec.heart_rate,
                "hrv_ms": vec.hrv_ms,
                "spo2_pct": vec.spo2_pct,
                "activity_level": vec.activity_level,
                "circadian_phase": vec.circadian_phase,
            }
            break

    channel_types = []
    if state.channel_manager and hasattr(state.channel_manager, 'channels'):
        for ch_id, ch in state.channel_manager.channels.items():
            if getattr(ch, 'enabled', False):
                channel_types.append({"type": ch_id, "connected": getattr(ch, '_running', False)})

    # Sub-device summary — counted from the truth store, not invented.
    # ``subdevices_total`` is every row we know about (live + stale);
    # ``subdevices_live`` is only those still inside their heartbeat
    # window. Phase-1 dashboard binds Home/HubLauncher dots to
    # ``subdevices_live > 0`` so "Active" never shows for a peripheral
    # whose phone has been offline for a minute.
    #
    # Phase-1.5: read failures are surfaced as
    # ``subdevices_unavailable: <error_text>`` so the dashboard
    # renders a real "Sub-device data temporarily unavailable"
    # warning instead of silently lying that the user has none.
    subdevices_total = 0
    subdevices_live = 0
    subdevices_unavailable: str | None = None
    subdevice_rows: list[dict] = []
    subdevice_store = getattr(state, "node_subdevices", None)
    if subdevice_store is None:
        subdevices_unavailable = "subdevice store not initialised"
    else:
        try:
            subdevice_rows = subdevice_store.list_all()
        except Exception as exc:
            logger.warning("node_subdevices.list_all failed: %s", exc)
            subdevices_unavailable = (
                f"{exc.__class__.__name__}: {str(exc)[:200]}"
            )
    subdevices_total = len(subdevice_rows)
    subdevices_live = sum(1 for r in subdevice_rows if r.get("live"))

    # Attach to the matching device row when present so a single
    # `/api/dashboard` round-trip carries everything Home/HubLauncher
    # need to render the per-device sub-device chips.
    if subdevice_rows:
        sub_by_node: dict[str, list[dict]] = {}
        for row in subdevice_rows:
            sub_by_node.setdefault(row["node_id"], []).append(row)
        for entry in devices_list:
            nid = entry.get("node_id")
            if nid and nid in sub_by_node:
                entry["subdevices"] = sub_by_node[nid]

    return {
        # `devices` now includes paired-but-offline rows alongside
        # live ones (each row carries `connected: bool`). The legacy
        # `device_count` stays as live-only for back-compat with any
        # client that already keys off it; new clients should prefer
        # `online_count` + `paired_count`.
        "devices": devices_list,
        "device_count": len(state.daemons),
        "online_count": len(state.daemons),
        "paired_count": paired_count,
        "paired_offline_count": paired_offline,
        "subdevices_total": subdevices_total,
        "subdevices_live": subdevices_live,
        "subdevices_unavailable": subdevices_unavailable,
        # Mirrors `subdevices_unavailable` for the paired-pairing
        # store so the dashboard can render distinct warnings: the
        # truth-store may be reachable while the pairing store is
        # not (or vice versa). `null` on the success branch.
        "paired_unavailable": paired_unavailable,
        "channels": channel_types,
        # Whether this process is executing the version that is
        # installed. A Python process never reloads its source, so
        # `pip install --upgrade feral-ai` cannot reach a running brain:
        # the upgrade succeeds, nothing errors, and the operator keeps
        # using the old build with no symptom. Measured on a real
        # install, a brain served for two days and one hour from code
        # that predated four releases. Local comparison, no network.
        "runtime": _runtime_staleness(),
        # The other half of the same story: whether a newer release
        # exists at all. That question needs the network, so it is
        # opt-in and this field only ever reads a cache written
        # elsewhere. No socket is opened on this request path whatever
        # pypi.org is doing, and a check that has never run, is turned
        # off, or failed reports itself rather than erroring.
        "update": _update_availability(),
        "session_count": len(state.sessions), "health": latest_health,
        "memory": stats, "skills_count": len(state.skill_registry.skills),
        "llm_available": _check_llm_available(),
        "audio_available": state.audio.available,
        "sync": state.sync_engine.stats if state.sync_engine else {},
        "wasm_available": state.wasm_sandbox.available if state.wasm_sandbox else False,
        "wake_word_enabled": state.wake_word.enabled if state.wake_word else False,
        "taskflows": state.taskflows.stats() if state.taskflows else {},
        "boot": boot_data,
        # What today has cost, and the tier that decides what runs
        # without asking. Both are real state the operator acts on and
        # neither had any HTTP surface: the budget lived only on
        # LLMProvider._budget_snapshot() and autonomy only on
        # GET /api/autonomy, so the dashboard's cost and autonomy
        # readouts had nothing to read and rendered as absent forever.
        #
        # Carried on the existing dashboard poll rather than as two more
        # endpoints, because the shell already polls this one and the
        # numbers are wanted together.
        "budget": _budget_status(),
        # Seconds this process has been up. The Brain readout had no
        # uptime to show because nothing recorded a start time.
        "uptime_s": _uptime_seconds(),
        "brain_activity": _brain_activity(),
        "autonomy": _autonomy_mode(),
        "demo": is_demo,
        "is_demo_mode": getattr(state, "_demo", None) is not None,
        "somatic": somatic_state,
    }


def _runtime_staleness() -> dict:
    """Whether the running code matches the installed code.

    Never raises into the dashboard. This is a diagnostic on the
    endpoint the whole shell polls, so a failure here must cost the
    panel, not the page. A brain that cannot answer reports
    `stale: False` rather than guessing, on the principle that telling
    somebody to restart on the strength of a failed lookup is worse
    than staying quiet.
    """
    try:
        from config.staleness import runtime_staleness

        return runtime_staleness()
    except Exception as exc:
        logger.debug("runtime staleness check failed: %s", exc)
        return {"stale": False, "detail": "unavailable"}


def _update_availability() -> dict:
    """Whether a newer release exists, from cache only.

    ``update_status`` reads a JSON file and opens no socket, which is
    the property that makes this safe to put on the endpoint the whole
    shell polls: a slow or unreachable pypi.org cannot slow this
    response down, because this response never asks it anything. The
    cache is filled by the brain's own opt-in refresher and by
    `feral update`.

    Wrapped anyway. Same rule as `_runtime_staleness` above: a
    diagnostic on this route costs its own panel, never the page.
    """
    try:
        from config.update_check import update_status

        return update_status()
    except Exception as exc:
        logger.debug("update availability check failed: %s", exc)
        return {"enabled": False, "status": "unknown", "detail": "unavailable"}


def _require_number(value, *, allow_none: bool = False):
    """``value`` as a float, or raise so the caller can fall back.

    Written as a rejection rather than a coercion because of the exact
    trap `_uptime_seconds` above documents: a `MagicMock` implements
    `__float__` and answers 1.0, so `float(state.cost_budget.
    current_spend(...))` would invent a spend of $1.00 on any stubbed
    state instead of degrading. A number here has to actually be a
    number.
    """
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"not a number: {type(value).__name__}")
    return float(value)


def _budget_from_ledger(budget) -> dict:
    """Live spend and caps read from the thing that enforces them.

    ``CostBudget`` is the only object in the process that knows what has
    actually been spent: ``record_usage`` writes ``cost_events`` and
    ``cost_rollup`` and keeps a per (call_site, window) tally that
    ``check_and_reserve`` then bills against. Reporting anything else
    means the header and the enforcer can disagree, which is exactly
    what the operator saw.

    ``chat`` is the call site the header is about. The hourly cap on it
    is what stops a conversation, and the global hourly cap stands in
    when no per-site cap is configured.
    """
    hour_spend = _require_number(budget.current_spend("chat", "hour"))
    hour_cap = _require_number(budget._cap_for("chat", "hour"), allow_none=True)
    if hour_cap is None:
        hour_cap = _require_number(
            budget._cap_for("__global__", "hour"), allow_none=True,
        )
    # Global, not per-site: the label on this number is "spent today
    # across every provider", so it must include the screen loop, the
    # learner and the proactive engine, not just chat.
    day_spend = _require_number(budget.current_spend(None, "day"))
    day_cap = _require_number(budget._cap_for("__global__", "day"), allow_none=True)

    caps_set = hour_cap is not None or day_cap is not None
    daily_budget = float(day_cap or 0.0)
    remaining = daily_budget - day_spend
    return {
        # True when the operator has configured ANY cap. The old snapshot
        # keyed this off ``llm.daily_budget_usd``, a setting nothing
        # writes, so an install with a $10/hour chat cap reported
        # "no budget configured".
        "enabled": bool(caps_set),
        "source": "cost_budget",
        "hour_spend_usd": hour_spend,
        "hour_cap_usd": float(hour_cap or 0.0),
        "daily_spend_usd": day_spend,
        "daily_budget_usd": daily_budget,
        "remaining_usd": remaining,
        "headroom_ratio": (remaining / daily_budget) if daily_budget > 0 else 1.0,
    }


def _budget_status() -> dict:
    """Today's spend against the cap, or an empty dict if unknowable.

    Empty rather than zeros on failure: a bar that reports $0.00 when it
    simply could not read the number is worse than one that shows
    nothing, because $0.00 is a claim.

    It used to be a claim. This read ``LLMProvider._budget_snapshot()``,
    which resolves ``FERAL_LLM_DAILY_SPEND_USD`` or the config key
    ``llm.daily_spend_usd``, and the only producer of that key is the
    static ``0.0`` default in ``config/loader.py``. Nothing ever writes
    it, and it is not connected to the ledger, so the header read
    "$0.00" on an install that had just spent $9.99 of a $10 hourly cap
    and was refusing turns because of it.

    ``state.cost_budget`` is now the preferred source. The provider
    snapshot stays as the fallback for a process that has no CostBudget
    (its construction is wrapped in a try at boot), because an operator
    who did set ``FERAL_LLM_DAILY_BUDGET_USD`` should still see it.
    """
    budget = getattr(state, "cost_budget", None)
    if budget is not None:
        try:
            return _budget_from_ledger(budget)
        except Exception:
            logger.debug("dashboard: cost_budget unreadable", exc_info=True)

    try:
        provider = getattr(getattr(state, "orchestrator", None), "llm", None)
        snapshot = getattr(provider, "_budget_snapshot", None)
        if callable(snapshot):
            return dict(snapshot() or {})
    except Exception:
        logger.debug("dashboard: budget snapshot unavailable", exc_info=True)
    return {}


def _uptime_seconds() -> float:
    """Seconds since this process came up, or 0.0 if unknowable.

    The arithmetic is guarded rather than inlined, and the reason is a
    real 500 this shipped with: `getattr(state, "started_at", default)`
    looks safe and is not, because `state` is a MagicMock under test and
    a mock ALWAYS has every attribute. The default therefore never
    applied, `time.time() - <MagicMock>` raised TypeError, and the whole
    dashboard endpoint answered 500.

    Coercing through float() covers the same shape in production: an
    older BrainState pickled without the field, or anything that leaves
    a non-numeric there, degrades to "unknown" instead of taking down
    the one endpoint the entire shell polls.
    """
    try:
        started = float(getattr(state, "started_at", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if started <= 0.0:
        return 0.0
    return max(0.0, time.time() - started)


def _brain_activity() -> dict:
    """When a turn last ran, and how full the context view was.

    Both were unmeasured, so the dashboard's Brain readout could not
    answer "when did this last do anything" or "how much room is left",
    which are two of the four rows the design specifies. The orchestrator
    records them off paths that already run per turn.

    `last_turn_at` is 0.0 when no turn has run in this process, and a
    caller must read that as "unknown" rather than "just now".
    """
    try:
        orch = getattr(state, "orchestrator", None)
        if orch is None:
            return {}
        # `runtime_status` is a @property, so this is already the dict.
        # Calling it returns a TypeError, and guarding with `callable()`
        # silently returns nothing at all, which is how this first
        # shipped: the field was present and permanently empty, with no
        # error anywhere. `/api/system/info` reads it the same way.
        data = orch.runtime_status
        # Deliberately not `or {}`: None and [] are falsy, so that idiom
        # turns "the orchestrator told us nothing" into a valid empty
        # dict and then into two zeros. Zero is a reading; unknown is
        # not, and the readout cannot tell them apart.
        if not isinstance(data, dict) or not data:
            return {}
        return {
            "last_turn_at": float(data.get("last_turn_at") or 0.0),
            "context_used_pct": float(data.get("context_used_pct") or 0.0),
        }
    except Exception:
        logger.debug("dashboard: brain activity unavailable", exc_info=True)
        return {}


def _autonomy_mode() -> str:
    """The live tier from the ToolRunner, which is the thing that gates."""
    try:
        orch = getattr(state, "orchestrator", None)
        runner = getattr(orch, "tool_runner", None)
        mode = getattr(runner, "autonomy_mode", "")
        return str(mode or "")
    except Exception:
        return ""


@router.get("/api/dashboard")
async def dashboard_data():
    """Aggregated data for the live dashboard — weather, devices, health, activity."""
    return await _get_dashboard_data()


@router.get("/api/activity")
async def get_activity():
    """Recent brain activity log."""
    return {"entries": list(state.activity_log)}


# ── HealthKit ingest (iOS companion) ─────────────────────────────
#
# Operator report 2026-06-07: the iOS companion's HealthKitAdapter
# was POSTing samples to ``/api/health/ingest`` — a route that
# didn't exist. The phone-bearer middleware short-circuited the
# request with HTTP 401 ("Unauthorized — provide Authorization:
# Bearer <key>") because the path was not on the
# ``_PHONE_BEARER_POST`` allowlist; the iOS DebugLog filled with
# repeated ``healthkit ingest failed: ingest failed HTTP 401``
# warnings on every poll cycle and HealthKit data never reached
# the brain's long-lived memory store.
#
# This route closes the gap. It is a phone-bearer-authenticated
# write that turns each sample into a normal memory record so the
# memory tool surface ("What was my heart rate this morning?") can
# answer from the same store as the rest of the brain. Realtime HR
# / SpO2 still flows over the WebSocket as ``device_event`` frames
# — that path is unchanged; the ingest route is purely additive.
@router.post("/api/health/ingest")
async def ingest_health_samples(request: Request):
    """Accept a batch of HealthKit (or compatible) samples from the
    iOS companion and persist each one as a memory record.

    Body shape mirrors ``BrainHTTP.ingest`` in the iOS SDK::

        {
          "source": "ios.healthkit",
          "ingested_at": "<ISO-8601>",
          "samples": [
            {
              "event_type": "heart_rate",
              "bpm": 72,
              "source": "apple_healthkit",
              "pipeline": "Apple Health",
              "sample_source": "Apple Watch",
              "sampled_at_ms": 1717800000000.0
            },
            ...
          ]
        }

    Auth: gated by the phone-bearer middleware allowlist. Returns
    ``{"persisted": N, "skipped": M}`` so the iOS Devices tab can
    render "Synced N / M samples" without a follow-up query.
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError) as exc:
        return JSONResponse(
            {"error": f"Invalid JSON body: {exc}"}, status_code=400,
        )
    if not isinstance(body, dict):
        return JSONResponse(
            {"error": "Body must be a JSON object"}, status_code=400,
        )
    samples = body.get("samples")
    if not isinstance(samples, list):
        return JSONResponse(
            {"error": "Body must contain a `samples` array"}, status_code=400,
        )
    bridge_source = str(body.get("source") or "ios.healthkit")

    memory = getattr(state, "memory", None)
    if memory is None or not hasattr(memory, "save"):
        return JSONResponse(
            {"error": "Memory store unavailable"}, status_code=503,
        )

    persisted = 0
    skipped = 0
    for raw in samples:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        event_type = str(raw.get("event_type") or "").strip().lower()
        if not event_type:
            skipped += 1
            continue
        # Build a compact human-readable content line per sample so
        # the memory recall path renders the right thing without a
        # custom serializer. Fall back to the raw dict as a JSON tail
        # so nothing is lost.
        value = (
            raw.get("bpm")
            or raw.get("current")
            or raw.get("value")
            or raw.get("count")
            or raw.get("celsius")
        )
        unit_hint = {
            "heart_rate": "bpm",
            "spo2": "%",
            "steps": "steps",
            "temperature": "°C",
            "hrv": "ms",
        }.get(event_type, "")
        sample_source = str(
            raw.get("sample_source")
            or raw.get("pipeline")
            or raw.get("source")
            or "Apple Health"
        )
        if value is not None:
            content_line = f"{event_type} {value}{unit_hint} ({sample_source})".strip()
        else:
            content_line = f"{event_type} sample ({sample_source})"
        try:
            await memory.save(
                content=content_line,
                tags=["health", "ingest", event_type, bridge_source],
                importance="normal",
                source=f"ingest:{bridge_source}:{event_type}",
            )
            persisted += 1
        except Exception as exc:
            logger.warning(
                "health/ingest: memory.save failed for event_type=%s: %s",
                event_type, exc,
            )
            skipped += 1

    return {"persisted": persisted, "skipped": skipped}


# ── Health frame (brain → node) ─────────────────────────────────────
# Companion to the ingest route above, running the other direction.
# Health data previously only reached the Theora iOS app as English
# prose inside a ``chat_response``, because the app's HUP parser had no
# health frame to decode. ``health_update`` (models/protocol.py) is that
# frame; its envelope mirrors the daemon → brain ``device_event``
# convention exactly (``{node_id, event_type, data, ts}``).
#
# This route both RETURNS the frame over HTTP and PUSHES it to every
# connected HUP node, so an app gets it either by asking or by being
# told. Fetching is also what keeps the durable Whoop mirror warm, since
# ``get_health_summary`` consults the sync service.

async def _push_health_update(frame: dict) -> int:
    """Fan a ``health_update`` frame out to connected HUP nodes.

    Each node gets its own copy stamped with its own ``node_id``,
    matching the ``device_event`` convention where ``node_id`` names the
    node the frame belongs to. Returns how many nodes were reached; a
    dead socket is skipped, never fatal.
    """
    daemons = getattr(state, "daemons", None) or {}
    delivered = 0
    for node_id, ws in list(daemons.items()):
        try:
            payload = dict(frame.get("payload") or {})
            payload["node_id"] = str(node_id)
            await ws.send_json(stamp_hup_envelope({**frame, "payload": payload}))
            delivered += 1
        except Exception as exc:
            # Deliberately still debug, unlike the other handlers in this file.
            # A socket dying mid-broadcast is ordinary churn on a per-frame
            # path, the count of successes is returned to the caller, and the
            # broadcast-level failure is reported at warning by the caller. A
            # warning here would fire on every phone that walks out of range
            # and would be the first log line anyone learned to ignore.
            logger.debug("health_update push to %s failed: %s", node_id, exc)
    return delivered


@router.get("/api/health/frame")
async def health_frame(request: Request):
    """Return (and broadcast) the canonical ``health_update`` frame.

    Query args:
        ``event_type``: ``health_summary`` (default) or ``vitals_trend``.
        ``days``: window for ``vitals_trend`` (default 7).
        ``push``: ``0`` to skip the broadcast to connected nodes.
    """
    from integrations.health_canonical import (
        HEALTH_EVENT_SUMMARY,
        HEALTH_EVENT_TYPES,
        build_health_update_frame,
    )

    aggregator = getattr(state, "health_aggregator", None)
    if aggregator is None or not hasattr(aggregator, "build_health_update"):
        return build_health_update_frame(
            event_type=HEALTH_EVENT_SUMMARY,
            note="Health aggregator is not initialized.",
        )

    params = request.query_params
    event_type = params.get("event_type") or HEALTH_EVENT_SUMMARY
    if event_type not in HEALTH_EVENT_TYPES:
        return JSONResponse(
            {"error": f"event_type must be one of {list(HEALTH_EVENT_TYPES)}"},
            status_code=400,
        )
    try:
        days = int(params.get("days") or 7)
    except (TypeError, ValueError):
        return JSONResponse({"error": "days must be an integer"}, status_code=400)

    try:
        frame = await aggregator.build_health_update(
            event_type=event_type, days=max(days, 1),
        )
    except Exception as exc:
        logger.warning("health_frame build failed: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)

    if (params.get("push") or "1") not in ("0", "false", "no"):
        try:
            await _push_health_update(frame)
        except Exception as exc:  # pragma: no cover - defensive
            # The frame is returned either way, so without this the devices
            # simply stop updating and the API still looks healthy.
            logger.warning("health_update broadcast failed: %s", exc)
    return frame
