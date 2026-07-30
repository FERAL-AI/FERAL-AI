"""Tests for agents/tool_runner.py — the security-critical tool execution pipeline.

Covers safety classification, enforcement gates, anti-loop detection,
approval lifecycle, autonomy modes, and the execute_tool_call_for_llm flow.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agents.tool_runner import ToolRunner, SafetyLevel, READ_ONLY_PATTERNS
from security.exec_approvals import ApprovalManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_runner(autonomy_mode: str = "hybrid") -> ToolRunner:
    """Build a ToolRunner with a lightweight mock orchestrator and isolated DB."""
    orch = MagicMock()
    orch.daemons = {}
    orch.skills = MagicMock()
    orch.executor = AsyncMock()
    orch.llm = MagicMock()
    orch._mcp_client = None
    orch._send_text = AsyncMock()
    orch.send_permission_request = AsyncMock()
    orch._max_iterations = 8
    runner = ToolRunner(orch, autonomy_mode=autonomy_mode)
    runner._approval_mgr = ApprovalManager(db_path=":memory:")
    return runner


# ═══════════════════════════════════════════════════════════════════════════
# Safety Classification
# ═══════════════════════════════════════════════════════════════════════════

class TestClassifySafety:
    """Verify classify_safety returns the correct level for every category."""

    def setup_method(self):
        self.runner = _make_runner()

    # ── DENY ──────────────────────────────────────────────────────────────

    @pytest.mark.parametrize("tool_name", [
        "desktop_control__format_disk",
        "device__factory_reset",
        "system__erase_all_data",
        "chaos__self_destruct",
    ])
    def test_deny_blocks_dangerous_keywords(self, tool_name):
        assert self.runner.classify_safety(tool_name, {}) == SafetyLevel.DENY

    def test_deny_robot_move_excessive_speed(self):
        assert self.runner.classify_safety(
            "robot_ext__robot_move", {"direction": "fwd", "speed": 90}
        ) == SafetyLevel.DENY

    def test_deny_actuator_excessive_speed(self):
        assert self.runner.classify_safety(
            "arm__actuator_set", {"speed": 100}
        ) == SafetyLevel.DENY

    def test_robot_move_at_boundary_speed_not_denied(self):
        assert self.runner.classify_safety(
            "robot_ext__robot_move", {"speed": 80}
        ) != SafetyLevel.DENY

    # ── CONFIRM ───────────────────────────────────────────────────────────

    @pytest.mark.parametrize("tool_name", [
        "messaging_sms__send_sms",
        "github__post_comment",
        "project__create_issue",
        "notes_memory__delete_note",
        "settings__update_profile",
        "robot_ext__robot_move",
        "smart_home__lock_door",
        "spotify_music__play_song",
        "media__pause_video",
        "media__skip_track",
        "desktop_control__volume_set",
        "comms__message_user",
        "shop__order_item",
        "calendar__schedule_meeting",
        "daemon__restart_service",
        "code__execute_snippet",
        "arm__motor_speed",
        "gripper__grip_object",
    ])
    def test_confirm_for_impactful_tools(self, tool_name):
        assert self.runner.classify_safety(tool_name, {}) == SafetyLevel.CONFIRM

    def test_now_playing_is_confirm_due_to_play_substring(self):
        """'now_playing' contains 'play' — CONFIRM patterns are checked before AUTO,
        so the CONFIRM match wins even though 'now_playing' is in the AUTO list."""
        assert self.runner.classify_safety("spotify_music__now_playing", {}) == SafetyLevel.CONFIRM

    def test_list_playlists_is_confirm_due_to_play_substring(self):
        """'playlists' contains 'play' — CONFIRM match wins over 'list' AUTO match."""
        assert self.runner.classify_safety("spotify_music__list_playlists", {}) == SafetyLevel.CONFIRM

    # ── AUTO ──────────────────────────────────────────────────────────────

    @pytest.mark.parametrize("tool_name", [
        "web_search__web_search",
        "db__query_users",
        "github_api__get_repos",
        "files__list_directory",
        "sensor__current_temp",
        "weather__forecast_week",
        "smart_home__status_lights",
        "fs__read_file",
        "notes_memory__search",
    ])
    def test_auto_for_safe_readonly_tools(self, tool_name):
        assert self.runner.classify_safety(tool_name, {}) == SafetyLevel.AUTO

    def test_unknown_tool_defaults_to_confirm(self):
        assert self.runner.classify_safety("totally_unknown__something", {}) == SafetyLevel.CONFIRM

    # ── Pattern ordering: CONFIRM must be checked BEFORE AUTO ─────────────

    def test_confirm_before_auto_delete(self):
        """A tool matching both 'notes_memory' (AUTO) and 'delete' (CONFIRM)
        must be classified as CONFIRM because CONFIRM is checked first."""
        level = self.runner.classify_safety("notes_memory__delete_note", {})
        assert level == SafetyLevel.CONFIRM

    def test_confirm_before_auto_update(self):
        """'status_update' matches 'status' (AUTO) and 'update' (CONFIRM).
        CONFIRM must win."""
        level = self.runner.classify_safety("system__status_update", {})
        assert level == SafetyLevel.CONFIRM

    def test_confirm_before_auto_execute(self):
        """'execute_search' matches 'execute' (CONFIRM) and 'search' (AUTO).
        CONFIRM must win."""
        level = self.runner.classify_safety("tool__execute_search", {})
        assert level == SafetyLevel.CONFIRM

    def test_confirm_before_auto_create_query(self):
        """'create_query' contains both 'create' (CONFIRM) and 'query' (AUTO)."""
        level = self.runner.classify_safety("db__create_query", {})
        assert level == SafetyLevel.CONFIRM

    # ── Edge cases ────────────────────────────────────────────────────────

    def test_empty_tool_name(self):
        assert self.runner.classify_safety("", {}) == SafetyLevel.CONFIRM

    def test_case_insensitive(self):
        assert self.runner.classify_safety("Device__FACTORY_RESET", {}) == SafetyLevel.DENY

    def test_case_insensitive_confirm(self):
        assert self.runner.classify_safety("Messaging__SEND_email", {}) == SafetyLevel.CONFIRM

    def test_deny_takes_precedence_over_confirm(self):
        """'format' (DENY) should beat 'send' (CONFIRM) when both present."""
        level = self.runner.classify_safety("system__format_and_send", {})
        assert level == SafetyLevel.DENY

    def test_robot_move_no_speed_key_is_confirm(self):
        """robot_move without speed arg defaults to 0, so no DENY — CONFIRM instead."""
        level = self.runner.classify_safety("robot_ext__robot_move", {"direction": "left"})
        assert level == SafetyLevel.CONFIRM


# ═══════════════════════════════════════════════════════════════════════════
# Safety Enforcement Gate
# ═══════════════════════════════════════════════════════════════════════════

class TestEnforceSafety:
    """Test enforce_safety gating across autonomy modes and surface policies."""

    def setup_method(self):
        self.runner = _make_runner("hybrid")

    # ── DENY always blocked ───────────────────────────────────────────────

    def test_deny_returns_blocked_dict(self):
        result = self.runner.enforce_safety("system__factory_reset", {}, session_id="s1")
        assert result is not None
        assert result["status"] == "PermissionOutcome::Deny"
        assert result["safety_level"] == "deny"

    # ── Surface deny (from dangerous_tools.is_tool_allowed) ───────────────

    def test_surface_deny_blocks_tool(self):
        result = self.runner.enforce_safety(
            "system.run", {}, session_id="s1", surface="http_api",
        )
        assert result is not None
        assert "Surface Policy" in result.get("error", "")

    def test_surface_local_cli_confirms_unknown_tool(self):
        result = self.runner.enforce_safety(
            "system.run", {}, session_id="s1", surface="local_cli",
        )
        # Unknown tools now default to CONFIRM, so even local CLI gets a confirmation request
        assert result is not None
        assert result.get("safety_level") == "confirm"

    # ── Hybrid mode: AUTO passes, CONFIRM requires approval ───────────────

    def test_hybrid_auto_passes(self):
        result = self.runner.enforce_safety("web_search__web_search", {"q": "hi"}, session_id="s1")
        assert result is None

    def test_hybrid_confirm_requires_approval(self):
        result = self.runner.enforce_safety(
            "messaging__send_sms", {"to": "+1"}, session_id="s1",
        )
        assert result is not None
        assert result["status"] == "pending_approval"
        assert result["tool_name"] == "messaging__send_sms"

    # ── Strict mode: even AUTO non-readonly needs approval ────────────────

    def test_strict_readonly_passes(self):
        runner = _make_runner("strict")
        result = runner.enforce_safety("web_search__web_search", {}, session_id="s1")
        assert result is None

    def test_strict_nonreadonly_needs_approval(self):
        runner = _make_runner("strict")
        result = runner.enforce_safety("notes__save_note", {"text": "hi"}, session_id="s1")
        assert result is not None
        assert result["status"] == "pending_approval"

    # ── Loose mode: everything auto-executes ──────────────────────────────

    def test_loose_confirm_auto_executes(self):
        runner = _make_runner("loose")
        result = runner.enforce_safety("messaging__send_sms", {"to": "+1"}, session_id="s1")
        assert result is None

    def test_loose_deny_still_blocked(self):
        runner = _make_runner("loose")
        result = runner.enforce_safety("system__factory_reset", {}, session_id="s1")
        assert result is not None
        assert result["safety_level"] == "deny"

    # ── Standing approval bypasses gate ───────────────────────────────────

    def test_standing_approval_bypasses_confirm(self):
        self.runner._approval_mgr.grant_approval(
            "messaging__send_sms", "s1", scope="session",
        )
        result = self.runner.enforce_safety("messaging__send_sms", {"to": "+1"}, session_id="s1")
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# Anti-Loop Detection
# ═══════════════════════════════════════════════════════════════════════════

class TestAntiLoop:
    """Verify the tool-repeat / anti-loop tracking logic."""

    def setup_method(self):
        self.runner = _make_runner()

    def test_first_call_streak_is_one(self):
        streak = self.runner.register_tool_attempt("s1", "search__go", {"q": "hi"})
        assert streak == 1

    def test_identical_call_increments_streak(self):
        self.runner.register_tool_attempt("s1", "search__go", {"q": "hi"})
        streak = self.runner.register_tool_attempt("s1", "search__go", {"q": "hi"})
        assert streak == 2

    def test_different_args_resets_streak(self):
        self.runner.register_tool_attempt("s1", "search__go", {"q": "hi"})
        self.runner.register_tool_attempt("s1", "search__go", {"q": "hi"})
        streak = self.runner.register_tool_attempt("s1", "search__go", {"q": "bye"})
        assert streak == 1

    def test_different_tool_resets_streak(self):
        self.runner.register_tool_attempt("s1", "a__x", {})
        self.runner.register_tool_attempt("s1", "a__x", {})
        streak = self.runner.register_tool_attempt("s1", "b__y", {})
        assert streak == 1

    def test_different_session_independent(self):
        self.runner.register_tool_attempt("s1", "a__x", {})
        streak = self.runner.register_tool_attempt("s2", "a__x", {})
        assert streak == 1

    def test_clear_session_resets(self):
        self.runner.register_tool_attempt("s1", "a__x", {})
        self.runner.register_tool_attempt("s1", "a__x", {})
        self.runner.clear_session("s1")
        streak = self.runner.register_tool_attempt("s1", "a__x", {})
        assert streak == 1

    def test_anti_loop_guidance_message(self):
        msg = ToolRunner.anti_loop_guidance("some_tool", 3)
        assert "STOP" in msg
        assert "some_tool" in msg
        assert "3 times" in msg

    def test_anti_loop_guidance_shell_includes_hint(self):
        msg = ToolRunner.anti_loop_guidance("desktop_control__shell_command", 4)
        assert "coding_tools__write_file" in msg

    def test_tool_signature_deterministic(self):
        sig1 = ToolRunner.tool_signature("t", {"b": 2, "a": 1})
        sig2 = ToolRunner.tool_signature("t", {"a": 1, "b": 2})
        assert sig1 == sig2


# ═══════════════════════════════════════════════════════════════════════════
# Approval Lifecycle
# ═══════════════════════════════════════════════════════════════════════════

class TestApprovalLifecycle:
    """Test approve_pending / deny_pending round-trips."""

    def setup_method(self):
        self.runner = _make_runner("hybrid")

    def test_approve_pending_returns_tool_info(self):
        pending = self.runner.enforce_safety(
            "messaging__send_sms", {"to": "+1"}, session_id="s1",
        )
        assert pending is not None
        assert pending["status"] == "pending_approval"
        req_id = pending["request_id"]

        result = self.runner.approve_pending(req_id)
        assert result is not None
        assert result["tool_name"] == "messaging__send_sms"
        assert result["args"] == {"to": "+1"}

    def test_deny_pending_returns_denial(self):
        pending = self.runner.enforce_safety(
            "messaging__send_sms", {"to": "+1"}, session_id="s1",
        )
        assert pending is not None
        req_id = pending["request_id"]

        result = self.runner.deny_pending(req_id)
        assert result is not None
        assert result["status"] == "PermissionOutcome::Deny"
        assert "denied" in result["note"].lower() or "Denied" in result["note"]

    def test_approve_nonexistent_returns_none(self):
        assert self.runner.approve_pending("does-not-exist") is None

    def test_deny_nonexistent_returns_none(self):
        assert self.runner.deny_pending("does-not-exist") is None

    def test_approve_pending_with_wrong_session_returns_none(self):
        pending = self.runner.enforce_safety(
            "messaging__send_sms", {"to": "+1"}, session_id="s1",
        )
        assert pending is not None
        req_id = pending["request_id"]
        assert self.runner.approve_pending(req_id, session_id="s2") is None
        # Original request remains pending.
        assert self.runner.get_pending(req_id) is not None

    def test_deny_pending_with_wrong_session_returns_none(self):
        pending = self.runner.enforce_safety(
            "messaging__send_sms", {"to": "+1"}, session_id="s1",
        )
        assert pending is not None
        req_id = pending["request_id"]
        assert self.runner.deny_pending(req_id, session_id="s2") is None
        # Original request remains pending.
        assert self.runner.get_pending(req_id) is not None

    def test_double_approve_returns_none(self):
        pending = self.runner.enforce_safety(
            "messaging__send_sms", {"to": "+1"}, session_id="s1",
        )
        assert pending is not None
        req_id = pending["request_id"]
        self.runner.approve_pending(req_id)
        assert self.runner.approve_pending(req_id) is None

    def test_duplicate_pending_reuses_existing_request_id(self):
        first = self.runner.enforce_safety(
            "messaging__send_sms", {"to": "+1"}, session_id="s1",
        )
        assert first is not None
        second = self.runner.enforce_safety(
            "messaging__send_sms", {"to": "+1"}, session_id="s1",
        )
        assert second is not None
        assert second["request_id"] == first["request_id"]

    def test_pending_for_session_helpers_order_and_pop_latest(self):
        self.runner.enforce_safety("messaging__send_sms", {"to": "+1"}, session_id="s1")
        self.runner.enforce_safety("desktop_control__open_app", {"app": "Safari"}, session_id="s1")
        rows = self.runner.pending_for_session("s1")
        assert len(rows) == 2
        latest = self.runner.latest_pending_for_session("s1")
        assert latest is not None
        popped = self.runner.pop_latest_pending_for_session("s1")
        assert popped is not None
        assert popped["request_id"] == latest["request_id"]
        assert len(self.runner.pending_for_session("s1")) == 1


def test_tool_runner_uses_injected_approval_manager():
    orch = MagicMock()
    orch.daemons = {}
    orch.skills = MagicMock()
    orch.executor = AsyncMock()
    orch.llm = MagicMock()
    orch._mcp_client = None
    orch._send_text = AsyncMock()
    orch._max_iterations = 8
    mgr = ApprovalManager(db_path=":memory:")
    runner = ToolRunner(orch, autonomy_mode="hybrid", approval_manager=mgr)
    assert runner._approval_mgr is mgr


def test_external_approval_grant_is_seen_by_tool_runner():
    orch = MagicMock()
    orch.daemons = {}
    orch.skills = MagicMock()
    orch.executor = AsyncMock()
    orch.llm = MagicMock()
    orch._mcp_client = None
    orch._send_text = AsyncMock()
    orch._max_iterations = 8
    mgr = ApprovalManager(db_path=":memory:")
    runner = ToolRunner(orch, autonomy_mode="hybrid", approval_manager=mgr)

    first = runner.enforce_safety(
        "browser__navigate",
        {"url": "https://google.com"},
        session_id="s-approve",
    )
    assert first is not None
    assert first["status"] == "pending_approval"

    # Simulates an external grant path (e.g. REST/controller) writing
    # into the shared ApprovalManager instance.
    mgr.grant_approval("browser__navigate", "s-approve", scope="session")
    second = runner.enforce_safety(
        "browser__navigate",
        {"url": "https://google.com"},
        session_id="s-approve",
    )
    assert second is None


# ═══════════════════════════════════════════════════════════════════════════
# Autonomy Mode
# ═══════════════════════════════════════════════════════════════════════════

class TestAutonomyMode:
    """Test runtime autonomy mode management."""

    def test_default_hybrid(self):
        runner = _make_runner()
        assert runner.autonomy_mode == "hybrid"

    def test_set_valid_mode(self):
        runner = _make_runner()
        result = runner.set_autonomy_mode("strict")
        assert result == "strict"
        assert runner.autonomy_mode == "strict"

    def test_set_invalid_mode_keeps_current(self):
        runner = _make_runner("hybrid")
        result = runner.set_autonomy_mode("yolo")
        assert result == "hybrid"
        assert runner.autonomy_mode == "hybrid"

    @patch.dict("os.environ", {"FERAL_AUTONOMY": "strict"})
    def test_env_var_overrides_constructor_arg(self):
        runner = _make_runner("loose")
        assert runner.autonomy_mode == "strict"

    @patch.dict("os.environ", {"FERAL_AUTONOMY": "invalid_garbage"})
    def test_invalid_env_var_falls_back_to_hybrid(self):
        """Invalid FERAL_AUTONOMY env is truthy (bypasses the `or` fallback)
        but not in VALID_AUTONOMY_MODES, so the code falls back to 'hybrid'."""
        runner = _make_runner("loose")
        assert runner.autonomy_mode == "hybrid"


# ═══════════════════════════════════════════════════════════════════════════
# End-to-End Tool Execution (execute_tool_call_for_llm)
# ═══════════════════════════════════════════════════════════════════════════

class TestExecuteToolCallForLLM:
    """Test the LLM-loop tool execution entry point with mocked dependencies."""

    def setup_method(self):
        self.runner = _make_runner("loose")
        self.orch = self.runner._orch

    async def test_invalid_tool_format_returns_error(self):
        result = await self.runner.execute_tool_call_for_llm(
            "s1", {"name": "no_double_underscore", "args": {}}, [],
        )
        assert "error" in result
        assert "Invalid tool reference" in result["error"]

    async def test_safety_denial_blocks_execution(self):
        runner = _make_runner("hybrid")
        result = await runner.execute_tool_call_for_llm(
            "s1",
            {"name": "system__factory_reset", "args": {}},
            [],
        )
        assert result["status"] == "PermissionOutcome::Deny"

    async def test_anti_loop_blocks_after_five(self):
        runner = _make_runner("loose")
        skill = MagicMock()
        skill.endpoints = [MagicMock(id="do_thing")]
        runner._orch.skills.skills = {"test": skill}
        runner._orch.executor.execute = AsyncMock(return_value={"success": True, "data": None})

        call = {"name": "test__do_thing", "args": {"x": 1}}
        for _ in range(4):
            await runner.execute_tool_call_for_llm("s1", call, [])

        result = await runner.execute_tool_call_for_llm("s1", call, [])
        assert result.get("anti_loop_blocked") is True
        assert result["anti_loop_streak"] == 5

    async def test_successful_execution_returns_result(self):
        skill = MagicMock()
        ep = MagicMock()
        ep.id = "search"
        skill.endpoints = [ep]
        self.orch.skills.skills = {"web": skill}
        self.orch.executor.execute = AsyncMock(
            return_value={"success": True, "data": {"results": []}}
        )

        result = await self.runner.execute_tool_call_for_llm(
            "s1", {"name": "web__search", "args": {"q": "weather"}}, [],
        )
        assert result["success"] is True
        self.orch.executor.execute.assert_called_once()

    async def test_computer_use_permission_denial_sends_grant_request(self):
        skill = MagicMock()
        ep = MagicMock()
        ep.id = "write_file"
        skill.endpoints = [ep]
        self.orch.skills.skills = {"computer_use": skill}
        self.orch.executor.execute = AsyncMock(return_value={
            "success": False,
            "status_code": 403,
            "data": {
                "permission_needed": True,
                "path": "/Users/example/Desktop/game.html",
                "operation": "write",
            },
            "error": "Permission denied",
        })

        result = await self.runner.execute_tool_call_for_llm(
            "s1",
            {
                "name": "computer_use__write_file",
                "args": {
                    "path": "/Users/example/Desktop/game.html",
                    "content": "<html></html>",
                },
            },
            [],
        )

        assert result["_permission_request_sent"] is True
        assert "wait for the grant" in result["remediation"]
        self.orch.send_permission_request.assert_awaited_once()
        args = self.orch.send_permission_request.await_args.args
        assert args[:3] == ("s1", "/Users/example/Desktop/game.html", "write")
        assert "computer_use__write_file" not in self.orch.send_permission_request.await_args.kwargs["reason"]

    async def test_skill_not_found_returns_error(self):
        self.orch.skills.skills = {}
        result = await self.runner.execute_tool_call_for_llm(
            "s1", {"name": "missing__action", "args": {}}, [],
        )
        assert "error" in result
        assert "Skill not found" in result["error"]

    async def test_endpoint_not_found_returns_error(self):
        skill = MagicMock()
        skill.endpoints = []
        self.orch.skills.skills = {"myplugin": skill}

        result = await self.runner.execute_tool_call_for_llm(
            "s1", {"name": "myplugin__nonexistent", "args": {}}, [],
        )
        assert "error" in result
        assert "Endpoint not found" in result["error"]

    async def test_mcp_tool_dispatches_to_mcp_client(self):
        mcp_client = AsyncMock()
        mcp_client.call_tool = AsyncMock(
            return_value={"content": [{"text": "mcp result"}]}
        )
        self.orch._mcp_client = mcp_client

        result = await self.runner.execute_tool_call_for_llm(
            "s1", {"name": "mcp_weather__get_forecast", "args": {"city": "NY"}}, [],
        )
        assert result["data"] == "mcp result"
        mcp_client.call_tool.assert_called_once_with(
            "mcp_weather__get_forecast", {"city": "NY"},
        )

    async def test_daemon_tool_dispatched_and_awaits_ack(self):
        """A2 fix: the daemon branch now waits for the daemon ack instead
        of short-circuiting with a stub success. We simulate the ack by
        resolving the pending future as soon as the command is sent."""
        import asyncio as _asyncio

        ws = AsyncMock()
        self.orch.daemons = {"mynode": ws}

        async def _resolve_once_sent(*_args, **_kwargs):
            for req_id in list(self.runner._pending_daemon_acks.keys()):
                self.runner.resolve_daemon_ack(
                    req_id,
                    {"success": True, "data": {"output": "ok"}, "error": None},
                )

        ws.send_json = AsyncMock(side_effect=_resolve_once_sent)

        result = await self.runner.execute_tool_call_for_llm(
            "s1", {"name": "daemon_mynode__do_thing", "args": {"val": 1}}, [],
        )
        assert result["success"] is True
        assert result["data"]["output"] == "ok"
        ws.send_json.assert_called_once()

    async def test_daemon_tool_times_out_without_ack(self):
        """No ack within the timeout → success: False with actionable error."""
        ws = AsyncMock()
        self.orch.daemons = {"mynode": ws}

        result = await self.runner.execute_daemon_command_with_ack(
            "s1", "daemon_mynode", "do_thing", {"val": 1}, timeout=0.05,
        )
        assert result["success"] is False
        assert "did not acknowledge" in result["error"]

    async def test_anti_loop_guidance_attached_at_streak_3(self):
        skill = MagicMock()
        ep = MagicMock()
        ep.id = "act"
        skill.endpoints = [ep]
        self.orch.skills.skills = {"t": skill}
        self.orch.executor.execute = AsyncMock(return_value={"success": True, "data": None})

        call = {"name": "t__act", "args": {"k": "v"}}
        for _ in range(2):
            await self.runner.execute_tool_call_for_llm("s1", call, [])

        result = await self.runner.execute_tool_call_for_llm("s1", call, [])
        assert "_anti_loop_guidance" in result
        assert result["_anti_loop_streak"] == 3

    async def test_subagent_spawn_respects_safety_gate(self):
        """In strict mode, subagent__spawn_subagent is not read-only,
        so it requires approval even though it classifies as AUTO."""
        runner = _make_runner("strict")
        result = await runner.execute_tool_call_for_llm(
            "s1",
            {"name": "subagent__spawn_subagent", "args": {"task": "test"}},
            [],
        )
        assert result is not None
        assert result.get("status") == "pending_approval"
