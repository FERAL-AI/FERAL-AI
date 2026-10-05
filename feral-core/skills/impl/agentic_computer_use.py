"""
FERAL Agentic Computer Use — Vision-Action Loop
==================================================
Combines screen capture, VLM analysis, and desktop automation into
an autonomous loop: screenshot -> understand -> act -> verify.

Actions use the existing executor and caller authority. Unsupported
primitives and unavailable authority stop the task without dispatch.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import sys
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from agents.computer_use_driver import (  # boundary-ok: provider-neutral driver lives in agents/ by design (PR 4)
    GUI_ENDPOINT_FOR,
    NormalizedAction,
    gui_args_for,
    normalize_action,
)
from skills.base import BaseSkill
from skills.impl import register_skill
from skills.impl.gui_computer_use import (
    detect_dpi_scale,
)

logger = logging.getLogger("feral.agentic_cu")

MAX_ITERATIONS = 15
SCREENSHOT_DELAY = 0.8


class _ActionStopped(Exception):
    """Preserve a dispatch refusal without retrying the vision loop."""

    def __init__(self, result: dict):
        self.result = result
        super().__init__("Computer-use action did not complete")

ACTION_SYSTEM_PROMPT = """You are an AI agent that controls a computer to accomplish tasks.
You are given a screenshot of the current screen and a task to perform.

Available actions (return EXACTLY ONE as JSON):
- {"action": "click", "x": <int>, "y": <int>, "description": "what you're clicking"}
- {"action": "double_click", "x": <int>, "y": <int>, "description": "what you're double-clicking"}
- {"action": "right_click", "x": <int>, "y": <int>, "description": "what you're right-clicking"}
- {"action": "type", "text": "<string>", "description": "what you're typing"}
- {"action": "key", "keys": "<combo>", "description": "what shortcut"} (e.g. "cmd+c", "enter", "tab")
- {"action": "scroll", "direction": "up"|"down", "amount": <int>, "description": "why scrolling"}
- {"action": "shell", "command": "open -a '<App Name>'", "description": "application to open"}
- {"action": "done", "summary": "what was accomplished"}
- {"action": "failed", "reason": "why it cannot be done"}

