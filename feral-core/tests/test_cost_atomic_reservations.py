"""Actual SQLite + public provider attempts; all wire effects are inert sinks."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from unittest.mock import AsyncMock
from types import SimpleNamespace

import httpx
import pytest

from agents.llm_provider import LLMProvider, _budget_scope
from agents.llm_failover import ProviderCooldownTracker, _retry_llm_call
from cost.budget import BudgetExceeded, CostBudget, PricingUnavailable
from cost.pricing import ModelPricing


@pytest.fixture
def ledger(tmp_path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"providers": {"fixture": {"pricing": {
        "fixture-model": {"input": 0.5, "output": 1.0},
        "fixture-fallback": {"input": 0.25, "output": 0.5},
    }}}}))
    return {"db_path": tmp_path / "cost.db", "pricing": ModelPricing(catalog),
            "settings": {"cost": {"global_per_hour_usd": 1.5}}}


def provider(budget, handler, *, config=None, model="fixture-model", name="openai"):
    item = LLMProvider.__new__(LLMProvider)
    item.provider, item.model = name, model
    item.base_url, item.api_key = "https://fixture.invalid/v1", "inert-key"
    item._config = config or {}
    item._local_engine = None
    item._cooldown = ProviderCooldownTracker()
    item._last_budget_routing = {}
    item._auth_permanent_until = {}
    item._auth_permanent_logged = set()
    item.set_cost_budget(budget)
    item.client = httpx.AsyncClient(base_url=item.base_url, transport=httpx.MockTransport(handler))
    return item


def answer(*, usage=True):
    data = {"choices": [{"message": {"role": "assistant", "content": "inert result"}, "finish_reason": "stop"}]}
    if usage:
        data["usage"] = {"prompt_tokens": 10, "completion_tokens": 100}
    return httpx.Response(200, json=data)


async def test_two_instances_admit_only_one_shared_cap_reservation(ledger):
    first, second = CostBudget(**ledger), CostBudget(**ledger)
    try:
        await first.ensure_ready()
        await second.ensure_ready()
        results = await asyncio.gather(first.reserve("chat", "fixture-model", 0, 1000),
                                       second.reserve("chat", "fixture-model", 0, 1000), return_exceptions=True)
        assert sum(isinstance(result, BudgetExceeded) for result in results) == 1
        assert len(await first.get_reservations()) == 1
    finally:
        await asyncio.gather(first.close(), second.close())


@pytest.mark.parametrize("initialization", range(5))
async def test_two_instances_initialize_and_admit_concurrently(ledger, initialization):
    first, second = CostBudget(**ledger), CostBudget(**ledger)
    try:
        results = await asyncio.gather(first.reserve("chat", "fixture-model", 0, 1000),
                                       second.reserve("chat", "fixture-model", 0, 1000), return_exceptions=True)
        assert sum(isinstance(result, BudgetExceeded) for result in results) == 1, results
        assert sum(isinstance(result, str) for result in results) == 1, results
    finally:
        await asyncio.gather(first.close(), second.close())


async def test_reopen_and_window_reset_never_expire_unknown_hold(ledger, monkeypatch):
    budget = CostBudget(**ledger)
    identity = await budget.reserve("chat", "fixture-model", 0, 1000)
    await budget.mark_dispatched(identity)
    await budget.mark_unknown(identity)
    await budget.close()
    reopened = CostBudget(**ledger)
    try:
        import time
        future = time.time() + 172800
        monkeypatch.setattr("cost.budget.time.time", lambda: future)
        with pytest.raises(BudgetExceeded):
            await reopened.reserve("chat", "fixture-model", 0, 1000)
        assert (await reopened.get_reservations())[0]["status"] == "outcome_unknown"
    finally:
        await reopened.close()


async def test_release_requires_proven_undispatched_receipt(ledger):
    budget = CostBudget(**ledger)
    try:
        first = await budget.reserve("chat", "fixture-model", 0, 1000)
        await budget.release(first)
        second = await budget.reserve("chat", "fixture-model", 0, 1000)
        await budget.mark_dispatched(second)
        with pytest.raises(ValueError, match="reconciliation"):
            await budget.release(second)
        await budget.mark_unknown(second)
        with pytest.raises(ValueError, match="reconciliation"):
            await budget.release(second)
    finally:
        await budget.close()


async def test_settlement_atomic_once_and_late_receipt_reconciles_unknown(ledger):
    budget = CostBudget(**ledger)
    try:
        identity = await budget.reserve("chat", "fixture-model", 0, 1000)
        await budget.mark_dispatched(identity)
        await budget.mark_unknown(identity)
        for _ in range(2):
            assert await budget.record_usage("chat", "fixture-model", 10, 100, reservation_id=identity) == pytest.approx(.105)
        with sqlite3.connect(ledger["db_path"]) as db:
            count, dollars = db.execute("SELECT COUNT(*), SUM(dollars) FROM cost_events").fetchone()
            assert count == 1 and dollars == pytest.approx(.105)
        assert (await budget.get_reservations())[0]["status"] == "settled"
        assert await budget.reserve("chat", "fixture-model", 0, 1000)
    finally:
        await budget.close()


async def test_settlement_must_match_owner_site_and_model(ledger):
    budget = CostBudget(**ledger)
    try:
        identity = await budget.reserve("chat", "fixture-model", 0, 1000)
        await budget.mark_dispatched(identity)
        for site, model in (("learner", "fixture-model"), ("chat", "fixture-fallback")):
            with pytest.raises(ValueError, match="scope"):
                await budget.record_usage(site, model, 0, 100, reservation_id=identity)
        assert budget.current_spend() == 0
    finally:
        await budget.close()


async def test_incurred_over_estimate_is_persisted_even_over_cap(ledger):
    budget = CostBudget(**ledger)
    try:
        identity = await budget.reserve("chat", "fixture-model", 0, 1000)
        await budget.mark_dispatched(identity)
        with pytest.raises(BudgetExceeded):
            await budget.record_usage("chat", "fixture-model", 0, 2000, reservation_id=identity)
        row = (await budget.get_reservations())[0]
        assert row["status"] == "settled" and row["actual_dollars"] == 2.0
        assert budget.current_spend() == 2.0
    finally:
        await budget.close()


@pytest.mark.parametrize("unknown,unpriceable", [(True, False), (False, True)])
async def test_bounded_unknown_or_multimodal_pricing_is_explicit(ledger, unknown, unpriceable):
    budget = CostBudget(**ledger)
    try:
        with pytest.raises(PricingUnavailable):
            await budget.reserve("chat", "unknown" if unknown else "fixture-model", 1, 1,
                                 unpriceable_input=unpriceable)
        assert await budget.get_reservations() == []
    finally:
        await budget.close()


async def test_unlimited_unknown_basis_is_visible_not_zero_pricing(ledger):
    budget = CostBudget(**{**ledger, "settings": {"cost": {"enabled": True}}})
    try:
        await budget.reserve("chat", "unknown", 1000, 1000)
        row = (await budget.get_reservations())[0]
        assert row["pricing_basis"] == "fallback_estimate_unverified" and row["estimated_dollars"] > 0
    finally:
        await budget.close()


async def test_disabled_choice_has_no_hold(ledger):
    budget = CostBudget(**ledger, enabled=False)
    try:
        assert await budget.reserve("chat", "unknown", 1000, 1000) is None
        assert await budget.get_reservations() == []
    finally:
        await budget.close()


async def test_explicit_local_compute_basis_does_not_bill_api_tokens(ledger):
    budget = CostBudget(**ledger)
    try:
        identity = await budget.reserve("chat", "unknown-local", 1000, 1000,
                                        local_free=True, unpriceable_input=True)
        await budget.mark_dispatched(identity)
        assert await budget.record_usage("chat", "unknown-local", 1000, 1000, reservation_id=identity) == 0
        assert (await budget.get_reservations())[0]["pricing_basis"] == "local_compute_only"
    finally:
        await budget.close()


async def test_prompt_estimate_contributes_to_admission(ledger):
    budget = CostBudget(**ledger)
    try:
        with pytest.raises(BudgetExceeded):
            await budget.reserve("chat", "fixture-model", 4000, 1)
        assert await budget.get_reservations() == []
    finally:
        await budget.close()


@pytest.mark.parametrize("shared_provider", [False, True])
async def test_actual_public_chat_two_instances_have_one_wire_attempt(ledger, shared_provider):
    first, second = CostBudget(**ledger), CostBudget(**ledger)
    entered, finish = asyncio.Event(), asyncio.Event()
    requests = []

    async def wire(request):
        requests.append(request)
        entered.set()
        await finish.wait()
        return answer()

    p1 = provider(first, wire)
    p2 = p1 if shared_provider else provider(second, wire)
    try:
        task = asyncio.create_task(p1.chat([{"role": "user", "content": "inert"}], max_tokens=1000))
        await asyncio.wait_for(entered.wait(), 3)
        result = await p2.chat([{"role": "user", "content": "inert"}], max_tokens=1000)
        assert result.get("error") and len(requests) == 1
        finish.set()
        assert not (await task).get("error")
        rows = await first.get_reservations()
        assert len(rows) == 1 and rows[0]["status"] == "settled"
        assert rows[0]["prompt_tokens"] >= 256
    finally:
        finish.set()
        await asyncio.gather(p1.client.aclose(), p2.client.aclose(), first.close(), second.close())


@pytest.mark.parametrize("usage", [None, {"prompt_tokens": 10},
                                  {"prompt_tokens": 10, "completion_tokens": 10, "truncated": True}])
async def test_actual_response_missing_or_truncated_usage_never_releases_hold(ledger, usage):
    budget = CostBudget(**ledger)

    def wire(request):
        response = answer(usage=False).json()
        if usage is not None:
            response["usage"] = usage
        return httpx.Response(200, json=response)

    item = provider(budget, wire)
    try:
        await item.chat([{"role": "user", "content": "inert"}], max_tokens=1000)
        row = (await budget.get_reservations())[0]
        assert row["status"] == "outcome_unknown"
    finally:
        await item.client.aclose()
        await budget.close()


async def test_partial_cumulative_receipts_reconcile_without_double_charge(ledger):
    budget = CostBudget(**ledger)
    try:
        identity = await budget.reserve("chat", "fixture-model", 0, 1000)
        await budget.mark_dispatched(identity)
        await budget.record_usage("chat", "fixture-model", 10, 0,
                                  reservation_id=identity, usage_complete=False)
        assert (await budget.get_reservations())[0]["status"] == "outcome_unknown"
        assert budget.current_spend() == pytest.approx(.005)
        await budget.record_usage("chat", "fixture-model", 10, 100, reservation_id=identity)
        assert budget.current_spend() == pytest.approx(.105)
        assert (await budget.get_reservations())[0]["status"] == "settled"
    finally:
        await budget.close()


async def test_same_scope_partial_then_full_receipt_settles_only_latest_attempt(ledger):
    budget = CostBudget(**ledger)
    item = provider(budget, lambda request: answer())
    scope = {"provider": item, "task": asyncio.current_task(), "site": "chat", "attempts": []}
    token = _budget_scope.set(scope)
    try:
        # The first attempt lost its response; the retry has cumulative usage.
        body = {"model": "fixture-model", "messages": [], "max_tokens": 300}
        await item._budget_dispatch(body)
        await item._budget_dispatch(body)
        partial = {"usage": {"prompt_tokens": 10, "completion_tokens": 0},
                   "_feral_usage_complete": False}
        complete = {"usage": {"prompt_tokens": 10, "completion_tokens": 100}}
        await item._budget_record("chat", "fixture-model", partial)
        await item._budget_record("chat", "fixture-model", partial)
        await item._budget_record("chat", "fixture-model", complete)
        await item._budget_record("chat", "fixture-model", complete)
        await item._budget_finish_scope(scope)
        rows = {row["reservation_id"]: row for row in await budget.get_reservations()}
        assert rows[scope["attempts"][0]["id"]]["status"] == "outcome_unknown"
        assert rows[scope["attempts"][0]["id"]]["actual_dollars"] is None
        assert rows[scope["attempts"][1]["id"]]["status"] == "settled"
        assert budget.current_spend() == pytest.approx(.105)
        assert "unmatched_usage" not in scope
    finally:
        _budget_scope.reset(token)
        await item.client.aclose()
        await budget.close()


@pytest.mark.parametrize("responses,streaming", [(False, False), (False, True),
                                               (True, False), (True, True)])
async def test_public_reasoning_subset_is_billed_once(ledger, responses, streaming):
    model = "gpt-5.6-sol" if responses else "fixture-model"
    catalog = ledger["pricing"].catalog_path
    data = json.loads(catalog.read_text())
    data["providers"]["fixture"]["pricing"][model] = {"input": .5, "output": 1}
    catalog.write_text(json.dumps(data))
    usage = ({"input_tokens": 10, "output_tokens": 100,
              "output_tokens_details": {"reasoning_tokens": 60}} if responses else
             {"prompt_tokens": 10, "completion_tokens": 100,
              "completion_tokens_details": {"reasoning_tokens": 60}})
    payload = ({"model": model, "status": "completed", "output": [
        {"type": "message", "content": [{"type": "output_text", "text": "inert"}]}], "usage": usage}
        if responses else {**answer(usage=False).json(), "usage": usage})

    def wire(request):
        if not streaming:
            return httpx.Response(200, json=payload)
        event = ({"type": "response.completed", "response": payload} if responses else
                 {"choices": [], "usage": usage})
        return httpx.Response(200, text="data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n")

    budget = CostBudget(**ledger)
    item = provider(budget, wire, model=model)
    try:
        messages = [{"role": "user", "content": "inert"}]
        if streaming:
            events = [event async for event in item.chat_stream(messages, max_tokens=1000)]
            assert events[-1]["type"] == "done"
        else:
            assert not (await item.chat(messages, max_tokens=1000)).get("error")
        assert (await budget.get_reservations())[0]["status"] == "settled"
        assert budget.current_spend() == pytest.approx(.105)
        assert LLMProvider._extract_usage({"usage": usage}) == (10, 40, 60)
    finally:
        await item.client.aclose()
        await budget.close()


def test_top_level_reasoning_preserves_existing_additive_contract():
    assert LLMProvider._extract_usage({"usage": {
        "input_tokens": 10, "output_tokens": 100, "thinking_tokens": 60}}) == (10, 100, 60)


async def test_unknown_pricing_actual_public_call_has_explicit_code_and_zero_wire(ledger):
    budget = CostBudget(**ledger)
    wire = AsyncMock(return_value=answer())
    item = provider(budget, wire, model="unknown")
    try:
        result = await item.chat([{"role": "user", "content": "inert"}], max_tokens=1000)
        assert result["error_code"] == "pricing_unavailable"
        wire.assert_not_called()
    finally:
        await item.client.aclose()
        await budget.close()


@pytest.mark.parametrize("after_commit", [False, True])
async def test_admission_cancellation_at_sqlite_boundary(ledger, monkeypatch, after_commit):
    budget = CostBudget(**ledger)
    try:
        await budget.ensure_ready()
        identity = "fixture-stable-reservation"
        if after_commit:
            original = budget._conn.commit

            async def cancelled_commit():
                await original()
                monkeypatch.setattr(budget._conn, "commit", original)
                raise asyncio.CancelledError

            monkeypatch.setattr(budget._conn, "commit", cancelled_commit)
        else:
            original = budget._conn.execute

            async def cancelled_begin(sql, *args):
                cursor = await original(sql, *args)
                if sql == "BEGIN IMMEDIATE":
                    monkeypatch.setattr(budget._conn, "execute", original)
                    raise asyncio.CancelledError
                return cursor

            monkeypatch.setattr(budget._conn, "execute", cancelled_begin)
        with pytest.raises(asyncio.CancelledError):
            await budget.reserve("chat", "fixture-model", 0, 1000, reservation_id=identity)
        assert not budget._conn.in_transaction
        rows = await budget.get_reservations()
        assert len(rows) == int(after_commit)
        if after_commit:
            assert rows[0]["reservation_id"] == identity and rows[0]["status"] == "held"
            await budget.release(identity)
    finally:
        await budget.close()


async def test_actual_public_chat_cancellation_after_wire_keeps_unknown(ledger):
    budget = CostBudget(**ledger)
    entered = asyncio.Event()

    async def wire(request):
        entered.set()
        await asyncio.Future()

    item = provider(budget, wire)
    try:
        task = asyncio.create_task(item.chat([{"role": "user", "content": "inert"}], max_tokens=1000))
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await budget.get_reservations())[0]["status"] == "outcome_unknown"
        with pytest.raises(BudgetExceeded):
            await budget.reserve("chat", "fixture-model", 0, 1000)
    finally:
        await item.client.aclose()
        await budget.close()


async def test_dispatch_marker_commit_cancellation_retains_unknown_without_wire(ledger, monkeypatch):
    budget = CostBudget(**ledger)
    mark = budget.mark_dispatched

    async def cancelled_mark(identity):
        await mark(identity)
        raise asyncio.CancelledError

    monkeypatch.setattr(budget, "mark_dispatched", cancelled_mark)
    wire = AsyncMock(return_value=answer())
    item = provider(budget, wire)
    try:
        with pytest.raises(asyncio.CancelledError):
            await item.chat([{"role": "user", "content": "inert"}], max_tokens=1000)
        wire.assert_not_called()
        assert (await budget.get_reservations())[0]["status"] == "outcome_unknown"
    finally:
        await item.client.aclose()
        await budget.close()


async def test_hold_commit_cancellation_releases_only_undispatched_wire(ledger, monkeypatch):
    budget = CostBudget(**ledger)
    await budget.ensure_ready()
    commit = budget._conn.commit

    async def cancelled_commit():
        await commit()
        monkeypatch.setattr(budget._conn, "commit", commit)
        raise asyncio.CancelledError

    monkeypatch.setattr(budget._conn, "commit", cancelled_commit)
    wire = AsyncMock(return_value=answer())
    item = provider(budget, wire)
    try:
        with pytest.raises(asyncio.CancelledError):
            await item.chat([{"role": "user", "content": "inert"}], max_tokens=1000)
        wire.assert_not_called()
        assert (await budget.get_reservations())[0]["status"] == "released"
    finally:
        await item.client.aclose()
        await budget.close()


async def test_actual_public_chat_missing_usage_retains_hold(ledger):
    budget = CostBudget(**ledger)
    item = provider(budget, lambda request: answer(usage=False))
    try:
        assert not (await item.chat([{"role": "user", "content": "inert"}], max_tokens=1000)).get("error")
        assert (await budget.get_reservations())[0]["status"] == "outcome_unknown"
        assert budget.current_spend() == 0
    finally:
        await item.client.aclose()
        await budget.close()


async def test_copied_context_cannot_dispatch_with_parent_hold(ledger):
    budget = CostBudget(**ledger)
    item = provider(budget, lambda request: answer())
    token = _budget_scope.set({"provider": item, "task": asyncio.current_task(), "site": "chat", "attempts": []})
    try:
        child = asyncio.create_task(item._budget_dispatch({"model": "fixture-model", "max_tokens": 1}))
        with pytest.raises(RuntimeError, match="copied child"):
            await child
        assert await budget.get_reservations() == []
    finally:
        _budget_scope.reset(token)
        await item.client.aclose()
        await budget.close()


async def test_actual_retry_holds_unknown_first_and_settles_second(ledger, monkeypatch):
    budget = CostBudget(**{**ledger, "settings": {"cost": {"global_per_hour_usd": 4}}})
    count = 0

    async def fast_retry(factory, **kwargs):
        return await _retry_llm_call(factory, max_retries=2, delays=[0])

    def wire(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(503, json={"error": "server error"})
        return answer()

    monkeypatch.setattr("agents.llm_provider._retry_llm_call", fast_retry)
    item = provider(budget, wire)
    try:
        assert not (await item.chat([{"role": "user", "content": "inert"}], max_tokens=1000)).get("error")
        rows = await budget.get_reservations()
        assert count == 2 and {row["status"] for row in rows} == {"settled", "outcome_unknown"}
    finally:
        await item.client.aclose()
        await budget.close()


async def test_actual_retry_requires_fresh_reservation_before_second_wire(ledger, monkeypatch):
    budget = CostBudget(**ledger)
    count = 0

    async def fast_retry(factory, **kwargs):
        return await _retry_llm_call(factory, max_retries=2, delays=[0])

    def wire(request):
        nonlocal count
        count += 1
        return httpx.Response(503, json={"error": "server error"})

    monkeypatch.setattr("agents.llm_provider._retry_llm_call", fast_retry)
    item = provider(budget, wire)
    try:
        assert (await item.chat([{"role": "user", "content": "inert"}], max_tokens=1000)).get("error")
        assert count == 1 and (await budget.get_reservations())[0]["status"] == "outcome_unknown"
    finally:
        await item.client.aclose()
        await budget.close()


async def test_insert_failure_dispatches_zero_actual_requests(ledger):
    budget = CostBudget(**ledger)
    await budget.ensure_ready()
    await budget._conn.execute("CREATE TRIGGER reject_hold BEFORE INSERT ON cost_reservations BEGIN SELECT RAISE(ABORT, 'inert'); END")
    await budget._conn.commit()
    wire = AsyncMock(return_value=answer())
    item = provider(budget, wire)
    try:
        assert (await item.chat([{"role": "user", "content": "inert"}], max_tokens=1000)).get("error")
        wire.assert_not_called()
        assert await budget.get_reservations() == []
    finally:
        await item.client.aclose()
        await budget.close()


@pytest.mark.parametrize("name,model,streaming", [
    ("openai", "fixture-model", True),
    ("openai", "gpt-5.6-sol", False),
    ("openai", "gpt-5.6-sol", True),
    ("anthropic", "claude-sonnet-4-6", False),
    ("anthropic", "claude-sonnet-4-6", True),
    ("codex", "fixture-model", False),
    ("codex", "fixture-model", True),
])
async def test_actual_public_endpoint_routes_settle_one_attempt(ledger, monkeypatch, name, model, streaming):
    catalog = ledger["pricing"].catalog_path
    data = json.loads(catalog.read_text())
    data["providers"]["fixture"]["pricing"][model] = {"input": .5, "output": 1}
    catalog.write_text(json.dumps(data))
    # Thinking-capable Anthropic shaping can raise the output ceiling. The
    # admission must reserve that actual bound, not the smaller caller value.
    budget = CostBudget(**{**ledger, "settings": {"cost": {"global_per_hour_usd": 100}}})
    requests = []

    def wire(request):
        requests.append(request)
        if streaming:
            if name == "anthropic":
                events = [
                    {"type": "message_start", "message": {"model": model, "usage": {"input_tokens": 10, "output_tokens": 0}}},
                    {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "inert"}},
                    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 100}},
                    {"type": "message_stop"},
                ]
            elif "responses" in request.url.path:
                events = [{"type": "response.output_text.delta", "delta": "inert"},
                          {"type": "response.completed", "response": {"model": model,
                           "usage": {"input_tokens": 10, "output_tokens": 100}}}]
            else:
                events = [{"choices": [{"delta": {"content": "inert"}}]},
                          {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 100}}]
            text = "\n\n".join("data: " + json.dumps(event) for event in events)
            return httpx.Response(200, text=text + "\n\ndata: [DONE]\n\n")
        if name == "anthropic":
            return httpx.Response(200, json={"content": [{"type": "text", "text": "inert"}],
                                           "stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 100}})
        return httpx.Response(200, json={"status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "inert"}]}],
            "usage": {"input_tokens": 10, "output_tokens": 100}})

    item = provider(budget, wire, name=name, model=model)
    actual_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(wire)
        return actual_client(*args, **kwargs)

    if name == "anthropic" and streaming:
        monkeypatch.setattr("agents.llm_provider.httpx.AsyncClient", fake_client)
    if name == "codex":
        async def codex_chat(*args, **kwargs):
            requests.append("inert adapter chat")
            return SimpleNamespace(text="inert", tool_calls=[], finish_reason="stop", model=model,
                                   usage={"prompt_tokens": 10, "completion_tokens": 100})

        async def codex_stream(*args, **kwargs):
            requests.append("inert adapter stream")
            yield {"type": "text_delta", "content": "inert"}
            yield {"type": "done", "usage": {"prompt_tokens": 10, "completion_tokens": 100}}

        item._codex_adapter = SimpleNamespace(chat=codex_chat, stream_events=codex_stream)
    try:
        messages = [{"role": "user", "content": "inert"}]
        if streaming:
            events = [event async for event in item.chat_stream(messages, max_tokens=1000)]
            assert events[-1]["type"] == "done"
        else:
            assert not (await item.chat(messages, max_tokens=1000)).get("error")
        rows = await budget.get_reservations()
        assert len(requests) == len(rows) == 1
        assert rows[0]["model"] == model and rows[0]["status"] == "settled"
        assert budget.current_spend() == pytest.approx(.105)
    finally:
        await item.client.aclose()
        await budget.close()


async def test_actual_stream_close_before_terminal_keeps_partial_hold(ledger):
    budget = CostBudget(**ledger)
    lines = [{"choices": [{"delta": {"content": "inert"}}]},
             {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 0}}]
    item = provider(budget, lambda request: httpx.Response(200,
                    text="\n\n".join("data: " + json.dumps(line) for line in lines) + "\n\n"))
    try:
        events = [event async for event in item.chat_stream([{"role": "user", "content": "inert"}], max_tokens=1000)]
        assert events[-1]["type"] == "text_delta"
        assert (await budget.get_reservations())[0]["status"] == "outcome_unknown"
        assert budget.current_spend() == pytest.approx(.005)
    finally:
        await item.client.aclose()
        await budget.close()


@pytest.mark.parametrize("reported,settled", [("gpt-5.6-sol-2026-10-01", True),
                                              ("fixture-fallback", False)])
async def test_public_responses_dated_alias_preserves_exact_attempt(ledger, reported, settled):
    catalog = ledger["pricing"].catalog_path
    data = json.loads(catalog.read_text())
    data["providers"]["fixture"]["pricing"]["gpt-5.6-sol"] = {"input": .5, "output": 1}
    catalog.write_text(json.dumps(data))
    budget = CostBudget(**ledger)
    events = [{"type": "response.output_text.delta", "delta": "inert"},
              {"type": "response.completed", "response": {"model": reported,
               "usage": {"input_tokens": 10, "output_tokens": 100}}}]
    item = provider(budget, lambda request: httpx.Response(200, text="\n\n".join(
        "data: " + json.dumps(event) for event in events) + "\n\ndata: [DONE]\n\n"),
        model="gpt-5.6-sol")
    try:
        results = [event async for event in item.chat_stream(
            [{"role": "user", "content": "inert"}], max_tokens=1000)]
        assert results[-1]["type"] == "done"
        row = (await budget.get_reservations())[0]
        assert row["model"] == "gpt-5.6-sol"
        assert row["status"] == ("settled" if settled else "outcome_unknown")
        assert budget.current_spend() == pytest.approx(.105 if settled else .0525)
    finally:
        await item.client.aclose()
        await budget.close()


@pytest.mark.parametrize("name", ["openai", "anthropic", "codex"])
async def test_public_terminal_marker_without_output_counter_keeps_hold(ledger, monkeypatch, name):
    model = "gpt-5.6-sol" if name == "openai" else "fixture-model"
    catalog = ledger["pricing"].catalog_path
    data = json.loads(catalog.read_text())
    data["providers"]["fixture"]["pricing"][model] = {"input": .5, "output": 1}
    catalog.write_text(json.dumps(data))
    budget = CostBudget(**ledger)
    events = ([{"type": "response.output_text.delta", "delta": "inert"},
               {"type": "response.completed", "response": {"model": model, "usage": {"input_tokens": 10}}}]
              if name == "openai" else
              [{"type": "message_start", "message": {"model": model, "usage": {"input_tokens": 10}}},
               {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "inert"}},
               {"type": "message_stop"}])
    actual_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="\n\n".join(
        "data: " + json.dumps(event) for event in events) + "\n\n"))
    item = provider(budget, transport.handler, name=name, model=model)
    if name == "anthropic":
        def fake_client(*args, **kwargs):
            return actual_client(*args, **{**kwargs, "transport": transport})
        monkeypatch.setattr("agents.llm_provider.httpx.AsyncClient", fake_client)
    elif name == "codex":
        async def codex_stream(*args, **kwargs):
            yield {"type": "text_delta", "content": "inert"}
            yield {"type": "done", "usage": {"prompt_tokens": 10}}
        item._codex_adapter = SimpleNamespace(stream_events=codex_stream)
    try:
        results = [event async for event in item.chat_stream(
            [{"role": "user", "content": "inert"}], max_tokens=1000)]
        assert results[-1]["type"] == "done"
        row = (await budget.get_reservations())[0]
        assert row["status"] == "outcome_unknown" and row["actual_dollars"] == pytest.approx(.005)
        if name != "codex":
            assert "output_tokens" not in results[-1]["usage"]
            assert "total_tokens" not in results[-1]["usage"]
        assert budget.current_spend() == pytest.approx(.005)
    finally:
        await item.client.aclose()
        await budget.close()


async def test_actual_stream_consumer_close_retains_hold_without_context_leak(ledger):
    budget = CostBudget(**ledger)
    item = provider(budget, lambda request: httpx.Response(200,
                    text='data: {"choices":[{"delta":{"content":"inert"}}]}\n\ndata: [DONE]\n\n'))
    stream = item.chat_stream([{"role": "user", "content": "inert"}], max_tokens=1000)
    try:
        event = await stream.__anext__()
        assert event["type"] == "text_delta" and _budget_scope.get() is None
        await stream.aclose()
        assert (await budget.get_reservations())[0]["status"] == "outcome_unknown"
        assert _budget_scope.get() is None
    finally:
        await stream.aclose()
        await item.client.aclose()
        await budget.close()


async def test_actual_failover_reserves_answering_model_separately(ledger, monkeypatch):
    budget = CostBudget(**{**ledger, "settings": {"cost": {"global_per_hour_usd": 4}}})
    requests = []

    def wire(request):
        body = json.loads(request.content)
        requests.append(body["model"])
        if body["model"] == "fixture-model":
            return httpx.Response(401, json={"error": "inert key refused"})
        return answer()

    item = provider(budget, wire, config={"fallback_providers": ["groq"]})
    item._build_candidate_list = lambda: [
        ("openai", {"base_url": item.base_url, "api_key": "inert", "model": "fixture-model", "supported": True}),
        ("groq", {"base_url": "https://fallback.invalid/v1", "api_key": "inert", "model": "fixture-fallback", "supported": True}),
    ]
    actual_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(wire)
        return actual_client(*args, **kwargs)

    monkeypatch.setattr("agents.llm_provider.httpx.AsyncClient", fake_client)
    try:
        result = await item.chat([{"role": "user", "content": "inert"}], max_tokens=1000, call_site="learner")
        assert not result.get("error") and requests == ["fixture-model", "fixture-fallback"]
        rows = await budget.get_reservations()
        assert len(rows) == 2 and len({row["reservation_id"] for row in rows}) == 2
        statuses = {row["model"]: row["status"] for row in rows}
        assert statuses == {"fixture-model": "outcome_unknown", "fixture-fallback": "settled"}
        assert {row["call_site"] for row in rows} == {"learner"}
        assert budget.current_spend() == pytest.approx(.0525)
    finally:
        await item.client.aclose()
        await budget.close()
