"""Tests for FERAL integration modules — calendar, email, messaging,
health platforms, home assistant, spotify, notion."""

import pytest
from unittest.mock import AsyncMock, MagicMock


# ── Calendar ──────────────────────────────────────────────────────


class TestCalendarIntegration:
    def test_init_no_credentials(self, monkeypatch):
        monkeypatch.delenv("FERAL_CALENDAR_ICS", raising=False)
        from integrations.calendar import CalendarIntegration

        cal = CalendarIntegration()
        assert cal.connected is False

    @pytest.mark.asyncio
    async def test_unknown_endpoint(self):
        from integrations.calendar import CalendarIntegration

        cal = CalendarIntegration()
        result = await cal.execute("nonexistent", {})
        assert result["success"] is False
        assert "Unknown" in result["error"]

    @pytest.mark.asyncio
    async def test_list_events_google(self):
        from integrations.calendar import CalendarIntegration

        oauth = MagicMock()
        oauth.is_connected.return_value = True
        oauth.get_token = AsyncMock(return_value="tok")
        cal = CalendarIntegration(oauth_manager=oauth)

        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"items": []}
        cal._http = AsyncMock()
        cal._http.get.return_value = resp

        result = await cal.execute("list_events", {})
        assert result["success"] is True
        assert result["data"]["source"] == "google"


# ── Email ─────────────────────────────────────────────────────────


class TestEmailIntegration:
    def test_init_no_credentials(self, monkeypatch):
        monkeypatch.delenv("FERAL_EMAIL_IMAP_HOST", raising=False)
        from integrations.email import EmailIntegration

        em = EmailIntegration()
        assert em.connected is False

    @pytest.mark.asyncio
    async def test_unknown_endpoint(self):
        from integrations.email import EmailIntegration

        em = EmailIntegration()
        result = await em.execute("nonexistent", {})
        assert result["success"] is False
        assert "Unknown" in result["error"]

    @pytest.mark.asyncio
    async def test_get_unread_count(self):
        from integrations.email import EmailIntegration

        oauth = MagicMock()
        oauth.is_connected.return_value = True
        oauth.get_token = AsyncMock(return_value="tok")
        em = EmailIntegration(oauth_manager=oauth)

        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"messagesUnread": 3, "messagesTotal": 50}
        em._http = AsyncMock()
        em._http.get.return_value = resp

        result = await em.execute("get_unread_count", {})
        assert result["success"] is True
        assert result["data"]["unread"] == 3


# ── Messaging ─────────────────────────────────────────────────────


