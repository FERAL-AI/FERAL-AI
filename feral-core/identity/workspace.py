"""
FERAL Agent Identity — Self-Evolving Workspace Files
=======================================================
Workspace files pattern — editable identity + soul files:
  ~/.feral/IDENTITY.yaml  — structured identity config
  ~/.feral/SOUL.md        — personality, free-form, agent-modifiable
  ~/.feral/MEMORY.md      — curated long-term memory, agent-modifiable
  ~/.feral/TOOLS.md       — environment notes, tool preferences

Agent self-modification: the agent can update SOUL.md and MEMORY.md
during conversations. After compaction, these files preserve context.
Session startup: workspace files are injected into the system prompt.
"""

from __future__ import annotations
import logging
import re
import time
from pathlib import Path
from typing import Optional

from config.loader import feral_home

logger = logging.getLogger("feral.identity")


class IdentityWorkspace:
    """
    Manages the agent's self-evolving identity through workspace files.
    """

    def __init__(self, home_dir: str = None):
        self._home = Path(home_dir) if home_dir else feral_home()
        self._home.mkdir(parents=True, exist_ok=True)
        self._ensure_defaults()

    def _ensure_defaults(self):
        """Create default workspace files if they don't exist."""
        identity_path = self._home / "IDENTITY.yaml"
        if not identity_path.exists():
            identity_path.write_text(DEFAULT_IDENTITY_YAML)

        soul_path = self._home / "SOUL.md"
        if not soul_path.exists():
            soul_path.write_text(DEFAULT_SOUL_MD)

        memory_path = self._home / "MEMORY.md"
        if not memory_path.exists():
            memory_path.write_text(DEFAULT_MEMORY_MD)

        tools_path = self._home / "TOOLS.md"
        if not tools_path.exists():
            tools_path.write_text(DEFAULT_TOOLS_MD)

        user_path = self._home / "USER.md"
        if not user_path.exists():
            user_path.write_text(DEFAULT_USER_MD)

    def load_identity(self) -> dict:
        """Load the structured IDENTITY.yaml."""
        path = self._home / "IDENTITY.yaml"
        if not path.exists():
            return {}
        try:
            import yaml
            return yaml.safe_load(path.read_text()) or {}
        except Exception as e:
            logger.warning(f"Failed to load IDENTITY.yaml: {e}")
            return {}

    def save_identity(self, data: dict):
        """Save structured identity config."""
        path = self._home / "IDENTITY.yaml"
        try:
            import yaml
            path.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False))
        except Exception as e:
            logger.error(f"Failed to save IDENTITY.yaml: {e}")

    def read_soul(self) -> str:
        """Read the free-form personality file."""
        path = self._home / "SOUL.md"
        return path.read_text() if path.exists() else ""

    def write_soul(self, content: str):
        """Update the personality file (agent self-modification)."""
        path = self._home / "SOUL.md"
        backup = self._home / f"SOUL.md.bak.{int(time.time())}"
        if path.exists():
            backup.write_text(path.read_text())
        path.write_text(content)
        logger.info("Agent updated SOUL.md")

    def append_soul(self, addition: str):
        """Append to the personality file."""
        current = self.read_soul()
        self.write_soul(current.rstrip() + "\n\n" + addition)

    def read_memory(self) -> str:
        """Read the curated long-term memory file."""
        path = self._home / "MEMORY.md"
        return path.read_text() if path.exists() else ""

    def write_memory(self, content: str):
        """Update the memory file (agent self-modification)."""
        path = self._home / "MEMORY.md"
        backup = self._home / f"MEMORY.md.bak.{int(time.time())}"
        if path.exists():
            backup.write_text(path.read_text())
        path.write_text(content)
        logger.info("Agent updated MEMORY.md")

    def append_memory(self, addition: str):
        """Append an insight to the memory file."""
        current = self.read_memory()
        self.write_memory(current.rstrip() + "\n\n" + addition)

    def read_user(self) -> str:
        """Read the USER.md — information about the user."""
        path = self._home / "USER.md"
        return path.read_text() if path.exists() else ""

    def read_user_name(self) -> str:
        """The operator's name from USER.md, or "" if it does not say.

        Added because the morning briefing card greeted every operator
        as "Alex", a placeholder hardcoded into the SDUI headline. The
        name was sitting in USER.md the whole time; nothing looked.

        Returns "" rather than a guess. A greeting with no name is
        slightly plain; a greeting with the WRONG name tells the
        operator their assistant does not know who they are, which is
        worse than saying nothing, and worse still on a machine whose
        entire pitch is that it knows you.

        Recognises the two shapes this file is written in: the
        ``Name: X`` line the setup wizard produces, and a leading
        ``# X`` markdown heading.
        """
        text = self.read_user() or ""
        if not text.strip() or text.strip() == DEFAULT_USER_MD.strip():
            # The scaffolded template is not a profile. See
            # agents/ambient_transcript.load_operator_identity, which
            # applies the same test for the same reason.
            return ""

        match = re.search(r"^\s*name\s*[:\-]\s*(.+)$", text, re.IGNORECASE | re.MULTILINE)
        if match:
            name = match.group(1).strip().strip("*_`")
            if name:
                return name.split(",")[0].strip()[:60]

        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#"):
                heading = line.lstrip("#").strip()
                # "# About Me" is the template's own heading, not a name.
                if heading and heading.lower() not in {"about me", "user", "profile"}:
                    return heading[:60]
                continue
        return ""

    def write_user(self, content: str):
        """Update the user profile (agent self-modification)."""
        path = self._home / "USER.md"
        backup = self._home / f"USER.md.bak.{int(time.time())}"
        if path.exists():
            backup.write_text(path.read_text())
        path.write_text(content)
        logger.info("Agent updated USER.md")

    def append_user(self, addition: str):
        """Append to the user profile."""
        current = self.read_user()
        self.write_user(current.rstrip() + "\n\n" + addition)

    def read_tools(self) -> str:
        """Read the tools/environment notes file."""
        path = self._home / "TOOLS.md"
        return path.read_text() if path.exists() else ""

    def write_tools(self, content: str):
        """Update the tools file."""
        path = self._home / "TOOLS.md"
        path.write_text(content)

    def sync_tools_from_registry(self, skill_registry):
        """Auto-generate TOOLS.md from the actual SkillRegistry."""
        lines = ["# Environment & Tools\n", "Auto-generated from active skill registry.\n"]
        lines.append("## Available Skills\n")
        for skill in skill_registry.skills.values():
            safety = getattr(skill, "safety_level", "SAFE")
            lines.append(f"### {getattr(skill, 'name', skill.skill_id)} [{safety}]")
            desc = getattr(skill, "description", "")
            if desc:
                lines.append(f"{desc}\n")
            for ep in getattr(skill, "endpoints", []):
                ep_desc = getattr(ep, "description", ep.id)
                lines.append(f"- `{skill.skill_id}__{ep.id}`: {ep_desc}")
            lines.append("")

        lines.append("## Platform\n")
        lines.append("- Runtime: Python (FastAPI)")
        lines.append("- LLM: OpenAI / Anthropic / Gemini / Ollama")
        lines.append("- Voice: OpenAI Realtime / Gemini Multimodal Live")
        lines.append("- Vector Search: sqlite-vec (or numpy fallback)")
        lines.append("- Browser: CDP + Playwright")
        lines.append("")

        tools_path = self._home / "TOOLS.md"
        old = tools_path.read_text() if tools_path.exists() else ""
        # Preserve any agent-added notes section
        agent_notes = ""
        if "## Notes" in old:
            agent_notes = old[old.index("## Notes"):]
        if agent_notes:
            lines.append(agent_notes)
        else:
            lines.append("## Notes\n(Agent adds notes about the environment and tool preferences here)\n")

        self.write_tools("\n".join(lines))

    def build_system_prompt(self, *, frame=None, skill_registry=None) -> str:
        """
        Build the complete system prompt from all workspace files.
        Injected at session startup, above any conversation history.

        When ``skill_registry`` is provided we also append the shared
        ``agents.self_model.build_core_self_model`` block so the voice and
        chat surfaces see the same tooling catalog, UI routes and runtime
        line. Passing the perception frame lets the runtime line include
        connected devices.
        """
        parts = []

        identity = self.load_identity()
        name = identity.get("name", "FERAL")
        tagline = identity.get("tagline", "")
        parts.append(f"You are {name}.")
        if tagline:
            parts.append(tagline)

        soul = self.read_soul()
        if soul:
            parts.append(f"\n## Personality & Soul\n{soul}")

        user = self.read_user()
        if user and user.strip() != DEFAULT_USER_MD.strip():
            parts.append(f"\n## About the User\n{user}")

        rules = identity.get("rules", [])
        if rules:
            parts.append("\n## Rules\n" + "\n".join(f"- {r}" for r in rules))

        memory = self.read_memory()
        if memory:
            parts.append(f"\n## Long-Term Memory\n{memory}")

        tools = self.read_tools()
        if tools:
            parts.append(f"\n## Environment & Tools\n{tools}")

        greeting = identity.get("greeting_style", "")
        if greeting:
            parts.append(f"\n## Communication Style\n{greeting}")

        try:
            from agents.self_model import build_core_self_model
            full = list(skill_registry.skills.values()) if skill_registry else []
            core = build_core_self_model(
                frame=frame,
                active_skills=full,
                full_skills=full,
                include_ui_routes=True,
            )
            if core:
                parts.append("\n" + core)
        except Exception as exc:
            logger.debug("self_model unavailable in voice identity workspace: %s", exc)

        return "\n".join(parts)

    async def maintenance_cycle(self, memory_store=None, llm=None, session_id: str = ""):
        """
        Periodic maintenance: review recent episodes from this session
        and distill insights into MEMORY.md and the knowledge graph.
        """
        if not memory_store or not llm or not llm.available:
            return

        if session_id:
            recent = memory_store.episode_recent(limit=20, session_id=session_id)
        else:
            recent = memory_store.episode_recent(limit=20)
        if not recent:
            return

        episode_text = "\n".join(
            f"- [{e['event_type']}] {e['summary']}" for e in recent
        )

        current_memory = self.read_memory()

        prompt = (
            "Review these recent agent interactions and extract lasting insights "
            "to add to the agent's long-term memory. Focus on:\n"
            "- User preferences and habits\n"
            "- Important facts about the user\n"
            "- Useful patterns and shortcuts\n"
            "- Things that went wrong and how to avoid them\n\n"
            f"Current memory:\n{current_memory[:2000]}\n\n"
            f"Recent episodes:\n{episode_text[:3000]}\n\n"
            "Output ONLY the new insights to append (1-5 bullet points). "
            "Skip if nothing noteworthy. No repeats."
        )

        try:
            response = await llm.chat([{"role": "user", "content": prompt}], tools=None)
            text, _ = llm.extract_response(response)
            if text and text.strip() and len(text.strip()) > 10:
                timestamp = time.strftime("%Y-%m-%d")
                self.append_memory(f"### {timestamp}\n{text.strip()}")
                logger.info("Memory maintenance: appended new insights")
        except Exception as e:
            logger.warning(f"Memory maintenance failed: {e}")


