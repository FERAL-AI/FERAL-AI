"""Places lookup: honest about location, and never unattributed.

"Find a café near me" had no answer path at all. This skill adds one,
with three rules that matter more than the lookup itself:

* location is opt-in, so "I don't know where you are" is an answer to
  give, not an error to raise;
* rating and open-now are reported only when Google returns them, because
  a café with no rating must not read as a café rated zero;
* the Maps terms allow grounded output to reach a user only with its
  source links alongside, and the glasses have no screen, so every result
  carries its sources for the phone to render.
"""
from __future__ import annotations

import json

import pytest

from skills.impl.places import PlacesSkill, _haversine_m

VAULT = {"places": "test-maps-key"}

# Dubai Marina, roughly.
ORIGIN = {"lat": 25.0772, "lon": 55.1385}


class _Conn:
    """Stands in for the Grounding Lite MCP connection."""

    is_connected = True

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def call_tool(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        return {"content": [{"type": "text", "text": json.dumps(self.payload)}]}


def _skill(monkeypatch, payload=None, *, location=ORIGIN, policy_ok=True):
    skill = PlacesSkill()
    conn = _Conn(payload or {"places": []})

    async def _connection(api_key):
        return conn

    monkeypatch.setattr(skill, "_connection", _connection)
    monkeypatch.setattr(PlacesSkill, "_policy_allows", staticmethod(lambda: policy_ok))
    monkeypatch.setattr(PlacesSkill, "_origin", staticmethod(
        lambda args: dict(location, source="device") if location else None))
    return skill, conn


def _places_payload():
    return {"places": [
        {"displayName": {"text": "Nero"}, "formattedAddress": "Marina Walk",
         "location": {"latitude": 25.0800, "longitude": 55.1400}, "rating": 4.3,
         "currentOpeningHours": {"openNow": True},
         "googleMapsLinks": {"placeUrl": "https://maps.google.com/?cid=1"}},
        {"displayName": {"text": "Tim Hortons"}, "formattedAddress": "JBR",
         "location": {"latitude": 25.0900, "longitude": 55.1500},
         "googleMapsLinks": {"placeUrl": "https://maps.google.com/?cid=2"}},
    ]}


@pytest.mark.asyncio
async def test_missing_key_is_reported_as_unconfigured(monkeypatch):
    skill, _ = _skill(monkeypatch)
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    out = await skill.execute("find_places", {"query": "cafe"}, {})
    assert out["ok"] is False
    assert out["reason"] == "not_configured"


@pytest.mark.asyncio
async def test_no_location_is_a_question_not_an_error(monkeypatch):
    skill, conn = _skill(monkeypatch, location=None)
    out = await skill.execute("find_places", {"query": "cafe"}, VAULT)
    assert out["ok"] is False
    assert out["reason"] == "location_unknown"
    assert "share it" in out["message"].lower() or "share" in out["message"].lower()
    assert conn.calls == [], "must not call the API without an origin"


@pytest.mark.asyncio
async def test_policy_can_block_the_remote_server(monkeypatch):
    skill, conn = _skill(monkeypatch, policy_ok=False)
    out = await skill.execute("find_places", {"query": "cafe"}, VAULT)
    assert out["reason"] == "blocked_by_policy"
    assert conn.calls == []


@pytest.mark.asyncio
async def test_results_carry_distance_and_sort_by_it(monkeypatch):
    skill, conn = _skill(monkeypatch, _places_payload())
    out = await skill.execute("find_places", {"query": "cafe"}, VAULT)
    assert out["ok"] is True
    names = [p["name"] for p in out["places"]]
    assert names == ["Nero", "Tim Hortons"]
    assert out["places"][0]["distance_m"] < out["places"][1]["distance_m"]
    # Distance is measured from the wearer, which only the brain knows.
    expected = round(_haversine_m(ORIGIN["lat"], ORIGIN["lon"], 25.0800, 55.1400))
    assert out["places"][0]["distance_m"] == expected


@pytest.mark.asyncio
async def test_rating_and_open_now_are_absent_not_zero(monkeypatch):
    skill, _ = _skill(monkeypatch, _places_payload())
    out = await skill.execute("find_places", {"query": "cafe"}, VAULT)
    nero, tims = out["places"]
    assert (nero["rating"], nero["open_now"]) == (4.3, True)
    assert tims["rating"] is None and tims["open_now"] is None


@pytest.mark.asyncio
async def test_every_result_carries_its_google_sources(monkeypatch):
    skill, _ = _skill(monkeypatch, _places_payload())
    out = await skill.execute("find_places", {"query": "cafe"}, VAULT)
    assert out["attribution_required"] is True
    assert out["unattributed"] == []
    assert out["places"][0]["sources"][0]["url"] == "https://maps.google.com/?cid=1"


@pytest.mark.asyncio
async def test_a_place_with_no_source_is_named_so_the_phone_can_refuse_it(monkeypatch):
    payload = {"places": [{"displayName": {"text": "Unlinked Cafe"},
                           "formattedAddress": "Somewhere"}]}
    skill, _ = _skill(monkeypatch, payload)
    out = await skill.execute("find_places", {"query": "cafe"}, VAULT)
    assert out["unattributed"] == ["Unlinked Cafe"]


@pytest.mark.asyncio
async def test_the_search_is_biased_to_the_wearer(monkeypatch):
    skill, conn = _skill(monkeypatch, _places_payload())
    await skill.execute("find_places", {"query": "pharmacy", "radius_m": 800}, VAULT)
    tool, args = conn.calls[0]
    assert tool == "search_places"
    assert args["text_query"] == "pharmacy"
    circle = args["location_bias"]["circle"]
    assert circle["center"] == {"latitude": ORIGIN["lat"], "longitude": ORIGIN["lon"]}
    assert circle["radius_meters"] == 800


@pytest.mark.asyncio
async def test_an_unknown_endpoint_is_refused(monkeypatch):
    skill, _ = _skill(monkeypatch)
    out = await skill.execute("book_table", {"query": "x"}, VAULT)
    assert out["ok"] is False
