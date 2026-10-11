"""No-effect selected-Chrome waiting calls; JSON never grants dispatch authority."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time

from api.job_chrome_grants import (
    JobChromeError, _task_snapshot, first_consent_proposal, job_binding,
    remember_task_principal, task_browser_scope,
)
from security.approval_ingress import PairedDevicePrincipal, canonical_device_binding
from skills.call_context import bind_context

REASON = "selected_chrome_consent_required"
_BASE = {"status", "reason", "pending_call", "pending_call_digest", "model_actions",
         "model_actions_digest", "pending_call_dispatch_started"}


class BrowserConsentWait(asyncio.CancelledError):
    """Internal control signal; caught only by the real TaskFlow step wrapper."""


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_encoded(value).encode()).hexdigest()


def _environment(runtime):
    from api import state as state_module
    brain, orch = state_module.state, runtime._orchestrator
    runner = getattr(orch, "tool_runner", None)
    if (getattr(brain, "orchestrator", None) is not orch
            or getattr(brain, "taskflows", None) is not runtime
            or getattr(orch, "taskflows", None) is not runtime
            or runner is None or runner._orch is not orch or runtime._stop_event.is_set()
            or (runtime._supervisor is not None and runtime._supervisor.paused)
            or getattr(brain, "_native_bootstrap_required", False) is True):
        return None
    return brain, orch, runner, getattr(brain, "_native_agent_turn_generation", 0)


def _guard_environment(runtime, captured):
    current = _environment(runtime)
    if current is None or any(a is not b for a, b in zip(current[:3], captured[:3])) or current[3] != captured[3]:
        raise JobChromeError("job_chrome_runtime_changed")


def structural_checkpoint(result):
    """Validate known no-dispatch data without manufacturing source authority."""
    try:
        if (type(result) is not dict or set(result) not in (_BASE, _BASE | {"resume_grant"})
                or result["status"] != "waiting" or result["reason"] != REASON
                or result["pending_call_dispatch_started"] is not False
                or _digest(result["pending_call"]) != result["pending_call_digest"]
                or _digest(result["model_actions"]) != result["model_actions_digest"]):
            return None
        pending, actions = result["pending_call"], result["model_actions"]
        if (type(pending) is not dict or set(pending) != {"contract_version", "flow_id", "step_id",
                "flow_terms_digest", "session_id", "surface", "call", "browser_resource"}
                or type(pending["contract_version"]) is not int or pending["contract_version"] != 1
                or type(pending["step_id"]) is not int or pending["step_id"] < 1
                or type(pending["call"]) is not dict or set(pending["call"]) != {"id", "name", "args"}
                or type(pending["call"]["id"]) is not str or not 1 <= len(pending["call"]["id"]) <= 256
                or type(pending["call"]["name"]) is not str or not pending["call"]["name"].startswith("browser__")
                or type(pending["call"]["args"]) is not dict
                or type(pending["browser_resource"]) is not dict
                or set(pending["browser_resource"]) != {"owner_session_id", "connection_id", "target_id"}
                or type(actions) is not dict or len(actions) > 32):
            return None
        for call_id, record in actions.items():
            if (type(record) is not dict or set(record) != {"terms", "terms_digest", "result"}
                    or record["terms"].get("call_id") != call_id
                    or record["terms"].get("flow_id") != pending["flow_id"]
                    or record["terms"].get("step_id") != pending["step_id"]
                    or record["terms"].get("session_id") != pending["session_id"]
                    or _digest(record["terms"]) != record["terms_digest"]
                    or type(record["result"]) is not dict or record["result"].get("success") is not True
                    or record["result"].get("status") == "outcome_unknown"):
                return None
        if len(_encoded(result).encode()) > 131072:
            return None
        return pending
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def validated_checkpoint(runtime, flow, step, *, principal=None):
    result = step.get("result") or {}
    pending = structural_checkpoint(result)
    if pending is None:
        raise JobChromeError("job_chrome_checkpoint_unavailable")
    snapshot = _task_snapshot(runtime, flow["id"], step["id"], source_principal=principal)
    if (pending["flow_id"] != flow["id"] or pending["step_id"] != step["id"]
            or pending["flow_terms_digest"] != snapshot["flow_terms_digest"]
            or pending["session_id"] != snapshot["session_id"]
            or pending["surface"] != runtime.execution_surface_for_flow(flow["id"])):
        raise JobChromeError("job_chrome_task_changed")
    runner = runtime._orchestrator.tool_runner
    call = pending["call"]
    skill_id, endpoint_id = call["name"].split("__", 1)
    validation = runner._get_dispatch_validator().validate(skill_id, endpoint_id, call["args"])
    if not validation.ok or (validation.fixed_args is not None and validation.fixed_args != call["args"]):
        raise JobChromeError("job_chrome_call_changed")
    return pending, snapshot


def pause_if_missing(runtime, flow, step, runner, call, surface, *, actions=None, model_scope=None):
    if model_scope is not None and getattr(model_scope, "browser_consent_wait", False):
        raise BrowserConsentWait()
    prior = step.get("result") or {}
    if prior and (model_scope is None or prior.get("reason") != "model_continuation"):
        return  # A failed/revoked/unknown earlier dispatch is never first use.
    proposal = first_consent_proposal(runner, call.get("name", ""),
                                      flow.get("session_id") or f"taskflow-{flow['id']}")
    if proposal is None:
        return
    if model_scope is not None and (model_scope.error or model_scope.unknown or model_scope.calls):
        return
    actions = copy.deepcopy(actions or {})
    if any(record.get("terms", {}).get("browser_job_grant") for record in actions.values()):
        return  # A prior selected effect is not a first-use missing-consent call.
    if (type(call.get("id")) is not str or not 1 <= len(call["id"]) <= 256
            or type(call.get("args", {})) is not dict):
        return
    skill_id, endpoint_id = call["name"].split("__", 1)
    validation = runner._get_dispatch_validator().validate(skill_id, endpoint_id, call.get("args", {}))
    from security.safety_resolver import LEVEL_DENY
    if (not validation.ok or runner.enforce_plan_mode(call["name"], proposal["task"]["session_id"])
            or runner.policy_for(call["name"], call.get("args", {}), surface=surface).level == LEVEL_DENY):
        return
    if surface != runtime.execution_surface_for_flow(flow["id"]):
        raise JobChromeError("job_chrome_call_changed")
    pending = {"contract_version": 1, "flow_id": flow["id"], "step_id": step["id"],
               "flow_terms_digest": proposal["task"]["flow_terms_digest"],
               "session_id": proposal["task"]["session_id"], "surface": surface,
               "call": {"id": call["id"], "name": call["name"],
                        "args": copy.deepcopy(validation.fixed_args if validation.fixed_args is not None else call.get("args", {}))},
               "browser_resource": proposal["resource"]}
    checkpoint = {"status": "waiting", "reason": REASON, "pending_call": pending,
                  "pending_call_digest": _digest(pending), "model_actions": actions,
                  "model_actions_digest": _digest(actions), "pending_call_dispatch_started": False}
    if structural_checkpoint(checkpoint) is None:
        raise JobChromeError("job_chrome_checkpoint_unavailable")
    environment = _environment(runtime)
    if environment is None or environment[2] is not runner:
        raise JobChromeError("job_chrome_runtime_changed")
    with runtime._lock:
        try:
            _guard_environment(runtime, environment)
            snapshot = _task_snapshot(runtime, flow["id"], step["id"])
            if snapshot["flow_terms_digest"] != pending["flow_terms_digest"]:
                raise JobChromeError("job_chrome_task_changed")
            changed = runtime._conn.execute(
                "UPDATE taskflow_steps SET status='waiting',result_json=?,finished_at=NULL WHERE id=? AND status='running' "
                "AND EXISTS(SELECT 1 FROM taskflows WHERE id=? AND status='running' AND current_step=?)",
                (json.dumps(checkpoint, allow_nan=False), step["id"], flow["id"], step["step_index"])).rowcount
            if changed != 1:
                raise JobChromeError("job_chrome_task_changed")
            runtime._conn.execute("UPDATE taskflows SET status='waiting',wait_until=NULL,updated_at=? WHERE id=?",
                                  (time.time(), flow["id"]))
            snapshot["principal"].require_current()
            _guard_environment(runtime, environment)
            runtime._conn.commit()
        except BaseException:
            runtime._conn.rollback()
            raise
    if model_scope is not None:
        model_scope.browser_consent_wait = True
    raise BrowserConsentWait()


def _live_grant(runtime, flow, step, pending):
    environment = _environment(runtime)
    if environment is None:
        raise JobChromeError("job_chrome_runtime_changed")
    brain, orch, runner, _ = environment
    runner._guard_agent_lease()
    resource = brain.browser.approval_binding()
    if (resource is None or any(resource[key] != pending["browser_resource"][key]
                                for key in ("owner_session_id", "target_id"))):
        raise JobChromeError("job_chrome_resource_changed")
    with task_browser_scope(runtime, flow, step):
        grant = job_binding(orch, pending["call"]["name"], pending["session_id"], resource)
    if grant is None or grant["flow_terms_digest"] != pending["flow_terms_digest"]:
        raise JobChromeError("job_chrome_grant_required")
    _guard_environment(runtime, environment)
    return grant, environment


def resume_waiting_call(runtime, flow):
    has_saved = any((s.get("result") or {}).get("reason") == REASON for s in flow["steps"])
    if not 0 <= flow["current_step"] < len(flow["steps"]):
        return {**flow, "resource_resume": {"status": "refused", "error_code": "job_chrome_task_changed"}} if has_saved else None
    step = flow["steps"][flow["current_step"]]
    if (step.get("result") or {}).get("reason") != REASON:
        return {**flow, "resource_resume": {"status": "refused", "error_code": "job_chrome_task_changed"}} if has_saved else None
    refused = {**flow, "resource_resume": {"status": "refused", "error_code": "job_chrome_consent_unavailable"}}
    if flow["status"] != "waiting" or step["status"] != "waiting":
        return refused
    try:
        pending, snapshot = validated_checkpoint(runtime, flow, step)
        grant, environment = _live_grant(runtime, flow, step, pending)
        queued = {**step["result"], "resume_grant": grant}
        with runtime._lock:
            try:
                snapshot["principal"].require_current()
                _guard_environment(runtime, environment)
                checked, _ = validated_checkpoint(runtime, runtime.get_flow(flow["id"]), step)
                if checked != pending or _live_grant(runtime, flow, step, pending)[0] != grant:
                    raise JobChromeError()
                raw = runtime._conn.execute("SELECT result_json FROM taskflow_steps WHERE id=?", (step["id"],)).fetchone()[0]
                if json.loads(raw) != step["result"]:
                    raise JobChromeError()
                changed = runtime._conn.execute("UPDATE taskflow_steps SET status='pending',result_json=? WHERE id=? AND status='waiting' AND result_json=? "
                    "AND EXISTS(SELECT 1 FROM taskflows WHERE id=? AND status='waiting' AND current_step=?)",
                    (json.dumps(queued, allow_nan=False), step["id"], raw, flow["id"], step["step_index"])).rowcount
                if changed != 1:
                    raise JobChromeError()
                runtime._conn.execute("UPDATE taskflows SET status='queued',wait_until=NULL,updated_at=? WHERE id=?",
                                      (time.time(), flow["id"]))
                snapshot["principal"].require_current()
                _guard_environment(runtime, environment)
                runtime._conn.commit()
            except BaseException:
                runtime._conn.rollback()
                raise
        runtime._wake_event.set()
        return {**runtime.get_flow(flow["id"]), "resource_resume": {"status": "queued"}}
    except (Exception, asyncio.CancelledError):
        return refused


def _restore_wait(runtime, flow, step):
    if _environment(runtime) is None or structural_checkpoint(step.get("result")) is None:
        return
    checkpoint = {key: copy.deepcopy(step["result"][key]) for key in _BASE}
    with runtime._lock:
        changed = runtime._conn.execute("UPDATE taskflow_steps SET status='waiting',result_json=? WHERE id=? AND status='running' AND result_json=? "
            "AND EXISTS(SELECT 1 FROM taskflows WHERE id=? AND status='running' AND current_step=?)",
            (json.dumps(checkpoint, allow_nan=False), step["id"], json.dumps(step["result"], allow_nan=False), flow["id"], step["step_index"])).rowcount
        if changed:
            runtime._conn.execute("UPDATE taskflows SET status='waiting',wait_until=NULL,updated_at=? WHERE id=?", (time.time(), flow["id"]))
            runtime._conn.commit()


def _persist_skill_review(runtime, flow, step, pending, grant, environment, refusal):
    runner = environment[2]
    refusal["taskflow"] = {"flow_id": flow["id"], "step_id": step["id"]}
    review = {"status": "waiting", "reason": "approval_required", "dispatch_surface": pending["surface"],
              "approval": {key: copy.deepcopy(refusal[key]) for key in ("request_id", "session_id", "tool_name", "args")}}
    try:
        with runtime._lock:
            try:
                _guard_environment(runtime, environment)
                if _live_grant(runtime, flow, step, pending)[0] != grant:
                    raise JobChromeError()
                latest = runtime.get_flow(flow["id"])
                active = latest["steps"][latest["current_step"]] if latest and 0 <= latest["current_step"] < len(latest["steps"]) else None
                if (latest is None or latest["status"] != "running" or active is None
                        or active["id"] != step["id"] or active["status"] != "running"
                        or active["result"].get("pending_call") != pending):
                    raise JobChromeError()
                changed = runtime._conn.execute("UPDATE taskflow_steps SET status='waiting',result_json=? WHERE id=? AND status='running'",
                    (json.dumps(review, allow_nan=False), step["id"])).rowcount
                if changed != 1:
                    raise JobChromeError()
                runtime._conn.execute("UPDATE taskflows SET status='waiting',wait_until=NULL,updated_at=? WHERE id=? AND status='running'",
                                      (time.time(), flow["id"]))
                runtime._bind_phone_review_checkpoint(runner, refusal, commit=False)
                if _live_grant(runtime, flow, step, pending)[0] != grant:
                    raise JobChromeError()
                _guard_environment(runtime, environment)
                runtime._conn.commit()
            except BaseException:
                runtime._conn.rollback()
                raise
    except BaseException:
        runner.deny_pending(refusal["request_id"], session_id=pending["session_id"])
        raise


async def dispatch_saved_call(runtime, flow, step):
    """Run only the explicitly resumed call under the existing effect lane."""
    prior = step.get("result") or {}
    if prior.get("reason") != REASON:
        return None
    try:
        pending, _ = validated_checkpoint(runtime, flow, step)
        grant, environment = _live_grant(runtime, flow, step, pending)
        if prior.get("resume_grant") != grant:
            raise JobChromeError("job_chrome_grant_changed")
    except (Exception, asyncio.CancelledError):
        _restore_wait(runtime, flow, step)
        return {"status": "deferred"}
    from agents.taskflow import TaskFlowModelStep, _model_step
    runner, call = environment[2], pending["call"]
    skill_id, endpoint_id = call["name"].split("__", 1)
    skill = runtime._skill_registry.skills[skill_id]
    endpoint = next(e for e in skill.endpoints if e.id == endpoint_id)
    # Use manifest semantics and the real adapter's finite read endpoints. Never
    # let a changed autonomy posture substitute for an exact resumed action review.
    read = endpoint.read_only_hint is True and endpoint_id in {"get_page_info", "snapshot", "screenshot", "wait"}
    scope = TaskFlowModelStep(runtime, flow, step, pending["session_id"]) if step["step_type"] == "llm.chat" else None
    token = _model_step.set(scope)
    try:
        _guard_environment(runtime, environment)
        with bind_context(session_id=pending["session_id"], surface=pending["surface"],
                          tool_name=call["name"], call_id=call["id"]):
            if not read:
                if scope is not None:
                    scope.admit_call(runner, call, pending["surface"])
                refusal = runner.enforce_plan_mode(call["name"], pending["session_id"])
                if refusal is None:
                    refusal = runner.enforce_safety(call["name"], call["args"], session_id=pending["session_id"],
                                                    surface=pending["surface"], _require_review=True)
                if not refusal or refusal.get("status") != "pending_approval":
                    return {"status": "failed", "error": "Saved action review is unavailable", "dispatch_started": False}
                if scope is None:
                    _persist_skill_review(runtime, flow, step, pending, grant, environment, refusal)
                await runner._notify_user_of_pending_approval(pending["session_id"], call["name"], refusal)
                return {"status": "deferred"}
            if scope is None:
                with runtime._lock:
                    _guard_environment(runtime, environment)
                    if _live_grant(runtime, flow, step, pending)[0] != grant:
                        raise JobChromeError()
                    raw = runtime._conn.execute("SELECT result_json FROM taskflow_steps WHERE id=?", (step["id"],)).fetchone()[0]
                    if json.loads(raw) != prior:
                        raise JobChromeError()
                    started = {**prior, "pending_call_dispatch_started": True}
                    changed = runtime._conn.execute("UPDATE taskflow_steps SET result_json=? WHERE id=? AND status='running' AND result_json=?",
                        (json.dumps(started, allow_nan=False), step["id"], raw)).rowcount
                    if changed != 1:
                        raise JobChromeError()
                    runtime._conn.commit()
            result = await runner.execute_tool_call_for_llm(pending["session_id"], copy.deepcopy(call), [], surface=pending["surface"])
        if scope is not None and scope.review_issued:
            return {"status": "deferred"}
        if scope is None and result.get("status") == "pending_approval":
            _persist_skill_review(runtime, flow, step, pending, grant, environment, result)
            await runner._notify_user_of_pending_approval(pending["session_id"], call["name"], result)
            return {"status": "deferred"}
        _guard_environment(runtime, environment)
        _task_snapshot(runtime, flow["id"], step["id"])["principal"].require_current()
        _guard_environment(runtime, environment)
        if result.get("success") is not True:
            return {"status": "failed", "result": result, "dispatch_started": True}
        if scope is None:
            return {"status": "completed", "result": result, "dispatch_started": True}
        if scope.calls or scope.error or scope.unknown:
            return {"status": "outcome_unknown"}
        continuation = {"status": "pending", "reason": "model_continuation", "model_actions": scope.actions}
        _guard_environment(runtime, environment)
        with runtime._lock:
            try:
                source = _task_snapshot(runtime, flow["id"], step["id"])["principal"]
                source.require_current()
                _guard_environment(runtime, environment)
                changed = runtime._conn.execute("UPDATE taskflow_steps SET status='pending',result_json=? WHERE id=? AND status='running' "
                    "AND EXISTS(SELECT 1 FROM taskflows WHERE id=? AND status='running' AND current_step=?)",
                    (json.dumps(continuation, allow_nan=False), step["id"], flow["id"], step["step_index"])).rowcount
                if changed != 1:
                    raise JobChromeError()
                runtime._conn.execute("UPDATE taskflows SET status='queued',wait_until=NULL,updated_at=? WHERE id=? AND status='running'",
                                      (time.time(), flow["id"]))
                source.require_current()
                _guard_environment(runtime, environment)
                runtime._conn.commit()
            except BaseException:
                runtime._conn.rollback()
                raise
        runtime._wake_event.set()
        return {"status": "deferred"}
    finally:
        _model_step.reset(token)


def refresh_waiting_task_sources(runtime, principal, *, limit=128):
    """Observational authenticated handshake refresh; safe for asyncio.to_thread."""
    if type(principal) is not PairedDevicePrincipal or type(limit) is not int or not 1 <= limit <= 128:
        raise ValueError("Current paired task source is required")
    principal.require_current()
    from agents.taskflow import TaskFlowRuntime
    if type(runtime) is not TaskFlowRuntime:
        return 0
    environment = _environment(runtime)
    if environment is None:
        return 0
    binding = canonical_device_binding(principal.storage_binding())
    with runtime._lock:
        rows = runtime._conn.execute("SELECT id FROM taskflows WHERE origin_principal_json=? AND status='waiting' ORDER BY updated_at DESC LIMIT ?",
                                     (binding, limit)).fetchall()
    refreshed = 0
    for row in rows:
        principal.require_current()
        _guard_environment(runtime, environment)
        flow = runtime.get_flow(row[0])
        if flow is None or not 0 <= flow["current_step"] < len(flow["steps"]):
            continue
        step = flow["steps"][flow["current_step"]]
        try:
            if (step.get("result") or {}).get("reason") == REASON:
                validated_checkpoint(runtime, flow, step, principal=principal)
            elif runtime._device_review_checkpoint(flow, origin_session_id=runtime.origin_session_for_flow(flow["id"]),
                                                   source_principal=principal) is None:
                continue
            with runtime._lock:
                principal.require_current()
                _guard_environment(runtime, environment)
                latest = runtime.get_flow(flow["id"])
                if (latest is None or latest["status"] != "waiting"
                        or latest["current_step"] != flow["current_step"]
                        or latest["steps"][latest["current_step"]]["result"] != step["result"]):
                    continue
                remember_task_principal(runtime, flow["id"], principal)
            refreshed += 1
        except JobChromeError:
            continue
    principal.require_current()
    _guard_environment(runtime, environment)
    return refreshed
