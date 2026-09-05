"""Responses-only models on the failover chain, and provider errors as errors.

Two defects from ~/.feral/logs/brain.err, hit on 2026-08-01, 08-03, 08-05,
08-06, 08-07, 08-16 through 08-25 and four times on 2026-09-02:

    [feral.llm] Provider openai failed (unknown): HTTP 400 - invalid_request_error,
    param=reasoning_effort: Function tools with reasoning_effort are not supported
    for gpt-5.6-sol in /v1/chat/completions. To use function tools, use
    /v1/responses or set reasoning_effort to 'none'

A. ``classify_endpoint("openai", "gpt-5.6-sol")`` is ``"responses"``, but only
   ``LLMProvider.chat`` consulted it. ``_call_provider`` (every failover hop,
   primary and fallback) built a chat-completions body, ``apply_reasoning_fork``
   added ``reasoning_effort``, and OpenAI refused it. On 2026-09-02 the primary
   was misconfigured (``deepseek`` with model ``anthropic/claude-sonnet-5``), so
   ``openai/gpt-5.6-sol`` was a FALLBACK candidate and went through the
   fallback branch four times in a row.

B. ``LLMProvider.extract_response`` returned the provider's error string in the
   TEXT slot, so the orchestrator delivered "HTTP 400 ..." as an assistant
   bubble, stored it in ``conversation_history`` and replayed it to the model
   as its own prior turn on the next request.

The fake client below behaves like OpenAI on the one point that matters: a
``/chat/completions`` POST carrying BOTH ``tools`` and ``reasoning_effort``
answers the real 400. Every routing test therefore asserts two things at once:
the request never took that shape, and the turn succeeded.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from agents.llm_provider import (
    LLMProvider,
    ProviderCooldownTracker,
    llm_response_error,
)
from agents.multi_agent import (
    AgentWorker,
    MultiAgentOrchestrator,
    MultiAgentProviderError,
)
from agents.orchestrator import Orchestrator


OPENAI_400_TEXT = (
    "Function tools with reasoning_effort are not supported for gpt-5.6-sol in "
    "/v1/chat/completions. To use function tools, use /v1/responses or set "
    "reasoning_effort to 'none'"
)
PROVIDER_ERROR_TEXT = (
    "HTTP 400 - invalid_request_error, param=reasoning_effort: " + OPENAI_400_TEXT
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search__web_search",
            "description": "Search the web.",
            "parameters": {"type": "object", "properties": {}},
        },
        "_feral_meta": {"skill_id": "web_search"},
    }
]
MESSAGES = [
    {"role": "system", "content": "You are FERAL."},
    {"role": "user", "content": "what is new today?"},
]

RESPONSES_OK = {
    "id": "resp_1",
    "model": "gpt-5.6-sol",
    "output": [
        {
            "type": "message",
            "content": [{"type": "output_text", "text": "hi from sol"}],
        }
    ],
    "usage": {"input_tokens": 12, "output_tokens": 3},
}
CHAT_OK = {
    "choices": [{"message": {"role": "assistant", "content": "hi from chat"}}],
    "usage": {"prompt_tokens": 12, "completion_tokens": 3},
}


class _FakeResponse:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)
        self.headers: dict = {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://api.openai.com/v1/fake")
            raise httpx.HTTPStatusError(
                f"Client error '{self.status_code}'",
                request=request,
                response=httpx.Response(
                    self.status_code, json=self._payload, request=request,
                ),
            )


class _RecordingClient:
    """Stand-in for ``httpx.AsyncClient`` that records every POST.

    Answers like OpenAI on the defect under test: ``/chat/completions``
    with ``tools`` AND ``reasoning_effort`` is the real 400. A model
    under a foreign vendor prefix (the operator's misconfigured
    ``anthropic/claude-sonnet-5`` on DeepSeek) is a 400 too, so the
    failover loop has a reason to hop.
    """

    instances: list["_RecordingClient"] = []

    def __init__(self, *args, **kwargs):
        self.calls: list[tuple[str, dict]] = []
        self.base_url = str(kwargs.get("base_url", ""))
        self.headers = dict(kwargs.get("headers", {}) or {})
        self.responses_status = 200
        _RecordingClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, path: str, json: dict | None = None, **kwargs):
        body = json or {}
        self.calls.append((path, body))
        if path == "/responses":
            if self.responses_status >= 400:
                return _FakeResponse(
                    self.responses_status,
                    {"error": {"message": "responses rejected", "type": "invalid_request_error"}},
                )
            return _FakeResponse(200, RESPONSES_OK)
        if path == "/chat/completions":
            if body.get("tools") and body.get("reasoning_effort"):
                return _FakeResponse(400, {
                    "error": {
                        "message": OPENAI_400_TEXT,
                        "type": "invalid_request_error",
                        "param": "reasoning_effort",
                        "code": None,
                    },
                })
            if str(body.get("model", "")).startswith("anthropic/"):
                return _FakeResponse(400, {
                    "error": {"message": "Model Not Exist", "type": "invalid_request_error"},
                })
            return _FakeResponse(200, CHAT_OK)
        return _FakeResponse(404, {"error": {"message": f"no route {path}"}})

    def paths(self) -> list[str]:
        return [path for path, _ in self.calls]


def _provider(provider: str, model: str, *, fallbacks: list[str] | None = None) -> LLMProvider:
    llm = LLMProvider.__new__(LLMProvider)
    llm.provider = provider
    llm.model = model
    llm.api_key = "sk-primary"
    llm.base_url = "https://api.example.test/v1"
    llm.available = True
    llm._config = {"fallback_providers": list(fallbacks or [])}
    llm._local_engine = None
    llm._hybrid_cloud_provider = None
    llm._cooldown = ProviderCooldownTracker()
    llm._last_failover = None
    llm._auth_permanent_until = {}
    llm._auth_permanent_logged = set()
    llm.client = _RecordingClient()
    return llm


def _no_chat_completions_with_tools_and_reasoning(client: _RecordingClient) -> None:
    for path, body in client.calls:
        assert not (
            path == "/chat/completions" and body.get("tools") and body.get("reasoning_effort")
        ), f"posted the rejected shape: {path} {sorted(body)}"


class _ClientRecorder:
    """Handle onto every ``_RecordingClient`` built during a test.

    ``_provider`` builds one for ``llm.client`` (the shared primary
    client) before the test body runs, so ``hops`` filters it out: what
    these tests care about is the temporary per-candidate client
    ``_call_provider``'s fallback branch constructs.
    """

    def __init__(self):
        self.primary: _RecordingClient | None = None

    def hops(self) -> list["_RecordingClient"]:
        return [c for c in _RecordingClient.instances if c is not self.primary]

    @property
    def instances(self) -> list["_RecordingClient"]:
        return list(_RecordingClient.instances)


@pytest.fixture
def patched_async_client(monkeypatch):
    """Route every temporary fallback client ``_call_provider`` builds
    through ``_RecordingClient`` so the request can be inspected."""
    _RecordingClient.instances.clear()
    monkeypatch.setattr("agents.llm_provider.httpx.AsyncClient", _RecordingClient)
    yield _ClientRecorder()
    _RecordingClient.instances.clear()


# ---------------------------------------------------------------------------
# Bug A: responses-only model as a FAILOVER candidate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_candidate_gpt56_sol_goes_to_responses_not_chat_completions(
    patched_async_client,
):
    """The 2026-09-02 shape: primary is deepseek, openai/gpt-5.6-sol is a
    fallback candidate. ``_call_provider``'s fallback branch must build a
    Responses body on the candidate's own client, never a chat-completions
    body with tools + reasoning_effort."""
    llm = _provider("deepseek", "anthropic/claude-sonnet-5", fallbacks=["openai"])
    patched_async_client.primary = llm.client

    result = await llm._call_provider(
        "openai",
        {
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-openai",
            "model": "gpt-5.6-sol",
            "supported": True,
        },
        MESSAGES,
        TOOLS,
        temperature=0.7,
        max_tokens=1024,
    )

    hops = patched_async_client.hops()
    assert len(hops) == 1
    tmp = hops[0]
    assert tmp.base_url == "https://api.openai.com/v1"
    assert tmp.headers["Authorization"] == "Bearer sk-openai"
    assert tmp.paths() == ["/responses"]
    body = tmp.calls[0][1]
    assert body["model"] == "gpt-5.6-sol"
    assert "input" in body and "messages" not in body
    assert "reasoning_effort" not in body
    assert body["tools"][0]["name"] == "web_search__web_search"
    assert "_feral_meta" not in body["tools"][0]
    _no_chat_completions_with_tools_and_reasoning(tmp)
    assert result["choices"][0]["message"]["content"] == "hi from sol"


@pytest.mark.asyncio
async def test_fallback_candidate_chat_model_still_uses_chat_completions(
    patched_async_client,
):
    """Negative control: the routing rule is per model. A chat-completions
    model on the same fallback provider keeps the old path."""
    llm = _provider("deepseek", "deepseek-chat", fallbacks=["openai"])
    patched_async_client.primary = llm.client

    result = await llm._call_provider(
        "openai",
        {
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-openai",
            "model": "gpt-4o",
            "supported": True,
        },
        MESSAGES,
        TOOLS,
    )

    tmp = patched_async_client.hops()[0]
    assert tmp.paths() == ["/chat/completions"]
    assert tmp.calls[0][1]["messages"] == MESSAGES
    assert result["choices"][0]["message"]["content"] == "hi from chat"


@pytest.mark.asyncio
async def test_primary_candidate_gpt56_sol_goes_to_responses_on_shared_client():
    """Same rule in the primary-provider branch, which reuses ``self.client``
    and honours the adaptive router's per-call model override."""
    llm = _provider("openai", "gpt-5.6-sol")

    result = await llm._call_provider(
        "openai",
        {
            "base_url": llm.base_url,
            "api_key": llm.api_key,
            "model": "gpt-5.6-sol",
            "supported": True,
        },
        MESSAGES,
        TOOLS,
    )

    assert llm.client.paths() == ["/responses"]
    assert "reasoning_effort" not in llm.client.calls[0][1]
    assert result["choices"][0]["message"]["content"] == "hi from sol"


