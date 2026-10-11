"""Bounded executor receipts for realtime provider tool-output messages.

These are the existing success/data/error envelopes, not spoken text. Redaction
covers named credential/card fields and recognized credential/PAN text patterns;
it cannot identify every secret in arbitrary prose. No payload is logged here.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import base64
import io
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
    "request_id", "tool_name", "session_id", "_image_omitted", "_image_delivery",
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
MAX_REALTIME_IMAGE_CHARS = 2_000_000
_SCREENSHOT_TOOLS = frozenset({"screen_capture__capture", "screen_capture__capture_screen",
                              "gui_computer_use__screenshot", "browser__screenshot"})
_UNSEEN_IMAGE = (
    "You have NOT seen this screenshot. Image bytes were withheld from realtime input. "
    "Use returned analysis or registered browser DOM/Accessibility tools for evidence; "
    "do not guess screen contents or claim a task completed without verifying its outcome."
)


@dataclass(frozen=True)
class RealtimeImageBinding:
    producer: object = field(repr=False)
    session_id: str
    call_id: str
    response_epoch: int
    response_id: str
    socket: object = field(repr=False)
    attempt: Any = field(repr=False)
    task: Any = field(repr=False)


class RealtimeToolResult(str):
    """Text receipt plus one ephemeral image; never stored or replayed."""

    _image_url: str
    _binding: RealtimeImageBinding | None

    def __new__(cls, text: str, image_url: str, binding: RealtimeImageBinding | None = None):
        value = super().__new__(cls, text)
        object.__setattr__(value, "_image_url", image_url)
        object.__setattr__(value, "_binding", binding)
        return value

    def __setattr__(self, name, value):
        raise AttributeError("Realtime image carriers are immutable")

    @property
    def image_url(self) -> str:
        return self._image_url

    @property
    def binding(self) -> RealtimeImageBinding | None:
        return self._binding


def coherent_capture_receipt(result: Any, *, require_status: bool = True,
                             exact_status: int | None = None) -> bool:
    """Pixels do not override a denied, pending, failed or uncertain receipt."""
    if not isinstance(result, dict):
        return False
    if (require_status or "success" in result) and result.get("success") is not True:
        return False
    if require_status or "status_code" in result:
        code = result.get("status_code")
        if (type(code) is not int or not 200 <= code < 300
                or exact_status is not None and code != exact_status):
            return False
    if any(not (result.get(key) is None or isinstance(result.get(key), str) and result[key] == "")
           for key in ("error", "error_code", "approval_request_id")):
        return False
    if any(key in result and result[key] is not False for key in ("pending_approval", "outcome_unknown")):
        return False
    if any(key in result and result[key] is not True for key in ("verified", "tool_outcome_verified")):
        return False
    if any(key in result and (not isinstance(result[key], str)
           or result[key] not in {"completed", "success", "executed", "verified", "ok"})
           for key in ("status", "outcome")):
        return False
    return True


def prepare_realtime_screenshot_result(tool_name: str, result: Any, *, registry=None,
                                      allow_images: bool = False,
                                      binding: RealtimeImageBinding | None = None) -> str:
    """Lift one validated built-in screenshot without widening text budgets.

    Only the direct capture payload is inspected. Arbitrary result branches,
    remote URLs, failed receipts and unsupported image formats never upload.
    """
    if tool_name == "desktop_control__screenshot" and isinstance(result, dict):
        result = {**result, "_image_delivery": (
            "You have NOT seen the screen. This desktop screenshot tool returns command output/path, "
            "not pixels. Use screen_capture__capture, gui_computer_use__screenshot or browser__screenshot "
            "for image input, or use browser DOM/Accessibility readback."
        )}
    if (not allow_images or tool_name not in _SCREENSHOT_TOOLS or not isinstance(result, dict)
            # Registered capture primitives return 200 for completed pixels;
            # accepted/deferred 2xx receipts do not establish a capture.
            or not coherent_capture_receipt(result, exact_status=200) or not isinstance(result.get("data"), dict)
            or not coherent_capture_receipt(result["data"], require_status=False, exact_status=200)
            or len(result["data"]) > 50):
        return serialize_realtime_tool_result(tool_name, result, registry=registry)
    data = result["data"]
    candidates = {key: data[key] for key in sorted(_IMAGE_KEYS) if isinstance(data.get(key), str)}
    if not candidates:
        return serialize_realtime_tool_result(tool_name, result, registry=registry)
    # Keep the generic extractor bounded independently of the producer's env.
    from agents.multimodal_blocks import extract_tool_result_images
    candidates.update({key: data[key] for key in ("format", "encoding", "mime_type", "media_type")
                       if isinstance(data.get(key), str)})
    hint = str(data.get("encoding") or data.get("format") or "png").lower()
    hint = "jpeg" if hint == "jpg" else hint
    for key in _IMAGE_KEYS:
        value = candidates.get(key)
        if isinstance(value, str) and not value.startswith("data:"):
            candidates[key] = f"data:image/{hint};base64,{value}"
    _, images, _ = extract_tool_result_images({"data": candidates}, tool_name=tool_name,
                                             max_images=1, max_b64_chars=MAX_REALTIME_IMAGE_CHARS,
                                             max_depth=2)
    if not images:
        return serialize_realtime_tool_result(tool_name, result, registry=registry)
    image = images[0]
    try:
        if image.media_type not in {"image/jpeg", "image/png"}:
            raise ValueError("unsupported_format")
        prefix = f"data:{image.media_type};base64,"
        if not image.data_url.startswith(prefix):
            raise ValueError("inline_image_required")
        encoded = image.data_url[len(prefix):]
        raw = base64.b64decode(encoded, validate=True)
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as decoded:
            if decoded.format not in {"JPEG", "PNG"} or decoded.width * decoded.height > 16_000_000:
                raise ValueError("invalid_image_dimensions")
            if (decoded.format == "JPEG") != (image.media_type == "image/jpeg"):
                raise ValueError("mismatched_format")
            decoded.verify()
    except Exception:
        return serialize_realtime_tool_result(tool_name, result, registry=registry)
    projected = {**result, "data": {**data}}
    for key in candidates:
        if key in _IMAGE_KEYS:
            projected["data"][key] = "[image bytes withheld from JSON; separate image input follows]"
    projected["_image_omitted"] = True
    projected["_image_delivery"] = (
        "A separate image input follows this capture receipt before the next response. "
        "Only that image input or returned analysis is evidence of screenshot contents."
    )
    return RealtimeToolResult(serialize_realtime_tool_result(tool_name, projected, registry=registry),
                              image.data_url, binding)


def withhold_realtime_image(result: str) -> str:
    """Policy changed after capture; keep the receipt and explain no image."""
    payload = json.loads(result)
    payload["_image_delivery"] = _UNSEEN_IMAGE
    return json.dumps(payload)


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
                elif key in _IMAGE_KEYS and isinstance(item, str) and item and not item.startswith("["):
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
        payload["_image_delivery"] = _UNSEEN_IMAGE
    encoded = serialize_tool_result(tool_name, payload, registry=registry, budget=budget)
    decoded = json.loads(encoded)
    if len(encoded) <= MAX_REALTIME_RESULT_CHARS and isinstance(decoded, dict) and all(
        key not in payload or decoded.get(key) == payload[key] for key in _RECEIPT_KEYS
    ) and ("data" not in payload or "data" in decoded) and (
        "data" not in payload or bool(payload["data"]) or decoded["data"] == payload["data"]
    ) and not (tool_name == "macos_ax__snapshot" and clipped):
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
    if (tool_name == "macos_ax__snapshot" and coherent_capture_receipt(result)
            and coherent_capture_receipt(result.get("data"), require_status=False)):
        # Compact the original executor page, not the bounded node prefix;
        # only the original pagination can establish contiguous next_offset.
        from voice.ax_snapshot_receipt import compact_ax_snapshot
        ceiling = min(budget.max_result_chars, MAX_REALTIME_RESULT_CHARS)
        receipt["data"] = None
        receipt["_truncation_note"] = "Accessibility snapshot compacted into a contiguous page; use next_offset for remaining references."
        room = ceiling - len(json.dumps(receipt, allow_nan=False)) + len("null")
        try:
            compact = compact_ax_snapshot(result.get("data"), room, _redact_text)
        except Exception:
            compact = None
        if compact:
            receipt["data"] = compact
            compact_encoded = json.dumps(receipt, allow_nan=False)
            if len(compact_encoded) <= ceiling:
                return compact_encoded
        receipt["data"] = None
        receipt["_truncation_note"] = "Payload omitted to retain the executor receipt within the realtime limit."
    return json.dumps(receipt, allow_nan=False)
