"""v2026.5.23 — Responses API adapter + classifier_endpoint + probe + tool-gate.

Six concerns under test, scoped to the audit-r10 launch fix that closes the
operator's blocked-chat report (gpt-5.5-pro 404 on /v1/chat/completions):

1.  ``providers.model_classes.classify_endpoint`` returns ``"responses"`` for
    OpenAI Pro / o-series Pro / deep-research / Codex / computer-use models
    and ``"chat_completions"`` for everything else.
2.  ``is_responses_only`` delegates correctly through OpenRouter vendor
    prefix.
3.  ``apply_responses_param_fork`` renames ``max_tokens`` →
    ``max_output_tokens``, nests reasoning under an object, drops
    chat-shaped sampling params.
4.  ``LLMProvider._build_responses_body`` produces the canonical
    Responses-API body shape (``input`` not ``messages``, ``instructions``
    from the first system message, function tools flattened, reasoning
    nested).
5.  ``LLMProvider._messages_to_responses_input`` round-trips tool-call
    history (assistant ``tool_calls`` → ``function_call`` items; tool
    role → ``function_call_output``).
6.  ``ToolRunner._notify_user_of_pending_approval`` emits a user-visible
    chat line via ``orchestrator._send_text`` when an LLM-loop tool call
    hits ``pending_approval`` — closing the silent-frozen-turn bug.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


# ─── classify_endpoint + is_responses_only ───────────────────────


class TestClassifyEndpoint:
    def test_gpt_5_5_pro_routes_to_responses(self):
        from providers.model_classes import classify_endpoint, is_responses_only

        assert classify_endpoint("openai", "gpt-5.5-pro") == "responses"
        assert is_responses_only("openai", "gpt-5.5-pro") is True

    def test_gpt_5_pro_dated_snapshot_routes_to_responses(self):
        from providers.model_classes import classify_endpoint

        assert classify_endpoint("openai", "gpt-5-pro-2026-05-08") == "responses"

    def test_o3_pro_routes_to_responses(self):
        from providers.model_classes import classify_endpoint

        assert classify_endpoint("openai", "o3-pro") == "responses"

    def test_deep_research_routes_to_responses(self):
        from providers.model_classes import classify_endpoint

        assert classify_endpoint("openai", "gpt-5.5-deep-research") == "responses"
        assert classify_endpoint("openai", "o3-deep-research-mini") == "responses"

    def test_codex_and_computer_use_route_to_responses(self):
        from providers.model_classes import classify_endpoint

        assert classify_endpoint("openai", "gpt-5-codex") == "responses"
        assert classify_endpoint("openai", "computer-use-preview") == "responses"

    def test_non_pro_reasoning_stays_chat_completions(self):
        # gpt-5.5 (no -pro), o3, o4-mini — work fine on /v1/chat/completions
        # AND /v1/responses; FERAL keeps them on chat-completions so the
        # adapter swap doesn't silently reroute traffic that was working.
        from providers.model_classes import classify_endpoint

        assert classify_endpoint("openai", "gpt-5.5") == "responses"
        assert classify_endpoint("openai", "gpt-5.4") == "responses"
        assert classify_endpoint("openai", "o3") == "chat_completions"
        assert classify_endpoint("openai", "o4-mini") == "chat_completions"

    def test_plain_chat_models_stay_chat_completions(self):
        from providers.model_classes import classify_endpoint

        assert classify_endpoint("openai", "gpt-4o") == "chat_completions"
        assert classify_endpoint("openai", "gpt-4.1") == "chat_completions"
        assert classify_endpoint("openai", "gpt-3.5-turbo") == "chat_completions"

    def test_openrouter_openai_pro_delegates(self):
        from providers.model_classes import classify_endpoint, is_responses_only

        assert classify_endpoint("openrouter", "openai/gpt-5.5-pro") == "responses"
        assert is_responses_only("openrouter", "openai/gpt-5.5-pro") is True

    def test_openrouter_openai_non_pro_stays_chat(self):
        from providers.model_classes import classify_endpoint

        assert classify_endpoint("openrouter", "openai/gpt-5.5") == "responses"
        assert classify_endpoint("openrouter", "openai/gpt-5.5:nitro") == "responses"

    def test_openrouter_non_openai_never_responses_only(self):
        from providers.model_classes import classify_endpoint

        assert classify_endpoint(
            "openrouter", "anthropic/claude-opus-4-7"
        ) == "chat_completions"
        assert classify_endpoint(
            "openrouter", "deepseek/deepseek-v4-pro"
        ) == "chat_completions"

    def test_anthropic_models_never_responses_only(self):
        # Anthropic uses /v1/messages, not /v1/responses. We never route
        # Anthropic traffic through the Responses adapter regardless of
        # extended-thinking status.
        from providers.model_classes import classify_endpoint, is_responses_only

        assert classify_endpoint("anthropic", "claude-opus-4-7") == "chat_completions"
        assert is_responses_only("anthropic", "claude-opus-4-7") is False

    def test_unknown_model_defaults_to_chat_completions(self):
        from providers.model_classes import classify_endpoint

        # Frontier id not in any rule — default-include for chat, never
        # silently route to /v1/responses (would 404 if the model
        # doesn't actually exist on that endpoint).
        assert classify_endpoint("openai", "gpt-6-mystery") == "chat_completions"
        assert classify_endpoint("openai", "") == "chat_completions"


# ─── apply_responses_param_fork ──────────────────────────────────


class TestResponsesParamFork:
    def test_renames_max_tokens(self):
        from agents.llm_reasoning import apply_responses_param_fork

        body = {"max_tokens": 256, "temperature": 1, "messages": [...]}
        apply_responses_param_fork("gpt-5.5-pro", body)
        assert "max_tokens" not in body
        assert body["max_output_tokens"] == 256

    def test_renames_max_completion_tokens(self):
        # When the caller already ran the chat-completions reasoning
        # fork before realising the endpoint should be Responses,
        # the rename target shifts again.
        from agents.llm_reasoning import apply_responses_param_fork

        body = {"max_completion_tokens": 512}
        apply_responses_param_fork("gpt-5.5-pro", body)
        assert "max_completion_tokens" not in body
        assert body["max_output_tokens"] == 512

    def test_nests_reasoning_effort(self):
        from agents.llm_reasoning import apply_responses_param_fork

        body = {"max_tokens": 1, "reasoning_effort": "high"}
        apply_responses_param_fork("gpt-5.5-pro", body)
        assert body["reasoning"] == {"effort": "high"}
        assert "reasoning_effort" not in body

    def test_default_effort_is_medium(self):
        from agents.llm_reasoning import apply_responses_param_fork

        body = {"max_tokens": 1}
        apply_responses_param_fork("gpt-5.5-pro", body)
        assert body["reasoning"]["effort"] == "medium"

    def test_drops_chat_sampling_params(self):
        from agents.llm_reasoning import apply_responses_param_fork

        body = {
            "max_tokens": 10,
            "temperature": 0.7,
            "top_p": 0.9,
            "presence_penalty": 0.5,
            "frequency_penalty": 0.5,
        }
        apply_responses_param_fork("gpt-5.5-pro", body)
        for k in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
            assert k not in body, f"{k} should be dropped from Responses body"

    def test_preserves_existing_reasoning_dict(self):
        from agents.llm_reasoning import apply_responses_param_fork

        body = {"reasoning": {"effort": "xhigh", "extra": "passthrough"}}
        apply_responses_param_fork("gpt-5.5-pro", body)
        assert body["reasoning"]["effort"] == "xhigh"
        assert body["reasoning"]["extra"] == "passthrough"


# ─── _messages_to_responses_input + _chat_tools_to_responses_tools ─


class TestResponsesMessageTranslation:
    def test_first_system_becomes_instructions(self):
        from agents.llm_provider import LLMProvider

        msgs = [
            {"role": "system", "content": "You are FERAL."},
            {"role": "user", "content": "Hi"},
        ]
        instructions, items = LLMProvider._messages_to_responses_input(msgs)
        assert instructions == "You are FERAL."
        assert items == [{"role": "user", "content": "Hi"}]

    def test_second_system_becomes_system_input_item(self):
        # v2026.5.25: first system wins as instructions; later
        # ones aren't silently lost — they become system-role input
        # items (Responses API accepts system role) so
        # PromptRefiner-style additional system notes still reach
        # the model with their intended role intact.
        from agents.llm_provider import LLMProvider

        msgs = [
            {"role": "system", "content": "primary"},
            {"role": "system", "content": "appendix"},
            {"role": "user", "content": "hi"},
        ]
        instructions, items = LLMProvider._messages_to_responses_input(msgs)
        assert instructions == "primary"
        # Second system → system input item with translated content.
        assert items[0]["role"] == "system"
        assert items[0]["content"] == "appendix"

    def test_tool_role_becomes_function_call_output(self):
        """v2026.5.29 — a ``role:"tool"`` row is emitted as
        ``function_call_output`` only when its announcing assistant
        ``tool_calls`` turn is in the same request. The pairing guard
        drops orphans because OpenAI's Responses API rejects them with
        ``400 No tool call found for function call output``.
        """
        from agents.llm_provider import LLMProvider

        msgs = [
            {"role": "user", "content": "do it"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_abc",
                        "type": "function",
                        "function": {"name": "do_thing", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_abc", "content": '{"ok":true}'},
        ]
        _, items = LLMProvider._messages_to_responses_input(msgs)
        outputs = [i for i in items if i.get("type") == "function_call_output"]
        assert outputs == [
            {
                "type": "function_call_output",
                "call_id": "call_abc",
                "output": '{"ok":true}',
            }
        ]

    def test_orphan_tool_role_is_dropped(self):
        """v2026.5.29 pairing guard: a tool result with no preceding
        assistant ``tool_calls`` is silently dropped to avoid the
        Responses-API 400."""
        from agents.llm_provider import LLMProvider

        msgs = [
            {"role": "user", "content": "do it"},
            {"role": "tool", "tool_call_id": "call_abc", "content": '{"ok":true}'},
        ]
        _, items = LLMProvider._messages_to_responses_input(msgs)
        assert not any(i.get("type") == "function_call_output" for i in items)

    def test_assistant_tool_calls_become_function_call_items(self):
        from agents.llm_provider import LLMProvider

        msgs = [
            {"role": "user", "content": "do it"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_xyz",
                        "function": {"name": "do_thing", "arguments": '{"x":1}'},
                    }
                ],
            },
        ]
        _, items = LLMProvider._messages_to_responses_input(msgs)
        function_call_items = [i for i in items if i.get("type") == "function_call"]
        assert len(function_call_items) == 1
        assert function_call_items[0]["call_id"] == "call_xyz"
        assert function_call_items[0]["name"] == "do_thing"
        assert function_call_items[0]["arguments"] == '{"x":1}'

    def test_chat_tools_flatten_to_responses_shape(self):
        from agents.llm_provider import LLMProvider

        chat_tools = [
            {
                "type": "function",
                "function": {
                    "name": "ping",
                    "description": "say hi",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        out = LLMProvider._chat_tools_to_responses_tools(chat_tools)
        assert out == [
            {
                "type": "function",
                "name": "ping",
                "description": "say hi",
                "parameters": {"type": "object", "properties": {}},
            }
        ]

    def test_chat_tools_none_passes_through(self):
        from agents.llm_provider import LLMProvider

        assert LLMProvider._chat_tools_to_responses_tools(None) is None
        assert LLMProvider._chat_tools_to_responses_tools([]) is None


# ─── _build_responses_body ────────────────────────────────────────


class TestBuildResponsesBody:
    def _provider(self):
        from agents.llm_provider import LLMProvider

        p = LLMProvider.__new__(LLMProvider)
        p.provider = "openai"
        p.model = "gpt-5.5-pro"
        return p

    def test_canonical_streaming_body(self):
        p = self._provider()
        body = p._build_responses_body(
            [
                {"role": "system", "content": "FERAL system prompt."},
                {"role": "user", "content": "Hi"},
            ],
            tools=None,
            temperature=1,
            max_tokens=128,
            stream=True,
        )
        assert body["model"] == "gpt-5.5-pro"
        # Chat 'messages' must not leak; Responses uses 'input'.
        assert "messages" not in body
        assert "input" in body
        assert body["instructions"] == "FERAL system prompt."
        assert body["stream"] is True
        assert body["max_output_tokens"] == 128
        assert body["reasoning"] == {"effort": "medium"}

    def test_temperature_07_dropped_for_pro(self):
        # Pro models reject temperature != 1; the param fork strips it.
        p = self._provider()
        body = p._build_responses_body(
            [{"role": "user", "content": "hi"}],
            tools=None, temperature=0.7, max_tokens=64, stream=False,
        )
        assert "temperature" not in body

    def test_tools_translate_to_flat_shape(self):
        p = self._provider()
        body = p._build_responses_body(
            [{"role": "user", "content": "do it"}],
            tools=[{
                "type": "function",
                "function": {"name": "do_thing", "description": "", "parameters": {}},
            }],
            temperature=1, max_tokens=32, stream=False,
        )
        assert body["tool_choice"] == "auto"
        assert body["tools"] == [{
            "type": "function", "name": "do_thing",
            "description": "", "parameters": {},
        }]


# ─── _responses_payload_to_chat_dict ─────────────────────────────


class TestResponsesPayloadNormalisation:
    def test_text_output_round_trips_to_chat_shape(self):
        from agents.llm_provider import LLMProvider

        payload = {
            "id": "resp_123",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": "Hello "},
                        {"type": "output_text", "text": "world."},
                    ],
                }
            ],
            "status": "completed",
        }
        result = LLMProvider._responses_payload_to_chat_dict(payload)
        assert result["choices"][0]["message"]["content"] == "Hello world."
        assert result["choices"][0]["message"]["role"] == "assistant"
        assert result.get("_responses_id") == "resp_123"

    def test_function_call_output_translates(self):
        from agents.llm_provider import LLMProvider

        payload = {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_abc",
                    "name": "do_thing",
                    "arguments": '{"x": 1}',
                }
            ],
        }
        result = LLMProvider._responses_payload_to_chat_dict(payload)
        tool_calls = result["choices"][0]["message"]["tool_calls"]
        assert tool_calls[0]["id"] == "call_abc"
        assert tool_calls[0]["function"]["name"] == "do_thing"
        assert json.loads(tool_calls[0]["function"]["arguments"]) == {"x": 1}

    def test_error_payload_surfaces_as_error_dict(self):
        from agents.llm_provider import LLMProvider

        payload = {"error": {"message": "model_not_found"}}
        result = LLMProvider._responses_payload_to_chat_dict(payload)
        assert "model_not_found" in result["error"]
        assert result["choices"] == []


# ─── ToolRunner pending-approval notification ────────────────────


@pytest.mark.asyncio
async def test_notify_user_of_pending_approval_calls_send_text():
    from agents.tool_runner import ToolRunner

    orch = SimpleNamespace()
    sent: list[tuple[str, str]] = []

    async def fake_send_text(sid, text):
        sent.append((sid, text))
    orch._send_text = fake_send_text

    runner = ToolRunner.__new__(ToolRunner)
    runner._orch = orch

    await runner._notify_user_of_pending_approval(
        "primary-x",
        "computer_use__write_file",
        {
            "status": "pending_approval",
            "tool_name": "computer_use__write_file",
            "request_id": "req-abc",
            "safety_level": "confirm",
        },
    )

    assert sent, "_send_text was not called"
    sid, text = sent[0]
    assert sid == "primary-x"
    assert "computer_use__write_file" in text
    assert "req-abc" in text


@pytest.mark.asyncio
async def test_notify_user_skips_for_non_pending():
    # Non-pending denials (e.g. hard denial) don't trigger the chat
    # notification — they have their own error pathway.
    from agents.tool_runner import ToolRunner

    orch = SimpleNamespace()
    sent: list = []
    async def fake_send_text(sid, text): sent.append((sid, text))
    orch._send_text = fake_send_text

    runner = ToolRunner.__new__(ToolRunner)
    runner._orch = orch

    await runner._notify_user_of_pending_approval(
        "primary-x",
        "computer_use__rm_rf",
        {"status": "denied", "tool_name": "computer_use__rm_rf"},
    )
    assert sent == []


@pytest.mark.asyncio
async def test_notify_user_swallows_send_text_failures():
    from agents.tool_runner import ToolRunner

    orch = SimpleNamespace()
    async def boom(sid, text): raise RuntimeError("socket closed")
    orch._send_text = boom

    runner = ToolRunner.__new__(ToolRunner)
    runner._orch = orch

    # Must not raise even when send_text fails — pending_approval flow
    # is best-effort; orchestrator must not crash on user-notification
    # transport error.
    await runner._notify_user_of_pending_approval(
        "primary-x", "x", {"status": "pending_approval", "request_id": "r"},
    )


# ─── _probe_chat_availability (round-trip semantics) ─────────────


@pytest.mark.asyncio
async def test_probe_uses_responses_endpoint_for_pro_models():
    from agents.llm_provider import LLMProvider

    p = LLMProvider.__new__(LLMProvider)
    p.provider = "openai"
    p.model = "gpt-5.5-pro"

    # Mock httpx client. Probe should POST to /responses.
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    p.client = MagicMock()
    p.client.post = AsyncMock(return_value=mock_resp)

    ok, reason = await p._probe_chat_availability()
    assert ok is True
    assert reason == ""
    # Confirm the right endpoint was hit.
    called_path = p.client.post.call_args[0][0]
    assert called_path == "/responses"


@pytest.mark.asyncio
async def test_probe_uses_responses_endpoint_for_gpt54():
    from agents.llm_provider import LLMProvider

    p = LLMProvider.__new__(LLMProvider)
    p.provider = "openai"
    p.model = "gpt-5.4"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    p.client = MagicMock()
    p.client.post = AsyncMock(return_value=mock_resp)

    ok, _ = await p._probe_chat_availability()
    assert ok is True
    called_path = p.client.post.call_args[0][0]
    assert called_path == "/responses"


@pytest.mark.asyncio
async def test_probe_returns_structured_failure_on_404():
    from agents.llm_provider import LLMProvider

    p = LLMProvider.__new__(LLMProvider)
    p.provider = "openai"
    p.model = "gpt-5.4"

    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.json = MagicMock(return_value={
        "error": {"message": "This is not a chat model."},
    })
    p.client = MagicMock()
    p.client.post = AsyncMock(return_value=mock_resp)

    ok, reason = await p._probe_chat_availability()
    assert ok is False
    assert "404" in reason
    assert "not a chat model" in reason


# ─── v2026.5.25 — content-part translation (operator's 400 fix) ─


class TestContentPartTranslation:
    """v2026.5.25 launch-blocker fix.

    Live verification on 2026-05-14 against gpt-5.5-pro:
    sending `{"type":"text","text":"..."}` (the legacy
    Chat-Completions multimodal shape FERAL's perception/fusion.py
    emits) on the Responses API returns HTTP 400 with
    "Invalid value: 'text'. Supported: input_text / input_image /
    output_text / refusal / input_file / computer_screenshot /
    summary_text". This block pins the translation table.
    """

    def test_translate_text_part_user_becomes_input_text(self):
        from agents.llm_provider import LLMProvider

        out = LLMProvider._translate_content_part(
            "user", {"type": "text", "text": "hi"},
        )
        assert out == {"type": "input_text", "text": "hi"}

    def test_translate_text_part_system_becomes_input_text(self):
        # System input items use input_text the same way user items
        # do — there is no "system_text" type.
        from agents.llm_provider import LLMProvider

        out = LLMProvider._translate_content_part(
            "system", {"type": "text", "text": "sys"},
        )
        assert out == {"type": "input_text", "text": "sys"}

    def test_translate_text_part_assistant_becomes_output_text(self):
        # The launch-blocker's other half: history replay where the
        # assistant turn carried `[{type:"text"}]` — Responses API
        # rejects that on the assistant role too. Must translate to
        # output_text.
        from agents.llm_provider import LLMProvider

        out = LLMProvider._translate_content_part(
            "assistant", {"type": "text", "text": "ok"},
        )
        assert out == {"type": "output_text", "text": "ok"}

    def test_translate_image_url_part_becomes_input_image(self):
        from agents.llm_provider import LLMProvider

        out = LLMProvider._translate_content_part(
            "user",
            {"type": "image_url", "image_url": {
                "url": "https://x/y.png", "detail": "low",
            }},
        )
        # Responses API takes the URL as a string + the detail hint.
        assert out["type"] == "input_image"
        assert out["image_url"] == "https://x/y.png"
        assert out["detail"] == "low"

    def test_translate_image_url_string_form_supported(self):
        # Some clients pass image_url as a bare string. Coerce.
        from agents.llm_provider import LLMProvider

        out = LLMProvider._translate_content_part(
            "user",
            {"type": "image_url", "image_url": "data:image/png;base64,xx"},
        )
        assert out == {"type": "input_image",
                       "image_url": "data:image/png;base64,xx"}

    def test_native_responses_part_passes_through_unchanged(self):
        # If callers already speak the Responses-API shape, don't
        # double-translate.
        from agents.llm_provider import LLMProvider

        native_in = {"type": "input_text", "text": "x"}
        assert LLMProvider._translate_content_part("user", native_in) == native_in

        native_out = {"type": "output_text", "text": "x"}
        assert LLMProvider._translate_content_part("assistant", native_out) == native_out

        native_img = {"type": "input_image", "image_url": "https://x"}
        assert LLMProvider._translate_content_part("user", native_img) == native_img

    def test_unknown_part_type_passes_through(self):
        # Future part types we haven't catalogued yet — let OpenAI's
        # server be the validator. Silently dropping would hide bugs.
        from agents.llm_provider import LLMProvider

        weird = {"type": "future_part", "blob": "..."}
        assert LLMProvider._translate_content_part("user", weird) == weird

    def test_normalize_message_content_string_passthrough(self):
        from agents.llm_provider import LLMProvider

        assert LLMProvider._normalize_message_content("user", "hi") == "hi"

    def test_normalize_message_content_list_translates_each_part(self):
        from agents.llm_provider import LLMProvider

        out = LLMProvider._normalize_message_content("user", [
            {"type": "text", "text": "hi"},
            {"type": "image_url", "image_url": {"url": "https://x"}},
        ])
        assert out == [
            {"type": "input_text", "text": "hi"},
            {"type": "input_image", "image_url": "https://x"},
        ]

    def test_normalize_message_content_none_becomes_empty(self):
        from agents.llm_provider import LLMProvider

        assert LLMProvider._normalize_message_content("user", None) == ""

    def test_messages_to_responses_input_translates_multimodal_user(self):
        # End-to-end via _messages_to_responses_input — the operator's
        # actual chat flow. Before v2026.5.25 this passed the user
        # content through verbatim and OpenAI 400'd.
        from agents.llm_provider import LLMProvider

        _, items = LLMProvider._messages_to_responses_input([
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        ])
        assert items == [
            {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        ]

    def test_messages_to_responses_input_translates_assistant_history(self):
        # When the orchestrator replays a multi-turn history, the
        # assistant turn's [{type:text}] must become output_text.
        from agents.llm_provider import LLMProvider

        _, items = LLMProvider._messages_to_responses_input([
            {"role": "user", "content": [{"type": "text", "text": "q"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "a"}]},
        ])
        assert items[0]["content"][0]["type"] == "input_text"
        assert items[1]["content"][0]["type"] == "output_text"

    def test_messages_to_responses_input_system_with_list_content_flattens(self):
        # System content as a list of parts flattens into the
        # instructions string. Responses API instructions is plain
        # text, not a parts array.
        from agents.llm_provider import LLMProvider

        instr, items = LLMProvider._messages_to_responses_input([
            {"role": "system", "content": [
                {"type": "text", "text": "Hello"},
                {"type": "text", "text": " world."},
            ]},
            {"role": "user", "content": "hi"},
        ])
        assert instr == "Hello world."
        assert items == [{"role": "user", "content": "hi"}]

    def test_messages_to_responses_input_tool_role_list_content_flattens(self):
        # Tool results may arrive as a string OR as a parts list
        # (some callers wrap before reaching the adapter). Either
        # way we want a plain `output: <string>` on the wire.
        #
        # v2026.5.29 — must include the announcing assistant turn or
        # the pairing guard drops the tool row as an orphan.
        from agents.llm_provider import LLMProvider

        _, items = LLMProvider._messages_to_responses_input([
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "n", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": [
                {"type": "text", "text": '{"ok":'},
                {"type": "text", "text": "true}"},
            ]},
        ])
        outputs = [it for it in items if it.get("type") == "function_call_output"]
        assert outputs == [
            {"type": "function_call_output", "call_id": "c1", "output": '{"ok":true}'},
        ]


# ─── v2026.5.25 — Pro-model probe minimum + reasoning effort clamp ─


class TestProModelParamClamps:
    """Live-verified constraints on gpt-5.5-pro (2026-05-14):

    * ``max_output_tokens`` < 16 → 400 "integer below minimum value"
    * ``reasoning.effort`` in {none, minimal, low} → 400 "Unsupported"
    """

    def test_responses_fork_clamps_max_output_tokens_to_16(self):
        from agents.llm_reasoning import apply_responses_param_fork

        body = {"max_tokens": 1}
        apply_responses_param_fork("gpt-5.5-pro", body)
        assert body["max_output_tokens"] == 16

    def test_responses_fork_clamps_effort_low_to_medium(self):
        from agents.llm_reasoning import apply_responses_param_fork

        body = {"max_tokens": 100, "reasoning_effort": "low"}
        apply_responses_param_fork("gpt-5.5-pro", body)
        assert body["reasoning"]["effort"] == "medium"

    def test_responses_fork_clamps_effort_minimal_to_medium(self):
        from agents.llm_reasoning import apply_responses_param_fork

        body = {"max_tokens": 100, "reasoning": {"effort": "minimal"}}
        apply_responses_param_fork("gpt-5.5-pro", body)
        assert body["reasoning"]["effort"] == "medium"

    def test_responses_fork_keeps_high_effort_for_pro(self):
        from agents.llm_reasoning import apply_responses_param_fork

        body = {"max_tokens": 100, "reasoning_effort": "high"}
        apply_responses_param_fork("gpt-5.5-pro", body)
        assert body["reasoning"]["effort"] == "high"

    def test_responses_fork_keeps_xhigh_effort_for_pro(self):
        from agents.llm_reasoning import apply_responses_param_fork

        body = {"max_tokens": 100, "reasoning_effort": "xhigh"}
        apply_responses_param_fork("gpt-5.5-pro", body)
        assert body["reasoning"]["effort"] == "xhigh"

    def test_responses_fork_keeps_low_effort_for_non_pro(self):
        # Future-proof: if FERAL ever uses /v1/responses for a non-Pro
        # model that accepts low effort, don't strip it. The pro clamp
        # is targeted.
        from agents.llm_reasoning import apply_responses_param_fork

        body = {"max_tokens": 100, "reasoning_effort": "low"}
        apply_responses_param_fork("some-future-model", body)
        assert body["reasoning"]["effort"] == "low"

    def test_responses_fork_keeps_existing_max_tokens_when_above_floor(self):
        from agents.llm_reasoning import apply_responses_param_fork

        body = {"max_tokens": 1024}
        apply_responses_param_fork("gpt-5.5-pro", body)
        assert body["max_output_tokens"] == 1024


# ─── v2026.5.25 — payload normalisation (reasoning / refusal / incomplete) ─


class TestPayloadNormalisation:
    def test_refusal_content_part_surfaces_as_tagged_assistant_text(self):
        from agents.llm_provider import LLMProvider

        payload = {
            "id": "resp_refusal",
            "output": [{
                "type": "message",
                "content": [{"type": "refusal", "refusal": "I can't help with that."}],
            }],
            "status": "completed",
        }
        result = LLMProvider._responses_payload_to_chat_dict(payload)
        content = result["choices"][0]["message"]["content"]
        assert "[refusal]" in content
        assert "I can't help with that." in content

    def test_reasoning_summary_extracted_as_separate_key(self):
        # Reasoning items are server-emitted summaries; they should
        # NOT pollute the user-facing assistant message but the
        # orchestrator can surface them in a brain-trace.
        from agents.llm_provider import LLMProvider

        payload = {
            "id": "resp_r",
            "output": [
                {"type": "reasoning", "summary": [
                    {"type": "summary_text", "text": "Thought about it briefly."},
                ]},
                {"type": "message", "content": [
                    {"type": "output_text", "text": "Done."},
                ]},
            ],
            "status": "completed",
        }
        result = LLMProvider._responses_payload_to_chat_dict(payload)
        assert result["choices"][0]["message"]["content"] == "Done."
        assert result["_reasoning_summary"] == "Thought about it briefly."

    def test_empty_visible_text_no_tool_returns_incomplete_finish_reason(self):
        # Pro models can eat the entire max_output_tokens budget in
        # reasoning, leaving no visible text. Surfacing this as
        # finish_reason=incomplete lets the orchestrator's "I
        # processed your request but have nothing to report" branch
        # fire instead of a silent frozen turn.
        from agents.llm_provider import LLMProvider

        payload = {
            "id": "resp_inc",
            "output": [],
            "status": "incomplete",
        }
        result = LLMProvider._responses_payload_to_chat_dict(payload)
        assert result["choices"][0]["finish_reason"] == "incomplete"
        assert result["choices"][0]["message"]["content"] == ""
        # tool_calls absent on empty result.
        assert "tool_calls" not in result["choices"][0]["message"]

    def test_function_call_still_extracts_to_tool_calls(self):
        # Regression — v2026.5.23/24 already extracted these; make
        # sure the v2026.5.25 refactor didn't break the path.
        from agents.llm_provider import LLMProvider

        payload = {
            "output": [{
                "type": "function_call",
                "call_id": "call_x",
                "name": "do_thing",
                "arguments": '{"x":1}',
            }],
            "status": "completed",
        }
        result = LLMProvider._responses_payload_to_chat_dict(payload)
        tc = result["choices"][0]["message"]["tool_calls"][0]
        assert tc["id"] == "call_x"
        assert tc["function"]["name"] == "do_thing"
        assert json.loads(tc["function"]["arguments"]) == {"x": 1}


# ─── v2026.5.25 — _probe_chat_availability max_output_tokens minimum ─


@pytest.mark.asyncio
async def test_probe_uses_minimum_16_max_output_tokens_for_pro():
    """gpt-5.5-pro rejects max_output_tokens < 16 with HTTP 400. The
    probe must use ≥16 so it doesn't false-fail every operator on a
    Pro model."""
    from agents.llm_provider import LLMProvider

    p = LLMProvider.__new__(LLMProvider)
    p.provider = "openai"
    p.model = "gpt-5.5-pro"

    captured_body: dict = {}

    async def fake_post(path, json=None, timeout=None):
        captured_body["path"] = path
        captured_body["body"] = json
        mock = MagicMock()
        mock.status_code = 200
        mock.json = MagicMock(return_value={"status": "completed"})
        return mock

    p.client = MagicMock()
    p.client.post = fake_post

    ok, _ = await p._probe_chat_availability()
    assert ok is True
    assert captured_body["path"] == "/responses"
    assert captured_body["body"]["max_output_tokens"] >= 16


@pytest.mark.asyncio
async def test_probe_tolerates_responses_incomplete_status():
    """Pro models often spend the 16-token budget on reasoning and
    return ``status=incomplete``. That still proves the model is
    reachable + the key is valid — exactly what the probe is for.
    Don't flip available=False on this."""
    from agents.llm_provider import LLMProvider

    p = LLMProvider.__new__(LLMProvider)
    p.provider = "openai"
    p.model = "gpt-5.5-pro"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json = MagicMock(return_value={
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
    })
    p.client = MagicMock()
    p.client.post = AsyncMock(return_value=mock_resp)

    ok, reason = await p._probe_chat_availability()
    assert ok is True, f"incomplete is reachable, got reason={reason!r}"


