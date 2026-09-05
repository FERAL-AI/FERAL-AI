"""Agent Mitosis — self-spawning persistent specialist agents from observed patterns."""
from __future__ import annotations
import json
import logging
import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("feral.agent_mitosis")

@dataclass
class TaskPattern:
    """A recurring task pattern detected from user interactions."""
    pattern_id: str
    topic_cluster: str
    tool_affinities: list[str]
    time_pattern: Optional[str] = None  # e.g., "weekday_morning", "monday_9am"
    occurrence_count: int = 0
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    sample_prompts: list[str] = field(default_factory=list)

@dataclass
class SpecialistAgent:
    """A permanently spawned specialist agent."""
    agent_id: str
    name: str
    description: str
    system_prompt: str
    source_pattern: str
    tool_permissions: list[str]
    schedule: Optional[str] = None  # cron expression
    memory_filter: Optional[str] = None  # topic filter for memory subset
    created_at: float = field(default_factory=time.time)
    last_active: float = 0.0
    tasks_completed: int = 0
    satisfaction_score: float = 0.5  # 0-1, evolves from feedback
    prompt_version: int = 1

SPAWN_THRESHOLD = 5
MAX_SPECIALISTS = 10

class AgentMitosisEngine:
    def __init__(self, llm=None, memory=None, db_path: Optional[str] = None):
        self._llm = llm
        self._memory = memory
        self._patterns: dict[str, TaskPattern] = {}
        self._specialists: dict[str, SpecialistAgent] = {}
        self._topic_tracker: dict[str, Counter] = {}  # session -> topic counts
        self._db_path = db_path
        if db_path:
            self._init_db()
            self._load_from_db()

    def observe_interaction(self, session_id: str, text: str, tools_used: list[str]):
        topic = self._classify_topic(text, tools_used)
        if not topic:
            return

        counter = self._topic_tracker.setdefault(session_id, Counter())
        counter[topic] += 1

        pattern_id = f"pattern_{topic}"
        if pattern_id in self._patterns:
            p = self._patterns[pattern_id]
            p.occurrence_count += 1
            p.last_seen = time.time()
            for t in tools_used:
                if t not in p.tool_affinities:
                    p.tool_affinities.append(t)
            if len(p.sample_prompts) < 5:
                p.sample_prompts.append(text[:200])
        else:
            t = time.localtime()
            time_label = f"{'weekday' if t.tm_wday < 5 else 'weekend'}_{['morning','afternoon','evening','night'][t.tm_hour // 6]}"
            self._patterns[pattern_id] = TaskPattern(
                pattern_id=pattern_id,
                topic_cluster=topic,
                tool_affinities=tools_used[:],
                time_pattern=time_label,
                occurrence_count=1,
                sample_prompts=[text[:200]],
            )
        self._persist_pattern(pattern_id)

    def _classify_topic(self, text: str, tools: list[str]) -> Optional[str]:
        text_lower = text.lower()
        topic_keywords = {
            "health_monitoring": ["health", "heart", "sleep", "exercise", "spo2", "stress", "wellness"],
            "code_review": ["code", "review", "pull request", "pr", "diff", "commit", "bug"],
            "email_management": ["email", "inbox", "reply", "draft", "send email", "unread"],
            "calendar_planning": ["calendar", "schedule", "meeting", "appointment", "event"],
            "research": ["research", "search", "find", "look up", "article", "paper"],
            "writing": ["write", "draft", "essay", "blog", "content", "summarize"],
            "finance": ["budget", "expense", "price", "cost", "payment", "financial"],
            "home_automation": ["lights", "thermostat", "home", "temperature", "scene"],
            "music": ["play", "song", "playlist", "music", "spotify"],
            "news": ["news", "headlines", "trending", "current events"],
        }

        for topic, keywords in topic_keywords.items():
            if any(kw in text_lower for kw in keywords):
                return topic

        if tools:
            tool_prefix = tools[0].split("__")[0] if "__" in tools[0] else tools[0]
            return f"tool_{tool_prefix}"

        return None

    def get_spawn_proposals(self) -> list[dict]:
        proposals = []
        for pid, pattern in self._patterns.items():
            if pattern.occurrence_count >= SPAWN_THRESHOLD and pid not in self._specialists:
                name = pattern.topic_cluster.replace("_", " ").title() + " Agent"
                proposals.append({
                    "pattern_id": pid,
                    "name": name,
                    "topic": pattern.topic_cluster,
                    "seen_count": pattern.occurrence_count,
                    "tools": pattern.tool_affinities,
                    "time_pattern": pattern.time_pattern,
                    "sample_prompts": pattern.sample_prompts[:3],
                })
        return proposals[:MAX_SPECIALISTS - len(self._specialists)]

    async def spawn_specialist(self, pattern_id: str) -> Optional[SpecialistAgent]:
        pattern = self._patterns.get(pattern_id)
        if not pattern or not self._llm:
            return None
        if len(self._specialists) >= MAX_SPECIALISTS:
            return None

        prompt_gen = (
            f"Create a focused system prompt for a specialist AI agent.\n"
            f"Topic: {pattern.topic_cluster}\n"
            f"Tools available: {', '.join(pattern.tool_affinities)}\n"
            f"Sample user requests:\n" + "\n".join(f"- {p}" for p in pattern.sample_prompts[:3]) +
            "\n\nThe prompt should be 3-5 sentences. Be specific about the agent's expertise."
        )

        try:
            response = await self._llm.chat([
                {"role": "system", "content": "You write concise, focused system prompts for AI specialist agents."},
                {"role": "user", "content": prompt_gen},
            ])
            text, _ = self._llm.extract_response(response)
            if not text:
                from agents.llm_provider import llm_response_error
                raise RuntimeError(
                    llm_response_error(response) or "empty specialist prompt",
                )

            agent_id = f"specialist_{pattern.topic_cluster}"
            name = pattern.topic_cluster.replace("_", " ").title() + " Agent"

            specialist = SpecialistAgent(
                agent_id=agent_id,
                name=name,
                description=f"Specialist for {pattern.topic_cluster} tasks",
                system_prompt=text.strip(),
                source_pattern=pattern_id,
                tool_permissions=pattern.tool_affinities[:],
                schedule=self._pattern_to_cron(pattern),
            )
            self._specialists[pattern_id] = specialist
            self._persist_specialist(pattern_id)
            logger.info(f"Agent Mitosis: spawned {agent_id} from pattern {pattern.topic_cluster}")
            return specialist
        except Exception as e:
            logger.warning(f"Agent Mitosis spawn failed: {e}")
            return None

    def register_specialist_from_manifest(
        self,
        *,
        agent_id: Optional[str] = None,
        name: str,
        description: str = "",
        system_prompt: str,
        tool_permissions: Optional[list[str]] = None,
        source_pattern: str = "",
        schedule: Optional[str] = None,
        memory_filter: Optional[str] = None,
    ) -> SpecialistAgent:
        """Create a SpecialistAgent directly from a persona manifest.

        Distinct from ``spawn_specialist``: this path skips the Mitosis
        topic-classifier + LLM system-prompt generation because the user
        (or a curated persona JSON from ``feral-core/agents/personas/``)
        already supplies the full prompt + permissions + memory filter.

        The v2 Agents page "Spawn specialist from persona" button POSTs
        a persona body here; without this entry point the button was a
        silent no-op (`/api/agents/spawn` only read ``pattern_id``).

        Keyed by ``agent_id``. If an agent with the same id exists, its
        row is overwritten — so repeated clicks update instead of
        accumulating duplicates.
        """
        agent_id = agent_id or f"persona_{name.lower().replace(' ', '_')}"
        pattern_id = f"manifest_{agent_id}"
        specialist = SpecialistAgent(
            agent_id=agent_id,
            name=name,
            description=description,
            system_prompt=system_prompt.strip(),
            source_pattern=source_pattern or pattern_id,
            tool_permissions=list(tool_permissions or []),
            schedule=schedule,
            memory_filter=memory_filter,
        )
        self._specialists[pattern_id] = specialist
        self._persist_specialist(pattern_id)
        logger.info(
            "Agent Mitosis: registered %s from persona manifest (pattern=%s, tools=%s)",
            agent_id, pattern_id, specialist.tool_permissions,
        )
        return specialist

    def match_specialist(self, query: str) -> Optional[str]:
        """Return the agent_id of the best-matched specialist, or None."""
        topic = self._classify_topic(query, [])
        if not topic:
            return None
        for pid, spec in self._specialists.items():
            pattern = self._patterns.get(pid)
            if pattern and pattern.topic_cluster == topic:
                return spec.agent_id
        return None

    def get_specialist(self, agent_id: str) -> Optional[SpecialistAgent]:
        """Return a specialist by agent_id."""
        for spec in self._specialists.values():
            if spec.agent_id == agent_id:
                return spec
        return None

    def route_to_specialist(self, query: str, session_id: str = "") -> Optional[SpecialistAgent]:
        """Classify the query and return the matching specialist (or None).

        Used by the orchestrator at the top of ``handle_command`` to swap in a
        specialist's system prompt + narrow tool set for this turn.
        Instead of always running the generalist, we pick the domain-limb that has
        better context for this kind of work.
        """
        agent_id = self.match_specialist(query)
        if not agent_id:
            return None
        spec = self.get_specialist(agent_id)
        if spec is None:
            return None
        spec.last_active = time.time()
        for pid, s in self._specialists.items():
            if s.agent_id == spec.agent_id:
                self._persist_specialist(pid)
                break
        return spec

    def propose_specialist(
        self,
        pattern_tag: str,
        skills_used: list[str],
        sample_prompts: Optional[list[str]] = None,
    ) -> Optional[str]:
        """Record a lightweight specialist proposal from Tool Genesis.

        Called when genesis notices a repeated tool-call sequence. We seed a
        TaskPattern so that the usual ``get_spawn_proposals`` → ``spawn_specialist``
        pipeline picks it up. Returns the new ``pattern_id`` on success.
        """
        if not pattern_tag:
            return None
        pattern_id = f"pattern_{pattern_tag}"
        if pattern_id in self._patterns:
            p = self._patterns[pattern_id]
            p.occurrence_count += SPAWN_THRESHOLD  # fast-track: genesis already saw it repeat
            p.last_seen = time.time()
            for t in skills_used or []:
                if t not in p.tool_affinities:
                    p.tool_affinities.append(t)
            if sample_prompts:
                for sp in sample_prompts:
                    if sp not in p.sample_prompts and len(p.sample_prompts) < 5:
                        p.sample_prompts.append(sp[:200])
        else:
            self._patterns[pattern_id] = TaskPattern(
                pattern_id=pattern_id,
                topic_cluster=pattern_tag,
                tool_affinities=list(skills_used or []),
                occurrence_count=SPAWN_THRESHOLD,
                sample_prompts=list((sample_prompts or [])[:5]),
            )
        self._persist_pattern(pattern_id)
        return pattern_id

    def record_feedback(self, agent_id: str, positive: bool):
        for pid, spec in self._specialists.items():
            if spec.agent_id == agent_id:
                delta = 0.05 if positive else -0.1
                spec.satisfaction_score = max(0, min(1, spec.satisfaction_score + delta))
                spec.tasks_completed += 1
                spec.last_active = time.time()
                self._persist_specialist(pid)
                break

    @staticmethod
    def _pattern_to_cron(pattern: TaskPattern) -> Optional[str]:
        if pattern.time_pattern and "morning" in pattern.time_pattern:
            return "0 9 * * *"  # 9 AM daily
        if pattern.time_pattern and "evening" in pattern.time_pattern:
            return "0 18 * * *"
        return None

    def list_specialists(self) -> list[dict]:
        return [
            {"agent_id": s.agent_id, "name": s.name, "description": s.description,
             "tools": s.tool_permissions, "schedule": s.schedule,
             "tasks": s.tasks_completed, "satisfaction": s.satisfaction_score,
             "created": s.created_at, "last_active": s.last_active}
            for s in self._specialists.values()
        ]

    def stats(self) -> dict:
        return {
            "patterns_tracked": len(self._patterns),
            "proposals_ready": len(self.get_spawn_proposals()),
            "specialists_active": len(self._specialists),
            "total_tasks": sum(s.tasks_completed for s in self._specialists.values()),
        }

    # ── SQLite persistence ──────────────────────

    def _init_db(self):
        con = sqlite3.connect(self._db_path)
        con.execute(
            "CREATE TABLE IF NOT EXISTS task_patterns ("
            "  pattern_id TEXT PRIMARY KEY,"
            "  topic_cluster TEXT NOT NULL,"
            "  tool_affinities_json TEXT NOT NULL,"
            "  time_pattern TEXT,"
            "  occurrence_count INTEGER NOT NULL DEFAULT 0,"
            "  first_seen REAL NOT NULL,"
            "  last_seen REAL NOT NULL,"
            "  sample_prompts_json TEXT NOT NULL"
            ")"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS specialist_agents ("
            "  pattern_id TEXT PRIMARY KEY,"
            "  agent_id TEXT NOT NULL,"
            "  name TEXT NOT NULL,"
            "  description TEXT NOT NULL,"
            "  system_prompt TEXT NOT NULL,"
            "  source_pattern TEXT NOT NULL,"
            "  tool_permissions_json TEXT NOT NULL,"
            "  schedule TEXT,"
            "  memory_filter TEXT,"
            "  created_at REAL NOT NULL,"
            "  last_active REAL NOT NULL DEFAULT 0,"
            "  tasks_completed INTEGER NOT NULL DEFAULT 0,"
            "  satisfaction_score REAL NOT NULL DEFAULT 0.5,"
            "  prompt_version INTEGER NOT NULL DEFAULT 1"
            ")"
        )
        con.commit()
        con.close()

    def _load_from_db(self):
        con = sqlite3.connect(self._db_path)
        for row in con.execute(
            "SELECT pattern_id, topic_cluster, tool_affinities_json, time_pattern,"
            "       occurrence_count, first_seen, last_seen, sample_prompts_json FROM task_patterns"
        ):
            self._patterns[row[0]] = TaskPattern(
                pattern_id=row[0], topic_cluster=row[1],
                tool_affinities=json.loads(row[2]), time_pattern=row[3],
                occurrence_count=row[4], first_seen=row[5], last_seen=row[6],
                sample_prompts=json.loads(row[7]),
            )
        for row in con.execute(
            "SELECT pattern_id, agent_id, name, description, system_prompt, source_pattern,"
            "       tool_permissions_json, schedule, memory_filter, created_at,"
            "       last_active, tasks_completed, satisfaction_score, prompt_version"
            " FROM specialist_agents"
        ):
            self._specialists[row[0]] = SpecialistAgent(
                agent_id=row[1], name=row[2], description=row[3],
                system_prompt=row[4], source_pattern=row[5],
                tool_permissions=json.loads(row[6]), schedule=row[7],
                memory_filter=row[8], created_at=row[9],
                last_active=row[10], tasks_completed=row[11],
                satisfaction_score=row[12], prompt_version=row[13],
            )
        con.close()
        logger.info("Agent Mitosis DB loaded: %d patterns, %d specialists", len(self._patterns), len(self._specialists))

    def _persist_pattern(self, pattern_id: str):
        if not self._db_path:
            return
        p = self._patterns[pattern_id]
        con = sqlite3.connect(self._db_path)
        con.execute(
            "INSERT OR REPLACE INTO task_patterns"
            " (pattern_id, topic_cluster, tool_affinities_json, time_pattern,"
            "  occurrence_count, first_seen, last_seen, sample_prompts_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (p.pattern_id, p.topic_cluster, json.dumps(p.tool_affinities),
             p.time_pattern, p.occurrence_count, p.first_seen, p.last_seen,
             json.dumps(p.sample_prompts)),
        )
        con.commit()
        con.close()

    def _persist_specialist(self, pattern_id: str):
        if not self._db_path:
            return
        s = self._specialists[pattern_id]
        con = sqlite3.connect(self._db_path)
        con.execute(
            "INSERT OR REPLACE INTO specialist_agents"
            " (pattern_id, agent_id, name, description, system_prompt, source_pattern,"
            "  tool_permissions_json, schedule, memory_filter, created_at,"
            "  last_active, tasks_completed, satisfaction_score, prompt_version)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pattern_id, s.agent_id, s.name, s.description, s.system_prompt,
             s.source_pattern, json.dumps(s.tool_permissions), s.schedule,
             s.memory_filter, s.created_at, s.last_active, s.tasks_completed,
             s.satisfaction_score, s.prompt_version),
        )
        con.commit()
        con.close()
