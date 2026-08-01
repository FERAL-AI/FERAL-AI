"""The LLM/voice tool surface must be deterministic and must keep coding tools.

Two defects are pinned here.

1. Voice builds its tool list from ``get_all_tools()`` (176 tools) and caps
   it at the OpenAI hard limit of 128, dropping 48 tools every session.
   ``PINNED_OPENAI_TOOL_NAMES`` was entirely robot/routine flavoured, so no
   coding tool ever survived the cap.

2. ``Orchestrator.ALWAYS_INCLUDE_SKILLS`` was a ``set``. ``_ensure_core_skills``
   appends in iteration order, and set iteration follows hash order, which
   varies with PYTHONHASHSEED across processes. The tail of the tool list
   therefore differed boot to boot, so any cap would drop different tools on
   different machines. It also carried ``"browser"``, which matches no
   registered manifest and was silently swallowed by the membership guard.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import skills.impl  # noqa: F401,E402  register backing skills

from agents.orchestrator import Orchestrator  # noqa: E402
from agents.tool_list import (  # noqa: E402
    OPENAI_TOOL_HARD_LIMIT,
    PINNED_OPENAI_TOOL_NAMES,
    cap_tools_with_pins,
)
from skills.registry import SkillRegistry  # noqa: E402

REQUIRED_CODING_PINS = (
    "coding_tools__read_file",
    "coding_tools__edit_file",
    "coding_tools__write_file",
    "coding_tools__grep_search",
    "coding_tools__bash",
)


class TestCodingToolsArePinned:
    def test_every_required_coding_tool_is_pinned(self):
        for name in REQUIRED_CODING_PINS:
            assert name in PINNED_OPENAI_TOOL_NAMES, f"{name} is not pinned"

    def test_pinned_names_all_exist_in_the_registry(self):
        """A pin that matches no endpoint is dead weight, not protection."""
        reg = SkillRegistry()
        reg.load_builtin_skills()
        available = {
            (tool.get("function") or tool).get("name")
            for tool in reg.get_tools_for_skills(list(reg.skills.values()))
        }
        for name in REQUIRED_CODING_PINS:
            assert name in available, f"pinned tool {name} has no matching endpoint"

    def test_coding_tools_survive_the_128_cap(self):
        """Reproduces the voice path: a 176-tool list capped to 128."""
        reg = SkillRegistry()
        reg.load_builtin_skills()
        all_tools = reg.get_tools_for_skills(list(reg.skills.values()))
        # Pad so the cap definitely bites even on a trimmed manifest set.
        padding = [
            {"type": "function", "function": {"name": f"filler__ep{i}", "parameters": {}}}
            for i in range(max(0, OPENAI_TOOL_HARD_LIMIT * 2 - len(all_tools)))
        ]
        # Coding tools sit late enough that a naive head-slice evicts them.
        oversized = padding + all_tools
        assert len(oversized) > OPENAI_TOOL_HARD_LIMIT

        capped = cap_tools_with_pins(oversized, max_tools=OPENAI_TOOL_HARD_LIMIT)
        kept = {(t.get("function") or t).get("name") for t in capped}

        assert len(capped) == OPENAI_TOOL_HARD_LIMIT
        for name in REQUIRED_CODING_PINS:
            assert name in kept, f"{name} was dropped by the 128-tool cap"


class TestAlwaysIncludeSkillsIsDeterministic:
    def test_is_an_ordered_sequence_not_a_set(self):
        assert isinstance(Orchestrator.ALWAYS_INCLUDE_SKILLS, tuple)

    def test_browser_phantom_is_gone(self):
        assert "browser" not in Orchestrator.ALWAYS_INCLUDE_SKILLS

    def test_no_duplicates(self):
        ids = list(Orchestrator.ALWAYS_INCLUDE_SKILLS)
        assert len(ids) == len(set(ids))

    def test_every_entry_resolves_to_a_registered_manifest(self):
        reg = SkillRegistry()
        reg.load_builtin_skills()
        missing = [s for s in Orchestrator.ALWAYS_INCLUDE_SKILLS if s not in reg.skills]
        assert missing == [], f"always-include skills with no manifest: {missing}"

    def test_order_is_stable_across_python_hash_seeds(self):
        """The actual regression: a set iterates in PYTHONHASHSEED order."""
        snippet = (
            "import sys; sys.path.insert(0, %r);"
            "from agents.orchestrator import Orchestrator;"
            "print(','.join(Orchestrator.ALWAYS_INCLUDE_SKILLS))" % str(ROOT)
        )
        seen = set()
        for seed in ("0", "1", "12345", "99999"):
            out = subprocess.run(
                [sys.executable, "-c", snippet],
                capture_output=True, text=True, cwd=str(ROOT),
                env={**__import__("os").environ, "PYTHONHASHSEED": seed},
                timeout=180,
            )
            assert out.returncode == 0, out.stderr[-2000:]
            seen.add(out.stdout.strip().splitlines()[-1])
        assert len(seen) == 1, f"order varied with PYTHONHASHSEED: {seen}"

    def test_ensure_core_skills_appends_in_declared_order(self):
        reg = SkillRegistry()
        reg.load_builtin_skills()
        orch = Orchestrator.__new__(Orchestrator)
        orch.skills = reg

        appended = [s.skill_id for s in orch._ensure_core_skills([])]

        expected = [
            s for s in Orchestrator.ALWAYS_INCLUDE_SKILLS
            if s in reg.skills
            and s not in Orchestrator._SMART_LOOPS_SKILLS
        ]
        assert appended[:len(expected)] == expected
