"""
Tests for agents.multi_agent — dataclasses, AgentBus, AgentRouter, ResponseMerger,
AgentWorker, and MultiAgentOrchestrator.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.multi_agent import (
    AgentBus,
    AgentMessage,
    AgentRouter,
    AgentWorker,
    MultiAgentOrchestrator,
    ResponseMerger,
    WorkerResult,
)


# ── AgentMessage ─────────────────────────────────────────────────────────────


def test_agent_message_fields_and_defaults():
    msg = AgentMessage(from_agent="a", to_agent="b", content="hello")
    assert msg.from_agent == "a"
    assert msg.to_agent == "b"
    assert msg.content == "hello"
    assert msg.data == {}
    assert isinstance(msg.timestamp, float)


def test_agent_message_explicit_data_and_timestamp():
    ts = time.time()
    msg = AgentMessage(
        from_agent="x",
        to_agent="y",
        content="payload",
        data={"k": 1},
        timestamp=ts,
    )
    assert msg.data == {"k": 1}
    assert msg.timestamp == ts


def test_agent_message_data_is_not_shared_across_instances():
    m1 = AgentMessage("a", "b", "c")
    m2 = AgentMessage("a", "b", "c")
    m1.data["x"] = 1
    assert "x" not in m2.data


# ── WorkerResult ─────────────────────────────────────────────────────────────


def test_worker_result_defaults():
    r = WorkerResult(worker_id="w1")
    assert r.worker_id == "w1"
    assert r.text == ""
    assert r.tool_calls_made == []
    assert r.tool_results == []
    assert r.confidence == 1.0
    assert r.error == ""


def test_worker_result_all_fields():
    r = WorkerResult(
        worker_id="w2",
        text="ok",
        tool_calls_made=[{"name": "t"}],
        tool_results=[{"data": 1}],
        confidence=0.5,
        error="",
    )
    assert r.text == "ok"
    assert r.tool_calls_made == [{"name": "t"}]
    assert r.confidence == 0.5


def test_worker_result_mutable_lists_not_shared():
    a = WorkerResult("a")
    b = WorkerResult("b")
    a.tool_calls_made.append({})
    assert a.tool_calls_made == [{}]
    assert b.tool_calls_made == []


# ── AgentBus ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_bus_register_post_receive():
    bus = AgentBus()
    bus.register("alice")
    msg = AgentMessage(from_agent="bob", to_agent="alice", content="hi")
    await bus.post(msg)
    received = await bus.receive("alice", timeout=1.0)
    assert received is not None
    assert received.content == "hi"
    assert received.from_agent == "bob"


@pytest.mark.asyncio
async def test_agent_bus_receive_timeout_empty_queue():
    bus = AgentBus()
    bus.register("lonely")
    out = await bus.receive("lonely", timeout=0.05)
    assert out is None


@pytest.mark.asyncio
async def test_agent_bus_receive_unregistered_agent_returns_none_immediately():
    bus = AgentBus()
    # No register — _queues has no entry
    out = await bus.receive("nobody", timeout=5.0)
    assert out is None


@pytest.mark.asyncio
async def test_agent_bus_post_to_unregistered_still_logs():
    bus = AgentBus()
    msg = AgentMessage("a", "missing", "ghost")
    await bus.post(msg)
    assert len(bus._log) == 1
    log = bus.message_log
    assert log[0]["from"] == "a"
    assert log[0]["to"] == "missing"
    assert log[0]["content"] == "ghost"


@pytest.mark.asyncio
async def test_agent_bus_message_log_truncates_long_content():
    bus = AgentBus()
    bus.register("r")
    long_content = "x" * 150
    await bus.post(AgentMessage("a", "r", long_content))
    entry = bus.message_log[-1]
    assert len(entry["content"]) == 100


@pytest.mark.asyncio
async def test_agent_bus_message_log_keeps_last_50():
    bus = AgentBus()
    bus.register("z")
    for i in range(55):
        await bus.post(AgentMessage("s", "z", str(i)))
    # Drain so queue does not grow unbounded in long runs
    for _ in range(55):
        await bus.receive("z", timeout=0.01)
    assert len(bus._log) == 55
    assert len(bus.message_log) == 50
    assert bus.message_log[-1]["content"] == "54"[:100]


# ── AgentRouter._route_with_keywords ─────────────────────────────────────────


def test_router_keywords_health():
    r = AgentRouter(llm=None)
    out = r._route_with_keywords("What is my heart rate today?")
    assert out["workers"] == ["health"]
    assert out["strategy"] == "single"


def test_router_keywords_home():
    r = AgentRouter(llm=None)
    out = r._route_with_keywords("Turn on the living room light")
    assert out["workers"] == ["home"]
    assert out["strategy"] == "single"


def test_router_keywords_research():
    r = AgentRouter(llm=None)
    out = r._route_with_keywords("Please search wikipedia for photosynthesis")
    assert out["workers"] == ["research"]
    assert out["strategy"] == "single"


def test_router_keywords_creative():
    r = AgentRouter(llm=None)
    out = r._route_with_keywords("Play music on spotify")
    assert out["workers"] == ["creative"]
    assert out["strategy"] == "single"


def test_router_keywords_no_match_general():
    r = AgentRouter(llm=None)
    out = r._route_with_keywords("asdf qwerty zxcv")
    assert out["workers"] == ["general"]
    assert out["strategy"] == "single"


def test_router_keywords_parallel_two_categories_high_scores():
    r = AgentRouter(llm=None)
    # Health: >=2 keyword hits; Home: >=2 — second top score >= 2 triggers parallel
    text = "heart rate blood pressure light thermostat"
    out = r._route_with_keywords(text)
    assert out["strategy"] == "parallel"
    assert set(out["workers"]) == {"health", "home"}
    assert len(out["workers"]) == 2


# ── AgentRouter.route (LLM vs keywords) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_router_route_no_llm_uses_keywords():
    r = AgentRouter(llm=None)
    out = await r.route("my blood pressure")
    assert out["workers"] == ["health"]


@pytest.mark.asyncio
async def test_router_route_llm_unavailable_uses_keywords():
    llm = MagicMock()
    llm.available = False
    r = AgentRouter(llm=llm)
    out = await r.route("thermostat setting")
    assert out["workers"] == ["home"]


@pytest.mark.asyncio
async def test_router_route_uses_llm_when_available():
    llm = MagicMock()
    llm.available = True
    llm.chat = AsyncMock(
        return_value={"choices": [{"message": {"content": '{"workers": ["research"], "strategy": "single"}'}}]}
    )
    llm.extract_response = MagicMock(
        return_value=('{"workers": ["research"], "strategy": "single"}', [])
    )
    r = AgentRouter(llm=llm)
    out = await r.route("anything")
    assert out["workers"] == ["research"]
    assert out["strategy"] == "single"
    llm.chat.assert_awaited()


@pytest.mark.asyncio
async def test_router_route_llm_exception_falls_back_to_keywords():
    llm = MagicMock()
    llm.available = True
    llm.chat = AsyncMock(side_effect=RuntimeError("network"))
    r = AgentRouter(llm=llm)
    out = await r.route("steps and sleep tracking")
    assert out["workers"] == ["health"]


@pytest.mark.asyncio
async def test_route_with_llm_strips_markdown_fence():
    llm = MagicMock()
    llm.available = True
    raw = '```json\n{"workers": ["creative"], "strategy": "single"}\n```'
    llm.chat = AsyncMock(return_value={})
    llm.extract_response = MagicMock(return_value=(raw, []))

    r = AgentRouter(llm=llm)
    out = await r._route_with_llm("schedule a meeting")
    assert out["workers"] == ["creative"]


# ── AgentRouter general-override + word boundaries ──────────────────────────


@pytest.mark.asyncio
async def test_router_coding_request_forces_general_before_llm():
    """'Build it on my desktop as an html' must NEVER reach a specialist —
    only the general worker has computer_use/file tools. Regression for the
    iOS 'I'm the FERAL Home Controller, I don't have file system access'
    refusal."""
    llm = MagicMock()
    llm.available = True
    llm.chat = AsyncMock()
    r = AgentRouter(llm=llm)
    out = await r.route("Build it on my desktop as an html and open after you build it")
    assert out["workers"] == ["general"]
    assert out["strategy"] == "single"
    llm.chat.assert_not_awaited()  # hard guard runs before the LLM classifier


@pytest.mark.asyncio
async def test_router_file_and_code_keywords_force_general():
    r = AgentRouter(llm=None)
    for text in (
        "write a python script for me",
        "save that to a file please",
        "run this command in the terminal",
        "make a css animation",
    ):
        out = await r.route(text)
        assert out["workers"] == ["general"], text


def test_router_keywords_use_word_boundaries_not_substrings():
    """'play' in 'display', 'ac' in 'exactly', 'fan' in 'fantastic' must not
    score — bare substring matching dragged unrelated text to specialists."""
    r = AgentRouter(llm=None)
    out = r._route_with_keywords("display exactly that fantastic thing")
    assert out["workers"] == ["general"]


def test_router_keywords_real_words_still_match():
    r = AgentRouter(llm=None)
    out = r._route_with_keywords("play a song")
    assert out["workers"] == ["creative"]


# ── AgentWorker empty-response recovery + token budget ───────────────────────


@pytest.mark.asyncio
async def test_agent_worker_empty_response_nudges_then_recovers():
    llm = MagicMock()
    llm.available = True
    llm.chat = AsyncMock(return_value={})
    responses = iter([("", []), ("recovered answer", [])])
    llm.extract_response = MagicMock(side_effect=lambda _r: next(responses))

    w = AgentWorker("gen", "G", "SYS", [], llm=llm)
    r = await w.run("s1", "hello")
    assert r.text == "recovered answer"
    assert llm.chat.await_count == 2


@pytest.mark.asyncio
async def test_agent_worker_never_surfaces_no_response_generated():
    llm = MagicMock()
    llm.available = True
    llm.chat = AsyncMock(return_value={})
    llm.extract_response = MagicMock(return_value=("", []))

    w = AgentWorker("gen", "G", "SYS", [], llm=llm)
    r = await w.run("s1", "hello")
    assert "No response generated" not in r.text
    assert r.text  # friendly, non-empty fallback


@pytest.mark.asyncio
async def test_agent_worker_chat_uses_expanded_token_budget():
    """The provider default (1024) hard-truncated long answers mid-sentence
    on the multi-agent path. Workers must request an explicit larger budget."""
    llm = MagicMock()
    llm.available = True
    llm.chat = AsyncMock(return_value={})
    llm.extract_response = MagicMock(return_value=("ok", []))

    w = AgentWorker("gen", "G", "SYS", [], llm=llm)
    await w.run("s1", "long question")
    assert llm.chat.call_args.kwargs.get("max_tokens", 0) >= 4096


# ── ResponseMerger ──────────────────────────────────────────────────────────


def test_response_merger_single_valid():
    merged = ResponseMerger.merge([WorkerResult("a", text="only")])
    assert merged == "only"


def test_response_merger_multiple_joined():
    merged = ResponseMerger.merge(
        [
            WorkerResult("a", text="first"),
            WorkerResult("b", text="second"),
        ]
    )
    assert merged == "first\n\nsecond"


def test_response_merger_all_errors_returns_first_error():
    merged = ResponseMerger.merge(
        [
            WorkerResult("a", error="e1"),
            WorkerResult("b", error="e2"),
        ]
    )
    assert merged == "e1"


def test_response_merger_no_results_empty_list():
    assert ResponseMerger.merge([]) == "No response from any worker."


def test_response_merger_ignores_empty_text_and_errors_for_valid():
    merged = ResponseMerger.merge(
        [
            WorkerResult("a", text="", error="skip"),
            WorkerResult("b", text="keep"),
        ]
    )
    assert merged == "keep"


# ── AgentWorker ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_worker_run_no_llm():
    w = AgentWorker("w", "n", "sys", [], llm=None)
    r = await w.run("sid", "hi")
    assert r.error == "LLM not available"


@pytest.mark.asyncio
async def test_agent_worker_run_llm_not_available():
    llm = MagicMock()
    llm.available = False
    w = AgentWorker("w", "n", "sys", [], llm=llm)
    r = await w.run("sid", "hi")
    assert r.error == "LLM not available"


@pytest.mark.asyncio
async def test_agent_worker_run_success_mock_llm():
    llm = MagicMock()
    llm.available = True
    llm.chat = AsyncMock(return_value={"choices": []})
    llm.extract_response = MagicMock(return_value=("Hello from worker", []))

    w = AgentWorker("gen", "General", "You are helpful.", [], llm=llm)
    r = await w.run("session-1", "ping", context="")
    assert r.text == "Hello from worker"
    assert r.error == ""
    llm.chat.assert_awaited()


@pytest.mark.asyncio
async def test_agent_worker_run_with_memory_and_perception_mock():
    llm = MagicMock()
    llm.available = True
    llm.chat = AsyncMock(return_value={})
    llm.extract_response = MagicMock(return_value=("done", []))

    perception = MagicMock()
    frame = MagicMock()
    frame.to_system_context = MagicMock(return_value="screen: ok")
    perception.get_frame = MagicMock(return_value=frame)

    memory = MagicMock()
    memory.build_context_for_llm = AsyncMock(return_value="mem: x")

    w = AgentWorker(
        "gen",
        "G",
        "SYS",
        [],
        llm=llm,
        memory=memory,
        perception=perception,
    )
    r = await w.run("s1", "q")
    assert r.text == "done"
    perception.get_frame.assert_called_once_with("s1")
    memory.build_context_for_llm.assert_called_once()
    call_kw = llm.chat.call_args
    messages = call_kw[1]["messages"]
    sys_content = messages[0]["content"]
    assert "[Environment]" in sys_content
    assert "[Memory]" in sys_content


def test_agent_worker_get_tools_no_registry():
    w = AgentWorker("w", "n", "sys", ["x"], llm=MagicMock(available=True))
    assert w.get_tools() == []


def test_agent_worker_get_tools_with_registry_empty_skill_ids_uses_get_all_tools():
    reg = MagicMock()
    reg.skills = {}
    reg.get_all_tools = MagicMock(return_value=[{"type": "function", "function": {"name": "all"}}])
    w = AgentWorker("w", "n", "sys", [], llm=MagicMock(available=True), skill_registry=reg)
    tools = w.get_tools()
    assert tools == [{"type": "function", "function": {"name": "all"}}]
    reg.get_all_tools.assert_called_once()


def test_agent_worker_get_tools_manifest_from_matching_skills():
    reg = MagicMock()
    skill = MagicMock()
    reg.skills = {"my_skill": skill}
    reg._manifest_to_tools = MagicMock(return_value=[{"type": "function", "function": {"name": "my_skill__ep"}}])
    w = AgentWorker(
        "w",
        "n",
        "sys",
        ["my_skill"],
        llm=MagicMock(available=True),
        skill_registry=reg,
    )
    tools = w.get_tools()
    assert len(tools) == 1
    reg._manifest_to_tools.assert_called_once_with(skill)


# ── MultiAgentOrchestrator ───────────────────────────────────────────────────


def test_multi_agent_orchestrator_init_creates_workers_and_stats():
    llm = MagicMock()
    llm.available = True
    orch = MultiAgentOrchestrator(llm=llm)
    keys = set(orch._workers.keys())
    assert keys == {"health", "home", "research", "creative", "general"}
    st = orch.stats
    assert set(st["workers"]) == keys
    assert st["bus_messages"] == 0


@pytest.mark.asyncio
async def test_multi_agent_orchestrator_run_keyword_routing_mock_llm():
    llm = MagicMock()
    llm.available = True
    llm.chat = AsyncMock(return_value={})
    llm.extract_response = MagicMock(return_value=(" Routed answer ", []))

    orch = MultiAgentOrchestrator(llm=llm)
    # Avoid router LLM path consuming chat/extract; force health worker
    async def _mock_route(_text: str):
        return {"workers": ["health"], "strategy": "single"}

    orch._router.route = _mock_route
    out = await orch.run("sess", "ignored for routing")
    assert out.strip() == "Routed answer"


@pytest.mark.asyncio
async def test_multi_agent_orchestrator_run_parallel_merge():
    llm = MagicMock()
    llm.available = True
    llm.chat = AsyncMock(return_value={})
    responses = iter(
        [
            ("Health line", []),
            ("Home line", []),
        ]
    )

    def _extract(_resp):
        return next(responses)

    llm.extract_response = MagicMock(side_effect=_extract)

    orch = MultiAgentOrchestrator(llm=llm)

    async def _mock_route(_text: str):
        return {"workers": ["health", "home"], "strategy": "parallel"}

    orch._router.route = _mock_route
    out = await orch.run("sess", "parallel domains")
    assert "Health line" in out
    assert "Home line" in out
    assert "\n\n" in out


@pytest.mark.asyncio
async def test_multi_agent_orchestrator_run_returns_error_string_when_worker_fails():
    llm = MagicMock()
    llm.available = True
    llm.chat = AsyncMock(side_effect=ValueError("boom"))
    orch = MultiAgentOrchestrator(llm=llm)

    async def _mock_route(_text: str):
        return {"workers": ["general"], "strategy": "single"}

    orch._router.route = _mock_route
    out = await orch.run("s", "hello world")
    assert "boom" in out
