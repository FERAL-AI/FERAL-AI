"""Bounded executor receipts for realtime provider tool-output messages.

These are the existing success/data/error envelopes, not spoken text. Redaction
covers named credential/card fields and recognized credential/PAN text patterns;
it cannot identify every secret in arbitrary prose. No payload is logged here.
"""
from __future__ import annotations

from dataclasses import replace
from itertools import islice
import json
import math
import re
from typing import Any

from observability.log_redaction import REDACTED, redact
from skills.result_budget import budget_for_tool, serialize_tool_result


MAX_REALTIME_RESULT_CHARS = 8_192
_RECEIPT_KEYS = (
    "success", "status_code", "error", "status", "code", "error_code", "outcome",
    "outcome_unknown", "verified", "pending_approval", "approval_request_id",
    "_truncated", "_truncation_note", "_pagination_hint",
    "_upstream_truncation_note",
    "request_id", "tool_name", "session_id", "_image_omitted",
)
_SECRET_KEYS = frozenset({
    "key", "apikey", "password", "passphrase", "secret", "token",
    "accesstoken", "refreshtoken", "authtoken", "authorization",
    "cookie", "setcookie", "credentials", "clientsecret", "privatekey",
    "cardnumber", "creditcardnumber", "pan", "cvv", "cvc", "paymentcard",
})
_IMAGE_KEYS = frozenset({"image_base64", "image_b64", "screenshot_base64", "screenshot_b64"})
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_API_SECRET = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,})\b")


def _redact_text(text: str) -> str:
    text = _API_SECRET.sub(REDACTED, redact(text))

    def card(match: re.Match) -> str:
        digits = [int(char) for char in match.group() if char.isdigit()]
        if not 13 <= len(digits) <= 19:
            return match.group()
        total = 0
        for index, digit in enumerate(reversed(digits)):
            if index % 2:
                digit *= 2
                if digit > 9:
                    digit -= 9
            total += digit
        return REDACTED if total % 10 == 0 else match.group()

    return _CARD.sub(card, text)


def serialize_realtime_tool_result(tool_name: str, result: Any, *, registry=None) -> str:
    """Preserve receipt truth, including falsy data, on both realtime wires.

    Tool-declared budgets still bound the payload. An 8,192-character wire ceiling
    includes receipt/truncation metadata. Pathological results can lose data,
    but cannot silently lose their failure/uncertainty flags or become success.
    Unsupported objects are withheld rather than stringifying private reprs.
    """
    budget = budget_for_tool(tool_name, registry)
    budget = replace(budget, max_result_chars=min(budget.max_result_chars, MAX_REALTIME_RESULT_CHARS))
    remaining = 512
    clipped = False
    image_omitted = False

    def bounded(value: Any, depth: int, *, receipt: bool = False) -> Any:
        nonlocal remaining, clipped, image_omitted
        remaining -= 1
        if value is None or type(value) is bool:
            return value
        if type(value) is int and value.bit_length() <= 4_096:
            return value
        if type(value) is float and math.isfinite(value):
            return value
        if isinstance(value, (str, dict, list)) and not value:
            return value.copy() if isinstance(value, (dict, list)) else value
        if remaining < 0 or depth <= 0:
            clipped = True
            return "[result branch withheld: structural limit]"
        if isinstance(value, str):
            limit = 2_000 if receipt else min(budget.max_str_len, MAX_REALTIME_RESULT_CHARS)
            if len(value) > limit:
                clipped = True
            # Inspect beyond the visible cut so a credential or PAN crossing
            # that cut is not emitted as an unrecognized partial value.
            return _redact_text(value[:limit + 256])[:limit]
        if isinstance(value, dict):
            output = {}
            width = 20 if receipt else min(budget.max_dict_keys, 50)
            if len(value) > width:
                clipped = True
            for key, item in islice(value.items(), width):
                if not isinstance(key, str) or len(key) > 96:
                    clipped = True
                    continue
                normalized = re.sub(r"[^a-z0-9]", "", key.lower())
                if normalized in _SECRET_KEYS or normalized.endswith(("password", "secret", "accesstoken", "refreshtoken")):
                    output[key] = REDACTED
                elif key in _IMAGE_KEYS and isinstance(item, str) and len(item) > 512:
                    output[key] = "[image omitted from realtime tool output]"
                    image_omitted = True
                else:
                    output[key] = bounded(item, depth - 1, receipt=receipt)
            return output
        if isinstance(value, list):
            width = 20 if receipt else min(budget.max_list_len, 50)
            if len(value) > width:
                clipped = True
            return [bounded(item, depth - 1, receipt=receipt) for item in islice(value, width)]
        clipped = True
        return "[unsupported result value withheld]"

    if isinstance(result, dict):
        # Retain receipt fields before arbitrary extension keys consume width.
        payload = {key: bounded(result[key], 6, receipt=True) for key in _RECEIPT_KEYS if key in result}
        if "data" in result:
            payload["data"] = bounded(result["data"], budget.max_depth)
        extension = {key: value for key, value in islice(result.items(), 50)
                     if key not in _RECEIPT_KEYS and key != "data"}
        payload.update(bounded(extension, budget.max_depth))
        if len(result) > len(payload):
            clipped = True
    else:
        payload = {"success": False, "error": "Executor result envelope unavailable",
                   "data": None, "outcome": "outcome_unknown"}
    if clipped:
        payload["_truncated"] = True
        if "_truncation_note" in payload:
            payload["_upstream_truncation_note"] = payload["_truncation_note"]
        payload["_truncation_note"] = "Result fields were bounded or withheld; this is not the complete result."
    if image_omitted:
        payload["_image_omitted"] = True
    encoded = serialize_tool_result(tool_name, payload, registry=registry, budget=budget)
    decoded = json.loads(encoded)
    if len(encoded) <= MAX_REALTIME_RESULT_CHARS and isinstance(decoded, dict) and all(
        key not in payload or decoded.get(key) == payload[key] for key in _RECEIPT_KEYS
    ) and ("data" not in payload or "data" in decoded) and (
        "data" not in payload or bool(payload["data"]) or decoded["data"] == payload["data"]
    ):
        return encoded

    # The generic serializer may fall back to a preview for huge structures.
    # Keep authoritative receipt fields even when the payload cannot fit.
    receipt = {}
    for key in _RECEIPT_KEYS:
        if key in payload:
            value = payload[key]
            text = json.dumps(value, allow_nan=False)
            receipt[key] = value if len(text) <= 256 else "[receipt detail withheld: size limit]"
    data = payload.get("data")
    data = data if len(json.dumps(data, allow_nan=False)) <= 256 else None
    receipt.update({"data": data, "_truncated": True,
                    "_truncation_note": "Payload omitted to retain the executor receipt within the realtime limit."})
    return json.dumps(receipt, allow_nan=False)
