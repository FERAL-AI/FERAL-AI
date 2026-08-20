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
unchanged, so the helper is not on the hot path, but it's the right
shape if/when we switch back to the native ``:generateContent``
endpoint, it backs ``tool_result_images_as_gemini_content`` below, and
it pins the expected mapping in tests so a future regression is caught
at the unit-test layer.

The second half of this module (from "Image-bearing TOOL RESULTS") is
the tool-result side of the same problem: a screenshot returned by a
tool has to reach the model as an image rather than as 2 000 truncated
base64 characters, and the shape that carries it differs per provider.
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


# ─────────────────────────────────────────────────────────────────────
# Per-provider tool_choice translator
# ─────────────────────────────────────────────────────────────────────
#
# FERAL's orchestrator decides at turn-classification time whether the
# LLM MUST call a specific tool this turn (e.g. ``notes_memory__fused_timeline``
# on a temporal-recall query — the grounded-memory closure described in
# the v2026.5.47 follow-up). The chat-completions wire field that carries
# that intent is ``tool_choice``, and every provider names it slightly
# differently. Centralising the translation here keeps the per-call-site
# branches in ``agents.llm_provider`` thin (set one value, pass it to the
# translator at body-build time) and pins the mapping in unit tests.
#
# Providers that natively support naming a single tool to force:
#   * OpenAI + every OpenAI-compatible adapter we ship today
#     (openrouter, deepseek, groq, kimi, qwen, lmstudio, ollama)
#     →  {"type": "function", "function": {"name": <tool>}}
#   * Anthropic Messages API
#     →  {"type": "tool", "name": <tool>}
#
# Providers that DO NOT support naming a single tool on the wire shape
# FERAL actually targets:
#   * Gemini's OpenAI-compatibility endpoint
#     (``/v1beta/openai/chat/completions``) accepts ``tool_choice`` but
#     only as ``"auto"`` / ``"none"`` / ``"required"`` — the native
#     ``:generateContent`` ``function_calling_config.mode = "ANY"`` with
#     ``allowed_function_names = [<tool>]`` lives on a different
#     endpoint we don't currently drive. Degrade to ``"required"`` so
#     the model still must call SOME tool, and let the orchestrator's
#     side-channel fall back to its deterministic emit for the
#     widget-mount safety net.
#
# Unknown providers (catalog-only descriptors that don't ship a runtime
# adapter, user-typed provider strings) return ``None`` so callers fall
# back to the default ``"auto"`` they would have used pre-fix — never
# raise; the prompt + side-channel remain the path.
_OPENAI_COMPAT_FORCE_PROVIDERS: frozenset[str] = frozenset({
    "openai",
    "openrouter",
    "deepseek",
    "groq",
    "kimi",
    "qwen",
    "lmstudio",
    "ollama",
})

# Providers that support the broader ``"required"`` /  ``"any"`` shape
# (force-call SOME tool, not a specific one) but not named forcing on
# the wire we drive. Kept as a separate set so the truthful degrade
# is documented next to the data.
_REQUIRED_ONLY_FORCE_PROVIDERS: frozenset[str] = frozenset({
    "gemini",
})


def to_provider_tool_choice(provider: str, force_tool: str) -> Any:
    """Translate ``force_tool`` to the right wire-shape for *provider*.

    * Anthropic →  ``{"type": "tool", "name": force_tool}``
    * OpenAI / OpenRouter / DeepSeek / Groq / Kimi / Qwen / LM Studio /
      Ollama →  ``{"type": "function", "function": {"name": force_tool}}``
    * Gemini (OpenAI-compat endpoint) →  ``"required"`` (degrade — the
      OpenAI-compat layer can't name a single tool; prompt + side-channel
      remain the path for the named-tool grounding).
    * Unknown / unsupported provider → ``None`` so the caller falls
      back to its default ``"auto"`` instead of erroring on a typo.

    Returns ``None`` when ``force_tool`` is empty/falsy so callers can
    use the result directly as a truthiness gate.
    """
    if not force_tool:
        return None
    p = (provider or "").lower()
    if p == "anthropic":
        return {"type": "tool", "name": force_tool}
    if p in _OPENAI_COMPAT_FORCE_PROVIDERS:
        return {"type": "function", "function": {"name": force_tool}}
    if p in _REQUIRED_ONLY_FORCE_PROVIDERS:
        return "required"
    return None


