"""Streamed turns must reach the cost budget.

Regression cover for the billing hole that opened when
``config.loader.DEFAULT_STREAMING`` flipped to True: both SSE routes in
``agents/llm_provider.py`` streamed a turn and then recorded nothing, so
``cost_events`` stayed empty, ``CostBudget._spend`` never moved, and a
configured chat cap could not trip (``check_and_reserve`` gates on that
same counter).

What is pinned here:

1. chat-completions asks for ``stream_options.include_usage`` and bills
   the final usage chunk exactly once per turn.
2. Anthropic bills ``message_start`` (input) + ``message_delta``
   (output) exactly once per turn.
3. A stream with no usage at all completes normally and records
   NOTHING (no crash, no fabricated tokens).
4. A provider that 400s on ``stream_options`` still delivers its turn.
5. End to end: a small chat cap actually trips after one streamed turn.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio

from agents.llm_provider import LLMProvider


OPENAI_ENV = {
    "FERAL_LLM_PROVIDER": "openai",
    "OPENAI_API_KEY": "sk-openai-test",
    "FERAL_LLM_MODEL": "gpt-test",
}


# ─────────────────────────────────────────────────────────────────────
# Mock plumbing
# ─────────────────────────────────────────────────────────────────────


def _sse_response(lines: list[str]):
    """An httpx-stream-shaped response that replays ``lines``."""
    async def aiter_lines():
        for line in lines:
            yield line

    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.aiter_lines = aiter_lines
    return resp


def _stream_cm(lines: list[str]):
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=_sse_response(lines))
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _failing_cm(exc: Exception):
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(side_effect=exc)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _usage_aware_stream(content_lines: list[str], usage_line: str):
    """A provider-faithful mock: it emits the usage chunk ONLY when the
    request asked for ``stream_options.include_usage``, which is what
    OpenAI actually does. Without this the mock would hand back usage
    the real API would never have sent, and the cap proof below would
    pass even if the opt-in were dropped."""
    def _stream(_method, _url, **kw):
        body = kw.get("json") or {}
        opts = body.get("stream_options") or {}
        lines = list(content_lines)
        if opts.get("include_usage"):
            lines.append(usage_line)
        lines.append("data: [DONE]")
        return _stream_cm(lines)

    return MagicMock(side_effect=_stream)


def _make_openai_provider() -> LLMProvider:
    with patch.dict(os.environ, OPENAI_ENV, clear=False):
        with patch.object(LLMProvider, "_detect_ollama", return_value=None):
            return LLMProvider()


# The exact shape OpenAI emits when include_usage is on: content chunks,
# then a final chunk with an EMPTY choices array carrying usage, then
# the [DONE] sentinel.
OPENAI_CONTENT_LINES = [
    'data: {"model":"gpt-test","choices":[{"delta":{"content":"Hel"}}]}',
    'data: {"model":"gpt-test","choices":[{"delta":{"content":"lo"}}]}',
]

OPENAI_USAGE_LINE = (
    'data: {"model":"gpt-test","choices":[],'
    '"usage":{"prompt_tokens":800,"completion_tokens":160,"total_tokens":960}}'
)

OPENAI_LINES_NO_USAGE = [
    'data: {"choices":[{"delta":{"content":"Hel"}}]}',
    'data: {"choices":[{"delta":{"content":"lo"}}]}',
    "data: [DONE]",
]

ANTHROPIC_LINES = [
    'data: {"type":"message_start","message":{"model":"claude-test",'
    '"usage":{"input_tokens":800,"output_tokens":1}}}',
    'data: {"type":"content_block_start","content_block":{"type":"text"}}',
    'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hel"}}',
    'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"lo"}}',
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
    '"usage":{"output_tokens":160}}',
    'data: {"type":"message_stop"}',
]

ANTHROPIC_LINES_NO_USAGE = [
    'data: {"type":"content_block_start","content_block":{"type":"text"}}',
    'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hi"}}',
    'data: {"type":"message_stop"}',
]


def _patch_anthropic_httpx(lines: list[str]):
    """Patch ``httpx.AsyncClient`` so the Anthropic branch replays ``lines``."""
    resp = _sse_response(lines)

    class _FakeStream:
        async def __aenter__(self_inner):
            return resp

        async def __aexit__(self_inner, *_a, **_k):
            return False

    class _FakeClient:
        async def __aenter__(self_inner):
            return self_inner

        async def __aexit__(self_inner, *_a, **_k):
            return False

        def stream(self_inner, *_a, **_k):
            return _FakeStream()

    return patch("agents.llm_provider.httpx.AsyncClient", lambda *a, **k: _FakeClient())


def _make_anthropic_provider() -> LLMProvider:
    llm = LLMProvider.__new__(LLMProvider)
    llm.provider = "anthropic"
    llm.model = "claude-test"
    llm.api_key = "sk-ant-test"
    llm.base_url = "https://api.anthropic.test/v1"
    llm._config = {}
    llm._cooldown = MagicMock()
    llm._cooldown._last_probe = {}
    return llm


class _RecordSpy:
    """Stand-in for ``_budget_record`` that captures every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    async def __call__(self, call_site: str, model: str, result: Any) -> None:
        self.calls.append((call_site, model, result))