@pytest.mark.asyncio
async def test_chat_with_failover_hops_from_misconfigured_primary_to_responses(
    patched_async_client,
):
    """End to end through the failover loop with the operator's 2026-09-02
    configuration. The primary fails on the wire, the loop hops to openai,
    and the hop lands on /v1/responses with a usable answer."""
    llm = _provider("deepseek", "anthropic/claude-sonnet-5", fallbacks=["openai"])
    patched_async_client.primary = llm.client
    llm._get_provider_config = lambda name: {  # type: ignore[method-assign]
        "base_url": "https://api.openai.com/v1",
        "api_key": "sk-openai",
        "model": "gpt-5.6-sol",
        "supported": True,
    }

    result = await llm.chat_with_failover(MESSAGES, TOOLS, max_tokens=512)

    # Primary went out on the shared client and was refused.
    assert llm.client.paths() == ["/chat/completions"]
    # The hop built its own client for the candidate and used Responses.
    hop_clients = patched_async_client.hops()
    assert len(hop_clients) == 1
    assert hop_clients[0].paths() == ["/responses"]
    for client in patched_async_client.instances:
        _no_chat_completions_with_tools_and_reasoning(client)
    assert result["choices"][0]["message"]["content"] == "hi from sol"
    assert result["last_failover"]["from"] == "deepseek"
    assert result["last_failover"]["to"] == "openai"


