"""Wiring between action checkpoints and the live brain.

``skills/checkpoints.py`` is storage. It knows what an inverse call looks
like and it knows how to write the row, and it deliberately knows nothing
about how a tool call is made, because it is also read by ``feral
checkpoints`` in a process with no brain in it.

This module is the other half:

* :func:`record_reversal` runs at the executor chokepoint and turns a
  successful create into a stored compensation.
* :func:`make_compensator` hands ``revert_turn`` a way to place the
  inverse call, from a worker thread, on the running event loop.

Both fail soft. A checkpoint that cannot be written must never be the
reason a tool call fails, and a compensator that cannot be built comes
back as ``None`` so the revert reports honestly instead of pretending.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextvars import ContextVar
from typing import Callable, Optional

from skills.call_context import current_context
from skills.checkpoints import REVERSIBLE_ACTIONS, get_store

logger = logging.getLogger("feral.skills.checkpoint_actions")

__all__ = [
    "record_reversal",
    "make_compensator",
    "compensating_call",
    "COMPENSATION_TIMEOUT_S",
]

# A compensating call is one HTTP request or one local store write. If it
# has not answered in this long the revert reports it as failed rather
# than parking the thread the revert is running on.
COMPENSATION_TIMEOUT_S = 30.0

# The one inverse call currently being made to undo a checkpointed
# action, as ``(tool_name, target_id)``, or None. Read by
# ``SkillExecutor._gate``; see :func:`compensating_call`.
_COMPENSATING: ContextVar[Optional[tuple[str, str]]] = ContextVar(
    "feral_checkpoint_compensating", default=None,
)


def _first_value(args: dict) -> str:
    """The id an inverse call carries. Inverses take exactly one."""
    for value in (args or {}).values():
        return str(value)
    return ""


def compensating_call() -> Optional[tuple[str, str]]:
    """The inverse call this task is making to undo a checkpoint, if any.

    ``SkillExecutor._gate`` skips plan mode and the approval gate when
    this names the tool it is about to run. That is a deliberate and
    deliberately narrow exception, and it widens nothing:

    * It is only ever set by :func:`make_compensator`, around a single
      ``executor.execute`` call, and reset in a ``finally``.
    * The tool and the id both come from a ``reversals`` row FERAL wrote
      itself. No model-supplied argument reaches the call, and no tool
      can ask for this state.
    * It is only reachable from ``revert_turn``, which is confirm-tier
      itself. The operator has already been asked "undo this turn", and
      deleting the event that turn created is the thing they approved.
      Asking a second time per item does not add a decision, it blocks
      the one already made: the pending approval an inverse call would
      raise here has no resume path, because this call never went
      through ``ToolRunner``.
    """
    return _COMPENSATING.get()


def record_reversal(tool_name: str, result) -> Optional[str]:
    """Store how to undo ``tool_name`` from what it returned.

    Called after the tool ran, with its result, because the created
    object's id does not exist until then and a failed call created
    nothing to compensate.

    Returns the reversal id, or ``None`` when nothing was recorded. Never
    raises: this runs inside the executor's return path, and a
    bookkeeping failure must not turn a completed tool call into an
    error.

    When a registered create SUCCEEDS but no compensation could be
    stored, the tool's earned-autonomy streak is dropped on the spot.
    That is the only way the two stay honest with each other: the tool is
    in ``UNDOABLE_TOOLS`` on the promise that this record exists, so if
    the record is missing the tool has to go back to asking.
    """
    spec = REVERSIBLE_ACTIONS.get(tool_name)
    if spec is None:
        return None
    if not isinstance(result, dict) or result.get("success") is not True:
        # A refusal, a pending approval or a plain failure. Nothing was
        # created, so there is nothing to take back.
        return None

    try:
        ctx = current_context()
        if not ctx.turn_id:
            # No turn to group under, so a revert could never find this
            # row. Same fail-open shape file checkpoints already use for
            # unbound callers (cron, taskflows), and the same
            # consequence: no undo, so no trust.
            _revoke_trust(tool_name, "no turn id was bound for this call")
            return None

        reversal_id = get_store().capture_action(
            tool_name=tool_name,
            result=result,
            turn_id=ctx.turn_id,
            session_id=ctx.session_id,
            surface=ctx.surface,
            call_id=ctx.call_id,
        )
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.warning("action checkpoint failed for %s: %s", tool_name, exc)
        _revoke_trust(tool_name, f"the undo record could not be written: {exc}")
        return None

    if reversal_id is None:
        logger.warning(
            "%s succeeded but no id was found at %s in its result, so no "
            "undo was recorded; this tool cannot be trusted to run "
            "unattended until the result shape and %s agree again",
            tool_name, ".".join(spec.id_path), "skills/checkpoints.py",
        )
        _revoke_trust(tool_name, "the result carried no id to undo")
    return reversal_id


def _revoke_trust(tool_name: str, reason: str) -> None:
    """Send a tool back to asking for approval. Best effort by design."""
    try:
        from security.trust_ledger import get_ledger

        get_ledger().revoke(tool_name, reason=reason)
    except Exception as exc:  # noqa: BLE001 - never fails a tool call
        logger.debug("could not revoke trust for %s: %s", tool_name, exc)


def make_compensator(
    loop: Optional[asyncio.AbstractEventLoop] = None,
) -> Optional[Callable[[str, dict], dict]]:
    """Build the sync callable ``revert_turn`` uses to place inverse calls.

    Returns ``None`` when there is no live brain to dispatch through, which
    is the normal state of the CLI. The revert then reports its actions as
    ``unrecoverable`` and says why, rather than dropping them.

    The returned callable is synchronous because ``revert_turn`` runs in a
    worker thread (SQLite, blob reads and file restores are all blocking).
    It marshals each call back onto ``loop`` with
    ``run_coroutine_threadsafe`` and waits with a timeout, so a provider
    that never answers costs one entry rather than the whole revert.

    The inverse call goes through ``SkillExecutor.execute``, so it gets
    the executor's rate limiting, parameter defaults and audit row like
    any other call. It is exempted from the approval and plan-mode gates
    for the duration; :func:`compensating_call` says why, and why that
    exemption widens nothing.
    """
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None

    state_mod = sys.modules.get("api.state")
    state_obj = getattr(state_mod, "state", None)
    executor = getattr(state_obj, "skill_executor", None)
    registry = getattr(state_obj, "skill_registry", None)
    if executor is None or registry is None:
        return None

    def _compensate(inverse_tool: str, args: dict) -> dict:
        skill_id, _, endpoint_id = inverse_tool.partition("__")
        manifest = getattr(registry, "skills", {}).get(skill_id)
        if manifest is None:
            return {
                "success": False,
                "error": f"skill '{skill_id}' is not registered on this brain.",
            }
        endpoint = next(
            (ep for ep in manifest.endpoints if ep.id == endpoint_id), None,
        )
        if endpoint is None:
            return {
                "success": False,
                "error": f"'{skill_id}' has no endpoint '{endpoint_id}'.",
            }

        async def _run():
            # Set INSIDE the coroutine, not in this worker thread. A
            # contextvar set here would not reach the task
            # run_coroutine_threadsafe creates on the loop, so the gate
            # would never see it and every compensation under hybrid
            # would come back pending_approval.
            token = _COMPENSATING.set((inverse_tool, _first_value(args)))
            try:
                return await executor.execute(
                    inverse_tool, dict(args), manifest, endpoint,
                )
            finally:
                _COMPENSATING.reset(token)

        future = asyncio.run_coroutine_threadsafe(_run(), loop)
        try:
            outcome = future.result(timeout=COMPENSATION_TIMEOUT_S)
        except TimeoutError:
            future.cancel()
            return {
                "success": False,
                "error": (
                    f"{inverse_tool} did not answer within "
                    f"{COMPENSATION_TIMEOUT_S:.0f}s."
                ),
            }
        if isinstance(outcome, dict):
            return outcome
        return {"success": False, "error": f"{inverse_tool} returned no envelope."}

    return _compensate