# ─────────────────────────────────────────────────────────────────────
# 1. chat-completions SSE
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_completions_stream_requests_usage_and_bills_once():
    llm = _make_openai_provider()
    llm.client.stream = _usage_aware_stream(OPENAI_CONTENT_LINES, OPENAI_USAGE_LINE)
    spy = _RecordSpy()
    llm._budget_record = spy  # type: ignore[method-assign]

    events = []
    async for ev in llm.chat_stream([{"role": "user", "content": "x"}], tools=None):
        events.append(ev)

    # The request must opt in, or the provider sends no usage at all.
    body = llm.client.stream.call_args[1]["json"]
    assert body["stream_options"] == {"include_usage": True}

    # Text still streams normally.
    assert "".join(e["content"] for e in events if e.get("type") == "text_delta") == "Hello"

    # Exactly one billing call, with the provider's own numbers.
    assert len(spy.calls) == 1, spy.calls
    call_site, model, result = spy.calls[0]
    assert call_site == "chat"
    assert model == "gpt-test"
    assert LLMProvider._extract_usage(result) == (800, 160, 0)

    # And the terminal event carries usage for the UI.
    assert events[-1]["type"] == "done"
    assert events[-1]["usage"] == {
        "input_tokens": 800,
        "output_tokens": 160,
        "total_tokens": 960,
    }


@pytest.mark.asyncio
async def test_chat_completions_usage_on_final_content_chunk_also_bills():
    """OpenRouter / DeepSeek attach usage to the last content chunk
    rather than sending a separate empty-choices chunk."""
    llm = _make_openai_provider()
    lines = [
        'data: {"choices":[{"delta":{"content":"hi"}}]}',
        'data: {"model":"deepseek-chat","choices":[{"delta":{},"finish_reason":"stop"}],'
        '"usage":{"prompt_tokens":11,"completion_tokens":7}}',
        "data: [DONE]",
    ]
    llm.client.stream = MagicMock(return_value=_stream_cm(lines))
    spy = _RecordSpy()
    llm._budget_record = spy  # type: ignore[method-assign]

    async for _ in llm.chat_stream([{"role": "user", "content": "x"}]):
        pass

    assert len(spy.calls) == 1
    assert spy.calls[0][1] == "deepseek-chat"
    assert LLMProvider._extract_usage(spy.calls[0][2]) == (11, 7, 0)


@pytest.mark.asyncio
async def test_chat_completions_stream_without_usage_records_nothing():
    """No usage chunk → normal completion, and NOTHING billed. We do not
    estimate: a fabricated number is worse than a visible gap."""
    llm = _make_openai_provider()
    llm.client.stream = MagicMock(return_value=_stream_cm(OPENAI_LINES_NO_USAGE))
    spy = _RecordSpy()
    llm._budget_record = spy  # type: ignore[method-assign]

    events = []
    async for ev in llm.chat_stream([{"role": "user", "content": "x"}]):
        events.append(ev)

    assert "".join(e["content"] for e in events if e.get("type") == "text_delta") == "Hello"
    assert events[-1] == {"type": "done"}
    assert spy.calls == []


