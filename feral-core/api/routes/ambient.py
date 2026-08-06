"""Ambient surface API — briefing, next event, snapshot, wind-down, wake-word."""

import logging
import os
import random
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException

from api.state import state

router = APIRouter(prefix="/api/ambient", tags=["ambient"])
logger = logging.getLogger("feral.ambient")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _outfit_from_temp(t: float) -> str:
    if t < 5:
        return "heavy coat, scarf, gloves"
    elif t < 15:
        return "jacket, long sleeves"
    elif t < 22:
        return "light layer"
    return "t-shirt, sunscreen"


# Two spellings are in use: proactive_engine reads "hrv_ms", ideas_engine
# reads "hrv". Accept both rather than pick a winner here, which would make
# the briefing disagree with whichever engine it did not match.
_HRV_METRIC_IDS = ("hrv_ms", "hrv")


def _trend_direction(values: list[float], window: int = 7) -> str:
    """Direction over the last *window* readings, or "stable".

    BaselineEngine.check_trend answers the same question but persists a
    BaselineAlert as a side effect. This is a GET, so it does the arithmetic
    without writing: a briefing must not manufacture alert history simply by
    being requested.
    """
    tail = values[-window:]
    if len(tail) < 2:
        return "unknown"
    diffs = [tail[i + 1] - tail[i] for i in range(len(tail) - 1)]
    if all(d > 0 for d in diffs):
        return "upward"
    if all(d < 0 for d in diffs):
        return "downward"
    return "stable"


# ── Briefing ─────────────────────────────────────────────────────────────────

@router.get("/briefing")
async def get_briefing():
    """Morning briefing: sleep recap, today's agenda, weather, goals."""
    if not state.orchestrator:
        raise HTTPException(503, "Orchestrator not initialized")

    # Sections whose lookup raised. An empty section is not the same question
    # as a broken one, and for the whole life of this endpoint the response
    # could not tell them apart: every block below failed permanently and
    # reported its failure at debug, so a briefing of empty fields looked
    # exactly like a quiet morning.
    degraded: list[str] = []

    briefing: dict = {
        "greeting": "",
        "sleep": None,
        "agenda": [],
        "weather": None,
        "goals": [],
        "vip_emails": [],
        "degraded": degraded,
    }

    # get_all_baselines returns BaselineMetric dataclasses, not dicts, as
    # api/routes/baseline.py has always known (it calls asdict on them). The
    # old .get("metric") raised AttributeError on the first element, so
    # "sleep" was None in every briefing ever served. There is no "value" or
    # "trend" field either: the rolling average is .mean and direction has to
    # be derived from .values.
    try:
        if state.baseline_engine:
            metrics = state.baseline_engine.get_all_baselines()
            hrv = next((m for m in metrics if m.metric_id in _HRV_METRIC_IDS), None)
            if hrv is not None:
                briefing["sleep"] = {
                    "hrv_ms": hrv.mean,
                    "trend": _trend_direction(hrv.values),
                    "samples": len(hrv.values),
                }
    except Exception as e:
        logger.warning("briefing: sleep recap unavailable: %s", e)
        degraded.append("sleep")

    # IntentCompiler exposes get_today_actions(); there is no today(). The old
    # call raised AttributeError, and the shape was wrong on top of that:
    # get_today_actions returns the action list directly, not {"actions": [...]}.
    try:
        if getattr(state, "intent_compiler", None):
            briefing["agenda"] = state.intent_compiler.get_today_actions()[:3]
    except Exception as e:
        logger.warning("briefing: agenda unavailable: %s", e)
        degraded.append("agenda")

    # Likewise list_active() does not exist; the method is list_plans(), which
    # returns every plan rather than only the active ones, so the status
    # filter the old name implied has to be applied here. Keys are plan_id and
    # intent, so p["id"] would have been a KeyError even had the call landed.
    try:
        if getattr(state, "intent_compiler", None):
            plans = state.intent_compiler.list_plans()
            briefing["goals"] = [
                {
                    "id": p["plan_id"],
                    "title": p.get("intent", ""),
                    "progress": p.get("progress", 0),
                }
                for p in plans
                if p.get("status") == "active"
            ][:3]
    except Exception as e:
        logger.warning("briefing: goals unavailable: %s", e)
        degraded.append("goals")

    # EmailWatcher has no get_recent_vip, here or anywhere in the tree, so the
    # hasattr guard makes this a permanent no-op: the field reads "no VIP mail"
    # when the truth is that nothing ever looks. Kept guarded rather than
    # deleted because the surface consumes the key, and reported so the empty
    # list is not mistaken for a quiet inbox. Implementing VIP recall is its
    # own change.
    if getattr(state, "email_watcher", None) is not None:
        getter = getattr(state.email_watcher, "get_recent_vip", None)
        if getter is None:
            degraded.append("vip_emails:not_implemented")
        else:
            try:
                briefing["vip_emails"] = getter(limit=3)
            except Exception as e:
                logger.warning("briefing: vip emails unavailable: %s", e)
                degraded.append("vip_emails")

    # Weather (optional — requires OPENWEATHER_API_KEY). Vault uses
    # ``retrieve(key_name)`` — there is no ``get`` method — so we fall through
    # to an env lookup + vault retrieve without tripping AttributeError when
    # the key is absent.
    ow_key = os.environ.get("OPENWEATHER_API_KEY") or ""
    if not ow_key and hasattr(state, "vault") and state.vault:
        try:
            ow_key = state.vault.retrieve("OPENWEATHER_API_KEY") or ""
        except Exception:
            ow_key = ""
    if ow_key:
        try:
            import httpx

            lat = os.environ.get("FERAL_USER_LAT", "40.7128")
            lng = os.environ.get("FERAL_USER_LNG", "-74.0060")
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(
                    f"https://api.openweathermap.org/data/2.5/weather"
                    f"?lat={lat}&lon={lng}&appid={ow_key}&units=metric"
                )
                if r.status_code == 200:
                    data = r.json()
                    briefing["weather"] = {
                        "temp_c": data["main"]["temp"],
                        "feels_like_c": data["main"]["feels_like"],
                        "condition": data["weather"][0]["main"],
                        "description": data["weather"][0]["description"],
                        "outfit_hint": _outfit_from_temp(data["main"]["feels_like"]),
                    }
        except Exception as e:
            logger.warning("briefing: weather unavailable: %s", e)
            degraded.append("weather")

    now = datetime.now()
    hour = now.hour
    if 5 <= hour < 12:
        briefing["greeting"] = "Good morning"
    elif 12 <= hour < 17:
        briefing["greeting"] = "Good afternoon"
    else:
        briefing["greeting"] = "Good evening"

    return briefing


