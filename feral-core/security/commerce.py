"""Spend caps and the purchase audit trail.

There was no spend limit anywhere in the brain. ``result_budget.py``
bounds how much text a tool may return, and nothing bounded money.

Three rules this file exists to hold:

* A cap refusal is a refusal, never a prompt. An amount over the limit
  must not reach the user as "approve?", because then the answer to the
  question is the thing that breaks the limit.
* No currency conversion, ever. A cap written in AED is not a statement
  about dollars, and a brain that guesses a rate to compare them is
  inventing the most consequential number in the transaction. A reading
  in another currency is refused, and says so.
* The trail is append-only and records refusals too. "Nothing was
  bought" and "nothing was attempted" are different facts.

The amount is NOT known when a purchase tool is first called:
``web_actions.make_purchase(url, item_description)`` takes no price, and
discovers an unverified observed price by scraping the page. So the cap
is evaluated where the amount first exists, on the tool's own result, and any
future completion path must call :func:`evaluate` again with the amount
it is about to charge.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

logger = logging.getLogger("feral.security.commerce")

#: Symbols that appear glued to an amount, mapped to ISO codes. Kept
#: small on purpose: a symbol this does not know is reported as unknown
#: rather than guessed, and an unknown currency cannot clear a cap.
_SYMBOLS = {
    "$": "USD", "£": "GBP", "€": "EUR", "₹": "INR", "¥": "JPY",
    "د.إ": "AED", "AED": "AED", "Dhs": "AED", "DH": "AED",
}
_CODE_RE = re.compile(r"\b([A-Z]{3})\b")
_AMOUNT_RE = re.compile(r"(\d{1,3}(?:[ ,.]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)")


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.amount} {self.currency}"


@dataclass(frozen=True)
class Verdict:
    """The answer to "may this be spent?", and why."""
    allowed: bool
    reason: str = ""
    code: str = ""
    money: Optional[Money] = None
    spent_today: Optional[Decimal] = None


@dataclass
class SpendCaps:
    """Operator limits, read from the ``commerce`` settings section."""
    currency: str = "USD"
    per_transaction_max: Decimal = Decimal("0")
    per_day_max: Decimal = Decimal("0")
    merchant_allowlist: list[str] = field(default_factory=list)

    @classmethod
    def from_settings(cls, block: Optional[dict]) -> "SpendCaps":
        block = block or {}
        def _dec(key: str) -> Decimal:
            try:
                return Decimal(str(block.get(key, "0") or "0"))
            except (InvalidOperation, ValueError):
                logger.warning("commerce.%s is not a number; treating as 0 (deny)", key)
                return Decimal("0")
        return cls(
            currency=str(block.get("currency") or "USD").upper(),
            per_transaction_max=_dec("per_transaction_max"),
            per_day_max=_dec("per_day_max"),
            merchant_allowlist=[
                str(m).strip().lower() for m in (block.get("merchant_allowlist") or []) if str(m).strip()
            ],
        )


def load_caps() -> SpendCaps:
    """Read the operator's limits from the ``commerce`` settings block.

    An install that has never set them gets zeroes, and zero is a deny:
    a brain with no configured limit must refuse to spend rather than
    treat "unset" as "unlimited".
    """
    try:
        from config.loader import load_settings
        settings = load_settings() or {}
    except Exception:  # pragma: no cover - defensive
        logger.warning("commerce settings unreadable; refusing purchases", exc_info=True)
        return SpendCaps()
    return SpendCaps.from_settings(settings.get("commerce") or {})


def parse_money(text: str, *, default_currency: str = "") -> Optional[Money]:
    """Best-effort amount and currency from a scraped price string.

    Returns ``None`` when either half is missing. A price we cannot read
    is not a small price: callers must treat ``None`` as "unknown", and
    unknown never clears a cap.
    """
    if not text:
        return None
    raw = str(text).strip()

    currency = ""
    code = _CODE_RE.search(raw.upper())
    if code and code.group(1) in set(_SYMBOLS.values()):
        currency = code.group(1)
    if not currency:
        for symbol, iso in _SYMBOLS.items():
            if symbol in raw:
                currency = iso
                break
    if not currency:
        currency = (default_currency or "").upper()
    if not currency:
        return None

    match = _AMOUNT_RE.search(raw.replace(" ", " "))
    if not match:
        return None
    digits = match.group(1)
    # "1,234.56" and "1.234,56" both occur. The last separator is the
    # decimal one when it is followed by one or two digits.
    if "," in digits and "." in digits:
        digits = (digits.replace(",", "") if digits.rfind(".") > digits.rfind(",")
                  else digits.replace(".", "").replace(",", "."))
    elif "," in digits:
        head, _, tail = digits.rpartition(",")
        digits = f"{head.replace(' ', '')}.{tail}" if len(tail) in (1, 2) else digits.replace(",", "")
    digits = digits.replace(" ", "")
    try:
        amount = Decimal(digits)
    except InvalidOperation:
        return None
    if amount < 0:
        return None
    return Money(amount=amount, currency=currency)


def merchant_from_url(url: str) -> str:
    """The host a purchase would be made at, lowercased, without ``www.``."""
    if not url:
        return ""
    host = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", str(url)).split("/")[0]
    host = host.split("@")[-1].split(":")[0].strip().lower()
    return host[4:] if host.startswith("www.") else host


class PurchaseAuditUnavailable(RuntimeError):
    """The purchase ledger could not persist or verify spending."""


class PurchaseAudit:
    """Append-only record of every purchase the brain evaluated."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        if db_path is None:
            from config.loader import feral_home
            db_path = str(Path(feral_home()) / "purchases.db")
        self._db_path = db_path
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        conn = self._conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS purchases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    outcome TEXT NOT NULL,
                    merchant TEXT NOT NULL DEFAULT '',
                    amount TEXT NOT NULL DEFAULT '',
                    currency TEXT NOT NULL DEFAULT '',
                    tool TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '',
                    approval_id TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT ''
                )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_purchases_ts ON purchases(ts)")
            conn.commit()
        finally:
            conn.close()

    def record(
        self, *, outcome: str, merchant: str = "", money: Optional[Money] = None,
        tool: str = "", session_id: str = "", approval_id: str = "", reason: str = "",
    ) -> None:
        """Append one row, or raise if the ledger did not persist it.

        Callers must not offer or report a purchase when its audit evidence
        could not be saved. Healthy calls retain their existing None return.
        """
        try:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO purchases (ts, outcome, merchant, amount, currency, "
                    "tool, session_id, approval_id, reason) VALUES (?,?,?,?,?,?,?,?,?)",
                    (time.time(), outcome, merchant,
                     str(money.amount) if money else "", money.currency if money else "",
                     tool, session_id, approval_id, reason),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("purchase audit write failed: %s", exc)
            raise PurchaseAuditUnavailable("purchase audit could not be saved") from exc

    def spent_today(self, currency: str, *, now: Optional[float] = None) -> Decimal:
        """Money actually charged today, in ``currency``.

        Only ``completed`` rows count. An offer that was refused, or one
        the user never confirmed, spent nothing, and counting it would
        let a day of browsing lock the operator out of their own cap.
        """
        since = (now or time.time()) - 86400
        total = Decimal("0")
        try:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT amount FROM purchases WHERE ts >= ? AND outcome = 'completed' "
                    "AND currency = ?", (since, currency.upper()),
                ).fetchall()
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("purchase audit read failed: %s", exc)
            raise PurchaseAuditUnavailable("purchase spending could not be verified") from exc
        for row in rows:
            try:
                total += Decimal(str(row["amount"] or "0"))
            except InvalidOperation:
                continue
        return total


