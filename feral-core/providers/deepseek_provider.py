"""DeepSeek provider adapter (OpenAI-compatible /v1/chat/completions).

2026-04-26 surface
------------------
* Live models: ``deepseek-v4-flash``, ``deepseek-v4-pro``. Legacy
  aliases ``deepseek-chat`` (= v4-flash non-thinking) and
  ``deepseek-reasoner`` (= v4-flash thinking) still served but will
  deprecate 2026-07-24 per upstream. See
  https://api-docs.deepseek.com/quick_start/pricing.
* Thinking mode: ``extra_body={"thinking":{"type":"enabled"|"disabled"}}``.
  Default: enabled on ``v4-pro``, disabled on ``v4-flash``. When enabled,
  DeepSeek ignores (and rejects in strict mode) ``temperature``,
  ``top_p``, ``presence_penalty``, ``frequency_penalty``.
* ``reasoning_effort`` accepts ``"high"`` / ``"max"``; default ``"high"``.
* ``reasoning_content`` is returned on the assistant message when
  thinking is on. In multi-turn tool-call cycles it MUST be carried
  forward into the next request; on non-tool turns it SHOULD be stripped
  from the user-facing assistant message. See :func:`carry_reasoning_content`
  and :func:`strip_reasoning_content_for_non_tool_turn` below.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

import httpx

from .base import BaseProvider, ChatMessage, ChatResponse
from .model_classes import classify

logger = logging.getLogger("feral.providers.deepseek")


# Params DeepSeek's thinking mode rejects (400 / 422 depending on mode).
# Matches the list the 2026-04-26 spec enumerates as "ignored in thinking
# mode" — we strip them rather than silently forward, so users who flip
# the switch don't have to wonder why their temperature is being dropped.
_REASONING_STRIP_PARAMS = frozenset(
    {"temperature", "top_p", "presence_penalty", "frequency_penalty"}
)


def _apply_reasoning_fork(
    model: str, payload: dict[str, Any], *, effort: Optional[str] = None
) -> dict[str, Any]:
    """Rewrite *payload* to match the DeepSeek thinking-mode contract.

    Leaves ``v4-flash`` / ``deepseek-chat`` untouched (thinking off).
    For ``v4-pro`` / ``deepseek-reasoner``, adds the ``extra_body``
    thinking block + ``reasoning_effort`` and strips the disallowed
    sampling params. The ``effort`` override is for the orchestrator's
    subagent path which bumps to ``"max"``.
    """
    if classify("deepseek", model) != "reasoning":
        return payload
    extra = payload.setdefault("extra_body", {})
    if not isinstance(extra, dict):
        extra = {}
        payload["extra_body"] = extra
    extra.setdefault("thinking", {"type": "enabled"})
    payload.setdefault("reasoning_effort", effort or "high")
    if effort:
        payload["reasoning_effort"] = effort
    for key in _REASONING_STRIP_PARAMS:
        payload.pop(key, None)
    return payload


def carry_reasoning_content(
    replay_messages: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Preserve ``reasoning_content`` on replayed assistant messages.

    The DeepSeek thinking-mode contract: if the previous turn's
    assistant message had ``reasoning_content`` AND that turn emitted
    a tool call we're now answering, the next request MUST include
    ``reasoning_content`` on the replayed assistant message. Dropping
    it triggers a 400 ``reasoning_content missing`` from the API.

    This helper walks the replay list, finds the last assistant
    message, and leaves its ``reasoning_content`` in place when a
    tool_use / tool message follows it. When no tool message follows,
    the helper strips ``reasoning_content`` per
    :func:`strip_reasoning_content_for_non_tool_turn`.
    """
    msgs = list(replay_messages)
    if not msgs:
        return msgs
    # Find the last assistant message and the messages after it.
    last_asst_idx = -1
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") == "assistant":
            last_asst_idx = i
            break
    if last_asst_idx == -1:
        return msgs
    tail = msgs[last_asst_idx + 1 :]
    tail_has_tool = any(m.get("role") == "tool" for m in tail)
    last_asst = dict(msgs[last_asst_idx])
    if tail_has_tool:
        # Keep reasoning_content intact — the API requires it.
        pass
    else:
        last_asst = strip_reasoning_content_for_non_tool_turn(last_asst)
    msgs[last_asst_idx] = last_asst
    return msgs