# ── Next event ───────────────────────────────────────────────────────────────

@router.get("/next_event")
async def get_next_event():
    """Return the next calendar event from the integrated calendar service.

    Audit-r9 fix: previously this route looked up `calendar_lookup` /
    `google_calendar` only, but the registered skill ID (per
    `api/state.py:633`) is `calendar_google`. So the route always fell
    through to the "Connect Google Calendar" hint even with a working
    integration. Try `calendar_google` first now and fall back to the
    legacy aliases. Also try `state.calendar` directly as a final
    fallback so the route works the moment the integration boots,
    even before skill registration runs.
    """
    try:
        cal_skill = None
        if state.skill_registry:
            for skill_id in ("calendar_google", "calendar_lookup", "google_calendar"):
                cal_skill = state.skill_registry.skills.get(skill_id)
                if cal_skill is not None:
                    break
        if cal_skill is None:
            cal_skill = state.calendar
        if cal_skill is not None and hasattr(cal_skill, "execute"):
            result = await cal_skill.execute("next_event", {}, {})
            if isinstance(result, dict) and result.get("success") and result.get("data"):
                return result["data"]
    except Exception as e:
        # The hint below tells the user to connect a calendar. When the lookup
        # raised, the calendar may well be connected and failing, and sending
        # someone to reconnect an already-connected integration is worse than
        # saying nothing. Report the failure instead of guessing the cause.
        logger.warning("next_event lookup failed: %s", e)
        return {
            "event": None,
            "degraded": f"{type(e).__name__}: {e}",
            "hint": "Calendar lookup failed. Check Settings > Integrations.",
        }

    return {"event": None, "hint": "Connect Google Calendar via Settings > Integrations"}


# ── Snapshot ─────────────────────────────────────────────────────────────────