def evaluate(
    money: Optional[Money], merchant: str, caps: SpendCaps,
    *, audit: Optional[PurchaseAudit] = None,
) -> Verdict:
    """May this amount be spent at this merchant?

    Deny is the default for every uncertainty: an unreadable price, an
    unset cap, a currency we do not hold a limit in. The failure this
    guards against is spending money nobody authorised, and every one of
    those states is indistinguishable from that from the inside.
    """
    if money is None:
        return Verdict(False, "the price could not be read, so it cannot be checked "
                              "against your spend limit", "price_unreadable")
    if caps.per_transaction_max <= 0 or caps.per_day_max <= 0:
        return Verdict(False, "no spend limit is configured, so purchases are refused. "
                              "Set commerce.per_transaction_max and commerce.per_day_max.",
                       "no_cap_configured", money)
    if money.currency != caps.currency:
        return Verdict(False, f"the price is in {money.currency} but your limit is set in "
                              f"{caps.currency}, and FERAL does not convert currencies",
                       "currency_mismatch", money)
    if caps.merchant_allowlist and merchant not in caps.merchant_allowlist:
        return Verdict(False, f"{merchant or 'this merchant'} is not on your allowed "
                              f"merchant list", "merchant_not_allowed", money)
    if money.amount > caps.per_transaction_max:
        return Verdict(False, f"{money.amount} {money.currency} is over your "
                              f"{caps.per_transaction_max} {caps.currency} per-purchase limit",
                       "over_per_transaction", money)
    try:
        spent = audit.spent_today(caps.currency) if audit else Decimal("0")
    except PurchaseAuditUnavailable:
        return Verdict(False, "the purchase ledger is unavailable, so today's spending "
                              "cannot be verified", "audit_unavailable", money)
    if spent + money.amount > caps.per_day_max:
        return Verdict(False, f"this would take today's spending to "
                              f"{spent + money.amount} {caps.currency}, over your "
                              f"{caps.per_day_max} daily limit",
                       "over_per_day", money, spent)
    return Verdict(True, "", "within_limits", money, spent)
