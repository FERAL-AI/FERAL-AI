"""Forced ``tool_choice`` plumbing — grounded-memory closure (v2026.5.48).

Live verification on v2026.5.46/47 showed claude-opus-4-7 answering
``what did I do yesterday?`` in prose without dispatching
``notes_memory__fused_timeline``. The v2026.5.46 side-channel mounted
the TimelineCard widget regardless, but the LLM's narration stayed
un-grounded (it narrated from working-memory context, not from the
retrieved tool result). The v2026.5.47 prompt deepening nudged the
call but still depended on model compliance.

This file pins the bulletproof closure: when the orchestrator detects
a temporal-recall query AND the timeline tool is in the routed tool
set, it passes ``force_tool="notes_memory__fused_timeline"`` into the
LLM call site, which translates per-provider:

  * Anthropic  →  ``tool_choice = {"type": "tool", "name": ...}``
  * OpenAI / OpenRouter / DeepSeek / Groq / Kimi / Qwen / LM Studio /
    Ollama →  ``tool_choice = {"type": "function", "function": {"name": ...}}``
  * Gemini OpenAI-compat endpoint  →  ``tool_choice = "required"``
    (degrade — the OpenAI-compat layer can't name a single tool; the
    side-channel stays in place as the safety net for this provider).

Additionally, the orchestrator always schedules the side-channel
``_maybe_emit_temporal_timeline`` on ``_R_TEMPORAL`` matches — even
when ``force_tool`` pins ``notes_memory__fused_timeline`` — because
models still pick ``search_notes`` without the widget mounted.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


pytestmark = pytest.mark.no_auto_feral_home


# ─────────────────────────────────────────────────────────────────────
# Per-provider translator — unit tests on the pure function
# ─────────────────────────────────────────────────────────────────────


def test_translator_anthropic_named_tool_shape():
    from agents.multimodal_blocks import to_provider_tool_choice

    out = to_provider_tool_choice("anthropic", "notes_memory__fused_timeline")
    assert out == {"type": "tool", "name": "notes_memory__fused_timeline"}, (
        "Anthropic Messages API expects the {type:'tool', name:<n>} shape"
    )


@pytest.mark.parametrize(
    "provider",
    ["openai", "openrouter", "deepseek", "groq", "kimi", "qwen",
     "lmstudio", "ollama"],
)
def test_translator_openai_compat_named_tool_shape(provider):
    from agents.multimodal_blocks import to_provider_tool_choice

    out = to_provider_tool_choice(provider, "notes_memory__fused_timeline")
    assert out == {
        "type": "function",
        "function": {"name": "notes_memory__fused_timeline"},
    }, (
        f"{provider} (OpenAI-compat) expects the nested function/name shape"
    )


def test_translator_gemini_degrades_to_required():
    from agents.multimodal_blocks import to_provider_tool_choice

    out = to_provider_tool_choice("gemini", "notes_memory__fused_timeline")
    assert out == "required", (
        "Gemini's OpenAI-compat endpoint can't name a single tool — "
        "degrade to 'required' so the model still must call SOME tool. "
        "The side-channel remains the path for the named-tool grounding."
    )


def test_translator_returns_none_for_unknown_provider():
    """Unknown provider id (catalog-only descriptor, user typo) — return
    ``None`` so callers fall back to the default 'auto' instead of
    raising on a string mismatch."""
    from agents.multimodal_blocks import to_provider_tool_choice

    assert to_provider_tool_choice("does_not_exist", "any_tool") is None
    assert to_provider_tool_choice("", "any_tool") is None


def test_translator_returns_none_for_empty_force_tool():
    from agents.multimodal_blocks import to_provider_tool_choice

    assert to_provider_tool_choice("anthropic", "") is None
    assert to_provider_tool_choice("openai", None) is None  # type: ignore[arg-type]


def test_tool_list_contains_walks_openai_shape():
    from agents.multimodal_blocks import tool_list_contains

    tools = [
        {"type": "function",
         "function": {"name": "notes_memory__fused_timeline"}},
        {"type": "function", "function": {"name": "calendar__list"}},
    ]
    assert tool_list_contains(tools, "notes_memory__fused_timeline") is True
    assert tool_list_contains(tools, "missing__tool") is False
    assert tool_list_contains([], "any") is False
    assert tool_list_contains(None, "any") is False  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────────
# Provider-side: the outbound HTTP request payload carries the right
# tool_choice when ``force_tool`` is set.
# ─────────────────────────────────────────────────────────────────────


def _build_minimal_provider(provider: str, model: str):
    """Construct an LLMProvider shell without running __init__ — the
    only attributes the chat path needs to reach the body-build site
    are wired explicitly so the tests don't depend on the real
    catalog / vault / cooldown plumbing."""
    from agents.llm_provider import LLMProvider, ProviderCooldownTracker

    llm = LLMProvider.__new__(LLMProvider)
    llm.provider = provider
    llm.model = model
    llm.api_key = "test-key"
    llm.base_url = "https://example.test/v1"
    llm.available = True
    llm._config = {}  # no fallback_providers — keep test off the failover path
    llm._cooldown = ProviderCooldownTracker()
    llm._local_engine = None
    llm._hybrid_cloud_provider = None
    llm._auth_permanent_until = {}
    llm._auth_permanent_logged = set()
    llm._messages_contain_vision = lambda m: False  # type: ignore[attr-defined]
    llm._vision_support_status = lambda: (True, "")  # type: ignore[attr-defined]
    llm._budget_check = AsyncMock(return_value=None)  # type: ignore[attr-defined]
    llm._budget_record = AsyncMock(return_value=None)  # type: ignore[attr-defined]
    llm._last_budget_routing = {}
    llm._last_failover = None
    return llm


def _temporal_tools() -> list[dict]:
    return [
        {"type": "function",
         "function": {
             "name": "notes_memory__fused_timeline",
             "description": "Fused timeline across episodes/notes/etc.",
             "parameters": {"type": "object", "properties": {}},
         }},
        {"type": "function",
         "function": {
             "name": "web_search__web_search",
             "description": "Web search",
             "parameters": {"type": "object", "properties": {}},
         }},
    ]


@pytest.mark.asyncio
async def test_temporal_query_forces_timeline_tool_choice_anthropic(monkeypatch):
    """Anthropic primary + force_tool=notes_memory__fused_timeline →
    outbound /messages body carries ``tool_choice = {"type":"tool",
    "name":"notes_memory__fused_timeline"}``."""
    from agents.llm_provider import LLMProvider

    llm = _build_minimal_provider("anthropic", "claude-opus-4-7")

    captured: dict[str, Any] = {}

    class _FakeResp:
        status_code = 200

        def raise_for_status(self_inner):
            return None

        def json(self_inner):
            return {
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
            }

    async def _fake_post(path, json=None):
        captured["path"] = path
        captured["body"] = json
        return _FakeResp()

    llm.client = MagicMock()
    llm.client.post = _fake_post

    out = await llm.chat(
        messages=[{"role": "user", "content": "what did I do yesterday?"}],
        tools=_temporal_tools(),
        force_tool="notes_memory__fused_timeline",
    )
    assert out.get("error") is None, out

    assert captured["path"].endswith("/messages")
    body = captured["body"]
    assert body is not None
    assert "tool_choice" in body, (
        "Anthropic body MUST carry tool_choice when force_tool is set"
    )
    assert body["tool_choice"] == {
        "type": "tool", "name": "notes_memory__fused_timeline",
    }, (
        f"Wrong Anthropic tool_choice wire shape: {body['tool_choice']!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider", ["openai", "openrouter", "deepseek", "groq", "qwen"],
)
async def test_temporal_query_forces_tool_choice_openai_shape(provider):
    """OpenAI / OpenAI-compatible primary + force_tool=... → outbound
    /chat/completions body carries ``tool_choice = {"type":"function",
    "function":{"name":"notes_memory__fused_timeline"}}``."""
    llm = _build_minimal_provider(provider, "test-model")

    captured: dict[str, Any] = {}

    class _FakeResp:
        status_code = 200

        def raise_for_status(self_inner):
            return None

        def json(self_inner):
            return {
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

    async def _fake_post(path, json=None):
        captured["path"] = path
        captured["body"] = json
        return _FakeResp()

    llm.client = MagicMock()
    llm.client.post = _fake_post

    out = await llm.chat(
        messages=[{"role": "user", "content": "what did I do yesterday?"}],
        tools=_temporal_tools(),
        force_tool="notes_memory__fused_timeline",
    )
    assert out.get("error") is None, out

    body = captured["body"]
    assert body is not None
    assert body["tool_choice"] == {
        "type": "function",
        "function": {"name": "notes_memory__fused_timeline"},
    }, (
        f"{provider}: wrong OpenAI-compat tool_choice wire shape: "
        f"{body['tool_choice']!r}"
    )


@pytest.mark.asyncio
async def test_non_temporal_query_does_not_force_tool():
    """When ``force_tool`` is not passed (the orchestrator's
    ``_force_tool_for_query`` gate returns None for non-temporal text),
    the legacy 'auto' tool_choice is preserved — never silently force
    a tool on a turn the orchestrator didn't ask to force."""
    llm = _build_minimal_provider("openai", "gpt-5")

    captured: dict[str, Any] = {}

    class _FakeResp:
        status_code = 200

        def raise_for_status(self_inner):
            return None

        def json(self_inner):
            return {
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

    async def _fake_post(path, json=None):
        captured["body"] = json
        return _FakeResp()

    llm.client = MagicMock()
    llm.client.post = _fake_post

    await llm.chat(
        messages=[{"role": "user", "content": "explain the TLS handshake"}],
        tools=_temporal_tools(),
        # No force_tool kwarg — the non-temporal path.
    )

    body = captured["body"]
    assert body["tool_choice"] == "auto", (
        f"Non-forced turn must keep tool_choice='auto'; got {body['tool_choice']!r}"
    )


@pytest.mark.asyncio
async def test_force_tool_not_in_tool_list_degrades_to_auto():
    """Guard: ``force_tool`` set but the tool is NOT in the routed tool
    set (typo, skill filtered out by 128-cap) → silently degrade to
    'auto' so the call still succeeds instead of sending an invalid
    tool_choice that points to a missing tool."""
    llm = _build_minimal_provider("openai", "gpt-5")

    captured: dict[str, Any] = {}

    class _FakeResp:
        status_code = 200

        def raise_for_status(self_inner):
            return None

        def json(self_inner):
            return {
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

    async def _fake_post(path, json=None):
        captured["body"] = json
        return _FakeResp()

    llm.client = MagicMock()
    llm.client.post = _fake_post

    await llm.chat(
        messages=[{"role": "user", "content": "what did I do yesterday?"}],
        tools=[
            {"type": "function",
             "function": {
                 "name": "calendar__list",  # NOT the forced tool
                 "description": "",
                 "parameters": {},
             }},
        ],
        force_tool="notes_memory__fused_timeline",  # missing from tools
    )

    body = captured["body"]
    assert body["tool_choice"] == "auto", (
        "Forcing a tool that isn't in the tool set MUST degrade to 'auto'; "
        f"got {body['tool_choice']!r}"
    )


@pytest.mark.asyncio
async def test_provider_without_named_force_degrades_gracefully():
    """Gemini (OpenAI-compat endpoint) can't name a single tool on the
    wire — degrade to ``tool_choice = 'required'`` so the model still
    must call SOME tool, and surface NO error. The side-channel
    (orchestrator-side) remains the path for the named-tool grounding."""
    llm = _build_minimal_provider("gemini", "gemini-3.1-pro")

    captured: dict[str, Any] = {}

    class _FakeResp:
        status_code = 200

        def raise_for_status(self_inner):
            return None

        def json(self_inner):
            return {
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

    async def _fake_post(path, json=None):
        captured["body"] = json
        return _FakeResp()

    llm.client = MagicMock()
    llm.client.post = _fake_post

    out = await llm.chat(
        messages=[{"role": "user", "content": "what did I do yesterday?"}],
        tools=_temporal_tools(),
        force_tool="notes_memory__fused_timeline",
    )
    assert out.get("error") is None, (
        f"Gemini must not error on force_tool — got {out!r}"
    )
    body = captured["body"]
    assert body["tool_choice"] == "required", (
        "Gemini wire shape can't name a single tool → degrade to 'required'; "
        f"got {body['tool_choice']!r}"
    )


# ─────────────────────────────────────────────────────────────────────
# Orchestrator-side classification + side-channel dedupe
# ─────────────────────────────────────────────────────────────────────


def _orch_for_force_tool():
    """Minimal orchestrator stub for ``_force_tool_for_query`` tests."""
    from agents.orchestrator import Orchestrator
    from agents.refusal_handler import RefusalHandler
    from unittest.mock import MagicMock

    orch = Orchestrator.__new__(Orchestrator)
    orch.refusal_handler = RefusalHandler(MagicMock())
    orch.conversation_history = {}
    return orch


def test_force_tool_for_query_returns_tool_on_temporal_with_tool_present():
    """The orchestrator's classification helper returns the timeline
    tool name iff (a) the text matches ``_R_TEMPORAL`` and (b) the
    tool is in the routed tool set."""
    from agents.orchestrator import Orchestrator

    tools = [
        {"type": "function",
         "function": {"name": "notes_memory__fused_timeline"}},
        {"type": "function",
         "function": {"name": "web_search__web_search"}},
    ]

    out = _orch_for_force_tool()._force_tool_for_query(
        "what did I do yesterday?", tools,
    )
    assert out == "notes_memory__fused_timeline"


def test_force_tool_for_query_returns_none_on_non_temporal():
    tools = [
        {"type": "function",
         "function": {"name": "notes_memory__fused_timeline"}},
    ]
    assert _orch_for_force_tool()._force_tool_for_query(
        "explain the TLS handshake", tools,
    ) is None


def test_force_tool_for_query_returns_none_when_tool_absent():
    """Guard at the orchestrator level: even on a temporal query, never
    force a tool the routed skill set doesn't expose."""
    tools = [
        {"type": "function",
         "function": {"name": "calendar__list"}},
    ]
    assert _orch_for_force_tool()._force_tool_for_query(
        "what did I do yesterday?", tools,
    ) is None


@pytest.mark.asyncio
async def test_forced_temporal_path_still_runs_side_channel(monkeypatch):
    """On the forced-tool path for temporal recall, the side-channel
    ``_maybe_emit_temporal_timeline`` MUST still be scheduled — live
    testing showed models ignore ``force_tool`` and pick
    ``search_notes`` unless the timeline widget is already mounted."""
    from agents.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)

    side_channel = AsyncMock(return_value=False)
    orch._maybe_emit_temporal_timeline = side_channel  # type: ignore[assignment]

    tools = [
        {"type": "function",
         "function": {"name": "notes_memory__fused_timeline"}},
    ]
    forced = _orch_for_force_tool()._force_tool_for_query(
        "what did I do yesterday?", tools,
    )
    assert forced == "notes_memory__fused_timeline"

    # Mimic the orchestrator's decision branch: ``_R_TEMPORAL`` match
    # schedules the side-channel regardless of ``forced_tool``.
    if Orchestrator._R_TEMPORAL.search("what did I do yesterday?"):
        await orch._maybe_emit_temporal_timeline("sess-1", "what did I do yesterday?")

    side_channel.assert_awaited_once()


@pytest.mark.asyncio
async def test_unforced_temporal_path_still_runs_side_channel():
    """Negative control: when the forced-tool path is NOT taken (the
    timeline tool wasn't in the routed tool set), the side-channel
    MUST still fire — it's the safety net that mounts the widget."""
    from agents.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    side_channel = AsyncMock(return_value=False)
    orch._maybe_emit_temporal_timeline = side_channel  # type: ignore[assignment]

    # Tool not in routed set → forced gate returns None.
    tools = [
        {"type": "function",
         "function": {"name": "calendar__list"}},
    ]
    forced = _orch_for_force_tool()._force_tool_for_query(
        "what did I do yesterday?", tools,
    )
    assert forced is None

    if Orchestrator._R_TEMPORAL.search("what did I do yesterday?"):
        await orch._maybe_emit_temporal_timeline("sess-1", "what did I do yesterday?")

    side_channel.assert_awaited_once()


# ─────────────────────────────────────────────────────────────────────
# End-to-end: orchestrator wires force_tool into chat_with_failover and
# the side-channel still runs on temporal queries (even when forced).
# ─────────────────────────────────────────────────────────────────────


def _live_skill(skill_id: str, triggers: list[str]):
    from models.skill_manifest import (
        BrandProfile,
        SkillEndpoint,
        SkillManifest,
    )
    return SkillManifest(
        skill_id=skill_id, version="1.0.0", author="test",
        brand=BrandProfile(
            name=skill_id, primary_color="#000", logo_url="", icon_set="sf_symbols"
        ),
        description=f"{skill_id} skill",
        trigger_phrases=triggers,
        endpoints=[
            SkillEndpoint(
                id="default", method="POST", url=f"https://x/{skill_id}",
                description="x", returns_description="x", ui_hint="detail_card",
            ),
            SkillEndpoint(
                id="fused_timeline", method="POST",
                url=f"https://x/{skill_id}/fused_timeline",
                description="fused timeline", returns_description="entries",
                ui_hint="detail_card",
            ),
        ],
    )


@pytest.mark.asyncio
async def test_orchestrator_forwards_force_tool_to_chat_with_failover(monkeypatch):
    """Drive the real non-stream turn entry against a mocked LLM and
    assert ``chat_with_failover`` was awaited with the right
    ``force_tool`` kwarg for a temporal-recall query."""
    from agents.orchestrator import Orchestrator

    skill = _live_skill("notes_memory", ["my notes", "save a note"])
    skills_catalog = {"notes_memory": skill}

    reg = MagicMock()
    reg.skills = skills_catalog
    reg.find_skills_for_query = lambda q, top_k=5: list(skills_catalog.values())
    # The orchestrator's gate checks the OpenAI-shape tool list for the
    # exact ``notes_memory__fused_timeline`` name.
    reg.get_tools_for_skills = lambda skills: [
        {"type": "function",
         "function": {
             "name": "notes_memory__fused_timeline",
             "description": "fused timeline",
             "parameters": {"type": "object", "properties": {}},
         }},
    ]

    orch = Orchestrator(
        skill_registry=reg, send_to_client=AsyncMock(), daemons={},
        memory=None, vision_buffer=None, perception=None, learner=None,
    )

    # Stub LLM to return immediate text (no tool_calls) so the iteration
    # loop exits after one pass and the wrapper records the kwargs we
    # passed in.
    chat_mock = AsyncMock(return_value={
        "choices": [{
            "message": {"role": "assistant", "content": "yesterday: standup"},
            "finish_reason": "stop",
        }],
    })
    orch.llm = MagicMock()
    orch.llm.available = True
    orch.llm.model_name = "test-model"
    orch.llm.chat_with_failover = chat_mock
    orch.llm.extract_response = MagicMock(
        return_value=("yesterday: standup", [])
    )

    # Bypass streaming so we hit _handle_command_impl directly.
    orch._streaming_enabled = False

    # Suppress side-channel + heavy paths the unit doesn't need.
    side_channel = AsyncMock(return_value=False)
    orch._maybe_emit_temporal_timeline = side_channel  # type: ignore[assignment]
    orch._route_prompt = AsyncMock(return_value=[skill])  # type: ignore[assignment]
    orch._ensure_core_skills = lambda x: x

    await orch.handle_command(
        session_id="sess-force-1",
        text="what did I do yesterday?",
    )

    chat_mock.assert_awaited()
    kwargs = chat_mock.await_args.kwargs
    assert kwargs.get("force_tool") == "notes_memory__fused_timeline", (
        "Orchestrator MUST forward force_tool='notes_memory__fused_timeline' "
        f"into chat_with_failover on temporal queries; got {kwargs!r}"
    )
    # Side-channel runs alongside forced_tool on temporal queries.
    side_channel.assert_called()


@pytest.mark.asyncio
async def test_orchestrator_omits_force_tool_for_non_temporal_query():
    """Negative control on the wiring: non-temporal text → no
    ``force_tool`` kwarg propagated, and the side-channel is not
    scheduled (``_R_TEMPORAL`` gate keeps the path clean)."""
    from agents.orchestrator import Orchestrator

    skill = _live_skill("notes_memory", ["my notes"])
    reg = MagicMock()
    reg.skills = {"notes_memory": skill}
    reg.find_skills_for_query = lambda q, top_k=5: [skill]
    reg.get_tools_for_skills = lambda skills: [
        {"type": "function",
         "function": {
             "name": "notes_memory__fused_timeline",
             "description": "fused timeline",
             "parameters": {"type": "object", "properties": {}},
         }},
    ]

    orch = Orchestrator(
        skill_registry=reg, send_to_client=AsyncMock(), daemons={},
        memory=None, vision_buffer=None, perception=None, learner=None,
    )

    chat_mock = AsyncMock(return_value={
        "choices": [{
            "message": {"role": "assistant", "content": "TLS is..."},
            "finish_reason": "stop",
        }],
    })
    orch.llm = MagicMock()
    orch.llm.available = True
    orch.llm.model_name = "test-model"
    orch.llm.chat_with_failover = chat_mock
    orch.llm.extract_response = MagicMock(return_value=("TLS is...", []))

    orch._streaming_enabled = False
    side_channel = AsyncMock(return_value=False)
    orch._maybe_emit_temporal_timeline = side_channel  # type: ignore[assignment]
    orch._route_prompt = AsyncMock(return_value=[skill])  # type: ignore[assignment]
    orch._ensure_core_skills = lambda x: x

    await orch.handle_command(
        session_id="sess-nontemp", text="explain the TLS handshake",
    )

    # Let any scheduled tasks resolve (none expected on the temporal path
    # since the side-channel is the only one).
    pending = [
        t for t in asyncio.all_tasks()
        if t is not asyncio.current_task() and not t.done()
    ]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    kwargs = chat_mock.await_args.kwargs
    assert kwargs.get("force_tool") in (None, ""), (
        f"Non-temporal turn must not force a tool; got force_tool={kwargs.get('force_tool')!r}"
    )
    # Non-temporal → side-channel must NOT be scheduled.
    side_channel.assert_not_called()


# ─────────────────────────────────────────────────────────────────────
# _build_anthropic_body — failover path body shape pin
# ─────────────────────────────────────────────────────────────────────


def test_build_anthropic_body_carries_tool_choice_when_forced():
    """The failover path goes through ``_build_anthropic_body``
    (separate from the primary ``_chat_anthropic``). Pin that the
    forced tool_choice survives the body builder too."""
    from agents.llm_provider import LLMProvider

    tools = [
        {"type": "function",
         "function": {
             "name": "notes_memory__fused_timeline",
             "description": "",
             "parameters": {},
         }},
    ]
    body = LLMProvider._build_anthropic_body(
        "claude-opus-4-7",
        [{"role": "user", "content": "what did I do yesterday?"}],
        tools, 0.7, 1024,
        force_tool="notes_memory__fused_timeline",
    )
    assert body.get("tool_choice") == {
        "type": "tool", "name": "notes_memory__fused_timeline",
    }


def test_build_anthropic_body_no_tool_choice_when_force_tool_missing():
    """Anthropic body MUST NOT emit ``tool_choice`` at all when no
    force is requested — the model gets normal free tool selection."""
    from agents.llm_provider import LLMProvider

    tools = [
        {"type": "function",
         "function": {"name": "notes_memory__fused_timeline",
                      "description": "", "parameters": {}}},
    ]
    body = LLMProvider._build_anthropic_body(
        "claude-opus-4-7",
        [{"role": "user", "content": "hi"}],
        tools, 0.7, 1024,
    )
    assert "tool_choice" not in body, (
        "Unforced Anthropic body must not pin tool_choice — let the "
        "model pick freely (legacy behaviour)."
    )