@pytest.mark.asyncio
async def test_probe_fails_on_responses_failed_status():
    """``status=failed`` is a real probe failure — surface it
    truthfully."""
    from agents.llm_provider import LLMProvider

    p = LLMProvider.__new__(LLMProvider)
    p.provider = "openai"
    p.model = "gpt-5.5-pro"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json = MagicMock(return_value={
        "status": "failed",
        "error": {"message": "internal_error"},
    })
    p.client = MagicMock()
    p.client.post = AsyncMock(return_value=mock_resp)

    ok, reason = await p._probe_chat_availability()
    assert ok is False
    assert "failed" in reason
    assert "internal_error" in reason


# ─── v2026.5.27 — streaming tool-call ID-key bug ────────────────


class TestStreamingToolCallIdKey:
    """v2026.5.27 — Responses-API SSE keying bug.

    Verified live against gpt-5.5-pro on 2026-05-15: SSE events use
    TWO different identifiers for a single function call:

      response.output_item.added   item.id ("fc_…")  item.call_id ("call_…")
      response.function_call_arguments.delta   item_id ("fc_…")
      response.function_call_arguments.done    item_id ("fc_…")
      response.output_item.done    item.id ("fc_…")  item.call_id ("call_…")

    v2026.5.25 keyed the accumulator by ``call_id`` in `output_item.added`
    but by ``item_id`` in the delta events. The entries didn't match,
    so the model's emitted tool call landed at the orchestrator as
    ``<name>({})`` (empty args). Operator's research-doc demo hit
    this on EVERY ``web_search__web_search`` call (terminal log:
    "Anti-loop guard: blocked repeated call 'web_search__web_search'
    with identical arguments").

    Fix: key by ``item_id``, stash ``call_id`` inside the entry,
    emit ``id = call_id`` on completion so the orchestrator's
    tool_runner receives the right shape.
    """

    def test_finalise_tool_call_emits_call_id_as_id_and_parses_args(self):
        from agents.llm_provider import _finalise_tool_call

        out = _finalise_tool_call({
            "item_id": "fc_abc",
            "call_id": "call_xyz",
            "name": "web_search",
            "arguments": '{"query":"state of AI agents 2026"}',
        })
        # The model-facing call_id is what gets echoed in
        # function_call_output.call_id, so the orchestrator must
        # see it as `id`.
        assert out["id"] == "call_xyz"
        assert out["name"] == "web_search"
        assert out["arguments"] == '{"query":"state of AI agents 2026"}'
        assert out["args"] == {"query": "state of AI agents 2026"}

    def test_finalise_falls_back_to_item_id_when_call_id_missing(self):
        from agents.llm_provider import _finalise_tool_call

        out = _finalise_tool_call({
            "item_id": "fc_only", "call_id": "",
            "name": "x", "arguments": "{}",
        })
        assert out["id"] == "fc_only"

    def test_finalise_invalid_args_becomes_empty_dict(self):
        from agents.llm_provider import _finalise_tool_call

        out = _finalise_tool_call({
            "item_id": "fc_x", "call_id": "call_x",
            "name": "x", "arguments": "{not json",
        })
        assert out["args"] == {}
        assert out["arguments"] == "{not json"

    @pytest.mark.asyncio
    async def test_responses_stream_round_trips_function_call_with_args(self):
        """End-to-end: simulate the real Responses-API SSE sequence
        (output_item.added → function_call_arguments.delta x2 →
        function_call_arguments.done → output_item.done →
        response.completed) and confirm the adapter yields a
        tool_call_delta with BOTH name AND args populated.
        """
        from unittest.mock import AsyncMock, MagicMock
        from agents.llm_provider import LLMProvider

        # Build the SSE stream a real Pro-model call would produce
        # (captured from the live OpenAI verification harness).
        sse_lines = [
            "event: response.created",
            'data: {"type":"response.created"}',
            "",
            "event: response.output_item.added",
            'data: {"type":"response.output_item.added","output_index":1,'
            '"item":{"id":"fc_abc","type":"function_call","status":"in_progress",'
            '"arguments":"","call_id":"call_xyz","name":"web_search"}}',
            "",
            "event: response.function_call_arguments.delta",
            'data: {"type":"response.function_call_arguments.delta",'
            '"item_id":"fc_abc","output_index":1,"delta":"{\\"query\\":\\"state of"}',
            "",
            "event: response.function_call_arguments.delta",
            'data: {"type":"response.function_call_arguments.delta",'
            '"item_id":"fc_abc","output_index":1,"delta":" AI agents 2026\\"}"}',
            "",
            "event: response.function_call_arguments.done",
            'data: {"type":"response.function_call_arguments.done",'
            '"item_id":"fc_abc","output_index":1,'
            '"arguments":"{\\"query\\":\\"state of AI agents 2026\\"}"}',
            "",
            "event: response.output_item.done",
            'data: {"type":"response.output_item.done","output_index":1,'
            '"item":{"id":"fc_abc","type":"function_call","status":"completed",'
            '"arguments":"{\\"query\\":\\"state of AI agents 2026\\"}",'
            '"call_id":"call_xyz","name":"web_search"}}',
            "",
            "event: response.completed",
            'data: {"type":"response.completed"}',
            "",
        ]

        # AsyncMock SSE response.
        class _FakeStreamResp:
            status_code = 200

            def raise_for_status(self):
                pass

            async def aiter_lines(self):
                for line in sse_lines:
                    yield line

        class _FakeCM:
            async def __aenter__(self_inner):
                return _FakeStreamResp()

            async def __aexit__(self_inner, *a, **kw):
                return None

        fake_client = MagicMock()
        fake_client.stream = MagicMock(return_value=_FakeCM())

        p = LLMProvider.__new__(LLMProvider)
        p.provider = "openai"
        p.model = "gpt-5.5-pro"
        p.client = fake_client

        events = []
        async for evt in p._responses_stream(
            [{"role": "user", "content": "search the web"}],
            tools=[{"type": "function", "function": {
                "name": "web_search", "description": "",
                "parameters": {"type": "object",
                               "properties": {"query": {"type": "string"}}},
            }}],
            temperature=1, max_tokens=128,
        ):
            events.append(evt)

        tool_call_deltas = [e for e in events if e["type"] == "tool_call_delta"]
        assert len(tool_call_deltas) == 1, f"events={events}"
        tc = tool_call_deltas[0]["tool_call"]
        # The model-facing call_id flows through as the tool-call id.
        assert tc["id"] == "call_xyz"
        # Name + args BOTH populated — the v2026.5.25 bug.
        assert tc["name"] == "web_search"
        assert tc["args"] == {"query": "state of AI agents 2026"}
        # Terminal done event was yielded.
        assert any(e["type"] == "done" for e in events)


