"""Pinning tests for the agent + orchestrator prompt deepening pass.

Live testing showed the LLM answering "what did I do yesterday?" from its
own context WITHOUT calling `notes_memory__fused_timeline` — purely a
prompt-quality problem.

These tests pin the load-bearing directives so a future refactor that
trims the master system prompt or any worker prompt can't silently
regress to the shallow-prompt era. They do NOT try to unit-test LLM
output quality (that's eval territory) — they only assert that the
strings the model is supposed to read are actually present.
"""

from __future__ import annotations

import pytest

from agents.identity_loader import IdentityLoader


class _Frame:
    """Minimal PerceptionFrame stand-in."""

    connected_nodes: list = []

    def to_system_context(self) -> str:
        return "No sensor data available."


async def _build_master_prompt() -> str:
    loader = IdentityLoader(memory=None, somatic_engine=None, calendar=None)
    return await loader.build_system_prompt(
        _Frame(),
        [],
        session_id="prompt-pin-test",
        identity_text="",
        full_catalog=[],
    )


# ─────────────────────────────────────────────────────────────────────────
# Master orchestrator system prompt — `IdentityLoader.build_system_prompt`
# ─────────────────────────────────────────────────────────────────────────


async def test_master_prompt_has_tool_selection_discipline_section() -> None:
    """The Tool-Selection Discipline section must exist and be at the top.

    Anthropic-class models honour authority signals at the start of the
    prompt; budget models honour the last instruction. We ship both, but
    this test asserts the upfront block is present.
    """
    prompt = await _build_master_prompt()
    assert "## Tool-Selection Discipline" in prompt
    # The section should land before the dynamic context blocks.
    discipline_idx = prompt.find("## Tool-Selection Discipline")
    memory_idx = prompt.find("## Memory")
    if memory_idx >= 0:
        assert discipline_idx < memory_idx, (
            "Tool-Selection Discipline must come BEFORE the Memory block "
            "so the model reads it before being tempted to answer from "
            "the working-set hint."
        )


async def test_master_prompt_pins_fused_timeline_directive() -> None:
    """The temporal-recall directive must name the tool explicitly.

    The single-line concrete gap from the parent task: live testing
    showed the LLM answering 'what did I do yesterday?' from context
    without calling notes_memory__fused_timeline. The prompt must
    mention the tool by qualified name and tell the model when to use
    it; otherwise we're back to fragile keyword routing.
    """
    prompt = await _build_master_prompt()
    assert "notes_memory__fused_timeline" in prompt, (
        "Master prompt must name notes_memory__fused_timeline so the "
        "model knows which tool to call for personal-recall questions."
    )
    # The directive language should make the routing explicit, not
    # bury it as one bullet among many.
    lowered = prompt.lower()
    assert "what did i" in lowered, (
        "Master prompt should include the canonical 'what did I…' "
        "phrasing as a trigger pattern."
    )
    assert "summarize my" in lowered or "summarise my" in lowered, (
        "Master prompt should include the canonical 'summarize my…' "
        "phrasing as a trigger pattern."
    )


async def test_master_prompt_warns_against_memory_block_alone() -> None:
    """The `## Memory` working-set block is lossy.

    The deepening pass added an explicit warning that answering from
    the Memory block alone fabricates specifics. Pin that — it's the
    sentence that disambiguates 'rich context' from 'tool-grounded'.
    """
    prompt = await _build_master_prompt()
    assert "Memory` block" in prompt or "Memory block" in prompt, (
        "Master prompt should reference the `## Memory` block by name "
        "so the model knows what it is."
    )
    lowered = prompt.lower()
    assert "lossy" in lowered or "working-set hint" in lowered or "working set hint" in lowered, (
        "Master prompt should describe the Memory block as a hint, not "
        "the source of truth."
    )


async def test_master_prompt_has_grounded_synthesis_section() -> None:
    """Pin the Grounded Memory Synthesis directive.

    Even after the model calls a tool, it can still hallucinate by
    cherry-picking. The synthesis section tells it to cite specifics
    from the actual returned entries.
    """
    prompt = await _build_master_prompt()
    assert "## Grounded Memory Synthesis" in prompt
    lowered = prompt.lower()
    # The "say nothing if data is empty" anchor is the load-bearing one.
    assert "no entries in" in lowered, (
        "Grounded synthesis must tell the model to say 'no entries in "
        "<window>' when a tool returns nothing — not fabricate."
    )


async def test_master_prompt_has_agentic_planning_section() -> None:
    """Pin the agentic planning directive."""
    prompt = await _build_master_prompt()
    assert "## Agentic Planning" in prompt
    lowered = prompt.lower()
    assert "decompose" in lowered, (
        "Agentic planning must tell the model to decompose multi-step "
        "tasks before acting."
    )
    assert "do not produce a plan-only answer" in lowered or "plan-only" in lowered, (
        "Agentic planning must tell the model not to narrate the plan "
        "instead of executing it (planning_only_retry mirrors this)."
    )


