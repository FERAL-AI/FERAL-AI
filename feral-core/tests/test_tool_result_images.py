"""Image-bearing tool results reach the model as images, per provider.

THE DEFECT
----------
Every tool result went through ``skills.result_budget`` twice:
``SkillExecutor._sanitize_with_note`` (structural clamp, ``max_str_len``)
and then ``serialize_tool_result`` (``max_result_chars``). Both tiers for
``gui_computer_use__screenshot`` / ``screen_capture__capture`` /
``browser__screenshot`` resolve to ``standard`` = 2 000. A screenshot is
~400 000 base64 chars, so the model received ~2 000 chars of a JPEG plus
"You have NOT seen the whole result". Half a base64 string is a decode
error, not a smaller image, so every vision-dependent capability in FERAL
was dead.

WHAT THESE TESTS PIN
--------------------
* the image survives BOTH budget layers whole, or is dropped whole;
* the text half is still budgeted exactly as before (regression);
* each provider gets the shape its API actually accepts;
* a provider that cannot take an image is TOLD one existed;
* screenshots are pruned from history in batches, keeping the last N.
"""

from __future__ import annotations

import json

import pytest

from agents.llm_anthropic_shape import _convert_messages_for_anthropic
from agents.multimodal_blocks import (
    IMAGE_DELIVERY_ANTHROPIC_BLOCKS,
    IMAGE_DELIVERY_FOLLOWUP_USER,
    IMAGE_DELIVERY_GEMINI_PARTS,
    IMAGE_DELIVERY_NONE,
    SCREENSHOT_KEEP_LAST_DEFAULT,
    SCREENSHOT_PRUNE_EVERY_DEFAULT,
    extract_tool_result_images,
    image_delivery_mode,
    materialize_tool_result_images,
    prune_tool_result_images,
    should_prune_images,
    tool_result_images_as_gemini_content,
)
from skills.executor import SkillExecutor
from skills.result_budget import (
    budget_for,
    clamp,
    get_budget,
    serialize_tool_result,
    serialize_tool_result_with_images,
    TruncationReport,
)


# ── fixtures / helpers ───────────────────────────────────────────────

#: A JPEG payload of realistic size. ``/9j/`` is the real JPEG base64
#: magic, and it starts with a slash — the detail that broke the first
#: "is this a filesystem path?" heuristic.
JPEG_B64 = "/9j/4AAQSkZJRgABAQAAAQABAAD" + "A" * 400_000
PNG_B64 = "iVBORw0KGgoAAAANSUhEUg" + "B" * 200_000


def gui_screenshot_envelope() -> dict:
    """Exact shape of ``skills/impl/gui_computer_use.py::_screenshot``."""
    return {
        "success": True,
        "status_code": 200,
        "data": {
            "image_base64": JPEG_B64,
            "format": "jpeg",
            "dpi_scale": 2.0,
        },
        "error": None,
    }


def screen_capture_envelope() -> dict:
    """Exact shape of ``skills/impl/screen_capture.py::_capture``."""
    return {
        "success": True,
        "status_code": 200,
        "data": {
            "path": "/tmp/feral_screen_1.png",
            "encoding": "jpeg",
            "captured_at": 1,
            "size_bytes": 300_000,
            "region": None,
            "image_b64": JPEG_B64,
        },
        "error": None,
    }


def browser_screenshot_envelope() -> dict:
    """Exact shape of ``skills/impl/browser_use.py::screenshot``."""
    return {"success": True, "image_b64": PNG_B64, "format": "png"}


def tool_history(text: str, call_id: str = "call_1") -> list[dict]:
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "what is on screen"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": call_id, "type": "function",
                "function": {"name": "gui_computer_use__screenshot", "arguments": "{}"},
            }],
        },
        {
            "role": "tool", "tool_call_id": call_id,
            "name": "gui_computer_use__screenshot", "content": text,
        },
    ]


def side_table(images, call_id: str = "call_1") -> dict:
    return {call_id: {
        "images": [i.to_dict() for i in images],
        "pruned": False,
        "tool_name": "gui_computer_use__screenshot",
    }}


# ── 1. the image is lifted out, whole ────────────────────────────────

