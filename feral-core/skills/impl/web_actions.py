"""
FERAL Web Actions Skill
========================
Higher-level browser automation for real-world tasks:
purchases, reservations, bookings, price comparison.

Purchase support is a read-only price preview, not checkout or payment.
All flows stop before financial commitment.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from uuid import uuid4

from skills.base import BaseSkill
from security.commerce import (
    PurchaseAudit, evaluate, load_caps, merchant_from_url, parse_money,
)
from skills.impl import register_skill

logger = logging.getLogger("feral.skills.web_actions")

_PRICE_RE = re.compile(r"\$[\d,]+\.?\d*|\d+[\.,]\d{2}\s*(?:USD|EUR|GBP)")


def _build_confirmation_card(
    *,
    title: str,
    items: list[dict[str, str]],
    total: str = "",
    action_id: str = "",
    extra_text: str = "",
) -> dict:
    """Build an SDUI confirmation card with Confirm / Cancel buttons."""
    children: list[dict] = [
        {"type": "Text", "value": title, "style": "headline"},
    ]
    if extra_text:
        children.append({"type": "Text", "value": extra_text, "style": "caption"})
    children.append({"type": "Divider"})
    for item in items:
        row_children = [{"type": "Text", "value": item.get("label", ""), "style": "body"}]
        if item.get("value"):
            row_children.append({"type": "Text", "value": item["value"], "style": "subtitle"})
        children.append({"type": "HStack", "children": row_children})
    if total:
        children.append({"type": "Divider"})
        children.append(
            {"type": "HStack", "children": [
                {"type": "Text", "value": "Total", "style": "subtitle"},
                {"type": "Text", "value": total, "style": "headline", "color": "#e17055"},
            ]}
        )
    action_id = action_id or f"confirm_{uuid4().hex[:8]}"
    children.append(
        {"type": "HStack", "spacing": 12, "children": [
            {"type": "Button", "label": "Confirm", "action_id": f"{action_id}_yes", "color": "#00b894"},
            {"type": "Button", "label": "Cancel", "action_id": f"{action_id}_no", "color": "#e17055"},
        ]}
    )
    return {"type": "Card", "corner_radius": 16, "padding": 16, "children": children}


_AUDIT: PurchaseAudit | None = None


def _purchase_audit() -> PurchaseAudit:
    """Process-wide append-only purchase trail, opened on first use."""
    global _AUDIT
    if _AUDIT is None:
        _AUDIT = PurchaseAudit()
    return _AUDIT


@register_skill
class WebActionsSkill(BaseSkill):
    """Commerce & booking browser automation with SDUI confirmation gates."""

    def __init__(self):
        super().__init__(skill_id="web_actions")
        self._browser: Any = None

    def set_browser(self, browser: Any):
        self._browser = browser

    async def _ensure_browser(self) -> Any:
        if self._browser is None:
            from skills.impl.browser_use import BrowserController
            self._browser = BrowserController()
        if not self._browser.connected:
            await self._browser.initialize()
        return self._browser

    async def execute(self, endpoint_id: str, args: dict[str, Any], vault: dict[str, str]) -> dict[str, Any]:
        dispatch = {
            "search_and_compare": self.search_and_compare,
            "fill_web_form": self.fill_web_form,
            "make_purchase": self.make_purchase,
            "book_reservation": self.book_reservation,
            "extract_page_data": self.extract_page_data,
        }
        fn = dispatch.get(endpoint_id)
        if not fn:
            return {"success": False, "status_code": 400, "data": None, "error": f"Unknown endpoint: {endpoint_id}"}
        try:
            data = await fn(**args)
            return {"success": True, "status_code": 200, "data": data, "error": None}
        except Exception as exc:
            logger.exception("web_actions.%s failed", endpoint_id)
            return {"success": False, "status_code": 500, "data": None, "error": str(exc)}

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    async def search_and_compare(self, query: str, max_results: int = 5, **_kw: Any) -> dict:
        """Search the web, extract prices/reviews, return structured comparison."""
        browser = await self._ensure_browser()

        search_url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
        await browser.navigate(search_url)
        await browser.wait(2000)

        js_extract = """
        (function() {
            var results = [];
            document.querySelectorAll('.g').forEach(function(el, i) {
                if (i >= %d) return;
                var a = el.querySelector('a');
                var h3 = el.querySelector('h3');
                var snippet = el.querySelector('.VwiC3b, .lEBKkf');
                if (a && h3) {
                    results.push({
                        title: h3.textContent || '',
                        url: a.href || '',
                        snippet: snippet ? snippet.textContent : ''
                    });
                }
            });
            return JSON.stringify(results);
        })()
        """ % max(1, min(max_results, 10))

        eval_result = await browser.evaluate(js_extract)
        raw = eval_result.get("result", "[]")
        try:
            results = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            results = []

        for item in results:
            prices = _PRICE_RE.findall(item.get("snippet", ""))
            item["prices_found"] = prices

        screenshot = await browser.screenshot()

        return {
            "query": query,
            "results": results[:max_results],
            "screenshot_b64": screenshot.get("image_b64"),
        }

    async def fill_web_form(self, url: str, form_data: dict[str, str], **_kw: Any) -> dict:
        """Navigate to URL, fill form fields, screenshot for confirmation."""
        browser = await self._ensure_browser()

        nav = await browser.navigate(url)
        if not nav.get("success"):
            return {"filled": False, "error": nav.get("error", "Navigation failed")}

        await browser.wait(1500)
        fill_result = await browser.fill_form(form_data)
        screenshot = await browser.screenshot()

        return {
            "filled": fill_result.get("success", False),
            "fields_ok": fill_result.get("filled", []),
            "fields_failed": fill_result.get("failed", {}),
            "screenshot_b64": screenshot.get("image_b64"),
            "url": url,
        }

    async def make_purchase(self, url: str, item_description: str = "", **_kw: Any) -> dict:
        """Preview an unverified observed price; checkout is unavailable."""
        browser = await self._ensure_browser()

        nav = await browser.navigate(url)
        if not nav.get("success"):
            return {"purchased": False, "error": nav.get("error", "Navigation failed")}
        await browser.wait(2000)

        page_info = await browser.get_page_info()
        page_title = page_info.get("title", "")

        price_js = """
        (function() {
            var candidates = document.querySelectorAll(
                '[class*="price"], [data-price], [itemprop="price"], .a-price .a-offscreen'
            );
            var prices = [];
            candidates.forEach(function(el) {
                var t = el.textContent.trim();
                if (t && /\\$|\\d/.test(t)) prices.push(t.substring(0, 50));
            });
            return JSON.stringify(prices.slice(0, 5));
        })()
        """
        eval_result = await browser.evaluate(price_js)
        raw_prices = []
        try:
            parsed = eval_result.get("result", "[]")
            raw_prices = json.loads(parsed) if isinstance(parsed, str) else (parsed or [])
        except (json.JSONDecodeError, TypeError):
            pass

        observed_price_display = raw_prices[0] if raw_prices else "Price not found"

        # The amount exists for the first time HERE. make_purchase takes
        # no price: it scrapes one. Keep the existing spend-cap screen
        # before showing a preview, without claiming this observed price
        # is a cart total or creating authorization to spend.
        #
        # No default currency is passed: a bare "24.00" with no symbol is
        # an unknown currency, and guessing the operator's own is exactly
        # the assumption this module refuses to make elsewhere.
        caps = load_caps()
        money = parse_money(observed_price_display)
        merchant = merchant_from_url(page_info.get("url", url) or url)
        audit = _purchase_audit()
        verdict = evaluate(money, merchant, caps, audit=audit)
        if not verdict.allowed:
            audit.record(
                outcome="refused_cap", merchant=merchant, money=money,
                tool="web_actions__make_purchase", reason=verdict.code,
            )
            return {
                "purchased": False,
                "awaiting_confirmation": False,
                "refused": True,
                "reason": verdict.code,
                "error": verdict.reason,
                "merchant": merchant,
                "observed_price_display": observed_price_display,
                "price_verified": False,
                "page_title": page_title,
            }
        audit.record(
            outcome="offered", merchant=merchant, money=money,
            tool="web_actions__make_purchase", reason=verdict.code,
        )

        screenshot = await browser.screenshot()

        # No checkout or registered approval exists for this preview. A
        # generated Confirm/Cancel button would have no action to resolve.
        preview_card = {
            "type": "Card", "corner_radius": 16, "padding": 16,
            "children": [
                {"type": "Text", "value": "Product Preview", "style": "headline"},
                {"type": "Text", "style": "body", "value": (
                    "Preview only. No purchase has been made. Checkout is unavailable in this tool."
                )},
                {"type": "Divider"},
                {"type": "Text", "value": item_description or page_title, "style": "subtitle"},
                {"type": "Text", "value": page_info.get("url", url), "style": "caption"},
                {"type": "HStack", "children": [
                    {"type": "Text", "value": "Observed price (unverified)", "style": "body"},
                    {"type": "Text", "value": observed_price_display, "style": "subtitle"},
                ]},
                {"type": "Text", "style": "caption", "value": (
                    "This page price is not a verified checkout total. Items, variants, "
                    "taxes and shipping have not been verified."
                )},
            ],
        }

        return {
            "purchased": False,
            "awaiting_confirmation": False,
            "preview_only": True,
            "checkout_available": False,
            "page_title": page_title,
            "detected_prices": raw_prices,
            "observed_price_display": observed_price_display,
            "price_verified": False,
            # Parsed observed price for display/cap screening, never an
            # authorized or verified checkout amount.
            "merchant": merchant,
            "amount": str(money.amount) if money else "",
            "currency": money.currency if money else "",
            "sdui_card": preview_card,
            "screenshot_b64": screenshot.get("image_b64"),
        }

    async def book_reservation(self, service_url: str, details: dict[str, str], **_kw: Any) -> dict:
        """Fill booking form, return confirmation card — does NOT submit."""
        browser = await self._ensure_browser()

        nav = await browser.navigate(service_url)
        if not nav.get("success"):
            return {"booked": False, "error": nav.get("error", "Navigation failed")}
        await browser.wait(2000)

        if details:
            await browser.fill_form(details)
            await browser.wait(1000)

        page_info = await browser.get_page_info()
        screenshot = await browser.screenshot()

        card_items = [{"label": k, "value": str(v)} for k, v in details.items()]
        card_items.insert(0, {"label": "Service", "value": page_info.get("title", service_url)})

        confirmation_card = _build_confirmation_card(
            title="Confirm Reservation",
            items=card_items,
            extra_text="FERAL will NOT submit this reservation without your explicit confirmation.",
        )

        return {
            "booked": False,
            "awaiting_confirmation": True,
            "service": page_info.get("title", service_url),
            "details_filled": details,
            "sdui_card": confirmation_card,
            "screenshot_b64": screenshot.get("image_b64"),
        }

    async def extract_page_data(self, url: str, what_to_extract: str = "", **_kw: Any) -> dict:
        """Navigate and extract structured data from the page content."""
        browser = await self._ensure_browser()

        nav = await browser.navigate(url)
        if not nav.get("success"):
            return {"extracted": False, "error": nav.get("error", "Navigation failed")}
        await browser.wait(2000)

        content_js = """
        (function() {
            var body = document.body.innerText || '';
            return body.substring(0, 8000);
        })()
        """
        eval_result = await browser.evaluate(content_js)
        page_text = eval_result.get("result", "")

        page_info = await browser.get_page_info()

        return {
            "extracted": True,
            "url": url,
            "title": page_info.get("title", ""),
            "what_to_extract": what_to_extract,
            "page_text": page_text[:8000] if isinstance(page_text, str) else "",
        }


def get_web_actions_manifest() -> dict:
    """Return the skill manifest for web actions."""
    return {
        "skill_id": "web_actions",
        "name": "Web Actions",
        "description": "Higher-level browser automation for product previews, booking form preparation, price comparison, and data extraction",
        "safety_level": "CONFIRM",
        "endpoints": [
            {
                "id": "search_and_compare",
                "description": "Search the web, extract prices and reviews, return structured comparison",
                "params": [
                    {"name": "query", "type": "string", "required": True, "description": "Search query (product, service, etc.)"},
                    {"name": "max_results", "type": "integer", "required": False, "description": "Maximum results to return (default 5)"},
                ],
            },
            {
                "id": "fill_web_form",
                "description": "Navigate to a URL and fill form fields, returning screenshot for confirmation",
                "params": [
                    {"name": "url", "type": "string", "required": True, "description": "URL of the page with the form"},
                    {"name": "form_data", "type": "object", "required": True, "description": "Mapping of CSS selector or field name to value"},
                ],
            },
            {
                "id": "make_purchase",
                "description": "Preview an unverified observed product-page price with spend-cap screening. Returns a read-only SDUI card. Checkout/payment is unavailable; no purchase or approval is created.",
                "params": [
                    {"name": "url", "type": "string", "required": True, "description": "Product or checkout URL"},
                    {"name": "item_description", "type": "string", "required": False, "description": "Human-readable item description"},
                ],
            },
            {
                "id": "book_reservation",
                "description": "Fill a booking/reservation form and return confirmation card — does NOT submit",
                "params": [
                    {"name": "service_url", "type": "string", "required": True, "description": "Booking service URL"},
                    {"name": "details", "type": "object", "required": True, "description": "Reservation details to fill (name, date, guests, etc.)"},
                ],
            },
            {
                "id": "extract_page_data",
                "description": "Navigate to URL and extract structured data from page content",
                "params": [
                    {"name": "url", "type": "string", "required": True, "description": "URL to extract data from"},
                    {"name": "what_to_extract", "type": "string", "required": False, "description": "Description of what data to extract"},
                ],
            },
        ],
    }
