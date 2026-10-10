"""Explicit local, volatile delegation of one selected tab to one task step.

This does not change Chrome ownership or approve actions. Public IDs never issue
dispatch authority. Only the existing TaskFlow runner and its exact backing task
receive the opaque scope; a copied ContextVar is insufficient.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import secrets
import time
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from uuid import uuid4

from security.approval_ingress import PairedDevicePrincipal, canonical_device_binding
from skills.call_context import context_enabled, current_context
from skills.impl.existing_chrome import ExistingChromeController


class JobChromeError(RuntimeError):
    def __init__(self, code="job_chrome_grant_unavailable", status=409):
        self.code, self.status = code, status
        super().__init__(code)


def remember_task_principal(runtime, flow_id, principal):
    if principal is None:
        return
    with runtime._lock:
        return _remember_task_principal_locked(runtime, flow_id, principal)


def _remember_task_principal_locked(runtime, flow_id, principal):
    """Called only after actual tracked creation/renewal, never from JSON."""
    if principal is None:
        return
    if type(principal) is not PairedDevicePrincipal:
        raise JobChromeError()
    principal.require_current()
    if not runtime.flow_matches_source(flow_id, principal):
        raise JobChromeError()
    registry = vars(runtime).setdefault("_job_chrome_principals", {})
    existing = registry.get(flow_id)
    if (type(existing) is PairedDevicePrincipal
            and canonical_device_binding(existing.storage_binding()) == canonical_device_binding(principal.storage_binding())):
        try:
            existing.require_current()
        except asyncio.CancelledError:
            pass  # A revoked source is replaced; its old grants stay invalid.
        else:
            return  # Fresh HTTP admission never revokes a still-current source.
    if flow_id not in registry and len(registry) >= 256:
        for old in list(registry):
            flow = runtime.get_flow(old)
            if flow is None or flow["status"] in {"completed", "failed", "cancelled"}:
                registry.pop(old, None)
        if len(registry) >= 256:
            return  # Task creation remains valid; new browser delegation fails closed.
    registry[flow_id] = principal


def _task_snapshot(runtime, flow_id, step_id, *, source_principal=None):
    """Read immutable scope from the existing store, not model-supplied context."""
    with runtime._lock:
        row = runtime._conn.execute("SELECT * FROM taskflows WHERE id=?", (flow_id,)).fetchone()
        step = runtime._conn.execute("SELECT * FROM taskflow_steps WHERE flow_id=? AND id=?", (flow_id, step_id)).fetchone()
        if (row is None or step is None or runtime._stop_event.is_set()
                or row["status"] not in {"queued", "running", "waiting"}
                or step["status"] not in {"pending", "running", "waiting"}
                or step["step_type"] not in {"skill.invoke", "llm.chat"}
                or row["current_step"] != step["step_index"]
                or not row["origin_principal_json"] or not row["terms_digest"]):
            raise JobChromeError("job_chrome_task_changed")
        principal = source_principal if source_principal is not None else vars(runtime).get("_job_chrome_principals", {}).get(flow_id)
        if type(principal) is not PairedDevicePrincipal:
            raise JobChromeError("job_chrome_source_unavailable", 403)
        private = canonical_device_binding(json.loads(row["origin_principal_json"]))
        if private != canonical_device_binding(principal.storage_binding()):
            raise JobChromeError("job_chrome_source_changed", 403)
        terms = {"flow_id": flow_id, "step_id": step_id, "session_id": row["session_id"],
                 "title": row["title"], "context": json.loads(row["context_json"]),
                 "origin_session_id": row["origin_session_id"], "origin_surface": row["origin_surface"],
                 "terms_digest": row["terms_digest"], "source": private,
                 "step_type": step["step_type"], "step_payload": json.loads(step["payload_json"])}
        digest = hashlib.sha256(json.dumps(terms, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        snapshot = {"flow_id": flow_id, "step_id": step_id, "session_id": row["session_id"] or f"taskflow-{flow_id}",
                    "flow_terms_digest": digest, "title": row["title"], "principal": principal,
                    "flow_status": row["status"], "step_status": step["status"], "step_type": step["step_type"]}
    try:
        principal.require_current()
    except asyncio.CancelledError:
        raise JobChromeError("job_chrome_source_changed", 403) from None
    return snapshot


@dataclass(eq=False)
class _TaskScope:
    runtime: object
    flow_id: str
    step_id: object
    owner: object
    active: bool = True
    backing: set = field(default_factory=set)

    def guard(self):
        if not self.active or asyncio.current_task() not in {self.owner, *self.backing}:
            raise JobChromeError("job_chrome_task_scope_unavailable", 403)


_scope = ContextVar("feral_job_chrome_task_scope", default=None)


@contextmanager
def task_browser_scope(runtime, flow, step):
    """Only the trusted existing TaskFlow invocation creates this scope."""
    scope = _TaskScope(runtime, flow["id"], step["id"], asyncio.current_task())
    token = _scope.set(scope)
    try:
        yield
    finally:
        scope.active = False
        _scope.reset(token)


@dataclass
class _BackingTransfer:
    scope: _TaskScope
    owner: object
    used: bool = False


def capture_backing_transfer():
    scope = _scope.get()
    if scope is None:
        return None
    scope.guard()
    if asyncio.current_task() is not scope.owner:
        raise JobChromeError("job_chrome_task_scope_unavailable", 403)
    return _BackingTransfer(scope, scope.owner)


@contextmanager
def bind_backing_transfer(transfer):
    if transfer is None:
        yield
        return
    task = asyncio.current_task()
    if (type(transfer) is not _BackingTransfer or transfer.used or not transfer.scope.active
            or task is transfer.owner or _scope.get() is not transfer.scope):
        raise JobChromeError("job_chrome_task_scope_unavailable", 403)
    transfer.used = True
    transfer.scope.backing.add(task)
    try:
        yield
    finally:
        transfer.scope.backing.discard(task)


def manager_for(orch):
    value = vars(orch).get("_job_chrome_grants")
    return value if type(value) is JobChromeGrants else None


def job_binding(orch, tool_name, session_id, resource):
    if resource is None:
        return None  # Existing non-selected-browser paths retain their contract.
    scope = _scope.get()
    if scope is None:
        return None
    scope.guard()
    manager = manager_for(orch)
    if manager is None or scope.runtime is not getattr(orch, "taskflows", None):
        raise JobChromeError("job_chrome_grant_required", 403)
    return manager.admit(scope, tool_name, session_id, resource)


def first_consent_proposal(runner, tool, session):
    """Observe a missing first grant in the actual task; never admit dispatch."""
    scope = _scope.get()
    if scope is None or not tool.startswith("browser__"):
        return None
    scope.guard()
    from api import state as state_module
    brain, orch = state_module.state, runner._orch
    browser = getattr(brain, "browser", None)
    if (getattr(brain, "orchestrator", None) is not orch or orch.tool_runner is not runner
            or orch.taskflows is not scope.runtime or getattr(brain, "taskflows", None) is not scope.runtime
            or not isinstance(browser, ExistingChromeController)):
        return None
    resource = browser.approval_binding()
    supported = {"browser__" + name for name in browser._supported | {"screenshot"}}
    if resource is None or tool not in supported:
        return None
    supplier = getattr(orch, "_browser_resource_supplier", None)
    if not callable(supplier) or supplier() != resource:
        raise JobChromeError("job_chrome_resource_changed")
    snapshot = _task_snapshot(scope.runtime, scope.flow_id, scope.step_id)
    if snapshot["session_id"] != session:
        raise JobChromeError("job_chrome_task_changed")
    manager = manager_for(orch)
    if manager is None:
        from api.routes.existing_chrome import job_chrome_grants
        manager = job_chrome_grants
    if type(manager) is not JobChromeGrants or manager._brain() is not brain:
        raise JobChromeError("job_chrome_runtime_changed")
    if any(g["runtime"] is scope.runtime and g["task"]["flow_id"] == scope.flow_id
           and g["task"]["step_id"] == scope.step_id for g in manager._grants.values()):
        return None  # Revoked/expired/changed grants are not a new first-use wait.
    runner._guard_agent_lease()
    if (state_module.state is not brain or brain.orchestrator is not orch
            or orch.tool_runner is not runner or orch.taskflows is not scope.runtime
            or brain.taskflows is not scope.runtime or brain.browser is not browser
            or browser.approval_binding() != resource):
        raise JobChromeError("job_chrome_runtime_changed")
    return {"task": snapshot, "resource": copy.deepcopy(resource), "brain": brain, "runner": runner}


def require_selected_job(resource):
    scope = _scope.get()
    if scope is None:
        raise JobChromeError("job_chrome_grant_required", 403)
    orch = scope.runtime._orchestrator
    context = current_context()
    if job_binding(orch, context.tool_name, context.session_id, resource) is None:
        raise JobChromeError("job_chrome_grant_required", 403)
    snapshot = _task_snapshot(scope.runtime, scope.flow_id, scope.step_id)
    if (snapshot["flow_status"], snapshot["step_status"]) != ("running", "running"):
        raise JobChromeError("job_chrome_task_changed")
    if (scope.runtime._effect_owner != (scope.flow_id, scope.step_id)
            or scope.runtime._effect_depth <= 0):
        raise JobChromeError("job_chrome_effect_lane_unavailable", 403)


def renewal_binding(orch, tool, session, resource, previous):
    """Explicit renewal may narrow a new local grant, never widen an old action."""
    current = job_binding(orch, tool, session, resource)
    if current == previous:
        return current
    immutable = ("contract_version", "flow_id", "step_id",
                 "flow_terms_digest", "execution_session_id", "grant_revision", "action_permission")
    if (type(previous) is not dict or type(current) is not dict
            or any(current.get(key) != previous.get(key) for key in immutable)
            or type(previous.get("browser_resource")) is not dict
            or set(previous["browser_resource"]) != {"owner_session_id", "connection_id", "target_id"}
            or any(previous["browser_resource"][key] != current["browser_resource"][key]
                   for key in ("owner_session_id", "target_id"))
            or previous.get("status") != "active"
            or type(previous.get("allowed_tools")) is not list
            or not 1 <= len(previous["allowed_tools"]) <= 16
            or not all(type(name) is str for name in previous["allowed_tools"])
            or len(set(previous["allowed_tools"])) != len(previous["allowed_tools"])
            or not set(current["allowed_tools"]).issubset(previous["allowed_tools"])):
        raise JobChromeError("job_chrome_renewal_scope_changed", 403)
    return current


def has_task_scope():
    return _scope.get() is not None


def approval_browser_scope(runner, approval):
    from agents.tool_runner import _ExactApproval
    if not isinstance(approval, _ExactApproval) or approval.browser_job_grant is None:
        return nullcontext()
    manager = manager_for(runner._orch)
    if manager is None or approval.pending is None:
        raise JobChromeError()
    grant = manager.pending(approval.pending)
    runtime = grant["runtime"]
    if not runtime.approved_dispatch_allowed(approval.pending):
        raise JobChromeError("job_chrome_task_changed")
    flow = runtime.get_flow(grant["task"]["flow_id"])
    step = next(s for s in flow["steps"] if s["id"] == grant["task"]["step_id"])
    return task_browser_scope(runtime, flow, step)


class JobChromeGrants:
    """Same-process resource grants with bounded, one-use local reviews."""
    def __init__(self, brain, clock=time.monotonic):
        self._brain, self._clock = brain, clock
        self._reviews, self._grants = {}, {}

    @staticmethod
    def _owner(session):
        from memory.runtime_session_checkpoint import validate_session_id
        validate_session_id(session)
        if not context_enabled() or current_context().session_id != session:
            raise JobChromeError("job_chrome_local_owner_required", 403)

    def _capture(self, owner, connection, target, flow, step, tools, ttl):
        self._owner(owner)
        brain = self._brain()
        orch = getattr(brain, "orchestrator", None)
        runtime = getattr(orch, "taskflows", None)
        browser = getattr(brain, "browser", None)
        resource = browser.approval_binding() if isinstance(browser, ExistingChromeController) else None
        if resource != {"owner_session_id": owner, "connection_id": connection, "target_id": target}:
            raise JobChromeError("job_chrome_resource_changed")
        if (runtime is None or runtime._orchestrator is not orch
                or type(ttl) is not int or not 1 <= ttl <= 300
                or not isinstance(tools, list) or not 1 <= len(tools) <= 16
                or len(set(tools)) != len(tools)
                or not all(name in {"browser__" + name for name in browser._supported | {"screenshot"}} for name in tools)):
            raise JobChromeError("job_chrome_scope_invalid", 422)
        snapshot = _task_snapshot(runtime, flow, step)
        if (self._brain() is not brain or brain.orchestrator is not orch
                or orch.taskflows is not runtime or brain.browser is not browser):
            raise JobChromeError("job_chrome_runtime_changed")
        return {"brain": brain, "orch": orch, "runner": orch.tool_runner, "runtime": runtime,
                "browser": browser, "resource": resource, "task": snapshot,
                "agent_generation": getattr(brain, "_native_agent_turn_generation", 0),
                "allowed_tools": sorted(tools), "ttl": ttl}

    def _check(self, grant):
        brain, orch, runtime = grant["brain"], grant["orch"], grant["runtime"]
        if grant["grant_id"] in self._grants:
            newest = next((g for g in reversed(list(self._grants.values()))
                           if g["runtime"] is runtime and g["task"]["flow_id"] == grant["task"]["flow_id"]
                           and g["task"]["step_id"] == grant["task"]["step_id"]), None)
            if newest is not grant:
                raise JobChromeError("job_chrome_grant_superseded")
        if (grant.get("revoked") or grant.get("superseded") or self._clock() >= grant["deadline"]
                or self._brain() is not brain or brain.orchestrator is not orch
                or orch.tool_runner is not grant["runner"] or orch.taskflows is not runtime
                or brain.browser is not grant["browser"]
                or getattr(brain, "_native_agent_turn_generation", 0) != grant["agent_generation"]
                or getattr(brain, "_native_bootstrap_required", False) is True
                or (runtime._supervisor is not None and runtime._supervisor.paused)
                or grant["browser"].approval_binding() != grant["resource"]):
            raise JobChromeError("job_chrome_grant_expired_or_changed")
        from security.agent_turn_lease import guard_agent_dispatch
        try:
            guard_agent_dispatch(getattr(grant["runner"], "_native_agent_dispatch_lease", None))
        except asyncio.CancelledError:
            raise JobChromeError("job_chrome_runtime_changed") from None
        snapshot = _task_snapshot(runtime, grant["task"]["flow_id"], grant["task"]["step_id"])
        if snapshot["flow_terms_digest"] != grant["task"]["flow_terms_digest"] or snapshot["principal"] is not grant["task"]["principal"]:
            raise JobChromeError("job_chrome_task_changed")
        if (self._brain() is not brain or brain.orchestrator is not orch
                or orch.tool_runner is not grant["runner"] or orch.taskflows is not runtime
                or brain.browser is not grant["browser"]
                or getattr(brain, "_native_agent_turn_generation", 0) != grant["agent_generation"]
                or getattr(brain, "_native_bootstrap_required", False) is True
                or (runtime._supervisor is not None and runtime._supervisor.paused)
                or grant["browser"].approval_binding() != grant["resource"]):
            raise JobChromeError("job_chrome_runtime_changed")
        return snapshot

    def _receipt(self, grant, status="active"):
        return {"success": True, "contract_version": 1, "grant_id": grant["grant_id"], "status": status,
                "browser_resource": copy.deepcopy(grant["resource"]),
                **{key: grant["task"][key] for key in ("flow_id", "step_id", "flow_terms_digest")},
                "grant_revision": 1, "allowed_tools": list(grant["allowed_tools"]),
                "execution_session_id": grant["task"]["session_id"],
                "expires_at": grant["expires_at"], "action_permission": "separate_review_required"}

    def review(self, **body):
        captured = self._capture(body["session_id"], body["connection_id"], body["target_id"],
                                 body["flow_id"], body["step_id"], body["allowed_tools"], body["expires_in_seconds"])
        if body["flow_terms_digest"] != captured["task"]["flow_terms_digest"]:
            raise JobChromeError("job_chrome_task_changed")
        self._reviews = {k: v for k, v in self._reviews.items() if v["deadline"] > self._clock()}
        if len(self._grants) >= 128:
            self._grants = {k: v for k, v in self._grants.items() if not v.get("revoked") and v["deadline"] > self._clock()}
        if len(self._reviews) >= 64 or len(self._grants) >= 128:
            raise JobChromeError("job_chrome_grant_capacity", 429)
        token, review_id = secrets.token_urlsafe(32), str(uuid4())
        captured.update(grant_id=str(uuid4()), deadline=self._clock() + captured["ttl"],
                        expires_at=time.time() + captured["ttl"], review_token=token)
        self._reviews[review_id] = captured
        return {"success": True, "contract_version": 1, "review_id": review_id, "review_token": token,
                "scope": self._receipt(captured, "review"), "task_title": captured["task"]["title"]}

    def tasks(self, owner, connection):
        self._owner(owner)
        brain = self._brain()
        browser = getattr(brain, "browser", None)
        resource = browser.approval_binding() if isinstance(browser, ExistingChromeController) else None
        if resource is None or resource["owner_session_id"] != owner or resource["connection_id"] != connection:
            raise JobChromeError("job_chrome_resource_changed")
        runtime = getattr(getattr(brain, "orchestrator", None), "taskflows", None)
        if runtime is None:
            raise JobChromeError("job_chrome_source_unavailable", 403)
        rows = []
        for flow_id in list(vars(runtime).get("_job_chrome_principals", {})):
            flow = runtime.get_flow(flow_id)
            if not flow or not 0 <= flow["current_step"] < len(flow["steps"]):
                continue
            try:
                snapshot = _task_snapshot(runtime, flow_id, flow["steps"][flow["current_step"]]["id"])
            except JobChromeError:
                continue
            rows.append({**{key: snapshot[key] for key in ("flow_id", "step_id", "flow_terms_digest", "title", "flow_status", "step_status", "step_type")},
                         "execution_session_id": snapshot["session_id"]})
            if len(rows) >= 128:
                break
        return {"success": True, "contract_version": 1, "browser_resource": resource, "tasks": rows,
                "allowed_tools": sorted("browser__" + name for name in browser._supported | {"screenshot"})}

    def confirm(self, review_id, token):
        grant = self._reviews.get(review_id)
        if grant is None or type(token) is not str or not secrets.compare_digest(grant["review_token"], token):
            raise JobChromeError("job_chrome_review_unavailable")
        self._owner(grant["resource"]["owner_session_id"])
        self._check(grant)
        self._grants = {k: v for k, v in self._grants.items() if not v.get("revoked") and v["deadline"] > self._clock()}
        if len(self._grants) >= 128:
            raise JobChromeError("job_chrome_grant_capacity", 429)
        # A changed local consent scope permanently retires prior grants and
        # prepared reviews. Pruning a later revoked object cannot revive them.
        for previous in [*self._grants.values(), *self._reviews.values()]:
            if (previous is not grant and previous["runtime"] is grant["runtime"]
                    and previous["task"]["flow_id"] == grant["task"]["flow_id"]
                    and previous["task"]["step_id"] == grant["task"]["step_id"]):
                previous["superseded"] = True
        self._reviews.pop(review_id)
        self._grants[grant["grant_id"]] = grant
        grant["orch"]._job_chrome_grants = self
        return self._receipt(grant)

    def status(self, grant_id, owner, connection):
        self._owner(owner)
        grant = self._grants.get(grant_id)
        confirmed = grant is not None
        if grant is None:
            grant = next((g for g in self._reviews.values() if g["grant_id"] == grant_id), None)
        if grant is None or grant["resource"]["owner_session_id"] != owner or grant["resource"]["connection_id"] != connection:
            raise JobChromeError()
        try:
            self._check(grant)
            status = "active" if confirmed else "unavailable"
        except JobChromeError:
            status = "revoked" if grant.get("revoked") else "unavailable"
        return self._receipt(grant, status)

    def revoke(self, grant_id, owner, connection):
        self.status(grant_id, owner, connection)
        grant = self._grants.get(grant_id)
        if grant is None:
            grant = next(g for g in self._reviews.values() if g["grant_id"] == grant_id)
        grant["revoked"] = True
        return self._receipt(grant, "revoked")

    def admit(self, scope, tool, session, resource):
        scope.guard()
        candidates = [g for g in self._grants.values() if g["runtime"] is scope.runtime
                      and g["task"]["flow_id"] == scope.flow_id and g["task"]["step_id"] == scope.step_id]
        for grant in reversed(candidates):
            snapshot = self._check(grant)
            if session == snapshot["session_id"] and resource == grant["resource"] and tool in grant["allowed_tools"]:
                return self._receipt(grant)
        raise JobChromeError("job_chrome_grant_required", 403)

    def pending(self, pending):
        metadata = pending.get("browser_job_grant")
        grant = self._grants.get(metadata.get("grant_id")) if isinstance(metadata, dict) else None
        if grant is None:
            raise JobChromeError()
        self._check(grant)
        binding = pending.get("taskflow") or {}
        if (metadata != self._receipt(grant) or pending.get("browser_resource") != grant["resource"]
                or pending.get("tool_name") not in grant["allowed_tools"]
                or pending.get("session_id") != grant["task"]["session_id"]
                or binding.get("flow_id") != grant["task"]["flow_id"] or binding.get("step_id") != grant["task"]["step_id"]):
            raise JobChromeError("job_chrome_task_changed")
        return grant