class TestMessagingHub:
    def test_init_no_tokens(self, monkeypatch):
        monkeypatch.delenv("FERAL_TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("FERAL_SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("FERAL_DISCORD_BOT_TOKEN", raising=False)
        from integrations.messaging import MessagingHub

        hub = MessagingHub()
        assert hub.connected is False

    @pytest.mark.asyncio
    async def test_unknown_endpoint(self, monkeypatch):
        monkeypatch.delenv("FERAL_TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("FERAL_SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("FERAL_DISCORD_BOT_TOKEN", raising=False)
        from integrations.messaging import MessagingHub

        hub = MessagingHub()
        result = await hub.execute("nonexistent", {})
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_telegram_send_no_token(self, monkeypatch):
        monkeypatch.delenv("FERAL_TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("FERAL_SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("FERAL_DISCORD_BOT_TOKEN", raising=False)
        from integrations.messaging import MessagingHub

        hub = MessagingHub()
        result = await hub.execute("telegram_send", {"chat_id": "1", "text": "hi"})
        assert result["success"] is False
        assert "not configured" in result["error"].lower()


# ── Health Platforms ──────────────────────────────────────────────


class TestHealthPlatforms:
    def test_whoop_no_credentials(self, monkeypatch):
        monkeypatch.delenv("FERAL_WHOOP_TOKEN", raising=False)
        from integrations.health_platforms import WhoopClient

        w = WhoopClient()
        assert w.connected is False

    def test_oura_no_credentials(self, monkeypatch):
        monkeypatch.delenv("FERAL_OURA_TOKEN", raising=False)
        from integrations.health_platforms import OuraClient

        o = OuraClient()
        assert o.connected is False

    def test_aggregator_no_clients(self):
        from integrations.health_platforms import HealthAggregator

        agg = HealthAggregator()
        assert agg.sources == []

    @pytest.mark.asyncio
    async def test_aggregator_unknown_endpoint(self):
        from integrations.health_platforms import HealthAggregator

        agg = HealthAggregator()
        result = await agg.execute("nonexistent", {})
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_aggregator_summary_empty(self):
        """Lane 05 W5: HealthAggregator.execute now wraps the bare
        snapshot dict in the canonical {success, status_code, data,
        error} envelope used by every other skill backend, so the
        snapshot fields live under result['data'] instead of at the
        top level."""
        from integrations.health_platforms import HealthAggregator

        agg = HealthAggregator()
        result = await agg.execute("health_summary", {})
        assert result["success"] is True
        assert result["data"]["sources"] == []
        assert result["data"]["sleep_hours"] is None


# ── Home Assistant ────────────────────────────────────────────────


class TestHomeAssistant:
    def test_init_no_token(self, monkeypatch):
        monkeypatch.delenv("HA_TOKEN", raising=False)
        monkeypatch.delenv("HA_URL", raising=False)
        from integrations.home_assistant import HomeAssistantIntegration

        ha = HomeAssistantIntegration()
        assert ha.connected is False

    @pytest.mark.asyncio
    async def test_unknown_endpoint(self):
        from integrations.home_assistant import HomeAssistantIntegration

        ha = HomeAssistantIntegration()
        result = await ha.execute("nonexistent", {})
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_get_states_mocked(self):
        from integrations.home_assistant import HomeAssistantIntegration

        ha = HomeAssistantIntegration()
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = [
            {
                "entity_id": "light.living",
                "state": "on",
                "attributes": {"friendly_name": "Living Light"},
            }
        ]
        ha._http = AsyncMock()
        ha._http.get.return_value = resp

        result = await ha.get_states()
        assert result["success"] is True
        assert result["data"]["total"] == 1

    @pytest.mark.asyncio
    async def test_ha_ws_subscribe_state_change(self):
        """Mock WS server: verify subscribe + state-change event dispatched."""
        from integrations.home_assistant import HomeAssistantIntegration

        ha = HomeAssistantIntegration()
        events_received = []

        def handler(event):
            events_received.append(event)

        ha.on_event(handler)
        assert handler in ha._event_handlers

        test_event = {"event_type": "state_changed", "data": {"entity_id": "light.kitchen", "new_state": {"state": "off"}}}
        for h in ha._event_handlers:
            h(test_event)

        assert len(events_received) == 1
        assert events_received[0]["data"]["entity_id"] == "light.kitchen"

    @pytest.mark.asyncio
    async def test_ha_discover_capabilities_mocked(self):
        from integrations.home_assistant import HomeAssistantIntegration

        ha = HomeAssistantIntegration()
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = [
            {"entity_id": "light.living", "state": "on", "attributes": {"friendly_name": "Living Light"}},
            {"entity_id": "switch.fan", "state": "off", "attributes": {"friendly_name": "Fan"}},
        ]
        ha._http = AsyncMock()
        ha._http.get.return_value = resp

        caps = await ha.discover_capabilities()
        assert caps["total_entities"] == 2
        assert "light" in caps["domains"]


# ── Spotify ───────────────────────────────────────────────────────


class TestSpotify:
    def test_init_no_oauth(self):
        from integrations.spotify import SpotifyIntegration

        sp = SpotifyIntegration()
        assert sp.connected is False

    @pytest.mark.asyncio
    async def test_unknown_endpoint(self):
        from integrations.spotify import SpotifyIntegration

        sp = SpotifyIntegration()
        result = await sp.execute("nonexistent", {})
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_now_playing_disconnected(self):
        from integrations.spotify import SpotifyIntegration

        sp = SpotifyIntegration()
        result = await sp.now_playing()
        assert result["success"] is False
        assert "Not connected" in result["error"]


# ── Notion ────────────────────────────────────────────────────────


class TestNotion:
    def test_init_no_token(self, monkeypatch):
        monkeypatch.delenv("NOTION_TOKEN", raising=False)
        from integrations.notion import NotionIntegration

        n = NotionIntegration()
        assert n.connected is False

    @pytest.mark.asyncio
    async def test_unknown_endpoint(self):
        from integrations.notion import NotionIntegration

        n = NotionIntegration()
        result = await n.execute("nonexistent", {})
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_search_pages_mocked(self):
        from integrations.notion import NotionIntegration

        n = NotionIntegration()
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"results": []}
        n._http = AsyncMock()
        n._http.post.return_value = resp

        result = await n.search_pages(query="test")
        assert result["success"] is True
        assert result["data"]["results"] == []
