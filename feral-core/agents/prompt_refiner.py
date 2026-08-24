"""Phase 2 (audit-r10 overhaul) — PromptRefiner.

Operator complaint #14:
> "Can we have a layer that refines the prompt that is coming from the user
>  so when it sends it to the brain, the orchestrator knows what to do in a
>  clean way?"

Position in the pipeline (Phase 2 is the new step):

    client → /v1/session or /v1/node WS → server.py chat path
                                           ↓
                                  ➤ PromptRefiner.refine(text, ctx)
                                           ↓
                                    RefinedRequest{...}
                                           ↓
                              orchestrator.handle_command(refined_text, context)
                                  (existing _route_prompt / multi-agent / mitosis
                                  layers consume refined_text + suggested_skills)

What the refiner emits is a small structured envelope so:
  1. The orchestrator can route by `device_target` (Phase 1 wire field) —
     the iOS operator can finally say "open my Mac browser" and the brain
     dispatches to `brain_host` without keyword-guessing.
  2. The Mind tab (Phase 10) renders a "what the brain heard" preview so
     the operator sees the inferred intent BEFORE the LLM acts on it.
  3. Downstream routers (`_route_prompt`, `MultiAgentOrchestrator`,
     `MitosisEngine`) consume `suggested_skills` instead of starting from
     scratch — overlap kept manageable.

Constraints (per plan):
  - Fast model only (`gpt-5.4-mini` / `composer-2-fast`). Latency budget
    < 400ms p50.
  - Cache by hash of `(text, device_target_hint, last_2_turns)`.
  - Fast-path skip for short utterances + known control verbs (mute, stop,
    cancel, end, yes, no) so trivial inputs don't pay the refiner tax.
  - Feature-flagged via `FERAL_PROMPT_REFINER` env var. Default OFF for
    this PR; flips on once shadow-mode metrics show the refiner is faster
    than the LLM-route-prompt path it replaces.
  - LLM-unavailable fallback: return an identity `RefinedRequest` so the
    orchestrator never gets a None — wire contract is single-shape always.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger("feral.prompt_refiner")


# Public wire contract for what the refiner emits. Stable across phases —
# downstream code (orchestrator, Mind tab, observability) reads these
# fields by name.
class RefinedRequest(BaseModel):
    """Structured interpretation of a user utterance.

    Fields are intentionally tolerant: every value has a safe default so
    a partial / malformed LLM output still yields a usable envelope.
    """

    refined_text: str = ""
    """The cleaned + disambiguated rewrite the orchestrator should treat as
    the user message. For trivial inputs this equals `raw_text`."""

    raw_text: str = ""
    """Original input verbatim. Kept for audit + Mind tab observability."""

    intent_class: Literal[
        "chat",           # conversational / open-ended
        "device_action",  # "do X on device Y"
        "query",          # information lookup (calendar, contacts, web)
        "task",           # long-running / scheduled work
        "ambient",        # status / passive observation
        "control",        # voice/session control (mute, stop)
        "unknown",
    ] = "unknown"

    slots: dict[str, Any] = Field(default_factory=dict)
    """Extracted entities: `{"contact": "Mom", "when": "tomorrow 9am", ...}`.
    Free-form by design; downstream consumers handle missing keys."""

    device_target: Optional[Literal["brain", "phone", "glasses", "auto"]] = None
    """When the refiner can resolve the target device deterministically.
    Wired to `ExecutionSurfacePolicy` via the `context["device_target"]`
    path landed in Phase 1."""

    suggested_skills: list[str] = Field(default_factory=list)
    """Skill ids the refiner thinks are needed. Consumed by
    `_route_prompt` / multi-agent / mitosis as a hint, not a mandate."""

    confidence: float = 0.0
    """0.0 (fast-path / no LLM) → 1.0 (LLM returned high-confidence)."""

    source: Literal["llm", "fast_path", "fallback", "cache"] = "fallback"
    """How this envelope was produced. `fallback` means the LLM was
    unavailable and the refiner returned an identity record."""

    latency_ms: int = 0
    """Wall-clock time the refiner spent. Reported for observability."""


# ───────────────────────── fast-path heuristics ──────────────────────────


# Single-token control verbs — never worth an LLM round-trip. These map
# 1:1 to the `control` intent class.
_CONTROL_TOKENS: frozenset[str] = frozenset({
    "mute", "unmute",
    "stop", "end", "quit", "cancel", "abort",
    "yes", "no", "ok", "okay", "nope", "yep", "yeah",
    "skip", "pass", "later",
    "pause", "resume",
})

_MIN_REFINER_CHARS = 12
"""Inputs shorter than this skip the LLM. Threshold tuned to skip
"call mom" (8 chars, control-class via slot extraction) but include
"call mom on facetime" (20 chars, device_target=phone, etc.)."""


def _is_control_token(text: str) -> bool:
    norm = text.strip().lower().rstrip(".!?")
    return norm in _CONTROL_TOKENS


def _device_target_keyword(text: str) -> Optional[str]:
    """Cheap deterministic device_target extraction. Returns one of
    `brain` / `phone` / `glasses` if the text mentions the device by
    name; None if ambiguous (so the LLM gets to decide).

    Match rules are intentionally conservative — a wrong guess is
    worse than no guess, because the orchestrator falls back to
    `http_api` for None and the operator's existing tools still work.
    """
    lower = text.lower()
    # "on my mac" / "my mac" / "the mac"
    if re.search(r"\b(on\s+)?(my|the)\s+mac\b", lower) or "macbook" in lower:
        return "brain"
    # "on the brain" / "via the brain"
    if re.search(r"\b(on|via|through)\s+(the\s+)?brain\b", lower):
        return "brain"
    # "on my phone" / "from my phone"
    if re.search(r"\b(on|from|via)\s+(my|the)\s+(phone|iphone)\b", lower):
        return "phone"
    # "via the glasses" / "on my glasses"
    if re.search(r"\b(on|via|through)\s+(my|the)\s+glasses\b", lower):
        return "glasses"
    return None


def infer_device_target(text: str) -> Optional[str]:
    """Where the user said to run this, or None if they did not say.

    Public, deterministic, and deliberately NOT behind
    `FERAL_PROMPT_REFINER`. That flag gates LLM rewriting of the user's
    text, which is a much larger behaviour change than reading which
    device a sentence names, and the two were coupled only because the
    keyword extractor happened to live inside the refiner.

    The coupling had a visible cost: with the flag off (the default),
    `refine` returns an identity envelope that echoes back whatever
    `device_target_hint` the caller passed and infers nothing, so every
    client had to reimplement these rules to reach its own Mac. The iOS
    client did exactly that, and a second copy of a security-routing
    rule is a drift bug waiting to happen.

    Callers apply this only when no explicit `device_target` was sent.
    An explicit field always wins: the client knows something the text
    does not say.
    """
    return _device_target_keyword(text or "")


# ───────────────────────── cache ──────────────────────────


class _RefinerCache:
    """Bounded in-process LRU. `(text, device_target_hint, history_hash)`
    → `RefinedRequest`. Hit rate is the whole game for keeping refiner
    latency below the orchestrator-tool path it competes with."""

    def __init__(self, max_entries: int = 256) -> None:
        self._data: dict[str, RefinedRequest] = {}
        self._order: list[str] = []
        self._max = max_entries

    @staticmethod
    def key(text: str, device_target_hint: Optional[str], history: str) -> str:
        h = hashlib.sha1()
        h.update(text.strip().lower().encode("utf-8"))
        h.update(b"\x00")
        h.update((device_target_hint or "").encode("utf-8"))
        h.update(b"\x00")
        h.update(history.encode("utf-8"))
        return h.hexdigest()

    def get(self, key: str) -> Optional[RefinedRequest]:
        return self._data.get(key)

    def put(self, key: str, value: RefinedRequest) -> None:
        if key in self._data:
            self._data[key] = value
            return
        self._data[key] = value
        self._order.append(key)
        while len(self._order) > self._max:
            evict = self._order.pop(0)
            self._data.pop(evict, None)


_DEFAULT_CACHE = _RefinerCache()


# ───────────────────────── refiner main entry ──────────────────────────


_SYSTEM_PROMPT = (
    "You are FERAL's prompt refiner. The user's message is going to a "
    "tool-using orchestrator next. Your job is to return STRICT JSON "
    "with these fields:\n"
    "  refined_text: the cleaned, disambiguated rewrite the orchestrator "
    "should treat as the user message. Resolve relative time references "
    "to absolute ones if the input gives enough signal. Do not invent.\n"
    "  intent_class: one of "
    "[\"chat\",\"device_action\",\"query\",\"task\",\"ambient\",\"control\",\"unknown\"].\n"
    "  slots: an object with extracted entities — names, times, places, "
    "apps, files. Free-form keys.\n"
    "  device_target: one of [\"brain\",\"phone\",\"glasses\",\"auto\",null]. "
    "`brain` = run on the Mac that hosts FERAL. `phone` = run on the iOS/"
    "Android companion natively (CallKit / MusicKit / etc.). `glasses` = "
    "run via the BLE glasses (always bridged through the phone). "
    "`auto` = brain decides; `null` = unspecified.\n"
    "  suggested_skills: an array of skill ids (e.g. "
    "[\"calendar_google\",\"desktop_control\"]) the orchestrator should "
    "consider. Empty array if unsure.\n"
    "  confidence: 0.0 to 1.0.\n"
    "Return ONLY the JSON object, no prose. No backticks. No trailing text."
)


async def refine(
    text: str,
    *,
    llm: Any = None,
    device_target_hint: Optional[str] = None,
    history: Optional[list[dict]] = None,
    cache: Optional[_RefinerCache] = None,
    fast_model: Optional[str] = None,
) -> RefinedRequest:
    """Produce a `RefinedRequest` for `text`.

    Args:
        text: Raw user input.
        llm: An `LLMProvider`-shaped object with `async chat(messages,
            tools, temperature, max_tokens) -> dict` + `.available: bool`.
            When None or unavailable, returns the fallback envelope.
        device_target_hint: Caller-supplied hint (e.g. iOS payload sent
            `device_target=brain`). Used as the cache key AND echoed onto
            the result when the LLM doesn't override it.
        history: Optional list of last few `{role, text}` turns for
            cache-key composition + LLM context. Capped at 2 turns inside.
        cache: Test-injectable cache. Default uses module-level singleton.
        fast_model: Override the model used. Default reads
            `FERAL_PROMPT_REFINER_MODEL` env or falls back to whatever
            the LLM provider has configured.

    Behavior contract:
      - Always returns a `RefinedRequest`. Never raises, never returns None.
      - `refined_text` is at minimum `text` (identity rewrite) — downstream
        callers can use the result unconditionally.
      - `device_target_hint` is preserved on the result when the LLM
        returns None for the field, so caller-supplied targeting wins
        over LLM ambiguity.
      - Disabled when `FERAL_PROMPT_REFINER` is unset / falsy; returns
        an identity envelope so the rest of the chain runs unchanged.
    """
    start = time.monotonic()
    cache = cache if cache is not None else _DEFAULT_CACHE

    raw = text or ""

    # Feature-flag off → identity envelope. Lets us land the wiring
    # without changing behavior; flip the flag when ready.
    if os.environ.get("FERAL_PROMPT_REFINER", "").lower() not in ("1", "true", "yes", "on"):
        return RefinedRequest(
            refined_text=raw,
            raw_text=raw,
            intent_class="unknown",
            device_target=device_target_hint,
            source="fallback",
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    # Fast-path: control tokens.
    if _is_control_token(raw):
        return RefinedRequest(
            refined_text=raw,
            raw_text=raw,
            intent_class="control",
            device_target=device_target_hint,
            confidence=0.95,
            source="fast_path",
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    # Fast-path: too short for LLM to add value.
    if len(raw.strip()) < _MIN_REFINER_CHARS:
        kw_target = _device_target_keyword(raw) or device_target_hint
        return RefinedRequest(
            refined_text=raw,
            raw_text=raw,
            intent_class="unknown",
            device_target=kw_target,
            confidence=0.5 if kw_target else 0.2,
            source="fast_path",
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    # Cache lookup. Keyed on the last 2 turns so the same utterance in
    # different conversation contexts gets a fresh refinement.
    hist = list(history or [])[-2:]
    history_blob = json.dumps(
        [{"r": h.get("role", ""), "t": (h.get("text") or h.get("content") or "")[:200]}
         for h in hist],
        sort_keys=True,
    )
    cache_key = _RefinerCache.key(raw, device_target_hint, history_blob)
    cached = cache.get(cache_key)
    if cached is not None:
        # Return a copy with updated latency + source so observability
        # reflects this turn, not the original.
        return cached.model_copy(update={
            "source": "cache",
            "latency_ms": int((time.monotonic() - start) * 1000),
        })

    # LLM call.
    if llm is None or not getattr(llm, "available", False):
        # Identity + keyword target so callers can still benefit from the
        # cheap deterministic device_target extraction even without an LLM.
        kw_target = _device_target_keyword(raw) or device_target_hint
        result = RefinedRequest(
            refined_text=raw,
            raw_text=raw,
            intent_class="unknown",
            device_target=kw_target,
            confidence=0.3 if kw_target else 0.0,
            source="fallback",
            latency_ms=int((time.monotonic() - start) * 1000),
        )
        cache.put(cache_key, result)
        return result

    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
    for h in hist:
        role = h.get("role", "user")
        msg = h.get("text") or h.get("content") or ""
        if msg:
            messages.append({"role": role, "content": msg[:500]})
    messages.append({"role": "user", "content": raw})

    model_override = fast_model or os.environ.get("FERAL_PROMPT_REFINER_MODEL", "")

    try:
        kwargs: dict[str, Any] = {
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 300,
        }
        if model_override:
            kwargs["model"] = model_override  # llm_provider.chat will honour if supported
        response = await asyncio.wait_for(llm.chat(**kwargs), timeout=2.5)
    except asyncio.TimeoutError:
        logger.warning("PromptRefiner LLM timeout — falling back to identity envelope")
        kw_target = _device_target_keyword(raw) or device_target_hint
        result = RefinedRequest(
            refined_text=raw,
            raw_text=raw,
            intent_class="unknown",
            device_target=kw_target,
            confidence=0.3 if kw_target else 0.0,
            source="fallback",
            latency_ms=int((time.monotonic() - start) * 1000),
        )
        cache.put(cache_key, result)
        return result
    except TypeError:
        # Older llm.chat signatures may not accept `model` kwarg.
        kwargs.pop("model", None)
        try:
            response = await asyncio.wait_for(llm.chat(**kwargs), timeout=2.5)
        except Exception as exc:  # noqa: BLE001
            logger.warning("PromptRefiner LLM call failed: %s — fallback", exc)
            kw_target = _device_target_keyword(raw) or device_target_hint
            return RefinedRequest(
                refined_text=raw,
                raw_text=raw,
                intent_class="unknown",
                device_target=kw_target,
                confidence=0.3 if kw_target else 0.0,
                source="fallback",
                latency_ms=int((time.monotonic() - start) * 1000),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("PromptRefiner LLM call failed: %s — fallback", exc)
        kw_target = _device_target_keyword(raw) or device_target_hint
        return RefinedRequest(
            refined_text=raw,
            raw_text=raw,
            intent_class="unknown",
            device_target=kw_target,
            confidence=0.3 if kw_target else 0.0,
            source="fallback",
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    content = _extract_response_text(response)
    parsed = _parse_json(content)
    if parsed is None:
        logger.warning(
            "PromptRefiner LLM returned non-JSON content (len=%d) — fallback",
            len(content),
        )
        kw_target = _device_target_keyword(raw) or device_target_hint
        return RefinedRequest(
            refined_text=raw,
            raw_text=raw,
            intent_class="unknown",
            device_target=kw_target,
            confidence=0.3 if kw_target else 0.0,
            source="fallback",
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    try:
        result = RefinedRequest(**{
            **parsed,
            "raw_text": raw,
            "source": "llm",
            "latency_ms": int((time.monotonic() - start) * 1000),
        })
    except ValidationError as exc:
        logger.warning("PromptRefiner LLM JSON failed validation: %s — fallback", exc)
        kw_target = _device_target_keyword(raw) or device_target_hint
        result = RefinedRequest(
            refined_text=parsed.get("refined_text", raw) or raw,
            raw_text=raw,
            intent_class="unknown",
            device_target=kw_target,
            confidence=0.2,
            source="fallback",
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    # Caller hint wins when LLM is ambiguous.
    if result.device_target is None and device_target_hint:
        result = result.model_copy(update={"device_target": device_target_hint})

    if not result.refined_text:
        result = result.model_copy(update={"refined_text": raw})

    cache.put(cache_key, result)
    return result


# ───────────────────────── helpers ──────────────────────────


def _extract_response_text(response: Any) -> str:
    """LLMProvider.chat returns OpenAI-shaped dict. Extract the assistant
    string defensively — providers vary in nesting."""
    if not isinstance(response, dict):
        return ""
    choices = response.get("choices") or []
    if choices and isinstance(choices, list):
        msg = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(msg, dict):
            content = msg.get("content") or ""
            if isinstance(content, str):
                return content
    text = response.get("content") or response.get("text") or ""
    return text if isinstance(text, str) else ""


_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def _parse_json(content: str) -> Optional[dict]:
    """Tolerate fenced JSON, leading commentary, trailing whitespace."""
    text = (content or "").strip()
    if not text:
        return None
    m = _FENCE_RE.match(text)
    if m:
        text = m.group(1).strip()
    # First strict attempt.
    try:
        result = json.loads(text)
        return result if isinstance(result, dict) else None
    except (json.JSONDecodeError, ValueError):
        pass
    # Locate the first balanced `{...}` substring.
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for idx in range(start, len(text)):
        ch = text[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start : idx + 1])
                    return obj if isinstance(obj, dict) else None
                except (json.JSONDecodeError, ValueError):
                    return None
    return None


__all__ = ["RefinedRequest", "refine", "infer_device_target"]
