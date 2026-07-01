"""
Refusal detection and action-intent fallback for the FERAL orchestrator.

Detects when the LLM refuses to act, builds direct action-intent tool calls
for common operations (open app, open URL, create file, etc.), and executes
fallback paths when the LLM won't cooperate.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import time
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from agents.orchestrator import Orchestrator
    from models.skill_manifest import SkillManifest

logger = logging.getLogger("feral.orchestrator.refusal")


class RefusalHandler:
    """Detects LLM refusals and provides fallback action-intent execution."""

    REFUSAL_PHRASES = (
        "i can't",
        "i cannot",
        "i am unable",
        "i'm unable",
        "not possible",
        "unfortunately i",
        "i don't have the ability",
        "cannot do that",
        "can't do that",
        "can't create",
        "cannot create",
        # Broader paraphrases that Production agents never emit
        "not supported",
        "no access to",
        "unable to",
        "beyond my abilities",
        "beyond my capabilities",
        "outside my capabilities",
        "i don't have access",
        "i do not have access",
        "i lack the ability",
        "i am not able",
        "i'm not able",
    )

    # Never-stall retry instruction strings. These are NOT appended to
    # history as new user messages — they are injected directly into the next
    # model prompt (prompt-addition style, see Orchestrator._retry_steer).
    REASONING_ONLY_RETRY_INSTRUCTION = (
        "The previous attempt recorded reasoning but produced no user-visible "
        "answer. Continue from the current state and produce the visible answer "
        "now. Do not restart from scratch."
    )
    EMPTY_RESPONSE_RETRY_INSTRUCTION = (
        "The previous attempt produced no user-visible answer. Continue and "
        "produce the answer now. Do not restart."
    )
    ACK_EXECUTION_FAST_PATH_INSTRUCTION = (
        "The latest user message is a short approval to proceed. Do not recap "
        "or restate the plan. Start with the first concrete tool action "
        "immediately. Keep any user-facing follow-up brief."
    )

    # Short, normalized forms of "yes, go ahead" style acks. Exact-match only
    # on a trimmed+lowercased version; never fuzzy, to avoid false positives
    # on substantive messages.
    ACK_EXECUTION_PHRASES = frozenset({
        "yes", "y", "yep", "yeah", "yup",
        "go", "go ahead", "go for it", "proceed", "proceed please",
        "do it", "do it please", "please do", "please proceed",
        "ok", "okay", "ok go", "okay go",
        "approved", "approve", "sure", "sure go",
        "sounds good", "looks good", "lgtm",
        "continue", "keep going", "ship it",
    })

    ACTION_INTENT_WORDS = {
        "create", "open", "make", "write", "run", "play", "generate", "send",
        "install", "build", "edit", "delete", "move", "copy", "search", "find",
        "start", "stop", "launch", "record", "download", "upload",
        "spin", "flash", "schedule", "rotate",
    }

    DESTRUCTIVE_ACTION_WORDS = {
        "delete", "remove", "erase", "destroy", "wipe", "format", "factory reset",
        "shutdown", "reboot", "kill", "terminate", "rm -rf",
    }

    COMMON_APP_NAMES = {
        "music": "Music",
        "spotify": "Spotify",
        "safari": "Safari",
        "chrome": "Google Chrome",
        "terminal": "Terminal",
        "notes": "Notes",
        "finder": "Finder",
        "mail": "Mail",
        "calendar": "Calendar",
        "vscode": "Visual Studio Code",
        "visual studio code": "Visual Studio Code",
        "slack": "Slack",
    }

    def __init__(self, orchestrator: "Orchestrator"):
        self._orch = orchestrator

    # ─────────────────────────────────────────────
    # Detection helpers
    # ─────────────────────────────────────────────

    MESSAGING_INTENT_WORDS = (
        "telegram", "slack", "discord", "whatsapp",
        "send message", "send a message", "send text", "dm ", "message ",
    )

    PLANNING_ONLY_PHRASES = (
        "here is how you can",
        "you can do this by",
        "to do this, you would",
        "you would need to",
        "the steps would be",
        "the steps are",
        "first, you", "second, you", "third, you",
        "you should", "you could",
    )

    def is_refusal(self, text: str) -> bool:
        if not text:
            return False
        normalized = re.sub(r"\s+", " ", text.lower()).strip()
        return any(phrase in normalized for phrase in self.REFUSAL_PHRASES)

    def is_plan_only(self, text: str) -> bool:
        if not text:
            return False
        normalized = re.sub(r"\s+", " ", text.lower()).strip()
        return any(phrase in normalized for phrase in self.PLANNING_ONLY_PHRASES)

    def is_messaging_intent(self, text: str) -> bool:
        if not text:
            return False
        lowered = text.lower()
        return any(token in lowered for token in self.MESSAGING_INTENT_WORDS)

    # ─────────────────────────────────────────────────────────
    # Never-stall: reasoning-only / empty-response / ack detection
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def is_reasoning_only(response: dict | None) -> bool:
        """True when the model produced reasoning tokens/trace but no visible text.

        Supports the common shapes used by our LLM providers:
        - OpenAI-style: {"reasoning": "...", "content": ""}
        - Anthropic-style: {"thinking": [...], "content": []}
        - dict with "reasoning_tokens" but empty/missing "content"
        """
        if not response or not isinstance(response, dict):
            return False
        has_reasoning = bool(
            response.get("reasoning")
            or response.get("thinking")
            or response.get("reasoning_tokens")
            or response.get("reasoning_content")
        )
        content = response.get("content") or response.get("text") or ""
        if isinstance(content, list):
            # Anthropic content blocks
            content = "".join(
                (b.get("text") or "") for b in content if isinstance(b, dict)
            )
        tool_calls = response.get("tool_calls") or []
        return bool(has_reasoning and not str(content).strip() and not tool_calls)

    @staticmethod
    def is_empty_response(response: dict | None) -> bool:
        """True when the model produced no visible text AND no tool calls AND no reasoning."""
        if not response or not isinstance(response, dict):
            return True
        content = response.get("content") or response.get("text") or ""
        if isinstance(content, list):
            content = "".join(
                (b.get("text") or "") for b in content if isinstance(b, dict)
            )
        tool_calls = response.get("tool_calls") or []
        reasoning = (
            response.get("reasoning")
            or response.get("thinking")
            or response.get("reasoning_tokens")
        )
        return not str(content).strip() and not tool_calls and not reasoning

    @classmethod
    def is_ack_execution(cls, user_text: str) -> bool:
        """True when the user message is a short approval like 'go ahead' / 'yes'."""
        if not user_text:
            return False
        trimmed = user_text.strip().lower()
        # Strip trailing punctuation ("yes!", "go.", "do it please!!!")
        trimmed = re.sub(r"[!?.,;:]+$", "", trimmed).strip()
        if not trimmed or len(trimmed) > 40 or "\n" in trimmed:
            return False
        return trimmed in cls.ACK_EXECUTION_PHRASES

    @staticmethod
    def resolve_reasoning_only_retry_instruction() -> str:
        return RefusalHandler.REASONING_ONLY_RETRY_INSTRUCTION

    @staticmethod
    def resolve_empty_response_retry_instruction() -> str:
        return RefusalHandler.EMPTY_RESPONSE_RETRY_INSTRUCTION

    @classmethod
    def resolve_ack_execution_fast_path_instruction(cls, user_text: str) -> str | None:
        if cls.is_ack_execution(user_text):
            return cls.ACK_EXECUTION_FAST_PATH_INSTRUCTION
        return None

    @staticmethod
    def _live_channel_list() -> str:
        try:
            from api.state import state as _state
            cm = getattr(_state, "channel_manager", None)
            if not cm:
                return ""
            rows = []
            for ctype, ch in cm.channels.items():
                bot = getattr(ch, "_bot_username", None)
                rows.append(f"{ctype} (@{bot})" if bot else ctype)
            return ", ".join(rows)
        except Exception:
            return ""

    def planning_only_retry_instruction(self, user_text: str) -> str:
        """Act-now retry: specific, tool-pointing, one-chance."""
        if self.is_messaging_intent(user_text):
            channels = self._live_channel_list() or "the configured channel"
            return (
                "The previous response refused or described what the user should do.\n"
                "Tool `messaging_channels__send` IS available. Active channels: "
                f"{channels}.\n"
                "Act NOW: call `messaging_channels__send` with the correct channel, to, and text.\n"
                "If the user gave a @handle on Telegram, first call "
                "`messaging_channels__resolve_chat_id`, then send.\n"
                "If a real blocker exists, reply with ONE sentence stating the specific blocker."
            )
        return (
            "The previous response described what the user should do, or said you can't do it.\n"
            "You HAVE tools for this. Act NOW by calling the appropriate tool.\n"
            "If a real blocker exists, reply with ONE sentence stating the specific blocker."
        )

    def query_implies_action(self, text: str) -> bool:
        normalized = re.sub(r"[^a-z0-9\s]", " ", (text or "").lower())
        words = [w for w in normalized.split() if w]
        if len(words) >= 6:
            return True
        return any(word in self.ACTION_INTENT_WORDS for word in words)

    def action_text_is_destructive(self, text: str) -> bool:
        lowered = (text or "").lower()
        return any(token in lowered for token in self.DESTRUCTIVE_ACTION_WORDS)

    @staticmethod
    def extract_first_url(text: str) -> str:
        match = re.search(r"(https?://[^\s]+)", text or "", flags=re.IGNORECASE)
        if not match:
            return ""
        return match.group(1).rstrip(").,;!?")

    def extract_open_app_name(self, text: str) -> str:
        lowered = (text or "").lower()
        for hint, app in self.COMMON_APP_NAMES.items():
            if any(phrase in lowered for phrase in (f"open {hint}", f"launch {hint}", f"start {hint}")):
                return app

        match = re.search(r"(?:open|launch|start)\s+([a-z0-9 ._+-]{2,40})(?:\s+app(?:lication)?)?", lowered)
        if not match:
            return ""
        candidate = match.group(1).strip(" ._+-")
        if not candidate:
            return ""
        return " ".join(w.capitalize() for w in candidate.split())

    @staticmethod
    def capability_key(text: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()
        return normalized[:180]

    # ─────────────────────────────────────────────
    # Action intent construction
    # ─────────────────────────────────────────────

    def build_action_intent_tool_call(self, text: str) -> Optional[dict]:
        lowered = (text or "").lower()
        app_name = self.extract_open_app_name(text)
        if app_name:
            return {
                "name": "desktop_control__open_app",
                "args": {"script": f'tell application "{app_name}" to activate'},
                "_intent": f"open {app_name}",
            }

        url = self.extract_first_url(text)
        if url:
            return {
                "name": "desktop_control__shell_command",
                "args": {"command": f"open {shlex.quote(url)}"},
                "_intent": f"open URL {url}",
            }

        if "desktop" in lowered and any(token in lowered for token in ("note", "file", "txt")):
            content_match = re.search(r"(?:add|with|containing|content[: ]+)(.+)$", text, flags=re.IGNORECASE)
            content = (content_match.group(1).strip() if content_match else "hello world").strip("\"'")
            python_code = (
                "from pathlib import Path; "
                "p=Path.home()/'Desktop'/'feral_note.txt'; "
                f"p.write_text({content!r}); "
                "print(f'Created {p}')"
            )
            return {
                "name": "computer_use__bash",
                "args": {"command": f"python3 -c {shlex.quote(python_code)}"},
                "_intent": "create desktop note",
            }

        if self.query_implies_action(text):
            return {
                "name": "agentic_computer_use__execute_task",
                "args": {"task": text, "max_steps": 8},
                "_intent": "execute GUI workflow",
            }

        return None

    @staticmethod
    def summarize_action_result(tool_call: dict, result_data: dict) -> str:
        tool_name = tool_call.get("name", "action")
        if not isinstance(result_data, dict):
            return f"I executed {tool_name}."

        if result_data.get("status") == "command_sent_to_hardware_daemon":
            return "Command sent to the connected device daemon."

        if result_data.get("success"):
            data = result_data.get("data")
            if isinstance(data, dict):
                if data.get("note"):
                    return f"Done. {data.get('note')}"
                if data.get("stdout"):
                    return f"Done. {str(data.get('stdout'))[:240]}"
            return f"Done. Executed {tool_name} successfully."

        error = result_data.get("error") or result_data.get("note") or "Unknown error"
        return f"I attempted {tool_name}, but it failed: {error}"

    # ─────────────────────────────────────────────
    # Fallback execution
    # ─────────────────────────────────────────────

    async def execute_action_intent_fallback(
        self,
        session_id: str,
        text: str,
        available_skills: list["SkillManifest"],
    ) -> bool:
        """
        Attempt to directly execute the user's intent when the LLM refuses.
        Returns True if the intent was handled (even if denied by safety).
        """
        from agents.tool_runner import SafetyLevel

        tool_call = self.build_action_intent_tool_call(text)
        if not tool_call:
            return False

        orch = self._orch
        tool_name = tool_call["name"]
        args = tool_call.get("args", {})
        safety_level = orch.tool_runner.classify_safety(tool_name, args)
        if self.action_text_is_destructive(text) and safety_level == SafetyLevel.AUTO:
            safety_level = SafetyLevel.CONFIRM

        if safety_level == SafetyLevel.DENY:
            denial = orch.tool_runner.enforce_safety(tool_name, args) or {
                "error": "This action is blocked by safety policy.",
            }
            await orch._send_text(session_id, denial.get("error", "Blocked by safety policy."))
            return True

        if safety_level == SafetyLevel.CONFIRM:
            await orch._maybe_auto_expand_capability(session_id, text)
            await orch._queue_action_confirmation(
                session_id=session_id,
                tool_call=tool_call,
                available_skills=available_skills,
                reason=text,
            )
            return True

        result_data = await orch.tool_runner.execute_tool_call_for_llm(session_id, tool_call, available_skills)
        await orch._try_genui_for_result(session_id, tool_call, result_data)
        summary = self.summarize_action_result(tool_call, result_data)
        await orch._send_text(session_id, summary)
        if orch.memory:
            orch.memory.working_push(session_id, {"role": "assistant", "text": summary[:300]})
        await orch._maybe_auto_expand_capability(session_id, text)
        return True
