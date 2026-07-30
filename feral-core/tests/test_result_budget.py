"""v2026.7.30 — per-tool result budgets.

Before this change every tool result was cut to 2 000 chars and 20 list
items, TWICE, before the model saw it:

* ``SkillExecutor._sanitize_response`` clamped every string/list on every
  lane with one global constant, and
* every conversation-history append did ``json.dumps(result)[:2000]`` — a
  blind byte slice that frequently produced invalid JSON and never said
  anything had been removed.

So ``coding_tools__read_file`` returned ~30 lines of source regardless of
``limit``, ``grep_search`` computed 250 matches and returned 20, and
``bash`` computed 50 000 chars and returned 4% of them.

These tests pin both halves of the fix: first-party local read/search/shell
results now survive intact, third-party HTTP results are still bounded, and
what does get cut is announced with a pagination hint.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from models.skill_manifest import (  # noqa: E402
    BrandProfile,
    SkillEndpoint,
    SkillManifest,
)
from skills.impl import coding_tools  # noqa: E402
from skills.result_budget import (  # noqa: E402
    BUILTIN_BUDGET_TIERS,
    DEFAULT_TIER,
    TruncationReport,
    budget_for,
    budget_for_tool,
    builtin_skill_ids,
    clamp,
    get_budget,
    reset_budget_cache,
    serialize_tool_result,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("FERAL_RESULT_BUDGET_TIER", raising=False)
    reset_budget_cache()
    yield
    reset_budget_cache()


def _registry():
    """Real manifests, loaded the way the brain loads them."""
    from skills.registry import SkillRegistry
    reg = SkillRegistry()
    manifests = ROOT / "skills" / "manifests"
    reg.load_from_directory(manifests)
    return reg


# ── the budget is a property of the tool, not a global constant ──────────


def test_default_tier_is_unchanged_for_third_party_skills():
    """The tight bound is the ORIGINAL, legitimate reason the sanitizer
    exists (prompt injection / context flooding via an API you do not
    control). Raising budgets for first-party read tools must not relax
    it."""
    std = get_budget(DEFAULT_TIER)
    assert std.max_str_len == 2_000
    assert std.max_list_len == 20
    assert std.max_result_chars == 2_000


def test_workspace_tier_covers_what_the_tools_already_compute():
    """Binds the declared budget to the tools' own limits so raising
    MAX_OUTPUT / GREP_DEFAULT_HEAD_LIMIT without raising the budget fails
    CI instead of silently re-introducing the bug."""
    ws = BUILTIN_BUDGET_TIERS["workspace"]
    assert ws.max_str_len >= coding_tools.MAX_OUTPUT
    assert ws.max_result_chars >= 2 * coding_tools.MAX_OUTPUT
    assert ws.max_list_len >= coding_tools.GREP_DEFAULT_HEAD_LIMIT
    assert ws.max_list_len >= coding_tools.GLOB_DEFAULT_HEAD_LIMIT


def test_coding_tools_declares_workspace_but_web_fetch_stays_standard():
    """Per-ENDPOINT resolution matters: the same first-party skill reads
    the operator's disk on one endpoint and relays a stranger's HTML on
    another."""
    reg = _registry()
    assert budget_for_tool("coding_tools__read_file", reg).name == "workspace"
    assert budget_for_tool("coding_tools__grep_search", reg).name == "workspace"
    assert budget_for_tool("coding_tools__bash", reg).name == "workspace"
    assert budget_for_tool("coding_tools__web_fetch", reg).name == "standard"


def test_third_party_http_skills_get_the_default_tier():
    reg = _registry()
    for tool in ("notion__read_page", "github_api__list_issues",
                 "web_search__web_search", "spotify_music__search"):
        assert budget_for_tool(tool, reg).name == DEFAULT_TIER


def test_email_feed_endpoints_declare_the_feed_tier():
    """Replaces the hardcoded ``_email_sanitize_list_limit`` exemption that
    used to live in the executor."""
    reg = _registry()
    assert budget_for_tool("email__search", reg).name == "feed"
    assert budget_for_tool("email__list_inbox", reg).name == "feed"
    assert budget_for_tool("email__read_email", reg).name == "feed"
    assert budget_for_tool("email__send_email", reg).name == DEFAULT_TIER


def test_unknown_tool_names_fall_back_to_the_default_tier():
    assert budget_for_tool("mcp_something", None).name == DEFAULT_TIER
    assert budget_for_tool("", None).name == DEFAULT_TIER
    assert budget_for_tool("daemon_robot__drive", _registry()).name == DEFAULT_TIER


# ── trust clamp ──────────────────────────────────────────────────────────


def test_runtime_installed_manifest_cannot_widen_its_own_budget():
    """A marketplace skill is untrusted input. If a declared tier were
    honoured unconditionally, any installed skill could declare
    ``workspace`` and flood the context with attacker-chosen text."""
    hostile = SkillManifest(
        skill_id="totally_not_evil",
        brand=BrandProfile(name="Evil"),
        description="x",
        result_budget="workspace",
        endpoints=[SkillEndpoint(
            id="fetch", method="GET", url="https://evil.example",
            description="x", result_budget="workspace",
        )],
    )
    assert "totally_not_evil" not in builtin_skill_ids()
    assert budget_for("totally_not_evil", "fetch", hostile).name == DEFAULT_TIER


def test_builtin_skill_ids_are_derived_from_the_shipped_manifests():
    ids = builtin_skill_ids()
    assert "coding_tools" in ids
    assert "email" in ids
    assert len(ids) > 20


# ── clamp keeps the safety property ──────────────────────────────────────


def test_clamp_still_bounds_depth_width_and_length():
    tiny = get_budget(DEFAULT_TIER)
    deep = {"a": {"b": {"c": {"d": {"e": {"f": {"g": "bottom"}}}}}}}
    assert "truncated" in json.dumps(clamp(deep, tiny))

    wide = {str(i): i for i in range(500)}
    assert len(clamp(wide, tiny)) == tiny.max_dict_keys

    long_list = list(range(500))
    assert len(clamp(long_list, tiny)) == tiny.max_list_len

    assert len(clamp("x" * 50_000, tiny)) < 50_000


def test_clamp_reports_and_marks_what_it_removed():
    report = TruncationReport()
    out = clamp({"body": "x" * 10_000, "rows": list(range(100))},
                get_budget(DEFAULT_TIER), report)
    assert report.chars_dropped == 8_000
    assert report.items_dropped == 80
    assert "truncated" in out["body"]
    assert "offset/limit" in out["body"]
    assert "8000" in report.summary()


def test_env_pin_forces_every_tool_back_to_standard(monkeypatch):
    reg = _registry()
    monkeypatch.setenv("FERAL_RESULT_BUDGET_TIER", "standard")
    reset_budget_cache()
    assert budget_for_tool("coding_tools__read_file", reg).name == "standard"


# ── serialize_tool_result: the four ex-[:2000] sites ─────────────────────


def _read_file_result(lines: int) -> dict:
    """Shape emitted by coding_tools._read_file."""
    content = "\n".join(f"{i:>6}|    some_function_call(argument_{i}, other={i})"
                        for i in range(1, lines + 1))
    return {
        "success": True,
        "status_code": 200,
        "data": {"path": "/repo/big.py", "content": content, "total_lines": lines},
        "error": None,
    }


def _through_both_layers(tool_name: str, data: dict, reg) -> str:
    """Both truncation layers, in the order the real pipeline applies them:
    the executor's structural clamp, then history serialization. Testing
    only the second one overstates how much survives."""
    from skills.executor import SkillExecutor
    skill_id, _, endpoint_id = tool_name.partition("__")
    budget = budget_for(skill_id, endpoint_id, reg.skills.get(skill_id))
    payload, note = SkillExecutor._sanitize_with_note(data, budget)
    envelope = {"success": True, "status_code": 200, "data": payload, "error": None}
    if note:
        envelope["_truncation_note"] = note
    return serialize_tool_result(tool_name, envelope, registry=reg)


def test_large_read_file_result_reaches_the_model_intact():
    """The headline regression. 800 lines of source is ~43 KB; the old
    pipeline delivered 2 000 chars of it — about 30 lines — no matter what
    ``limit`` the model asked for."""
    reg = _registry()
    result = _read_file_result(800)
    assert len(result["data"]["content"]) > 40_000  # the tool computed this much

    blob = _through_both_layers("coding_tools__read_file", result["data"], reg)
    parsed = json.loads(blob)  # never invalid JSON
    assert parsed.get("_truncated") is not True
    assert "_truncation_note" not in parsed
    # Every line survives, including the last.
    assert "some_function_call(argument_800" in parsed["data"]["content"]
    assert parsed["data"]["content"].count("\n") == 799
    assert len(blob) > 40_000


def test_read_beyond_the_budget_is_cut_but_says_so_with_a_hint():
    """Budgets are raised, not removed. A read that overshoots is still
    bounded — but the model is told, instead of silently receiving a
    prefix and assuming it read the whole file."""
    reg = _registry()
    ws = BUILTIN_BUDGET_TIERS["workspace"]
    result = _read_file_result(4_000)
    assert len(result["data"]["content"]) > ws.max_str_len

    parsed = json.loads(
        _through_both_layers("coding_tools__read_file", result["data"], reg)
    )
    content = parsed["data"]["content"]
    # ~30x more than the 2 000 chars the old pipeline delivered.
    assert len(content) > 20 * 2_000
    assert "truncated" in content
    assert "offset/limit" in content
    assert "offset/limit" in parsed["_truncation_note"]


def test_large_grep_result_keeps_every_match_the_tool_computed():
    reg = _registry()
    matches = [
        {"file": f"src/module_{i}.py", "line": str(i), "text": f"    match {i}"}
        for i in range(coding_tools.GREP_DEFAULT_HEAD_LIMIT)
    ]
    parsed = json.loads(_through_both_layers(
        "coding_tools__grep_search",
        {"mode": "content", "matches": matches, "total": len(matches)},
        reg,
    ))
    # Was 20 of the 250 the tool had already computed.
    assert len(parsed["data"]["matches"]) == coding_tools.GREP_DEFAULT_HEAD_LIMIT
    assert parsed["data"]["matches"][-1]["file"] == "src/module_249.py"
    assert "_truncation_note" not in parsed


def test_large_bash_output_reaches_the_model():
    reg = _registry()
    stdout = "\n".join(f"test_case_{i} PASSED" for i in range(2_000))
    assert len(stdout) < coding_tools.MAX_OUTPUT  # within what bash itself allows
    parsed = json.loads(_through_both_layers(
        "coding_tools__bash",
        {"stdout": stdout, "stderr": "", "exit_code": 0},
        reg,
    ))
    # The last test case survives — previously ~96% of bash output was
    # discarded before the model saw it.
    assert "test_case_1999 PASSED" in parsed["data"]["stdout"]
    assert len(parsed["data"]["stdout"]) == len(stdout)


def test_third_party_http_result_is_still_bounded():
    """The safety property. A vendor API that returns a megabyte of text
    must not be able to spend the whole context window."""
    reg = _registry()
    result = {
        "success": True, "status_code": 200,
        "data": {"page": "INJECTED. " * 100_000},
        "error": None,
    }
    blob = serialize_tool_result("notion__read_page", result, registry=reg)
    assert len(blob) <= get_budget(DEFAULT_TIER).max_result_chars
    parsed = json.loads(blob)
    assert parsed["_truncated"] is True


def test_truncated_output_is_always_valid_json():
    """The old ``json.dumps(...)[:2000]`` cut mid-token, so the model was
    routinely handed JSON that does not parse."""
    reg = _registry()
    for payload in (
        {"data": {"content": "é" * 40_000}},
        {"data": [{"k": "v" * 5_000} for _ in range(200)]},
        {"data": {str(i): "y" * 900 for i in range(400)}},
        {"data": {"nested": {"deep": ["z" * 30_000] * 40}}},
    ):
        blob = serialize_tool_result("notion__read_page", payload, registry=reg)
        json.loads(blob)  # raises if the old byte-slicing behaviour returns
        assert len(blob) <= get_budget(DEFAULT_TIER).max_result_chars


def test_truncation_carries_the_pagination_hint_the_tool_produced():
    """coding_tools already emits truncated / pagination{next_offset} /
    total precisely so the caller can page. Those breadcrumbs are useless
    if the layer above drops them."""
    reg = _registry()
    result = {
        "success": True, "status_code": 200,
        "data": {
            "mode": "content",
            "matches": [{"file": f"f{i}.py", "text": "q" * 4_000} for i in range(60)],
            "total": 900,
            "truncated": True,
            "pagination": {"limit": 250, "offset": 0, "next_offset": 250},
        },
        "error": None,
    }
    parsed = json.loads(
        serialize_tool_result("notion__query_database", result, registry=reg)
    )
    assert parsed["_truncated"] is True
    assert "offset/limit" in parsed["_truncation_note"]
    assert parsed["_pagination_hint"]["pagination"]["next_offset"] == 250
    assert parsed["_pagination_hint"]["total"] == 900


def test_small_results_are_passed_through_untouched():
    reg = _registry()
    result = {"success": True, "data": {"ok": True}, "error": None}
    blob = serialize_tool_result("web_search__web_search", result, registry=reg)
    assert json.loads(blob) == result
    assert "_truncated" not in blob


def test_pathological_payload_still_lands_inside_the_budget():
    """Tens of thousands of tiny keys cannot be shrunk by clamping string
    length; the last-resort preview envelope must still be valid JSON and
    still fit."""
    reg = _registry()
    result = {"data": {f"key_{i}": i for i in range(200_000)}}
    blob = serialize_tool_result("notion__read_page", result, registry=reg)
    parsed = json.loads(blob)
    assert parsed["_truncated"] is True
    assert len(blob) <= get_budget(DEFAULT_TIER).max_result_chars * 2


# ── executor lane wiring ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_executor_applies_the_declared_budget_to_python_skills():
    """End-to-end through SkillExecutor: the layer that used to clamp every
    Python-backed skill result to 2 000 chars."""
    from skills.executor import SkillExecutor
    from skills.impl import SKILL_IMPLEMENTATIONS

    reg = _registry()
    skill = reg.skills["coding_tools"]
    endpoint = next(e for e in skill.endpoints if e.id == "read_file")

    class _FakeImpl:
        skill_id = "coding_tools"

        async def execute(self, endpoint_id, args, vault):
            return {"success": True, "status_code": 200,
                    "data": {"content": "L" * 40_000, "total_lines": 900}}

    previous = SKILL_IMPLEMENTATIONS.get("coding_tools")
    SKILL_IMPLEMENTATIONS["coding_tools"] = _FakeImpl()
    try:
        ex = SkillExecutor()
        out = await ex.execute("coding_tools__read_file", {}, skill, endpoint)
        await ex.close()
    finally:
        if previous is None:
            SKILL_IMPLEMENTATIONS.pop("coding_tools", None)
        else:
            SKILL_IMPLEMENTATIONS["coding_tools"] = previous

    assert len(out["data"]["content"]) == 40_000
    assert "_truncation_note" not in out


@pytest.mark.asyncio
async def test_executor_still_clamps_an_undeclared_skill_and_says_so():
    from skills.executor import SkillExecutor
    from skills.impl import SKILL_IMPLEMENTATIONS

    reg = _registry()
    skill = reg.skills["notion"]
    endpoint = next(e for e in skill.endpoints if e.id == "read_page")

    class _FakeImpl:
        skill_id = "notion"

        async def execute(self, endpoint_id, args, vault):
            return {"success": True, "status_code": 200,
                    "data": {"page": "N" * 40_000}}

    previous = SKILL_IMPLEMENTATIONS.get("notion")
    SKILL_IMPLEMENTATIONS["notion"] = _FakeImpl()
    try:
        ex = SkillExecutor()
        out = await ex.execute("notion__read_page", {}, skill, endpoint)
        await ex.close()
    finally:
        if previous is None:
            SKILL_IMPLEMENTATIONS.pop("notion", None)
        else:
            SKILL_IMPLEMENTATIONS["notion"] = previous

    assert len(out["data"]["page"]) < 3_000
    assert "truncated" in out["_truncation_note"]
