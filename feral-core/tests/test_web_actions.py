"""
Tests for FERAL Web Actions skill (skills.impl.web_actions).

Covers confirmation card generation, endpoint dispatch, skill manifest,
and mocked browser automation flows.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from skills.impl.web_actions import (
    WebActionsSkill,
    _build_confirmation_card,
    get_web_actions_manifest,
)


class TestConfirmationCard:
    """Tests for the SDUI confirmation card builder."""

    def test_basic_card_structure(self) -> None:
        card = _build_confirmation_card(
            title="Test Purchase",
            items=[{"label": "Widget", "value": "$9.99"}],
            total="$9.99",
        )
        assert card["type"] == "Card"
        assert card["corner_radius"] == 16
        children = card["children"]
        assert children[0]["type"] == "Text"
        assert children[0]["value"] == "Test Purchase"
        button_row = children[-1]
        assert button_row["type"] == "HStack"
        labels = {c["label"] for c in button_row["children"]}
        assert labels == {"Confirm", "Cancel"}

    def test_card_with_extra_text(self) -> None:
        card = _build_confirmation_card(
            title="Book",
            items=[],
            extra_text="Safety notice",
        )
        texts = [c["value"] for c in card["children"] if c["type"] == "Text"]
        assert "Safety notice" in texts

    def test_card_action_ids_match(self) -> None:
        card = _build_confirmation_card(
            title="T",
            items=[],
            action_id="my_action",
        )
        button_row = card["children"][-1]
        ids = [c["action_id"] for c in button_row["children"]]
        assert ids == ["my_action_yes", "my_action_no"]


class TestWebActionsSkill:
    """Tests for WebActionsSkill endpoint dispatch and basic logic."""

    def test_skill_id(self) -> None:
        skill = WebActionsSkill()
        assert skill.skill_id == "web_actions"

    @pytest.mark.asyncio
    async def test_unknown_endpoint_returns_error(self) -> None:
        skill = WebActionsSkill()
        result = await skill.execute("nonexistent", {}, {})
        assert result["success"] is False
        assert "Unknown endpoint" in result["error"]

    @pytest.mark.asyncio
    async def test_search_and_compare_extracts_results(self) -> None:
        skill = WebActionsSkill()
        mock_browser = AsyncMock()
        mock_browser.connected = True
        mock_browser.navigate = AsyncMock(return_value={"success": True})
        mock_browser.wait = AsyncMock(return_value={"success": True})
        mock_browser.evaluate = AsyncMock(return_value={
            "result": json.dumps([
                {"title": "Widget A", "url": "https://a.com", "snippet": "Only $19.99"},
                {"title": "Widget B", "url": "https://b.com", "snippet": "From $24.50"},
            ])
        })
        mock_browser.screenshot = AsyncMock(return_value={"image_b64": "abc123"})
        skill._browser = mock_browser

        result = await skill.execute("search_and_compare", {"query": "widgets"}, {})
        assert result["success"] is True
        data = result["data"]
        assert data["query"] == "widgets"
        assert len(data["results"]) == 2
        assert "$19.99" in data["results"][0]["prices_found"]

    @pytest.mark.asyncio
    async def test_make_purchase_is_refused_with_no_spend_limit(self, tmp_path, monkeypatch) -> None:
        """A stock install has no commerce caps, and unset means refuse.

        Treating "no limit configured" as "no limit" is how an errand
        agent spends money nobody authorised. See security/commerce.py.
        """
        from security.commerce import PurchaseAudit
        from skills.impl import web_actions

        monkeypatch.setattr(web_actions, "_AUDIT", PurchaseAudit(db_path=str(tmp_path / "p.db")))
        skill = WebActionsSkill()
        mock_browser = AsyncMock()
        mock_browser.connected = True
        mock_browser.navigate = AsyncMock(return_value={"success": True})
        mock_browser.wait = AsyncMock(return_value={"success": True})
        mock_browser.get_page_info = AsyncMock(return_value={"title": "Cool Gadget", "url": "https://shop.com/gadget"})
        mock_browser.evaluate = AsyncMock(return_value={"result": json.dumps(["$49.99"])})
        skill._browser = mock_browser

        result = await skill.execute("make_purchase", {"url": "https://shop.com/gadget"}, {})
        data = result["data"]
        assert data["purchased"] is False
        assert data["refused"] is True
        assert data["reason"] == "no_cap_configured"
        assert "sdui_card" not in data

    @pytest.mark.asyncio
    async def test_make_purchase_returns_sdui_card(self, tmp_path, monkeypatch) -> None:
        from decimal import Decimal

        from security.commerce import PurchaseAudit, SpendCaps
        from skills.impl import web_actions

        monkeypatch.setattr(web_actions, "_AUDIT", PurchaseAudit(db_path=str(tmp_path / "p.db")))
        monkeypatch.setattr(web_actions, "load_caps", lambda: SpendCaps(
            currency="USD", per_transaction_max=Decimal("100"), per_day_max=Decimal("500")))
        skill = WebActionsSkill()
        mock_browser = AsyncMock()
        mock_browser.connected = True
        mock_browser.navigate = AsyncMock(return_value={"success": True})
        mock_browser.wait = AsyncMock(return_value={"success": True})
        mock_browser.get_page_info = AsyncMock(return_value={"title": "Cool Gadget", "url": "https://shop.com/gadget"})
        mock_browser.evaluate = AsyncMock(return_value={"result": json.dumps(["$49.99"])})
        mock_browser.screenshot = AsyncMock(return_value={"image_b64": "screenshot"})
        skill._browser = mock_browser

        result = await skill.execute("make_purchase", {"url": "https://shop.com/gadget", "item_description": "Gadget"}, {})
        assert result["success"] is True
        data = result["data"]
        assert data["purchased"] is False
        assert data["awaiting_confirmation"] is True
        assert data["sdui_card"]["type"] == "Card"
        assert data["total_display"] == "$49.99"

    @pytest.mark.asyncio
    async def test_fill_web_form_reports_results(self) -> None:
        skill = WebActionsSkill()
        mock_browser = AsyncMock()
        mock_browser.connected = True
        mock_browser.navigate = AsyncMock(return_value={"success": True})
        mock_browser.wait = AsyncMock(return_value={"success": True})
        mock_browser.fill_form = AsyncMock(return_value={
            "success": True, "filled": ["#name", "#email"], "failed": {},
        })
        mock_browser.screenshot = AsyncMock(return_value={"image_b64": "img"})
        skill._browser = mock_browser

        result = await skill.execute(
            "fill_web_form",
            {"url": "https://example.com/form", "form_data": {"#name": "Alice", "#email": "a@b.com"}},
            {},
        )
        assert result["success"] is True
        assert result["data"]["filled"] is True

    @pytest.mark.asyncio
    async def test_book_reservation_returns_confirmation(self) -> None:
        skill = WebActionsSkill()
        mock_browser = AsyncMock()
        mock_browser.connected = True
        mock_browser.navigate = AsyncMock(return_value={"success": True})
        mock_browser.wait = AsyncMock(return_value={"success": True})
        mock_browser.fill_form = AsyncMock(return_value={"success": True, "filled": [], "failed": {}})
        mock_browser.get_page_info = AsyncMock(return_value={"title": "OpenTable", "url": "https://opentable.com"})
        mock_browser.screenshot = AsyncMock(return_value={"image_b64": "pic"})
        skill._browser = mock_browser

        result = await skill.execute(
            "book_reservation",
            {"service_url": "https://opentable.com", "details": {"name": "Alice", "date": "2026-05-01", "guests": "2"}},
            {},
        )
        assert result["success"] is True
        assert result["data"]["booked"] is False
        assert result["data"]["awaiting_confirmation"] is True
        card = result["data"]["sdui_card"]
        assert card["type"] == "Card"

    @pytest.mark.asyncio
    async def test_extract_page_data_returns_text(self) -> None:
        skill = WebActionsSkill()
        mock_browser = AsyncMock()
        mock_browser.connected = True
        mock_browser.navigate = AsyncMock(return_value={"success": True})
        mock_browser.wait = AsyncMock(return_value={"success": True})
        mock_browser.evaluate = AsyncMock(return_value={"result": "Some page content here"})
        mock_browser.get_page_info = AsyncMock(return_value={"title": "Info Page", "url": "https://example.com"})
        skill._browser = mock_browser

        result = await skill.execute(
            "extract_page_data",
            {"url": "https://example.com", "what_to_extract": "contact info"},
            {},
        )
        assert result["success"] is True
        assert result["data"]["extracted"] is True
        assert "page content" in result["data"]["page_text"]


class TestWebActionsManifest:
    """Tests for the manifest helper."""

    def test_manifest_shape(self) -> None:
        m = get_web_actions_manifest()
        assert m["skill_id"] == "web_actions"
        assert m["safety_level"] == "CONFIRM"
        endpoint_ids = {e["id"] for e in m["endpoints"]}
        assert endpoint_ids == {
            "search_and_compare",
            "fill_web_form",
            "make_purchase",
            "book_reservation",
            "extract_page_data",
        }

    def test_all_endpoints_have_params(self) -> None:
        m = get_web_actions_manifest()
        for ep in m["endpoints"]:
            assert "params" in ep
            assert isinstance(ep["params"], list)