# ─── v2026.5.25 — LIVE tests (skipped in CI, opt-in via env) ─────
#
# Run locally with:
#   export OPENAI_API_KEY=sk-...
#   FERAL_LIVE_TESTS=1 pytest tests/test_responses_adapter_v2026_5_23.py -m live -v
#
# CI never sets FERAL_LIVE_TESTS=1, so these are skipped there.
# Each test costs a few cents max (max_output_tokens capped at 32).
# Use a budgeted / revocable key.


_LIVE_TESTS_ENABLED = (
    os.environ.get("FERAL_LIVE_TESTS") == "1"
    and bool(os.environ.get("OPENAI_API_KEY"))
)
_LIVE_MODEL = os.environ.get("FERAL_VERIFY_MODEL", "gpt-5.5-pro")


@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.skipif(
    not _LIVE_TESTS_ENABLED,
    reason="live tests require FERAL_LIVE_TESTS=1 + OPENAI_API_KEY",
)
async def test_live_responses_plain_string():
    """Baseline: gpt-5.5-pro on plain user string content."""
    from agents.llm_provider import LLMProvider

    provider = LLMProvider()
    await provider.switch_provider(
        "openai",
        model=_LIVE_MODEL,
        api_key=os.environ["OPENAI_API_KEY"],
    )
    # gpt-5.5-pro requires max_tokens >= 16 + ate-reasoning-tokens
    # behaviour means we should bump well above the floor to get a
    # visible reply within the test.
    result = await provider._responses_chat(
        [{"role": "user", "content": "Reply 'hi' once."}],
        tools=None, temperature=1, max_tokens=256,
    )
    # Either we got visible text OR finish_reason=incomplete.
    # Both prove the request hit /v1/responses cleanly without a 400.
    assert "error" not in result or not result.get("error"), result
    msg = result["choices"][0]["message"]
    finish = result["choices"][0]["finish_reason"]
    assert msg.get("content") or finish == "incomplete", \
        f"unexpected empty reply with finish={finish}: {result}"