@pytest.mark.asyncio
async def test_stream_options_rejected_still_completes_the_turn():
    """A strict OpenAI-compatible shim 400s on the unknown body key. The
    turn must survive; only the usage data is lost."""
    import agents.llm_provider as llm_mod

    llm = _make_openai_provider()
    llm.base_url = "http://localhost:8000/v1"
    llm.provider = "lmstudio"
    llm_mod._STREAM_OPTIONS_UNSUPPORTED.discard((llm.provider, llm.base_url))

    request = httpx.Request("POST", "http://localhost:8000/v1/chat/completions")
    response = httpx.Response(
        400,
        json={"error": {"message": "Unrecognized request argument: stream_options"}},
        request=request,
    )
    reject = httpx.HTTPStatusError("400", request=request, response=response)

    bodies: list[dict] = []

    def _stream(_method, _url, **kw):
        bodies.append(dict(kw.get("json") or {}))
        return _failing_cm(reject) if len(bodies) == 1 else _stream_cm(OPENAI_LINES_NO_USAGE)

    llm.client.stream = MagicMock(side_effect=_stream)
    spy = _RecordSpy()
    llm._budget_record = spy  # type: ignore[method-assign]

    try:
        events = []
        async for ev in llm.chat_stream([{"role": "user", "content": "x"}]):
            events.append(ev)

        # The turn completed with its text, not an error event.
        assert [e for e in events if e.get("type") == "error"] == []
        assert "".join(
            e["content"] for e in events if e.get("type") == "text_delta"
        ) == "Hello"
        assert events[-1] == {"type": "done"}

        # First attempt asked for usage, retry dropped the key.
        assert bodies[0]["stream_options"] == {"include_usage": True}
        assert "stream_options" not in bodies[1]

        # Endpoint remembered, so the next turn does not pay the retry.
        assert (llm.provider, llm.base_url) in llm_mod._STREAM_OPTIONS_UNSUPPORTED

        # Nothing billed, nothing invented.
        assert spy.calls == []
    finally:
        llm_mod._STREAM_OPTIONS_UNSUPPORTED.discard((llm.provider, llm.base_url))


@pytest.mark.asyncio
async def test_non_stream_options_400_does_not_disable_usage_capture():
    """A 400 about something else (bad model) must not poison the
    endpoint's usage capture for the rest of the process."""
    import agents.llm_provider as llm_mod

    llm = _make_openai_provider()
    llm.set_config({})
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(
        400,
        json={"error": {"type": "invalid_request_error", "param": "model"}},
        request=request,
    )
    err = httpx.HTTPStatusError("400", request=request, response=response)
    llm.client.stream = MagicMock(return_value=_failing_cm(err))

    events = []
    async for ev in llm.chat_stream([{"role": "user", "content": "x"}]):
        events.append(ev)

    assert events[-1]["type"] == "error"
    assert (llm.provider, llm.base_url) not in llm_mod._STREAM_OPTIONS_UNSUPPORTED


# ─────────────────────────────────────────────────────────────────────
# 2. Anthropic SSE
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_anthropic_stream_bills_message_start_plus_message_delta():
    llm = _make_anthropic_provider()
    spy = _RecordSpy()
    llm._budget_record = spy  # type: ignore[method-assign]

    events = []
    with _patch_anthropic_httpx(ANTHROPIC_LINES):
        async for ev in llm._chat_stream_anthropic(
            [{"role": "user", "content": "x"}], call_site="learner",
        ):
            events.append(ev)

    assert "".join(e["content"] for e in events if e.get("type") == "text_delta") == "Hello"

    assert len(spy.calls) == 1, spy.calls
    call_site, model, result = spy.calls[0]
    # Input comes from message_start, output from message_delta.
    assert LLMProvider._extract_usage(result) == (800, 160, 0)
    # call_site is threaded through so background loops bill their own cap.
    assert call_site == "learner"
    assert model == "claude-test"

    assert events[-1]["type"] == "done"
    assert events[-1]["usage"] == {
        "input_tokens": 800,
        "output_tokens": 160,
        "total_tokens": 960,
    }


@pytest.mark.asyncio
async def test_anthropic_stream_without_usage_records_nothing():
    llm = _make_anthropic_provider()
    spy = _RecordSpy()
    llm._budget_record = spy  # type: ignore[method-assign]

    events = []
    with _patch_anthropic_httpx(ANTHROPIC_LINES_NO_USAGE):
        async for ev in llm._chat_stream_anthropic([{"role": "user", "content": "x"}]):
            events.append(ev)

    assert events[-1] == {"type": "done"}
    assert spy.calls == []


