"""Per-provider multimodal content-block translators.

FERAL's upstream callers (``perception/fusion.py:to_llm_user_content``,
the ScreenLoop vision attach in ``perception/context_attach.py``, the
clipboard / browser-upload paths) assemble user-turn content in the
**OpenAI Chat-Completions** content-block shape because that's what
the bulk of FERAL's history runs through (and what
``providers/openai_provider.py`` natively accepts):

    [{"type": "text", "text": "..."},
     {"type": "image_url",
      "image_url": {"url": "data:image/png;base64,...", "detail": "low"}}]

Anthropic's Messages API uses a *different* content-block schema.
Sending the OpenAI shape is the v2026.5.44 regression caught in live
verification:

    HTTP 400 — Input tag 'image_url' found using 'type' does not match
    any of the expected tags: 'image', 'text', 'tool_result', ...

The fix is a tiny pure-function translator that runs on the
provider-boundary (``agents.llm_anthropic_shape._convert_messages_for_anthropic``).
The canonical internal shape stays OpenAI-flavoured for backwards
compatibility; we only emit the Anthropic-flavoured shape on the
wire.

The same idea is exposed for Gemini parity (``to_gemini_parts``). The
runtime ``gemini`` provider in ``agents.llm_provider`` hits Google's
OpenAI-compatibility endpoint
(``/v1beta/openai/chat/completions``) and accepts ``image_url`` parts
unchanged, so the helper is currently *unused* on the hot path — but
it's the right shape if/when we switch back to the native
``:generateContent`` endpoint, and it pins the expected mapping in
tests so a future regression is caught at the unit-test layer.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("feral.llm.multimodal")


# Anthropic accepts these block types on a user/assistant message.
# Pulled from the live 400 message body (Anthropic responded with the
# canonical list of expected tags when the schema validator tripped).
# We use this set to decide whether an "unknown" type should pass
# through silently (already-valid Anthropic blocks like ``tool_use``,
# ``tool_result``, ``document``) or be passed through with a warning
# (genuinely unknown — likely a new content type FERAL hasn't taught
# the translator yet).
_ANTHROPIC_NATIVE_BLOCK_TYPES = frozenset({
    "text",
    "image",
    "document",
    "thinking",
    "redacted_thinking",
    "tool_use",
    "tool_result",
    "server_tool_use",
    "web_search_tool_result",
    "web_fetch_tool_result",
    "code_execution_tool_result",
    "bash_code_execution_tool_result",
    "text_editor_code_execution_tool_result",
    "tool_search_tool_result",
    "search_result",
    "container_upload",
})


# Matches a data URL like ``data:image/png;base64,iVBORw0KG...``. We
# accept any image/* media type and pull the trailing base64 payload.
_DATA_URL_RE = re.compile(
    r"^data:(?P<media_type>image/[A-Za-z0-9.+-]+);base64,(?P<data>.+)$",
    re.DOTALL,
)


def _extract_image_url(part: dict) -> tuple[str, str | None]:
    """Return ``(url, detail)`` from an OpenAI-shape image_url part.

    OpenAI's documented shape is::

        {"type": "image_url",
         "image_url": {"url": "...", "detail": "low|high|auto"}}

    but some upstream emitters (older SDKs, hand-written code) flatten
    the URL into a plain string::

        {"type": "image_url", "image_url": "https://..."}

    Accept both so the translator is forgiving on the way in (the
    Anthropic side only cares about the canonical out-shape anyway).
    """
    img = part.get("image_url")
    if isinstance(img, dict):
        return str(img.get("url") or ""), img.get("detail")
    if isinstance(img, str):
        return img, None
    return "", None


def _openai_image_to_anthropic(url: str) -> dict | None:
    """Translate a single OpenAI image URL string to an Anthropic
    ``image`` content block. Returns ``None`` when ``url`` is empty
    (caller decides whether to drop or pass through the broken part).
    """
    if not url:
        return None
    m = _DATA_URL_RE.match(url)
    if m:
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": m.group("media_type"),
                "data": m.group("data"),
            },
        }
    # http / https / any other absolute URL → Anthropic's ``url``
    # source type. Anthropic added URL sources alongside base64 in the
    # 2024-10 messages API revision; ``anthropic-version: 2023-06-01``
    # accepts them as long as the URL is publicly reachable.
    return {
        "type": "image",
        "source": {"type": "url", "url": url},
    }


def to_anthropic_blocks(blocks: list[Any]) -> list[Any]:
    """Translate a list of OpenAI-shape content blocks to Anthropic shape.

    * ``{"type": "text", "text": "..."}`` → passed through unchanged.
    * ``{"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}}``
      → ``{"type": "image",
            "source": {"type": "base64",
                       "media_type": "image/png",
                       "data": "xxx"}}``
    * ``{"type": "image_url", "image_url": {"url": "https://..."}}`` →
      ``{"type": "image", "source": {"type": "url", "url": "https://..."}}``
    * Anthropic-native blocks (``tool_use``, ``tool_result``,
      ``document``, etc.) → passed through unchanged.
    * Unknown block types → passed through with a single WARN per
      unknown ``type`` value so a genuinely new upstream shape doesn't
      silently get dropped on the wire.

    Non-dict items pass through untouched so callers can mix in
    Anthropic-shaped blocks they assembled themselves (e.g. a
    ``tool_use`` block lifted by ``_convert_messages_for_anthropic``)
    without the translator having to special-case them.
    """
    out: list[Any] = []
    for part in blocks:
        if not isinstance(part, dict):
            out.append(part)
            continue
        ptype = part.get("type", "")

        if ptype == "text":
            out.append(part)
            continue

        if ptype == "image_url":
            url, _detail = _extract_image_url(part)
            translated = _openai_image_to_anthropic(url)
            if translated is None:
                logger.warning(
                    "multimodal: image_url part has empty/invalid url; "
                    "dropping for Anthropic request (part=%r)", part,
                )
                continue
            out.append(translated)
            continue

        # Anthropic also accepts a bare ``input_image`` (Responses-API
        # style) in some FERAL paths. Treat it the same as image_url.
        if ptype == "input_image":
            url = part.get("image_url") if isinstance(part.get("image_url"), str) else ""
            translated = _openai_image_to_anthropic(str(url))
            if translated is None:
                logger.warning(
                    "multimodal: input_image part has empty url; "
                    "dropping for Anthropic request (part=%r)", part,
                )
                continue
            out.append(translated)
            continue

        if ptype in _ANTHROPIC_NATIVE_BLOCK_TYPES:
            out.append(part)
            continue

        logger.warning(
            "multimodal: unknown content-block type %r — passing through "
            "to Anthropic unchanged (validation will happen upstream)",
            ptype,
        )
        out.append(part)

    return out


def translate_content_for_anthropic(content: Any) -> Any:
    """Translate a message's ``content`` field in place for Anthropic.

    Accepts the union shape FERAL uses on the wire:

    * ``str`` → passed through (plain text content).
    * ``list[dict]`` → each block run through :func:`to_anthropic_blocks`.
    * anything else → passed through unchanged so the translator never
      hides a bug in upstream code.
    """
    if isinstance(content, list):
        return to_anthropic_blocks(content)
    return content


# ─────────────────────────────────────────────────────────────────────
# Gemini parity helper
# ─────────────────────────────────────────────────────────────────────
#
# The active FERAL Gemini dispatcher hits Google's OpenAI-compat
# endpoint (``/v1beta/openai/chat/completions``) and so accepts the
# canonical OpenAI ``image_url`` shape unchanged. This helper exists
# for the day we either:
#
#   (a) wire ``providers/gemini_provider.py`` (native
#       ``:generateContent``) into the failover path, or
#   (b) need to emit Gemini-native parts from a tool / skill that
#       talks to the native endpoint directly (e.g. image-generation
#       previews like ``gemini-3.1-flash-image-preview``).
#
# Pinning the mapping in tests now means no surprise regression when
# we flip the surface.


def _openai_image_to_gemini(url: str, fallback_mime: str = "image/png") -> dict | None:
    """Translate a single OpenAI image URL to a Gemini ``parts`` entry."""
    if not url:
        return None
    m = _DATA_URL_RE.match(url)
    if m:
        return {
            "inline_data": {
                "mime_type": m.group("media_type"),
                "data": m.group("data"),
            }
        }
    # http / https → ``file_data``. Gemini's native API requires the
    # uploaded file URI ``files/...`` here in practice, but
    # ``file_uri`` with a public https URL is accepted on the public
    # Gemini 3.x flash/pro routes. The native fetch-and-encode
    # fallback lives in ``perception/context_attach.py`` for the
    # ``hybrid`` engine; the translator stays pure.
    return {
        "file_data": {
            "mime_type": fallback_mime,
            "file_uri": url,
        }
    }


def to_gemini_parts(blocks: list[Any]) -> list[Any]:
    """Translate OpenAI-shape content blocks to Gemini ``parts`` entries.

    * ``{"type": "text", "text": "..."}`` → ``{"text": "..."}``
    * data-URL ``image_url`` → ``{"inline_data": {"mime_type", "data"}}``
    * http(s) ``image_url`` → ``{"file_data": {"mime_type", "file_uri"}}``
    * Unknown block types → passed through unchanged with a WARN so
      the native API surfaces the validation error itself.
    """
    out: list[Any] = []
    for part in blocks:
        if not isinstance(part, dict):
            out.append(part)
            continue
        ptype = part.get("type", "")

        if ptype == "text":
            out.append({"text": str(part.get("text", ""))})
            continue

        if ptype in ("image_url", "input_image"):
            if ptype == "input_image":
                raw_url = part.get("image_url")
                url = raw_url if isinstance(raw_url, str) else ""
            else:
                url, _detail = _extract_image_url(part)
            translated = _openai_image_to_gemini(str(url))
            if translated is None:
                logger.warning(
                    "multimodal: image part has empty url; dropping for "
                    "Gemini request (part=%r)", part,
                )
                continue
            out.append(translated)
            continue

        logger.warning(
            "multimodal: unknown content-block type %r — passing through "
            "to Gemini unchanged", ptype,
        )
        out.append(part)

    return out


__all__ = [
    "to_anthropic_blocks",
    "to_gemini_parts",
    "translate_content_for_anthropic",
]
