"""Permission handling for external agents: it must fail closed, always.

The property under test is one-directional. It is easy to write a broker
that says yes when it should say no; every test here tries to find such a
path. There is deliberately no test asserting that anything is
auto-allowed, because nothing is.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridges.permissions import (  # noqa: E402
    ALLOW_KINDS,
    ApprovalManagerBroker,
    DenyAllBroker,
    PermissionDecision,
    QueueingBroker,
    parse_permission_request,
    reject,
)
from security.exec_approvals import ApprovalManager, ApprovalPolicy  # noqa: E402


def make_params(**overrides):
    params = {
        "sessionId": "sess-1",
        "toolCall": {
            "toolCallId": "tc-1",
            "title": "Run rm -rf build",
            "kind": "execute",
            "toolName": "bash",
        },
        "options": [
            {"optionId": "once", "name": "Allow once", "kind": "allow_once"},
            {"optionId": "always", "name": "Always allow", "kind": "allow_always"},
            {"optionId": "no", "name": "Reject", "kind": "reject_once"},
        ],
    }
    params.update(overrides)
    return params


class TestParsing:
    def test_pulls_the_tool_name_and_options_off_the_wire(self):
        request = parse_permission_request(make_params())
        assert request.session_id == "sess-1"
        assert request.tool_call_id == "tc-1"
        assert request.tool_name == "bash"
        assert request.title == "Run rm -rf build"
        assert [o.option_id for o in request.options] == ["once", "always", "no"]
        assert request.option("always").remembers is True
        assert request.option("once").remembers is False
        assert request.option("once").allows is True
        assert request.option("no").allows is False

    def test_falls_back_through_kind_and_title_for_a_name(self):
        params = make_params(
            toolCall={"toolCallId": "tc", "title": "Edit main.py", "kind": "edit"}
        )
        assert parse_permission_request(params).tool_name == "edit"
        params = make_params(toolCall={"toolCallId": "tc", "title": "Edit main.py"})
        assert parse_permission_request(params).tool_name == "Edit main.py"

    def test_approval_key_is_namespaced_away_from_ferals_own_tools(self):
        """An allow_always on an agent's bash must not grant FERAL's bash."""
        request = parse_permission_request(make_params())
        assert request.approval_key() == "external_agent:bash"
        assert request.approval_key() != "bash"

    def test_malformed_options_are_dropped_not_trusted(self):
        params = make_params(options=["not-an-object", {"optionId": "ok",
                                                        "name": "n",
                                                        "kind": "allow_once"}])
        request = parse_permission_request(params)
        assert [o.option_id for o in request.options] == ["ok"]


class TestOutcomeEncoding:
    def test_selected_outcome_carries_the_option_id(self):
        decision = PermissionDecision(option_id="once", allowed=True)
        assert decision.to_outcome() == {
            "outcome": {"outcome": "selected", "optionId": "once"}
        }

    def test_a_rejection_with_no_reject_option_becomes_cancelled(self):
        request = parse_permission_request(
            make_params(options=[{"optionId": "y", "name": "Yes", "kind": "allow_once"}])
        )
        decision = reject(request, "policy")
        assert decision.allowed is False
        assert decision.to_outcome() == {"outcome": {"outcome": "cancelled"}}

    def test_a_rejection_prefers_reject_once_over_reject_always(self):
        request = parse_permission_request(
            make_params(
                options=[
                    {"optionId": "never", "name": "Never", "kind": "reject_always"},
                    {"optionId": "no", "name": "No", "kind": "reject_once"},
                ]
            )
        )
        assert reject(request, "policy").option_id == "no"


class TestDenyAll:
    async def test_refuses_everything(self):
        request = parse_permission_request(make_params())
        decision = await DenyAllBroker().decide(request)
        assert decision.allowed is False
        assert decision.option_id == "no"


class TestQueueing:
    async def test_parks_until_answered(self):
        broker = QueueingBroker(timeout_seconds=10)
        request = parse_permission_request(make_params())
        task = asyncio.ensure_future(broker.decide(request))
        await asyncio.sleep(0.05)

        assert [r.request_id for r in broker.pending] == [request.request_id]
        assert broker.resolve(request.request_id, "once") is True

        decision = await task
        assert decision.allowed is True
        assert decision.option_id == "once"
        assert broker.pending == []

    async def test_an_unanswered_request_times_out_into_a_rejection(self):
        broker = QueueingBroker(timeout_seconds=0.15)
        request = parse_permission_request(make_params())
        decision = await broker.decide(request)
        assert decision.allowed is False
        assert "no answer" in decision.reason

    async def test_an_unknown_option_id_is_a_rejection_not_a_pass(self):
        broker = QueueingBroker(timeout_seconds=10)
        request = parse_permission_request(make_params())
        task = asyncio.ensure_future(broker.decide(request))
        await asyncio.sleep(0.05)
        assert broker.resolve(request.request_id, "definitely-not-an-option") is True
        decision = await task
        assert decision.allowed is False

    async def test_resolving_twice_is_a_no_op(self):
        broker = QueueingBroker(timeout_seconds=10)
        request = parse_permission_request(make_params())
        task = asyncio.ensure_future(broker.decide(request))
        await asyncio.sleep(0.05)
        assert broker.resolve(request.request_id, "once") is True
        assert broker.resolve(request.request_id, "always") is False
        assert (await task).option_id == "once"

    async def test_reject_all_clears_the_queue_on_cancel(self):
        broker = QueueingBroker(timeout_seconds=10)
        request = parse_permission_request(make_params())
        task = asyncio.ensure_future(broker.decide(request))
        await asyncio.sleep(0.05)
        broker.reject_all("session cancelled")
        decision = await task
        assert decision.allowed is False
        assert "cancelled" in decision.reason


class TestApprovalManagerIntegration:
    """The whole point: reuse FERAL's store, do not build a second one."""

    @pytest.fixture
    def manager(self):
        mgr = ApprovalManager(policy=ApprovalPolicy.ALLOWLIST, db_path=":memory:")
        yield mgr
        mgr.close()

    async def test_no_grant_falls_through_to_the_fallback(self, manager):
        broker = ApprovalManagerBroker(manager, "sess", fallback=DenyAllBroker())
        decision = await broker.decide(parse_permission_request(make_params()))
        assert decision.allowed is False

    async def test_an_existing_grant_answers_without_reprompting(self, manager):
        manager.grant_approval("external_agent:bash", "sess", scope="session")

        class Explode(DenyAllBroker):
            async def decide(self, request):
                raise AssertionError("fallback must not be consulted")

        broker = ApprovalManagerBroker(manager, "sess", fallback=Explode())
        decision = await broker.decide(parse_permission_request(make_params()))
        assert decision.allowed is True
        assert decision.option_id in ("once", "always")

    async def test_allow_always_is_persisted_into_the_store(self, manager):
        class SaysAlways(DenyAllBroker):
            async def decide(self, request):
                option = request.option("always")
                return PermissionDecision(
                    option_id=option.option_id, allowed=True, reason="user"
                )

        broker = ApprovalManagerBroker(manager, "sess", fallback=SaysAlways())
        await broker.decide(parse_permission_request(make_params()))
        allowed, _ = manager.check_approval("external_agent:bash", "sess")
        assert allowed is True

    async def test_allow_once_is_not_persisted(self, manager):
        class SaysOnce(DenyAllBroker):
            async def decide(self, request):
                option = request.option("once")
                return PermissionDecision(
                    option_id=option.option_id, allowed=True, reason="user"
                )

        broker = ApprovalManagerBroker(manager, "sess", fallback=SaysOnce())
        await broker.decide(parse_permission_request(make_params()))
        allowed, _ = manager.check_approval("external_agent:bash", "sess")
        assert allowed is False

    async def test_deny_policy_still_falls_through_and_denies(self):
        manager = ApprovalManager(policy=ApprovalPolicy.DENY, db_path=":memory:")
        try:
            broker = ApprovalManagerBroker(manager, "sess", fallback=DenyAllBroker())
            decision = await broker.decide(parse_permission_request(make_params()))
            assert decision.allowed is False
        finally:
            manager.close()

    async def test_a_broken_store_denies_rather_than_opening_the_door(self):
        class Broken:
            def check_approval(self, *_args, **_kwargs):
                raise RuntimeError("db is gone")

        class WouldSayYes(DenyAllBroker):
            async def decide(self, request):
                raise AssertionError("must not reach the fallback")

        broker = ApprovalManagerBroker(Broken(), "sess", fallback=WouldSayYes())
        decision = await broker.decide(parse_permission_request(make_params()))
        assert decision.allowed is False
        assert "unavailable" in decision.reason

    async def test_a_grant_with_no_allow_option_offered_still_rejects(self, manager):
        manager.grant_approval("external_agent:bash", "sess", scope="session")
        broker = ApprovalManagerBroker(manager, "sess")
        params = make_params(
            options=[{"optionId": "no", "name": "Reject", "kind": "reject_once"}]
        )
        decision = await broker.decide(parse_permission_request(params))
        assert decision.allowed is False

    async def test_default_fallback_denies_when_none_is_supplied(self, manager):
        broker = ApprovalManagerBroker(manager, "sess")
        decision = await broker.decide(parse_permission_request(make_params()))
        assert decision.allowed is False


def test_only_allow_shaped_kinds_count_as_allowing():
    """A regression guard: reject_always must never join ALLOW_KINDS."""
    assert set(ALLOW_KINDS) == {"allow_once", "allow_always"}