@pytest.mark.asyncio
async def test_anthropic_stream_bills_once_with_multiple_message_deltas():
    """``message_delta.usage.output_tokens`` is cumulative, so the last
    one wins and the turn is billed once, not once per delta."""
    llm = _make_anthropic_provider()
    lines = [
        'data: {"type":"message_start","message":{"usage":{"input_tokens":40}}}',
        'data: {"type":"message_delta","usage":{"output_tokens":10}}',
        'data: {"type":"message_delta","usage":{"output_tokens":25}}',
        'data: {"type":"message_stop"}',
    ]
    spy = _RecordSpy()
    llm._budget_record = spy  # type: ignore[method-assign]

    with _patch_anthropic_httpx(lines):
        async for _ in llm._chat_stream_anthropic([{"role": "user", "content": "x"}]):
            pass

    assert len(spy.calls) == 1
    assert LLMProvider._extract_usage(spy.calls[0][2]) == (40, 25, 0)


# ─────────────────────────────────────────────────────────────────────
# 3. The cap actually trips
# ─────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def chat_capped_budget(tmp_path):
    """Real ``CostBudget`` on an isolated SQLite file with a $0.05/hour
    chat cap. Async fixture so ``close()`` runs on the test's loop."""
    from cost.budget import CostBudget

    budget = CostBudget(
        settings={"cost": {"enabled": True, "chat": {"per_hour_usd": 0.05}}},
        db_path=str(tmp_path / "cost.db"),
    )
    try:
        yield budget
    finally:
        await budget.close()


@pytest.mark.asyncio
async def test_streamed_turn_moves_spend_and_trips_the_chat_cap(chat_capped_budget):
    """The user-visible outcome: one streamed turn now consumes budget,
    and the NEXT call is refused by the same cap."""
    budget = chat_capped_budget
    await budget.ensure_ready()

    llm = _make_openai_provider()
    llm.set_cost_budget(budget)
    llm.client.stream = _usage_aware_stream(OPENAI_CONTENT_LINES, OPENAI_USAGE_LINE)

    # Before: nothing spent, and a 1024-token call is affordable.
    assert budget.current_spend("chat", "hour") == 0.0
    assert budget.check_and_reserve("chat", "gpt-test", 1024) is True

    events = []
    async for ev in llm.chat_stream(
        [{"role": "user", "content": "x"}], max_tokens=64,
    ):
        events.append(ev)

    assert [e for e in events if e.get("type") == "budget_exceeded"] == []
    assert events[-1]["type"] == "done"

    # After: gpt-test bills 800 prompt @ $0.005/1k + 160 completion
    # @ $0.025/1k = $0.008. _spend moved off zero.
    spent = budget.current_spend("chat", "hour")
    assert spent == pytest.approx(0.008, rel=1e-6), spent
    assert budget._spend[("chat", "hour")] == pytest.approx(spent)

    # The event landed in the durable ledger too.
    assert budget._conn is not None
    async with budget._conn.execute(
        "SELECT call_site, model, prompt_tokens, completion_tokens FROM cost_events"
    ) as cur:
        rows = await cur.fetchall()
    assert rows == [("chat", "gpt-test", 800, 160)]

    # Now push spend just under the $0.05 cap and show the gate closes.
    await budget.record_usage(
        call_site="chat", model="gpt-test",
        prompt_tokens=7000, completion_tokens=0,
    )
    assert budget.current_spend("chat", "hour") == pytest.approx(0.043, rel=1e-6)
    assert budget.check_and_reserve("chat", "gpt-test", 1024) is False

    # And chat_stream refuses the next turn instead of silently spending.
    llm.client.stream = _usage_aware_stream(OPENAI_CONTENT_LINES, OPENAI_USAGE_LINE)
    blocked = []
    async for ev in llm.chat_stream(
        [{"role": "user", "content": "again"}], max_tokens=1024,
    ):
        blocked.append(ev)
    assert blocked[0]["type"] == "budget_exceeded"
    assert blocked[0]["payload"]["call_site"] == "chat"


@pytest.mark.asyncio
async def test_anthropic_streamed_turn_moves_spend(chat_capped_budget):
    """Same proof for the Anthropic branch."""
    budget = chat_capped_budget
    await budget.ensure_ready()

    llm = _make_anthropic_provider()
    llm.set_cost_budget(budget)

    assert budget.current_spend("chat", "hour") == 0.0

    with _patch_anthropic_httpx(ANTHROPIC_LINES):
        async for _ in llm._chat_stream_anthropic(
            [{"role": "user", "content": "x"}], call_site="chat",
        ):
            pass

    # claude-test falls back to $0.005/1k in + $0.025/1k out.
    assert budget.current_spend("chat", "hour") == pytest.approx(0.008, rel=1e-6)
