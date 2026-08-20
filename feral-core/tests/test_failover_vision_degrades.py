"""Failover onto a text-only provider degrades instead of hard-erroring.

``chat_with_failover`` used to run one pre-flight vision check against
``self.provider`` and answer ``{"error": ...}`` when it failed. Two ways
that was wrong:

* a text-only PRIMARY with a vision-capable fallback never reached the
  fallback that could actually see the image, and
* a vision-capable primary that failed over mid-chain to a text-only hop
  turned a recoverable degradation into a dead turn.

The comment at the DeepSeek branch of ``_vision_support_status`` already
claimed "returning False here makes the caller strip the image blocks
and send the text" — this pins that the code now matches the comment.

No provider is contacted: ``_call_provider`` is replaced by a recorder
and the request payloads are inspected in-process.
"""

from __future__ import annotations

import pytest

from agents.llm_provider import LLMProvider


IMAGE_TURN = [
    {"role": "system", "content": "you are feral"},
    {
        "role": "user",
        "content": [
            {"type": "text", "text": "what is on my screen"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    },
]


def _provider(primary: str, fallbacks: list[str]) -> LLMProvider:
    p = LLMProvider.__new__(LLMProvider)
    p.provider = primary
    p.model = "primary-model"
    p.base_url = "https://example.invalid/v1"
    p.api_key = "k"
    p.client = None
    p.available = True
    p._local_engine = None
    p._hybrid_cloud_provider = None
    p._config = {"fallback_providers": fallbacks}
    p._last_failover = None
    p._last_budget_routing = None
    p._cooldown = type("C", (), {
        "should_probe": lambda _s, _p: True,
        "record_success": lambda *_a, **_k: None,
        "record_failure": lambda *_a, **_k: None,
        "is_available": lambda *_a, **_k: True,
        "_cooldowns": {},
    })()
    return p


def _install_recorder(p: LLMProvider, calls: list, *, fail_first: bool = False):
    """Replace the wire call with a recorder. No network, no provider."""
    async def fake_call_provider(provider_name, config, messages, tools, **kwargs):
        calls.append({"provider": provider_name, "messages": messages})
        if fail_first and len(calls) == 1:
            raise RuntimeError("primary is down")
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    p._call_provider = fake_call_provider  # type: ignore[assignment]

    async def no_budget(*_a, **_kw):
        return None

    p._budget_check = no_budget  # type: ignore[assignment]
    p._budget_record = no_budget  # type: ignore[assignment]
    p._route_candidates_with_budget = lambda c, m, k: (c, {})  # type: ignore[assignment]
    return calls


def _images_in(messages) -> int:
    n = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            n += sum(1 for b in c if LLMProvider._is_vision_block(b))
    return n


# ── the stripper itself ──────────────────────────────────────────────────────


def test_strip_removes_images_and_leaves_a_note_the_model_can_read():
    out, dropped = LLMProvider._strip_vision_for_text_only_hop(
        IMAGE_TURN, provider="deepseek", reason="DeepSeek chat models are text-only.",
    )
    assert dropped == 1
    assert _images_in(out) == 0
    note = out[1]["content"][-1]["text"]
    assert "image(s) were removed" in note
    assert "deepseek" in note
    assert "have NOT seen" in note
    # The surviving text is preserved, not replaced by the note.
    assert any(
        b.get("text") == "what is on my screen"
        for b in out[1]["content"] if isinstance(b, dict)
    )


def test_strip_does_not_mutate_the_caller_s_messages():
    original = [dict(m) for m in IMAGE_TURN]
    LLMProvider._strip_vision_for_text_only_hop(IMAGE_TURN, provider="deepseek")
    assert _images_in(IMAGE_TURN) == 1
    assert IMAGE_TURN == original


def test_strip_is_a_no_op_on_a_text_only_transcript():
    msgs = [{"role": "user", "content": "hello"}]
    out, dropped = LLMProvider._strip_vision_for_text_only_hop(msgs, provider="x")
    assert dropped == 0
    assert out == msgs


def test_strip_collapses_a_tool_result_to_a_plain_string():
    """A ``role: "tool"`` message wants a string body on OpenAI-shape wires."""
    msgs = [{
        "role": "tool",
        "tool_call_id": "call-1",
        "content": [
            {"type": "text", "text": '{"ok": true}'},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
        ],
    }]
    out, dropped = LLMProvider._strip_vision_for_text_only_hop(
        msgs, provider="deepseek", reason="text-only.",
    )
    assert dropped == 1
    assert isinstance(out[0]["content"], str)
    assert '{"ok": true}' in out[0]["content"]
    assert "have NOT seen" in out[0]["content"]


# ── per-hop capability, not primary capability ───────────────────────────────


def test_vision_capability_is_asked_per_candidate():
    p = _provider("openai", ["deepseek"])
    assert p._vision_support_for("openai", "gpt-5.5")[0] is True
    assert p._vision_support_for("deepseek", "deepseek-v4-pro")[0] is False
    # The old entry point keeps its meaning: the LIVE provider.
    assert p._vision_support_status()[0] is True


# ── failover behaviour ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_failover_to_a_text_only_hop_strips_and_continues():
    p = _provider("openai", ["deepseek"])
    calls: list = []
    _install_recorder(p, calls, fail_first=True)

    out = await p.chat_with_failover(IMAGE_TURN, tools=None)

    assert "error" not in out, "a recoverable degradation became a dead turn"
    assert [c["provider"] for c in calls] == ["openai", "deepseek"]
    # The vision-capable primary got the image intact...
    assert _images_in(calls[0]["messages"]) == 1
    # ...and the text-only fallback got text plus a note.
    assert _images_in(calls[1]["messages"]) == 0
    assert out["vision_degraded"]["provider"] == "deepseek"
    assert out["vision_degraded"]["images_dropped"] == 1
    assert out["metadata"]["vision_degraded"]["images_dropped"] == 1


@pytest.mark.asyncio
async def test_text_only_primary_no_longer_blocks_a_vision_capable_fallback():
    """This chain used to return an error before trying anything."""
    p = _provider("deepseek", ["openai"])
    calls: list = []
    _install_recorder(p, calls, fail_first=True)

    out = await p.chat_with_failover(IMAGE_TURN, tools=None)

    assert "error" not in out
    assert [c["provider"] for c in calls] == ["deepseek", "openai"]
    assert _images_in(calls[0]["messages"]) == 0  # stripped for deepseek
    assert _images_in(calls[1]["messages"]) == 1  # intact for openai
    # openai answered with the image, so nothing was degraded on the
    # winning hop.
    assert "vision_degraded" not in out


@pytest.mark.asyncio
async def test_a_vision_capable_chain_is_untouched():
    p = _provider("openai", ["anthropic"])
    calls: list = []
    _install_recorder(p, calls)

    out = await p.chat_with_failover(IMAGE_TURN, tools=None)

    assert "error" not in out
    assert len(calls) == 1
    assert _images_in(calls[0]["messages"]) == 1
    assert "vision_degraded" not in out


@pytest.mark.asyncio
async def test_a_text_only_turn_is_never_rewritten():
    p = _provider("deepseek", [])
    calls: list = []
    _install_recorder(p, calls)
    msgs = [{"role": "user", "content": "hello"}]

    await p.chat_with_failover(msgs, tools=None)

    assert calls[0]["messages"] is msgs