def strip_reasoning_content_for_non_tool_turn(
    assistant_message: dict[str, Any],
) -> dict[str, Any]:
    """Remove ``reasoning_content`` from a completed (non-tool) turn.

    On a non-tool-cycle turn, leaving ``reasoning_content`` on the
    replayed assistant message causes the model to re-emit it and
    bloats the context. The upstream contract is: omit on non-tool
    turns. Returns a shallow copy — mutation-free by convention.
    """
    if "reasoning_content" not in assistant_message:
        return assistant_message
    out = dict(assistant_message)
    out.pop("reasoning_content", None)
    return out


class DeepSeekProvider(BaseProvider):
    provider_id = "deepseek"
    display_name = "DeepSeek"

    # Verified 2026-04-26 from the DeepSeek API pricing page. Legacy
    # ``deepseek-chat`` and ``deepseek-reasoner`` remain in the list
    # for compatibility until 2026-07-24.
    _models = [
        "deepseek-v4-pro",
        "deepseek-v4-flash",
        "deepseek-chat",
        "deepseek-reasoner",
    ]
    # Pricing (USD per 1k tokens). The v4-pro limited-time 75%-off
    # discount (valid until 2026-05-05) is reflected here; the
    # post-discount sticker price is in the comment on each row so a
    # reviewer can confirm the refresh script captured the right number.
    # Pricing lives in providers/model_catalog.json — see
    # findings/13-llm-core.md fix #4. Note: DeepSeek's discount window
    # ended 2026-05-05; the catalog entry is updated to sticker rate
    # in this lane (see PR pricing-reconciliation table).
    _pricing: dict[str, dict[str, float]] = {}
    _capabilities = {"tool_calling", "streaming", "thinking", "json_mode"}

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None) -> None:
        self._api_key = api_key
        # DeepSeek's OpenAI-compat surface lives at ``/v1`` under the
        # unversioned ``api.deepseek.com`` root. The ``/beta`` variant
        # unlocks FIM + prefix completion (W25 will wire those).
        self._base_url = (base_url or "https://api.deepseek.com/v1").rstrip("/")

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        tools: Optional[list[dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> ChatResponse:
        if not self._api_key:
            raise RuntimeError("deepseek provider has no api_key configured")
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {
                    k: v
                    for k, v in {
                        "role": m.role,
                        "content": m.content,
                        "name": m.name,
                        "tool_calls": m.tool_calls or None,
                    }.items()
                    if v is not None and v != []
                }
                for m in messages
            ],
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = tools

        # Fork: v4-pro / deepseek-reasoner demand the thinking block
        # + reasoning_effort and reject the sampling params.
        _apply_reasoning_fork(
            model, payload, effort=kwargs.get("reasoning_effort")
        )

        async with httpx.AsyncClient(timeout=90.0) as c:
            r = await c.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
        choice = data["choices"][0]
        msg = choice["message"]
        return ChatResponse(
            text=msg.get("content", ""),
            model=data.get("model", model),
            usage=data.get("usage", {}),
            finish_reason=choice.get("finish_reason", "stop"),
            tool_calls=msg.get("tool_calls") or [],
        )

    async def refresh_models(self) -> list[str]:
        if not self._api_key:
            return list(self._models)
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.get(
                f"{self._base_url}/models",
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            r.raise_for_status()
        ids = [m["id"] for m in r.json().get("data", [])]
        if ids:
            # Same doctrine as OpenAI: store the full raw list, let
            # ``list_models(model_class=...)`` filter via the
            # classifier at call time.
            self._models = sorted(ids)
        return list(self._models)