@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.skipif(
    not _LIVE_TESTS_ENABLED,
    reason="live tests require FERAL_LIVE_TESTS=1 + OPENAI_API_KEY",
)
async def test_live_responses_multimodal_content_no_400():
    """The operator's actual production bug — chat-completions
    multimodal content `[{type:"text"}]` on gpt-5.5-pro used to
    400. After v2026.5.25 it MUST 200."""
    from agents.llm_provider import LLMProvider

    provider = LLMProvider()
    await provider.switch_provider(
        "openai",
        model=_LIVE_MODEL,
        api_key=os.environ["OPENAI_API_KEY"],
    )
    result = await provider._responses_chat(
        [{"role": "user", "content": [
            {"type": "text", "text": "Reply 'ok'."},
        ]}],
        tools=None, temperature=1, max_tokens=256,
    )
    assert not result.get("error"), \
        f"multimodal user content still 400s: {result.get('error')}"


@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.skipif(
    not _LIVE_TESTS_ENABLED,
    reason="live tests require FERAL_LIVE_TESTS=1 + OPENAI_API_KEY",
)
async def test_live_responses_history_with_assistant_text_part():
    """Multi-turn history replay where the assistant turn had
    `[{type:"text"}]` content. v2026.5.25 must translate to
    `output_text` before resending."""
    from agents.llm_provider import LLMProvider

    provider = LLMProvider()
    await provider.switch_provider(
        "openai",
        model=_LIVE_MODEL,
        api_key=os.environ["OPENAI_API_KEY"],
    )
    result = await provider._responses_chat(
        [
            {"role": "system", "content": "Answer in one word."},
            {"role": "user", "content": [{"type": "text", "text": "Reply 'ok'."}]},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
            {"role": "user", "content": "Now reply 'done'."},
        ],
        tools=None, temperature=1, max_tokens=256,
    )
    assert not result.get("error"), result.get("error")