async def test_master_prompt_local_first_honesty_anchor() -> None:
    """Pin the local-first / no-overclaiming language."""
    prompt = await _build_master_prompt()
    lowered = prompt.lower()
    assert "local-first" in lowered or "sovereign" in lowered, (
        "Master prompt must declare local-first / sovereign posture so "
        "the model doesn't overclaim cloud-side actions."
    )
    assert "don't overclaim" in lowered or "do not overclaim" in lowered, (
        "Master prompt should explicitly tell the model not to overclaim."
    )


async def test_master_prompt_keeps_canonical_computer_use_paths() -> None:
    """Regression guard for `test_identity_loader_prompt_canonical.py`.

    The deepening pass restructured the static header. The canonical
    file-write directive must still resolve to computer_use__write_file
    and computer_use__bash, NOT to shell `echo` / `python3 -c` recipes.
    """
    prompt = await _build_master_prompt()
    assert "computer_use__write_file" in prompt
    assert "computer_use__bash" in prompt
    assert "permission_needed" in prompt
    assert "desktop_control__shell_command" not in prompt
    # The deepening pass spells out python3 -c as a forbidden pattern,
    # so the literal string appears once in negative-example form.
    # Ensure it's only the negative-example occurrence (i.e., we never
    # tell the model to USE python3 -c to write files).
    for line in prompt.splitlines():
        if "python3 -c" in line:
            lowered = line.lower()
            assert "don't" in lowered or "do not" in lowered or "forbidden" in lowered, (
                f"Unexpected non-negative use of 'python3 -c' in master "
                f"prompt: {line!r}"
            )


# ─────────────────────────────────────────────────────────────────────────
# Worker prompts — `agents/workers/*.py`
# ─────────────────────────────────────────────────────────────────────────


def test_research_worker_routes_personal_recall_to_notes_memory() -> None:
    from agents.workers.research_worker import RESEARCH_PROMPT

    assert "notes_memory" in RESEARCH_PROMPT, (
        "Research worker must route personal-recall questions to "
        "notes_memory before web_search."
    )
    assert "web_search" in RESEARCH_PROMPT
    # Must explicitly distinguish the two cases — that's the discipline.
    assert "personal" in RESEARCH_PROMPT.lower() or "what did i" in RESEARCH_PROMPT.lower()


def test_health_worker_grounds_in_actual_data() -> None:
    from agents.workers.health_worker import HEALTH_PROMPT

    assert "health_monitor" in HEALTH_PROMPT, (
        "Health worker must call health_monitor (or equivalent) before "
        "discussing metrics; never quote training-data numbers."
    )
    assert "baseline" in HEALTH_PROMPT.lower(), (
        "Health worker must compare against the user's personal baseline."
    )


def test_home_worker_lists_devices_before_acting() -> None:
    """The home-worker prompt must reference the registered home skill
    and instruct the LLM to list devices before acting on one.

    Historical note: this test originally asserted ``"home_assistant"``
    appears in ``HOME_PROMPT``. The home skill was refactored to
    ``smart_home_hue`` (see ``agents/workers/home_worker.py`` —
    ``_HOME_SKILLS = ("smart_home_hue", ...)`` — and the
    fan-out prompt mentions ``smart_home_hue.get_entities`` /
    ``smart_home_hue.get_entity_state``). The assertion is now updated
    to the real skill id so the test stops being the "pre-existing
    failure to ignore" that's been blocking release workers for
    months.
    """
    from agents.workers.home_worker import HOME_PROMPT

    assert "smart_home_hue" in HOME_PROMPT, (
        "Home worker prompt must reference the registered smart_home_hue "
        "skill (the home_assistant skill was retired)."
    )
    lowered = HOME_PROMPT.lower()
    assert "list" in lowered and ("devices" in lowered or "device" in lowered), (
        "Home worker must list devices before claiming one exists."
    )


def test_creative_worker_checks_calendar_before_scheduling() -> None:
    from agents.workers.creative_worker import CREATIVE_PROMPT

    assert "calendar" in CREATIVE_PROMPT.lower()
    lowered = CREATIVE_PROMPT.lower()
    assert "before" in lowered and "schedul" in lowered, (
        "Creative worker must check calendar BEFORE scheduling — "
        "no blind event creation."
    )


def test_general_worker_has_real_tool_discipline_prompt() -> None:
    """The 'general' worker used to be a one-line fallback.

    The deepening pass replaced the one-liner with a real prompt that
    mirrors the master's tool-selection rules in compact form.
    """
    from agents.multi_agent import MultiAgentOrchestrator

    orch = MultiAgentOrchestrator(
        llm=None,
        skill_registry=None,
        skill_executor=None,
        memory=None,
        perception=None,
        send_to_client=None,
    )
    general = orch._workers.get("general")
    assert general is not None
    prompt = general.system_prompt
    assert len(prompt) > 200, (
        "General worker prompt is too short — must include real tool "
        "discipline guidance, not a one-liner."
    )
    assert "notes_memory__fused_timeline" in prompt
    assert "web_search" in prompt
