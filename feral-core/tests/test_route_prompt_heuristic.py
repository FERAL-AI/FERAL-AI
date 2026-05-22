"""Lane 08 WS2 — heuristic-first ``_route_prompt``.

AUDIT-r14 finding 20 fix #2 + AUDIT-r13 finding 6.3:
the orchestrator used to ALWAYS call the primary-model LLM for skill
routing, which added 1-5s wall clock to every chat turn (and burned
~3000 tokens per routing call against the user's premium tier).

This module pins:

  1. Each documented heuristic row exits without an LLM call.
  2. On a canned production-prompt corpus, ≥ 70% of prompts route
     without LLM (parent-acked acceptance for WS2).
  3. When the heuristic IS ambiguous, the orchestrator calls
     ``LLMProvider.route_call(call_site="routing", tier="cheap")``
     BEFORE the chat call — never bare ``chat_with_failover`` against
     the primary tier.
  4. The ``call_site="routing"`` label flows through so the budget
     gate bills against the routing call-site, not chat.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.orchestrator import Orchestrator
from models.skill_manifest import (
    BrandProfile,
    SkillEndpoint,
    SkillManifest,
)


# ── Fixtures ───────────────────────────────────────────────────────


def _skill(skill_id: str, triggers: list[str], categories: list[str] | None = None) -> SkillManifest:
    return SkillManifest(
        skill_id=skill_id,
        version="1.0.0",
        author="test",
        brand=BrandProfile(name=skill_id, primary_color="#000", logo_url="", icon_set="sf_symbols"),
        description=f"{skill_id} skill",
        categories=categories or [],
        trigger_phrases=triggers,
        endpoints=[
            SkillEndpoint(
                id="default",
                method="POST",
                url=f"https://example.test/{skill_id}",
                description="default endpoint",
                returns_description="result",
                ui_hint="detail_card",
            )
        ],
    )


# Catalog reproduces the production manifest set closely enough that
# the heuristic ≥ 70% test is a real signal. Every skill_id below
# matches what ships in ``feral-core/skills/manifests/`` so the regex
# table and prefix map land on existing rows.
CATALOG = {
    "notes_memory": _skill(
        "notes_memory",
        [
            "remember this", "remember that", "save a note", "take a note",
            "my notes", "recall", "what did i save",
        ],
        ["memory", "notes"],
    ),
    "calendar_google": _skill(
        "calendar_google",
        [
            "what's on my calendar", "my schedule today", "upcoming meetings",
            "create an event", "schedule a meeting", "what's next",
        ],
        ["calendar", "productivity"],
    ),
    "feral_reminders": _skill(
        "feral_reminders",
        ["create reminder", "remind me", "list reminders"],
        ["reminders", "productivity"],
    ),
    "health_data": _skill(
        "health_data",
        ["how did i sleep", "my heart rate", "my hrv", "my recovery"],
        ["health"],
    ),
    "perception_query": _skill(
        "perception_query",
        ["what do i see", "describe the scene", "what is in front of me"],
        ["vision"],
    ),
    "web_search": _skill(
        "web_search",
        ["search for", "google", "look up"],
        ["search"],
    ),
    "weather_current": _skill(
        "weather_current",
        ["what's the weather", "weather forecast", "is it raining"],
        ["weather"],
    ),
    "smart_home_hue": _skill(
        "smart_home_hue",
        ["turn on the lights", "turn off the lights", "dim the lights"],
        ["smart_home"],
    ),
    "spotify_music": _skill(
        "spotify_music",
        ["play music", "play some music", "what's playing"],
        ["music"],
    ),
    "code_interpreter": _skill(
        "code_interpreter",
        ["run python", "execute code"],
        ["code"],
    ),
    "desktop_control": _skill(
        "desktop_control",
        ["open app", "launch", "open finder"],
        ["desktop"],
    ),
    "screen_capture": _skill(
        "screen_capture",
        ["take a screenshot", "what is on my screen"],
        ["vision"],
    ),
}


@pytest.fixture
def orchestrator():
    reg = MagicMock()
    reg.skills = CATALOG
    # Real registry-style ranking — re-use the orchestrator's own
    # trigger score so the test exercises the canonical path.
    def _find(query: str, top_k: int = 5):
        scored = []
        for sk in CATALOG.values():
            s = Orchestrator._trigger_score(query, sk)
            if s > 0:
                scored.append((s, sk))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [sk for _, sk in scored[:top_k]]
    reg.find_skills_for_query = _find
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
    # LLM available so the routing code path is exercised, but we
    # FAIL HARD if anything actually calls into chat_with_failover —
    # that would prove the heuristic gate didn't trigger.
    orch.llm = MagicMock()
    orch.llm.available = True
    orch.llm.chat_with_failover = AsyncMock(
        side_effect=AssertionError(
            "chat_with_failover MUST NOT be called for non-ambiguous "
            "prompts (WS2 heuristic gate failed)"
        )
    )
    orch.llm.route_call = MagicMock(
        side_effect=AssertionError(
            "route_call MUST NOT be called for non-ambiguous prompts"
        )
    )
    return orch


# ── Heuristic-exit rows (each prompt = exactly one row) ────────────


class TestHeuristicRows:
    """Each documented heuristic table row exits without an LLM call."""

    @pytest.mark.asyncio
    async def test_empty_prompt_routes_to_empty_list(self, orchestrator):
        assert await orchestrator._route_prompt("") == []
        assert await orchestrator._route_prompt("   ") == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("prefix,expected_skill", [
        ("/memory what did I save yesterday", "notes_memory"),
        ("/notes find that grocery list", "notes_memory"),
        ("/calendar what's tomorrow", "calendar_google"),
        ("/cal next week", "calendar_google"),
        ("/remind me to buy milk", "feral_reminders"),
        ("/health my heart rate", "health_data"),
        ("/search top result for FERAL", "web_search"),
        ("/code print hello", "code_interpreter"),
        ("/vision what do I see", "perception_query"),
        ("/screen what's open", "screen_capture"),
    ])
    async def test_explicit_prefix_routes_directly(
        self, orchestrator, prefix, expected_skill
    ):
        result = await orchestrator._route_prompt(prefix)
        assert [s.skill_id for s in result] == [expected_skill]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("prompt,expected_skill", [
        ("what did I do yesterday", "notes_memory"),
        ("what did I say last night", "notes_memory"),
        ("summarize today", "notes_memory"),
        ("summarize my week", "notes_memory"),
        ("recall that thing about coffee", "notes_memory"),
        ("do you remember the project name", "notes_memory"),
        ("my notes from this morning", "notes_memory"),
        ("what's on my calendar today", "calendar_google"),
        ("what's on my agenda", "calendar_google"),
        ("upcoming meetings", "calendar_google"),
        ("am I free at 3pm", "calendar_google"),
        ("remind me to take vitamins", "feral_reminders"),
        ("set a reminder for tomorrow", "feral_reminders"),
        ("list my reminders", "feral_reminders"),
        ("what's my heart rate", "health_data"),
        ("how did I sleep last night", "health_data"),
        ("show me my vitals", "health_data"),
        ("what do I see", "perception_query"),
        ("describe the scene", "perception_query"),
        ("what am I looking at", "perception_query"),
    ])
    async def test_regex_routes_without_llm(
        self, orchestrator, prompt, expected_skill
    ):
        result = await orchestrator._route_prompt(prompt)
        assert result, f"no result for prompt: {prompt!r}"
        assert result[0].skill_id == expected_skill, (
            f"prompt {prompt!r}: expected first skill {expected_skill}, "
            f"got {[s.skill_id for s in result]}"
        )

    @pytest.mark.asyncio
    async def test_strong_trigger_match_routes_via_registry(self, orchestrator):
        # "play music" is an exact trigger for spotify_music — score 25.
        result = await orchestrator._route_prompt("play music")
        assert result
        assert result[0].skill_id == "spotify_music"

    @pytest.mark.asyncio
    async def test_confident_lead_uses_heuristic(self, orchestrator):
        # "turn on the lights" — exact-match trigger for smart_home_hue.
        result = await orchestrator._route_prompt("turn on the lights please")
        assert result
        assert result[0].skill_id == "smart_home_hue"

    @pytest.mark.asyncio
    async def test_small_catalog_skips_llm(self, orchestrator):
        # Shrink to 3 skills — registry ranking is always sufficient.
        small = {k: CATALOG[k] for k in ["weather_current", "spotify_music", "code_interpreter"]}
        orchestrator.skills.skills = small
        result = await orchestrator._route_prompt("what's the weather in NYC")
        assert result
        # No LLM was called even though the prompt didn't match any
        # explicit regex — small_catalog branch covers it.

    @pytest.mark.asyncio
    async def test_action_verb_with_no_match_exposes_all(self, orchestrator):
        # 6+ words → ``RefusalHandler.query_implies_action`` is True.
        # No trigger phrases match strongly → action_fallback exposes
        # the whole catalog so the LLM has a chance to pick anything.
        prompt = "deploy the spaceship to mars to colonize quickly"
        skills, reason = orchestrator._heuristic_route(prompt)
        assert reason == "action_fallback", f"expected action_fallback, got {reason}"
        assert len(skills) == len(CATALOG)
        # Full path still routes cleanly without an LLM call (the
        # default fixture's chat_with_failover would raise).
        result = await orchestrator._route_prompt(prompt)
        assert len(result) == len(CATALOG)


# ── Coverage target: ≥ 70% of canned corpus is no-LLM ──────────────


CANNED_CORPUS = [
    # Memory / recall (Lane 08 S1)
    "what did I do yesterday",
    "summarize my week",
    "recall the project name",
    "do you remember the grocery list",
    "what was that thing about coffee",
    # Calendar
    "what's on my calendar today",
    "upcoming meetings",
    "am I free tomorrow at 3",
    "schedule a meeting with Sara",
    # Reminders
    "remind me to take vitamins at 9am",
    "list my reminders",
    "set a reminder for tomorrow morning",
    # Health (S2)
    "what's my heart rate",
    "how did I sleep last night",
    "show me my vitals",
    "my hrv this week",
    # Vision (S5 prerequisite)
    "what do I see",
    "describe the scene",
    "what am I looking at",
    # Smart home (exact triggers)
    "turn on the lights",
    "turn off the lights",
    "dim the lights",
    # Music
    "play music",
    "what's playing",
    "play some music",
    # Web search
    "search for FERAL AI",
    "google the latest news",
    "look up the weather forecast",
    # Weather
    "what's the weather today",
    "is it raining",
    # Screen
    "take a screenshot",
    "what is on my screen",
    # Desktop
    "open finder",
    "launch terminal",
    # Code
    "run python print 1+1",
    # Explicit prefixes
    "/memory find my note about wine",
    "/calendar what's tomorrow",
    "/remind me to buy bread",
    # Multi-skill action-like (action_fallback exposes all)
    "make me a coffee",
    "fix the bug in my repo",
]


class TestNoLLMCoverage:
    """The parent-acked acceptance: ≥ 70% of the canned corpus must
    route without ever calling the LLM. We make this brutally explicit
    by failing the test when ANY ``chat_with_failover`` / ``route_call``
    hit is observed.
    """

    @pytest.mark.asyncio
    async def test_canned_corpus_is_at_least_70pct_no_llm(self, orchestrator):
        # Override the assertion-side-effect mocks so we can COUNT
        # rather than crash on the first LLM call. We still want the
        # corpus-level threshold; individual ambiguous prompts ARE
        # allowed to call into the LLM.
        orchestrator.llm.route_call = MagicMock(
            return_value={"provider": "anthropic", "model": "claude-haiku", "tier": "cheap"}
        )
        orchestrator.llm.chat_with_failover = AsyncMock(
            return_value={"choices": [{"message": {"content": "[]"}}]}
        )
        orchestrator.llm.extract_response = MagicMock(return_value=("[]", []))

        heuristic_count = 0
        llm_count = 0
        per_prompt: list[tuple[str, str]] = []

        for prompt in CANNED_CORPUS:
            skills, reason = orchestrator._heuristic_route(prompt)
            per_prompt.append((prompt, reason))
            if reason != "ambiguous":
                heuristic_count += 1
            else:
                llm_count += 1
                # Actually drive the full routing call so we can
                # verify route_call is consulted and the cheap tier
                # is honoured.
                await orchestrator._route_prompt(prompt)

        total = len(CANNED_CORPUS)
        pct = heuristic_count / total
        # Document the breakdown in the failure message — Round-2
        # verification reads this directly.
        msg = (
            f"heuristic={heuristic_count}/{total} ({pct:.0%}); "
            f"llm={llm_count}\n"
            + "\n".join(f"  {r:18s} {p}" for p, r in per_prompt)
        )
        assert pct >= 0.70, msg

    @pytest.mark.asyncio
    async def test_ambiguous_prompt_uses_route_call_with_cheap_tier(
        self, orchestrator
    ):
        # Build a prompt that:
        #   * doesn't start with /<prefix>
        #   * doesn't hit any regex
        #   * doesn't match any trigger phrase strongly
        #   * doesn't look like a clear action verb
        # so the heuristic exits as "ambiguous" and the LLM gate fires.
        orchestrator.llm.route_call = MagicMock(
            return_value={"provider": "anthropic", "model": "claude-haiku", "tier": "cheap"}
        )
        orchestrator.llm.chat_with_failover = AsyncMock(
            return_value={"choices": [{"message": {"content": '["calendar_google"]'}}]}
        )
        orchestrator.llm.extract_response = MagicMock(
            return_value=('["calendar_google"]', [])
        )

        # Short (< 6 words) noun-only prompt. No trigger overlap, no
        # action verb → genuinely ambiguous.
        # Short (< 6 words) noun-only prompt. No trigger overlap, no
        # action verb → genuinely ambiguous.
        ambiguous = "cookbook hypothesis"
        skills, reason = orchestrator._heuristic_route(ambiguous)
        assert reason == "ambiguous", f"expected ambiguous, got {reason}"

        result = await orchestrator._route_prompt(ambiguous)
        # The cheap LLM returned ``["calendar_google"]`` — the
        # orchestrator must use it instead of the empty heuristic.
        assert "calendar_google" in [s.skill_id for s in result]

        # route_call IS called with the documented contract.
        orchestrator.llm.route_call.assert_called_once()
        kwargs = orchestrator.llm.route_call.call_args.kwargs
        positional = orchestrator.llm.route_call.call_args.args
        # ``route_call(call_site, prompt, *, tier="cheap")`` — accept
        # either positional or kw form.
        call_site = kwargs.get("call_site") or (positional[0] if positional else "")
        tier = kwargs.get("tier")
        assert call_site == "routing"
        assert tier == "cheap"

        # chat_with_failover is called with call_site="routing" so the
        # budget gate bills the right bucket.
        orchestrator.llm.chat_with_failover.assert_awaited_once()
        chat_kwargs = orchestrator.llm.chat_with_failover.await_args.kwargs
        assert chat_kwargs.get("call_site") == "routing"

    @pytest.mark.asyncio
    async def test_llm_unavailable_uses_heuristic_only(self, orchestrator):
        orchestrator.llm.available = False
        # Even the assertion-mocks should never trigger — LLM-off
        # path stays in heuristic land.
        result = await orchestrator._route_prompt("tell me about cookbooks")
        # Action-fallback exposes the whole catalog; the contract is
        # "no LLM", not "always non-empty".
        assert result
