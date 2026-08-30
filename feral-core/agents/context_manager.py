"""
Context window management for the FERAL orchestrator.

Handles conversation history compaction and message truncation
to keep the LLM context within token budgets.
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger("feral.orchestrator.context")

# Rough chars→tokens ratio for English prose plus JSON tool payloads.
# Only used to *size* the window; the provider does the real counting.
_CHARS_PER_TOKEN = 4
# Assumed usable context window of the serving model. Overridable for
# small local models via ``FERAL_CONTEXT_WINDOW_TOKENS``.
_DEFAULT_CONTEXT_WINDOW_TOKENS = 128_000
# Share of that window the conversation may occupy. The remainder is
# the system prompt, the tool schemas and the model's own reply.
_HISTORY_SHARE = 0.5


def configured_context_window_tokens() -> int:
    """Usable context window the operator has declared, in tokens.

    Public because this is the only context size the process can
    actually discover: it is read fresh from the environment on every
    call, so a test or a relaunch that changes
    ``FERAL_CONTEXT_WINDOW_TOKENS`` is honoured without a restart.
    ``memory/knowledge_graph.py`` sizes its extraction prompt against it
    rather than hard-coding a character cap.
    """
    raw = os.environ.get("FERAL_CONTEXT_WINDOW_TOKENS", "")
    if not raw.strip():
        return _DEFAULT_CONTEXT_WINDOW_TOKENS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "FERAL_CONTEXT_WINDOW_TOKENS=%r is not an integer; using %d",
            raw, _DEFAULT_CONTEXT_WINDOW_TOKENS,
        )
        return _DEFAULT_CONTEXT_WINDOW_TOKENS
    if value <= 0:
        logger.warning(
            "FERAL_CONTEXT_WINDOW_TOKENS=%d must be positive; using %d",
            value, _DEFAULT_CONTEXT_WINDOW_TOKENS,
        )
        return _DEFAULT_CONTEXT_WINDOW_TOKENS
    return value


class ContextManager:
    """Manages conversation history size and compaction."""

    def __init__(
        self,
        max_messages: int = 15,
        *,
        max_turns: int = 12,
        context_window_tokens: int = 0,
    ):
        # Fast path only: a history at or below this many raw rows is
        # returned untouched. This is NOT the window size — the window
        # is measured in turns and tokens (see below). It used to be
        # both, which is why one assistant turn carrying six parallel
        # tool calls (7 rows) evicted the rest of the session.
        self.max_messages = max_messages
        # Window size in USER TURNS. A turn is a user row plus every
        # assistant / tool / system row that follows it, so a tool-heavy
        # turn consumes one slot rather than seven.
        self.max_turns = max_turns
        # Hard ceiling so a few enormous tool payloads can't overflow
        # the model's real context window.
        self.context_window_tokens = (
            context_window_tokens or configured_context_window_tokens()
        )

    @property
    def history_budget_chars(self) -> int:
        """Character budget the conversation window may occupy."""
        return int(self.context_window_tokens * _HISTORY_SHARE) * _CHARS_PER_TOKEN

    def compact(self, history: list[dict]) -> list[dict]:
        """Return the per-request *view* of ``history`` for the LLM.

        This is a view, not a mutation: callers must keep the full
        transcript and re-derive this window every request. Storing the
        result back over the transcript made truncation permanent and
        cumulative, which is how a session lost every turn but the last
        one or two.

        Selection rules, in order:

        1. Keep the newest ``max_turns`` user turns, whole. The cut
           point is always a user row, so a tool round-trip is never
           split.
        2. Shrink that window turn-by-turn until it fits
           ``history_budget_chars`` — oldest first, mirroring
           Anthropic's context-editing contract where recent turns are
           never the ones removed.
        3. Never start after the newest assistant message. A window
           that drops the last assistant turn hands the model two
           consecutive user messages, and the model then correctly
           reports it never spoke — the amnesia bug this guards.

        v2026.5.29 — tool-aware compaction. A naive ``history[-N:]``
        slice can land inside an assistant ``tool_calls`` round-trip
        and drop the announcing assistant turn while keeping the
        ``role:"tool"`` rows that follow it. The Responses API then
        rejects the next request with
        ``400 No tool call found for function call output``.

        We expand the window backwards whenever the cut point would
        produce an orphan tool row, until either the announcing
        assistant turn is included or we hit a safe (user / plain
        assistant / system) boundary. If no safe boundary is found we
        drop the leading orphan tool rows instead — the translator
        guard then makes the request well-formed.
        """
        if len(history) <= self.max_messages:
            return coalesce_consecutive_users(history)
        start = self._window_start(history)
        # Expand the window backwards until the row at ``start`` is no
        # longer an orphan ``tool``. We pull in the preceding assistant
        # turn(s) so any matching ``function_call`` is in the same
        # window as its output.
        while start > 0 and _is_orphan_tool_boundary(history, start):
            start -= 1
        compacted = history[start:]
        # If the tail still begins with one or more orphan tool rows
        # (no assistant within reach), strip them: there is no
        # ``function_call`` to pair with and OpenAI will 400 if we
        # send them.
        compacted = _drop_leading_orphan_tools(compacted)
        compacted = coalesce_consecutive_users(compacted)
        logger.info(
            "Compacting context window from %d to %d rows (turn-aware)",
            len(history),
            len(compacted),
        )
        return compacted

    def _window_start(self, history: list[dict]) -> int:
        """Index of the first row the LLM should see."""
        user_idxs = [
            i for i, row in enumerate(history)
            if isinstance(row, dict) and row.get("role") == "user"
        ]
        if not user_idxs:
            # No user rows at all (synthetic / snapshot history). Fall
            # back to the raw row count.
            start = max(0, len(history) - self.max_messages)
        else:
            candidates = user_idxs[-self.max_turns:]
            budget = self.history_budget_chars
            # Oldest candidate first: the first one whose tail fits the
            # budget gives the largest well-formed window.
            start = candidates[-1]
            for idx in candidates:
                if _chars_from(history, idx) <= budget:
                    start = idx
                    break
        last_assistant = _last_role_index(history, "assistant")
        if 0 <= last_assistant < start:
            start = last_assistant
        return start


def coalesce_consecutive_users(history: list[dict]) -> list[dict]:
    """Merge back-to-back ``user`` rows into a single message.

    Anthropic's Messages API is stateless and expects alternating
    user/assistant turns; OpenAI's guidance matches. A turn that died
    before emitting an assistant row (or a live-voice reply that was
    never written back) leaves a dangling user message, and the next
    turn then hands the model two user rows in a row — the shape that
    produces "looks like we got cut off". Merging is lossless: both
    utterances still reach the model, in order.

    Returns the input object unchanged when nothing needs merging so
    the common case stays allocation-free.
    """
    if len(history) < 2:
        return history
    if not any(
        isinstance(row, dict)
        and row.get("role") == "user"
        and isinstance(history[i - 1], dict)
        and history[i - 1].get("role") == "user"
        for i, row in enumerate(history)
        if i > 0
    ):
        return history
    merged: list[dict] = []
    for row in history:
        if (
            merged
            and isinstance(row, dict)
            and row.get("role") == "user"
            and merged[-1].get("role") == "user"
        ):
            merged[-1] = _merge_user_rows(merged[-1], row)
            continue
        merged.append(dict(row) if isinstance(row, dict) else row)
    logger.info(
        "Coalesced %d consecutive user rows into %d messages",
        len(history), len(merged),
    )
    return merged


def _merge_user_rows(first: dict, second: dict) -> dict:
    """Concatenate two ``user`` rows' content, preserving block shape."""
    a = first.get("content")
    b = second.get("content")
    if isinstance(a, str) and isinstance(b, str):
        content: object = f"{a}\n\n{b}" if a and b else (a or b)
    else:
        content = [*_as_blocks(a), *_as_blocks(b)]
    out = dict(first)
    out["content"] = content
    return out