# ---------------------------------------------------------------------------
# Bug A: primary fall-through in chat() and chat_stream()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_primary_responses_failure_returns_error_without_chat_completions():
    """No fallback chain and the Responses call fails: ``chat`` must return
    the error, not fall through to /chat/completions where the same model
    can only 400 again with tools + reasoning_effort."""
    llm = _provider("openai", "gpt-5.6-sol")
    llm.client.responses_status = 500

    result = await llm.chat(MESSAGES, TOOLS)

    assert result["choices"] == []
    assert result["error"]
    assert set(llm.client.paths()) == {"/responses"}
    _no_chat_completions_with_tools_and_reasoning(llm.client)


@pytest.mark.asyncio
async def test_chat_primary_responses_success_no_fallbacks_uses_responses_only():
    llm = _provider("openai", "gpt-5.6-sol")

    result = await llm.chat(MESSAGES, TOOLS)

    assert llm.client.paths() == ["/responses"]
    assert result["choices"][0]["message"]["content"] == "hi from sol"


@pytest.mark.asyncio
async def test_chat_with_fallbacks_routes_primary_through_failover_responses(
    patched_async_client,
):
    """With a chain configured, ``chat`` defers to ``chat_with_failover``
    and the primary candidate is served through Responses exactly once
    (no direct attempt first, so a failure is not tried twice)."""
    llm = _provider("openai", "gpt-5.6-sol", fallbacks=["openrouter"])

    result = await llm.chat(MESSAGES, TOOLS)

    assert llm.client.paths() == ["/responses"]
    assert result["choices"][0]["message"]["content"] == "hi from sol"
    assert "last_failover" not in result


