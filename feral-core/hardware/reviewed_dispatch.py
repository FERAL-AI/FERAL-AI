"""Fail-closed review/command coordinator, awaiting explicit Mesh integration.

All providers are injected. There are no live-state imports, OS calls, background
workers or automatic retries. The dispatcher MUST synchronously reserve the exact
captured socket/command and call guard immediately before enqueueing. It may then
schedule its owned send task; an async dispatcher is deliberately unsupported.
The existing Mesh.invoke cannot satisfy this contract unchanged. Authorize is
an injected *passive verifier* of separately issued one-use, owner/review-bound
operator authorization, including privileged/dangerous approval where required.
It must tolerate repeated freshness checks without consuming authority; the
coordinator consumes the review once. No client-provided confirmed boolean is
an authorization. Route wiring must supply this trusted verifier and derive
owners/connection generations from authenticated server state.
"""
from dataclasses import dataclass, replace
import inspect
import json
import math
import re
import time
from typing import Any, Callable
from uuid import UUID, uuid4

from hardware.action_frames import build_action_request
from hardware.command_contract import CommandEnvelope, CommandState
from security.capability_grants import action_denied
from security.hardware_policy import capability_refusal, permits_unattended


class HardwareReviewError(ValueError):
    """Validation/refusal. Dispatch exceptions can still mean an unknown effect."""


def _id(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value):
        raise HardwareReviewError("Invalid exact identifier")
    return value


def _uuid(value: Any) -> str:
    if not isinstance(value, str):
        raise HardwareReviewError("Invalid command UUID")
    try:
        if str(UUID(value)) != value:
            raise ValueError()
    except ValueError:
        raise HardwareReviewError("Invalid command UUID") from None
    return value


def _json(value: Any, limit: int = 65536) -> str:
    try:
        data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise HardwareReviewError("Unsupported JSON") from None
    if len(data.encode()) > limit:
        raise HardwareReviewError("JSON exceeds bound")
    return data


@dataclass(frozen=True)
class HardwareBinding:
    """Server-owned connection generation, not a client timestamp assertion."""
    node_id: str
    generation: str
    connection: object
    manifest: dict


@dataclass(frozen=True)
class HardwareReview:
    review_id: str
    owner: str
    node_id: str
    generation: str
    command: str
    params_json: str
    manifest_json: str
    permission_tier: str
    requires_confirmation: bool
    timeout: float
    created_at: float
    expires_at: float


@dataclass(frozen=True)
class _Pending:
    review: HardwareReview
    binding: HardwareBinding


@dataclass(frozen=True)
class _OwnedCommand:
    owner: str
    binding: HardwareBinding
    envelope_json: str
    review: HardwareReview
    enqueue_accepted: bool = False


class _CheckedGrant:
    """One strict prechecked decision; prevents the legacy fail-open fallback."""
    def __init__(self, node: str, command: str, granted: bool):
        self.node, self.command, self.granted = node, command, granted

    def is_granted(self, node: str, command: str) -> bool:
        return self.granted if (node, command) == (self.node, self.command) else False


