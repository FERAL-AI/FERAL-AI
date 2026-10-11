"""Injected safety coordinator: real policy helpers + temporary SQLite ledger/grants.

No mesh connections, account services, physical devices or background tasks.
"""
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4
from unittest.mock import Mock

import pytest

from hardware.command_contract import CommandLedger, CommandState
from hardware.reviewed_dispatch import HardwareBinding, HardwareReviewError, ReviewedHardwareCoordinator
from security.capability_grants import CapabilityGrantStore

pytestmark = pytest.mark.no_auto_feral_home


class Policy:
    sensors = {"temperature"}
    actuators = {"move"}
    confirm_actuators = True
    def can_read_sensor(self, name):
        return name in self.sensors
    def can_use_actuator(self, name):
        return name in self.actuators, self.confirm_actuators
    def can_capture_camera(self):
        return False
    def can_emergency_stop(self):
        return False


@pytest.fixture
def rig(tmp_path):
    cap = {"id": "temperature", "category": "sensor", "permission_tier": "passive",
           "requires_confirmation": False, "action_type": "read", "parameters": []}
    manifest = {"device_id": "glasses", "connection_type": "websocket", "capabilities": [cap]}
    binding = HardwareBinding("glasses", str(uuid4()), object(), manifest)
    state = SimpleNamespace(binding=binding, policy=Policy(),
                            grants=CapabilityGrantStore(str(tmp_path / "grants.db")), now=1000., frames=[])
    ledger = CommandLedger(str(tmp_path / "ledger.db"))
    def enqueue(captured, envelope, frame, guard):
        guard()
        assert captured.connection is state.binding.connection
        state.frames.append((captured, envelope, frame))
        return True
    authorizer = Mock(side_effect=lambda review, token: token == (review.review_id, review.owner))
    coordinator = ReviewedHardwareCoordinator(binding=lambda node: state.binding if node == "glasses" else None,
        policy=lambda: state.policy, grants=lambda: state.grants, ledger=ledger,
        enqueue=enqueue, authorize=authorizer, clock=lambda: state.now)
    return state, ledger, coordinator, authorizer


def review(rig, **kwargs):
    return rig[2].review(owner="session-1", node_id="glasses", command="temperature", params={}, **kwargs)


def send(rig, item=None, **kwargs):
    item = item or review(rig)
    return rig[2].dispatch(owner=item.owner, review_id=item.review_id, **kwargs)


def test_server_uuid_frame_ledger_and_readback_are_exact_without_physical_claim(rig):
    state, ledger, coordinator, authorizer = rig
    receipt = send(rig)
    command_id = receipt["command_id"]
    assert str(UUID(command_id)) == command_id
    captured, envelope, frame = state.frames[0]
    assert frame["payload"]["action_id"] == command_id == envelope.command_id
    assert frame["payload"]["name"] == "temperature"
    assert captured is state.binding
    assert ledger.get(command_id).envelope.node_id == "glasses"
    assert receipt["state"] == "submitted" and receipt["queued"] is True
    assert receipt["acknowledged"] is False and receipt["device_reported_success"] is None
    assert receipt["effect"] == "unknown" and receipt["physical_outcome_verified"] is False
    assert receipt["automatic_retry"] is False
    authorizer.assert_not_called()


def test_ack_final_and_replay_have_distinct_strict_receipts(rig):
    state, _, coordinator, _ = rig
    cid = send(rig)["command_id"]
    assert coordinator.receive(binding=state.binding, command_id=cid, payload={"ack": True})
    receipt = coordinator.readback(owner="session-1", command_id=cid)
    assert receipt["state"] == "acked" and receipt["acknowledged"] is True
    assert receipt["device_reported_success"] is None
    assert coordinator.receive(binding=state.binding, command_id=cid, payload={"success": True})
    receipt = coordinator.readback(owner="session-1", command_id=cid)
    assert receipt["state"] == "succeeded" and receipt["device_reported_success"] is True
    assert receipt["physical_outcome_verified"] is False
    assert not coordinator.receive(binding=state.binding, command_id=cid, payload={"success": False})
    assert not coordinator.receive(binding=state.binding, command_id=cid, payload={"ack": True})
    assert coordinator.readback(owner="session-1", command_id=cid)["state"] == "succeeded"


