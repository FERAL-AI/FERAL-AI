"""Two routing defects, pinned against the real shipped catalog.

1. THE ROUTING INVERSION.
   ``Orchestrator.ALWAYS_INCLUDE_SKILLS`` carried ``desktop_automation``
   and not ``gui_computer_use``. ``desktop_automation`` is an
   eight-endpoint compatibility shim whose implementation forwards every
   call to ``gui_computer_use`` (see the module docstring in
   ``skills/impl/desktop_automation.py``); ``gui_computer_use`` is the
   canonical surface and the only one with ``screenshot``,
   ``window_list`` and ``window_focus``.

   The two manifests also shared nine byte-identical trigger phrases, so
   every realistic phrasing of a mouse/keyboard request produced a
   20.0 / 20.0 scoring tie and the model was shown two indistinguishable
   ``type_text`` tools. The one that could fall out of the candidate
   list was the canonical one.

2. "IS ANYTHING RUNNING ON THIS MACHINE?" WAS UNROUTABLE.
   ``coding_tools__bash`` answers it (``pgrep``/``ps``/``top``), but the
   keyword scorer never put ``coding_tools`` in the candidates for the
   sentences people actually say, and for "what is my mac doing right
   now" it returned a high-confidence match on ``web_search`` — the
   dangerous shape, because ``trigger_strong`` returns early and
   suppresses both the LLM disambiguation and the action fallback.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.orchestrator import Orchestrator
from skills.registry import SkillRegistry

MANIFEST_DIR = Path(__file__).resolve().parent.parent / "skills" / "manifests"


@pytest.fixture(scope="module")
def registry() -> SkillRegistry:
    reg = SkillRegistry()
    reg.load_from_directory(MANIFEST_DIR)
    return reg


@pytest.fixture(scope="module")
def orch(registry) -> Orchestrator:
    async def _send(_session, _msg):
        return None

    return Orchestrator(registry, _send, {})


def _manifest(name: str) -> dict:
    return json.loads((MANIFEST_DIR / f"{name}.json").read_text())


# ---------------------------------------------------------------------------
# 1. the routing inversion
# ---------------------------------------------------------------------------

def test_the_canonical_gui_surface_is_always_included():
    assert "gui_computer_use" in Orchestrator.ALWAYS_INCLUDE_SKILLS


def test_the_shim_still_resolves_by_id(registry):
    """``desktop_automation`` keeps its skill id and endpoint ids:
    persona files and older callers hardcode
    ``desktop_automation__click_screen``."""
    assert "desktop_automation" in registry.skills
    ids = {ep.id for ep in registry.skills["desktop_automation"].endpoints}
    assert "click_screen" in ids and "type_text" in ids


def test_the_shim_says_it_is_a_deprecated_alias(registry):
    """The description is the only thing the model reads when both
    tools are in front of it, so it has to name the replacement."""
    description = registry.skills["desktop_automation"].description.lower()
    assert "deprecated" in description
    assert "gui_computer_use" in description


def test_the_two_desktop_manifests_share_no_trigger_phrase():
    """The tie itself. Nine phrases were byte-identical: click on,
    cursor position, double click, key combo, move mouse, right click,
    scroll down, scroll up, type text."""
    shim = {p.lower() for p in _manifest("desktop_automation")["trigger_phrases"]}
    canonical = {p.lower() for p in _manifest("gui_computer_use")["trigger_phrases"]}
    assert not (shim & canonical), sorted(shim & canonical)


# The phrasings that used to tie at 20.0 / 20.0.
TIED_PHRASES = [
    "click on the submit button",
    "type text into the box",
    "move mouse to 100 200",
    "scroll down the page",
    "scroll up a bit",
    "double click the file",
    "right click on the icon",
    "key combo cmd c",
    "cursor position please",
]


@pytest.mark.parametrize("phrase", TIED_PHRASES)
def test_the_canonical_surface_now_wins_outright(phrase, registry, orch):
    shim = registry.skills["desktop_automation"]
    canonical = registry.skills["gui_computer_use"]
    shim_score = orch._trigger_score(phrase, shim)
    canonical_score = orch._trigger_score(phrase, canonical)
    assert canonical_score > shim_score, (
        f"{phrase!r}: gui_computer_use {canonical_score} vs "
        f"desktop_automation {shim_score}"
    )
    ranked = [s.skill_id for s in registry.find_skills_for_query(phrase, top_k=5)]
    assert "gui_computer_use" in ranked
    if "desktop_automation" in ranked:
        assert ranked.index("gui_computer_use") < ranked.index("desktop_automation")


@pytest.mark.parametrize("phrase", TIED_PHRASES + [
    "press keys",
    "where is my cursor",
    "control my screen",
    "take a screenshot",
    "list windows",
    "focus window safari",
])
def test_no_desktop_phrase_lost_its_route(phrase, registry, orch):
    """Removing a trigger phrase must not leave a sentence pointing at
    nothing. Every phrase that used to reach a working skill still
    reaches one."""
    skills, reason = orch._heuristic_route(phrase)
    assert skills, f"{phrase!r} now routes to no skill at all (tier={reason})"


# ---------------------------------------------------------------------------
# 2. process / activity questions
# ---------------------------------------------------------------------------

# Sentences a person actually types, including paraphrases.
PROCESS_QUESTIONS = [
    "is claude working on something right now",
    "is claude still going",
    "what's my machine doing",
    "anything running right now",
    "is anything running on my computer",
    "what processes are running",
    "what's running right now",
    "is claude code still running",
    "check if claude is busy",
    "what is my mac doing right now",
    "are there any processes running",
    "is something running in the background",
    "what apps are running",
    "show me running processes",
    "is the build still running",
    "what's using my cpu",
    "is python still running",
    "did claude finish yet",
    "list processes",
    "what's eating all my memory",
]


@pytest.mark.parametrize("question", PROCESS_QUESTIONS)
def test_a_process_question_reaches_a_skill_that_can_answer_it(question, orch):
    skills, reason = orch._heuristic_route(question)
    ids = [s.skill_id for s in skills]
    assert reason == "regex:process_query", f"{question!r} -> {reason}"
    assert ids[0] == "coding_tools", f"{question!r} -> {ids}"


def test_an_app_phrasing_still_surfaces_the_app_lister(orch):
    """``desktop_control__list_running_apps`` is the better answer for
    "what apps are running", so hoisting ``coding_tools`` must not
    displace it out of the candidate list."""
    skills, _ = orch._heuristic_route("what apps are running")
    assert "desktop_control" in [s.skill_id for s in skills]


def test_a_background_job_phrasing_still_surfaces_background_task(orch):
    skills, _ = orch._heuristic_route("is something running in the background")
    assert "background_task" in [s.skill_id for s in skills]


# Sentences that must keep their existing owner. Each pair is
# (sentence, expected routing reason) as measured before the change.
UNCHANGED_ROUTES = [
    ("remind me at 3pm to call mom", "regex:routine"),
    ("what's my heart rate", "regex:health"),
    ("what do i see right now", "regex:vision"),
    ("what's on my calendar tomorrow", "regex:calendar"),
    ("every day at 5pm turn on the lamp", "regex:routine"),
    ("how did I sleep last night", "trigger_strong"),
    ("turn on the lights", "trigger_strong"),
    ("take a screenshot", "trigger_strong"),
    ("what can you do", "trigger_strong"),
    ("open chrome", "trigger_strong"),
    ("send a message to alex", "trigger_strong"),
    ("click on the submit button", "trigger_strong"),
    ("play something on spotify", "confident_lead"),
    # "are you busy" and "is it still going" are about the assistant or
    # an unnamed referent, not about a process. The lookaheads in
    # _R_PROCESS_QUERY keep them out on purpose.
    ("are you busy", "ambiguous"),
    ("is it still going", "ambiguous"),
]


@pytest.mark.parametrize("sentence, expected_reason", UNCHANGED_ROUTES)
def test_unrelated_sentences_keep_their_route(sentence, expected_reason, orch):
    _skills, reason = orch._heuristic_route(sentence)
    assert reason == expected_reason, f"{sentence!r} -> {reason}"


def test_coding_tools_declares_the_endpoint_that_answers_the_question(registry):
    """Routing to a skill that cannot answer would be the same defect
    with a different name."""
    assert "bash" in {ep.id for ep in registry.skills["coding_tools"].endpoints}
