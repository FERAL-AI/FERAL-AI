"""
FERAL Agentic Computer Use — Vision-Action Loop
==================================================
Combines screen capture, VLM analysis, and desktop automation into
an autonomous loop: screenshot -> understand -> act -> verify.

This is the component that makes FERAL capable of performing any
GUI task, surpassing single-shot tool calling by iterating until
the objective is achieved.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, ValidationError

from agents.computer_use_driver import (  # boundary-ok: provider-neutral driver lives in agents/ by design (PR 4)
    GUI_ENDPOINT_FOR,
    NormalizedAction,
    gui_args_for,
    normalize_action,
)
from skills.base import BaseSkill
from skills.impl import register_skill
from skills.impl.gui_computer_use import (
    capture_screenshot_bytes,
    detect_dpi_scale,
)

logger = logging.getLogger("feral.agentic_cu")

MAX_ITERATIONS = 15
SCREENSHOT_DELAY = 0.8

ACTION_SYSTEM_PROMPT = """You are an AI agent that controls a computer to accomplish tasks.
You are given a screenshot of the current screen and a task to perform.

Available actions (return EXACTLY ONE as JSON):
- {"action": "click", "x": <int>, "y": <int>, "description": "what you're clicking"}
- {"action": "double_click", "x": <int>, "y": <int>, "description": "what you're double-clicking"}
- {"action": "right_click", "x": <int>, "y": <int>, "description": "what you're right-clicking"}
- {"action": "type", "text": "<string>", "description": "what you're typing"}
- {"action": "key", "keys": "<combo>", "description": "what shortcut"} (e.g. "cmd+c", "enter", "tab")
- {"action": "scroll", "direction": "up"|"down", "amount": <int>, "description": "why scrolling"}
- {"action": "shell", "command": "<string>", "description": "shell command to run"}
- {"action": "done", "summary": "what was accomplished"}
- {"action": "failed", "reason": "why it cannot be done"}

Rules:
- Look at the screenshot carefully to determine element positions.
- Click coordinates should target the CENTER of the element you want to interact with.
- After clicking a text field, use "type" to enter text.
- Use "key" for keyboard shortcuts (cmd+a, cmd+v, enter, tab, escape, etc.).
- Use "shell" for opening apps (open -a "App Name") or running commands.
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

        max_steps = min(int(args.get("max_steps", MAX_ITERATIONS)), MAX_ITERATIONS)
        steps_log: list[dict] = []

        llm = await self._get_vlm(vault)
        if not llm:
            return {"success": False, "status_code": 503, "data": None, "error": "No VLM available. Set OPENAI_API_KEY or FERAL_VLM_PROVIDER."}

        for i in range(max_steps):
            screenshot_b64 = await self._capture_screen()
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

            result = await self._execute_action(action)
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

    async def _capture_screen(self) -> Optional[str]:
        """Capture the screen and return base64-encoded JPEG."""
        try:
            raw = await capture_screenshot_bytes()
            if raw is None:
                return None
            return base64.b64encode(raw).decode()
        except Exception as e:
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

    async def _execute_action(self, action: dict) -> str:
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
            return f"Unknown action: {action_type}"

        try:
            if normalized.action == "shell":
                return await self._do_shell(normalized.command)

            if normalized.action == "wait":
                ms = max(0, int(normalized.duration_ms))
                await asyncio.sleep(ms / 1000.0)
                return f"Waited {ms}ms"

            if normalized.action == "drag":
                return await self._do_drag(normalized.path)

            return await self._dispatch_via_gui(normalized)
        except Exception as e:
            return f"Action failed: {e}"

    async def _dispatch_via_gui(self, action: NormalizedAction) -> str:
        """Route a normalized action to ``GUIComputerUseSkill`` so DPI,
        rate limiting, and pyautogui/AppleScript fallbacks live in a
        single module. Returns the human-readable message the legacy
        ladder produced so step logs / tests stay stable.
        """
        endpoint_id = GUI_ENDPOINT_FOR.get(action.action)
        if endpoint_id is None:
            return f"Unknown action: {action.action}"
        gui_args = gui_args_for(action)

        from skills.impl import get_implementation

        gui = get_implementation("gui_computer_use")
        if gui is None:
            return (
                "gui_computer_use skill is not registered; "
                "agentic_computer_use cannot execute physical actions"
            )

        result = await gui.execute(endpoint_id, gui_args, vault={})
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

    async def _do_drag(self, path: list) -> str:
        """Best-effort drag along ``path`` via pyautogui. We keep this
        local because gui_computer_use does not yet expose a ``drag``
        endpoint — adding one is a follow-up. Until then, drag on a
        host without pyautogui is honestly reported as unavailable."""
        if not path:
            return "drag failed: empty path"
        try:
            import pyautogui
        except ImportError:
            return "drag failed: pyautogui not available"
        from skills.impl.gui_computer_use import scale_coordinates
        scale = self.dpi_scale
        scaled = [scale_coordinates(int(x), int(y), scale) for (x, y) in path]
        sx, sy = scaled[0]
        await asyncio.to_thread(pyautogui.moveTo, sx, sy)
        await asyncio.to_thread(pyautogui.mouseDown)
        try:
            for ex, ey in scaled[1:]:
                await asyncio.to_thread(pyautogui.moveTo, ex, ey, duration=0.1)
        finally:
            await asyncio.to_thread(pyautogui.mouseUp)
        return f"Dragged through {len(scaled)} points"

    # Anything beyond this allowlist must NOT execute through the GUI
    # vision loop. Real shell work goes through `coding_tools__bash` so
    # the canonical sandbox/policy boundary applies.
    _SHELL_ACTION_ALLOWLIST = ("open", "osascript", "screencapture")

    @classmethod
    def _shell_command_allowed(cls, command: str) -> bool:
        head = command.strip().split(None, 1)
        if not head:
            return False
        program = Path(head[0]).name.lower()
        return program in cls._SHELL_ACTION_ALLOWLIST

    async def _do_shell(self, command: str) -> str:
        if not self._shell_command_allowed(command):
            # Refusing here is the canonical-execution promise: the VLM
            # loop can launch UIs (`open -a`, `osascript`) but cannot
            # become a generic host shell. Free-form commands belong on
            # `coding_tools__bash` where the execution-mode decision applies.
            return (
                "blocked: agentic_computer_use shell only permits "
                f"{', '.join(self._SHELL_ACTION_ALLOWLIST)}; route other "
                "commands through coding_tools__bash"
            )
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15.0)
        output = (stdout or b"").decode()[:500]
        err = (stderr or b"").decode()[:200]
        return f"exit={proc.returncode} out={output}" + (f" err={err}" if err else "")