@pytest.mark.asyncio
async def test_chat_stream_responses_ended_empty_does_not_fall_through_to_sse():
    """The Responses stream finishing with no content used to fall through
    to the chat-completions SSE body. For a responses-only model that
    second request is the 400 in brain.err."""
    llm = _provider("openai", "gpt-5.6-sol")

    async def fake_responses_stream(*args, **kwargs):
        yield {"type": "done"}

    llm._responses_stream = fake_responses_stream  # type: ignore[method-assign]
    llm.client.stream = MagicMock(side_effect=AssertionError("SSE must not open"))

    events = [event async for event in llm.chat_stream(MESSAGES, TOOLS)]

    assert events == [{"type": "done"}]
    llm.client.stream.assert_not_called()
    assert llm.client.calls == []


@pytest.mark.asyncio
async def test_chat_stream_responses_error_without_fallbacks_surfaces_error_only():
    llm = _provider("openai", "gpt-5.6-sol")

    async def fake_responses_stream(*args, **kwargs):
        yield {"type": "error", "content": PROVIDER_ERROR_TEXT}

    llm._responses_stream = fake_responses_stream  # type: ignore[method-assign]
    llm.client.stream = MagicMock(side_effect=AssertionError("SSE must not open"))

    events = [event async for event in llm.chat_stream(MESSAGES, TOOLS)]

    assert events == [{"type": "error", "content": PROVIDER_ERROR_TEXT}]
    llm.client.stream.assert_not_called()
    assert llm.client.calls == []


@pytest.mark.asyncio
async def test_chat_stream_responses_error_with_fallbacks_hops_via_nonstream_failover(
    patched_async_client,
):
    """A pre-token Responses failure with a chain configured behaves like
    the SSE and Anthropic branches: one non-stream failover attempt,
    converted to stream events, instead of an error bubble."""
    llm = _provider("openai", "gpt-5.6-sol", fallbacks=["openrouter"])
    patched_async_client.primary = llm.client
    llm._get_provider_config = lambda name: {  # type: ignore[method-assign]
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "sk-or",
        "model": "openai/gpt-4o",
        "supported": True,
    }

    async def fake_responses_stream(*args, **kwargs):
        yield {"type": "error", "content": PROVIDER_ERROR_TEXT}

    llm._responses_stream = fake_responses_stream  # type: ignore[method-assign]
    llm.client.stream = MagicMock(side_effect=AssertionError("SSE must not open"))

    events = [event async for event in llm.chat_stream(MESSAGES, TOOLS)]

    assert [e["type"] for e in events] == ["text_delta", "done"]
    assert events[0]["content"] == "hi from chat"
    llm.client.stream.assert_not_called()


