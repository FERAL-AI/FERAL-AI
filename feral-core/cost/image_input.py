"""Conservative admission bound for the documented GPT-5.4 image input.

Sources fetched 2026-10-10:
https://developers.openai.com/api/docs/guides/images-vision
https://developers.openai.com/api/docs/models/gpt-5.4

This is a per-request upper bound, not measured usage. Unknown models, media,
origins, pricing tiers, and hidden history references remain unpriceable.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ImageInputBound:
    prompt_tokens: int
    image_count: int
    image_tokens: int


# Includes one token for the documented floating-point billing rounding.
_IMAGE_TOKEN_BOUNDS = {"low": 7374, "high": 3001, "auto": 3001, "original": 12001}
_MODELS = frozenset({"gpt-5.4", "gpt-5.4-2026-03-05"})
_MEDIA_TYPES = frozenset({"input_audio", "audio", "audio_url", "video_url", "image", "image_base64",
                          "input_file", "file", "item_reference", "reasoning"})
_MEDIA_KEYS = frozenset({"audio_url", "video_url", "file_id", "file_data", "file_url", "encrypted_content"})


def openai_image_input_bound(body: dict, *, provider: str, model: str,
                             base_url: str) -> ImageInputBound | None:
    """Only an exact documented, standard-priced image request gains a bound."""
    if provider != "openai" or model not in _MODELS:
        return None
    try:
        endpoint = urlsplit(base_url)
        if (endpoint.scheme != "https" or endpoint.hostname != "api.openai.com"
                or endpoint.port not in {None, 443} or endpoint.username or endpoint.password
                or endpoint.query or endpoint.fragment or endpoint.path.rstrip("/") != "/v1"):
            return None
    except (TypeError, ValueError):
        return None
    tier = body.get("service_tier", "default")
    if (not isinstance(tier, str) or tier not in {"auto", "default"}
            or body.get("tools") or body.get("previous_response_id") or body.get("conversation")):
        return None
    image_count, image_tokens, nodes = 0, 0, 0

    def walk(value, depth=0):
        nonlocal image_count, image_tokens, nodes
        nodes += 1
        if depth > 20 or nodes > 20000:
            raise ValueError()
        if isinstance(value, list):
            return [walk(item, depth + 1) for item in value]
        if not isinstance(value, dict):
            return value
        kind = value.get("type")
        if kind in {"input_image", "image_url"}:
            if kind == "input_image":
                if set(value) - {"type", "image_url", "detail"}:
                    raise ValueError()
                url, detail = value.get("image_url"), value.get("detail", "auto")
            else:
                if set(value) != {"type", "image_url"} or not isinstance(value["image_url"], dict):
                    raise ValueError()
                image = value["image_url"]
                if set(image) - {"url", "detail"}:
                    raise ValueError()
                url, detail = image.get("url"), image.get("detail", "auto")
            if not isinstance(url, str) or not url or not isinstance(detail, str) or detail not in _IMAGE_TOKEN_BOUNDS:
                raise ValueError()
            image_count += 1
            if image_count > 64:
                raise ValueError()
            image_tokens += _IMAGE_TOKEN_BOUNDS[detail]
            # URLs/base64 are image media, not prompt text. Count their image
            # bound once; preserve all other input in the byte upper bound.
            return {"type": "input_image", "detail": detail}
        if kind in _MEDIA_TYPES or any(key in value for key in _MEDIA_KEYS) or "image_url" in value:
            raise ValueError()
        return {key: walk(item, depth + 1) for key, item in value.items()}

    try:
        normalized = walk(body)
        # Byte-level text tokens cannot exceed UTF-8 input bytes. The serialized
        # envelope plus framing margin is deliberately conservative. Keep the
        # whole request below GPT-5.4's long-context surcharge threshold.
        text_bytes = len(json.dumps(normalized, ensure_ascii=False, allow_nan=False).encode())
    except (ValueError, TypeError, RecursionError):
        return None
    bound = text_bytes + image_tokens + 4096
    if not image_count or bound > 272000:
        return None
    return ImageInputBound(bound, image_count, image_tokens)