@pytest.mark.parametrize("envelope,field", [
    (gui_screenshot_envelope(), "image_base64"),
    (screen_capture_envelope(), "image_b64"),
    (browser_screenshot_envelope(), "image_b64"),
])
def test_every_first_party_capture_shape_is_recognised(envelope, field):
    stripped, images, notes = extract_tool_result_images(envelope)
    assert notes == []
    assert len(images) == 1
    assert images[0].data_url.startswith("data:image/")
    assert images[0].payload_chars > 100_000
    # The blob is gone from the text half and replaced by a marker.
    blob = json.dumps(stripped)
    assert JPEG_B64[:400] not in blob and PNG_B64[:400] not in blob
    assert "lifted out of this JSON" in blob


def test_media_type_comes_from_the_payload_not_a_guess():
    _s, images, _n = extract_tool_result_images(gui_screenshot_envelope())
    assert images[0].media_type == "image/jpeg"
    _s, images, _n = extract_tool_result_images(browser_screenshot_envelope())
    assert images[0].media_type == "image/png"


def test_a_path_under_an_image_key_is_not_mistaken_for_an_image():
    result = {"success": True, "data": {"screenshot": "/tmp/shot.png", "image": None}}
    stripped, images, notes = extract_tool_result_images(result)
    assert images == [] and notes == []
    assert stripped is result  # untouched object on the no-image path


def test_result_without_an_image_is_byte_identical_to_the_old_path():
    """The regression surface for every non-vision tool in the system."""
    result = {"success": True, "data": {"content": "x" * 50_000}, "error": None}
    old = serialize_tool_result("coding_tools__read_file", result)
    new, images = serialize_tool_result_with_images("coding_tools__read_file", result)
    assert images == []
    assert new == old


# ── 2. the 400 KB blob does not go through the text budget ───────────

def test_screenshot_is_not_truncated_to_2000_chars_on_a_vision_provider():
    """The headline measurement from the bug report.

    Before: ``serialize_tool_result('gui_computer_use__screenshot', ...)``
    returned 1405 chars ending in "You have NOT seen the whole result".
    """
    envelope = gui_screenshot_envelope()
    budget = get_budget("standard")
    assert budget.max_result_chars == 2_000  # the tier that killed it

    # The plain text-only serializer still cannot deliver a screenshot:
    # ~400 000 chars cannot be squeezed into 2 000, so it returns a
    # truncation envelope. That is the defect, pinned.
    old = serialize_tool_result("gui_computer_use__screenshot", envelope)
    assert len(old) <= 2_000
    assert json.loads(old)["_truncated"] is True
    assert JPEG_B64 not in old

    text, images = serialize_tool_result_with_images(
        "gui_computer_use__screenshot", envelope, allow_images=True,
    )
    # Text half: still small, still valid JSON, and NOT a truncation notice.
    assert len(text) < budget.max_result_chars
    parsed = json.loads(text)
    assert parsed.get("_truncated") is not True
    assert parsed["data"]["format"] == "jpeg"
    assert parsed["data"]["dpi_scale"] == 2.0
    # Image half: complete, to the character.
    assert len(images) == 1
    assert images[0].payload_chars == len(JPEG_B64)
    assert images[0].data_url == f"data:image/jpeg;base64,{JPEG_B64}"


def test_executor_clamp_no_longer_shreds_the_base64_one_layer_earlier():
    """``_sanitize_with_note`` runs BEFORE history serialization and used
    to cut ``image_base64`` to ``max_str_len`` (2 000). No downstream fix
    could have recovered the image after that."""
    budget = get_budget("standard")
    payload, note = SkillExecutor._sanitize_with_note(
        gui_screenshot_envelope()["data"], budget,
    )
    assert payload["image_base64"] == JPEG_B64
    assert note == ""


def test_clamp_still_bounds_ordinary_text_in_the_same_dict():
    """The image exemption is exactly one key wide. Everything else in the
    same object is still budgeted."""
    budget = get_budget("standard")
    report = TruncationReport()
    out = clamp(
        {"image_base64": JPEG_B64, "console_log": "y" * 50_000},
        budget, report,
    )
    assert out["image_base64"] == JPEG_B64
    assert len(out["console_log"]) < 3_000
    assert "truncated" in out["console_log"]
    assert report.chars_dropped > 40_000


