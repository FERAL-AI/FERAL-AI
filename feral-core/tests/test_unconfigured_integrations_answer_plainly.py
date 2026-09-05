"""An unconfigured integration says so, without dialling anything.

Two failures the 2026-09-04 skills audit found, both of which reached the
model as a technical error about something other than the actual problem:

* Notion sent ``Authorization: Bearer `` with an empty token, so httpx
  refused the header and every endpoint answered ``Illegal header value
  b'Bearer '``.
* Home Assistant defaults to ``homeassistant.local:8123``, so with nothing
  configured every call spent about five seconds failing to resolve that
  name and returned ``[Errno 8] nodename nor servname provided``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from integrations.home_assistant import (  # noqa: E402
    NOT_CONFIGURED_ERROR,
    HomeAssistantIntegration,
)
from integrations.notion import NOT_CONNECTED_ERROR, NotionIntegration  # noqa: E402


def _oauth(token: str = ""):
    mgr = MagicMock()
    mgr.get_token = AsyncMock(return_value=token)
    # No vault. A bare MagicMock answers every attribute with another
    # MagicMock, and ``resolve_base_url`` would then read a stored Home
    # Assistant URL that is a mock repr.
    mgr._vault = None
    return mgr


class _ExplodingClient:
    """Any network use at all is a test failure."""

    def __getattr__(self, name):
        raise AssertionError(f"an unconfigured integration must not call {name}")


# ── Notion ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint, args",
    [
        ("search_pages", {"query": "roadmap"}),
        ("read_page", {"page_id": "abc"}),
        ("create_page", {"parent_id": "abc", "title": "t"}),
        ("update_page", {"page_id": "abc"}),
        ("query_database", {"database_id": "db"}),
        ("create_database_entry", {"database_id": "db", "properties": {}}),
    ],
)
async def test_notion_without_a_token_answers_not_connected(
    monkeypatch, endpoint, args,
):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    notion = NotionIntegration(oauth_manager=_oauth(""))
    notion._http = _ExplodingClient()

    result = await notion.execute(endpoint, args)

    assert result["success"] is False
    assert result["error"] == NOT_CONNECTED_ERROR
    assert "Bearer" not in result["error"]
    assert "Illegal header value" not in result["error"]


@pytest.mark.asyncio
async def test_notion_never_builds_a_client_with_an_empty_bearer(monkeypatch):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    notion = NotionIntegration(oauth_manager=_oauth(""))

    assert await notion._ensure_client() is not None
    assert notion._http is None, (
        "a client built with an empty token is the bug; it also poisons "
        "every later call, because the header is cached for the process"
    )


@pytest.mark.asyncio
async def test_notion_picks_up_a_token_authorised_after_the_first_call(monkeypatch):
    """The client used to be built once and kept forever."""
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    oauth = _oauth("")
    notion = NotionIntegration(oauth_manager=oauth)

    assert await notion._ensure_client() is not None

    oauth.get_token = AsyncMock(return_value="secret_ntn")
    assert await notion._ensure_client() is None
    assert notion._http.headers["Authorization"] == "Bearer secret_ntn"

    oauth.get_token = AsyncMock(return_value="secret_refreshed")
    assert await notion._ensure_client() is None
    assert notion._http.headers["Authorization"] == "Bearer secret_refreshed"

    await notion.close()


# ── Home Assistant ───────────────────────────────────────────────────


@pytest.fixture()
def unconfigured_ha(monkeypatch):
    for env_key in ("HA_TOKEN", "SUPERVISOR_TOKEN", "FERAL_HA_URL", "HA_URL"):
        monkeypatch.delenv(env_key, raising=False)
    ha = HomeAssistantIntegration(oauth_manager=_oauth(""))
    ha._http = _ExplodingClient()
    return ha


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint, args",
    [
        ("get_states", {}),
        ("get_entities", {"domain": "light"}),
        ("get_entity_state", {"entity_id": "light.kitchen"}),
        ("call_service", {"domain": "light", "service": "turn_on"}),
        ("set_light", {"entity_id": "light.kitchen", "brightness": 100}),
        ("toggle_entity", {"entity_id": "light.kitchen"}),
        ("get_automations", {}),
        ("trigger_automation", {"entity_id": "automation.night"}),
        ("vacuum_start", {"entity_id": "vacuum.roomba"}),
    ],
)
async def test_home_assistant_without_a_token_short_circuits(
    unconfigured_ha, endpoint, args,
):
    result = await unconfigured_ha.execute(endpoint, args)

    assert result["success"] is False
    assert result["error"] == NOT_CONFIGURED_ERROR
    assert "nodename" not in result["error"]
    assert "Settings > Integrations" in result["error"]


@pytest.mark.asyncio
async def test_home_assistant_does_not_dial_the_default_host(unconfigured_ha):
    """The whole point: no DNS lookup for homeassistant.local."""
    assert unconfigured_ha.base_url == "http://homeassistant.local:8123"

    guard = await unconfigured_ha._ensure_client()

    assert guard is not None
    assert isinstance(unconfigured_ha._http, _ExplodingClient), (
        "the client must not be replaced, i.e. no connection was attempted"
    )


@pytest.mark.asyncio
async def test_home_assistant_with_a_token_builds_its_client(monkeypatch):
    for env_key in ("SUPERVISOR_TOKEN", "FERAL_HA_URL", "HA_URL"):
        monkeypatch.delenv(env_key, raising=False)
    monkeypatch.setenv("HA_TOKEN", "llat_abc")
    ha = HomeAssistantIntegration(oauth_manager=_oauth(""))

    assert await ha._ensure_client() is None
    assert ha._http.headers["Authorization"] == "Bearer llat_abc"

    await ha._http.aclose()