def tool_list_contains(tools: Any, tool_name: str) -> bool:
    """True iff ``tool_name`` appears in an OpenAI-shape tools list.

    Accepts the canonical ``[{"type": "function", "function": {"name": ...}}]``
    shape FERAL uses on the wire and the flat ``{"name": ...}`` shape
    some upstream callers assemble. Returns ``False`` on any malformed
    input — the caller treats absence as "don't force this tool" so a
    typo or empty list silently degrades to auto instead of raising.
    """
    if not tool_name or not isinstance(tools, list):
        return False
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function")
        if isinstance(fn, dict) and fn.get("name") == tool_name:
            return True
        if t.get("name") == tool_name:
            return True
    return False


__all__ = [
    "to_anthropic_blocks",
    "to_gemini_parts",
    "translate_content_for_anthropic",
    "to_provider_tool_choice",
    "tool_list_contains",
]


# ─────────────────────────────────────────────────────────────────────
# Image-bearing TOOL RESULTS
# ─────────────────────────────────────────────────────────────────────
#
# THE DEFECT THIS SECTION FIXES
# -----------------------------
# Every tool result reaching the conversation history went through
# ``skills.result_budget.serialize_tool_result``, which stringifies the
# result and clamps it to the tool's tier budget. ``gui_computer_use``
# resolves to the ``standard`` tier (max_result_chars = 2000), so a
# screenshot -- ~400 000 base64 chars under ``data.image_base64`` --
# came out as:
#
#   '{"success": true, "data": {"image_base64": "/9j/4AAQ...'   (1405 chars)
#   ...ending in "You have NOT seen the whole result".
#
# The model therefore never saw a single screenshot FERAL took. Half a
# base64 string is not a degraded image, it is a decode error, so the
# only honest outcomes are "whole image" or "no image plus a sentence
# saying why".
#
# THE SHAPE OF THE FIX
# --------------------
# 1. The image is *lifted out* of the JSON before the text budget runs
#    (``extract_tool_result_images``). The text half -- success,
#    dimensions, dpi_scale, path, format -- is budgeted exactly as
#    before; only the blob leaves the character budget.
# 2. The lifted image travels out-of-band (the orchestrator keeps a
#    per-session side table keyed by ``tool_call_id``; the history rows
#    themselves stay pure-text and wire-safe on every provider).
# 3. Immediately before a chat call, ``materialize_tool_result_images``
#    splices the images back in using the shape the SELECTED PROVIDER
#    accepts. This cannot be one code path:
#
#      * Anthropic Messages API  -- an ``image`` block is legal INSIDE
#        the ``tool_result`` content array. Verified against the current
#        computer-use tool docs (platform.claude.com,
#        /en/agents-and-tools/tool-use/computer-use-tool, "Implement
#        proper screenshot handling"), which shows exactly:
#            {"role": "user", "content": [{"type": "tool_result",
#             "tool_use_id": "...", "content": [{"type": "image",
#             "source": {"type": "base64", "media_type": "image/png",
#                        "data": "iVBORw0KGgo..."}}]}]}
#        FERAL's canonical internal shape stays OpenAI-flavoured, so we
#        emit ``[{type:text},{type:image_url}]`` as the tool row's
#        content and let ``llm_anthropic_shape._convert_messages_for_anthropic``
#        translate it at the wire boundary.
#
#      * OpenAI chat-completions -- images are NOT accepted on a
#        ``role:"tool"`` message; tool-message content is text-only and
#        image parts are only legal on a ``role:"user"`` message. So the
#        tool row keeps its text string and the image is delivered in a
#        FOLLOW-UP ``role:"user"`` message placed after the whole
#        contiguous run of tool rows (splitting the run would orphan the
#        assistant's tool_calls and 400).
#
#      * Gemini -- native ``:generateContent`` has its own parts shape
#        and its ``functionResponse`` payload is JSON-only, so the image
#        again rides a follow-up user turn, built with
#        ``tool_result_images_as_gemini_content``. FERAL's runtime
#        ``gemini`` provider currently drives Google's OpenAI-compat
#        endpoint, so it resolves to the follow-up-user mode above; the
#        native helper is kept (and tested) for the native surface.
#
#      * Anything that cannot take an image at all (DeepSeek, a
#        text-only local engine, an unknown provider) -- the image is
#        NOT sent and the tool row is annotated with an explicit
#        ``_image_delivery`` sentence saying a screenshot exists and
#        could not be delivered. Silent degradation is the defect class
#        this repo is fighting; an undelivered image the model is never
#        told about is the same bug wearing a different hat.