def test_an_oversize_image_is_dropped_whole_never_sliced():
    """A partial base64 string is a decode error. If it cannot go whole it
    does not go at all — and the text says so."""
    huge = "/9j/" + "A" * 9_000_000
    stripped, images, notes = extract_tool_result_images(
        {"success": True, "data": {"image_base64": huge}},
    )
    assert images == []
    assert len(notes) == 1
    assert "NOT sent" in notes[0] and "decode error" in notes[0]
    assert huge[:5_000] not in json.dumps(stripped)

    # And at the clamp layer, same rule.
    out = clamp({"image_base64": huge}, get_budget("standard"))
    assert out["image_base64"].startswith("[image dropped whole:")
    assert huge[:1_000] not in out["image_base64"]


def test_oversize_image_surfaces_in_the_serialized_text():
    huge = "/9j/" + "A" * 9_000_000
    text, images = serialize_tool_result_with_images(
        "gui_computer_use__screenshot",
        {"success": True, "data": {"image_base64": huge, "format": "jpeg"}},
    )
    assert images == []
    parsed = json.loads(text)
    assert parsed["_image_omitted"] is True
    assert parsed["_image_omission_reasons"]


# ── 3. provider matrix ───────────────────────────────────────────────

@pytest.mark.parametrize("provider,expected", [
    ("anthropic", IMAGE_DELIVERY_ANTHROPIC_BLOCKS),
    ("openai", IMAGE_DELIVERY_FOLLOWUP_USER),
    ("openrouter", IMAGE_DELIVERY_FOLLOWUP_USER),
    ("groq", IMAGE_DELIVERY_FOLLOWUP_USER),
    # FERAL's runtime gemini provider drives Google's OpenAI-compat
    # endpoint (/v1beta/openai/chat/completions), so it takes the
    # OpenAI-compatible follow-up-user shape, not the native parts shape.
    ("gemini", IMAGE_DELIVERY_FOLLOWUP_USER),
    ("gemini_native", IMAGE_DELIVERY_GEMINI_PARTS),
    ("ollama", IMAGE_DELIVERY_FOLLOWUP_USER),
    ("some_provider_we_never_heard_of", IMAGE_DELIVERY_NONE),
])
def test_image_delivery_mode_matrix(provider, expected):
    assert image_delivery_mode(provider, vision_supported=True) == expected


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini", "ollama"])
def test_no_vision_capability_overrides_every_provider(provider):
    """Capability is per-MODEL for several providers. A text-only model on
    a vision-capable provider must not get an image on the wire."""
    assert image_delivery_mode(provider, vision_supported=False) == IMAGE_DELIVERY_NONE


def test_anthropic_gets_an_image_block_inside_the_tool_result():
    """Anthropic's computer-use docs, "Implement proper screenshot
    handling": the screenshot goes back as an image content block inside
    the ``tool_result`` content array."""
    text, images = serialize_tool_result_with_images(
        "gui_computer_use__screenshot", gui_screenshot_envelope(),
    )
    msgs = materialize_tool_result_images(
        tool_history(text), side_table(images), IMAGE_DELIVERY_ANTHROPIC_BLOCKS,
    )
    # Canonical internal shape stays OpenAI-flavoured...
    tool_row = msgs[3]
    assert [b["type"] for b in tool_row["content"]] == ["text", "image_url"]
    assert len(msgs) == 4  # no follow-up user turn on this provider

    # ...and the wire shape is Anthropic's.
    _system, anthropic_msgs = _convert_messages_for_anthropic(msgs)
    result_block = anthropic_msgs[-1]["content"][0]
    assert result_block["type"] == "tool_result"
    assert result_block["tool_use_id"] == "call_1"
    assert [b["type"] for b in result_block["content"]] == ["text", "image"]
    image_block = result_block["content"][1]
    assert image_block["source"] == {
        "type": "base64", "media_type": "image/jpeg", "data": JPEG_B64,
    }
    # Whole. Not a prefix.
    assert len(image_block["source"]["data"]) == len(JPEG_B64)


def test_openai_gets_a_followup_user_message_never_an_image_on_the_tool_row():
    """OpenAI chat-completions rejects image content on a ``role:"tool"``
    message; image parts are legal on ``role:"user"`` only."""
    text, images = serialize_tool_result_with_images(
        "gui_computer_use__screenshot", gui_screenshot_envelope(),
    )
    msgs = materialize_tool_result_images(
        tool_history(text), side_table(images), IMAGE_DELIVERY_FOLLOWUP_USER,
    )
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "tool", "user"]
    assert isinstance(msgs[3]["content"], str)      # tool row stays text
    assert json.loads(msgs[3]["content"])           # and stays valid JSON
    followup = msgs[4]
    assert [b["type"] for b in followup["content"]] == ["text", "image_url"]
    assert followup["content"][1]["image_url"]["url"].endswith(JPEG_B64)
    assert "gui_computer_use__screenshot" in followup["content"][0]["text"]
    assert "call_1" in followup["content"][0]["text"]


