"""Money has a limit, and an unreadable price is not a small one.

There was no spend cap anywhere in the brain: result_budget.py bounds how
much text a tool returns, and nothing bounded money. make_purchase takes
no price, it scrapes one, so the cap is evaluated where the amount first
exists, and a refusal replaces the confirmation card rather than riding
along with it.

Deny is the default for every uncertainty. An unreadable price, an unset
cap, a currency the limit is not written in: from the inside each of
those is indistinguishable from spending money nobody authorised.
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from security.commerce import (
    Money, PurchaseAudit, PurchaseAuditUnavailable, SpendCaps, evaluate,
    merchant_from_url, parse_money,
)

CAPS = SpendCaps(currency="AED", per_transaction_max=Decimal("200"),
                 per_day_max=Decimal("500"))


class TestParsing:
    @pytest.mark.parametrize("text,amount,currency", [
        ("AED 24.00", "24.00", "AED"),
        ("$12.99", "12.99", "USD"),
        ("£8", "8", "GBP"),
        ("24,00 €", "24.00", "EUR"),
        ("1.234,56 EUR", "1234.56", "EUR"),
        ("1,234.56 USD", "1234.56", "USD"),
    ])
    def test_reads_real_price_strings(self, text, amount, currency):
        money = parse_money(text)
        assert money == Money(Decimal(amount), currency)

    @pytest.mark.parametrize("text", ["Price not found", "", "call for pricing"])
    def test_unreadable_is_none(self, text):
        assert parse_money(text) is None

    def test_a_bare_number_has_no_currency_and_is_not_guessed(self):
        """Assuming the operator's own currency is exactly the guess to avoid."""
        assert parse_money("24.00") is None

    def test_merchant_is_the_host(self):
        assert merchant_from_url("https://www.noon.com/uae-en/item") == "noon.com"


class TestVerdicts:
    def test_within_limits_is_allowed(self):
        assert evaluate(parse_money("AED 24.00"), "noon.com", CAPS).allowed

    def test_an_unreadable_price_is_refused(self):
        v = evaluate(None, "noon.com", CAPS)
        assert not v.allowed and v.code == "price_unreadable"

    def test_no_configured_cap_refuses_rather_than_allows(self):
        v = evaluate(parse_money("AED 5.00"), "noon.com", SpendCaps())
        assert not v.allowed and v.code == "no_cap_configured"

    def test_another_currency_is_refused_not_converted(self):
        v = evaluate(parse_money("$12.99"), "noon.com", CAPS)
        assert not v.allowed and v.code == "currency_mismatch"
        assert "convert" in v.reason

    def test_over_the_per_transaction_limit(self):
        v = evaluate(parse_money("AED 240.00"), "noon.com", CAPS)
        assert not v.allowed and v.code == "over_per_transaction"

    def test_merchant_allowlist_is_enforced_when_set(self):
        caps = SpendCaps(currency="AED", per_transaction_max=Decimal("200"),
                         per_day_max=Decimal("500"), merchant_allowlist=["noon.com"])
        assert evaluate(parse_money("AED 10.00"), "noon.com", caps).allowed
        v = evaluate(parse_money("AED 10.00"), "sketchy.example", caps)
        assert not v.allowed and v.code == "merchant_not_allowed"


class TestAuditAndDailyTotal:
    def test_unavailable_ledger_refuses_instead_of_resetting_spending(self, tmp_path):
        import sqlite3

        path = tmp_path / "unavailable.db"
        audit = PurchaseAudit(db_path=str(path))
        audit.record(outcome="completed", money=Money(Decimal("450"), "AED"))
        candidate = parse_money("AED 100.00")
        healthy = evaluate(candidate, "noon.com", CAPS, audit=audit)
        assert not healthy.allowed and healthy.code == "over_per_day"
        assert healthy.spent_today == Decimal("450")

        with sqlite3.connect(str(path)) as conn:
            conn.execute("ALTER TABLE purchases RENAME TO purchases_unavailable")

        with pytest.raises(PurchaseAuditUnavailable, match="could not be verified"):
            audit.spent_today("AED")
        unavailable = evaluate(candidate, "noon.com", CAPS, audit=audit)
        assert not unavailable.allowed and unavailable.code == "audit_unavailable"
        assert unavailable.spent_today is None

    def test_failed_audit_write_reports_failure_and_preserves_existing_rows(self, tmp_path):
        import sqlite3

        path = tmp_path / "unwritable.db"
        audit = PurchaseAudit(db_path=str(path))
        assert audit.record(outcome="completed", money=Money(Decimal("50"), "AED")) is None
        with sqlite3.connect(str(path)) as conn:
            conn.execute("ALTER TABLE purchases RENAME TO purchases_unavailable")

        with pytest.raises(PurchaseAuditUnavailable, match="could not be saved"):
            audit.record(outcome="completed", money=Money(Decimal("100"), "AED"))
        with sqlite3.connect(str(path)) as conn:
            rows = conn.execute("SELECT outcome, amount FROM purchases_unavailable").fetchall()
        assert rows == [("completed", "50")]

    def test_only_completed_purchases_count_against_the_day(self, tmp_path):
        audit = PurchaseAudit(db_path=str(tmp_path / "p.db"))
        audit.record(outcome="offered", money=Money(Decimal("100"), "AED"))
        audit.record(outcome="refused_cap", money=Money(Decimal("900"), "AED"))
        assert audit.spent_today("AED") == Decimal("0")
        audit.record(outcome="completed", money=Money(Decimal("100"), "AED"))
        assert audit.spent_today("AED") == Decimal("100")

    def test_the_daily_cap_counts_what_was_actually_spent(self, tmp_path):
        audit = PurchaseAudit(db_path=str(tmp_path / "p.db"))
        audit.record(outcome="completed", money=Money(Decimal("450"), "AED"))
        v = evaluate(parse_money("AED 100.00"), "noon.com", CAPS, audit=audit)
        assert not v.allowed and v.code == "over_per_day"
        assert v.spent_today == Decimal("450")

    def test_a_refusal_is_recorded_so_the_trail_shows_attempts(self, tmp_path):
        audit = PurchaseAudit(db_path=str(tmp_path / "p.db"))
        audit.record(outcome="refused_cap", merchant="noon.com",
                     money=Money(Decimal("900"), "AED"), reason="over_per_transaction")
        import sqlite3
        conn = sqlite3.connect(str(tmp_path / "p.db"))
        rows = conn.execute("SELECT outcome, merchant, reason FROM purchases").fetchall()
        assert rows == [("refused_cap", "noon.com", "over_per_transaction")]


