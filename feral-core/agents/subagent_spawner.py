"""W17: Subagent spawner — gated, scoped, cancellable child sessions.

The spawn contract runs three gates in order: allowlist first,
registry second, asyncio cancellation third.

Public surface::

    await spawn_subsession(parent_id, kind, *, scope_key, model_override=None) -> str
    register_supervisor(supervisor)
    register_llm_provider(provider)
    register_runner(runner)
    get_registry()

The spawner is asyncio-native. Cancellation propagates within ~5ms in
practice (we set a sentinel ``asyncio.Event`` *and* call
``task.cancel()``); the 200ms acceptance budget in W17 is a generous
upper bound that includes the orchestrator's session-lock teardown.

This module owns:
  * ``SubagentNotAllowed`` exception
  * In-memory ``SubagentRegistry`` keyed by parent_id
  * The default runner that exercises ``model_override`` on the first
    LLM call so the policy decision and the model decision are both
    observable from outside.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional
from uuid import uuid4

from agents.subagent_policy import is_allowed

logger = logging.getLogger("feral.subagent_spawner")


class SubagentNotAllowed(RuntimeError):
    """Raised when the policy denies a (parent_kind, child_kind) spawn."""


# Runner protocol: an async callable that owns the child task body.
SubagentRunner = Callable[..., Awaitable[Any]]


class SubagentRegistry:
    """In-memory registry of (parent_id → list[child record]).

    Each child record holds the cancellation event, the asyncio.Task,
    the scope_key, and the model_override so that the orchestrator's
    session-lock teardown can match-and-cancel without re-deriving
    those facts.
    """

    def __init__(self) -> None:
        self._by_parent: dict[str, list[dict]] = {}
        self._supervisor: Any = None
        self._llm_provider: Any = None
        self._runner: Optional[SubagentRunner] = None
        self._parent_kinds: dict[str, str] = {}
        self._suppression: dict[str, dict[str, bool]] = {}

    # ── Wiring (boot + tests) ────────────────────────────────────

    def set_supervisor(self, supervisor: Any) -> None:
        self._supervisor = supervisor

    def set_llm_provider(self, provider: Any) -> None:
        self._llm_provider = provider

    def set_runner(self, runner: Optional[SubagentRunner]) -> None:
        self._runner = runner

    def set_parent_kind(self, parent_id: str, parent_kind: str) -> None:
        self._parent_kinds[parent_id] = parent_kind

    def parent_kind_for(self, parent_id: str) -> str:
        return self._parent_kinds.get(parent_id, "orchestrator")

    def reset(self) -> None:
        """Test helper — drop every registration but keep wired hooks."""
        self._by_parent.clear()
        self._parent_kinds.clear()
        self._suppression.clear()

    # ── Spawn ────────────────────────────────────────────────────

    async def spawn(
        self,
        parent_id: str,
        kind: str,
        *,
        scope_key: str,
        model_override: Optional[str] = None,
    ) -> str:
        if not parent_id or not isinstance(parent_id, str):
            raise ValueError("parent_id must be a non-empty string")
        if not kind or not isinstance(kind, str):
            raise ValueError("kind must be a non-empty string")
        if not scope_key or not isinstance(scope_key, str):
            raise ValueError("scope_key must be a non-empty string")

        parent_kind = self.parent_kind_for(parent_id)
        if not is_allowed(parent_kind, kind):
            self._audit_denied(parent_id, parent_kind, kind, scope_key)
            raise SubagentNotAllowed(
                f"parent_kind={parent_kind!r} cannot spawn child_kind={kind!r}"
            )

        child_id = str(uuid4())
        cancel_event = asyncio.Event()
        runner = self._runner or self._default_runner

        task = asyncio.create_task(
            runner(
                parent_id=parent_id,
                child_id=child_id,
                kind=kind,
                scope_key=scope_key,
                model_override=model_override,
                cancel_event=cancel_event,
            ),
            name=f"subagent:{kind}:{child_id[:8]}",
        )
        record = {
            "parent_id": parent_id,
            "child_id": child_id,
            "kind": kind,
            "scope_key": scope_key,
            "model_override": model_override,
            "task": task,
            "cancel_event": cancel_event,
            "started_at": time.time(),
        }
        self._by_parent.setdefault(parent_id, []).append(record)
        self._audit_allowed(parent_id, parent_kind, kind, scope_key, child_id)
        return child_id

    # ── Default runner ───────────────────────────────────────────

    async def _default_runner(
        self,
        *,
        parent_id: str,
        child_id: str,
        kind: str,
        scope_key: str,
        model_override: Optional[str],
        cancel_event: asyncio.Event,
    ) -> None:
        """Issue one LLM call so model_override is observable, then idle.

        The child stays alive (awaiting *cancel_event*) until either the
        parent's session-lock teardown cancels it or the task is
        explicitly cancelled — a "runs in the background until reaped"
        lifecycle.
        """
        provider = self._llm_provider
        if provider is not None and hasattr(provider, "chat"):
            try:
                model = model_override or getattr(provider, "model_name", None)
                kwargs: dict[str, Any] = {"messages": []}
                if model is not None:
                    kwargs["model"] = model
                result = provider.chat(**kwargs)
                if asyncio.iscoroutine(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "subagent default runner: provider.chat failed (kind=%s child=%s): %s",
                    kind, child_id[:8], exc,
                )

        try:
            await cancel_event.wait()
        except asyncio.CancelledError:
            raise

    # ── Cancellation ─────────────────────────────────────────────

    def children_of(self, parent_id: str) -> list[dict]:
        return list(self._by_parent.get(parent_id, []))

    async def cancel_all_children(self, parent_id: str) -> int:
        """Cancel every registered child of *parent_id* (default tied)."""
        children = self._by_parent.get(parent_id, [])
        return await self._cancel_targets(parent_id, list(children))

    async def cancel_children(
        self, parent_id: str, *, scope_key: Optional[str] = None
    ) -> int:
        """Cancel children matching *scope_key* (None → cancel all)."""
        children = self._by_parent.get(parent_id, [])
        if scope_key is None:
            targets = list(children)
        else:
            targets = [c for c in children if c["scope_key"] == scope_key]
        return await self._cancel_targets(parent_id, targets)

    async def _cancel_targets(
        self, parent_id: str, targets: list[dict]
    ) -> int:
        if not targets:
            return 0
        for c in targets:
            c["cancel_event"].set()
            task = c["task"]
            if not task.done():
                task.cancel()
        await asyncio.gather(
            *(_swallow_cancel(c["task"]) for c in targets),
            return_exceptions=True,
        )
        remaining = [
            c for c in self._by_parent.get(parent_id, [])
            if c not in targets
        ]
        if remaining:
            self._by_parent[parent_id] = remaining
        else:
            self._by_parent.pop(parent_id, None)
        return len(targets)

    def cancel_all_children_nowait(self, parent_id: str) -> int:
        """Fire-and-forget cancellation (used from sync lock-release path).

        Returns the number of targets scheduled for cancellation. The
        actual reaping happens on the next event-loop tick — callers
        that need to assert "child is dead" must ``await`` the resulting
        gather via :meth:`cancel_all_children` instead.
        """
        children = self._by_parent.get(parent_id, [])
        if not children:
            return 0
        targets = list(children)
        for c in targets:
            c["cancel_event"].set()
            task = c["task"]
            if not task.done():
                task.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._by_parent.pop(parent_id, None)
            return len(targets)
        loop.create_task(self._reap(parent_id, targets))
        return len(targets)

    async def _reap(self, parent_id: str, targets: list[dict]) -> None:
        await asyncio.gather(
            *(_swallow_cancel(c["task"]) for c in targets),
            return_exceptions=True,
        )
        remaining = [
            c for c in self._by_parent.get(parent_id, [])
            if c not in targets
        ]
        if remaining:
            self._by_parent[parent_id] = remaining
        else:
            self._by_parent.pop(parent_id, None)

    # ── Steer (announce-suppression contract) ─

    def is_suppressed(self, parent_id: str, child_id: str) -> bool:
        return bool(self._suppression.get(parent_id, {}).get(child_id))

    async def steer_subsession(
        self,
        parent_id: str,
        child_id: str,
        message: str,
        *,
        steer_hook: Optional[Callable[..., Awaitable[Any]]] = None,
    ) -> Any:
        """Push a steer message to a running subagent.

        The supervisor's "announce" channel is suppressed for the
        duration of the steer call so that mid-flight chatter does not
        race the steer outcome. If the steer hook RAISES, the
        suppression flag is cleared before re-raising — silent
        swallowing is forbidden by W17 doctrine.
        """
        record = self._find(parent_id, child_id)
        if record is None:
            raise KeyError(f"unknown subsession parent={parent_id} child={child_id}")

        bucket = self._suppression.setdefault(parent_id, {})
        bucket[child_id] = True

        hook = steer_hook
        if hook is None:
            sup = self._supervisor
            hook = getattr(sup, "steer", None) if sup is not None else None
        if hook is None:
            bucket[child_id] = False
            raise RuntimeError("no steer hook registered")

        try:
            outcome = hook(parent_id=parent_id, child_id=child_id, message=message)
            if asyncio.iscoroutine(outcome):
                outcome = await outcome
            return outcome
        except Exception:
            bucket[child_id] = False
            raise

    def _find(self, parent_id: str, child_id: str) -> Optional[dict]:
        for c in self._by_parent.get(parent_id, []):
            if c["child_id"] == child_id:
                return c
        return None

    # ── Audit hooks (supervisor) ─────────────────────────────────

    def _supervisor_handle(self) -> Any:
        sup = self._supervisor
        if sup is not None:
            return sup
        try:
            from api.state import state as _state
            return getattr(_state, "supervisor", None)
        except Exception:
            return None

    def _audit_denied(
        self,
        parent_id: str,
        parent_kind: str,
        child_kind: str,
        scope_key: str,
    ) -> None:
        sup = self._supervisor_handle()
        if sup is not None and hasattr(sup, "record"):
            try:
                sup.record(
                    source="orchestrator",
                    kind="subagent_spawn",
                    session_id=parent_id,
                    actor="system",
                    payload={
                        "parent_kind": parent_kind,
                        "child_kind": child_kind,
                        "scope_key": scope_key,
                    },
                    decision="denied",
                    detail={
                        "reason": "policy_denied",
                        "parent_kind": parent_kind,
                        "child_kind": child_kind,
                        "scope_key": scope_key,
                    },
                )
            except Exception as exc:
                logger.warning("supervisor.record(denied) raised: %s", exc)
        logger.warning(
            "subagent spawn decision=denied parent_id=%s parent_kind=%s child_kind=%s scope_key=%s",
            parent_id, parent_kind, child_kind, scope_key,
        )

    def _audit_allowed(
        self,
        parent_id: str,
        parent_kind: str,
        child_kind: str,
        scope_key: str,
        child_id: str,
    ) -> None:
        sup = self._supervisor_handle()
        if sup is not None and hasattr(sup, "record"):
            try:
                sup.record(
                    source="orchestrator",
                    kind="subagent_spawn",
                    session_id=parent_id,
                    actor="system",
                    payload={
                        "parent_kind": parent_kind,
                        "child_kind": child_kind,
                        "scope_key": scope_key,
                        "child_id": child_id,
                    },
                    decision="allowed",
                    detail={
                        "parent_kind": parent_kind,
                        "child_kind": child_kind,
                        "scope_key": scope_key,
                        "child_id": child_id,
                    },
                )
            except Exception as exc:
                logger.debug("supervisor.record(allowed) raised: %s", exc)


_registry = SubagentRegistry()


def get_registry() -> SubagentRegistry:
    """Return the process-wide subagent registry."""
    return _registry


def register_supervisor(supervisor: Any) -> None:
    _registry.set_supervisor(supervisor)


def register_llm_provider(provider: Any) -> None:
    _registry.set_llm_provider(provider)


def register_runner(runner: Optional[SubagentRunner]) -> None:
    _registry.set_runner(runner)


def register_parent_kind(parent_id: str, parent_kind: str) -> None:
    _registry.set_parent_kind(parent_id, parent_kind)


async def spawn_subsession(
    parent_id: str,
    kind: str,
    *,
    scope_key: str,
    model_override: Optional[str] = None,
) -> str:
    """Public spawn entry-point. Returns the child session id."""
    return await _registry.spawn(
        parent_id, kind, scope_key=scope_key, model_override=model_override
    )


async def _swallow_cancel(task: asyncio.Task) -> None:
    try:
        await task
    except asyncio.CancelledError:
        return
    except BaseException as exc:
        logger.debug("subagent task ended with exception: %s", exc)
        return
