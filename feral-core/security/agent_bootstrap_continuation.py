"""Reviewed continuation protocol; no production state adapter is installed here.

BrainState.init currently publishes services progressively and server startup owns
additional hooks. An adapter must fence agent actions throughout that interval,
checkpoint before side effects, preserve the authenticated vault, start missing
hooks exactly once, and fail closed after partial initialization. Wrapping init
alone does not satisfy this protocol, so support is disabled by default.
"""
from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable, Awaitable


class BootstrapRefusal(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        message = "Current startup settings or pending OAuth state require automatic changes. Resolve the model, phone-bridge or pending authorization state explicitly before reviewing continuation; no startup effects were dispatched." if code == "bootstrap_config_requires_update" else "Agent continuation cannot proceed. Refresh its status; private details are withheld."
        super().__init__(message)


@dataclass(frozen=True)
class BootstrapAdapter:
    """Trusted release adapter, never constructed from HTTP request parameters.

    revision returns an immutable bytes snapshot covering all effective bootstrap
    settings (including credential/environment revisions), without exposing them.
    run calls its synchronous checkpoint before each new side-effect boundary.
    fence prevents other action entry points from using partially published state.
    commit is synchronous, clears bootstrap_required only after successful hooks.
    fail_closed must disable partially constructed services and retain the gate;
    process restart is required after any dispatched failure, even if cleanup works.
    """
    revision: Callable[[], bytes] = field(repr=False)
    fence: Callable[[], None] = field(repr=False)
    run: Callable[[Callable[[], None]], Awaitable[None]] = field(repr=False)
    commit: Callable[[], None] = field(repr=False)
    fail_closed: Callable[[], Awaitable[None]] = field(repr=False)
    hooks_complete: Callable[[], bool] = field(repr=False)


@dataclass(frozen=True)
class _Binding:
    state: object = field(repr=False)
    config: object = field(repr=False)
    memory: object = field(repr=False)
    vault: object = field(repr=False)
    generation: int
    revision: bytes = field(repr=False)
    adapter: BootstrapAdapter = field(repr=False)


class AgentBootstrapContinuation:
    TTL = 300
    SCOPE = (
        "Resume the previously blocked full agent bootstrap using authenticated "
        "credentials and restored encrypted memory. This is separate from vault "
        "unlock: configured providers may be contacted, MCP and device/network "
        "integrations may connect, background processing, scheduled jobs and "
        "proactive services may start, and local operational state may be written. "
        "Enabled services follow their existing settings; local embedding "
        "dependencies may require a model download on first use. Existing policy "
        "controls still apply. Partial startup cannot be rolled back reliably. A failure "
        "or changed identity requires process restart, not an automatic retry."
    )

    def __init__(self, state, coordinator, adapter: BootstrapAdapter | None = None):
        self.state = state
        self.coordinator = coordinator
        self.adapter = adapter
        self._reviews = {}
        self._operation = None
        self._phase = "pending"
        self._restart_required = False
        self._completed = None
        self._active_binding = None

    def _binding(self):
        if self.adapter is None:
            raise BootstrapRefusal("continuation_unsupported")
        try:
            vault = self.coordinator.require_ready()
            if self.coordinator.status()["in_flight"]:
                raise ValueError()
            if self.state.vault is not vault or self.state.memory is None:
                raise ValueError()
            revision = self.adapter.revision()
            if type(revision) is not bytes or len(revision) > 1024 * 1024:
                raise ValueError()
            return _Binding(self.state, self.state.config, self.state.memory, vault,
                            self.coordinator._generation, hashlib.sha256(revision).digest(), self.adapter)
        except BootstrapRefusal:
            raise
        except Exception:
            raise BootstrapRefusal("identity_unavailable") from None

    def _same(self, held):
        fresh = self._binding()
        if (fresh.state is not held.state or fresh.config is not held.config or
            fresh.memory is not held.memory or fresh.vault is not held.vault or
            fresh.adapter is not held.adapter or fresh.generation != held.generation or
            fresh.revision != held.revision):
            raise BootstrapRefusal("identity_changed")

    def _preflight(self):
        if self._restart_required:
            raise BootstrapRefusal("restart_required")
        if self._operation is not None and not self._operation.done():
            raise BootstrapRefusal("operation_in_progress")
        if not self.state._native_bootstrap_required:
            raise BootstrapRefusal("continuation_not_required")
        if getattr(self.state, "orchestrator", None) is not None:
            raise BootstrapRefusal("partial_state_present")
        return self._binding()

    def status(self):
        # Passive: revision callback is deliberately excluded from status.
        return {"supported": self.adapter is not None, "phase": self._phase,
                "in_flight": self._operation is not None and not self._operation.done(),
                "restart_required": self._restart_required,
                "credentials_available": self.coordinator.status()["credentials_available"],
                "memory_available": self.state.memory is not None,
                "orchestrator_available": getattr(self.state, "orchestrator", None) is not None,
                "bootstrap_required": bool(self.state._native_bootstrap_required),
                "agent_ready": self._phase == "ready" and self._completed is not None and
                  self.coordinator._generation == self._completed.generation and
                  self.coordinator._vault is self._completed.vault and
                  self.state.vault is self._completed.vault and
                  self.state.config is self._completed.config and
                  self.state.memory is self._completed.memory and not self._restart_required and
                  not self.state._native_bootstrap_required and self.state.memory is not None and
                  getattr(self.state, "orchestrator", None) is not None and
                  self.coordinator.status()["credentials_available"]}

    def review(self):
        held = self._preflight()
        now = time.monotonic()
        self._reviews = {k: v for k, v in self._reviews.items() if now - v[0] <= self.TTL}
        if len(self._reviews) >= 16:
            raise BootstrapRefusal("review_limit")
        token = secrets.token_urlsafe(32)
        self._reviews[token] = (now, held)
        return {"review_token": token, "expires_in_seconds": self.TTL,
                "scope": self.SCOPE, "previous_status": self.status()}

    def invalidate(self):
        """Synchronous lock callback: immediately fence/cancel active bootstrap."""
        self._reviews.clear()
        if self._operation is not None and not self._operation.done():
            self._restart_required = True
            self._phase = "failed"
            self.state._native_bootstrap_required = True
            if self._operation is not asyncio.current_task():
                self._operation.cancel()
        elif self._phase == "ready":
            self._restart_required = True
            self._phase = "failed"
            self.state._native_bootstrap_required = True
            self._operation = asyncio.create_task(self._cleanup_after_lock())
        return self._active_binding is not None

    async def _cleanup_after_lock(self):
        try:
            await self._completed.adapter.fail_closed()
        except BaseException:
            self._phase = "cleanup_failed"

    def cancel(self, token):
        self._reviews.pop(token, None)

    async def confirm(self, token, *, timeout=5.0):
        review = self._reviews.pop(token, None)
        if review is None or time.monotonic() - review[0] > self.TTL:
            raise BootstrapRefusal("review_expired")
        self._preflight()
        self._same(review[1])
        self._reviews.clear()
        self._phase = "starting"
        self._operation = asyncio.create_task(self._run(review[1]))
        try:
            await asyncio.wait_for(asyncio.shield(self._operation), timeout=max(0, min(timeout, 30)))
        except asyncio.TimeoutError:
            pass  # Explicit operation continues; no second dispatch or retry.
        except asyncio.CancelledError:
            # A lock may cancel the owned task before its first instruction.
            # That is a failed operation, distinct from cancelling this caller.
            if not self._operation.cancelled() or asyncio.current_task().cancelling():
                raise
        return {"ok": self.status()["agent_ready"], "operation": "bootstrap",
                "review_token": token, "status": self.status()}

    async def _run(self, held):
        self._active_binding = held
        try:
            self._same(held)
            held.adapter.fence()
            self._same(held)
            await held.adapter.run(lambda: self._same(held))
            self._same(held)
            if (getattr(self.state, "orchestrator", None) is None or
                    held.adapter.hooks_complete() is not True):
                raise BootstrapRefusal("bootstrap_incomplete")
            held.adapter.commit()
            self._same(held)
            if (self.state._native_bootstrap_required or
                    getattr(self.state, "orchestrator", None) is None or
                    held.adapter.hooks_complete() is not True):
                raise BootstrapRefusal("bootstrap_incomplete")
            self._completed = held
            self._phase = "ready"
        except BaseException:
            self._restart_required = True
            self._phase = "failed"
            self.state._native_bootstrap_required = True
            try:
                await held.adapter.fail_closed()
            except BaseException:
                # Never report agent-ready or retry if cleanup itself fails.
                self._phase = "cleanup_failed"