import base64 as _base64
import json as _json
import os as _os
from dataclasses import dataclass


# Field names FERAL's own capture tools actually use. Verified by
# reading the implementations (NOT guessed):
#   skills/impl/gui_computer_use.py::_screenshot  -> data.image_base64 (+ format, dpi_scale)
#   skills/impl/screen_capture.py::_capture       -> data.image_b64    (+ encoding, path, size_bytes)
#   skills/impl/browser_use.py::screenshot        -> image_b64         (+ format)
#   skills/impl/agentic_computer_use.py           -> builds its own image_url part
# The rest are defensive synonyms for third-party / MCP tools.
TOOL_RESULT_IMAGE_FIELDS: tuple[str, ...] = (
    "image_base64",
    "image_b64",
    "img_b64",
    "base64_image",
    "screenshot_base64",
    "screenshot_b64",
    "image_data",
    "image_url",
    "screenshot",
    "image",
)

# base64 magic prefixes -> media type. Sniffed from the payload itself so
# a tool that forgets its ``format`` sibling still gets a correct
# media_type instead of a wrong hardcoded one.
_B64_MAGIC: tuple[tuple[str, str], ...] = (
    ("/9j/", "image/jpeg"),
    ("iVBORw0KGgo", "image/png"),
    ("R0lGOD", "image/gif"),
    ("UklGR", "image/webp"),
    ("Qk", "image/bmp"),
)

_FORMAT_SIBLING_KEYS: tuple[str, ...] = ("format", "encoding", "mime_type", "media_type")

_B64_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\n\r"
)

# A real screenshot is hundreds of thousands of base64 chars. The floor
# exists so a short path string under a key like ``screenshot`` is not
# mistaken for image bytes.
_MIN_B64_IMAGE_CHARS = 512