# ---------------------------------------------------------------------------
# Bug B: extract_response / llm_response_error contract
# ---------------------------------------------------------------------------


def test_extract_response_never_returns_provider_error_as_text():
    llm = LLMProvider.__new__(LLMProvider)
    text, tools = llm.extract_response({"error": PROVIDER_ERROR_TEXT, "choices": []})
    assert text is None
    assert tools == []
    assert llm_response_error({"error": PROVIDER_ERROR_TEXT, "choices": []}) == PROVIDER_ERROR_TEXT


def test_empty_payload_is_not_a_provider_error():
    llm = LLMProvider.__new__(LLMProvider)
    assert llm.extract_response({}) == (None, [])
    assert llm.extract_response({"choices": []}) == (None, [])
    assert llm_response_error({}) is None
    assert llm_response_error({"choices": []}) is None
    assert llm_response_error(None) is None


def test_extract_response_success_path_unchanged():
    llm = LLMProvider.__new__(LLMProvider)
    text, tools = llm.extract_response({
        "choices": [{
            "message": {
                "content": "done",
                "tool_calls": [{
                    "id": "call_1",
                    "function": {"name": "web_search__web_search", "arguments": '{"q": "x"}'},
                }],
            },
        }],
    })
    assert text == "done"
    assert tools == [{
        "id": "call_1",
        "name": "web_search__web_search",
        "args": {"q": "x"},
        "args_error": "",
    }]


# ---------------------------------------------------------------------------
# Bug B: orchestrator delivers an error frame and records no assistant row
# ---------------------------------------------------------------------------


def _orchestrator() -> Orchestrator:
    reg = MagicMock()
    reg.skills = {}
    reg.find_skills_for_query = MagicMock(return_value=[])
    reg.get_tools_for_skills = MagicMock(return_value=[])
    orch = Orchestrator(
        skill_registry=reg,
        send_to_client=AsyncMock(),
        daemons={},
        memory=None,
        vision_buffer=None,
        perception=None,
        learner=None,
    )
    orch.llm = MagicMock()
    orch.llm.available = True
    orch.llm.model_name = "gpt-5.6-sol"
    orch._streaming_enabled = False
    orch._multi_agent_enabled = False
    orch._route_prompt = AsyncMock(return_value=[])
    orch._maybe_emit_temporal_timeline = AsyncMock(return_value=False)
    orch._build_system_prompt = AsyncMock(return_value="SYSTEM")
    return orch


def _sent_frames(orch: Orchestrator):
    return [call.args[1] for call in orch.send.await_args_list]


@pytest.mark.asyncio
async def test_provider_error_dict_becomes_error_frame_not_assistant_text():
    orch = _orchestrator()
    orch.llm.chat_with_failover = AsyncMock(
        return_value={"error": PROVIDER_ERROR_TEXT, "choices": []},
    )
    orch.llm.extract_response = MagicMock(return_value=(None, []))
    sid = "sess-provider-error"

    out = await orch.handle_command(sid, "Can you flash the light screen?")

    assert out is None
    frames = _sent_frames(orch)
    types = [frame.type for frame in frames]
    assert "error" in types
    assert "text_response" not in types
    error_frame = next(frame for frame in frames if frame.type == "error")
    assert error_frame.payload["message"] == PROVIDER_ERROR_TEXT
    assert error_frame.payload["code"] == "llm_provider_error"
    assert error_frame.payload["recoverable"] is True

    history = orch.conversation_history[sid]
    assert [row["role"] for row in history] == ["user"]
    assert PROVIDER_ERROR_TEXT not in json.dumps(history)
    # One failing call; the empty-response retry must not fire a second one.
    orch.llm.chat_with_failover.assert_awaited_once()


