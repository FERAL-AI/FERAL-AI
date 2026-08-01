"""Plan mode: a per-session, ephemeral, non-mutating agent posture.

Plan mode is a SEPARATE AXIS from ``security.autonomy_mode``. Autonomy mode
is persisted and global; plan mode is per-session and dies with the session.
Nothing here may change the autonomy mode, and entering or leaving plan mode
must never hand out standing tool approvals.

The two enforcement points are tested independently because they fail
independently:

1. Exposure (orchestrator tool-list filter) is ADVISORY. The model can emit a
   tool name it was never given, and the voice surface builds its own list
   from ``get_all_tools()`` on a path the orchestrator filter never touches.
2. Dispatch (``ToolRunner``) is the real gate.

What these tests do NOT cover: the OpenAI Realtime and Gemini Live proxies
call ``SkillExecutor.execute`` directly rather than going through
``ToolRunner``, so the dispatch gate never runs for them.
``test_catches_a_tool_the_voice_path_would_have_exposed`` drives
``execute_tool_call_for_llm`` with ``surface="voice"``, which is the shape of
the CHAINED voice path (it routes through ``handle_command_stream``), not the
realtime one. Do not read a green run here as proof that plan mode holds on a
live realtime session; it does not.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.plan_mode import (  # noqa: E402
    PLAN_REFUSAL_CODE,
    PlanModeState,
    filter_skills_for_plan_mode,
    filter_tools_for_plan_mode,
    is_plan_safe_tool,
)
from agents.orchestrator import Orchestrator  # noqa: E402
from agents.self_model import build_tooling_catalog  # noqa: E402
from agents.tool_runner import ToolRunner  # noqa: E402
from models.skill_manifest import (  # noqa: E402
    AuthConfig,
    BrandProfile,
    SkillEndpoint,
    SkillManifest,
)
from security.safety_resolver import is_read_only  # noqa: E402
from skills.registry import SkillRegistry  # noqa: E402


# ── fixtures ──────────────────────────────────────────────────────────


def _manifest(skill_id: str, endpoints: list[SkillEndpoint]) -> SkillManifest:
    return SkillManifest(
        skill_id=skill_id,
        version="1.0.0",
        author="test",
        brand=BrandProfile(name=skill_id),
        description=f"{skill_id} test manifest",
        auth=AuthConfig(type="none"),
        endpoints=endpoints,
    )


@pytest.fixture
def registry() -> SkillRegistry:
    """A registry holding one skill with the three interesting shapes:

    * an explicitly read-only endpoint,
    * an explicitly mutating endpoint,
    * an UNANNOTATED endpoint whose name the legacy substring list admits.
    """
    # HTTP-lane urls on purpose: the dispatch validator demands a Python
    # backing implementation for `python://` endpoints, and this fixture is
    # about safety metadata, not about backends.
    reg = SkillRegistry()
    reg.register(_manifest("demo", [
        SkillEndpoint(
            id="read_file", method="GET", url="https://demo.test/read_file",
            description="read", read_only_hint=True, safety_tier="safe",
        ),
        SkillEndpoint(
            id="edit_file", method="POST", url="https://demo.test/edit_file",
            description="edit", safety_tier="confirm",
        ),
        # No safety metadata at all. "status" is in the legacy AUTO/read-only
        # substring list, so the non-strict resolver calls this read-only.
        SkillEndpoint(
            id="set_status", method="POST", url="https://demo.test/set_status",
            description="mutating, but the name contains 'status'",
        ),
    ]))
    return reg


# ── 1. strict read-only resolution ────────────────────────────────────


class TestStrictReadOnly:
    def test_legacy_mode_still_admits_substring_matches(self, registry):
        """Unchanged default: the substring fallback still applies.

        This is the behaviour strict mode has to opt OUT of, so pin it.
        """
        assert is_read_only("demo__set_status", registry=registry) is True

    def test_strict_mode_rejects_unannotated_endpoint(self, registry):
        """Absence of metadata means NOT plan-safe. That is the whole
        contract of a mode called 'cannot mutate'."""
        assert is_read_only("demo__set_status", registry=registry, strict=True) is False

    def test_strict_mode_accepts_read_only_hint(self, registry):
        assert is_read_only("demo__read_file", registry=registry, strict=True) is True

    def test_strict_mode_rejects_confirm_tier(self, registry):
        assert is_read_only("demo__edit_file", registry=registry, strict=True) is False

    def test_strict_mode_rejects_unknown_skill(self, registry):
        """No registry entry -> no metadata -> not plan-safe, fail closed."""
        assert is_read_only("nosuch__thing", registry=registry, strict=True) is False
        assert is_read_only("nosuch__thing", registry=None, strict=True) is False

    def test_strict_mode_honours_requires_user_approval_override(self):
        """A contradictory manifest (safe tier + explicit approval demand)
        resolves to NOT plan-safe."""
        reg = SkillRegistry()
        reg.register(_manifest("weird", [
            SkillEndpoint(
                id="peek", method="GET", url="https://weird.test/peek",
                description="x", safety_tier="safe", requires_user_approval=True,
            ),
        ]))
        assert is_read_only("weird__peek", registry=reg, strict=True) is False

    def test_shipped_manifests_leak_mutating_tools_under_the_legacy_list(self):
        """Regression evidence, not a hypothetical.

        These three endpoints ship in this repo, carry no safety metadata,
        and are admitted by the legacy substring list purely because their
        names contain 'read' (inside "thread"), 'status', and 'list'.
        Every one of them mutates remote state.
        """
        reg = SkillRegistry()
        reg.load_from_directory(ROOT / "skills" / "manifests")
        leaky = [
            "messaging_sms__slack_reply_to_thread",
            "messaging_sms__slack_set_status",
            "spotify_music__play_playlist",
        ]
        for name in leaky:
            assert is_read_only(name, registry=reg) is True, name
            assert is_plan_safe_tool(name, registry=reg) is False, name


# ── 2. plan-safe classification + exposure filter ─────────────────────


class TestExposureFilter:
    def test_filter_tools_keeps_only_plan_safe(self, registry):
        tools = registry.get_tools_for_skills([registry.skills["demo"]])
        assert len(tools) == 3
        kept = filter_tools_for_plan_mode(tools, registry=registry)
        names = {t["function"]["name"] for t in kept}
        assert names == {"demo__read_file"}

    def test_filter_tools_drops_mcp_tools(self, registry):
        """MCP tools carry no FERAL manifest metadata, so they fail closed."""
        tools = [{"type": "function", "function": {"name": "mcp_fs__write"}}]
        assert filter_tools_for_plan_mode(tools, registry=registry) == []

    def test_subagent_spawn_is_never_plan_safe(self, registry):
        assert is_plan_safe_tool("subagent__spawn_subagent", registry=registry) is False

    def test_plan_submit_is_plan_safe_even_without_a_registry(self):
        """The plan skill has to survive its own filter, including on the
        mock-registry paths used by tests and by early boot."""
        assert is_plan_safe_tool("plan__submit", registry=None) is True

    def test_filter_skills_prunes_endpoints_not_whole_skills(self, registry):
        """``build_tooling_catalog`` enumerates endpoints straight off the
        manifest, so the prompt view must be pruned at ENDPOINT granularity
        or the model is told ``edit_file`` is active and then refused."""
        pruned = filter_skills_for_plan_mode(
            [registry.skills["demo"]], registry=registry,
        )
        assert [s.skill_id for s in pruned] == ["demo"]
        assert [e.id for e in pruned[0].endpoints] == ["read_file"]

    def test_filter_skills_drops_skills_with_no_plan_safe_endpoints(self):
        reg = SkillRegistry()
        reg.register(_manifest("allbad", [
            SkillEndpoint(
                id="nuke", method="POST", url="https://allbad.test/nuke",
                description="x", safety_tier="confirm",
            ),
        ]))
        assert filter_skills_for_plan_mode(
            [reg.skills["allbad"]], registry=reg,
        ) == []

    def test_tooling_catalog_does_not_advertise_filtered_endpoints(self, registry):
        """The prompt-side trap: a filtered tools array is not enough,
        ``build_tooling_catalog`` reads the manifest independently."""
        skills = [registry.skills["demo"]]
        unfiltered = build_tooling_catalog(skills, skills)
        assert "demo__edit_file" in unfiltered

        pruned = filter_skills_for_plan_mode(skills, registry=registry)
        catalog = build_tooling_catalog(pruned, pruned)
        active_block = catalog.split("### Available (full catalog)")[0]
        assert "demo__read_file" in active_block
        assert "demo__edit_file" not in active_block


# ── 3. session state ──────────────────────────────────────────────────


class TestPlanModeState:
    def test_enter_and_exit(self):
        st = PlanModeState()
        assert st.is_active("s1") is False
        st.enter("s1", reason="research")
        assert st.is_active("s1") is True
        st.exit("s1")
        assert st.is_active("s1") is False

    def test_is_per_session_not_global(self):
        st = PlanModeState()
        st.enter("s1")
        assert st.is_active("s2") is False

    def test_subagent_child_sessions_inherit(self):
        """``ToolRunner._run_subagent_task`` mints
        ``{parent}:sub:{n}:{rand}``. Without ancestry the child session is
        not in the plan-mode set and plan mode leaks wholesale."""
        st = PlanModeState()
        st.enter("parent-1")
        assert st.is_active("parent-1:sub:0:ab12cd") is True

    def test_clear_session_drops_state(self):
        st = PlanModeState()
        st.enter("s1")
        st.record_plan("s1", {"summary": "x", "steps": []})
        st.clear_session("s1")
        assert st.is_active("s1") is False
        assert st.latest_plan("s1") is None

    def test_model_cannot_exit_plan_mode(self):
        """Only ``actor='user'`` may leave. A model-attributed exit is a
        no-op, which is what stops "I'm done planning now" from being a
        privilege escalation."""
        st = PlanModeState()
        st.enter("s1")
        changed = st.exit("s1", actor="model")
        assert changed is False
        assert st.is_active("s1") is True
        assert st.exit("s1", actor="user") is True
        assert st.is_active("s1") is False

    def test_record_plan_returns_latest(self):
        st = PlanModeState()
        st.enter("s1")
        st.record_plan("s1", {"summary": "a", "steps": ["one"]})
        st.record_plan("s1", {"summary": "b", "steps": ["two"]})
        assert st.latest_plan("s1")["summary"] == "b"

    def test_recording_a_plan_never_enters_plan_mode(self):
        """The `plan` skill is routable on its trigger phrases like any
        other, so the model can call `plan__submit` outside plan mode. If
        that stored into the active set it would flip the session into
        plan mode as a side effect of a tool choice, which is exactly the
        heuristic entry this design rules out."""
        st = PlanModeState()
        st.record_plan("s1", {"summary": "a", "steps": ["one"]})
        assert st.is_active("s1") is False
        assert st.latest_plan("s1")["summary"] == "a"

    def test_exit_does_not_reactivate_via_a_stored_plan(self):
        st = PlanModeState()
        st.enter("s1")
        st.record_plan("s1", {"summary": "a", "steps": ["one"]})
        st.exit("s1", approved=True, actor="user")
        assert st.is_active("s1") is False
        # The plan survives so the UI can still show what was approved.
        assert st.latest_plan("s1")["summary"] == "a"


# ── 4b. the plan skill ────────────────────────────────────────────────


class TestPlanSkill:
    @pytest.fixture
    def skill(self):
        import skills.impl.plan as mod
        state = PlanModeState()
        mod.set_plan_state_override(state)
        yield mod.PlanSkill(), state
        mod.set_plan_state_override(None)

    @pytest.mark.asyncio
    async def test_submit_records_the_plan(self, skill):
        plan_skill, state = skill
        state.enter("s1")
        out = await plan_skill.execute("submit", {
            "summary": "refactor the resolver",
            "steps": [{"title": "read", "tools": ["coding_tools__read_file"]}, "then edit"],
            "session_id": "s1",
        }, {})
        assert out["success"] is True
        assert out["data"]["plan"]["summary"] == "refactor the resolver"
        assert [s["title"] for s in out["data"]["plan"]["steps"]] == ["read", "then edit"]
        assert state.latest_plan("s1")["summary"] == "refactor the resolver"

    @pytest.mark.asyncio
    async def test_submit_does_not_leave_plan_mode(self, skill):
        plan_skill, state = skill
        state.enter("s1")
        out = await plan_skill.execute(
            "submit", {"summary": "x", "steps": ["a"], "session_id": "s1"}, {},
        )
        assert state.is_active("s1") is True
        assert out["data"]["plan_mode"] is True
        assert out["data"]["awaiting"] == "user_review"

    @pytest.mark.asyncio
    async def test_summary_and_steps_are_required(self, skill):
        plan_skill, _ = skill
        assert (await plan_skill.execute("submit", {"steps": ["a"]}, {}))["error_code"] \
            == "missing_required_field"
        assert (await plan_skill.execute("submit", {"summary": "x", "steps": []}, {}))["error_code"] \
            == "missing_required_field"

    def test_manifest_survives_its_own_filter(self):
        reg = SkillRegistry()
        reg.load_from_file(ROOT / "skills" / "manifests" / "plan.json")
        assert is_plan_safe_tool("plan__submit", registry=reg) is True
        tools = reg.get_tools_for_skills([reg.skills["plan"]])
        assert len(filter_tools_for_plan_mode(tools, registry=reg)) == 1

    def test_manifest_is_free_of_em_dashes(self):
        import json
        raw = (ROOT / "skills" / "manifests" / "plan.json").read_text()
        assert "—" not in raw
        assert "\\u2014" not in raw
        assert "—" not in json.dumps(json.loads(raw), ensure_ascii=False)


# ── 4. dispatch gate (the one that matters) ───────────────────────────


def _tool_runner(registry) -> ToolRunner:
    orch = MagicMock()
    orch.skills = registry
    orch._mcp_client = None
    orch._send_text = AsyncMock()
    return ToolRunner(orch)


class TestDispatchGate:
    @pytest.mark.asyncio
    async def test_blocks_mutating_tool(self, registry):
        runner = _tool_runner(registry)
        runner.plan_mode.enter("s1")
        out = await runner.execute_tool_call_for_llm(
            "s1", {"name": "demo__edit_file", "args": {}, "id": "c1"}, [],
        )
        assert out["success"] is False
        assert out["error_code"] == PLAN_REFUSAL_CODE
        assert out["plan_mode"] is True

    @pytest.mark.asyncio
    async def test_allows_plan_safe_tool(self, registry):
        runner = _tool_runner(registry)
        runner.plan_mode.enter("s1")
        runner._orch.executor.execute = AsyncMock(
            return_value={"success": True, "data": {"ok": 1}},
        )
        out = await runner.execute_tool_call_for_llm(
            "s1", {"name": "demo__read_file", "args": {}, "id": "c1"}, [],
        )
        assert out["success"] is True

    @pytest.mark.asyncio
    async def test_catches_a_tool_the_voice_path_would_have_exposed(self, registry):
        """Voice assembles its tool list from ``get_all_tools()``, a path the
        orchestrator's exposure filter never touches. So take a tool that IS
        in the voice list, confirm the exposure filter would have removed it,
        and prove the dispatch gate still refuses it.
        """
        voice_tools = registry.get_all_tools()
        voice_names = {t["function"]["name"] for t in voice_tools}
        assert "demo__edit_file" in voice_names

        exposed = {
            t["function"]["name"]
            for t in filter_tools_for_plan_mode(voice_tools, registry=registry)
        }
        assert "demo__edit_file" not in exposed

        runner = _tool_runner(registry)
        runner.plan_mode.enter("voice-session")
        runner._orch.executor.execute = AsyncMock(
            return_value={"success": True, "data": {}},
        )
        out = await runner.execute_tool_call_for_llm(
            "voice-session",
            {"name": "demo__edit_file", "args": {}, "id": "c1"},
            [],
            surface="voice",
        )
        assert out["error_code"] == PLAN_REFUSAL_CODE
        runner._orch.executor.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_blocks_subagent_spawn(self, registry):
        runner = _tool_runner(registry)
        runner.plan_mode.enter("s1")
        runner.spawn_subagents = AsyncMock()
        out = await runner.execute_tool_call_for_llm(
            "s1",
            {"name": "subagent__spawn_subagent", "args": {"tasks": ["go"]}, "id": "c1"},
            [],
        )
        assert out["error_code"] == PLAN_REFUSAL_CODE
        runner.spawn_subagents.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_blocks_spawn_subagents_called_directly(self, registry):
        """Defence in depth: even bypassing the dispatch branch, the
        spawner itself refuses under plan mode."""
        runner = _tool_runner(registry)
        runner.plan_mode.enter("s1")
        out = await runner.spawn_subagents("s1", {"tasks": ["go"]})
        assert out["success"] is False
        assert out["error_code"] == PLAN_REFUSAL_CODE

    @pytest.mark.asyncio
    async def test_blocks_mcp_tool(self, registry):
        runner = _tool_runner(registry)
        mcp = MagicMock()
        mcp.call_tool = AsyncMock(return_value={"content": []})
        runner._orch._mcp_client = mcp
        runner.plan_mode.enter("s1")
        out = await runner.execute_tool_call_for_llm(
            "s1", {"name": "mcp_fs__write", "args": {}, "id": "c1"}, [],
        )
        assert out["error_code"] == PLAN_REFUSAL_CODE
        mcp.call_tool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_gate_is_inert_outside_plan_mode(self, registry):
        """Outside plan mode the call falls through to the ORDINARY
        autonomy flow, which on hybrid asks for approval for a
        confirm-tier tool. The point is that the refusal is the safety
        gate's, not plan mode's."""
        runner = _tool_runner(registry)
        runner._orch.executor.execute = AsyncMock(
            return_value={"success": True, "data": {}},
        )
        out = await runner.execute_tool_call_for_llm(
            "s1", {"name": "demo__edit_file", "args": {}, "id": "c1"}, [],
        )
        assert out.get("error_code") != PLAN_REFUSAL_CODE
        assert out["status"] == "pending_approval"

        runner.set_autonomy_mode("loose")
        out = await runner.execute_tool_call_for_llm(
            "s1", {"name": "demo__edit_file", "args": {}, "id": "c2"}, [],
        )
        assert out["success"] is True

    @pytest.mark.asyncio
    async def test_ui_event_dispatch_path_is_gated_too(self, registry):
        runner = _tool_runner(registry)
        runner.plan_mode.enter("s1")
        runner._orch.executor.execute = AsyncMock(
            return_value={"success": True, "data": {}},
        )
        await runner.execute_tool_call(
            "s1", {"name": "demo__edit_file", "args": {}, "id": "c1"}, [],
        )
        runner._orch.executor.execute.assert_not_awaited()


# ── 5. plan mode does not touch autonomy, and grants nothing ──────────


class TestIndependenceFromAutonomyMode:
    def test_entering_plan_mode_does_not_change_autonomy_mode(self, registry):
        runner = _tool_runner(registry)
        before = runner.autonomy_mode
        runner.plan_mode.enter("s1")
        assert runner.autonomy_mode == before
        runner.plan_mode.exit("s1")
        assert runner.autonomy_mode == before

    def test_plan_approval_grants_no_standing_tool_approval(self, registry):
        """Approving a plan must not be blanket approval for the following
        turns. Every mutating call still goes through the session's autonomy
        mode."""
        runner = _tool_runner(registry)
        runner.set_autonomy_mode("hybrid")
        runner.plan_mode.enter("s1")
        runner.plan_mode.record_plan("s1", {"summary": "do it", "steps": ["a"]})
        runner.plan_mode.exit("s1", approved=True, actor="user")

        denial = runner.enforce_safety("demo__edit_file", {}, session_id="s1")
        assert denial is not None
        assert denial["status"] == "pending_approval"


# ── 6. orchestrator wiring ────────────────────────────────────────────


@pytest.fixture
def orchestrator(registry) -> Orchestrator:
    return Orchestrator(
        skill_registry=registry,
        send_to_client=AsyncMock(),
        daemons={},
        memory=None,
        vision_buffer=None,
        perception=None,
        learner=None,
    )


class TestOrchestratorPlanMode:
    def test_plan_mode_is_shared_with_tool_runner(self, orchestrator):
        assert orchestrator.plan_mode is orchestrator.tool_runner.plan_mode

    @pytest.mark.asyncio
    async def test_slash_plan_enters(self, orchestrator):
        handled = await orchestrator._maybe_handle_plan_meta_command("s1", "/plan")
        assert handled is True
        assert orchestrator.plan_mode.is_active("s1") is True

    @pytest.mark.asyncio
    async def test_slash_plan_off_exits(self, orchestrator):
        await orchestrator._maybe_handle_plan_meta_command("s1", "/plan")
        handled = await orchestrator._maybe_handle_plan_meta_command("s1", "/plan off")
        assert handled is True
        assert orchestrator.plan_mode.is_active("s1") is False

    @pytest.mark.asyncio
    async def test_entry_is_never_heuristic(self, orchestrator):
        """Prose that merely talks about planning must not enter the mode."""
        for text in (
            "make a plan for the migration",
            "plan",
            "can you /plan this out for me",
            "/planner",
        ):
            handled = await orchestrator._maybe_handle_plan_meta_command("s1", text)
            assert handled is False, text
            assert orchestrator.plan_mode.is_active("s1") is False, text

    @pytest.mark.asyncio
    async def test_api_entry_points_exist(self, orchestrator):
        state = await orchestrator.enter_plan_mode("s1", reason="api")
        assert state["plan_mode"] is True
        assert orchestrator.plan_mode.is_active("s1") is True
        state = await orchestrator.exit_plan_mode("s1", approved=True)
        assert state["plan_mode"] is False

    def test_plan_is_not_in_always_include_skills(self):
        assert "plan" not in Orchestrator.ALWAYS_INCLUDE_SKILLS

    def test_exposure_filter_is_a_no_op_outside_plan_mode(self, orchestrator, registry):
        tools = registry.get_tools_for_skills([registry.skills["demo"]])
        skills = [registry.skills["demo"]]
        out_tools, out_skills = orchestrator._apply_plan_mode_filter("s1", tools, skills)
        assert out_tools == tools
        assert out_skills == skills

    def test_exposure_filter_narrows_and_adds_the_plan_tool(self, orchestrator, registry):
        registry.load_from_file(ROOT / "skills" / "manifests" / "plan.json")
        orchestrator.plan_mode.enter("s1")
        tools = registry.get_tools_for_skills([registry.skills["demo"]])
        out_tools, out_skills = orchestrator._apply_plan_mode_filter(
            "s1", tools, [registry.skills["demo"]],
        )
        names = {t["function"]["name"] for t in out_tools}
        assert names == {"demo__read_file", "plan__submit"}
        assert {s.skill_id for s in out_skills} == {"demo", "plan"}

    @pytest.mark.asyncio
    async def test_system_prompt_carries_the_plan_block_and_no_write_tools(
        self, orchestrator, registry,
    ):
        """The prompt-side trap end to end: the catalog must not advertise
        `edit_file` as active while the dispatch gate refuses it."""
        from perception.fusion import PerceptionFrame

        orchestrator.plan_mode.enter("s1")
        _tools, skills = orchestrator._apply_plan_mode_filter(
            "s1",
            registry.get_tools_for_skills([registry.skills["demo"]]),
            [registry.skills["demo"]],
        )
        prompt = await orchestrator._build_system_prompt(
            PerceptionFrame(), skills, "s1",
        )
        assert "## Plan Mode (ACTIVE)" in prompt
        assert "demo__read_file" in prompt
        assert "demo__edit_file" not in prompt

    @pytest.mark.asyncio
    async def test_system_prompt_is_unchanged_outside_plan_mode(
        self, orchestrator, registry,
    ):
        from perception.fusion import PerceptionFrame

        prompt = await orchestrator._build_system_prompt(
            PerceptionFrame(), [registry.skills["demo"]], "s1",
        )
        assert "## Plan Mode (ACTIVE)" not in prompt
        assert "demo__edit_file" in prompt


# ── 7. REST surface ───────────────────────────────────────────────────


class TestPlanModeRoutes:
    @pytest.fixture
    def client(self, orchestrator):
        from types import SimpleNamespace
        from unittest.mock import patch

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        fake_state = SimpleNamespace(orchestrator=orchestrator)
        with patch("api.routes.sessions.state", fake_state):
            from api.routes.sessions import router
            app = FastAPI()
            app.include_router(router)
            yield TestClient(app, raise_server_exceptions=False), orchestrator

    def test_post_enter_then_get(self, client):
        api, orch = client
        res = api.post("/api/sessions/s1/plan_mode", json={"enabled": True})
        assert res.status_code == 200
        assert res.json()["plan_mode"] is True
        assert orch.plan_mode.is_active("s1") is True
        assert api.get("/api/sessions/s1/plan_mode").json()["plan_mode"] is True

    def test_post_exit(self, client):
        api, orch = client
        api.post("/api/sessions/s1/plan_mode", json={"enabled": True})
        res = api.post(
            "/api/sessions/s1/plan_mode", json={"enabled": False, "approved": True},
        )
        assert res.status_code == 200
        assert res.json()["plan_mode"] is False
        assert orch.plan_mode.is_active("s1") is False

    def test_enabled_is_required(self, client):
        api, _ = client
        assert api.post("/api/sessions/s1/plan_mode", json={}).status_code == 400

    def test_route_does_not_touch_autonomy_mode(self, client):
        api, orch = client
        before = orch.tool_runner.autonomy_mode
        api.post("/api/sessions/s1/plan_mode", json={"enabled": True})
        api.post("/api/sessions/s1/plan_mode", json={"enabled": False, "approved": True})
        assert orch.tool_runner.autonomy_mode == before