@pytest.mark.parametrize("mutation", ["socket", "generation", "manifest", "policy", "grants"])
def test_fresh_exact_node_manifest_and_operator_policy_before_enqueue(rig, mutation):
    state, _, coordinator, _ = rig
    item = review(rig)
    if mutation == "socket":
        state.binding = replace(state.binding, connection=object())
    elif mutation == "generation":
        state.binding = replace(state.binding, generation=str(uuid4()))
    elif mutation == "manifest":
        state.binding.manifest["capabilities"][0]["safety_notes"] = "changed"
    elif mutation == "policy":
        state.policy.sensors = set()
    else:
        state.grants.set_grant("glasses", "temperature", False)
    with pytest.raises(HardwareReviewError):
        send(rig, item)
    assert state.frames == []
    with pytest.raises(HardwareReviewError):
        send(rig, item)


def test_mutation_inside_dispatcher_rechecked_immediately_before_enqueue(rig):
    state, ledger, coordinator, _ = rig
    def enqueue(captured, envelope, frame, guard):
        state.binding = replace(state.binding, connection=object(), generation=str(uuid4()))
        guard()
        state.frames.append(frame)
        return True
    coordinator._enqueue = enqueue
    receipt = send(rig)
    assert state.frames == [] and receipt["queued"] is None
    assert receipt["state"] == "failed" and receipt["effect"] == "unknown"
    assert ledger.get(receipt["command_id"]).result is None


@pytest.mark.parametrize("unavailable", ["policy", "grants", "unreadable", "nonbool"])
def test_unavailable_operator_input_never_uses_legacy_fail_open(rig, unavailable):
    state, _, _, _ = rig
    if unavailable in {"policy", "grants"}:
        setattr(state, unavailable, None)
    elif unavailable == "unreadable":
        state.grants = SimpleNamespace(is_granted=Mock(side_effect=OSError("private path")))
    else:
        state.grants = SimpleNamespace(is_granted=Mock(return_value=1))
    with pytest.raises(HardwareReviewError):
        review(rig)
    assert state.frames == []


@pytest.mark.parametrize("tier,explicit", [("passive", True), ("active", False), ("privileged", False), ("dangerous", False)])
def test_declared_escalation_requires_separate_exact_authorization_even_sensor(rig, tier, explicit):
    state, _, coordinator, authorizer = rig
    cap = state.binding.manifest["capabilities"][0]
    cap.update(permission_tier=tier, requires_confirmation=explicit)
    item = review(rig)
    assert item.requires_confirmation
    with pytest.raises(HardwareReviewError, match="authorization"):
        send(rig, item, authorization=True)
    assert state.frames == []
    item = review(rig)
    receipt = send(rig, item, authorization=(item.review_id, item.owner))
    assert receipt["queued"] is True
    assert authorizer.call_count >= 2


def test_global_actuator_confirmation_cannot_be_deescalated_by_manifest(rig):
    state, _, coordinator, _ = rig
    cap = state.binding.manifest["capabilities"][0]
    cap.update(id="move", category="actuator", permission_tier="passive", action_type="execute")
    item = coordinator.review(owner="session-1", node_id="glasses", command="move", params={})
    assert item.requires_confirmation
    with pytest.raises(HardwareReviewError):
        send(rig, item)
    assert state.frames == []


@pytest.mark.parametrize("change", ["foreign-owner", "expired", "clock-backwards"])
def test_owner_expiry_and_single_use(rig, change):
    state, _, coordinator, _ = rig
    item = review(rig)
    if change == "foreign-owner":
        with pytest.raises(HardwareReviewError):
            coordinator.dispatch(owner="session-2", review_id=item.review_id)
        assert send(rig, item)["queued"] is True
    else:
        state.now += 300 if change == "expired" else -1
        with pytest.raises(HardwareReviewError):
            send(rig, item)
        assert state.frames == []
    with pytest.raises(HardwareReviewError):
        send(rig, item)