def test_followup_user_message_never_splits_a_run_of_tool_rows():
    """Three parallel tool calls: inserting the image message between two
    tool rows would orphan the remaining tool_call_ids and 400. It must
    land after the LAST tool row of the run."""
    text, images = serialize_tool_result_with_images(
        "gui_computer_use__screenshot", gui_screenshot_envelope(),
    )
    history = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function",
             "function": {"name": "t", "arguments": "{}"}} for i in (1, 2, 3)
        ]},
        {"role": "tool", "tool_call_id": "c1", "name": "t", "content": "{}"},
        {"role": "tool", "tool_call_id": "c2", "name": "t", "content": text},
        {"role": "tool", "tool_call_id": "c3", "name": "t", "content": "{}"},
        {"role": "assistant", "content": "done"},
    ]
    msgs = materialize_tool_result_images(
        history, side_table(images, "c2"), IMAGE_DELIVERY_FOLLOWUP_USER,
    )
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "assistant", "tool", "tool", "tool", "user", "assistant"]
    assert msgs[5]["content"][1]["type"] == "image_url"


def test_two_images_in_one_tool_run_share_one_followup_message():
    text, images = serialize_tool_result_with_images(
        "gui_computer_use__screenshot", gui_screenshot_envelope(),
    )
    history = [
        {"role": "tool", "tool_call_id": "a", "name": "t", "content": text},
        {"role": "tool", "tool_call_id": "b", "name": "t", "content": text},
    ]
    table = {**side_table(images, "a"), **side_table(images, "b")}
    msgs = materialize_tool_result_images(history, table, IMAGE_DELIVERY_FOLLOWUP_USER)
    assert [m["role"] for m in msgs] == ["tool", "tool", "user"]
    assert [b["type"] for b in msgs[2]["content"]] == ["text", "image_url", "image_url"]


def test_gemini_native_parts_shape():
    _text, images = serialize_tool_result_with_images(
        "gui_computer_use__screenshot", gui_screenshot_envelope(),
    )
    content = tool_result_images_as_gemini_content(
        images, tool_name="gui_computer_use__screenshot", tool_call_id="call_1",
    )
    assert content["role"] == "user"
    assert "text" in content["parts"][0]
    assert content["parts"][1] == {
        "inline_data": {"mime_type": "image/jpeg", "data": JPEG_B64},
    }


def test_text_only_provider_is_told_the_screenshot_could_not_be_delivered():
    """Silent degradation is the defect class this repo is fighting. The
    model must learn that an image existed and did not arrive."""
    text, images = serialize_tool_result_with_images(
        "gui_computer_use__screenshot", gui_screenshot_envelope(),
        allow_images=False,
    )
    assert images == []                     # nothing goes on the wire
    parsed = json.loads(text)
    assert parsed["_image_omitted"] is True
    assert "NOT sent" in parsed["_image_note"]

    # And the materializer says it again on the row itself, in case the
    # provider flipped between capture and send (routing / failover).
    _t, real_images = serialize_tool_result_with_images(
        "gui_computer_use__screenshot", gui_screenshot_envelope(),
    )
    msgs = materialize_tool_result_images(
        tool_history(text), side_table(real_images), IMAGE_DELIVERY_NONE,
    )
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "tool"]
    row = json.loads(msgs[3]["content"])
    assert "could NOT be delivered" in row["_image_delivery"]
    assert JPEG_B64[:500] not in msgs[3]["content"]


def test_materialization_never_mutates_the_stored_history():
    """The transcript must stay provider-agnostic: it is replayed on the
    next turn, possibly after a failover to a different provider, and it
    is handed to the memory compactor."""
    text, images = serialize_tool_result_with_images(
        "gui_computer_use__screenshot", gui_screenshot_envelope(),
    )
    history = tool_history(text)
    before = json.dumps(history)
    for mode in (IMAGE_DELIVERY_ANTHROPIC_BLOCKS, IMAGE_DELIVERY_FOLLOWUP_USER,
                 IMAGE_DELIVERY_NONE):
        materialize_tool_result_images(history, side_table(images), mode)
    assert json.dumps(history) == before