@pytest.mark.asyncio
async def test_multi_agent_provider_error_becomes_error_frame_without_single_agent_retry():
    orch = _orchestrator()
    orch._multi_agent_enabled = True
    orch._multi_agent = MagicMock()
    orch._multi_agent.run = AsyncMock(side_effect=MultiAgentProviderError(PROVIDER_ERROR_TEXT))
    orch.llm.chat_with_failover = AsyncMock(
        return_value={"error": PROVIDER_ERROR_TEXT, "choices": []},
    )
    sid = "sess-multi-agent-error"

    out = await orch.handle_command(sid, "hello Farrell")

    assert out is None
    frames = _sent_frames(orch)
    types = [frame.type for frame in frames]
    assert "error" in types
    assert "text_response" not in types
    assert "sdui" not in types
    orch.llm.chat_with_failover.assert_not_awaited()
    # The multi-agent hand-off runs before the user row is appended, so
    # this turn contributes NO transcript rows at all. What matters
    # either way: the 400 is not stored as anything the model will read
    # back. Before the fix it landed here via ``turn["reply_text"]``.
    history = orch.conversation_history.get(sid, [])
    assert not [row for row in history if row.get("role") == "assistant"]
    assert PROVIDER_ERROR_TEXT not in json.dumps(history)


@pytest.mark.asyncio
async def test_real_text_still_delivered_as_text_response_and_recorded():
    """The fix must not touch the success path."""
    orch = _orchestrator()
    orch.llm.chat_with_failover = AsyncMock(return_value={
        "choices": [{"message": {"role": "assistant", "content": "Lights are on."}}],
    })
    orch.llm.extract_response = MagicMock(return_value=("Lights are on.", []))
    sid = "sess-real-text"

    out = await orch.handle_command(sid, "turn on the lights")

    assert out == "Lights are on."
    types = [frame.type for frame in _sent_frames(orch)]
    assert "text_response" in types
    assert "error" not in types
    assert [row["role"] for row in orch.conversation_history[sid]] == ["user", "assistant"]


# ---------------------------------------------------------------------------
# Bug B: multi-agent worker and orchestrator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_worker_marks_provider_error_instead_of_answering_with_it():
    llm = MagicMock()
    llm.available = True
    llm.chat = AsyncMock(return_value={"error": PROVIDER_ERROR_TEXT, "choices": []})
    llm.extract_response = MagicMock(side_effect=AssertionError("must not be reached"))
    worker = AgentWorker("general", "General", "sys", [], llm=llm)

    result = await worker.run("s1", "hello")

    assert result.text == ""
    assert result.provider_error is True
    assert result.error == PROVIDER_ERROR_TEXT


@pytest.mark.asyncio
async def test_multi_agent_run_raises_on_provider_error_rather_than_returning_it():
    llm = MagicMock()
    llm.available = True
    llm.chat = AsyncMock(return_value={"error": PROVIDER_ERROR_TEXT, "choices": []})
    orch = MultiAgentOrchestrator(llm=llm)

    async def _route(_text: str):
        return {"workers": ["general"], "strategy": "single"}

    orch._router.route = _route  # type: ignore[method-assign]

    with pytest.raises(MultiAgentProviderError) as excinfo:
        await orch.run("s", "hello world")
    assert PROVIDER_ERROR_TEXT in str(excinfo.value)


@pytest.mark.asyncio
async def test_multi_agent_worker_exception_still_returns_string():
    """Regression guard for the existing contract: a worker-side exception
    (not a provider error dict) is still returned as text, as before."""
    llm = MagicMock()
    llm.available = True
    llm.chat = AsyncMock(side_effect=ValueError("boom"))
    orch = MultiAgentOrchestrator(llm=llm)

    async def _route(_text: str):
        return {"workers": ["general"], "strategy": "single"}

    orch._router.route = _route  # type: ignore[method-assign]
    out = await orch.run("s", "hello world")
    assert "boom" in out