@pytest.mark.parametrize("payload", [{"success": 1}, {"ack": 1}, {"ack": "true"}, {},
    {"success": True, "node_id": "foreign"}, {"success": True, "command_id": str(uuid4())},
    {"success": True, "command": "move"}, {"success": True, "params": {"extra": 1}}])
def test_uncorrelated_or_malformed_device_reply_never_finishes_command(rig, payload):
    state, _, coordinator, _ = rig
    cid = send(rig)["command_id"]
    assert not coordinator.receive(binding=state.binding, command_id=cid, payload=payload)
    assert coordinator.readback(owner="session-1", command_id=cid)["state"] == "submitted"


def test_foreign_socket_generation_owner_and_restarted_coordinator_cannot_claim_command(rig):
    state, ledger, coordinator, _ = rig
    cid = send(rig)["command_id"]
    assert not coordinator.receive(binding=replace(state.binding, connection=object()), command_id=cid, payload={"success": True})
    assert not coordinator.receive(binding=replace(state.binding, generation=str(uuid4())), command_id=cid, payload={"success": True})
    old = state.binding
    state.binding = replace(old, connection=object(), generation=str(uuid4()))
    assert not coordinator.receive(binding=old, command_id=cid, payload={"success": True})
    with pytest.raises(HardwareReviewError):
        coordinator.readback(owner="session-2", command_id=cid)
    other = ReviewedHardwareCoordinator(binding=lambda _: state.binding, policy=lambda: state.policy,
        grants=lambda: state.grants, ledger=ledger, enqueue=Mock(), authorize=Mock())
    with pytest.raises(HardwareReviewError):
        other.readback(owner="session-1", command_id=cid)


def test_timeout_is_effect_unknown_and_late_success_does_not_overwrite(rig):
    state, _, coordinator, _ = rig
    cid = send(rig)["command_id"]
    state.now += 10
    assert coordinator.expire_due() == [cid]
    assert coordinator.expire_due() == []
    assert not coordinator.receive(binding=state.binding, command_id=cid, payload={"success": True})
    receipt = coordinator.readback(owner="session-1", command_id=cid)
    assert receipt["state"] == "timed_out" and receipt["effect"] == "unknown"
    assert receipt["device_reported_success"] is None
    assert len(state.frames) == 1


def test_missing_guard_async_or_uncertain_enqueue_has_no_false_queue_receipt(rig):
    _, _, coordinator, _ = rig
    for callback in (lambda *args: True, Mock(side_effect=OSError("secret"))):
        coordinator._enqueue = callback
        receipt = send(rig)
        assert receipt["state"] == "failed" and receipt["queued"] is None
        assert "secret" not in str(receipt)
    async def enqueue(*args):
        raise AssertionError("Must never run coroutine")
    coordinator._enqueue = enqueue
    assert send(rig)["queued"] is None


@pytest.mark.parametrize("mutation", ["unknown", "duplicate", "tier", "category", "compound", "bool-number", "extra", "constraint"])
def test_unsupported_declared_capability_and_schema_fail_closed(rig, mutation):
    state, _, coordinator, _ = rig
    cap = state.binding.manifest["capabilities"][0]
    params, command = {}, "temperature"
    if mutation == "unknown":
        command = "made_up"
    elif mutation == "duplicate":
        state.binding.manifest["capabilities"].append(dict(cap))
    elif mutation in {"tier", "category"}:
        cap["permission_tier" if mutation == "tier" else "category"] = "made_up"
    elif mutation == "compound":
        cap["parameters"] = [{"name": "value", "type": "object"}]
    elif mutation == "bool-number":
        cap["parameters"] = [{"name": "value", "type": "number", "required": True}]
        params = {"value": True}
    elif mutation == "extra":
        params = {"value": 1}
    else:
        cap["parameters"] = [{"name": "value", "type": "string", "pattern": "safe"}]
    with pytest.raises(HardwareReviewError):
        coordinator.review(owner="session-1", node_id="glasses", command=command, params=params)
    assert state.frames == []