def test_materialization_is_a_noop_without_a_side_table():
    history = tool_history("{}")
    assert materialize_tool_result_images(history, {}, IMAGE_DELIVERY_FOLLOWUP_USER) == history
    assert materialize_tool_result_images(history, None, IMAGE_DELIVERY_ANTHROPIC_BLOCKS) == history


# ── 4. batch pruning ─────────────────────────────────────────────────
#
# Source: Anthropic computer-use tool docs, "Manage screenshot history for
# prompt caching" — screenshots cost roughly 1000-1800 input tokens each;
# "Prune old screenshots in *batches*, not one each turn. Dropping a
# screenshot every turn changes the prefix every turn and invalidates the
# cache. A reasonable default is to keep the last three screenshots and
# prune every 25 turns."

def test_defaults_match_the_documented_recommendation():
    assert SCREENSHOT_KEEP_LAST_DEFAULT == 3
    assert SCREENSHOT_PRUNE_EVERY_DEFAULT == 25


def test_pruning_keeps_the_most_recent_n():
    table = {
        f"c{i}": {"images": [{"data_url": "data:image/png;base64,AAA"}],
                  "pruned": False, "tool_name": "t"}
        for i in range(10)
    }
    order = [f"c{i}" for i in range(10)]
    pruned = prune_tool_result_images(table, order, keep_last=3)
    assert pruned == [f"c{i}" for i in range(7)]
    survivors = [cid for cid in order if not table[cid]["pruned"]]
    assert survivors == ["c7", "c8", "c9"]
    for cid in pruned:
        assert table[cid]["images"] == []
        assert table[cid]["pruned"] is True


def test_pruning_is_idempotent_and_stops_at_keep_last():
    table = {f"c{i}": {"images": [{"data_url": "x"}], "pruned": False, "tool_name": "t"}
             for i in range(4)}
    order = [f"c{i}" for i in range(4)]
    assert prune_tool_result_images(table, order, keep_last=3) == ["c0"]
    assert prune_tool_result_images(table, order, keep_last=3) == []


def test_pruning_happens_in_batches_not_every_turn():
    """Pruning one image per turn changes the cached prefix every turn,
    which is worse than not pruning at all. Between batch boundaries the
    scheduler must say no."""
    every, keep = SCREENSHOT_PRUNE_EVERY_DEFAULT, SCREENSHOT_KEEP_LAST_DEFAULT
    fires = [
        r for r in range(1, 3 * every + 1)
        if should_prune_images(round_counter=r, live_images=keep + 1,
                               keep_last=keep, prune_every=every, hard_cap=100)
    ]
    assert fires == [every, 2 * every, 3 * every]


def test_no_prune_while_at_or_under_keep_last():
    for live in range(0, SCREENSHOT_KEEP_LAST_DEFAULT + 1):
        assert not should_prune_images(
            round_counter=SCREENSHOT_PRUNE_EVERY_DEFAULT, live_images=live,
        )


def test_hard_cap_overrides_the_batch_interval():
    """The batch interval bounds cache churn; the hard cap bounds context.
    At ~1800 tokens an image, an unbounded 25-round window is not safe."""
    assert should_prune_images(round_counter=1, live_images=100,
                               keep_last=3, prune_every=25, hard_cap=12)
    assert not should_prune_images(round_counter=1, live_images=5,
                                   keep_last=3, prune_every=25, hard_cap=12)


def test_a_pruned_screenshot_is_announced_not_silently_dropped():
    table = {"call_1": {"images": [], "pruned": True,
                        "tool_name": "gui_computer_use__screenshot"}}
    msgs = materialize_tool_result_images(
        tool_history('{"success": true}'), table, IMAGE_DELIVERY_ANTHROPIC_BLOCKS,
    )
    row = json.loads(msgs[3]["content"])
    assert "pruned from the" in row["_image_delivery"]
    assert "Re-run the capture tool" in row["_image_delivery"]


# ── 5. orchestrator wiring ───────────────────────────────────────────

class _FakeLLM:
    def __init__(self, provider: str, vision: bool = True):
        self.provider = provider
        self._vision = vision

    def _vision_support_status(self):
        return (self._vision, "" if self._vision else "text-only model")


