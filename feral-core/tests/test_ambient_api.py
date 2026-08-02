"""
Tests for the Ambient surface API routes (briefing, snapshot, next_event,
wind_down, wake_word).
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = [pytest.mark.asyncio, pytest.mark.no_auto_feral_home]


def _make_mock_state():
    """Minimal BrainState mock for ambient endpoints."""
    s = MagicMock()
    s.orchestrator = MagicMock()
    s.sessions = {}
    s.perception = MagicMock()

    s.baseline_engine = MagicMock()
    s.baseline_engine.get_all_baselines = MagicMock(return_value=[])

    s.intent_compiler = None
    s.email_watcher = None

    s.skill_registry = MagicMock()
    s.skill_registry.skills = {}

    s.memory = None
    s.wake_word = None

    # Explicitly "no vault configured", matching a fresh brain that
    # never wired one. Left as an auto-vivified MagicMock, ``state.vault``
    # is truthy and ``state.vault.retrieve(...)`` returns another
    # unconfigured (truthy) Mock rather than ``None`` -- the briefing
    # route's ``ow_key = state.vault.retrieve(...) or ""`` then reads a
    # real-looking key out of thin air and dials OpenWeatherMap for
    # real on every test that hits ``/briefing``, key or no key.
    s.vault = None

    return s


@pytest.fixture
def _patched_app(_disable_api_key_middleware_for_tests, monkeypatch):
    """Import the app with a mocked state so we don't need the full brain."""
    # Belt-and-suspenders alongside ``s.vault = None`` above: if the
    # developer's shell (or CI) happens to export a real
    # OPENWEATHER_API_KEY, the briefing route would pick it up from
    # ``os.environ`` and dial out for real regardless of the mocked
    # vault. These tests never opt into that path, so keep it absent
    # by default; ``test_briefing_weather_null_without_api_key`` relies
    # on exactly this.
    monkeypatch.delenv("OPENWEATHER_API_KEY", raising=False)
    mock = _make_mock_state()
    with patch("api.routes.ambient.state", mock), \
         patch("api.state.state", mock):
        from api.routes.ambient import router
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(router)
        yield app, mock


# ── Briefing ─────────────────────────────────────────────────────────────────

async def test_briefing_returns_structure(_patched_app):
    app, mock = _patched_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/ambient/briefing")
    assert resp.status_code == 200
    body = resp.json()
    assert "greeting" in body
    assert "sleep" in body
    assert "agenda" in body
    assert "weather" in body
    assert "goals" in body
    assert "vip_emails" in body
    assert isinstance(body["agenda"], list)
    assert isinstance(body["goals"], list)


async def test_briefing_includes_sleep_when_hrv_present(_patched_app):
    app, mock = _patched_app
    mock.baseline_engine.get_all_baselines.return_value = [
        {"metric": "hrv_ms", "value": 42, "trend": "improving"},
    ]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/ambient/briefing")
    body = resp.json()
    assert body["sleep"] is not None
    assert body["sleep"]["hrv_ms"] == 42
    assert body["sleep"]["trend"] == "improving"


async def test_briefing_503_without_orchestrator(_patched_app):
    app, mock = _patched_app
    mock.orchestrator = None
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/ambient/briefing")
    assert resp.status_code == 503


async def test_briefing_weather_null_without_api_key(_patched_app):
    """Without OPENWEATHER_API_KEY the weather field should be null."""
    app, mock = _patched_app
    with patch.dict("os.environ", {}, clear=False):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/ambient/briefing")
    body = resp.json()
    assert body["weather"] is None


# ── Snapshot ─────────────────────────────────────────────────────────────────

async def test_snapshot_suggests_mode_by_time(_patched_app):
    app, _ = _patched_app
    morning = datetime(2026, 4, 16, 7, 30, 0)
    with patch("api.routes.ambient.datetime") as mock_dt:
        mock_dt.now.return_value = morning
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/ambient/snapshot")
    body = resp.json()
    assert body["suggested_mode"] == "briefing"


async def test_snapshot_desk_mode_during_work_hours(_patched_app):
    app, _ = _patched_app
    noon = datetime(2026, 4, 16, 12, 0, 0)
    with patch("api.routes.ambient.datetime") as mock_dt:
        mock_dt.now.return_value = noon
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/ambient/snapshot")
    body = resp.json()
    assert body["suggested_mode"] == "desk"


async def test_snapshot_wind_down_at_night(_patched_app):
    app, _ = _patched_app
    night = datetime(2026, 4, 16, 22, 0, 0)
    with patch("api.routes.ambient.datetime") as mock_dt:
        mock_dt.now.return_value = night
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/ambient/snapshot")
    body = resp.json()
    assert body["suggested_mode"] == "wind_down"