def _validate_params(cap: dict, params: dict) -> None:
    if type(params) is not dict or len(params) > 64:
        raise HardwareReviewError("Parameters must be a bounded object")
    _json(params, 16384)
    schema = cap.get("parameters")
    if type(schema) is not list or len(schema) > 64:
        raise HardwareReviewError("Missing or unsupported parameter schema")
    names = set()
    for field in schema:
        if type(field) is not dict:
            raise HardwareReviewError("Invalid parameter schema")
        if set(field) - {"name", "type", "required", "description", "default", "enum", "minimum", "maximum"}:
            raise HardwareReviewError("Unsupported parameter constraint")
        name = _id(field.get("name"))
        if name in names:
            raise HardwareReviewError("Duplicate parameter schema")
        names.add(name)
        kind = field.get("type")
        if kind not in {"string", "integer", "int", "number", "float", "boolean", "bool"}:
            raise HardwareReviewError("Unsupported compound parameter schema")
        required = field.get("required", False)
        if type(required) is not bool:
            raise HardwareReviewError("Invalid required flag")
        if "enum" in field and (type(field["enum"]) is not list or not field["enum"] or len(field["enum"]) > 256):
            raise HardwareReviewError("Unsupported parameter enum")
        for key in ("minimum", "maximum"):
            if key in field and (kind not in {"integer", "int", "number", "float"} or type(field[key]) not in {int, float} or abs(field[key]) > 2**53 - 1 or not math.isfinite(field[key])):
                raise HardwareReviewError("Unsupported parameter bounds")
        if "minimum" in field and "maximum" in field and field["minimum"] > field["maximum"]:
            raise HardwareReviewError("Inverted parameter bounds")
        if name not in params:
            if required:
                raise HardwareReviewError("Missing required parameter")
            continue
        value = params[name]
        valid = ((kind == "string" and type(value) is str and len(value) <= 4096)
                 or (kind in {"integer", "int"} and type(value) is int and abs(value) <= 2**53 - 1)
                 or (kind in {"number", "float"} and type(value) in {float, int} and abs(value) <= 2**53 - 1 and math.isfinite(value))
                 or (kind in {"boolean", "bool"} and type(value) is bool))
        if not valid:
            raise HardwareReviewError("Parameter type mismatch")
        if "enum" in field:
            choices = field["enum"]
            if type(choices) is not list or not choices or not any(type(v) is type(value) and v == value for v in choices):
                raise HardwareReviewError("Parameter outside enum")
        for key, lower in (("minimum", True), ("maximum", False)):
            if key in field:
                bound = field[key]
                if type(bound) not in {int, float} or not math.isfinite(bound) or type(value) not in {int, float}:
                    raise HardwareReviewError("Unsupported parameter bounds")
                if (lower and value < bound) or (not lower and value > bound):
                    raise HardwareReviewError("Parameter outside bounds")
    if set(params) - names:
        raise HardwareReviewError("Undeclared parameter")