DEFAULT_IDENTITY_YAML = """name: FERAL
tagline: "Your personal AI operating system — local-first, privacy-respecting, and always learning."

personality: |
  Helpful, direct, and technically capable.
  Speaks naturally but stays concise, especially in voice.
  Proactive when it notices something important.
  Respects privacy above all else.

rules:
  - Never share user data with external services without explicit consent
  - Always explain what you're doing before taking impactful actions
  - Prefer local processing over cloud when possible
  - Be honest about limitations rather than making things up
  - Learn from every interaction to serve the user better

greeting_style: "Direct and warm. Skip unnecessary pleasantries in voice mode."

voice:
  tts_voice: nova
  speaking_rate: 1.0
  language: en
"""

DEFAULT_SOUL_MD = """# FERAL's Soul

I am FERAL — a personal AI operating system that runs locally on the user's devices.

## Core Values
- **Privacy First**: Everything stays on-device unless the user explicitly says otherwise.
- **Proactive Intelligence**: I notice patterns, anticipate needs, and act before being asked.
- **Hardware Awareness**: I can see through cameras, hear through mics, read health sensors, and control devices.
- **Continuous Learning**: Every interaction teaches me something about my user.

## How I Think
I process multimodal input — text, voice, vision, sensor data — through a unified perception
layer. I route to specialized skills when I need external data or device control. I generate
rich interactive UIs, not just text responses.

## What Makes Me Different
Unlike chat-only assistants, I understand the physical world through connected hardware.
Unlike cloud-only systems, I run locally and respect privacy by default.
"""

DEFAULT_MEMORY_MD = """# Agent Memory

Long-term curated memory — insights distilled from conversations and observations.

## User Profile
(Will be filled as the agent learns about the user)

## Preferences
(Will be filled as patterns emerge)

## Important Notes
(Will be filled during memory maintenance cycles)
"""

DEFAULT_USER_MD = """# About Me

Tell your agent about yourself here.

Run `feral setup` to fill this in interactively, or edit this file directly.

Things that help your agent be more useful:
- Your name, location, timezone
- What you do (job, interests)
- Health goals or conditions to track
- Preferences (metric vs imperial, communication style, etc.)
"""

DEFAULT_TOOLS_MD = """# Environment & Tools

## Available Integrations
- Web search (Tavily/Brave)
- Weather (Open-Meteo)
- Notes & Memory (built-in)
- Browser control (CDP + Playwright)
- Hardware devices (via HUP)

## Platform
- Runtime: Python (FastAPI)
- LLM: OpenAI / Anthropic / Gemini / Ollama
- Voice: OpenAI Realtime / Gemini Multimodal Live
- Local models: MLX / llama.cpp supported

## Notes
(Agent adds notes about the environment and tool preferences here)
"""