# ── Next event ───────────────────────────────────────────────────────────────

async def test_next_event_graceful_without_calendar(_patched_app):
    app, mock = _patched_app
    mock.skill_registry.skills = {}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/ambient/next_event")
    body = resp.json()
    assert body["event"] is None
    assert "hint" in body
    assert "Calendar" in body["hint"]


async def test_next_event_returns_data_when_calendar_available(_patched_app):
    app, mock = _patched_app
    cal_mock = MagicMock()
    cal_mock.execute = AsyncMock(return_value={
        "success": True,
        "data": {"title": "Standup", "start": "09:00", "end": "09:15"},
    })
    mock.skill_registry.skills = {"calendar_lookup": cal_mock}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/ambient/next_event")
    body = resp.json()
    assert body["title"] == "Standup"


# ── Wind-Down ────────────────────────────────────────────────────────────────

async def test_wind_down_returns_expected_structure(_patched_app):
    app, _ = _patched_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/ambient/wind_down")
    assert resp.status_code == 200
    body = resp.json()
    assert "day_recap" in body
    assert "episodes" in body
    assert "sleep_prep" in body
    assert "journal_prompt" in body
    assert isinstance(body["day_recap"]["completed_tasks"], list)
    assert isinstance(body["episodes"], list)
    assert "time_to_bed_min" in body["sleep_prep"]
    assert "hints" in body["sleep_prep"]
    assert isinstance(body["journal_prompt"], str)
    assert len(body["journal_prompt"]) > 0


async def test_wind_down_bedtime_countdown(_patched_app):
    """At 10pm the bedtime countdown should be ~60 min."""
    app, _ = _patched_app
    evening = datetime(2026, 4, 16, 22, 0, 0)
    with patch("api.routes.ambient.datetime") as mock_dt:
        mock_dt.now.return_value = evening
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        mock_dt.fromisoformat = datetime.fromisoformat
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/ambient/wind_down")
    body = resp.json()
    assert body["sleep_prep"]["time_to_bed_min"] == 60


async def test_wind_down_with_episodes(_patched_app):
    app, mock = _patched_app
    mem_mock = MagicMock()
    mem_mock.episode_recent = AsyncMock(return_value=[
        {"id": "ep1", "summary": "Had a great standup", "ts": datetime.now().isoformat()},
        {"id": "ep2", "content": "Shipped the feature", "ts": datetime.now().isoformat()},
    ])
    mock.memory = mem_mock
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/ambient/wind_down")
    body = resp.json()
    assert len(body["episodes"]) == 2
    assert body["episodes"][0]["summary"] == "Had a great standup"
    assert body["episodes"][1]["summary"] == "Shipped the feature"


# ── Outfit helper ────────────────────────────────────────────────────────────

def test_outfit_from_temp_cold():
    from api.routes.ambient import _outfit_from_temp
    assert "coat" in _outfit_from_temp(-5)
    assert "coat" in _outfit_from_temp(0)
    assert "coat" in _outfit_from_temp(4)


def test_outfit_from_temp_mild():
    from api.routes.ambient import _outfit_from_temp
    assert "jacket" in _outfit_from_temp(10)
    assert "jacket" in _outfit_from_temp(14)


def test_outfit_from_temp_warm():
    from api.routes.ambient import _outfit_from_temp
    assert "light" in _outfit_from_temp(18)


def test_outfit_from_temp_hot():
    from api.routes.ambient import _outfit_from_temp
    assert "sunscreen" in _outfit_from_temp(30)
    assert "sunscreen" in _outfit_from_temp(35)


# ── Wake-Word ────────────────────────────────────────────────────────────────

async def test_wake_word_status_no_detector(_patched_app):
    """When wake_word is not initialised, report unsupported."""
    app, mock = _patched_app
    mock.wake_word = None
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/ambient/wake_word/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["supported"] is False
    assert body["enabled"] is False


async def test_wake_word_status_with_detector(_patched_app):
    app, mock = _patched_app
    ww = MagicMock()
    ww.enabled = True
    ww.phrase = "hey feral"
    mock.wake_word = ww
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/ambient/wake_word/status")
    body = resp.json()
    assert body["supported"] is True
    assert body["enabled"] is True
    assert body["phrase"] == "hey feral"


async def test_wake_word_toggle_503_without_detector(_patched_app):
    app, mock = _patched_app
    mock.wake_word = None
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post("/api/ambient/wake_word/toggle")
    assert resp.status_code == 503


async def test_wake_word_toggle_flips_state(_patched_app):
    app, mock = _patched_app
    ww = MagicMock()
    ww.enabled = False
    mock.wake_word = ww
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post("/api/ambient/wake_word/toggle")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