Rules:
- Look at the screenshot carefully to determine element positions.
- Click coordinates should target the CENTER of the element you want to interact with.
- After clicking a text field, use "type" to enter text.
- Use "key" for keyboard shortcuts (cmd+a, cmd+v, enter, tab, escape, etc.).
- "shell" only supports opening an application with open -a "App Name".
- Other shell commands and dragging require separately available reviewed tools.
- Return "done" when the task is complete.
- Return "failed" only if the task is truly impossible after trying.
- ALWAYS return valid JSON. Nothing else.
"""


# ── Pydantic models for structured VLM action parsing ─────────────

class ClickAction(BaseModel):
    action: str
    x: int
    y: int
    description: str = ""

class TypeAction(BaseModel):
    action: str
    text: str
    description: str = ""

class KeyAction(BaseModel):
    action: str
    keys: str
    description: str = ""

class ScrollAction(BaseModel):
    action: str
    direction: str
    amount: int = 3
    description: str = ""

class ShellAction(BaseModel):
    action: str
    command: str
    description: str = ""

class DoneAction(BaseModel):
    action: str
    summary: str = ""

class FailedAction(BaseModel):
    action: str
    reason: str = ""


_ACTION_JSON_RE = re.compile(r'\{[^{}]*"action"\s*:\s*"[^"]+?"[^{}]*\}', re.DOTALL)

_ACTION_MODELS = {
    "click": ClickAction,
    "double_click": ClickAction,
    "right_click": ClickAction,
    "type": TypeAction,
    "key": KeyAction,
    "scroll": ScrollAction,
    "shell": ShellAction,
    "done": DoneAction,
    "failed": FailedAction,
}


def parse_vlm_action(raw_text: str) -> Optional[dict]:
    """Parse a VLM response into a validated action dict.

    Strategy:
    1. Try to parse the full text as JSON directly.
    2. Strip markdown fences and retry.
    3. Regex-extract the first JSON object containing "action".
    4. Validate with the appropriate Pydantic model.
    """
    cleaned = raw_text.strip()

    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        start = 1
        if lines[0].strip().startswith("```"):
            start = 1
        end = len(lines)
        for i in range(len(lines) - 1, 0, -1):
            if lines[i].strip() == "```":
                end = i
                break
        inner = lines[start:end]
        if inner and inner[0].strip().lower().startswith("json"):
            inner = inner[1:]
        cleaned = "\n".join(inner).strip()

    candidates: List[str] = [cleaned]

    regex_matches = _ACTION_JSON_RE.findall(raw_text)
    candidates.extend(regex_matches)

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue

        if not isinstance(data, dict) or "action" not in data:
            continue

        action_type = data["action"]
        model_cls = _ACTION_MODELS.get(action_type)
        if model_cls is None:
            return data

        try:
            validated = model_cls.model_validate(data)
            return validated.model_dump()
        except (ValidationError, Exception):
            return data

    return None


@register_skill
class AgenticComputerUseSkill(BaseSkill):
    name = "Agentic Computer Use"
    description = "Autonomous vision-action loop for GUI tasks. Takes screenshots, analyzes them with AI, and performs actions until the task is complete."
    safety_level = "WARN"

    def __init__(self) -> None:
        super().__init__(skill_id="agentic_computer_use")
        self._dpi_scale: Optional[float] = None

    @property
    def dpi_scale(self) -> float:
        if self._dpi_scale is None:
            self._dpi_scale = detect_dpi_scale()
            logger.info("Agentic CU DPI scale: %.1f", self._dpi_scale)
        return self._dpi_scale

    async def execute(self, endpoint_id: str, args: Dict[str, Any], vault: Dict[str, str]) -> Dict[str, Any]:
        if endpoint_id == "execute_task":
            return await self._execute_task(args, vault)
        return {"success": False, "status_code": 404, "data": None, "error": f"Unknown endpoint: {endpoint_id}"}

    async def _execute_task(self, args: dict, vault: dict) -> dict:
        task = args.get("task", "").strip()
        if not task:
            return {"success": False, "status_code": 400, "data": None, "error": "task description is required"}

        authority = self._registered_authority("gui_computer_use", "screenshot")
        if isinstance(authority, dict):
            return authority

        max_steps = min(int(args.get("max_steps", MAX_ITERATIONS)), MAX_ITERATIONS)
        steps_log: list[dict] = []

        llm = await self._get_vlm(vault)
        if not llm:
            return {"success": False, "status_code": 503, "data": None, "error": "No VLM available. Set OPENAI_API_KEY or FERAL_VLM_PROVIDER."}

        for i in range(max_steps):
            try:
                screenshot_b64 = await self._capture_screen(stop_on_refusal=True)
            except _ActionStopped as stopped:
                return {
                    **stopped.result, "success": False,
                    "data": {"completed": False, "steps": len(steps_log), "log": steps_log,
                             "action_result": stopped.result.get("data")},
                }
            if not screenshot_b64:
                steps_log.append({"step": i + 1, "error": "Screenshot capture failed"})
                break

            action = await self._decide_action(llm, task, screenshot_b64, steps_log, vault)
            if not action:
                steps_log.append({"step": i + 1, "error": "VLM returned no valid action"})
                break

            action_type = action.get("action", "")
            step_record = {"step": i + 1, "action": action_type, "detail": action.get("description", "")}

            if action_type == "done":
                step_record["summary"] = action.get("summary", "Task completed")
                steps_log.append(step_record)
                return {
                    "success": True, "status_code": 200,
                    "data": {"completed": True, "steps": len(steps_log), "log": steps_log, "summary": action.get("summary", "")},
                    "error": None,
                }

            if action_type == "failed":
                step_record["reason"] = action.get("reason", "Unknown")
                steps_log.append(step_record)
                return {
                    "success": False, "status_code": 200,
                    "data": {"completed": False, "steps": len(steps_log), "log": steps_log, "reason": action.get("reason", "")},
                    "error": action.get("reason"),
                }

            try:
                result = await self._execute_action(action, stop_on_refusal=True)
            except _ActionStopped as stopped:
                step_record["result"] = stopped.result
                steps_log.append(step_record)
                return {
                    **stopped.result,
                    "success": False,
                    "data": {
                        "completed": False,
                        "steps": len(steps_log),
                        "log": steps_log,
                        "action_result": stopped.result.get("data"),
                    },
                }
            step_record["result"] = result
            steps_log.append(step_record)

            await asyncio.sleep(SCREENSHOT_DELAY)

        return {
            "success": False, "status_code": 200,
            "data": {"completed": False, "steps": len(steps_log), "log": steps_log, "reason": "Max iterations reached"},
            "error": "Max iterations reached without completing task",
        }

    async def _get_vlm(self, vault: dict) -> Optional[Any]:
        """Get an LLM provider that supports vision.

        This used to call ``LLMProvider(provider=, model=, api_key=)``.
        ``LLMProvider.__init__`` takes ``(self)`` and accepts none of those,
        so the call raised TypeError every single time, the except below
        turned it into ``None``, and the caller rendered that as "No VLM
        available. Set OPENAI_API_KEY". A user who had set the key was told
        to set the key, which is why a dead capability was never reported as
        a bug. See AUDIT-FIXES F-16.

        Configuration goes through ``switch_provider``, which is the real
        API for this and is async, hence this method is now async too. The
        only caller was already inside ``async def _execute_task``.
        """
        from agents.llm_provider import LLMProvider

        api_key = (
            vault.get("OPENAI_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or vault.get("ANTHROPIC_API_KEY")
            or os.getenv("ANTHROPIC_API_KEY")
        )
        # Genuinely not configured. Not an error, and deliberately silent:
        # the caller already returns an actionable 503 for this case.
        if not api_key:
            return None

        provider = os.getenv("FERAL_VLM_PROVIDER", "openai")
        model = os.getenv("FERAL_VLM_MODEL", "gpt-4o")
        try:
            llm = LLMProvider()
            await llm.switch_provider(provider, model=model, api_key=api_key)
            return llm
        except Exception as exc:
            # Reached only with a key present, so this is never a
            # configuration problem and must not be reported as one. Kept
            # narrow-in-meaning by the early return above: everything that
            # lands here is a real failure to build a provider.
            logger.warning(
                "VLM construction failed for provider=%s model=%s: %s. "
                "This is not a missing API key; a key was supplied.",
                provider, model, exc, exc_info=True,
            )
            return None

    async def _capture_screen(self, *, stop_on_refusal: bool = False) -> Optional[str]:
        """Use the registered capture primitive and its caller policy."""
        try:
            result = await self._execute_gated("screenshot", {})
            self._stop_if_refused(result, stop_on_refusal=stop_on_refusal)
            data = result.get("data")
            encoded = data.get("image_base64") if isinstance(data, dict) else None
            if not isinstance(encoded, str) or not encoded:
                self._stop_if_refused(self._refusal(
                    "The reviewed screenshot returned no image.",
                    code="computer_use_capture_unavailable",
                ), stop_on_refusal=stop_on_refusal)
                return None
            return encoded
        except _ActionStopped:
            raise
        except Exception as e:
            if stop_on_refusal:
                raise _ActionStopped(self._refusal(
                    "The reviewed screenshot did not complete.",
                    code="computer_use_capture_unavailable",
                )) from e
            logger.error(f"Screenshot failed: {e}")
            return None

    async def _decide_action(self, llm: Any, task: str, screenshot_b64: str, history: list, vault: dict) -> Optional[dict]:
        """Ask the VLM to decide the next action based on the screenshot."""
        history_text = ""
        if history:
            recent = history[-5:]
            history_text = "\n".join(
                f"Step {s['step']}: {s.get('action', '?')} - {s.get('detail', '')}"
                for s in recent
            )

        user_content = [
            {"type": "text", "text": f"Task: {task}"},
        ]
        if history_text:
            user_content.append({"type": "text", "text": f"Previous steps:\n{history_text}"})
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}", "detail": "high"},
        })
        user_content.append({"type": "text", "text": "What is the next action? Return ONLY JSON."})

        try:
            response = await llm.chat(
                messages=[
                    {"role": "system", "content": ACTION_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                temperature=0.1,
                max_tokens=300,
            )
            text, _ = llm.extract_response(response)
            if not text:
                return None

            return parse_vlm_action(text)
        except Exception as e:
            logger.warning(f"VLM action decision failed: {e}")
            return None

    async def _execute_action(self, action: dict, *, stop_on_refusal: bool = False) -> str:
        """Execute a single action on the computer.

        The previous implementation duplicated pyautogui calls and DPI
        scaling here, parallel to ``GUIComputerUseSkill``. With the
        provider-neutral driver in place, every non-shell action is
        normalized once and dispatched through the **single** primitive
        path (`gui_computer_use.execute(...)`), so DPI is applied
        exactly once and the rate limiter / Darwin fallbacks live in
        one module.
        """
        normalized = normalize_action(action)
        if normalized is None:
            action_type = action.get("action") or action.get("type") or "?"
            self._stop_if_refused(self._refusal(
                "Computer-use action could not be validated; nothing was dispatched.",
                code="computer_use_action_unavailable",
            ), stop_on_refusal=stop_on_refusal)
            return f"Unknown action: {action_type}"

        try:
            if normalized.action == "shell":
                return await self._do_shell(normalized.command, stop_on_refusal=stop_on_refusal)

            if normalized.action == "wait":
                ms = max(0, int(normalized.duration_ms))
                await asyncio.sleep(ms / 1000.0)
                return f"Waited {ms}ms"

            if normalized.action == "drag":
                return await self._do_drag(normalized.path, stop_on_refusal=stop_on_refusal)

            return await self._dispatch_via_gui(normalized, stop_on_refusal=stop_on_refusal)
        except _ActionStopped:
            raise
        except Exception as e:
            if stop_on_refusal:
                raise _ActionStopped({
                    "success": False, "status_code": 503, "data": None,
                    "error": "Computer-use dispatch did not return a verified result. Reconcile before retrying.",
                    "error_code": "computer_use_dispatch_unknown",
                    "outcome": "unknown",
                }) from e
            return f"Action failed: {e}"

    async def _dispatch_via_gui(self, action: NormalizedAction, *, stop_on_refusal: bool = False) -> str:
        """Route a normalized action to ``GUIComputerUseSkill`` so DPI,
        rate limiting, and pyautogui/AppleScript fallbacks live in a
        single module. Returns the human-readable message the legacy
        ladder produced so step logs / tests stay stable.
        """
        endpoint_id = GUI_ENDPOINT_FOR.get(action.action)
        if endpoint_id is None:
            self._stop_if_refused(self._refusal(
                "Computer-use action has no registered primitive.",
                code="computer_use_action_unavailable",
            ), stop_on_refusal=stop_on_refusal)
            return f"Unknown action: {action.action}"
        gui_args = gui_args_for(action)

        result = await self._execute_gated(endpoint_id, gui_args)
        self._stop_if_refused(result, stop_on_refusal=stop_on_refusal)
        return self._describe_gui_result(action, result)

    @staticmethod
    def _refusal(reason: str, *, code: str = "computer_use_authority_unavailable") -> dict:
        return {"success": False, "status_code": 503, "data": None,
                "error": reason, "error_code": code}

    @staticmethod
    def _stop_if_refused(result: Any, *, stop_on_refusal: bool) -> None:
        if not stop_on_refusal:
            return
        if not isinstance(result, dict) or result.get("success") is not True:
            if not isinstance(result, dict):
                result = AgenticComputerUseSkill._refusal(
                    "Computer-use dispatch returned no valid result. Reconcile before retrying.",
                    code="computer_use_dispatch_unknown",
                )
                result["outcome"] = "unknown"
            raise _ActionStopped(result)

    async def _execute_gated(self, endpoint_id: str, gui_args: Dict[str, Any]) -> Dict[str, Any]:
        return await self._execute_registered("gui_computer_use", endpoint_id, gui_args)

    async def _execute_registered(self, skill_id: str, endpoint_id: str, args: dict) -> dict:
        """Use the live central executor, never a raw implementation.

        Offline parsing remains available, but autonomous effects require a
        registered endpoint, caller session and policy runner. The approval
        of an outer task does not grant its different inner actions.
        """
        from skills.call_context import current_context

        authority = self._registered_authority(skill_id, endpoint_id)
        if isinstance(authority, dict):
            return authority
        runner, manifest, _endpoint = authority
        parent = current_context()
        # The normal ToolRunner entry isolates each inner call from an outer
        # one-use approval admission. It validates and reviews the exact new
        # action, then hands it to the same central executor.
        return await runner.execute_tool_call_for_llm(parent.session_id, {
            "id": f"computer-use-{uuid4().hex}",
            "name": f"{skill_id}__{endpoint_id}", "args": args,
        }, [manifest], surface=parent.surface)

    def _registered_authority(self, skill_id: str, endpoint_id: str):
        """Preflight availability before accessing the screen or VLM."""
        from security.session_identity import SessionIdentityValidationError, validate_session_id
        from skills.call_context import current_context

        parent = current_context()
        try:
            validate_session_id(parent.session_id)
        except SessionIdentityValidationError:
            return self._refusal("Computer use cannot execute without a valid caller session.")
        state_obj = getattr(sys.modules.get("api.state"), "state", None)
        executor = getattr(state_obj, "skill_executor", None)
        runner = getattr(state_obj, "tool_runner", None)
        if runner is None:
            runner = getattr(getattr(state_obj, "orchestrator", None), "tool_runner", None)
        if (not callable(getattr(executor, "execute", None))
                or not callable(getattr(runner, "execute_tool_call_for_llm", None))
                or not callable(getattr(runner, "enforce_plan_mode", None))
                or not (callable(getattr(runner, "enforce_executor_safety", None))
                        or callable(getattr(runner, "enforce_safety", None)))):
            return self._refusal("Computer use cannot execute without its central executor and policy runner.")
        registry = getattr(state_obj, "skill_registry", None)
        manifest = getattr(registry, "skills", {}).get(skill_id)
        endpoint = next((ep for ep in getattr(manifest, "endpoints", [])
                         if getattr(ep, "id", None) == endpoint_id), None)
        if manifest is None or endpoint is None:
            return self._refusal(f"{skill_id}__{endpoint_id} is not registered; computer use cannot execute this action.")
        return runner, manifest, endpoint

    def _describe_gui_result(self, action, result) -> str:
        """Human-readable message the step log and tests expect."""
        if isinstance(result, dict):
            if not result.get("success"):
                err = result.get("error") or result.get("reason") or "action failed"
                return f"{action.action} failed: {err}"
            data = result.get("data") or {}
            if isinstance(data, dict):
                msg = data.get("message")
                if msg:
                    return str(msg)
        return f"{action.action} executed"

    async def _do_drag(self, path: list, *, stop_on_refusal: bool = False) -> str:
        # No registered canonical drag endpoint exists. Never synthesize raw
        # pointer events outside the executor to fill that capability gap.
        result = self._refusal("drag failed: no registered reviewed drag endpoint is available.",
                               code="computer_use_action_unavailable")
        self._stop_if_refused(result, stop_on_refusal=stop_on_refusal)
        return result["error"]

    @staticmethod
    def _open_app_name(command: str) -> Optional[str]:
        """Accept one literal application name, never shell syntax."""
        if not isinstance(command, str) or len(command) > 512:
            return None
        if any(c in command for c in ";&|$`<>\n\r\x00"):
            return None
        try:
            parts = shlex.split(command)
        except ValueError:
            return None
        if len(parts) != 3 or parts[0] not in ("open", "/usr/bin/open") or parts[1] != "-a":
            return None
        name = parts[2]
        if (not name or name != name.strip() or len(name) > 128
                or not all(c.isalnum() or c in " ._-" for c in name)):
            return None
        return name

    @classmethod
    def _shell_command_allowed(cls, command: str) -> bool:
        return cls._open_app_name(command) is not None

    async def _do_shell(self, command: str, *, stop_on_refusal: bool = False) -> str:
        app = self._open_app_name(command)
        if app is None:
            result = self._refusal(
                "blocked: computer use only supports opening one literal application; "
                "other commands require separately reviewed coding_tools__bash or desktop_control tools.",
                code="computer_use_action_unavailable",
            )
        else:
            # The restricted literal name is not supplied shell or AppleScript.
            # The existing registered tool still applies central action policy.
            result = await self._execute_registered("desktop_control", "open_app", {
                "script": f'tell application "{app}" to activate',
            })
        self._stop_if_refused(result, stop_on_refusal=stop_on_refusal)
        if result.get("success") is not True:
            return str(result.get("error") or "Application opening did not complete")
        return "Application opening returned a successful tool result; verify the visible outcome."
