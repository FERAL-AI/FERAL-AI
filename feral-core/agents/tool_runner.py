"""
Tool execution engine for the FERAL orchestrator.

Handles tool call dispatch, safety classification, anti-loop detection,
daemon command forwarding, and subagent parallel execution.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Optional, TYPE_CHECKING
from uuid import uuid4

from security.exec_approvals import ApprovalManager
from security.safety_resolver import (
    LEVEL_AUTO,
    LEVEL_CONFIRM,
    LEVEL_DENY,
    PolicyDecision,
    is_read_only,
    resolve_policy,
)
from agents.tool_dispatch_validator import (
    ToolDispatchValidator,
    make_tool_error_envelope,
)
from skills.call_context import bind_context, new_turn_id
from skills.result_budget import serialize_tool_result

MAX_LLM_TOOLS = 64

if TYPE_CHECKING:
    from agents.orchestrator import Orchestrator

logger = logging.getLogger("feral.orchestrator.tool_runner")

VALID_AUTONOMY_MODES = ("strict", "hybrid", "loose")
READ_ONLY_PATTERNS = ("search", "get", "list", "query", "read", "current", "status", "forecast")
# Tools whose `permission_needed` response is turned into an Allow/Deny
# folder card for the operator. `computer_use` was folded into
# `coding_tools` (identical endpoints, identical trigger phrases), but the
# legacy ids stay listed so a third-party manifest that still uses the old
# skill id gets the same grant flow rather than a dead error.
_COMPUTER_USE_PERMISSION_TOOLS = {
    "computer_use__read_file",
    "computer_use__write_file",
    "computer_use__edit_file",
    "computer_use__grep_search",
    "computer_use__glob_search",
    "computer_use__index_folder",
    "coding_tools__read_file",
    "coding_tools__write_file",
    "coding_tools__edit_file",
    "coding_tools__grep_search",
    "coding_tools__glob_search",
    "coding_tools__index_folder",
    # `bash` joins the list because a shell whose cwd resolves outside every
    # grant now refuses with the same `permission_needed` contract the file
    # tools use. Without this entry the operator would see the refusal text
    # but never get the card that fixes it.
    "coding_tools__bash",
}


# ─────────────────────────────────────────────
# Safety Classification
# ─────────────────────────────────────────────

class SafetyLevel:
    # String values are intentionally identical to ``security.safety_resolver``
    # constants so the SDUI / REST surfaces don't need to translate.
    AUTO = LEVEL_AUTO          # Execute immediately
    CONFIRM = LEVEL_CONFIRM    # Ask user confirmation via SDUI
    DENY = LEVEL_DENY          # Block outright


class ToolRunner:
    """Encapsulates all tool-call execution, safety gating, and anti-loop logic."""

    def __init__(
        self,
        orchestrator: "Orchestrator",
        autonomy_mode: str = "hybrid",
        approval_manager: Optional[ApprovalManager] = None,
    ):
        self._orch = orchestrator
        self._tool_repeat_state: dict[str, dict] = {}
        self._active_subagent_tasks = 0
        self._daemon_session_map: dict[str, str] = {}
        # A2 fix: futures keyed by daemon request_id so the LLM loop can
        # actually ``await`` the hardware daemon's result instead of
        # short-circuiting with a stub "command_sent_to_hardware_daemon"
        # success. Resolved from ``ui_handlers.handle_daemon_result``.
        self._pending_daemon_acks: dict[str, asyncio.Future] = {}
        self._pending_approvals: dict[str, dict] = {}
        # -A9: approval state must be shared across BrainState/API +
        # ToolRunner. If no manager is injected (legacy/tests), fall back
        # to local construction.
        self._approval_mgr = approval_manager or ApprovalManager()
        self._dispatch_validator: Optional[ToolDispatchValidator] = None
        # Fallback turn ids for dispatch paths with no orchestrator turn
        # record behind them (cron, taskflows). See `_turn_id_for`.
        self._synthetic_turns: dict[str, tuple[str, float]] = {}

        raw_mode = os.environ.get("FERAL_AUTONOMY", "").strip().lower() or autonomy_mode
        self._autonomy_mode = raw_mode if raw_mode in VALID_AUTONOMY_MODES else "hybrid"
        logger.info(f"ToolRunner autonomy_mode={self._autonomy_mode}")

    def _get_dispatch_validator(self) -> ToolDispatchValidator:
        if self._dispatch_validator is None:
            self._dispatch_validator = ToolDispatchValidator(registry=self._skill_registry())
        return self._dispatch_validator

    @staticmethod
    def assemble_llm_tool_list(
        skills_registry,
        relevant_skills,
        *,
        mcp_client=None,
        max_tools: int = MAX_LLM_TOOLS,
    ) -> list[dict]:
        """Build the LLM-facing tool list with routing-priority cap.

        ``relevant_skills`` is already ranked by the orchestrator router;
        skill tools from higher-priority skills are kept first. MCP tools
        are appended after skill tools (same order as orchestrator today).
        When the combined list exceeds ``max_tools``, tail entries drop and
        ``feral_tool_list_truncated_total`` increments.
        """
        from observability.metrics import increment

        tools: list[dict] = []
        if skills_registry is not None and relevant_skills:
            for skill in relevant_skills:
                tools.extend(skills_registry.get_tools_for_skills([skill]))

        if mcp_client is not None:
            mcp_tools = mcp_client.to_llm_tool_definitions()
            if mcp_tools:
                tools = (tools or []) + mcp_tools

        if len(tools) > max_tools:
            dropped = len(tools) - max_tools
            increment(
                "feral_tool_list_truncated_total",
                attributes={"dropped": str(dropped), "cap": str(max_tools)},
            )
            logger.warning(
                "LLM tool list truncated: %d -> %d (dropped %d tail tools)",
                len(tools), max_tools, dropped,
            )
            tools = tools[:max_tools]
        return tools or []

    # ─────────────────────────────────────────────
    # Safety: Graduated Permission System
    # ─────────────────────────────────────────────

    def _skill_registry(self):
        """Best-effort lookup of the orchestrator's skill registry for the
        manifest-aware resolver. Returns ``None`` if the orchestrator is
        not wired (rare; mostly happens in unit tests using mocks)."""
        return getattr(self._orch, "skills", None)

    def classify_safety(self, tool_name: str, args: dict) -> str:
        """Manifest-aware safety classification.

        Prefer ``SkillEndpoint.safety_tier`` / ``read_only_hint`` /
        ``requires_user_approval`` over the legacy substring heuristic;
        consult ``TOOL_DANGER_MAP`` for centrally-policed tools;
        fall back to the substring heuristic only when nothing more
        authoritative is available so existing unannotated third-party
        manifests do not silently change behaviour. See
        ``security/safety_resolver.py`` for the full lookup order.
        """
        decision = resolve_policy(
            tool_name, args or {}, surface="websocket",
            registry=self._skill_registry(),
        )
        return decision.level

    def policy_for(
        self,
        tool_name: str,
        args: Optional[dict] = None,
        *,
        surface: str = "websocket",
    ) -> PolicyDecision:
        """Expose the full :class:`PolicyDecision` so REST / SDUI can
        render an explainable approval card (level + which source
        produced the verdict). Internal callers should use this rather
        than re-running the resolver."""
        return resolve_policy(
            tool_name, args or {}, surface=surface,
            registry=self._skill_registry(),
        )

    def enforce_safety(self, tool_name: str, args: dict, session_id: str = "", surface: str = "websocket") -> Optional[dict]:
        """
        Returns a denial dict if the action should be blocked, a pending-approval
        dict if the user must confirm, or None if the action is allowed.
        """
        decision = self.policy_for(tool_name, args, surface=surface)

        if decision.level == SafetyLevel.DENY:
            # Distinguish surface-deny from policy-deny so the UI can
            # render different remediation hints (surface deny = "use a
            # different surface"; policy deny = "this action is not
            # permitted at all").
            note = (
                decision.deny_reason
                or f"Action '{tool_name}' is classified as dangerous and has been blocked."
            )
            error = (
                "Surface Policy: Tool Blocked"
                if decision.sources.get("surface_deny")
                else "Safety Protocol: Action Blocked"
            )
            # Phase 1 (audit-r10 overhaul) — structured audit fields so
            # iOS / web clients can render a real `permission_card`
            # (Phase 6) instead of the LLM hallucinating "go to
            # Settings". `setup_step` is reserved for Phase 6 (macOS
            # TCC probes + structured deeplinks) and stays None here;
            # `target_device_capabilities` is reserved for Phase 5
            # (capability registry) and stays None too. The wire
            # contract is locked NOW so downstream phases just fill
            # the values without another protocol bump.
            return {
                "status": "PermissionOutcome::Deny",
                "error": error,
                "note": note,
                "safety_level": "deny",
                "policy_sources": dict(decision.sources),
                "surface": surface,
                "setup_step": None,
                "target_device_capabilities": None,
            }

        level = decision.level
        read_only_flag = is_read_only(tool_name, registry=self._skill_registry())

        needs_approval = False
        if self._autonomy_mode == "strict":
            needs_approval = not read_only_flag
        elif self._autonomy_mode == "hybrid":
            needs_approval = level == SafetyLevel.CONFIRM
        # loose: nothing needs approval

        if not needs_approval:
            if self._autonomy_mode == "loose" and level == SafetyLevel.CONFIRM:
                logger.info(f"Safety CONFIRM (loose mode auto-exec): {tool_name}")
            return None

        approved, reason = self._approval_mgr.check_approval(tool_name, session_id)
        if approved:
            logger.info(f"Standing approval for {tool_name}: {reason}")
            return None

        # Reuse any identical pending approval so repeated retries by the
        # model keep the same request_id instead of spawning fresh entries.
        for pending in self._pending_approvals.values():
            if (
                pending.get("session_id") == session_id
                and pending.get("tool_name") == tool_name
                and pending.get("args") == args
            ):
                return pending

        request_id = str(uuid4())
        pending = {
            "status": "pending_approval",
            "tool_name": tool_name,
            "args": args,
            "request_id": request_id,
            "session_id": session_id,
            "safety_level": level,
            "created_at": time.time(),
            # Explainability for the SDUI approval card. Renderers can
            # show "Why are we asking?" using sources without re-running
            # the resolver and without leaking internal types.
            "policy_sources": dict(decision.sources),
        }
        self._pending_approvals[request_id] = pending
        logger.info(f"Approval required ({self._autonomy_mode}): {tool_name} → request_id={request_id}")
        return pending

    # ─── Approval lifecycle ───

    def pending_for_session(self, session_id: str) -> list[dict]:
        """Return pending approvals for a session, oldest first."""
        rows = [
            p for p in self._pending_approvals.values()
            if p.get("session_id") == session_id
        ]
        rows.sort(key=lambda p: float(p.get("created_at", 0.0)))
        return rows

    def list_pending(self, *, session_id: Optional[str] = None, limit: int = 100) -> list[dict]:
        """Return pending approvals, optionally filtered by session."""
        rows = list(self._pending_approvals.values())
        if session_id:
            rows = [p for p in rows if p.get("session_id") == session_id]
        rows.sort(key=lambda p: float(p.get("created_at", 0.0)))
        if limit > 0:
            rows = rows[:limit]
        return [dict(p) for p in rows]

    def get_pending(self, request_id: str) -> Optional[dict]:
        """Return a copy of a pending approval by id, if present."""
        pending = self._pending_approvals.get(request_id)
        if pending is None:
            return None
        return dict(pending)

    def latest_pending_for_session(self, session_id: str) -> Optional[dict]:
        rows = self.pending_for_session(session_id)
        return rows[-1] if rows else None

    def pop_latest_pending_for_session(self, session_id: str) -> Optional[dict]:
        latest = self.latest_pending_for_session(session_id)
        if not latest:
            return None
        req_id = latest.get("request_id")
        if not req_id:
            return None
        return self._pending_approvals.pop(req_id, None)

    def grant_session_approval(self, tool_name: str, session_id: str) -> None:
        """Persist a per-session approval used to execute a confirmed call."""
        self._approval_mgr.grant_approval(tool_name, session_id, scope="session")

    def approve_pending(self, request_id: str, *, session_id: Optional[str] = None) -> Optional[dict]:
        """Approve a pending request; returns tool_name + args for re-execution."""
        pending = self._pending_approvals.get(request_id)
        if pending is None:
            return None
        if session_id and pending.get("session_id") != session_id:
            return None
        self._pending_approvals.pop(request_id, None)
        logger.info(f"Approved pending request {request_id} for {pending['tool_name']}")
        return {"tool_name": pending["tool_name"], "args": pending["args"]}

    def deny_pending(self, request_id: str, *, session_id: Optional[str] = None) -> Optional[dict]:
        """Deny and remove a pending request."""
        pending = self._pending_approvals.get(request_id)
        if pending is None:
            return None
        if session_id and pending.get("session_id") != session_id:
            return None
        self._pending_approvals.pop(request_id, None)
        logger.info(f"Denied pending request {request_id} for {pending['tool_name']}")
        return {
            "status": "PermissionOutcome::Deny",
            "tool_name": pending["tool_name"],
            "request_id": request_id,
            "note": "User denied this action.",
        }

    # ─── Surface resolution ───

    def _resolve_surface_for_session(self, session_id: str) -> str:
        """Resolve the execution surface for a session.

        Looks for ``Orchestrator._session_surfaces[session_id]`` first so the
        orchestrator can stamp surfaces from ``handle_command`` context, then
        falls back to ``"websocket"`` (the historical default for the
        interactive operator channel).
        """
        surfaces = getattr(self._orch, "_session_surfaces", None)
        if isinstance(surfaces, dict):
            value = surfaces.get(session_id)
            if isinstance(value, str) and value:
                return value
        return "websocket"

    # ─── Turn identity ───

    def _turn_id_for(self, session_id: str) -> str:
        """Stable id for the user message this tool call is answering.

        ``turn_id`` does not exist anywhere in FERAL today, and
        ``skills/checkpoints.py`` needs one to group "everything the agent
        wrote while answering this message" so a revert undoes a coherent
        unit rather than one arbitrary file write.

        Minting it here rather than in the orchestrator (which this lane
        must not touch) works because the orchestrator already opens one
        record per user message in ``Orchestrator._begin_turn`` and pushes
        it on ``_active_turns[session_id]``. We read the innermost record
        and stamp an id into it the first time we see it. That is an
        additive key on a dict the orchestrator only ever reads named
        fields from, so it changes no existing behaviour, and the id is a
        genuine per-user-message identity rather than a heuristic.

        Subagent sessions (``<parent>:sub:<n>:<rand>``) resolve to the
        parent's turn, so the six workers ``spawn_subagents`` runs in
        parallel all check point into the turn that spawned them. That is
        what makes "revert that" undo the whole fan-out.

        Paths with no orchestrator turn behind them at all (cron,
        taskflows, the REST tool surface) fall back to a per-session id
        that rotates after an idle gap. That fallback IS a heuristic and
        is only used where the alternative is no grouping at all.
        """
        base = session_id.split(":sub:")[0]
        stack = getattr(self._orch, "_active_turns", None)
        if isinstance(stack, dict):
            records = stack.get(base) or stack.get(session_id)
            if records:
                turn = records[-1]
                if isinstance(turn, dict):
                    turn_id = turn.get("_feral_turn_id")
                    if not turn_id:
                        turn_id = new_turn_id()
                        turn["_feral_turn_id"] = turn_id
                    return str(turn_id)
        return self._synthetic_turn_id(base)

    def _synthetic_turn_id(self, session_id: str) -> str:
        try:
            idle_after = float(os.environ.get("FERAL_TURN_IDLE_SECONDS", "180"))
        except ValueError:
            idle_after = 180.0
        now = time.time()
        existing = self._synthetic_turns.get(session_id)
        if existing and (now - existing[1]) <= idle_after:
            self._synthetic_turns[session_id] = (existing[0], now)
            return existing[0]
        turn_id = new_turn_id()
        self._synthetic_turns[session_id] = (turn_id, now)
        return turn_id

    def set_autonomy_mode(self, mode: str) -> str:
        """Runtime toggle for autonomy mode. Returns the effective mode."""
        mode = mode.strip().lower()
        if mode not in VALID_AUTONOMY_MODES:
            logger.warning(f"Invalid autonomy mode '{mode}', keeping {self._autonomy_mode}")
            return self._autonomy_mode
        self._autonomy_mode = mode
        logger.info(f"Autonomy mode changed to: {mode}")
        return self._autonomy_mode

    @property
    def autonomy_mode(self) -> str:
        return self._autonomy_mode

    # ─────────────────────────────────────────────
    # Anti-loop Detection
    # ─────────────────────────────────────────────

    @staticmethod
    def tool_signature(tool_name: str, args: dict) -> str:
        """Create a stable signature for anti-loop detection."""
        try:
            args_key = json.dumps(args or {}, sort_keys=True, default=str)
        except Exception:
            args_key = str(args)
        return f"{tool_name}::{args_key}"

    def register_tool_attempt(self, session_id: str, tool_name: str, args: dict) -> int:
        """Track consecutive identical tool calls and return current streak."""
        signature = self.tool_signature(tool_name, args)
        state = self._tool_repeat_state.get(session_id)
        if state and state.get("signature") == signature:
            count = int(state.get("count", 0)) + 1
        else:
            count = 1
        self._tool_repeat_state[session_id] = {
            "signature": signature,
            "count": count,
            "tool_name": tool_name,
        }
        return count

    @staticmethod
    def anti_loop_guidance(tool_name: str, streak: int) -> str:
        alt_hint = ""
        shell_tools = (
            "desktop_control__shell_command", "coding_tools__bash",
            "computer_use__bash",
            "desktop_control__shell", "shell_command",
        )
        if tool_name in shell_tools:
            alt_hint = (
                " IMPORTANT: For creating or writing files, use coding_tools__write_file instead "
                "of shell echo/printf. For running programs, check if coding_tools__bash or "
                "code_interpreter__execute can handle it directly."
            )
        return (
            f"STOP: You have called '{tool_name}' with the same arguments "
            f"{streak} times in a row. Do NOT repeat this call. "
            f"You MUST use a completely different tool or approach.{alt_hint}"
        )

    def clear_session(self, session_id: str):
        """Remove anti-loop state for a disconnected session."""
        self._tool_repeat_state.pop(session_id, None)

    # ─────────────────────────────────────────────
    # Daemon Command Execution
    # ─────────────────────────────────────────────

    async def execute_daemon_command(self, session_id: str, node_id: str, action: str, args: dict):
        actual_node_id = node_id.replace("daemon_", "")
        daemons = self._orch.daemons

        if actual_node_id not in daemons:
            available = list(daemons.keys()) if daemons else ["none"]
            await self._orch._send_text(session_id, f"Node '{actual_node_id}' not connected. Available: {available}")
            return

        ws = daemons[actual_node_id]
        request_id = str(uuid4())[:8]
        daemon_msg = {
            "type": "hup_action_request",
            "payload": {
                "action_id": request_id,
                "name": action,
                "params": args,
                "timeout_ms": 30000,
            },
        }

        self._daemon_session_map[request_id] = session_id
        await ws.send_json(daemon_msg)
        await self._orch._send_text(session_id, f"Action sent to node '{actual_node_id}'...")

    async def execute_capability_action(
        self,
        session_id: str,
        action: str,
        args: dict,
        timeout: float = 30.0,
    ) -> dict:
        """Phase 5 (audit-r10 overhaul) — capability-aware dispatch.

        The caller passes ONLY the action name (e.g. ``phone.call.start``)
        and the args; this method consults the brain's capability
        registry to find which connected node currently publishes that
        action, then dispatches through ``execute_daemon_command_with_ack``.

        When no connected node advertises the action, returns a
        structured ``capability_unavailable`` envelope so the LLM can
        answer truthfully ("you'd need to connect your iPhone first")
        instead of hallucinating a "done!" reply or timing out the
        HUP future. This is the missing-handler half of the Phase 1
        ``device_target`` work — together they close the loop on the
        operator's complaint #8.
        """
        registry = getattr(self._orch, "capability_registry", None)
        if registry is None:
            # Fall back to legacy behaviour for tests / contexts that
            # don't construct a full BrainState. The orchestrator
            # always wires `state` in production.
            return {
                "success": False,
                "status_code": 503,
                "error": (
                    f"capability_unavailable: no capability registry available "
                    f"to route action '{action}'"
                ),
                "data": None,
            }
        handler = registry.find_handler(action)
        if handler is None:
            connected_types = sorted({
                info["node_type"]
                for info in registry._nodes.values()  # noqa: SLF001 — internal read
            }) if registry.connected_node_ids() else []
            return {
                "success": False,
                "status_code": 404,
                "error": (
                    f"capability_unavailable: no connected node publishes "
                    f"`{action}`. Connected node types: "
                    f"{connected_types or 'none'}. The user likely needs to "
                    f"connect the relevant device (e.g. iPhone for "
                    f"`phone.*` actions, glasses for `glasses.*`)."
                ),
                "data": None,
            }

        # Phase 11 (audit-r10 overhaul) — brain-host dispatch.
        # `find_handler` may return a handler with `node_id=None` and
        # `surface="brain_host"` when the action is published by the
        # desktop_control facade running in-process on the Mac. Route
        # those through the synchronous facade dispatcher instead of
        # an HUP round-trip, then run the same post-processing path
        # (permission_card / tcc_card emission) so the orchestrator
        # behaves identically for brain-host vs node actions.
        if handler.node_id is None and handler.surface == "brain_host":
            try:
                from skills.desktop_control import dispatch_desktop_action

                result = dispatch_desktop_action(action, args)
            except Exception as exc:  # noqa: BLE001
                result = {
                    "success": False,
                    "status_code": 500,
                    "error": f"brain_host dispatch raised: {exc}",
                }
            await self._maybe_emit_capability_cards(
                session_id=session_id, action=action, handler=handler,
                result=result,
            )
            return result

        if handler.node_id is None:
            return {
                "success": False,
                "status_code": 503,
                "error": (
                    f"capability_unavailable: handler for `{action}` "
                    f"resolved to surface `{handler.surface}` but no "
                    "in-process dispatcher is wired."
                ),
                "data": None,
            }
        result = await self.execute_daemon_command_with_ack(
            session_id=session_id,
            node_id=handler.node_id,
            action=action,
            args=args,
            timeout=timeout,
        )
        await self._maybe_emit_capability_cards(
            session_id=session_id, action=action, handler=handler,
            result=result,
        )
        return result

    async def _maybe_emit_capability_cards(
        self,
        *,
        session_id: str,
        action: str,
        handler,
        result: dict,
    ) -> None:
        """Phase 6 (iOS permission_card) + Phase 11 (Mac tcc_card)
        post-processor for capability dispatch results.

        Looks at the result envelope; if the error string matches one
        of our structured contracts:
        * ``permission_denied:<NSKey>`` → iOS permission_card SDUI.
        * ``tcc_denied:<key>`` → Mac tcc_card SDUI (also opens the
          relevant System Settings pane on the Mac as a side effect).

        Wire-level result envelope is left untouched so the LLM still
        sees the truth and can verbalize a one-liner referencing the
        card. Emission is best-effort — any failure leaves the
        existing tool-result flow untouched.
        """
        try:
            from agents.permission_card import card_for_action_result as ios_card
            from agents.tcc_card import card_for_action_result as tcc_card_for
            from models.protocol import FeralMessage, SDUIPayload

            card = ios_card(
                result,
                skill_id=getattr(handler, "node_type", "") or "",
                action=action,
            )
            if card is None:
                card = tcc_card_for(
                    result,
                    skill_id=getattr(handler, "node_type", "") or "",
                    action=action,
                )
            if card is None:
                return

            try:
                await self._orch.send(
                    session_id,
                    FeralMessage(
                        session_id=session_id,
                        hop="brain",
                        type="sdui",
                        payload=SDUIPayload(root=card).model_dump(),
                    ),
                )
            except Exception:
                # Surface delivery is best-effort — if the client
                # socket is gone, fall through and let the LLM render
                # the textual error envelope.
                pass
        except Exception:
            # Card emission is strictly additive. Never break the
            # surrounding tool-result path on a card failure.
            pass

    async def execute_daemon_command_with_ack(
        self,
        session_id: str,
        node_id: str,
        action: str,
        args: dict,
        timeout: float = 30.0,
    ) -> dict:
        """Send a daemon command and wait for its ``tool_result`` ack.

        The previous behaviour returned ``{"status": "command_sent_to_hardware_daemon"}``
        immediately — a silent stub-success that let the LLM claim "I did it"
        while the daemon had either rejected the command or never received it.

        This variant:
          * registers a future in ``_pending_daemon_acks`` keyed by ``request_id``;
          * is resolved from ``ui_handlers.handle_daemon_result`` when the daemon
            sends back an ack;
          * times out with ``success: False`` after ``timeout`` seconds so a
            misbehaving daemon can't hang the LLM loop.
        """
        actual_node_id = node_id.replace("daemon_", "")
        daemons = self._orch.daemons

        if actual_node_id not in daemons:
            available = list(daemons.keys()) if daemons else ["none"]
            return {
                "success": False,
                "status_code": 503,
                "error": f"Daemon '{actual_node_id}' not connected. Available: {available}",
                "data": None,
            }

        ws = daemons[actual_node_id]
        request_id = str(uuid4())[:8]
        daemon_msg = {
            "type": "hup_action_request",
            "payload": {
                "action_id": request_id,
                "name": action,
                "params": args,
                "timeout_ms": int(timeout * 1000),
            },
        }

        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._pending_daemon_acks[request_id] = future
        self._daemon_session_map[request_id] = session_id
        try:
            await ws.send_json(daemon_msg)
        except Exception as exc:
            self._pending_daemon_acks.pop(request_id, None)
            return {
                "success": False,
                "status_code": 500,
                "error": f"Failed to send action to daemon '{actual_node_id}': {exc}",
                "data": None,
            }

        try:
            ack = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending_daemon_acks.pop(request_id, None)
            return {
                "success": False,
                "status_code": 504,
                "error": (
                    f"Daemon '{actual_node_id}' did not acknowledge {action} "
                    f"within {timeout:.0f}s."
                ),
                "data": None,
            }
        finally:
            self._pending_daemon_acks.pop(request_id, None)

        return ack if isinstance(ack, dict) else {"success": True, "data": ack}

    def resolve_daemon_ack(self, request_id: str, result: dict) -> bool:
        """Deliver a daemon ack to whichever LLM turn is waiting on it.

        Called from ``ui_handlers.handle_daemon_result``. Returns ``True`` if
        the request_id matched a pending future.
        """
        future = self._pending_daemon_acks.pop(request_id, None)
        if future is None or future.done():
            return False
        future.set_result(result)
        return True

    # ─────────────────────────────────────────────
    # Tool Execution (LLM loop variant)
    # ─────────────────────────────────────────────

    async def _notify_user_of_pending_approval(
        self, session_id: str, tool_name: str, denial: dict,
    ) -> None:
        """v2026.5.23 — emit a user-visible chat line + approval card
        when the LLM loop hits a `pending_approval` safety gate.

        Pre-fix: ``execute_tool_call_for_llm`` only logged ``Safety gate
        (pending_approval)`` and returned the envelope to the LLM. The
        streaming chat loop in the orchestrator then ``continue``d
        without emitting any text, so the user saw a frozen turn.
        This helper sends a short text + the approval ID so the user
        knows the brain wants to do something + how to approve it.
        Best-effort; failures here never block the underlying tool
        loop.
        """
        if (denial or {}).get("status") != "pending_approval":
            return
        try:
            send_text = getattr(self._orch, "_send_text", None)
            if send_text is None:
                return
            request_id = denial.get("request_id", "")
            human = denial.get("tool_name", tool_name)
            level = denial.get("safety_level", "confirm")
            msg = (
                f"I'd like to run `{human}` but it needs approval "
                f"({level}). Approve in the Devices/Approvals pane "
                f"to continue."
            )
            if request_id:
                msg += f"\n\nrequest_id: `{request_id}`"
            await send_text(session_id, msg)
        except Exception as exc:
            logger.debug("pending_approval user-notify failed: %s", exc)

    async def execute_tool_call_for_llm(
        self,
        session_id: str,
        tool_call: dict,
        available_skills,
        *,
        surface: Optional[str] = None,
    ) -> dict:
        """Bind tool-call identity, then run the call.

        This is the last layer that still knows the session:
        ``SkillExecutor.execute`` takes only (tool_name, args, skill,
        endpoint) and ``BaseSkill.execute`` takes even less. The bind
        publishes the identity on a contextvar so implementations can read
        it without a signature change. See ``skills/call_context.py``.
        """
        effective_surface = surface or self._resolve_surface_for_session(session_id)
        with bind_context(
            session_id=session_id,
            surface=effective_surface,
            tool_name=str(tool_call.get("name") or ""),
            call_id=str(tool_call.get("id") or ""),
            turn_id=self._turn_id_for(session_id),
        ):
            return await self._execute_tool_call_for_llm_inner(
                session_id, tool_call, available_skills,
                effective_surface=effective_surface,
            )

    async def _execute_tool_call_for_llm_inner(
        self,
        session_id: str,
        tool_call: dict,
        available_skills,
        *,
        effective_surface: str,
    ) -> dict:
        tool_name = tool_call["name"]
        args = tool_call["args"]
        logger.info(f"  LLM Tool call: {tool_name}({json.dumps(args)[:200]})")

        mcp_client = self._orch._mcp_client
        if tool_name.startswith("mcp_") and mcp_client:
            denial = self.enforce_safety(
                tool_name, args, session_id=session_id, surface=effective_surface,
            )
            if denial:
                logger.warning(f"Safety gate ({denial.get('status')}): {tool_name}")
                await self._notify_user_of_pending_approval(session_id, tool_name, denial)
                return denial
            logger.info(f"  MCP tool: {tool_name}")
            result = await mcp_client.call_tool(tool_name, args)
            content = result.get("content", [])
            if content and isinstance(content, list):
                return {"data": "\n".join(c.get("text", str(c)) for c in content)}
            return result

        parts = tool_name.split("__", 1)
        if len(parts) != 2:
            return {"error": f"Invalid tool reference: {tool_name}"}

        skill_id, endpoint_id = parts
        if skill_id == "subagent" and endpoint_id == "spawn_subagent":
            denial = self.enforce_safety(
                tool_name, args, session_id=session_id, surface=effective_surface,
            )
            if denial:
                logger.warning(f"Safety gate ({denial.get('status')}): {tool_name}")
                await self._notify_user_of_pending_approval(session_id, tool_name, denial)
                return denial
            return await self.spawn_subagents(session_id, args)

        logger.info(f"  Tool executing: {skill_id}__{endpoint_id}")

        streak = self.register_tool_attempt(session_id, tool_name, args)
        anti_loop_note = None
        if streak >= 5:
            message = (
                f"Anti-loop guard: blocked repeated call '{tool_name}' with identical "
                f"arguments ({streak}x in a row)."
            )
            logger.warning(message)
            return {
                "success": False,
                "error": message,
                "anti_loop_blocked": True,
                "anti_loop_streak": streak,
            }
        if streak >= 3:
            anti_loop_note = self.anti_loop_guidance(tool_name, streak)
            logger.warning(anti_loop_note)

        denial = self.enforce_safety(
            tool_name, args, session_id=session_id, surface=effective_surface,
        )
        if denial:
            logger.warning(f"Safety gate ({denial.get('status')}): {tool_name}")
            await self._notify_user_of_pending_approval(session_id, tool_name, denial)
            return denial

        if skill_id.startswith("daemon_"):
            return await self.execute_daemon_command_with_ack(
                session_id, skill_id, endpoint_id, args,
            )

        skills = self._orch.skills
        skill = skills.skills.get(skill_id)
        if not skill:
            return {"error": f"Skill not found: {skill_id}"}

        endpoint = next((ep for ep in skill.endpoints if ep.id == endpoint_id), None)
        if not endpoint:
            return make_tool_error_envelope(
                tool_call_id=tool_call.get("id", ""),
                error_code="unknown_endpoint",
                reason=f"Endpoint not found: {endpoint_id}",
            )

        validation = self._get_dispatch_validator().validate(skill_id, endpoint_id, args)
        if not validation.ok:
            logger.warning(
                "Tool dispatch validation failed for %s: %s",
                tool_name, validation.reason,
            )
            return make_tool_error_envelope(
                tool_call_id=tool_call.get("id", ""),
                error_code=validation.error_code or "invalid_args",
                reason=validation.reason or "Invalid tool arguments",
                schema_hint=validation.schema_hint,
            )
        if validation.fixed_args is not None:
            args = validation.fixed_args

        result = await self._orch.executor.execute(
            tool_name=tool_name, args=args, skill=skill, endpoint=endpoint,
        )

        result = await self._attach_permission_remediation(session_id, tool_name, result)

        if not result.get("success"):
            logger.warning(f"PostToolUse: Action failed — {result.get('error')}")

        if anti_loop_note:
            result = dict(result)
            result["_anti_loop_guidance"] = anti_loop_note
            result["_anti_loop_streak"] = streak

        return result

    async def _attach_permission_remediation(
        self,
        session_id: str,
        tool_name: str,
        result: dict,
    ) -> dict:
        """Send a folder-grant request for trusted computer_use policy denials."""
        if tool_name not in _COMPUTER_USE_PERMISSION_TOOLS or not isinstance(result, dict):
            return result
        data = result.get("data")
        if not isinstance(data, dict) or data.get("permission_needed") is not True:
            return result
        path = str(data.get("path") or "").strip()
        if not path:
            return result
        operation = str(data.get("operation") or "read").strip() or "read"
        sender = getattr(self._orch, "send_permission_request", None)
        if not callable(sender):
            return result

        next_result = dict(result)
        try:
            await sender(
                session_id,
                path,
                operation,
                reason=(
                    f"FERAL needs {operation} access to {path} to complete "
                    "the requested file operation."
                ),
            )
            next_result["_permission_request_sent"] = True
            next_result["remediation"] = (
                "Permission request sent to the operator; wait for the grant "
                "before retrying this file operation."
            )
        except Exception as exc:
            logger.warning("Failed to send permission request for %s: %s", path, exc)
            next_result["_permission_request_sent"] = False
            next_result["remediation"] = (
                f"Permission is required for {path}, but the grant request "
                f"could not be shown: {exc}"
            )
        return next_result

    # ─────────────────────────────────────────────
    # Tool Execution (direct / UI-event variant)
    # ─────────────────────────────────────────────

    async def execute_tool_call(
        self,
        session_id: str,
        tool_call: dict,
        available_skills,
        *,
        surface: Optional[str] = None,
    ):
        effective_surface = surface or self._resolve_surface_for_session(session_id)
        with bind_context(
            session_id=session_id,
            surface=effective_surface,
            tool_name=str(tool_call.get("name") or ""),
            call_id=str(tool_call.get("id") or ""),
            turn_id=self._turn_id_for(session_id),
        ):
            return await self._execute_tool_call_inner(
                session_id, tool_call, available_skills,
                effective_surface=effective_surface,
            )

    async def _execute_tool_call_inner(
        self,
        session_id: str,
        tool_call: dict,
        available_skills,
        *,
        effective_surface: str,
    ):
        from models.protocol import FeralMessage, SDUIPayload

        tool_name = tool_call["name"]
        args = tool_call["args"]
        logger.info(f"  Tool call: {tool_name}({json.dumps(args)[:200]})")

        parts = tool_name.split("__", 1)
        if len(parts) != 2:
            await self._orch._send_text(session_id, f"Invalid tool reference: {tool_name}")
            return

        skill_id, endpoint_id = parts

        denial = self.enforce_safety(
            tool_name, args, session_id=session_id, surface=effective_surface,
        )
        if denial:
            status = denial.get("status", "blocked")
            note = denial.get("note", denial.get("error", "Action blocked by safety policy."))
            if status == "pending_approval":
                await self._orch._send_text(
                    session_id,
                    f"Approval required for '{tool_name}'. Request ID: {denial.get('request_id')}",
                )
            else:
                await self._orch._send_text(session_id, note)
            return

        if skill_id.startswith("daemon_"):
            await self.execute_daemon_command(session_id, skill_id, endpoint_id, args)
            return

        skills = self._orch.skills
        skill = skills.skills.get(skill_id)
        if not skill:
            await self._orch._send_text(session_id, f"Skill not found: {skill_id}")
            return

        endpoint = next((ep for ep in skill.endpoints if ep.id == endpoint_id), None)
        if not endpoint:
            await self._orch._send_text(session_id, f"Endpoint not found: {endpoint_id}")
            return

        result = await self._orch.executor.execute(
            tool_name=tool_name, args=args, skill=skill, endpoint=endpoint,
        )

        if result["success"] and result["data"]:
            sdui = self._orch.genui.generate(
                data=result["data"],
                skill_brand=skill.brand.model_dump(),
                ui_hint=endpoint.ui_hint,
                endpoint_id=endpoint_id,
            )
            await self._orch._send_text(session_id, f"Here's the result from {skill.brand.name}:")
            await self._orch.send(session_id, FeralMessage(
                session_id=session_id, hop="brain", type="sdui",
                payload=SDUIPayload(root=sdui).model_dump(),
            ))
        else:
            error = result.get("error", "Unknown error")
            await self._orch._send_text(session_id, f"Failed to call {skill.brand.name}: {error}")

    # ─────────────────────────────────────────────
    # Subagent Parallel Execution
    # ─────────────────────────────────────────────

    async def spawn_subagents(self, session_id: str, args: dict) -> dict:
        """Run multiple sub-tasks in parallel with isolated subagent contexts."""
        tasks_arg = args.get("tasks")
        if isinstance(tasks_arg, str):
            tasks = [tasks_arg]
        elif isinstance(tasks_arg, list):
            tasks = [str(t).strip() for t in tasks_arg if str(t).strip()]
        else:
            single = str(args.get("task", "")).strip()
            tasks = [single] if single else []

        if not tasks:
            return {
                "success": False,
                "status_code": 400,
                "data": None,
                "error": "Provide 'tasks' (array) or 'task' (string) for subagent execution.",
            }
        llm = self._orch.llm
        if not llm or not llm.available:
            return {
                "success": False,
                "status_code": 503,
                "data": None,
                "error": "LLM unavailable; cannot spawn subagents.",
            }

        goal = str(args.get("goal", "") or "").strip()
        # 0 = unlimited (v2026.6.11 default) — spawned sub-workers still get
        # a concrete per-spawn default of 8 rounds unless the caller asks
        # for more, since they run unattended and in parallel.
        max_iterations = self._orch._max_iterations
        default_iters = min(max_iterations, 8) if max_iterations else 8
        try:
            max_workers = int(args.get("max_workers", min(3, len(tasks))) or 3)
        except Exception:
            max_workers = min(3, len(tasks))
        try:
            max_iters = int(args.get("max_iterations", default_iters) or default_iters)
        except Exception:
            max_iters = default_iters
        max_workers = max(1, min(max_workers, 6))
        max_iters = max(1, min(max_iters, 12))
        sem = asyncio.Semaphore(max_workers)

        async def _run_one(i: int, task_text: str) -> dict:
            scoped_task = task_text if not goal else f"Goal: {goal}\nTask: {task_text}"
            async with sem:
                started = time.time()
                self._active_subagent_tasks += 1
                try:
                    result = await self._run_subagent_task(
                        parent_session_id=session_id,
                        task_text=scoped_task,
                        max_iterations=max_iters,
                        ordinal=i,
                    )
                    result["elapsed_ms"] = round((time.time() - started) * 1000, 2)
                    return result
                except Exception as e:
                    return {
                        "task_index": i,
                        "task": task_text,
                        "success": False,
                        "result": "",
                        "error": str(e),
                        "iterations": 0,
                        "tool_calls_executed": 0,
                        "elapsed_ms": round((time.time() - started) * 1000, 2),
                    }
                finally:
                    self._active_subagent_tasks = max(0, self._active_subagent_tasks - 1)

        results = await asyncio.gather(*[_run_one(i, t) for i, t in enumerate(tasks, 1)])
        success_count = sum(1 for r in results if r.get("success"))

        return {
            "success": True,
            "status_code": 200,
            "data": {
                "goal": goal or None,
                "task_count": len(tasks),
                "max_workers": max_workers,
                "max_iterations": max_iters,
                "success_count": success_count,
                "results": results,
            },
            "error": None,
        }

    async def _run_subagent_task(
        self,
        *,
        parent_session_id: str,
        task_text: str,
        max_iterations: int,
        ordinal: int,
    ) -> dict:
        """Execute one subagent task with isolated history and full tool access."""
        orch = self._orch
        relevant_skills = await orch._route_prompt(task_text)
        tools = self.assemble_llm_tool_list(
            orch.skills, relevant_skills, mcp_client=orch._mcp_client,
        )

        frame = orch.perception.get_frame(parent_session_id)
        system_prompt = await orch._build_system_prompt(frame, relevant_skills, parent_session_id)
        history: list[dict] = [{"role": "user", "content": task_text}]
        sub_session_id = f"{parent_session_id}:sub:{ordinal}:{str(uuid4())[:6]}"
        final_text = ""
        tool_calls_executed = 0
        iterations_used = 0

        for i in range(max_iterations):
            iterations_used = i + 1
            response = await orch.llm.chat(
                messages=[{"role": "system", "content": system_prompt}, *history],
                tools=tools if tools else None,
            )
            text_content, tool_calls = orch.llm.extract_response(response)

            assistant_msg = {"role": "assistant"}
            if text_content:
                assistant_msg["content"] = text_content
            if "choices" in response and response["choices"]:
                raw_msg = response["choices"][0].get("message", {})
                if raw_msg.get("tool_calls"):
                    assistant_msg["tool_calls"] = raw_msg["tool_calls"]
            if len(assistant_msg) > 1:
                history.append(assistant_msg)

            if tool_calls:
                for tc in tool_calls:
                    if tc.get("name", "").startswith("subagent__"):
                        result_data = {
                            "success": False,
                            "error": "Nested subagent spawning is blocked to prevent recursion loops.",
                        }
                    else:
                        # Inherit the parent session's surface for any nested
                        # tool calls so the subagent can't escalate by virtue
                        # of running on an "unknown session" default.
                        result_data = await self.execute_tool_call_for_llm(
                            sub_session_id, tc, relevant_skills,
                            surface=self._resolve_surface_for_session(parent_session_id),
                        )
                    tool_calls_executed += 1
                    history.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", str(uuid4())[:8]),
                        "name": tc.get("name", ""),
                        # Subagents read and grep the same filesystem the
                        # parent does, so they get the same per-tool budget
                        # instead of the old blind [:2000] slice.
                        "content": serialize_tool_result(
                            tc.get("name", ""), result_data,
                            registry=self._orch.skills,
                        ),
                    })
                continue

            if text_content:
                final_text = text_content
            break

        if not final_text:
            final_text = "No final answer produced by subagent."

        return {
            "task_index": ordinal,
            "task": task_text,
            "success": True,
            "result": final_text,
            "error": None,
            "iterations": iterations_used,
            "tool_calls_executed": tool_calls_executed,
        }
