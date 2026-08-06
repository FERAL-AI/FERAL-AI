"""Intent Compiler — high-level goal declaration into persistent execution plans."""
from __future__ import annotations
import json
import logging
import time
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from uuid import uuid4

logger = logging.getLogger("feral.intent_compiler")

@dataclass
class MicroAction:
    action_id: str = field(default_factory=lambda: str(uuid4())[:8])
    description: str = ""
    tool_hint: str = ""
    scheduled_time: Optional[str] = None
    completed: bool = False
    completed_at: Optional[float] = None
    result_summary: str = ""
    difficulty: float = 0.5  # 0-1, adapts

@dataclass
class ExecutionPlan:
    plan_id: str = field(default_factory=lambda: str(uuid4())[:8])
    intent: str = ""
    goal_description: str = ""
    micro_actions: list[MicroAction] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_evaluated: float = 0.0
    progress: float = 0.0  # 0-1
    status: str = "active"  # active, paused, completed, abandoned
    adaptations: int = 0

class IntentCompiler:
    def __init__(self, llm=None, db_path: Optional[str] = None, skill_registry=None,
                 user_timezone: str = "UTC"):
        self._llm = llm
        self._plans: dict[str, ExecutionPlan] = {}
        self._db_path = db_path
        self._skill_registry = skill_registry
        self._user_timezone = user_timezone
        self._rejected_actions: list[dict] = []
        if db_path:
            self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS intent_plans (
                    plan_id TEXT PRIMARY KEY,
                    intent TEXT NOT NULL,
                    goal_description TEXT,
                    status TEXT DEFAULT 'active',
                    progress REAL DEFAULT 0.0,
                    created_at REAL,
                    last_evaluated REAL,
                    adaptations INTEGER DEFAULT 0,
                    micro_actions TEXT
                )
            """)
            conn.commit()
        finally:
            conn.close()
        self._load_plans()

    def _load_plans(self):
        if not self._db_path:
            return
        conn = sqlite3.connect(self._db_path)
        try:
            rows = conn.execute("SELECT * FROM intent_plans WHERE status = 'active'").fetchall()
            for r in rows:
                actions = json.loads(r[8]) if r[8] else []
                plan = ExecutionPlan(
                    plan_id=r[0], intent=r[1], goal_description=r[2] or "",
                    status=r[3], progress=r[4], created_at=r[5] or 0,
                    last_evaluated=r[6] or 0, adaptations=r[7] or 0,
                    micro_actions=[MicroAction(**a) for a in actions],
                )
                self._plans[plan.plan_id] = plan
        finally:
            conn.close()

    def _save_plan(self, plan: ExecutionPlan):
        if not self._db_path:
            return
        conn = sqlite3.connect(self._db_path)
        try:
            actions_json = json.dumps([
                {"action_id": a.action_id, "description": a.description, "tool_hint": a.tool_hint,
                 "scheduled_time": a.scheduled_time, "completed": a.completed,
                 "completed_at": a.completed_at, "result_summary": a.result_summary,
                 "difficulty": a.difficulty}
                for a in plan.micro_actions
            ])
            conn.execute(
                "INSERT OR REPLACE INTO intent_plans VALUES (?,?,?,?,?,?,?,?,?)",
                (plan.plan_id, plan.intent, plan.goal_description, plan.status,
                 plan.progress, plan.created_at, plan.last_evaluated, plan.adaptations,
                 actions_json),
            )
            conn.commit()
        finally:
            conn.close()

    def _validate_action(self, action: dict, skill_registry=None) -> tuple[bool, str]:
        """Check that action.tool references a real skill endpoint."""
        tool = action.get("tool", "")
        if not tool:
            return False, "empty tool"
        if tool == "manual":
            return True, "ok"
        parts = tool.split(".")
        if len(parts) < 2:
            return False, f"tool must be skill.endpoint, got {tool}"
        skill_id = parts[0]
        if skill_registry:
            skills = getattr(skill_registry, "skills", {})
            if skills and skill_id not in skills:
                return False, f"unknown skill: {skill_id}"
        return True, "ok"

    async def compile_intent(self, intent: str) -> ExecutionPlan:
        plan = ExecutionPlan(intent=intent)

        if self._llm:
            try:
                prompt = (
                    f"Break down this goal into 5-10 specific daily micro-actions:\n\n"
                    f"Goal: {intent}\n\n"
                    f"Return a JSON array of objects with fields:\n"
                    f'- "description": what to do (1 sentence)\n'
                    f'- "tool": which tool/skill to use in skill.endpoint format (or "manual")\n'
                    f'- "difficulty": 0.0-1.0 how hard\n\n'
                    f"Return ONLY valid JSON array."
                )
                response = await self._llm.chat([
                    {"role": "system", "content": "You create actionable execution plans. Return only JSON."},
                    {"role": "user", "content": prompt},
                ])
                text, _ = self._llm.extract_response(response)
                text = text.strip()
                if text.startswith("```"):
                    text = text.split("\n", 1)[1].rsplit("```", 1)[0]

                actions_data = json.loads(text)
                valid_actions = []
                for a in actions_data[:10]:
                    tool_ref = a.get("tool", a.get("tool_hint", "manual"))
                    ok, reason = self._validate_action(
                        {"tool": tool_ref}, self._skill_registry,
                    )
                    if ok:
                        valid_actions.append(
                            MicroAction(
                                description=a.get("description", ""),
                                tool_hint=tool_ref,
                                difficulty=min(1, max(0, float(a.get("difficulty", 0.5)))),
                            )
                        )
                    else:
                        logger.warning("Intent action rejected (%s): %s", reason, a.get("description", "")[:80])
                        self._rejected_actions.append({"action": a, "reason": reason})
                plan.micro_actions = valid_actions if valid_actions else [
                    MicroAction(description=intent, tool_hint="manual")
                ]
                plan.goal_description = f"Compiled from intent: {intent}"
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning("Intent compilation JSON parse failed, single-action fallback: %s", e)
                plan.micro_actions = [MicroAction(description=intent, tool_hint="manual")]
                plan.goal_description = intent
            except Exception as e:
                logger.warning(f"Intent compilation failed, creating basic plan: {e}")
                plan.micro_actions = [MicroAction(description=intent, tool_hint="manual")]
                plan.goal_description = intent
        else:
            plan.micro_actions = [MicroAction(description=intent, tool_hint="manual")]
            plan.goal_description = intent

        self._plans[plan.plan_id] = plan
        self._save_plan(plan)
        return plan

    def complete_action(self, plan_id: str, action_id: str, result: str = "") -> bool:
        plan = self._plans.get(plan_id)
        if not plan:
            return False
        for action in plan.micro_actions:
            if action.action_id == action_id:
                action.completed = True
                action.completed_at = time.time()
                action.result_summary = result
                break
        plan.progress = sum(1 for a in plan.micro_actions if a.completed) / max(1, len(plan.micro_actions))
        if plan.progress >= 1.0:
            plan.status = "completed"
        plan.last_evaluated = time.time()
        self._save_plan(plan)
        return True

    def get_completed_today(self, tz_name: str | None = None) -> list[dict]:
        """Actions finished today, the mirror of get_today_actions.

        /api/ambient/wind_down has always called this behind a hasattr guard
        that never passed, because the method did not exist, so the evening
        recap reported an empty day however much was done. complete_action
        already stamps completed_at, so the data was there the whole time.

        Unlike get_today_actions this does not skip non-active plans: work
        finished today counts toward today even if finishing it completed the
        plan, which is the common case and precisely the part worth recapping.
        """
        from datetime import datetime
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tz_name or self._user_timezone)
        today = datetime.now(tz).date()

        done = []
        for plan in self._plans.values():
            for action in plan.micro_actions:
                if not action.completed or not action.completed_at:
                    continue
                if datetime.fromtimestamp(action.completed_at, tz).date() != today:
                    continue
                done.append({
                    "plan_id": plan.plan_id,
                    "intent": plan.intent,
                    "action": action.description,
                    "action_id": action.action_id,
                    "completed_at": action.completed_at,
                    "result": action.result_summary,
                })
        done.sort(key=lambda a: a["completed_at"])
        return done

    def get_today_actions(self, tz_name: str | None = None) -> list[dict]:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tz_name or self._user_timezone)
        today = datetime.now(tz).date()

        actions = []
        for plan in self._plans.values():
            if plan.status != "active":
                continue
            for action in plan.micro_actions:
                if action.completed:
                    continue
                if action.scheduled_time:
                    try:
                        sched_dt = datetime.fromisoformat(action.scheduled_time)
                        if hasattr(sched_dt, "date") and sched_dt.date() != today:
                            continue
                    except (ValueError, TypeError):
                        pass
                actions.append({
                    "plan_id": plan.plan_id,
                    "intent": plan.intent,
                    "action": action.description,
                    "action_id": action.action_id,
                    "tool_hint": action.tool_hint,
                    "difficulty": action.difficulty,
                    "progress": plan.progress,
                })
                break  # one action per plan per day
        return actions

    def list_plans(self) -> list[dict]:
        return [
            {"plan_id": p.plan_id, "intent": p.intent, "status": p.status,
             "progress": p.progress, "actions_total": len(p.micro_actions),
             "actions_done": sum(1 for a in p.micro_actions if a.completed),
             "created": p.created_at, "adaptations": p.adaptations}
            for p in self._plans.values()
        ]

    def stats(self) -> dict:
        active = [p for p in self._plans.values() if p.status == "active"]
        return {
            "total_plans": len(self._plans),
            "active_plans": len(active),
            "completed_plans": sum(1 for p in self._plans.values() if p.status == "completed"),
            "today_actions": len(self.get_today_actions()),
            "avg_progress": sum(p.progress for p in active) / max(1, len(active)),
        }
