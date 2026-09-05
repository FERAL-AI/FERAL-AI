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
import re
import time
from typing import Optional, Any, Callable, Awaitable
from uuid import uuid4
from dataclasses import dataclass, field

from agents.turn_attribution import (
    accumulate_turn_usage,
    merge_turn_usage,
    model_of_llm_response,
)

from agents.llm_provider import llm_response_error
from skills.call_context import bind_context
from skills.result_budget import serialize_tool_result_with_images
from agents.multimodal_blocks import (
    image_delivery_mode,
    materialize_tool_result_images,
)

logger = logging.getLogger("feral.multi_agent")


@dataclass
class AgentMessage:
    """Message on the inter-agent bus."""
    from_agent: str
    to_agent: str
    content: str
    data: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class MultiAgentProviderError(RuntimeError):
    """The LLM provider failed for every worker that could have answered.

    Raised by ``MultiAgentOrchestrator.run`` instead of returning the
    provider's error text as the reply. ``run`` returns a bare string to
    the orchestrator, so without a distinct exception a 400 from OpenAI
    was indistinguishable from an answer and was rendered, stored and
    replayed as one. The orchestrator catches this type specifically,
    emits an ``error`` frame and does NOT retry on the single-agent
    path (same provider, same failure).
    """


@dataclass
class WorkerResult:
    """Output from a single worker execution."""
    worker_id: str
    text: str = ""
    tool_calls_made: list[dict] = field(default_factory=list)
    tool_results: list[dict] = field(default_factory=list)
    confidence: float = 1.0
    error: str = ""
    # True when ``error`` is the LLM provider's own failure (the
    # ``{"error": ...}`` dict ``LLMProvider.chat`` returns), as opposed
    # to a worker-side exception. ``MultiAgentOrchestrator.run`` raises
    # these instead of returning them as reply text.
    provider_error: bool = False
    # Per-turn attribution for THIS worker: the tokens it burned across
    # all of its rounds, and the model that produced its final text. The
    # orchestrator sums these across workers, because a parallel strategy
    # bills the user for every worker it ran, not just the one whose
    # answer ends up on screen. Empty when the provider reported nothing.
    usage: dict = field(default_factory=dict)
    model: str = ""


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
        orchestrator=None,
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
        self._orchestrator = orchestrator

    def _gate_tool_call(
        self, tool_name: str, args: dict, session_id: str
    ) -> Optional[dict]:
        """Run ToolRunner's gates for a call this path is about to execute.

        ``AgentWorker`` calls ``SkillExecutor.execute`` directly, so it
        never reaches ``ToolRunner``, which owns plan mode AND the
        safety/approval gate. Both were therefore skipped on the primary
        text chat path, since ``features.multi_agent`` defaults to True.

        Verified live before this existed: with plan mode active and
        autonomy set to ``strict``, ``feral_reminders__create`` executed
        and created the reminder, with no plan-mode refusal and no
        approval frame, while ``resolve_policy`` for that tool returns
        ``confirm``.

        Returns a refusal envelope to use as the tool result, or None to
        proceed. Gates are reached through the orchestrator rather than a
        new dependency so this stays a lookup, not a rewiring. A missing
        runner returns None: the older behaviour, not a new failure mode.
        """
        runner = getattr(self._orchestrator, "tool_runner", None)
        if runner is None:
            return None
        for gate_name, gate_args in (
            ("enforce_plan_mode", (tool_name, session_id)),
            ("enforce_safety", (tool_name, args, session_id)),
        ):
            gate = getattr(runner, gate_name, None)
            if not callable(gate):
                continue
            try:
                refusal = gate(*gate_args)
            except Exception:
                logger.exception(
                    "multi-agent %s check failed for %s", gate_name, tool_name
                )
                continue
            # Must be a real refusal envelope, not merely non-None. A
            # MagicMock runner returns a MagicMock from every call, which
            # is truthy, so a None check alone blocks every tool call in
            # every test that uses a mock orchestrator. This exact bug
            # already bit the voice barge-in path earlier; a dict check is
            # the fix that holds.
            if isinstance(refusal, dict):
                logger.info(
                    "multi-agent refused %s via %s", tool_name, gate_name
                )
                return refusal
        return None

    def get_tools(self) -> list[dict]:
        """Tools this worker may call this turn.

        The availability gate matters most here. The "general" worker is
        configured with ``skill_ids=[]`` and therefore falls through to
        the whole registry, so it is the worker that was being handed all
        266 schemas including the 79 with no key, no OAuth, no Docker and
        no robot behind them. See ``skills/availability.py``.
        """
        if not self._skills:
            return []
        from skills.availability import filter_unavailable_tools

        tools = []
        for sid in self.skill_ids:
            skill = self._skills.skills.get(sid)
            if skill:
                tools.extend(self._skills._manifest_to_tools(skill))
        if not tools:
            tools = self._skills.get_all_tools()
        return filter_unavailable_tools(tools)

    async def run(self, session_id: str, user_text: str, context: str = "") -> WorkerResult:
        if not self._llm or not self._llm.available:
            return WorkerResult(worker_id=self.worker_id, error="LLM not available")

        # Per-worker attribution, summed over every round this worker runs
        # (tool rounds, the empty-reply nudge, and the synthesis pass). All
        # of them are billed, so all of them count.
        w_usage: dict = {}
        w_model = ""

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

        # Tool-result images ride out of band, keyed by tool_call_id, and
        # are spliced into a per-request copy of ``messages`` just before
        # each chat call. Keeping them out of the message list means this
        # worker's history stays text-only and provider-agnostic, so it
        # survives a failover to a provider that cannot take images.
        _images_by_call_id: dict[str, dict] = {}

        def _materialize(msgs: list) -> list:
            """Per-request view of ``msgs`` with images spliced in."""
            if not _images_by_call_id:
                return msgs
            provider = str(getattr(self._llm, "provider", "") or "")
            vision_ok = True
            status = getattr(self._llm, "_vision_support_status", None)
            if callable(status):
                try:
                    vision_ok = bool(status()[0])
                except Exception:
                    vision_ok = False
            try:
                return materialize_tool_result_images(
                    msgs,
                    _images_by_call_id,
                    image_delivery_mode(provider, vision_supported=vision_ok),
                )
            except Exception:
                logger.warning(
                    "worker tool-result image materialization failed; text only",
                    exc_info=True,
                )
                return msgs

        messages = [
            {"role": "system", "content": full_prompt},
            {"role": "user", "content": user_text},
        ]

        tool_calls_made = []
        tool_results = []
        nudged_for_text = False

        # v2026.6.11 — UNLIMITED rounds by default (was a hard-coded 4,
        # which cut closed-loop hardware tasks off mid-verify). A limit is
        # user-set only (agents.max_tool_iterations / FERAL_MAX_ITERATIONS);
        # runaway loops are stopped by the no-progress guard + wall clock.
        from agents.iteration_budget import (
            GUARD_STOP,
            GUARD_WARN,
            IterationBudget,
            NO_PROGRESS_GUIDANCE,
            NO_PROGRESS_WARNING,
            resolve_max_tool_iterations,
            resolve_tool_loop_max_seconds,
        )
        budget = IterationBudget(
            resolve_max_tool_iterations(), resolve_tool_loop_max_seconds()
        )
        # Same two-level guard as the single-agent loop: a worker that hits
        # one broken tool keeps the rest of its toolset and gets a warning
        # first; only a genuine spin withdraws tools.
        final_answer_only = False
        no_progress_warned = False

        while budget.start_iteration():
            try:
                forced_tool = None
                if self._orchestrator is not None:
                    try:
                        forced_tool = self._orchestrator._force_tool_for_query(
                            user_text, tools, session_id,
                        )
                    except Exception:
                        forced_tool = None

                # Explicit max_tokens: the provider default is 1024, which
                # hard-truncates long answers mid-sentence on every surface
                # that rides the multi-agent path (iOS chat, voice). 4096
                # matches the single-agent orchestrator's chat budget.
                response = await self._llm.chat(
                    messages=_materialize(messages),
                    tools=None if final_answer_only else (tools if tools else None),
                    max_tokens=4096,
                    force_tool=forced_tool,
                )
                accumulate_turn_usage(w_usage, response)
                _m = model_of_llm_response(response)
                if _m:
                    w_model = _m

                # A provider failure ends the worker with an ERROR, not
                # with the failure text as its answer. ``run`` below
                # turns it into ``MultiAgentProviderError`` so the
                # orchestrator emits an error frame instead of an
                # assistant bubble that then lands in the transcript.
                provider_error = llm_response_error(response)
                if provider_error:
                    logger.error(
                        "Worker %s: LLM provider failed: %s",
                        self.worker_id, provider_error,
                    )
                    return WorkerResult(
                        worker_id=self.worker_id,
                        error=provider_error,
                        provider_error=True,
                        tool_calls_made=tool_calls_made,
                        tool_results=tool_results,
                        usage=w_usage,
                        model=w_model,
                    )

                text_content, tool_calls = self._llm.extract_response(response)

                if tool_calls and final_answer_only:
                    # Loop guard already withdrew tools and the model is
                    # still trying to call one — stop and synthesize from
                    # whatever tool results exist.
                    break

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
                                    t0 = time.time()
                                    # Gate before executing. This path calls
                                    # SkillExecutor directly and never reaches
                                    # ToolRunner, which owns both plan mode and
                                    # the safety/approval gate. Proven live: with
                                    # plan mode active and autonomy set to strict,
                                    # feral_reminders__create ran and created the
                                    # reminder with no refusal and no approval
                                    # frame. features.multi_agent defaults True,
                                    # so this is the primary text chat path, not
                                    # an edge case.
                                    _gate = self._gate_tool_call(
                                        tc["name"], tc.get("args") or {}, session_id
                                    )
                                    if _gate is not None:
                                        result = _gate
                                    else:
                                        # Bind so the executor's audit row
                                        # carries the session. Without it a
                                        # multi-agent tool call lands in
                                        # execution_log with session "".
                                        with bind_context(
                                            session_id=session_id,
                                            surface="multi_agent",
                                            tool_name=tc["name"],
                                            call_id=str(tc.get("id") or ""),
                                        ):
                                            result = await self._executor.execute(tc["name"], tc["args"], skill, endpoint)
                                    tool_results.append(result)
                                    if self._orchestrator is not None:
                                        try:
                                            await self._orchestrator._emit_tool_result(
                                                session_id,
                                                tc,
                                                result,
                                                (time.time() - t0) * 1000.0,
                                            )
                                        except Exception:
                                            logger.debug(
                                                "multi-agent tool_result emit failed",
                                                exc_info=True,
                                            )
                                    _tool_text, _tool_images = (
                                        serialize_tool_result_with_images(
                                            tc["name"],
                                            result.get("data") or result,
                                            registry=self._skills,
                                        )
                                    )
                                    messages.append({
                                        "role": "tool",
                                        "tool_call_id": tc.get("id", str(uuid4())[:8]),
                                        "name": tc["name"],
                                        # Fourth site of the same blind
                                        # [:2000] slice. Workers read files
                                        # too, so they share the per-tool
                                        # budget (skills/result_budget.py).
                                        # serialize_tool_result stringified the
                                        # whole envelope, so a screenshot (about
                                        # 400 000 base64 chars) was clamped to the
                                        # per-tool character budget and the worker
                                        # was handed a truncated blob it could not
                                        # decode. Images now travel out of band
                                        # and are spliced in at request-build time
                                        # (_materialize below), which keeps this
                                        # message list provider-agnostic.
                                        "content": _tool_text,
                                    })
                                    if _tool_images:
                                        # ``.to_dict()`` matters: materialize
                                        # rebuilds each image with
                                        # ToolResultImage.from_dict and filters
                                        # on isinstance(raw, dict), so storing
                                        # the objects themselves yields zero
                                        # images with no error raised. Same
                                        # shape the orchestrator stores.
                                        _images_by_call_id[
                                            str(tc.get("id", "")) or tc["name"]
                                        ] = {
                                            "images": [
                                                img.to_dict() for img in _tool_images
                                            ],
                                            "pruned": False,
                                            "tool_name": tc["name"],
                                        }
                                    guard_level = budget.observe_tool(
                                        tc["name"], tc.get("args", {}),
                                        bool(result.get("success")), result,
                                    )
                                    if guard_level == GUARD_STOP:
                                        final_answer_only = True
                                    elif (
                                        guard_level == GUARD_WARN
                                        and not no_progress_warned
                                    ):
                                        no_progress_warned = True
                                        messages.append({
                                            "role": "system",
                                            "content": NO_PROGRESS_WARNING,
                                        })
                    if final_answer_only:
                        messages.append({"role": "system", "content": NO_PROGRESS_GUIDANCE})
                    continue

                if text_content:
                    return WorkerResult(
                        worker_id=self.worker_id,
                        text=text_content,
                        tool_calls_made=tool_calls_made,
                        tool_results=tool_results,
                        usage=w_usage,
                        model=w_model,
                    )

                # Empty body with no tool calls (reasoning-only / refusal
                # artifacts). Nudge once for a plain-text answer instead of
                # surfacing "No response generated." to the user.
                if not nudged_for_text:
                    nudged_for_text = True
                    messages.append({
                        "role": "user",
                        "content": (
                            "(Your previous reply was empty. Respond now with "
                            "your final answer as plain text.)"
                        ),
                    })
                    continue
                break

            except Exception as e:
                logger.error(f"Worker {self.worker_id} error: {e}")
                # Rounds completed before the failure were still billed.
                return WorkerResult(
                    worker_id=self.worker_id, error=str(e),
                    usage=w_usage, model=w_model,
                )

        # Loop exhausted. If tools actually ran, synthesize a grounded
        # summary from their results rather than dropping the work on the
        # floor — this is the "did 3 tool rounds, never produced prose" case.
        if tool_results:
            try:
                messages.append({
                    "role": "user",
                    "content": (
                        "(Summarize the outcome of the actions you just "
                        "performed for the user, in plain text.)"
                    ),
                })
                response = await self._llm.chat(
                    messages=_materialize(messages), tools=None, max_tokens=2048
                )
                accumulate_turn_usage(w_usage, response)
                _m = model_of_llm_response(response)
                if _m:
                    w_model = _m
                text_content, _ = self._llm.extract_response(response)
                if text_content:
                    return WorkerResult(
                        worker_id=self.worker_id,
                        text=text_content,
                        tool_calls_made=tool_calls_made,
                        tool_results=tool_results,
                        usage=w_usage,
                        model=w_model,
                    )
            except Exception as e:
                logger.error(f"Worker {self.worker_id} synthesis error: {e}")

        return WorkerResult(
            worker_id=self.worker_id,
            text="Something went wrong and I couldn't generate a reply — please try that again.",
            tool_calls_made=tool_calls_made,
            usage=w_usage,
            model=w_model,
        )


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

    # Coding / filesystem / desktop-action requests must go to the
    # `general` worker — it's the ONLY worker whose tool set includes
    # `computer_use__*` (write_file, bash, open app, browser). The
    # specialist keyword lists otherwise misfire on these ("build it on
    # my desktop as an html" → home), and the specialists then refuse
    # with "my tools only let me control smart home devices".
    GENERAL_OVERRIDE_RE = re.compile(
        r"\b(html|css|javascript|python|json|code|coding|script|program|"
        r"file|files|folder|directory|desktop|terminal|command|shell)\b",
        re.IGNORECASE,
    )
    # Robot/CuteBot LED commands must NOT route to the home worker — it
    # only exposes smart_home_hue (Philips/HA bridge lights). The general
    # worker carries the full tool catalog including cutebot__set_lights.
    ROBOT_LIGHTS_OVERRIDE_RE = re.compile(
        r"\b(?:robot|cutebot|qtbot)\b.{0,40}\b(?:light|lights|led|glow|color|colour|"
        r"red|green|blue|off|on|dim|bright)\b|"
        r"\b(?:light|lights|led|glow|color|colour|red|green|blue|off|on|dim|bright)\b"
        r".{0,40}\b(?:robot|cutebot|qtbot)\b",
        re.IGNORECASE,
    )

    def __init__(self, llm=None):
        self._llm = llm
        # Token usage of the classifier call from the most recent
        # ``route``. Empty when the keyword fast paths answered without
        # calling the LLM at all, which is the common case.
        self.last_usage: dict = {}

    async def route(self, text: str) -> dict:
        """
        Returns: {"workers": ["health", "home", ...], "strategy": "single"|"parallel"|"sequential"}
        """
        # Cleared per call: the keyword guards below return without ever
        # reaching the classifier, and a stale tally from the previous
        # turn must not be billed to this one.
        self.last_usage = {}
        # Hard guard BEFORE any classifier: coding/file/desktop work can
        # only be served by the general worker (full tool set). Routing it
        # to a specialist guarantees a refusal.
        if self.GENERAL_OVERRIDE_RE.search(text):
            return {"workers": ["general"], "strategy": "single"}

        if self.ROBOT_LIGHTS_OVERRIDE_RE.search(text):
            return {"workers": ["general"], "strategy": "single"}

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
            # Word-boundary matching — bare substring containment misfired
            # constantly ("ac" in "exact", "play" in "display", "fan" in
            # "fantastic") and dragged unrelated requests to specialists.
            score = sum(
                1 for kw in info["keywords"]
                if re.search(rf"\b{re.escape(kw)}\b", text_lower)
            )
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
            "Route the user's request to the worker(s) whose TOOLS can serve it:\n"
            "- health: biometrics only (heart rate, SpO2, sleep, fitness, wellness).\n"
            "- home: smart-home DEVICES only (lights, locks, thermostat, scenes via Home Assistant). "
            "It has NO file system, NO browser, NO code tools. NOT for CuteBot/QtBot robot LED "
            "commands — those use cutebot__set_lights via the general worker.\n"
            "- research: web search, news, notes/documents lookup.\n"
            "- creative: music/media playback, calendar, reminders.\n"
            "- general: EVERYTHING else — including writing code or files, building HTML/apps, "
            "opening applications, running commands, anything on the user's computer or desktop, "
            "and plain conversation. It has the full tool set.\n"
            "When unsure, choose general — a specialist with the wrong tools must refuse, "
            "general never has to.\n"
            "Return JSON: {\"workers\": [\"category1\"], \"strategy\": \"single\"} or "
            "{\"workers\": [\"cat1\", \"cat2\"], \"strategy\": \"parallel\"}\n\n"
            f"User: {text}\n\nJSON:"
        )
        response = await self._llm.chat(
            [{"role": "user", "content": prompt}],
            tools=None, temperature=0.1, max_tokens=100,
        )
        # Cheap, but not free: the classifier runs on every multi-agent
        # turn, so its tokens belong in the turn total.
        accumulate_turn_usage(self.last_usage, response)
        text_content, _ = self._llm.extract_response(response)
        # ``None`` on a provider failure; the caller's ``except`` falls
        # back to keyword routing either way.
        cleaned = (text_content or "").strip()
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
        orchestrator=None,
    ):
        self._llm = llm
        self._skills = skill_registry
        self._executor = skill_executor
        self._memory = memory
        self._perception = perception
        self._send = send_to_client
        self._orchestrator = orchestrator
        self._bus = AgentBus()
        self._router = AgentRouter(llm=llm)
        self._workers: dict[str, AgentWorker] = {}
        # session_id -> {"model", "usage"} for the turn that just ran.
        # Consumed by ``pop_turn_attribution``; see ``run``.
        self._turn_attribution: dict[str, dict] = {}
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
                orchestrator=self._orchestrator,
            )
            self._workers[wid] = worker
            self._bus.register(wid)

    async def run(self, session_id: str, text: str, context: Optional[dict] = None) -> str:
        # Per-turn attribution for the whole multi-agent turn. Stashed on
        # the instance keyed by session rather than returned, because
        # ``run`` returns bare text to two orchestrator call sites and
        # widening that signature would ripple through every caller and
        # test. ``pop_turn_attribution`` consumes it, so a later turn can
        # never re-read a stale tally.
        turn_usage: dict = {}
        turn_model = ""

        routing = await self._router.route(text)
        # The router is a real (cheap) LLM call and is billed like any
        # other. Leaving it out would under-report every multi-agent turn.
        merge_turn_usage(turn_usage, getattr(self._router, "last_usage", {}) or {})
        worker_ids = routing.get("workers", ["general"])
        strategy = routing.get("strategy", "single")

        logger.info(f"Multi-agent routing: {worker_ids} ({strategy})")

        workers = [self._workers.get(wid, self._workers["general"]) for wid in worker_ids]

        # Temporal-recall parity with the single-agent path: always mount
        # the fused-timeline side-channel on ``_R_TEMPORAL`` matches —
        # even when force_tool pins fused_timeline, models still pick
        # search_notes unless the widget is already on screen.
        if self._orchestrator is not None:
            try:
                if self._orchestrator._R_TEMPORAL.search(text or ""):
                    await self._orchestrator._maybe_emit_temporal_timeline(
                        session_id, text,
                    )
            except Exception:
                logger.debug(
                    "multi-agent temporal side-channel skipped",
                    exc_info=True,
                )

        if strategy == "parallel" and len(workers) > 1:
            tasks = [w.run(session_id, text) for w in workers]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            valid_results = []
            for r in results:
                if isinstance(r, WorkerResult):
                    valid_results.append(r)
                elif isinstance(r, Exception):
                    logger.error(f"Worker exception: {r}")
            # Every worker that ran is billed, including ones whose text
            # the merger drops. Attribute the model of the first worker
            # that produced one; with a parallel strategy there is no
            # single answering model, and naming one is closer to the
            # truth than naming none.
            for r in valid_results:
                merge_turn_usage(turn_usage, r.usage)
                if not turn_model and r.model:
                    turn_model = r.model
            self._stash_turn_attribution(session_id, turn_model, turn_usage)
            # No worker produced text and at least one hit a provider
            # failure: raise, so the failure is delivered as an error
            # frame. ``ResponseMerger.merge`` would otherwise hand the
            # first error string back as the reply.
            if not any(r.text for r in valid_results):
                provider_failures = [
                    r.error for r in valid_results if r.provider_error and r.error
                ]
                if provider_failures:
                    raise MultiAgentProviderError(provider_failures[0])
            return ResponseMerger.merge(valid_results)
        else:
            result = await workers[0].run(session_id, text)
            merge_turn_usage(turn_usage, result.usage)
            if result.model:
                turn_model = result.model
            self._stash_turn_attribution(session_id, turn_model, turn_usage)
            if not result.text and result.provider_error and result.error:
                raise MultiAgentProviderError(result.error)
            return result.text if result.text else (result.error or "No response.")

    # Entries are normally popped by the orchestrator on the same turn, but
    # a turn that produced no text never reaches the pop, so the map is
    # bounded here rather than trusted to drain. One small dict per session
    # is not a leak worth a session-lifecycle hook; unbounded growth on a
    # long-lived brain is.
    _ATTRIBUTION_MAX_SESSIONS = 256

    def _stash_turn_attribution(self, session_id: str, model: str, usage: dict) -> None:
        if (
            len(self._turn_attribution) >= self._ATTRIBUTION_MAX_SESSIONS
            and session_id not in self._turn_attribution
        ):
            # dicts preserve insertion order, so this evicts the least
            # recently stashed session.
            self._turn_attribution.pop(next(iter(self._turn_attribution)), None)
        self._turn_attribution[session_id] = {
            "model": model or "",
            "usage": dict(usage or {}),
        }

    def pop_turn_attribution(self, session_id: str) -> dict:
        """Consume the attribution recorded by the last ``run`` for a session.

        Popping (not peeking) is deliberate: an unconsumed entry would be
        re-read by a later turn that produced no numbers of its own, which
        would show the user a token count belonging to a different message.
        """
        return self._turn_attribution.pop(session_id, {}) or {}

    @property
    def stats(self) -> dict:
        return {
            "workers": list(self._workers.keys()),
            "bus_messages": len(self._bus._log),
        }
