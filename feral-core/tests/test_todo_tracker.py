"""Agent-facing todo tracker (``feral_workflows__todo_write``).

Design note (see the manifest description): this is deliberately an endpoint
on ``feral_workflows`` rather than a sixth top-level list concept. FERAL
already ships ``feral_reminders`` (user-facing, time-anchored),
``feral_routines`` (recurring schedule), ``feral_workflows`` (ordered steps
the brain executes) and ``background_task`` (long-horizon autonomous runs),
plus a dormant ``CodingRun`` planner. Landing the todo list next to
``feral_workflows`` costs one tool instead of two plus a catalog entry, and
puts it beside the concept it is most confused with so the two descriptions
can disambiguate each other.

The semantics under test:

* full-list replacement, never incremental patches, so the model's view and
  the store cannot drift;
* at most one ``in_progress`` item, server-enforced, which is what makes it
  a focus mechanism rather than a wish list;
* a bounded echo that CANNOT be truncated by the result budget, because a
  model that rewrites its list from a truncated view silently loses items.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from models.skill_manifest import SkillManifest  # noqa: E402
from skills.impl.feral_workflows import FeralWorkflowsSkill  # noqa: E402
from skills.impl.todo_store import (  # noqa: E402
    MAX_TODO_ITEMS,
    TodoStore,
    TodoValidationError,
)
from skills.registry import SkillRegistry  # noqa: E402
from skills.result_budget import budget_for_tool, reset_budget_cache  # noqa: E402


MANIFEST_PATH = ROOT / "skills" / "manifests" / "feral_workflows.json"


@pytest.fixture
def store(tmp_path, monkeypatch) -> TodoStore:
    # Never touch the operator's real ~/.feral from a test run.
    monkeypatch.setenv("FERAL_HOME", str(tmp_path / "feral_home"))
    return TodoStore(mirror_path=tmp_path / "todos.json")


@pytest.fixture
def skill(store, monkeypatch) -> FeralWorkflowsSkill:
    import skills.impl.feral_workflows as mod
    monkeypatch.setattr(mod, "_TODO_STORE_OVERRIDE", store, raising=False)
    return FeralWorkflowsSkill()


async def _write(skill, todos, session_id="s1"):
    return await skill.execute(
        "todo_write", {"todos": todos, "session_id": session_id}, {},
    )


# ── store semantics ───────────────────────────────────────────────────


class TestTodoStore:
    def test_replace_is_wholesale(self, store):
        store.replace("s1", [
            {"id": "1", "content": "a", "status": "pending"},
            {"id": "2", "content": "b", "status": "pending"},
        ])
        store.replace("s1", [{"id": "3", "content": "c", "status": "pending"}])
        assert [t["id"] for t in store.get("s1")] == ["3"]

    def test_rejects_two_in_progress(self, store):
        with pytest.raises(TodoValidationError) as exc:
            store.replace("s1", [
                {"id": "1", "content": "a", "status": "in_progress"},
                {"id": "2", "content": "b", "status": "in_progress"},
            ])
        assert exc.value.error_code == "multiple_in_progress"
        assert store.get("s1") == []

    def test_allows_exactly_one_in_progress(self, store):
        store.replace("s1", [
            {"id": "1", "content": "a", "status": "in_progress"},
            {"id": "2", "content": "b", "status": "pending"},
            {"id": "3", "content": "c", "status": "completed"},
        ])
        assert [t["status"] for t in store.get("s1")] == [
            "in_progress", "pending", "completed",
        ]

    def test_rejects_unknown_status(self, store):
        with pytest.raises(TodoValidationError) as exc:
            store.replace("s1", [{"id": "1", "content": "a", "status": "blocked"}])
        assert exc.value.error_code == "invalid_status"

    def test_rejects_over_cap(self, store):
        rows = [
            {"id": str(i), "content": f"t{i}", "status": "pending"}
            for i in range(MAX_TODO_ITEMS + 1)
        ]
        with pytest.raises(TodoValidationError) as exc:
            store.replace("s1", rows)
        assert exc.value.error_code == "too_many_todos"

    def test_sessions_are_isolated(self, store):
        store.replace("s1", [{"id": "1", "content": "a", "status": "pending"}])
        store.replace("s2", [{"id": "2", "content": "b", "status": "pending"}])
        assert [t["id"] for t in store.get("s1")] == ["1"]
        assert [t["id"] for t in store.get("s2")] == ["2"]

    def test_durable_mirror_survives_restart(self, tmp_path):
        mirror = tmp_path / "todos.json"
        first = TodoStore(mirror_path=mirror)
        first.replace("s1", [{"id": "1", "content": "a", "status": "in_progress"}])
        assert mirror.exists()

        second = TodoStore(mirror_path=mirror)
        assert [t["content"] for t in second.get("s1")] == ["a"]

    def test_mirror_is_json_not_sqlite(self, tmp_path):
        mirror = tmp_path / "todos.json"
        TodoStore(mirror_path=mirror).replace(
            "s1", [{"id": "1", "content": "a", "status": "pending"}],
        )
        payload = json.loads(mirror.read_text())
        assert "s1" in payload["sessions"]

    def test_corrupt_mirror_does_not_crash_boot(self, tmp_path):
        mirror = tmp_path / "todos.json"
        mirror.write_text("{not json")
        assert TodoStore(mirror_path=mirror).get("s1") == []


# ── skill endpoint ────────────────────────────────────────────────────


class TestTodoWriteEndpoint:
    @pytest.mark.asyncio
    async def test_write_echoes_the_full_list(self, skill):
        out = await _write(skill, [
            {"id": "1", "content": "research", "status": "in_progress"},
            {"id": "2", "content": "write", "status": "pending"},
        ])
        assert out["success"] is True
        assert [t["content"] for t in out["data"]["todos"]] == ["research", "write"]
        assert out["data"]["counts"] == {
            "pending": 1, "in_progress": 1, "completed": 0, "total": 2,
        }

    @pytest.mark.asyncio
    async def test_two_in_progress_is_a_structured_refusal(self, skill):
        out = await _write(skill, [
            {"id": "1", "content": "a", "status": "in_progress"},
            {"id": "2", "content": "b", "status": "in_progress"},
        ])
        assert out["success"] is False
        assert out["error_code"] == "multiple_in_progress"

    @pytest.mark.asyncio
    async def test_missing_todos_is_rejected(self, skill):
        out = await skill.execute("todo_write", {"session_id": "s1"}, {})
        assert out["success"] is False
        assert out["error_code"] == "missing_required_field"

    @pytest.mark.asyncio
    async def test_ids_are_backfilled_when_omitted(self, skill):
        out = await _write(skill, [{"content": "a", "status": "pending"}])
        assert out["data"]["todos"][0]["id"]

    @pytest.mark.asyncio
    async def test_status_defaults_to_pending(self, skill):
        out = await _write(skill, [{"content": "a"}])
        assert out["data"]["todos"][0]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_write_does_not_need_the_taskflow_runtime(self, skill):
        """Every other feral_workflows endpoint 503s without a TaskFlow
        runtime. The todo list is agent-local scratch state and must not
        inherit that dependency."""
        out = await _write(skill, [{"content": "a"}])
        assert out["success"] is True


# ── manifest / budget contract ────────────────────────────────────────


class TestManifestContract:
    @pytest.fixture(autouse=True)
    def _reset(self):
        reset_budget_cache()
        yield
        reset_budget_cache()

    def test_endpoint_is_declared_in_the_shipped_manifest(self):
        data = json.loads(MANIFEST_PATH.read_text())
        ids = [e["id"] for e in data["endpoints"]]
        assert "todo_write" in ids

    def test_every_param_is_declared(self):
        """An undeclared manifest param is silently discarded by the
        dispatch validator, so the endpoint would receive {}."""
        data = json.loads(MANIFEST_PATH.read_text())
        ep = next(e for e in data["endpoints"] if e["id"] == "todo_write")
        assert {p["name"] for p in ep["params"]} == {"todos", "session_id"}

    def test_declares_a_wider_result_budget_than_standard(self):
        """``feral_workflows.json`` declares no skill-level budget, so it
        inherits ``standard`` at 20 list items. A 25-item list would be
        truncated in the echo and the model would then rewrite its list
        from a truncated view and silently lose items."""
        reg = SkillRegistry()
        reg.load_from_file(MANIFEST_PATH)
        budget = budget_for_tool("feral_workflows__todo_write", reg)
        assert budget.name != "standard"
        assert budget.max_list_len > MAX_TODO_ITEMS

    def test_sibling_endpoints_keep_the_standard_budget(self):
        reg = SkillRegistry()
        reg.load_from_file(MANIFEST_PATH)
        assert budget_for_tool("feral_workflows__create", reg).name == "standard"

    def test_a_full_list_round_trips_the_budget_without_truncation(self, store):
        """The real failure mode, end to end: serialize a max-size list
        through the budget and confirm every item survives."""
        from skills.result_budget import serialize_tool_result

        reg = SkillRegistry()
        reg.load_from_file(MANIFEST_PATH)
        rows = [
            {"id": str(i), "content": f"todo item number {i}", "status": "pending"}
            for i in range(MAX_TODO_ITEMS)
        ]
        store.replace("s1", rows)
        result = {"success": True, "data": {"todos": store.get("s1")}}
        text = serialize_tool_result(
            "feral_workflows__todo_write", result, registry=reg,
        )
        for i in range(MAX_TODO_ITEMS):
            assert f"todo item number {i}" in text

    def test_endpoint_is_plan_safe(self):
        """Tracking your own todos is agent-local scratch state, so it has
        to survive plan mode's filter or a planning agent cannot keep a
        list while it researches."""
        from agents.plan_mode import is_plan_safe_tool

        reg = SkillRegistry()
        reg.load_from_file(MANIFEST_PATH)
        assert is_plan_safe_tool("feral_workflows__todo_write", registry=reg) is True

    def test_impl_dispatches_the_endpoint(self):
        """The dispatch validator reads the backend's `execute` source. An
        endpoint declared in the manifest but absent from that dispatch
        table is rejected as `unknown_endpoint` at runtime."""
        from agents.tool_dispatch_validator import ToolDispatchValidator
        import skills.impl  # noqa: F401 - register python backings

        violations = ToolDispatchValidator().contract_violations(
            "feral_workflows", "todo_write",
        )
        assert violations == []

    def test_manifest_is_free_of_em_dashes(self):
        """Hard project rule, checked after the JSON round-trip too: a
        previous lane found em dashes stored as escapes where a raw byte
        scan read clean."""
        raw = MANIFEST_PATH.read_text()
        assert "—" not in raw
        assert "\\u2014" not in raw
        assert "—" not in json.dumps(json.loads(raw))
        manifest = SkillManifest(**json.loads(raw))
        assert "—" not in manifest.model_dump_json()


# ── UI frame ──────────────────────────────────────────────────────────


class TestTodoUpdateFrame:
    """The panel is fed by a dedicated frame, not by the tool card.

    The client suppresses the ToolCallCard for this tool name because the
    model rewrites the list constantly; without the frame the panel would
    have nothing to render.
    """

    @pytest.fixture
    def orchestrator(self):
        from agents.orchestrator import Orchestrator

        reg = SkillRegistry()
        reg.load_from_file(MANIFEST_PATH)
        return Orchestrator(
            skill_registry=reg,
            send_to_client=AsyncMock(),
            daemons={},
            memory=None,
            vision_buffer=None,
            perception=None,
            learner=None,
        )

    @pytest.mark.asyncio
    async def test_emits_todo_update(self, orchestrator):
        rows = [{"id": "1", "content": "a", "status": "in_progress"}]
        await orchestrator._maybe_emit_todo_frame(
            "s1",
            {"name": "feral_workflows__todo_write"},
            {"success": True, "data": {"todos": rows, "counts": {"total": 1}}},
        )
        sent = [c.args[1] for c in orchestrator.send.await_args_list]
        frames = [m for m in sent if m.type == "todo_update"]
        assert len(frames) == 1
        assert frames[0].payload["todos"] == rows

    @pytest.mark.asyncio
    async def test_silent_for_other_tools(self, orchestrator):
        await orchestrator._maybe_emit_todo_frame(
            "s1",
            {"name": "feral_workflows__create"},
            {"success": True, "data": {"todos": []}},
        )
        orchestrator.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_silent_on_a_failed_write(self, orchestrator):
        await orchestrator._maybe_emit_todo_frame(
            "s1",
            {"name": "feral_workflows__todo_write"},
            {"success": False, "error_code": "multiple_in_progress"},
        )
        orchestrator.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tolerates_a_malformed_payload(self, orchestrator):
        await orchestrator._maybe_emit_todo_frame(
            "s1", {"name": "feral_workflows__todo_write"}, {"success": True},
        )
        orchestrator.send.assert_not_awaited()