def _env_int(name: str, default: int) -> int:
    raw = (_os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring non-integer %s=%r; using %d", name, raw, default)
        return default
    return value if value > 0 else default


def _max_image_b64_chars() -> int:
    """Largest base64 payload we will put on the wire, in characters.

    5 000 000 base64 chars is ~3.75 MB of image bytes, comfortably under
    Anthropic's 5 MB per-image ceiling and OpenAI's 20 MB request
    ceiling. Anything larger is REFUSED WHOLE (never sliced) -- see
    ``extract_tool_result_images``.
    """
    return _env_int("FERAL_TOOL_IMAGE_MAX_B64_CHARS", 5_000_000)


def _max_images_per_result() -> int:
    return _env_int("FERAL_TOOL_IMAGE_MAX_PER_RESULT", 4)


def _image_detail() -> str:
    raw = (_os.environ.get("FERAL_TOOL_IMAGE_DETAIL") or "").strip().lower()
    return raw if raw in ("low", "high", "auto") else "high"


@dataclass(frozen=True)
class ToolResultImage:
    """One image lifted out of a tool result.

    ``data_url`` is always a complete, decodable payload -- either a
    ``data:image/...;base64,...`` URL or an absolute http(s) URL. There
    is deliberately no "partial" state: a truncated base64 string is a
    decode error, not a degraded image.
    """

    data_url: str
    media_type: str
    field_path: str
    payload_chars: int
    tool_name: str = ""

    def to_dict(self) -> dict:
        return {
            "data_url": self.data_url,
            "media_type": self.media_type,
            "field_path": self.field_path,
            "payload_chars": self.payload_chars,
            "tool_name": self.tool_name,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "ToolResultImage":
        return cls(
            data_url=str(raw.get("data_url") or ""),
            media_type=str(raw.get("media_type") or "image/png"),
            field_path=str(raw.get("field_path") or ""),
            payload_chars=int(raw.get("payload_chars") or 0),
            tool_name=str(raw.get("tool_name") or ""),
        )


def _sniff_media_type(b64: str, sibling_hint: str = "") -> str:
    hint = (sibling_hint or "").strip().lower().lstrip(".")
    if hint.startswith("image/"):
        return hint
    if hint in ("jpg", "jpeg"):
        return "image/jpeg"
    if hint in ("png", "gif", "webp", "bmp"):
        return f"image/{hint}"
    head = b64[:16]
    for prefix, media_type in _B64_MAGIC:
        if head.startswith(prefix):
            return media_type
    return "image/png"


def _looks_like_b64_image(value: str) -> bool:
    if len(value) < _MIN_B64_IMAGE_CHARS:
        return False
    # Only sample the head: scanning 400 000 chars per candidate field on
    # every tool result is a measurable cost for zero extra confidence.
    sample = value[:256]
    if not set(sample) <= _B64_ALPHABET:
        return False
    head = value[:16]
    if any(head.startswith(prefix) for prefix, _ in _B64_MAGIC):
        return True
    # Unknown magic but unmistakably base64-shaped and huge: accept, and
    # let the media-type sniff fall back to image/png.
    try:
        _base64.b64decode(value[:64] + "==", validate=False)
    except Exception:
        return False
    return True


def _as_image_payload(key: str, value: Any) -> tuple[str, str] | None:
    """Return ``(data_url, media_type)`` if ``value`` under ``key`` is an
    image payload, else ``None``. Pure; no IO."""
    if not isinstance(value, str) or not value:
        return None
    m = _DATA_URL_RE.match(value)
    if m:
        return value, m.group("media_type")
    lowered = value[:8].lower()
    if lowered.startswith(("http://", "https:")) and key in (
        "image_url", "image", "screenshot", "image_data",
    ):
        return value, "image/*"
    if _looks_like_b64_image(value):
        return "", ""  # caller supplies the sibling hint; see _walk
    return None


def _placeholder(media_type: str, chars: int, delivered: bool) -> str:
    if delivered:
        return (
            f"[image lifted out of this JSON: {chars} base64 chars, "
            f"{media_type}. It is delivered WHOLE as an image content "
            "block, not as text, so it is not subject to the text budget "
            "and was not truncated.]"
        )
    return (
        f"[image removed from this JSON: {chars} base64 chars, "
        f"{media_type}. See _image_delivery for whether it reached you.]"
    )


def extract_tool_result_images(
    result: Any,
    *,
    tool_name: str = "",
    deliverable: bool = True,
    max_images: int | None = None,
    max_b64_chars: int | None = None,
    max_depth: int = 8,
) -> tuple[Any, list[ToolResultImage], list[str]]:
    """Split a tool result into (text half, whole images, omission notes).

    Returns a DEEP COPY of ``result`` in which every recognised image
    payload has been replaced by a short human-readable placeholder, the
    list of images that can be sent whole, and a list of plain-English
    notes for images that were found but deliberately NOT sent (too
    large, too many). Nothing is ever half-sent.

    The returned text half is what goes through
    ``skills.result_budget.serialize_tool_result`` -- so dimensions,
    ``dpi_scale``, ``path``, ``format`` and ``success`` are still
    budgeted exactly as they were before this feature existed.
    """
    if max_images is None:
        max_images = _max_images_per_result()
    if max_b64_chars is None:
        max_b64_chars = _max_image_b64_chars()

    images: list[ToolResultImage] = []
    notes: list[str] = []

    def _walk(node: Any, path: str, depth: int) -> Any:
        if depth <= 0:
            return node
        if isinstance(node, dict):
            out: dict = {}
            for key, value in node.items():
                child_path = f"{path}.{key}" if path else str(key)
                if key in TOOL_RESULT_IMAGE_FIELDS and isinstance(value, str):
                    hit = _as_image_payload(key, value)
                    if hit is not None:
                        data_url, media_type = hit
                        if not data_url:
                            hint = ""
                            for sibling in _FORMAT_SIBLING_KEYS:
                                candidate = node.get(sibling)
                                if isinstance(candidate, str) and candidate:
                                    hint = candidate
                                    break
                            media_type = _sniff_media_type(value, hint)
                            data_url = f"data:{media_type};base64,{value}"
                        payload_chars = len(value)
                        if len(images) >= max_images:
                            notes.append(
                                f"{child_path}: additional image not sent "
                                f"(limit {max_images} images per tool result)."
                            )
                            out[key] = _placeholder(media_type, payload_chars, False)
                            continue
                        if payload_chars > max_b64_chars:
                            notes.append(
                                f"{child_path}: image NOT sent -- {payload_chars} "
                                f"base64 chars exceeds the {max_b64_chars}-char "
                                "wire limit. A partial base64 string is a decode "
                                "error, not a smaller image, so nothing was sent. "
                                "Re-capture at a lower resolution or higher "
                                "compression."
                            )
                            out[key] = _placeholder(media_type, payload_chars, False)
                            continue
                        images.append(ToolResultImage(
                            data_url=data_url,
                            media_type=media_type,
                            field_path=child_path,
                            payload_chars=payload_chars,
                            tool_name=tool_name,
                        ))
                        out[key] = _placeholder(
                            media_type, payload_chars, deliverable,
                        )
                        continue
                out[key] = _walk(value, child_path, depth - 1)
            return out
        if isinstance(node, list):
            return [
                _walk(item, f"{path}[{i}]", depth - 1)
                for i, item in enumerate(node)
            ]
        return node

    stripped = _walk(result, "", max_depth)
    if not images and not notes:
        # Nothing image-shaped: hand back the ORIGINAL object so the
        # no-image path is byte-identical to the pre-feature behaviour.
        return result, [], []
    return stripped, images, notes


def result_contains_image(result: Any) -> bool:
    """Cheap predicate: does this tool result carry an image payload?"""
    _stripped, images, notes = extract_tool_result_images(result)
    return bool(images or notes)


# ─────────────────────────────────────────────────────────────────────
# Provider matrix
# ─────────────────────────────────────────────────────────────────────

IMAGE_DELIVERY_ANTHROPIC_BLOCKS = "anthropic_tool_result_blocks"
IMAGE_DELIVERY_FOLLOWUP_USER = "followup_user_message"
IMAGE_DELIVERY_GEMINI_PARTS = "gemini_native_parts"
IMAGE_DELIVERY_NONE = "none"

# Every runtime provider in ``agents.llm_provider.SUPPORTED_RUNTIME_PROVIDERS``
# that speaks OpenAI chat-completions. They all share the same
# constraint: image parts are legal on ``role:"user"`` only, so a tool
# result's image rides a follow-up user message.
_OPENAI_COMPAT_IMAGE_PROVIDERS: frozenset[str] = frozenset({
    "openai",
    "openrouter",
    "groq",
    "gemini",      # FERAL drives Google's /v1beta/openai compat endpoint
    "deepseek",    # text-only in practice; gated by vision_supported
    "kimi",
    "qwen",
    "lmstudio",
    "ollama",
    "xai",
    "mistral",
    "codex",
    "local",
    "hybrid",
})


def image_delivery_mode(provider: str, *, vision_supported: bool = True) -> str:
    """Which wire shape carries a tool-result image on *provider*.

    ``vision_supported`` is the caller's answer from
    ``LLMProvider._vision_support_status()`` -- capability is per-MODEL
    for several providers (an Ollama text model, a non-vision OpenRouter
    route, DeepSeek), so provider name alone is not sufficient. When it
    is False the answer is ``IMAGE_DELIVERY_NONE`` and the caller MUST
    still tell the model an image existed.
    """
    if not vision_supported:
        return IMAGE_DELIVERY_NONE
    p = (provider or "").strip().lower()
    if p == "anthropic":
        return IMAGE_DELIVERY_ANTHROPIC_BLOCKS
    if p in ("gemini_native", "google_genai"):
        return IMAGE_DELIVERY_GEMINI_PARTS
    if p in _OPENAI_COMPAT_IMAGE_PROVIDERS:
        return IMAGE_DELIVERY_FOLLOWUP_USER
    # Unknown provider: never guess a shape onto the wire.
    return IMAGE_DELIVERY_NONE


def image_url_block(image: ToolResultImage) -> dict:
    """Canonical internal (OpenAI-flavoured) image content block."""
    return {
        "type": "image_url",
        "image_url": {"url": image.data_url, "detail": _image_detail()},
    }


def _annotate_tool_text(content: Any, note: str) -> Any:
    """Attach ``note`` to a serialized tool-result string.

    ``serialize_tool_result`` guarantees valid JSON, so we add a sibling
    key rather than appending prose that would break that guarantee.
    Falls back to a plain suffix when the content is not a JSON object.
    """
    if not isinstance(content, str):
        return content
    try:
        parsed = _json.loads(content)
    except (ValueError, TypeError):
        return f"{content}\n{note}"
    if isinstance(parsed, dict):
        parsed["_image_delivery"] = note
        return _json.dumps(parsed, default=str)
    return f"{content}\n{note}"


_UNDELIVERED_NOTE = (
    "An image WAS produced by this tool but could NOT be delivered to "
    "you: the active model/provider does not accept image input. You have "
    "not seen it. Do not guess at its contents -- either ask the operator "
    "to switch to a vision-capable model, or use a tool that returns a "
    "text description (e.g. an accessibility-tree read or a VLM describe "
    "endpoint)."
)

_PRUNED_NOTE = (
    "The image from this tool result has been pruned from the "
    "conversation to bound context (screenshots cost roughly 1000-1800 "
    "input tokens each). You can no longer see it. Re-run the capture "
    "tool if you need the current screen."
)


def _followup_user_message(entries: list[tuple[str, str, ToolResultImage]]) -> dict:
    """Build the ``role:"user"`` message that carries tool-result images
    on OpenAI-compatible providers."""
    blocks: list[dict] = []
    labels = []
    for tool_name, call_id, image in entries:
        labels.append(f"{tool_name or 'tool'} (tool_call_id={call_id or 'unknown'})")
    header = (
        "Image output from the tool call(s) above: "
        + "; ".join(labels)
        + ". This provider does not accept image content on a tool "
        "message, so the image is delivered here instead. It is the "
        "complete, untruncated image."
    )
    blocks.append({"type": "text", "text": header})
    for _tool_name, _call_id, image in entries:
        blocks.append(image_url_block(image))
    return {"role": "user", "content": blocks}


def tool_result_images_as_gemini_content(
    images: list[ToolResultImage],
    *,
    tool_name: str = "",
    tool_call_id: str = "",
) -> dict:
    """Native-Gemini follow-up turn carrying tool-result images.

    Gemini's ``functionResponse`` part takes a JSON ``response`` object
    only, so an image cannot ride the function response itself; it goes
    in a subsequent ``user`` content whose parts are Gemini-native
    (``inline_data`` / ``file_data``). FERAL's runtime ``gemini``
    provider currently uses Google's OpenAI-compatibility endpoint, so
    the live path resolves to :data:`IMAGE_DELIVERY_FOLLOWUP_USER`; this
    helper is the native shape for the native surface.
    """
    header = (
        f"Image output from tool call {tool_name or 'tool'} "
        f"(id={tool_call_id or 'unknown'}). Complete, untruncated."
    )
    openai_blocks: list[Any] = [{"type": "text", "text": header}]
    openai_blocks.extend(image_url_block(img) for img in images)
    return {"role": "user", "parts": to_gemini_parts(openai_blocks)}


def materialize_tool_result_images(
    messages: list[Any],
    images_by_call_id: dict[str, dict] | None,
    mode: str,
) -> list[Any]:
    """Splice out-of-band tool-result images into ``messages`` for *mode*.

    ``images_by_call_id`` maps ``tool_call_id`` to
    ``{"images": [ToolResultImage-as-dict, ...], "pruned": bool,
      "tool_name": str}``.

    Returns a NEW list; the input messages are never mutated (the caller's
    conversation history must stay provider-agnostic and text-only so it
    can be replayed on any provider on the next turn or after a failover).

    Ordering guarantee for :data:`IMAGE_DELIVERY_FOLLOWUP_USER`: the
    follow-up user message is emitted after the LAST tool row of a
    contiguous run. Inserting it between two tool rows of the same
    assistant turn would orphan the remaining tool_call_ids and the
    request would 400.
    """
    if not images_by_call_id:
        return list(messages)

    out: list[Any] = []
    pending: list[tuple[str, str, ToolResultImage]] = []

    def _flush() -> None:
        nonlocal pending
        if pending:
            out.append(_followup_user_message(pending))
            pending = []

    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            _flush()
            out.append(msg)
            continue

        call_id = str(msg.get("tool_call_id") or msg.get("id") or "")
        entry = images_by_call_id.get(call_id)
        if not entry:
            out.append(msg)
            continue

        row = dict(msg)
        if entry.get("pruned"):
            row["content"] = _annotate_tool_text(row.get("content"), _PRUNED_NOTE)
            out.append(row)
            continue

        images = [
            ToolResultImage.from_dict(raw)
            for raw in (entry.get("images") or [])
            if isinstance(raw, dict) and raw.get("data_url")
        ]
        if not images:
            out.append(msg)
            continue

        if mode == IMAGE_DELIVERY_ANTHROPIC_BLOCKS:
            text = row.get("content")
            blocks: list[Any] = []
            if isinstance(text, str) and text:
                blocks.append({"type": "text", "text": text})
            elif isinstance(text, list):
                blocks.extend(text)
            blocks.extend(image_url_block(img) for img in images)
            row["content"] = blocks
            out.append(row)
            continue

        if mode in (IMAGE_DELIVERY_FOLLOWUP_USER, IMAGE_DELIVERY_GEMINI_PARTS):
            out.append(row)
            tool_name = str(entry.get("tool_name") or row.get("name") or "")
            pending.extend((tool_name, call_id, img) for img in images)
            continue

        # IMAGE_DELIVERY_NONE (or an unrecognised mode): the image does
        # not go on the wire, and the model is told so in words.
        row["content"] = _annotate_tool_text(row.get("content"), _UNDELIVERED_NOTE)
        out.append(row)

    _flush()
    return out


# ─────────────────────────────────────────────────────────────────────
# Screenshot history pruning
# ─────────────────────────────────────────────────────────────────────
#
# SOURCE (read, not guessed): Anthropic computer-use tool documentation,
# https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool
# section "Manage screenshot history for prompt caching":
#
#   "Long agent loops accumulate screenshots quickly (roughly 1,000-1,800
#    input tokens each). To keep Prompt caching effective while bounding
#    context:
#      * Place one cache_control breakpoint after the system prompt and
#        tool definitions, and up to three more on the most recent
#        tool_result blocks, advancing them each turn.
#      * Prune old screenshots in *batches*, not one each turn. Dropping a
#        screenshot every turn changes the prefix every turn and
#        invalidates the cache. A reasonable default is to keep the last
#        three screenshots and prune every 25 turns, so the prefix stays
#        byte-identical between prune events."
#
# Hence the defaults below: keep 3, prune every 25 agent rounds. A hard
# cap is layered on top so a pathological loop cannot hold 25 rounds'
# worth of 1.8k-token images before the next scheduled prune; it is set
# well above the batch interval's normal working set so the batch path,
# not the cap, is the usual trigger.

SCREENSHOT_KEEP_LAST_DEFAULT = 3
SCREENSHOT_PRUNE_EVERY_DEFAULT = 25
SCREENSHOT_HARD_CAP_DEFAULT = 12


def screenshot_keep_last() -> int:
    return _env_int("FERAL_TOOL_IMAGE_KEEP_LAST", SCREENSHOT_KEEP_LAST_DEFAULT)


def screenshot_prune_every() -> int:
    return _env_int("FERAL_TOOL_IMAGE_PRUNE_EVERY", SCREENSHOT_PRUNE_EVERY_DEFAULT)


def screenshot_hard_cap() -> int:
    return _env_int("FERAL_TOOL_IMAGE_HARD_CAP", SCREENSHOT_HARD_CAP_DEFAULT)


def should_prune_images(
    *,
    round_counter: int,
    live_images: int,
    keep_last: int | None = None,
    prune_every: int | None = None,
    hard_cap: int | None = None,
) -> bool:
    """Batch-prune scheduler.

    Returns True only on a scheduled batch boundary (every
    ``prune_every`` agent rounds) or when the live image count breaches
    the hard cap. Between boundaries it returns False so the prompt-cache
    prefix stays byte-identical, which is the whole point of batching.
    """
    keep_last = screenshot_keep_last() if keep_last is None else keep_last
    prune_every = screenshot_prune_every() if prune_every is None else prune_every
    hard_cap = screenshot_hard_cap() if hard_cap is None else hard_cap
    if live_images <= keep_last:
        return False
    if live_images > hard_cap:
        return True
    return round_counter > 0 and round_counter % prune_every == 0


def prune_tool_result_images(
    images_by_call_id: dict[str, dict],
    order: list[str],
    *,
    keep_last: int | None = None,
) -> list[str]:
    """Drop all but the ``keep_last`` most recent images from the side table.

    ``order`` is the append-order of ``tool_call_id``s. Pruned entries are
    NOT deleted -- they are flipped to ``{"pruned": True}`` so
    ``materialize_tool_result_images`` can tell the model in words that a
    screenshot used to be there. Returns the list of pruned call ids.
    """
    keep_last = screenshot_keep_last() if keep_last is None else keep_last
    live = [
        call_id for call_id in order
        if isinstance(images_by_call_id.get(call_id), dict)
        and not images_by_call_id[call_id].get("pruned")
        and images_by_call_id[call_id].get("images")
    ]
    if len(live) <= keep_last:
        return []
    pruned: list[str] = []
    for call_id in live[: len(live) - keep_last]:
        entry = images_by_call_id[call_id]
        images_by_call_id[call_id] = {
            "images": [],
            "pruned": True,
            "tool_name": entry.get("tool_name", ""),
        }
        pruned.append(call_id)
    return pruned


__all__ += [
    "TOOL_RESULT_IMAGE_FIELDS",
    "ToolResultImage",
    "extract_tool_result_images",
    "result_contains_image",
    "image_delivery_mode",
    "image_url_block",
    "materialize_tool_result_images",
    "tool_result_images_as_gemini_content",
    "prune_tool_result_images",
    "should_prune_images",
    "screenshot_keep_last",
    "screenshot_prune_every",
    "screenshot_hard_cap",
    "IMAGE_DELIVERY_ANTHROPIC_BLOCKS",
    "IMAGE_DELIVERY_FOLLOWUP_USER",
    "IMAGE_DELIVERY_GEMINI_PARTS",
    "IMAGE_DELIVERY_NONE",
    "SCREENSHOT_KEEP_LAST_DEFAULT",
    "SCREENSHOT_PRUNE_EVERY_DEFAULT",
    "SCREENSHOT_HARD_CAP_DEFAULT",
]
