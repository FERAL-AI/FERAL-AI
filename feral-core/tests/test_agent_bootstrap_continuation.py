"""Reviewed bootstrap protocol fixtures. No real startup, providers or OS access."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from security.agent_bootstrap_continuation import (
    AgentBootstrapContinuation,
    BootstrapAdapter,
    BootstrapRefusal,
)
from security.vault_coordinator import VaultCoordinator


def fixture():
    vault = object()
    coordinator = VaultCoordinator(
        vault_factory=Mock(side_effect=AssertionError("No OS access"))
    )
    coordinator._vault = vault
    coordinator._state = "ready"
    coordinator._code = "ready"
    state = SimpleNamespace(
        vault=vault,
        config=object(),
        memory=object(),
        orchestrator=None,
        _native_bootstrap_required=True,
    )
    events = []
    revision = [b"configured-policy-revision"]

    async def run(checkpoint):
        checkpoint()
        events.append("init")
        state.orchestrator = object()
        checkpoint()
        events.append("memory-and-cron-hooks")

    def fence():
        events.append("fence")

    def commit():
        events.append("commit")
        state._native_bootstrap_required = False

    async def cleanup():
        events.append("cleanup")
        state.orchestrator = None

    adapter = BootstrapAdapter(
        lambda: revision[0],
        fence,
        run,
        commit,
        cleanup,
        lambda: "memory-and-cron-hooks" in events,
    )
    controller = AgentBootstrapContinuation(state, coordinator, adapter)
    return controller, state, coordinator, adapter, events, revision


def test_passive_status_never_calls_revision_or_os():
    c, s, v, a, events, revision = fixture()
    c.adapter = replace(
        a, revision=Mock(side_effect=AssertionError("Passive means passive"))
    )
    assert c.status()["bootstrap_required"]
    assert events == []


def test_no_unsafe_default_adapter():
    c, *_ = fixture()
    c.adapter = None
    assert c.status()["supported"] is False
    with pytest.raises(BootstrapRefusal, match="private details") as e:
        c.review()
    assert e.value.code == "continuation_unsupported"


@pytest.mark.asyncio
async def test_reviewed_completion_requires_actual_hooks_and_readback():
    c, s, v, a, events, revision = fixture()
    r = c.review()
    assert "providers may be contacted" in r["scope"]
    assert events == []
    receipt = await c.confirm(r["review_token"])
    assert receipt["ok"] and receipt["status"]["agent_ready"]
    assert events == ["fence", "init", "memory-and-cron-hooks", "commit"]
    with pytest.raises(BootstrapRefusal):
        await c.confirm(r["review_token"])
    await v.lock()
    assert not c.status()["agent_ready"]
    # Re-unlock cannot revive stale completion after lock generation changed.
    v._vault = s.vault
    v._state = "ready"
    v._code = "ready"
    assert not c.status()["agent_ready"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["config", "memory", "vault", "revision", "generation", "adapter"]
)
async def test_fresh_exact_binding_rejects_changed_identity_before_side_effects(change):
    c, s, v, a, events, revision = fixture()
    r = c.review()
    if change == "revision":
        revision[0] = b"changed provider credentials"
    elif change == "generation":
        v._generation += 1
    elif change == "adapter":
        c.adapter = replace(a)
    else:
        setattr(s, change, object())
    with pytest.raises(BootstrapRefusal):
        await c.confirm(r["review_token"])
    assert events == []


@pytest.mark.asyncio
async def test_cancel_expiry_and_one_use(monkeypatch):
    c, *_ = fixture()
    r = c.review()
    c.cancel(r["review_token"])
    with pytest.raises(BootstrapRefusal):
        await c.confirm(r["review_token"])
    r = c.review()
    born = c._reviews[r["review_token"]][0]
    monkeypatch.setattr(
        "security.agent_bootstrap_continuation.time.monotonic", lambda: born + 301
    )
    with pytest.raises(BootstrapRefusal):
        await c.confirm(r["review_token"])


@pytest.mark.asyncio
async def test_pending_timeout_and_caller_cancel_never_dispatch_twice():
    c, s, v, a, events, revision = fixture()
    gate = asyncio.Event()

    async def delayed(checkpoint):
        events.append("entered")
        await gate.wait()
        await a.run(checkpoint)

    c.adapter = replace(a, run=delayed)
    r = c.review()
    receipt = await c.confirm(r["review_token"], timeout=0)
    assert receipt["status"]["in_flight"] and not receipt["ok"]
    with pytest.raises(BootstrapRefusal):
        c.review()
    gate.set()
    await c._operation
    assert events.count("entered") == 1 and c.status()["agent_ready"]
    # A separate cancelled caller leaves the owned operation alive.
    c, s, v, a, events, revision = fixture()
    gate = asyncio.Event()

    async def second(checkpoint):
        events.append("entered")
        await gate.wait()
        await a.run(checkpoint)

    c.adapter = replace(a, run=second)
    r = c.review()
    caller = asyncio.create_task(c.confirm(r["review_token"]))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert c.status()["in_flight"]
    gate.set()
    await c._operation
    assert c.status()["agent_ready"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["partial", "missing_hooks", "lock", "cleanup", "shutdown"]
)
async def test_partial_failure_never_reports_ready_or_allows_retry(failure):
    c, s, v, a, events, revision = fixture()

    async def run(checkpoint):
        events.append("network-boundary")
        s.orchestrator = object()
        if failure == "lock":
            await v.lock()
            checkpoint()
        if failure == "shutdown":
            raise asyncio.CancelledError()
        if failure in ("partial", "cleanup"):
            raise RuntimeError("private credential")

    async def cleanup():
        events.append("cleanup")
        if failure == "cleanup":
            raise RuntimeError("private cleanup details")
        s.orchestrator = None

    c.adapter = replace(a, run=run, fail_closed=cleanup)
    r = c.review()
    receipt = await c.confirm(r["review_token"])
    assert not receipt["ok"] and receipt["status"]["restart_required"]
    assert s._native_bootstrap_required and "commit" not in events
    assert "private" not in str(receipt)
    with pytest.raises(BootstrapRefusal) as e:
        c.review()
    assert e.value.code == "restart_required"
    assert events.count("network-boundary") == 1


@pytest.mark.asyncio
async def test_failed_commit_retains_gate_and_cleanup():
    c, s, v, a, events, revision = fixture()
    c.adapter = replace(a, commit=lambda: None)
    result = await c.confirm(c.review()["review_token"])
    assert not result["ok"] and s._native_bootstrap_required and "cleanup" in events


@pytest.mark.asyncio
async def test_commit_cannot_publish_ready_without_actual_orchestrator():
    c, s, v, a, events, revision = fixture()

    def bad_commit():
        s._native_bootstrap_required = False
        s.orchestrator = None

    c.adapter = replace(a, commit=bad_commit)
    result = await c.confirm(c.review()["review_token"])
    assert not result["ok"] and result["status"]["restart_required"]
    assert s._native_bootstrap_required and "cleanup" in events