def _bare_orchestrator(provider: str, vision: bool = True):
    """An Orchestrator with only the attributes this feature touches.

    Constructing a real one pulls in the memory store, the skill registry
    and the perception engine; none of that is part of what is under test.
    """
    from agents.orchestrator import Orchestrator
    orch = Orchestrator.__new__(Orchestrator)
    orch.llm = _FakeLLM(provider, vision)
    orch.skills = None
    orch._tool_result_images = {}
    orch._tool_image_order = {}
    orch._tool_image_rounds = {}
    return orch


def test_orchestrator_round_trip_on_a_vision_provider():
    orch = _bare_orchestrator("anthropic")
    content = orch._serialize_tool_result_for_history(
        "s1", "call_1", "gui_computer_use__screenshot", gui_screenshot_envelope(),
    )
    assert len(content) < 2_000
    assert json.loads(content)["_images_attached"] == 1
    assert orch._tool_image_order["s1"] == ["call_1"]

    msgs = orch._materialize_tool_images("s1", tool_history(content))
    _sys, anthropic_msgs = _convert_messages_for_anthropic(msgs)
    image_block = anthropic_msgs[-1]["content"][0]["content"][1]
    assert image_block["source"]["data"] == JPEG_B64


def test_orchestrator_round_trip_on_a_text_only_provider():
    orch = _bare_orchestrator("deepseek", vision=False)
    content = orch._serialize_tool_result_for_history(
        "s1", "call_1", "gui_computer_use__screenshot", gui_screenshot_envelope(),
    )
    assert orch._tool_result_images == {}          # nothing stashed
    parsed = json.loads(content)
    assert parsed["_image_omitted"] is True
    msgs = orch._materialize_tool_images("s1", tool_history(content))
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "tool"]


def test_orchestrator_batch_prune_over_a_long_agent_loop():
    """Drive the counter the way the tool loop does and assert the prune
    fires only on batch boundaries."""
    orch = _bare_orchestrator("anthropic")
    every = SCREENSHOT_PRUNE_EVERY_DEFAULT
    prunes = []
    for round_no in range(1, 2 * every + 1):
        orch._serialize_tool_result_for_history(
            "s1", f"call_{round_no}", "gui_computer_use__screenshot",
            gui_screenshot_envelope(),
        )
        orch._note_agent_round("s1")
        dropped = orch._maybe_prune_tool_images("s1")
        if dropped:
            prunes.append((round_no, dropped))
    # Hard cap (12) fires first, then the batch boundaries take over.
    assert prunes, "pruning never fired over 50 screenshots"
    live = [
        cid for cid, e in orch._tool_result_images["s1"].items()
        if not e["pruned"]
    ]
    assert len(live) <= SCREENSHOT_KEEP_LAST_DEFAULT + 1
    # The survivors are the newest ones.
    assert f"call_{2 * every}" in live


def test_orchestrator_forgets_images_with_the_session():
    orch = _bare_orchestrator("anthropic")
    orch._serialize_tool_result_for_history(
        "s1", "call_1", "gui_computer_use__screenshot", gui_screenshot_envelope(),
    )
    assert orch._tool_result_images["s1"]
    orch._forget_tool_images("s1")
    assert orch._tool_result_images == {}
    assert orch._tool_image_order == {}
    assert orch._tool_image_rounds == {}


def test_serializer_falls_back_to_text_when_the_image_layer_raises(monkeypatch):
    """A bug in the image path must degrade to the old behaviour, never
    take the turn down."""
    import skills.result_budget as rb

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(rb, "serialize_tool_result_with_images", _boom)
    orch = _bare_orchestrator("anthropic")
    # Re-bind the module-level import the orchestrator captured.
    import agents.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "serialize_tool_result_with_images", _boom)
    content = orch._serialize_tool_result_for_history(
        "s1", "call_1", "gui_computer_use__screenshot", gui_screenshot_envelope(),
    )
    assert json.loads(content)  # valid JSON, old path
    assert orch._tool_result_images == {}


# ── 6. budgets for the capture tools are still the standard tier ─────

def test_capture_endpoints_still_resolve_to_the_standard_text_budget():
    """The fix is NOT "give screenshots a bigger text budget". The text
    half stays on ``standard``; only the image leaves the budget."""
    from skills.registry import SkillRegistry
    reg = SkillRegistry()
    reg.load_builtin_skills()
    for skill_id, endpoint in (
        ("gui_computer_use", "screenshot"),
        ("screen_capture", "capture"),
        ("browser", "screenshot"),
    ):
        manifest = reg.skills.get(skill_id)
        assert budget_for(skill_id, endpoint, manifest).name == "standard"
