"""Plan mode: a per-session posture in which the agent researches and
proposes but cannot mutate state.

Not a fourth autonomy mode
--------------------------
``security.autonomy_mode`` (strict / hybrid / loose) is persisted and
global: it answers "how much confirmation does this operator want?".
Plan mode is per-session and ephemeral: it answers "is this particular
conversation allowed to change anything right now?". They are orthogonal
axes and are kept in separate state. Entering or leaving plan mode never
reads or writes the autonomy mode, and approving a plan never grants a
standing tool approval, so the turns that follow an approved plan still
go through the session's autonomy mode for every mutating call.

Not the same thing as ``agents/coding_run.py``
----------------------------------------------
``CodingRun`` has a ``CodingRunPhase.PLAN`` and a ``Planner`` protocol,
but it is a plan-then-apply loop with no human in the middle, and it is
unwired (only ``cli/main.py``'s doctor check constructs its store). Its
module docstring also claims writes go through the ``coding_tools``
skill surface so SandboxPolicy gates them; the edit phase in fact calls
``target.write_text(...)`` directly. Neither the phase nor the planner is
reused here.

Two enforcement points
----------------------
1. **Exposure** (:func:`filter_tools_for_plan_mode`, applied by the
   orchestrator after ``get_tools_for_skills``). Advisory only. A model
   can emit a tool name it was never given, and the voice surfaces build
   their tool list from ``SkillRegistry.get_all_tools()`` on a path the
   orchestrator never touches.
2. **Dispatch** (``ToolRunner``, using :func:`is_plan_safe_tool` and
   :func:`plan_mode_refusal`). This is the gate that actually holds. It
   sits above the MCP branch, the subagent branch and the daemon branch,
   so every LLM-originated call passes through it whatever surface
   assembled the tool list.

Plan-safe means DECLARED safe
-----------------------------
An endpoint is plan-safe only when its manifest says ``read_only_hint:
true`` or ``safety_tier: "safe"``. Absence of metadata is not-plan-safe.
That is why this module calls ``security.safety_resolver.is_read_only``
with ``strict=True``: the lenient default falls back to a substring
token list that admits anything containing ``read``, ``status``,
``list`` or ``current``, which is not a boundary a mode called "cannot
mutate" can stand on.

Entry is explicit only
----------------------
A ``/plan`` meta-command or an API call. Never a heuristic on the user's
prose. And the model can never leave: :meth:`PlanModeState.exit` ignores
any actor other than ``"user"``.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Iterable, Optional

from security.safety_resolver import is_read_only

logger = logging.getLogger("feral.agents.plan_mode")

__all__ = [
    "PLAN_MODE_ALWAYS_ALLOWED",
    "PLAN_MODE_ALWAYS_BLOCKED",
    "PLAN_REFUSAL_CODE",
    "PlanModeState",
    "filter_skills_for_plan_mode",
    "filter_tools_for_plan_mode",
    "is_plan_safe_tool",
    "plan_mode_refusal",
    "plan_mode_prompt_block",
]

PLAN_REFUSAL_CODE = "plan_mode_blocked"

# The plan skill's own endpoint. It is declared ``read_only_hint: true``
# and ``safety_tier: "safe"`` in ``skills/manifests/plan.json`` so it
# survives the filter on its own merits; the literal is here so it also
# survives on the paths where no real registry is wired (early boot, the
# MagicMock registries unit tests use). Without it, plan mode would be a
# mode with no way to produce its one artefact.
PLAN_MODE_ALWAYS_ALLOWED = frozenset({"plan__submit"})

# Subagents run through the same dispatch gate, but under a session id
# minted by ``ToolRunner._run_subagent_task`` that the parent's plan-mode
# entry does not cover. :meth:`PlanModeState.is_active` walks that ``:sub:``
# ancestry, and ``Orchestrator.spawn_subsession`` mints opaque uuid4 ids
# that no ancestry walk can recover. So spawning is refused outright rather
# than relying on inheritance.
PLAN_MODE_ALWAYS_BLOCKED = frozenset({"subagent__spawn_subagent"})

# Sub-session id separator minted by ``ToolRunner._run_subagent_task``:
# ``f"{parent_session_id}:sub:{ordinal}:{rand}"``.
_SUBSESSION_SEP = ":sub:"

# Documented env default. ``config/loader.py`` is owned by another lane
# this wave, so the setting is read from the environment here and the
# settings.json key ``security.plan_mode_default`` can mirror it later
# without changing this module's contract.
_ENV_DEFAULT = "FERAL_PLAN_MODE_DEFAULT"


def plan_mode_default_on() -> bool:
    """Whether new sessions start in plan mode. Default off.

    Mirrors ``security.plan_mode_default`` once ``config/loader.py``
    grows the key; until then ``FERAL_PLAN_MODE_DEFAULT=1`` is the
    documented switch.
    """
    raw = (os.environ.get(_ENV_DEFAULT) or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


# ── classification ────────────────────────────────────────────────────


def _tool_name_of(tool: Any) -> str:
    """Pull the LLM-facing name out of an OpenAI-shaped tool definition."""
    if isinstance(tool, str):
        return tool
    if isinstance(tool, dict):
        fn = tool.get("function")
        if isinstance(fn, dict):
            return str(fn.get("name") or "")
        return str(tool.get("name") or "")
    return ""


def is_plan_safe_tool(tool_name: str, registry: Any = None) -> bool:
    """True when ``tool_name`` is declared incapable of mutating state.

    Fails closed on everything that has no FERAL manifest behind it:
    MCP tools (``mcp_*``) and daemon commands (``daemon_*``) carry no
    ``read_only_hint`` / ``safety_tier``, so there is nothing to trust.
    """
    name = (tool_name or "").strip()
    if not name:
        return False
    if name in PLAN_MODE_ALWAYS_BLOCKED:
        return False
    if name in PLAN_MODE_ALWAYS_ALLOWED:
        return True
    if name.startswith("mcp_") or name.startswith("daemon_"):
        return False
    return is_read_only(name, registry=registry, strict=True)


def filter_tools_for_plan_mode(tools: Optional[list], registry: Any = None) -> list:
    """Exposure filter for the LLM tool array. Advisory, see module docs."""
    if not tools:
        return []
    return [t for t in tools if is_plan_safe_tool(_tool_name_of(t), registry)]


def filter_skills_for_plan_mode(
    skills: Optional[Iterable],
    registry: Any = None,
) -> list:
    """Prune manifests to their plan-safe endpoints, for the PROMPT view.

    ``self_model.build_tooling_catalog`` enumerates every endpoint of
    every active skill straight from the manifest, independent of the
    ``tools`` array actually sent. Passing the unfiltered list would tell
    the model ``coding_tools__edit_file`` is "Active this turn" and then
    refuse it at dispatch, which reads as a malfunction rather than a
    posture. Pruning happens at endpoint granularity because a single
    skill routinely mixes both kinds (``coding_tools`` has both
    ``read_file`` and ``edit_file``); skills left with no plan-safe
    endpoint drop out entirely.
    """
    out: list = []
    for skill in list(skills or []):
        skill_id = getattr(skill, "skill_id", "")
        endpoints = getattr(skill, "endpoints", None) or []
        kept = [
            ep for ep in endpoints
            if is_plan_safe_tool(f"{skill_id}__{getattr(ep, 'id', '')}", registry)
        ]
        if not kept:
            continue
        if len(kept) == len(endpoints):
            out.append(skill)
            continue
        try:
            out.append(skill.model_copy(update={"endpoints": kept}))
        except Exception:
            # Never let a prompt-shaping helper break the turn.
            logger.debug("plan-mode skill prune failed for %s", skill_id, exc_info=True)
            out.append(skill)
    return out


def plan_mode_refusal(tool_name: str, *, session_id: str = "") -> dict:
    """Structured refusal returned by the dispatch gate.

    Shaped like the other tool-error envelopes so the model reads it as a
    tool result rather than as prose, and marked ``fixable_by_llm: False``
    because retrying is not the fix: only the user can leave plan mode.
    """
    reason = (
        f"Plan mode is active for this session, so '{tool_name}' was not run. "
        "Plan mode allows research and proposals only. Finish investigating "
        "with read-only tools, then call `plan__submit` with the plan. The "
        "user leaves plan mode with `/plan off` (discard) or `/plan approve`; "
        "you cannot leave it yourself, and saying you have left it does not "
        "make it so."
    )
    return {
        "success": False,
        "is_error": True,
        "error_code": PLAN_REFUSAL_CODE,
        "error": reason,
        "reason": reason,
        "plan_mode": True,
        "tool_name": tool_name,
        "session_id": session_id,
        "fixable_by_llm": False,
    }


def plan_mode_prompt_block() -> str:
    """System-prompt section injected while plan mode is active."""
    return (
        "## Plan Mode (ACTIVE)\n"
        "This session is in plan mode. You are researching and proposing, "
        "not acting.\n"
        "- Only read-only tools are available. Every mutating call is refused "
        "at dispatch, whatever tool list you were given.\n"
        "- Do not spawn subagents. They are blocked too.\n"
        "- When you have enough to propose, call `plan__submit` with a short "
        "summary and the ordered steps you would take, including which tools "
        "each step needs.\n"
        "- You cannot leave plan mode. Only the user can, with `/plan off` or "
        "`/plan approve`. Do not claim to have left it, and do not ask to be "
        "given write access mid-turn.\n"
        "- Submitting a plan does not start the work and does not pre-approve "
        "anything. After approval each step is still subject to the normal "
        "approval flow.\n"
    )


# ── session state ─────────────────────────────────────────────────────


class PlanModeState:
    """Ephemeral, per-session plan-mode registry.

    Deliberately in-process and unpersisted. Plan mode is a property of a
    conversation in flight, not of the install; a restart should not
    resurrect a posture the user cannot see. Contrast
    ``security.autonomy_mode``, which is persisted precisely because it
    IS a property of the install.
    """

    def __init__(self) -> None:
        # ACTIVE plan-mode sessions only. Membership here is what
        # `is_active` answers from, so nothing but `enter()` may add a key:
        # if submitting a plan could create one, calling `plan__submit`
        # outside plan mode would silently put the session INTO plan mode,
        # which is exactly the heuristic entry this design rules out.
        self._sessions: dict[str, dict] = {}
        # Submitted plans, keyed separately for that reason.
        self._plans: dict[str, list[dict]] = {}

    # -- ancestry ------------------------------------------------------

    @staticmethod
    def _ancestors(session_id: str) -> list[str]:
        """``a:sub:0:x`` -> ``["a:sub:0:x", "a"]``.

        Sub-session ids are built by ``ToolRunner._run_subagent_task`` as
        ``f"{parent}:sub:{ordinal}:{rand}"``, so a child is not literally
        in the plan-mode set. Walking the chain makes inheritance work for
        that shape. It cannot work for
        ``agents.subagent_spawner.spawn_subsession``, whose child ids are
        bare uuid4 with no parent in them, which is the second reason
        spawning is refused outright in plan mode.
        """
        sid = session_id or ""
        chain = [sid]
        while _SUBSESSION_SEP in sid:
            sid = sid.split(_SUBSESSION_SEP, 1)[0]
            chain.append(sid)
        return chain

    def root_session(self, session_id: str) -> str:
        return self._ancestors(session_id)[-1]

    # -- lifecycle -----------------------------------------------------

    def enter(
        self,
        session_id: str,
        *,
        reason: str = "",
        entered_by: str = "user",
    ) -> dict:
        """Put ``session_id`` into plan mode. Idempotent."""
        if not session_id:
            return self.describe(session_id)
        existing = self._sessions.get(session_id)
        if existing is None:
            existing = {
                "entered_at": time.time(),
                "reason": reason,
                "entered_by": entered_by,
            }
            self._sessions[session_id] = existing
            logger.info(
                "plan mode ON for session %s (by=%s reason=%r)",
                session_id[:8], entered_by, reason[:80],
            )
        return self.describe(session_id)

    def exit(
        self,
        session_id: str,
        *,
        approved: bool = False,
        actor: str = "user",
    ) -> bool:
        """Leave plan mode. Returns True when state actually changed.

        ``actor`` must be ``"user"``. A model-attributed exit is refused,
        which is the mechanism behind "the model can never exit plan mode
        itself": there is no tool, no meta-command reachable from model
        output, and no argument that reaches this method with the right
        actor.

        ``approved`` is recorded for the UI only. It grants nothing: no
        standing tool approval is created here, so every mutating call in
        the turns that follow still goes through the session's autonomy
        mode. Granting blanket approval on plan approval would be a real
        security regression, so the flag deliberately has no privilege
        attached to it.
        """
        if actor != "user":
            logger.warning(
                "refused plan-mode exit attributed to actor=%r for session %s",
                actor, (session_id or "")[:8],
            )
            return False
        record = self._sessions.pop(session_id, None)
        if record is None:
            return False
        logger.info(
            "plan mode OFF for session %s (approved=%s)",
            (session_id or "")[:8], approved,
        )
        return True

    def is_active(self, session_id: str) -> bool:
        if not session_id:
            return False
        return any(sid in self._sessions for sid in self._ancestors(session_id))

    def clear_session(self, session_id: str) -> None:
        """Session teardown hook. Plan mode dies with the session."""
        self._sessions.pop(session_id, None)
        self._plans.pop(session_id, None)

    def active_sessions(self) -> list[str]:
        return sorted(self._sessions)

    # -- plans ---------------------------------------------------------

    def _plan_owner(self, session_id: str) -> str:
        """The session a plan belongs to: the nearest ancestor that is
        actually in plan mode, else the session itself."""
        return next(
            (sid for sid in self._ancestors(session_id) if sid in self._sessions),
            session_id,
        )

    def record_plan(self, session_id: str, plan: dict) -> dict:
        """Store a submitted plan. Never activates plan mode.

        Kept out of ``_sessions`` deliberately. If this wrote there, a
        ``plan__submit`` call made outside plan mode (the skill is
        routable on its trigger phrases like any other) would flip the
        session into plan mode as a side effect of the model choosing a
        tool, which is precisely the heuristic entry this design rules out.
        """
        stored = dict(plan)
        stored["submitted_at"] = time.time()
        self._plans.setdefault(self._plan_owner(session_id), []).append(stored)
        return stored

    def latest_plan(self, session_id: str) -> Optional[dict]:
        for sid in self._ancestors(session_id):
            plans = self._plans.get(sid)
            if plans:
                return plans[-1]
        return None

    def describe(self, session_id: str) -> dict:
        record = self._sessions.get(session_id)
        return {
            "session_id": session_id,
            "plan_mode": self.is_active(session_id),
            "entered_at": (record or {}).get("entered_at"),
            "reason": (record or {}).get("reason", ""),
            "entered_by": (record or {}).get("entered_by", ""),
            "plan_count": len(self._plans.get(self._plan_owner(session_id), [])),
            "latest_plan": self.latest_plan(session_id),
        }