def _as_blocks(content) -> list:
    """Normalise a message content field to a list of content blocks."""
    if isinstance(content, list):
        return content
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if content is None:
        return []
    return [{"type": "text", "text": str(content)}]


def _row_chars(row) -> int:
    """Approximate serialized size of one history row."""
    if not isinstance(row, dict):
        return 0
    total = 8  # per-row envelope (role key, braces, separators)
    content = row.get("content")
    if isinstance(content, str):
        total += len(content)
    elif content is not None:
        total += len(json.dumps(content, default=str))
    tool_calls = row.get("tool_calls")
    if tool_calls:
        total += len(json.dumps(tool_calls, default=str))
    return total


def _chars_from(history: list[dict], start: int) -> int:
    return sum(_row_chars(row) for row in history[start:])


def _last_role_index(history: list[dict], role: str) -> int:
    for i in range(len(history) - 1, -1, -1):
        row = history[i]
        if isinstance(row, dict) and row.get("role") == role:
            return i
    return -1


def _is_orphan_tool_boundary(history: list[dict], idx: int) -> bool:
    """Return True when the row at ``idx`` is a ``tool`` result and
    the preceding row in the full history is *also* part of the same
    tool round-trip (so the window starts mid-call)."""
    if idx <= 0 or idx >= len(history):
        return False
    row = history[idx]
    if not isinstance(row, dict) or row.get("role") != "tool":
        return False
    prev = history[idx - 1]
    if not isinstance(prev, dict):
        return False
    prev_role = prev.get("role")
    if prev_role == "tool":
        return True
    if prev_role == "assistant" and prev.get("tool_calls"):
        # The announcing assistant turn is immediately before; pull it
        # into the window.
        return True
    return False


def _drop_leading_orphan_tools(history: list[dict]) -> list[dict]:
    """Strip any leading ``role:"tool"`` rows whose announcing
    assistant turn is missing from the window."""
    if not history:
        return history
    announced: set[str] = set()
    for row in history:
        if not isinstance(row, dict):
            continue
        if row.get("role") == "assistant" and row.get("tool_calls"):
            for tc in row["tool_calls"]:
                if isinstance(tc, dict):
                    cid = tc.get("id")
                    if isinstance(cid, str) and cid:
                        announced.add(cid)
    # Only drop *leading* orphans to preserve the chronological
    # interior; the translator drops mid-list orphans defensively.
    drop_until = 0
    for i, row in enumerate(history):
        if not isinstance(row, dict) or row.get("role") != "tool":
            break
        cid = row.get("tool_call_id") or row.get("call_id") or ""
        if cid and cid in announced:
            break
        drop_until = i + 1
    if drop_until == 0:
        return history
    return history[drop_until:]