class ReviewedHardwareCoordinator:
    def __init__(self, *, binding: Callable[[str], HardwareBinding | None],
                 policy: Callable[[], Any], grants: Callable[[], Any], ledger: Any,
                 enqueue: Callable, authorize: Callable[[HardwareReview, Any], bool],
                 clock: Callable[[], float] = time.time):
        self._binding, self._policy, self._grants = binding, policy, grants
        self._ledger, self._enqueue, self._authorize, self._clock = ledger, enqueue, authorize, clock
        self._reviews: dict[str, _Pending] = {}
        self._commands: dict[str, _OwnedCommand] = {}

    def _now(self) -> float:
        value = self._clock()
        if type(value) not in {float, int} or not math.isfinite(value) or not 0 < value <= 2**53 - 1:
            raise HardwareReviewError("Clock unavailable")
        return value

    def _live(self, node_id: str) -> HardwareBinding:
        live = self._binding(node_id)
        if not isinstance(live, HardwareBinding) or live.node_id != node_id or live.connection is None:
            raise HardwareReviewError("Exact node unavailable")
        _uuid(live.generation)
        manifest = live.manifest
        if type(manifest) is not dict or manifest.get("device_id") != node_id or manifest.get("connection_type") != "websocket":
            raise HardwareReviewError("Unsupported exact node manifest")
        _json(manifest)
        return live

    def _capability(self, manifest: dict, command: str, params: dict) -> dict:
        caps = manifest.get("capabilities")
        if type(caps) is not list or len(caps) > 256:
            raise HardwareReviewError("Capabilities unavailable")
        seen = set()
        selected = None
        for cap in caps:
            if type(cap) is not dict:
                raise HardwareReviewError("Invalid capability")
            ident = _id(cap.get("id"))
            if ident in seen:
                raise HardwareReviewError("Ambiguous capability")
            seen.add(ident)
            if ident == command:
                selected = cap
        if selected is None:
            raise HardwareReviewError("Unknown declared capability")
        if selected.get("category") not in {"sensor", "actuator", "display", "audio", "network", "compute"}:
            raise HardwareReviewError("Unknown category")
        if selected.get("permission_tier") not in {"passive", "active", "privileged", "dangerous"}:
            raise HardwareReviewError("Unknown permission tier")
        if type(selected.get("requires_confirmation")) is not bool:
            raise HardwareReviewError("Missing confirmation declaration")
        if selected.get("action_type") not in {None, "read", "write", "execute", "configure", "subscribe", "unsubscribe"}:
            raise HardwareReviewError("Unknown action type")
        _validate_params(selected, params)
        return selected

    def _permission(self, node: str, command: str, cap: dict) -> tuple[bool, _CheckedGrant]:
        policy, store = self._policy(), self._grants()
        if policy is None or store is None:
            raise HardwareReviewError("Operator policy or grants unavailable")
        # Check exceptions/strict booleans before calling the real legacy helper.
        try:
            granted = store.is_granted(node, command)
            if type(granted) is not bool:
                raise ValueError()
            checked = _CheckedGrant(node, command, granted)
            denial = action_denied(node, command, store=checked)
            refusal = capability_refusal(policy, command, cap["category"])
            unattended = permits_unattended(policy, command, cap["category"])
        except Exception:
            raise HardwareReviewError("Operator policy or grants unreadable") from None
        if denial or refusal:
            raise HardwareReviewError(denial or refusal)
        confirmed = (cap["requires_confirmation"] or cap["permission_tier"] != "passive"
                     or not unattended)
        return confirmed, checked

    def review(self, *, owner: str, node_id: str, command: str, params: dict, timeout: float = 10) -> HardwareReview:
        _id(owner), _id(node_id), _id(command)
        if type(timeout) not in {float, int} or not math.isfinite(timeout) or not 1 <= timeout <= 30:
            raise HardwareReviewError("Timeout outside bounds")
        now = self._now()
        # Expired unused reviews can be discarded; no live command ownership is evicted.
        self._reviews = {key: item for key, item in self._reviews.items() if item.review.expires_at > now}
        if len(self._reviews) >= 1024 or len(self._commands) >= 4096:
            raise HardwareReviewError("Review capacity unavailable")
        live = self._live(node_id)
        cap = self._capability(live.manifest, command, params)
        confirm, _ = self._permission(node_id, command, cap)
        review = HardwareReview(str(uuid4()), owner, node_id, live.generation, command,
                                _json(params, 16384), _json(live.manifest), cap["permission_tier"],
                                confirm, timeout, now, now + 300)
        self._reviews[review.review_id] = _Pending(review, live)
        return review

    def get_review(self, *, owner: str, review_id: str) -> HardwareReview:
        _id(owner), _uuid(review_id)
        pending = self._reviews.get(review_id)
        if pending is None or pending.review.owner != owner:
            raise HardwareReviewError("Unknown review for this owner")
        now = self._now()
        if not pending.review.created_at <= now < pending.review.expires_at:
            raise HardwareReviewError("Review expired")
        return pending.review

    def has_command(self, command_id: str) -> bool:
        return command_id in self._commands

    def validate_queued(self, *, binding: HardwareBinding, command_id: str) -> None:
        """Recheck captured approval/policy before an owned async send task writes.

        The review was consumed at synchronous queue acceptance; that approval
        belongs to this exact command. A revoked policy/grant or changed schema
        still prevents the write. No token is reused for another command.
        """
        owned = self._commands.get(command_id)
        if owned is None or not owned.enqueue_accepted or binding.connection is not owned.binding.connection or binding.generation != owned.binding.generation:
            raise HardwareReviewError("Exact queued command unavailable")
        current = self._live(binding.node_id)
        review = owned.review
        if current.connection is not binding.connection or current.generation != binding.generation or _json(current.manifest) != review.manifest_json:
            raise HardwareReviewError("Queued connection or manifest changed")
        record = self._ledger.get(command_id)
        if record is None or record.envelope.model_dump_json() != owned.envelope_json or record.state != CommandState.SUBMITTED or self._now() >= record.envelope.deadline:
            raise HardwareReviewError("Exact queued command expired or changed")
        cap = self._capability(current.manifest, review.command, json.loads(review.params_json))
        confirmation, _ = self._permission(binding.node_id, review.command, cap)
        if confirmation != review.requires_confirmation or cap["permission_tier"] != review.permission_tier:
            raise HardwareReviewError("Queued permission requirements changed")

    def dispatch(self, *, owner: str, review_id: str, authorization: Any = None) -> dict:
        _id(owner), _uuid(review_id)
        pending = self._reviews.get(review_id)
        if pending is None or pending.review.owner != owner:
            raise HardwareReviewError("Unknown review for this owner")
        # One-use before any uncertainty; a foreign owner cannot consume it.
        del self._reviews[review_id]
        review = pending.review
        params = json.loads(review.params_json)
        now = self._now()
        if not review.created_at <= now < review.expires_at:
            raise HardwareReviewError("Review expired or clock moved backwards")

        def guard() -> _CheckedGrant:
            current_time = self._now()
            if not review.created_at <= current_time < review.expires_at:
                raise HardwareReviewError("Review expired before enqueue")
            live = self._live(review.node_id)
            if (live.connection is not pending.binding.connection or live.generation != review.generation
                    or _json(live.manifest) != review.manifest_json):
                raise HardwareReviewError("Node connection or manifest changed")
            cap = self._capability(live.manifest, review.command, params)
            confirm, checked = self._permission(review.node_id, review.command, cap)
            if confirm != review.requires_confirmation or cap["permission_tier"] != review.permission_tier:
                raise HardwareReviewError("Operator confirmation requirements changed")
            if confirm:
                try:
                    authorized = self._authorize(review, authorization)
                    if inspect.isawaitable(authorized):
                        if inspect.iscoroutine(authorized):
                            authorized.close()
                        authorized = False
                except Exception:
                    authorized = False
                if authorized is not True:
                    raise HardwareReviewError("Separate exact reviewed authorization required")
            return checked

        checked = guard()
        envelope = CommandEnvelope(node_id=review.node_id, action=review.command, params=params,
                                   created_at=now, deadline=now + review.timeout)
        frame = build_action_request(review.node_id, review.command, params,
                                     timeout_ms=int(review.timeout * 1000), action_id=envelope.command_id,
                                     grant_store=checked)
        if not frame.allowed:
            raise HardwareReviewError("Capability grant refused")
        frozen = envelope.model_dump_json()
        record = self._ledger.submit(envelope)
        if record.envelope.model_dump_json() != frozen:
            raise HardwareReviewError("Ledger submitted a foreign command")
        self._commands[envelope.command_id] = _OwnedCommand(owner, pending.binding, frozen, review)
        try:
            # Dispatcher must invoke this immediately before exact socket reservation.
            guarded = False
            dispatch_envelope = envelope.model_copy(deep=True)
            frame_json = _json(frame.frame)
            def enqueue_guard():
                nonlocal guarded
                guard()
                if dispatch_envelope.model_dump_json() != frozen or _json(frame.frame) != frame_json:
                    raise HardwareReviewError("Exact dispatch payload changed")
                guarded = True
            accepted = self._enqueue(pending.binding, dispatch_envelope, frame.frame, enqueue_guard)
            if inspect.isawaitable(accepted):
                if inspect.iscoroutine(accepted):
                    accepted.close()
                raise HardwareReviewError("Async dispatcher cannot guarantee atomic reservation")
            if accepted is not True or not guarded:
                raise HardwareReviewError("Exact enqueue acceptance unavailable")
            self._commands[envelope.command_id] = replace(self._commands[envelope.command_id], enqueue_accepted=True)
        except Exception:
            self._ledger.update_state(envelope.command_id, CommandState.FAILED,
                                      message="Enqueue receipt unavailable; physical effect unknown")
            # A valid server command UUID is always available for uncertain readback.
        return self.readback(owner=owner, command_id=envelope.command_id)

    def receive(self, *, binding: HardwareBinding, command_id: str, payload: dict) -> bool:
        _uuid(command_id)
        owned = self._commands.get(command_id)
        if owned is None or binding.connection is not owned.binding.connection or binding.generation != owned.binding.generation or binding.node_id != owned.binding.node_id:
            return False
        try:
            current = self._live(binding.node_id)
            if current.connection is not binding.connection or current.generation != binding.generation:
                return False
            record = self._ledger.get(command_id)
            if record is None or record.envelope.model_dump_json() != owned.envelope_json:
                return False
            if record.state not in {CommandState.SUBMITTED, CommandState.ACKED, CommandState.RUNNING}:
                return False
            if record.envelope.deadline is None or self._now() >= record.envelope.deadline:
                self._ledger.update_state(command_id, CommandState.TIMED_OUT, message="Response deadline elapsed; physical effect unknown")
                return False
            if type(payload) is not dict:
                return False
            _json(payload)
            for key, expected in (("command_id", command_id), ("node_id", binding.node_id)):
                if key in payload and payload[key] != expected:
                    return False
            envelope = json.loads(owned.envelope_json)
            if "command" in payload and payload["command"] != envelope["action"]:
                return False
            if "params" in payload and _json(payload["params"]) != _json(envelope["params"]):
                return False
            if "ack" in payload and type(payload["ack"]) is not bool:
                return False
            if payload.get("ack") is True:
                self._ledger.ack(command_id)
            elif type(payload.get("success")) is bool:
                state = CommandState.SUCCEEDED if payload["success"] else CommandState.FAILED
                self._ledger.update_state(command_id, state, result=payload)
            else:
                return False
            return True
        except Exception:
            return False

    def expire_due(self) -> list[str]:
        """Explicit caller-driven ledger timeout; never resends or cancels hardware."""
        now, expired = self._now(), []
        for command_id, owned in self._commands.items():
            record = self._ledger.get(command_id)
            if record is None or record.envelope.model_dump_json() != owned.envelope_json:
                continue
            if record.state in {CommandState.SUBMITTED, CommandState.ACKED, CommandState.RUNNING} and record.envelope.deadline is not None and now >= record.envelope.deadline:
                self._ledger.update_state(command_id, CommandState.TIMED_OUT, message="Response deadline elapsed; physical effect unknown")
                expired.append(command_id)
        return expired

    def readback(self, *, owner: str, command_id: str) -> dict:
        _id(owner), _uuid(command_id)
        owned = self._commands.get(command_id)
        if owned is None or owned.owner != owner:
            raise HardwareReviewError("Command ownership unavailable")
        record = self._ledger.get(command_id)
        if record is None or record.envelope.model_dump_json() != owned.envelope_json:
            raise HardwareReviewError("Exact command ledger unavailable")
        state = record.state.value
        report = record.result
        if report is not None:
            if type(report) is not dict:
                raise HardwareReviewError("Private device report has unsupported type")
            _json(report, 65536)
        return {"command_id": command_id, "node_id": record.envelope.node_id,
                "generation": owned.binding.generation, "review_id": owned.review.review_id,
                "permission_tier": owned.review.permission_tier,
                "requires_confirmation": owned.review.requires_confirmation, "command": record.envelope.action,
                "params": record.envelope.params, "state": state,
                "queued": True if owned.enqueue_accepted else None,
                "acknowledged": record.ack_at is not None,
                "device_reported_success": record.result.get("success") if type(record.result) is dict and type(record.result.get("success")) is bool else None,
                "device_report": report, "physical_outcome_verified": False,
                "effect": "unknown", "automatic_retry": False}