@router.get("/snapshot")
async def get_snapshot():
    """Minimal ambient snapshot: time, vitals, greeting, mode suggestion."""
    now = datetime.now()
    hour = now.hour

    if 5 <= hour < 9:
        suggested_mode = "briefing"
    elif 9 <= hour < 19:
        suggested_mode = "desk"
    else:
        suggested_mode = "wind_down"

    vitals = {}
    degraded: list[str] = []
    try:
        if state.perception and state.sessions:
            for sid in state.sessions:
                frame = state.perception.get_frame(sid)
                if frame:
                    vitals = {
                        "heart_rate": getattr(frame, "heart_rate", 0),
                        "spo2": getattr(frame, "spo2_pct", 0),
                        "skin_temperature_c": getattr(frame, "skin_temperature_c", 0),
                        "battery_pct": getattr(frame, "battery_pct", 100),
                    }
                    break
    except Exception as e:
        # An empty vitals dict is what a glasses-less desk session looks like,
        # so a perception failure rendered as "no wearable connected".
        logger.warning("vitals snapshot failed: %s", e)
        degraded.append(f"vitals: {type(e).__name__}: {e}")

    return {
        "time": now.isoformat(),
        "suggested_mode": suggested_mode,
        "vitals": vitals,
        "degraded": degraded,
    }


# ── Wind-Down ────────────────────────────────────────────────────────────────

_JOURNAL_PROMPTS = [
    "What did you learn today?",
    "What went well?",
    "What's one thing you're grateful for?",
    "What do you want to carry forward?",
]


@router.get("/wind_down")
async def get_wind_down():
    """Evening wind-down: day recap, sleep prep, memory highlights."""
    day_recap: dict = {
        "completed_tasks": [],
        "active_durations_s": 0,
        "key_episodes": [],
    }

    degraded: list[str] = []

    # This used to sit behind a hasattr guard for a method IntentCompiler did
    # not have, so the evening recap reported an empty day no matter how much
    # was finished. The method exists now; call it directly, because a guard
    # that silently skips is how the absence lasted.
    try:
        if getattr(state, "intent_compiler", None):
            day_recap["completed_tasks"] = state.intent_compiler.get_completed_today()
    except Exception as e:
        logger.warning("wind_down: day recap unavailable: %s", e)
        degraded.append("completed_tasks")

    episodes: list = []
    try:
        if getattr(state, "memory", None):
            recent = (await state.memory.episode_recent(limit=10)) or []
            today = datetime.now().date()
            episodes = [
                e for e in recent
                if datetime.fromisoformat(e.get("ts", "1970-01-01")).date() == today
            ][:2]
    except Exception as e:
        # "Nothing memorable happened today" and "recall is broken" are
        # different evenings.
        logger.warning("wind_down: episodes unavailable: %s", e)
        degraded.append("key_episodes")

    now = datetime.now()
    bedtime = now.replace(hour=23, minute=0, second=0, microsecond=0)
    if now > bedtime:
        bedtime += timedelta(days=1)
    time_to_bed_min = max(0, int((bedtime - now).total_seconds() / 60))

    hints = [
        "Dim the lights" if time_to_bed_min < 60 else "Plan tomorrow",
    ]
    if time_to_bed_min < 30:
        hints.append("Avoid screens soon")

    sleep_prep = {
        "time_to_bed_min": time_to_bed_min,
        "hints": hints,
    }

    return {
        "day_recap": day_recap,
        "episodes": [
            {"id": e.get("id"), "summary": (e.get("summary") or e.get("content", ""))[:100]}
            for e in episodes
        ],
        "sleep_prep": sleep_prep,
        "journal_prompt": random.choice(_JOURNAL_PROMPTS),
        "degraded": degraded,
    }


# ── Wake-Word ────────────────────────────────────────────────────────────────

@router.get("/wake_word/status")
async def wake_word_status():
    """Returns current wake-word detector status."""
    if not hasattr(state, "wake_word") or not state.wake_word:
        return {"enabled": False, "supported": False}
    return {
        "enabled": getattr(state.wake_word, "enabled", False),
        "phrase": getattr(state.wake_word, "phrase", "hey feral"),
        "supported": True,
    }


@router.post("/wake_word/toggle")
async def toggle_wake_word():
    """Toggle the wake-word detector on/off."""
    if not hasattr(state, "wake_word") or not state.wake_word:
        raise HTTPException(503, "Wake word detector not initialized")
    state.wake_word.enabled = not state.wake_word.enabled
    return {"enabled": state.wake_word.enabled}
