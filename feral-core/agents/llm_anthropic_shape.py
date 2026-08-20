"""Anthropic Messages API request-shape helpers.

The dispatcher in ``agents.llm_provider`` accepts OpenAI-flavoured
transcripts from upstream callers. Anthropic's Messages API uses a
different shape: tool results live in a ``user`` message with a
``tool_result`` content block and assistant tool invocations live in
an ``assistant`` message with ``tool_use`` blocks. These helpers
convert the OpenAI shape to the Anthropic shape at the build site so
the rest of the dispatcher stays provider-agnostic. Extracted from
``agents.llm_provider`` (-A15); re-exported from there for import
compatibility.
"""

from __future__ import annotations

import json

from agents.multimodal_blocks import (
    to_anthropic_blocks,
    translate_content_for_anthropic,
)


_ANTHROPIC_THINKING_RESPONSE_ROOM = 1024  # mirrors AnthropicProvider.chat


def _convert_messages_for_anthropic(messages: list[dict]) -> tuple[str, list[dict]]:
    """Return ``(system_text, anthropic_messages)`` for the Messages API.

    * ``role: "system"`` → concatenated into the top-level ``system``
      field.
    * ``role: "tool"`` → emitted as a ``user`` message with a single
      ``tool_result`` content block. The upstream ``tool_call_id`` maps
      to Anthropic's ``tool_use_id``.
    * ``role: "assistant"`` with ``tool_calls`` → emitted with a list
      of content blocks: optional leading ``text`` block, then one
      ``tool_use`` block per call (OpenAI ``function.arguments`` JSON
      string is parsed to dict for Anthropic's ``input``).
    * Everything else passes through with its content unchanged.
    """
    system_text = ""
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content", "")
        if role == "system":
            if isinstance(content, str):
                system_text += content + "\n"
            else:
                system_text += str(content) + "\n"
            continue
        if role == "tool":
            tool_call_id = m.get("tool_call_id") or m.get("id") or ""
            if not tool_call_id:
                # Fail fast with a clear error instead of silently
                # letting Anthropic reject the message — we can't
                # reconstruct a ``tool_use_id`` from thin air.
                raise ValueError(
                    "Anthropic request build: tool-result message missing "
                    "'tool_call_id'; cannot convert to tool_result block"
                )
            # A tool result may now be a LIST of OpenAI-shape content
            # blocks rather than a plain string: that is how an
            # image-bearing tool result (a screenshot) reaches this
            # translator. Anthropic accepts an ``image`` block inside the
            # ``tool_result`` content array -- see the computer-use tool
            # docs, "Implement proper screenshot handling":
            #   {"type": "tool_result", "tool_use_id": "...",
            #    "content": [{"type": "image", "source": {...}}]}
            # so translate the blocks rather than ``json.dumps``-ing them
            # (which is what used to happen, and which would have shipped
            # the whole base64 blob back into the text as an escaped JSON
            # string).
            if isinstance(content, list):
                result_content: object = to_anthropic_blocks(content)
            elif isinstance(content, str):
                result_content = content
            else:
                result_content = json.dumps(content)
            out.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": str(tool_call_id),
                        "content": result_content,
                    }
                ],
            })
            continue
        if role == "assistant" and m.get("tool_calls"):
            blocks: list[dict] = []
            if isinstance(content, str) and content:
                blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                # Pre-translate any OpenAI-shape multimodal parts so a
                # vision-bearing assistant replay (rare but legal) lands
                # in Anthropic's content-block schema before we append
                # the tool_use blocks. ``translate_content_for_anthropic``
                # is a no-op on already-native blocks.
                for translated_part in translate_content_for_anthropic(content):
                    blocks.append(translated_part)
            for call in m["tool_calls"]:
                fn = call.get("function", {}) if isinstance(call, dict) else {}
                raw_args = fn.get("arguments", "{}")
                if isinstance(raw_args, str):
                    try:
                        parsed_args = json.loads(raw_args) if raw_args else {}
                    except json.JSONDecodeError:
                        parsed_args = {"_raw": raw_args}
                else:
                    parsed_args = raw_args
                blocks.append({
                    "type": "tool_use",
                    "id": str(call.get("id") or ""),
                    "name": str(fn.get("name") or ""),
                    "input": parsed_args,
                })
            out.append({"role": "assistant", "content": blocks or content})
            continue
        # v2026.5.44 multimodal fix: ``content`` may be a list of
        # OpenAI-shape content blocks assembled upstream
        # (``perception.fusion.to_llm_user_content`` and the
        # ScreenLoop attach path emit ``image_url`` blocks). Anthropic
        # rejects those with a 400 ("Input tag 'image_url' ... does not
        # match any of the expected tags"). Translate to Anthropic's
        # ``image`` / ``source`` schema at the provider boundary so
        # call-sites can keep building the canonical OpenAI shape.
        out.append(
            {"role": role, "content": translate_content_for_anthropic(content)}
        )
    return system_text, out


def _enforce_anthropic_thinking_max_tokens(body: dict) -> dict:
    """Enforce Anthropic's ``max_tokens > thinking.budget_tokens`` invariant.

    Matches ``AnthropicProvider.chat``: when the reasoning fork set a
    ``thinking.budget_tokens`` block, the dispatcher's default
    ``max_tokens=1024`` is smaller than every non-trivial budget
    (16k / 32k) and Anthropic 400s. Bump to
    ``budget + _ANTHROPIC_THINKING_RESPONSE_ROOM`` so there's room for
    the post-thinking answer. Mutates and returns ``body``.
    """
    thinking = body.get("thinking")
    if not isinstance(thinking, dict):
        return body
    budget = thinking.get("budget_tokens")
    if not isinstance(budget, int) or budget <= 0:
        return body
    required = budget + _ANTHROPIC_THINKING_RESPONSE_ROOM
    existing = body.get("max_tokens") or 0
    if existing < required:
        body["max_tokens"] = required
    return body


__all__ = [
    "_ANTHROPIC_THINKING_RESPONSE_ROOM",
    "_convert_messages_for_anthropic",
    "_enforce_anthropic_thinking_max_tokens",
]