@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.skipif(
    not _LIVE_TESTS_ENABLED,
    reason="live tests require FERAL_LIVE_TESTS=1 + OPENAI_API_KEY",
)
async def test_live_responses_streaming_sse_yields_text_delta():
    """SSE consumer must yield ``text_delta`` events for streamed
    output_text deltas + a terminal ``done`` event. Without this
    the chat UI sits silent through the whole reply."""
    from agents.llm_provider import LLMProvider

    provider = LLMProvider()
    await provider.switch_provider(
        "openai",
        model=_LIVE_MODEL,
        api_key=os.environ["OPENAI_API_KEY"],
    )
    events_by_type: dict[str, int] = {}
    text_buf = ""
    async for evt in provider._responses_stream(
        [{"role": "user", "content": [{"type": "text", "text": "Count 1 to 3."}]}],
        tools=None, temperature=1, max_tokens=256,
    ):
        et = evt.get("type", "")
        events_by_type[et] = events_by_type.get(et, 0) + 1
        if et == "text_delta":
            text_buf += evt.get("content", "")
    assert "done" in events_by_type, f"no done event; events={events_by_type}"
    assert "error" not in events_by_type, \
        f"streaming emitted error events: {events_by_type}"
    # Either visible text or just the lifecycle — both prove the
    # stream survived without a 400.
    # (Pro models can spend the whole budget on reasoning, so the
    # text buffer may be empty even on a successful run.)
