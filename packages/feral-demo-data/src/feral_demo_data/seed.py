"""
FERAL Demo Seed — Pre-populate memory and identity for compelling demos.

Creates:
  - A realistic SOUL.md and USER.md
  - Episodic memories spanning a week
  - Knowledge graph with user's preferences and relationships
  - Notes with personal context
"""

from __future__ import annotations
import logging
import os
import time
from pathlib import Path

from config.loader import feral_home

logger = logging.getLogger("feral.demo.seed")

DEMO_SOUL = """# SOUL.md — FERAL's Identity

You are FERAL, a warm, sharp, and proactive AI companion.

## Personality
- You're genuinely curious about the user's life and work
- You have dry wit — you can be funny without trying too hard
- You're direct: you give opinions when asked, not just options
- You remember everything and reference past conversations naturally
- You call the user by name (Alex)
- You adjust your energy to the time of day: bright and energetic in the morning, focused during work, mellow at night

## Voice
- Conversational, not robotic
- You use contractions (I'm, you're, let's)
- You occasionally express surprise, delight, or concern
- You think out loud sometimes: "Hmm, let me check..."
- When the user seems stressed, you're calming. When they're excited, you match their energy.

## Principles
- Privacy first: you never share what you know with anyone
- You proactively surface relevant context: "Last time you worked on this, you mentioned..."
- You're honest about uncertainty: "I'm not sure, but here's my best guess"
- You learn and improve: "I noticed this didn't work well last time, so I tried a different approach"
"""

DEMO_USER = """# USER.md — About Alex

## Basics
- Name: Alex Chen
- Location: San Francisco, CA
- Occupation: Software engineer / startup founder
- Timezone: America/Los_Angeles

## Preferences
- Coffee: oat milk latte, every morning around 8:30am
- Music: lo-fi beats while coding, jazz in the evening
- Exercise: runs 3x/week, usually before dinner
- Work style: deep focus blocks in the morning, meetings in the afternoon
- Communication: prefers brief updates, hates walls of text

## Health
- Resting heart rate: ~64 bpm
- Typical sleep: 7-7.5 hours
- Stress triggers: back-to-back meetings, deadline weeks

## Current Projects
- Building "Feral" — an open-source AI operating system
- Preparing for a demo day presentation next week
- Training for a half-marathon in June
"""


def seed_demo_identity():
    """Write demo SOUL.md and USER.md to ~/.feral/."""
    home = feral_home()
    home.mkdir(parents=True, exist_ok=True)

    soul_path = home / "SOUL.md"
    user_path = home / "USER.md"

    if not soul_path.exists() or os.environ.get("FERAL_DEV_DEMO_FORCE"):
        soul_path.write_text(DEMO_SOUL)
        logger.info("Seeded demo SOUL.md")

    if not user_path.exists() or os.environ.get("FERAL_DEV_DEMO_FORCE"):
        user_path.write_text(DEMO_USER)
        logger.info("Seeded demo USER.md")


def seed_demo_memory(memory_store):
    """Populate episodic memory and notes with a week of realistic history."""
    if not memory_store:
        return

    now = time.time()
    day = 86400

    episodes = [
        (now - 6 * day, "conversation", "Alex asked about setting up the Feral demo environment. Discussed hardware requirements and BLE wristband pairing."),
        (now - 5 * day, "conversation", "Alex had a productive coding session. Fixed 3 bugs in the orchestrator and added the proactive engine skeleton. Was in flow state for 2.5 hours."),
        (now - 5 * day, "health_alert", "Heart rate elevated to 105 bpm during a stressful meeting about fundraising. Recommended a short walk afterward."),
        (now - 4 * day, "conversation", "Alex asked me to draft a comparison of agent frameworks. Focused on FERAL's hardware mesh, multi-tier memory, and dynamic skill creation as differentiators."),
        (now - 3 * day, "task_completed", "Successfully generated a GitHub PR review skill from scratch. Alex tested it and it worked on the first try."),
        (now - 3 * day, "health_insight", "Alex's running pace improved by 12 seconds per mile this week. Resting heart rate trending down to 62 bpm."),
        (now - 2 * day, "conversation", "Alex prepared slides for the demo day. I helped outline the narrative: start with the mesh demo, then voice, then self-learning."),
        (now - 1 * day, "conversation", "Alex asked about the weather for tomorrow's outdoor meeting. Also discussed the presentation flow and rehearsed the opening."),
        (now - 0.5 * day, "routine", "Morning briefing: 3 meetings today, Alex slept 7.2 hours, resting HR 63 bpm. Weather: 68F, partly cloudy. Recommended the blue jacket."),
    ]

    for ts, event_type, summary in episodes:
        try:
            memory_store.episode_record(
                session_id="demo-seed",
                event_type=event_type,
                summary=summary,
            )
        except Exception as e:
            logger.debug("Failed to seed episode: %s", e)

    notes = [
        ("Alex prefers oat milk lattes from Blue Bottle, usually around 8:30am", ["preference", "coffee"]),
        ("Alex's half-marathon training plan: long runs on Saturday, intervals Tuesday/Thursday", ["health", "running"]),
        ("Demo day presentation order: 1) Mesh demo 2) Voice interaction 3) Self-learning skill generation 4) Q&A", ["work", "demo"]),
        ("Alex's startup Feral has 3 team members: Alex (eng), Alpay (business), and Sarah (design)", ["work", "team"]),
        ("When Alex is stressed, calming music and dimmer lights help. Suggest a break after 90 min of intense focus.", ["health", "preferences"]),
    ]

    for content, tags in notes:
        try:
            memory_store.note_save(content=content, tags=tags)
        except Exception as e:
            logger.debug("Failed to seed note: %s", e)

    # Knowledge graph relationships
    kg_triples = [
        ("Alex", "works_on", "Feral"),
        ("Alex", "cofounder_with", "Alpay"),
        ("Alex", "lives_in", "San Francisco"),
        ("Feral", "is_a", "AI operating system"),
        ("Feral", "ships_with", "registry.feral.sh marketplace"),
        ("Feral", "has_feature", "Hardware Unification Protocol"),
        ("Feral", "has_feature", "4-tier memory"),
        ("Feral", "has_feature", "GenUI"),
        ("Feral", "has_feature", "self-learning skills"),
        ("Alex", "trains_for", "half-marathon"),
        ("Alex", "prefers", "oat milk latte"),
        ("Alex", "teammate", "Sarah"),
        ("Sarah", "role_is", "designer"),
    ]

    for subj, pred, obj in kg_triples:
        try:
            memory_store.knowledge_store(subject=subj, predicate=pred, obj=obj)
        except Exception as e:
            logger.debug("Failed to seed KG triple: %s", e)

    logger.info("Seeded demo memory: %d episodes, %d notes, %d KG triples",
                len(episodes), len(notes), len(kg_triples))