class _Browser:
    def __init__(self, price):
        self.price = price
        self.screenshots = 0

    async def navigate(self, url):
        return {"success": True}

    async def wait(self, ms):
        return None

    async def get_page_info(self):
        return {"title": "Oat flat white", "url": "https://www.noon.com/item"}

    async def evaluate(self, js):
        return {"result": json.dumps([self.price] if self.price else [])}

    async def screenshot(self):
        self.screenshots += 1
        return {"image_b64": "x"}


async def _purchase(monkeypatch, tmp_path, price, caps=CAPS):
    from skills.impl import web_actions

    skill = web_actions.WebActionsSkill()
    browser = _Browser(price)

    async def _ensure(*a, **k):
        return browser

    monkeypatch.setattr(skill, "_ensure_browser", _ensure)
    monkeypatch.setattr(web_actions, "load_caps", lambda: caps)
    monkeypatch.setattr(web_actions, "_AUDIT", PurchaseAudit(db_path=str(tmp_path / "p.db")))
    result = await skill.make_purchase(url="https://www.noon.com/item")
    return result, browser


@pytest.mark.asyncio
async def test_an_over_cap_purchase_is_refused_not_offered(monkeypatch, tmp_path):
    result, browser = await _purchase(monkeypatch, tmp_path, "AED 900.00")
    assert result["purchased"] is False
    assert result["refused"] is True
    assert result["reason"] == "over_per_transaction"
    assert "sdui_card" not in result, "an over-limit purchase must not be offered"
    assert browser.screenshots == 0


@pytest.mark.asyncio
async def test_an_unreadable_price_is_refused(monkeypatch, tmp_path):
    result, _ = await _purchase(monkeypatch, tmp_path, "")
    assert result["refused"] is True
    assert result["reason"] == "price_unreadable"


@pytest.mark.asyncio
async def test_a_within_limit_purchase_is_previewed_without_approval_or_checkout(
    monkeypatch, tmp_path,
):
    result, _ = await _purchase(monkeypatch, tmp_path, "AED 24.00")
    assert result["purchased"] is False, "nothing may ever complete a purchase"
    assert result["awaiting_confirmation"] is False
    assert result["preview_only"] is True
    assert result["checkout_available"] is False
    assert result["price_verified"] is False
    assert "sdui_card" in result
    # Parsed for the approval frame, so the glasses can speak an amount.
    assert (result["merchant"], result["amount"], result["currency"]) == (
        "noon.com", "24.00", "AED")


@pytest.mark.asyncio
async def test_every_evaluation_lands_in_the_trail(monkeypatch, tmp_path):
    import sqlite3
    await _purchase(monkeypatch, tmp_path, "AED 24.00")
    await _purchase(monkeypatch, tmp_path, "AED 900.00")
    conn = sqlite3.connect(str(tmp_path / "p.db"))
    outcomes = [r[0] for r in conn.execute("SELECT outcome FROM purchases ORDER BY id")]
    assert outcomes == ["offered", "refused_cap"]


@pytest.mark.asyncio
async def test_failed_offer_audit_cannot_return_a_confirmation_card(monkeypatch, tmp_path):
    import sqlite3
    from skills.impl import web_actions

    path = tmp_path / "offer.db"
    audit = PurchaseAudit(db_path=str(path))
    with sqlite3.connect(str(path)) as conn:
        conn.execute("""CREATE TRIGGER reject_offer BEFORE INSERT ON purchases
                        WHEN NEW.outcome = 'offered'
                        BEGIN SELECT RAISE(FAIL, 'synthetic write failure'); END""")
    skill = web_actions.WebActionsSkill()
    browser = _Browser("AED 24.00")

    async def _ensure():
        return browser

    monkeypatch.setattr(skill, "_ensure_browser", _ensure)
    monkeypatch.setattr(web_actions, "load_caps", lambda: CAPS)
    monkeypatch.setattr(web_actions, "_AUDIT", audit)
    result = await skill.execute("make_purchase", {"url": "https://www.noon.com/item"}, {})
    assert result["success"] is False
    assert result["data"] is None
    assert "could not be saved" in result["error"]
    assert browser.screenshots == 0
    with sqlite3.connect(str(path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM purchases").fetchone()[0] == 0
