"""
FERAL Multi-Agent Collaboration — Router-Worker Architecture
===============================================================
Replaces the single-loop orchestrator with a router that dispatches
to specialist workers.  The router is a fast/cheap LLM call that
classifies intent; workers have specialized prompts and tool subsets.

Workers can run in parallel for complex multi-domain queries.
An AgentBus allows inter-worker communication.
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import time
from typing import Optional, Any, Callable, Awaitable
from uuid import uuid4
from dataclasses import dataclass, field

logger = logging.getLogger("feral.multi_agent")


@dataclass
class AgentMessage:
    """Message on the inter-agent bus."""
    from_agent: str
    to_agent: str
    content: str
    data: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


@dataclass
class WorkerResult:
    """Output from a single worker execution."""
    worker_id: str
    text: str = ""
    tool_calls_made: list[dict] = field(default_factory=list)
    tool_results: list[dict] = field(default_factory=list)
    confidence: float = 1.0
    error: str = ""


class AgentBus:
    """Simple asyncio-based inter-agent message bus."""

    def __init__(self):
        self._queues: dict[str, asyncio.Queue] = {}
        self._log: list[AgentMessage] = []

    def register(self, agent_id: str):
        if agent_id not in self._queues:
            self._queues[agent_id] = asyncio.Queue()

    async def post(self, msg: AgentMessage):
        self._log.append(msg)
        q = self._queues.get(msg.to_agent)
        if q:
            await q.put(msg)

    async def receive(self, agent_id: str, timeout: float = 0.1) -> Optional[AgentMessage]:
        q = self._queues.get(agent_id)
        if not q:
            return None
        try:
            return await asyncio.wait_for(q.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    @property
    def message_log(self) -> list[dict]:
        return [{"from": m.from_agent, "to": m.to_agent, "content": m.content[:100]} for m in self._log[-50:]]


class AgentWorker:
    """
    A specialist agent with its own system prompt and tool subset.
    Wraps an LLM call with domain-specific context.
    """

    def __init__(
        self,
        worker_id: str,
        name: str,
        system_prompt: str,
        skill_ids: list[str],
        *,
        llm=None,
        skill_registry=None,
        skill_executor=None,
        memory=None,
        perception=None,
        bus: AgentBus = None,
    ):
        self.worker_id = worker_id
        self.name = name
        self.system_prompt = system_prompt
        self.skill_ids = skill_ids
        self._llm = llm
        self._skills = skill_registry
        self._executor = skill_executor
        self._memory = memory
        self._perception = perception
        self._bus = bus

    def get_tools(self) -> list[dict]:
        if not self._skills:
            return []
        tools = []
        for sid in self.skill_ids:
            skill = self._skills.skills.get(sid)
            if skill:
                from skills.registry import SkillRegistry
                tools.extend(self._skills._manifest_to_tools(skill))
        if not tools:
            return self._skills.get_all_tools()
        return tools

    async def run(self, session_id: str, user_text: str, context: str = "") -> WorkerResult:
        if not self._llm or not self._llm.available:
            return WorkerResult(worker_id=self.worker_id, error="LLM not available")

        tools = self.get_tools()
        perception_ctx = ""
        if self._perception:
            frame = self._perception.get_frame(session_id)
            perception_ctx = frame.to_system_context()

        memory_ctx = ""
        if self._memory:
            memory_ctx = await self._memory.build_context_for_llm(session_id, max_tokens_budget=300)

        full_prompt = self.system_prompt
        if perception_ctx:
            full_prompt += f"\n\n[Environment]\n{perception_ctx}"
        if memory_ctx:
            full_prompt += f"\n\n[Memory]\n{memory_ctx}"
        if context:
            full_prompt += f"\n\n[Additional Context]\n{context}"

        messages = [
            {"role": "system", "content": full_prompt},
            {"role": "user", "content": user_text},
        ]

        tool_calls_made = []
        tool_results = []

        for iteration in range(3):
            try:
                response = await self._llm.chat(messages=messages, tools=tools if tools else None)
                text_content, tool_calls = self._llm.extract_response(response)

                if tool_calls and self._executor:
                    assistant_msg = {"role": "assistant"}
                    if text_content:
                        assistant_msg["content"] = text_content
                    if "choices" in response and response["choices"]:
                        raw_msg = response["choices"][0].get("message", {})
                        if raw_msg.get("tool_calls"):
                            assistant_msg["tool_calls"] = raw_msg["tool_calls"]
                    messages.append(assistant_msg)

                    for tc in tool_calls:
                        tool_calls_made.append(tc)
                        parts = tc["name"].split("__", 1)
                        if len(parts) == 2:
                            skill_id, endpoint_id = parts
                            skill = self._skills.skills.get(skill_id) if self._skills else None
                            if skill:
                                endpoint = next((ep for ep in skill.endpoints if ep.id == endpoint_id), None)
                                if endpoint:
                                    result = await self._executor.execute(tc["name"], tc["args"], skill, endpoint)
                                    tool_results.append(result)
                                    messages.append({
                                        "role": "tool",
                                        "tool_call_id": tc.get("id", str(uuid4())[:8]),
                                        "name": tc["name"],
                                        "content": json.dumps(result.get("data") or result, default=str)[:2000],
                                    })
                    continue

                if text_content:
                    return WorkerResult(
                        worker_id=self.worker_id,
                        text=text_content,
                        tool_calls_made=tool_calls_made,
                        tool_results=tool_results,
                    )
                break

            except Exception as e:
                logger.error(f"Worker {self.worker_id} error: {e}")
                return WorkerResult(worker_id=self.worker_id, error=str(e))

        return WorkerResult(worker_id=self.worker_id, text="No response generated.", tool_calls_made=tool_calls_made)


class AgentRouter:
    """
    Fast classifier that decides which workers to invoke.
    Uses a cheap LLM call or keyword heuristics.
    """

    CATEGORIES = {
        "health": {"keywords": ["heart rate", "blood pressure", "spo2", "health", "fitness", "steps", "sleep", "calories", "exercise", "bpm", "oxygen", "temperature", "stress", "wellness"]},
        "home": {"keywords": ["light", "thermostat", "lock", "door", "home", "room", "switch", "plug", "automation", "sensor", "blinds", "curtain", "fan", "heater", "ac", "air conditioning"]},
        "research": {"keywords": ["search", "find", "look up", "what is", "who is", "when did", "how to", "research", "article", "paper", "news", "wikipedia", "web", "google", "note", "notion", "document", "page"]},
        "creative": {"keywords": ["play", "music", "song", "spotify", "playlist", "album", "artist", "pause", "skip", "volume", "queue", "podcast", "radio", "calendar", "schedule", "meeting", "event", "reminder"]},
    }

    def __init__(self, llm=None):
        self._llm = llm

    async def route(self, text: str) -> dict:
        """
        Returns: {"workers": ["health", "home", ...], "strategy": "single"|"parallel"|"sequential"}
        """
        if self._llm and self._llm.available:
            try:
                return await self._route_with_llm(text)
            except Exception:
                pass

        return self._route_with_keywords(text)

    def _route_with_keywords(self, text: str) -> dict:
        text_lower = text.lower()
        scores = {}
        for category, info in self.CATEGORIES.items():
            score = sum(1 for kw in info["keywords"] if kw in text_lower)
            if score > 0:
                scores[category] = score

        if not scores:
            return {"workers": ["general"], "strategy": "single"}

        sorted_cats = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        if len(sorted_cats) >= 2 and sorted_cats[1][1] >= 2:
            return {"workers": [c for c, _ in sorted_cats[:2]], "strategy": "parallel"}

        return {"workers": [sorted_cats[0][0]], "strategy": "single"}

    async def _route_with_llm(self, text: str) -> dict:
        prompt = (
            "Classify the user's intent into one or more categories: health, home, research, creative, general.\n"
            "If the request spans multiple domains, list all relevant ones.\n"
            "Return JSON: {\"workers\": [\"category1\"], \"strategy\": \"single\"} or "
            "{\"workers\": [\"cat1\", \"cat2\"], \"strategy\": \"parallel\"}\n\n"
            f"User: {text}\n\nJSON:"
        )
        response = await self._llm.chat(
            [{"role": "user", "content": prompt}],
            tools=None, temperature=0.1, max_tokens=100,
        )
        text_content, _ = self._llm.extract_response(response)
        cleaned = text_content.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        result = json.loads(cleaned)
        if "workers" in result:
            return result
        return {"workers": ["general"], "strategy": "single"}


class ResponseMerger:
    """Merges results from multiple workers into a coherent response."""

    @staticmethod
    def merge(results: list[WorkerResult]) -> str:
        valid = [r for r in results if r.text and not r.error]
        if not valid:
            errors = [r.error for r in results if r.error]
            return errors[0] if errors else "No response from any worker."

        if len(valid) == 1:
            return valid[0].text

        parts = []
        for r in valid:
            parts.append(r.text)

        return "\n\n".join(parts)


class MultiAgentOrchestrator:
    """
    Top-level multi-agent coordinator.  Replaces the single-loop
    Orchestrator.handle_command_stream when multi-agent is enabled.
    """

    def __init__(
        self,
        *,
        llm=None,
        skill_registry=None,
        skill_executor=None,
        memory=None,
        perception=None,
        send_to_client=None,
    ):
        self._llm = llm
        self._skills = skill_registry
        self._executor = skill_executor
        self._memory = memory
        self._perception = perception
        self._send = send_to_client
        self._bus = AgentBus()
        self._router = AgentRouter(llm=llm)
        self._workers: dict[str, AgentWorker] = {}
        self._init_workers()

    def _init_workers(self):
        from agents.workers.health_worker import HEALTH_PROMPT, HEALTH_SKILLS
        from agents.workers.home_worker import HOME_PROMPT, HOME_SKILLS
        from agents.workers.research_worker import RESEARCH_PROMPT, RESEARCH_SKILLS
        from agents.workers.creative_worker import CREATIVE_PROMPT, CREATIVE_SKILLS

        # The "general" worker is the fallback when the router can't pin a
        # specialist domain. Its job is NOT to answer everything from
        # parametric memory — it's to escalate to the right tool when one
        # exists. Same tool-selection discipline as the orchestrator's
        # master prompt, in compact form.
        general_prompt = (
            "You are FERAL, a personal AI operating system. The user's request "
            "didn't pin to a specialist domain (health/home/research/creative), "
            "so you're handling it directly.\n"
            "\n"
            "Tool discipline:\n"
            "- Personal recall ('what did I…', 'summarize my…', 'what was I "
            "working on') → call `notes_memory__fused_timeline` BEFORE answering.\n"
            "- Current external info ('latest…', 'today's…') → call `web_search`.\n"
            "- Local actions (open app, write file, run command, browse, send "
            "message) → call the matching tool. Don't describe steps the user "
            "should perform when a tool exists.\n"
            "- After each tool returns, ground your reply in its actual output.\n"
            "\n"
            "If the request is genuinely conversational (small talk, opinion, "
            "explanation of a concept the user named), answer directly — but "
            "stay tight, warm, and honest. Don't overclaim ('I synced…') and "
            "don't underclaim ('I can't…') when a tool exists for the action."
        )

        worker_configs = [
            ("health", "Health Specialist", HEALTH_PROMPT, HEALTH_SKILLS),
            ("home", "Home Controller", HOME_PROMPT, HOME_SKILLS),
            ("research", "Research Assistant", RESEARCH_PROMPT, RESEARCH_SKILLS),
            ("creative", "Creative & Media", CREATIVE_PROMPT, CREATIVE_SKILLS),
            ("general", "General Assistant", general_prompt, []),
        ]

        for wid, name, prompt, skills in worker_configs:
            worker = AgentWorker(
                worker_id=wid, name=name, system_prompt=prompt, skill_ids=skills,
                llm=self._llm, skill_registry=self._skills, skill_executor=self._executor,
                memory=self._memory, perception=self._perception, bus=self._bus,
            )
            self._workers[wid] = worker
            self._bus.register(wid)

    async def run(self, session_id: str, text: str, context: Optional[dict] = None) -> str:
        routing = await self._router.route(text)
        worker_ids = routing.get("workers", ["general"])
        strategy = routing.get("strategy", "single")

        logger.info(f"Multi-agent routing: {worker_ids} ({strategy})")

        workers = [self._workers.get(wid, self._workers["general"]) for wid in worker_ids]

        if strategy == "parallel" and len(workers) > 1:
            tasks = [w.run(session_id, text) for w in workers]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            valid_results = []
            for r in results:
                if isinstance(r, WorkerResult):
                    valid_results.append(r)
                elif isinstance(r, Exception):
                    logger.error(f"Worker exception: {r}")
            return ResponseMerger.merge(valid_results)
        else:
            result = await workers[0].run(session_id, text)
            return result.text if result.text else (result.error or "No response.")

    @property
    def stats(self) -> dict:
        return {
            "workers": list(self._workers.keys()),
            "bus_messages": len(self._bus._log),
        }