def test_typed_enum_bounds_and_required_schema_are_enforced(rig):
    state, _, coordinator, _ = rig
    state.binding.manifest["capabilities"][0]["parameters"] = [
        {"name": "count", "type": "integer", "required": True, "minimum": 1, "maximum": 3},
        {"name": "mode", "type": "string", "required": True, "enum": ["once"]}]
    for params in ({}, {"count": True, "mode": "once"}, {"count": 4, "mode": "once"}, {"count": 1, "mode": "all"}):
        with pytest.raises(HardwareReviewError):
            coordinator.review(owner="session-1", node_id="glasses", command="temperature", params=params)
    params = {"count": 2, "mode": "once"}
    item = coordinator.review(owner="session-1", node_id="glasses", command="temperature", params=params)
    params["count"] = 100
    assert send(rig, item)["params"] == {"count": 2, "mode": "once"}


@pytest.mark.parametrize("mutation", ["expired", "envelope", "frame", "revoked"])
def test_queue_guard_rechecks_age_exact_outbound_context_and_grants(rig, mutation):
    state, _, coordinator, _ = rig
    def enqueue(captured, envelope, frame, guard):
        if mutation == "expired":
            state.now += 300
        elif mutation == "envelope":
            envelope.action = "move"
        elif mutation == "frame":
            frame["payload"]["name"] = "move"
        else:
            state.grants.set_grant("glasses", "temperature", False)
        guard()
        state.frames.append(frame)
        return True
    coordinator._enqueue = enqueue
    receipt = send(rig)
    assert not state.frames and receipt["queued"] is None
    assert receipt["state"] == "failed" and receipt["physical_outcome_verified"] is False


def test_ledger_readback_missing_or_foreign_is_unavailable_not_empty_or_success(rig):
    _, _, coordinator, _ = rig
    cid = send(rig)["command_id"]
    with pytest.raises(HardwareReviewError):
        coordinator.readback(owner="session-1", command_id=str(uuid4()))
    coordinator._ledger = SimpleNamespace(get=lambda _: None)
    with pytest.raises(HardwareReviewError, match="ledger unavailable"):
        coordinator.readback(owner="session-1", command_id=cid)


def test_exact_final_private_report_is_bounded_and_not_synthesized(rig):
    state, ledger, coordinator, _ = rig
    cid = send(rig)["command_id"]
    assert coordinator.readback(owner="session-1", command_id=cid)["device_report"] is None
    payload = {"success": True, "data": {"temperature": 36.5, "private": "fixture only"}}
    assert coordinator.receive(binding=state.binding, command_id=cid, payload=payload)
    receipt = coordinator.readback(owner="session-1", command_id=cid)
    assert receipt["device_report"] == payload
    assert receipt["physical_outcome_verified"] is False
    original = ledger.get(cid)
    for bad in ("serialized fallback", ["unsupported"], {"success": True, "data": "x" * 65537}):
        coordinator._ledger = SimpleNamespace(get=lambda _: SimpleNamespace(
            envelope=original.envelope, state=original.state, result=bad, ack_at=None))
        with pytest.raises(HardwareReviewError):
            coordinator.readback(owner="session-1", command_id=cid)


def test_oversized_device_report_never_enters_ledger(rig):
    state, _, coordinator, _ = rig
    cid = send(rig)["command_id"]
    assert not coordinator.receive(binding=state.binding, command_id=cid, payload={"success": True, "data": "x" * 65537})
    receipt = coordinator.readback(owner="session-1", command_id=cid)
    assert receipt["state"] == "submitted" and receipt["device_report"] is None
