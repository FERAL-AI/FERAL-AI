"""Phase 11 (audit-r10 overhaul) — brain-on-Mac desktop_control.

Six concerns under test:

1. ``run_applescript`` — platform guard + subprocess outcome →
   ``AppleScriptResult`` envelope.
2. AppleScript TCC error detection — stderr → structured
   ``tcc_target_bundle`` + ``tcc_denied:`` envelope.
3. ``dispatch_desktop_action`` — known actions map to facade
   functions; unknown actions return a structured 404.
4. ``CapabilityRegistry`` brain-host registration + ``find_handler``
   priority (brain-host beats node).
5. ``tcc_card`` builder + ``card_for_action_result`` envelope adapter.
6. ``ToolRunner.execute_capability_action`` brain-host dispatch
   path + tcc_card SDUI emission on Automation denial.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agents.tcc_card import (
    TCC_CATALOG,
    build_tcc_card,
    card_for_action_result,
    parse_tcc_error,
)
from memory.capability_registry import CapabilityRegistry
from skills.desktop_control import (
    BRAIN_HOST_MANIFESTS,
    dispatch_desktop_action,
)
from skills.desktop_control.applescript import (
    AppleScriptResult,
    _resolve_denial_target,
)


# ─── AppleScript result envelope ─────────────────────────────────


class TestAppleScriptResultEnvelope:
    def test_success_envelope(self):
        r = AppleScriptResult(
            success=True, stdout="hi", stderr="", exit_code=0, duration_ms=42
        )
        env = r.to_envelope(action="desktop.notify")
        assert env == {
            "success": True, "status_code": 200,
            "data": {"stdout": "hi"},
        }

    def test_tcc_denial_envelope_includes_structured_token(self):
        r = AppleScriptResult(
            success=False, stdout="", stderr="Not authorized to send Apple events to FaceTime.",
            exit_code=1, duration_ms=5, tcc_target_bundle="com.apple.FaceTime",
        )
        env = r.to_envelope(action="desktop.facetime.start")
        assert env["success"] is False
        assert env["status_code"] == 403
        assert env["error"] == "tcc_denied:automation:com.apple.FaceTime"
        assert env["data"]["action"] == "desktop.facetime.start"

    def test_generic_failure_envelope_carries_stderr(self):
        r = AppleScriptResult(
            success=False, stdout="", stderr="syntax error: bad token",
            exit_code=1, duration_ms=2,
        )
        env = r.to_envelope(action="desktop.app.activate")
        assert env["success"] is False
        assert env["status_code"] == 1
        assert "syntax error" in env["error"]


# ─── TCC error pattern detection ─────────────────────────────────


class TestResolveDenialTarget:
    def test_modern_apple_events_pattern(self):
        bundle = _resolve_denial_target(
            "Not authorized to send Apple events to FaceTime.", None,
        )
        assert bundle == "com.apple.FaceTime"

    def test_legacy_minus_1743_pattern(self):
        stderr = "execution error: Music got an error: User canceled. (-1743)"
        bundle = _resolve_denial_target(stderr, None)
        assert bundle == "com.apple.Music"

    def test_fallback_to_default_bundle_on_opaque_stderr(self):
        bundle = _resolve_denial_target(
            "errAEEventNotPermitted", default_bundle="com.apple.MobileSMS",
        )
        assert bundle == "com.apple.MobileSMS"

    def test_returns_none_for_non_tcc_errors(self):
        bundle = _resolve_denial_target("syntax error: bad token", None)
        assert bundle is None


# ─── Facade dispatcher ───────────────────────────────────────────


class TestDispatchDesktopAction:
    def test_manifests_cover_every_dispatched_action(self):
        # Pin the manifest ↔ dispatcher contract — every action name
        # that ships in BRAIN_HOST_MANIFESTS must route to a real
        # handler. If someone adds an action to the manifest but
        # forgets the dispatcher branch, this test breaks loudly.
        declared = set()
        for skill in BRAIN_HOST_MANIFESTS:
            for action in skill["actions"]:
                declared.add(action["name"])

        # Run each through the dispatcher with empty params. We
        # mostly care that it doesn't return the "unknown action"
        # 404 envelope — failures from missing params / unsupported
        # platform are fine and arrive as different status codes.
        for name in declared:
            env = dispatch_desktop_action(name, {})
            if env.get("status_code") == 404:
                pytest.fail(f"manifest action {name!r} has no dispatcher branch")

    def test_unknown_action_returns_404_envelope(self):
        env = dispatch_desktop_action("desktop.nonexistent", {})
        assert env["success"] is False
        assert env["status_code"] == 404
        assert "unknown desktop_control action" in env["error"]

    def test_messages_send_requires_to_and_body(self):
        env = dispatch_desktop_action("desktop.messages.send", {"to": ""})
        assert env["success"] is False
        # Missing-param error bubbles up as a 500 from the
        # _require helper raising ValueError; what matters is it's
        # NOT a silent success or a 404.
        assert env["status_code"] == 500


# ─── CapabilityRegistry brain-host ───────────────────────────────


class TestCapabilityRegistryBrainHost:
    def test_register_brain_host_skills_replaces_prior(self):
        reg = CapabilityRegistry()
        reg.register_brain_host_skills([{"id": "a", "actions": [{"name": "x"}]}])
        reg.register_brain_host_skills([{"id": "b", "actions": [{"name": "y"}]}])
        snap = reg.brain_host_skills()
        assert len(snap) == 1
        assert snap[0]["id"] == "b"

    def test_brain_host_handler_wins_over_node_handler(self):
        # Both surfaces publish the same action name; brain-host
        # priority is critical because Phase 11 actions are
        # in-process (no HUP latency).
        reg = CapabilityRegistry()
        reg.register_brain_host_skills([{"actions": [{"name": "shared.action"}]}])
        reg.register_node(
            "iphone-1", node_type="phone", platform="ios",
            skills=[{"actions": [{"name": "shared.action"}]}],
        )
        handler = reg.find_handler("shared.action")
        assert handler is not None
        assert handler.surface == "brain_host"
        assert handler.node_id is None

    def test_find_handler_returns_brain_host_when_only_brain_host_registered(self):
        reg = CapabilityRegistry()
        reg.register_brain_host_skills(BRAIN_HOST_MANIFESTS)
        handler = reg.find_handler("desktop.facetime.start")
        assert handler is not None
        assert handler.surface == "brain_host"
        assert handler.node_type == "desktop"
        assert handler.platform == "macos"


# ─── tcc_card builder + adapter ──────────────────────────────────


class TestTCCCard:
    def test_parse_canonical_token(self):
        assert parse_tcc_error("tcc_denied:automation:com.apple.FaceTime") \
            == "automation:com.apple.FaceTime"
        assert parse_tcc_error("tcc_denied:accessibility") == "accessibility"

    def test_parse_rejects_other_strings(self):
        assert parse_tcc_error("permission_denied:NSContactsUsageDescription") is None
        assert parse_tcc_error("") is None
        assert parse_tcc_error(None) is None

    def test_build_card_for_automation_target(self):
        card = build_tcc_card(
            "automation:com.apple.FaceTime",
            skill_id="desktop_facetime",
            action="desktop.facetime.start",
            open_settings_on_mac=False,  # don't shell out from a unit test
        )
        assert card["type"] == "tcc_card"
        assert card["permission_key"] == "automation:com.apple.FaceTime"
        assert "FaceTime" in card["title"]
        assert "Privacy_Automation" in card["macos_deeplink"]
        assert card["skill_id"] == "desktop_facetime"

    def test_build_card_for_accessibility_uses_catalog(self):
        card = build_tcc_card("accessibility", open_settings_on_mac=False)
        assert card["title"] == TCC_CATALOG["accessibility"]["title"]
        assert "Privacy_Accessibility" in card["macos_deeplink"]

    def test_build_card_for_unknown_key_falls_back(self):
        card = build_tcc_card("automation:com.example.unknown", open_settings_on_mac=False)
        assert card["type"] == "tcc_card"
        assert "com.example.unknown" in card["title"] or "Automation" in card["title"]

    def test_card_for_action_result_picks_up_envelope(self):
        env = {
            "success": False, "status_code": 403,
            "error": "tcc_denied:automation:com.apple.Music",
        }
        card = card_for_action_result(env, action="desktop.music.play")
        assert card is not None
        assert card["action"] == "desktop.music.play"
        assert "Music" in card["title"]

    def test_card_for_action_result_returns_none_on_other_failures(self):
        env = {"success": False, "error": "permission_denied:NSContactsUsageDescription"}
        assert card_for_action_result(env) is None  # That's an iOS card, not a Mac one.

    def test_card_for_action_result_returns_none_on_success(self):
        assert card_for_action_result({"success": True}) is None


# ─── ToolRunner brain-host routing ───────────────────────────────


class _RecordingOrchestrator:
    """Bare-minimum stand-in capturing every ``send(session_id, msg)``."""
    def __init__(self, registry):
        self.daemons: dict = {}
        self.capability_registry = registry
        self.sent: list = []

    async def send(self, session_id, message):
        self.sent.append((session_id, message))


@pytest.mark.asyncio
async def test_execute_capability_action_routes_brain_host_in_process(monkeypatch):
    from agents.tool_runner import ToolRunner

    reg = CapabilityRegistry()
    reg.register_brain_host_skills(BRAIN_HOST_MANIFESTS)
    orch = _RecordingOrchestrator(reg)
    runner = ToolRunner(orch)

    captured: dict = {}

    def fake_dispatch(name, params):
        captured["name"] = name
        captured["params"] = params
        return {"success": True, "status_code": 200, "data": {"ok": True}}

    monkeypatch.setattr(
        "skills.desktop_control.dispatch_desktop_action",
        fake_dispatch,
    )

    result = await runner.execute_capability_action(
        session_id="primary",
        action="desktop.notify",
        args={"title": "hello"},
    )
    assert result["success"] is True
    assert captured["name"] == "desktop.notify"
    assert captured["params"]["title"] == "hello"
    # No SDUI frame on success.
    sdui_frames = [m for _, m in orch.sent if getattr(m, "type", None) == "sdui"]
    assert sdui_frames == []


@pytest.mark.asyncio
async def test_execute_capability_action_emits_tcc_card_on_automation_denial(monkeypatch):
    from agents.tool_runner import ToolRunner

    reg = CapabilityRegistry()
    reg.register_brain_host_skills(BRAIN_HOST_MANIFESTS)
    orch = _RecordingOrchestrator(reg)
    runner = ToolRunner(orch)

    def fake_dispatch(name, params):
        return {
            "success": False, "status_code": 403,
            "error": "tcc_denied:automation:com.apple.FaceTime",
        }

    monkeypatch.setattr(
        "skills.desktop_control.dispatch_desktop_action",
        fake_dispatch,
    )
    # Suppress the side-effect that opens the macOS Settings pane.
    monkeypatch.setattr(
        "agents.tcc_card.subprocess.run",
        lambda *a, **kw: None,
    )

    result = await runner.execute_capability_action(
        session_id="primary",
        action="desktop.facetime.start",
        args={"contact": "+15551234"},
    )
    assert result["success"] is False
    assert result["error"] == "tcc_denied:automation:com.apple.FaceTime"
    sdui_frames = [m for _, m in orch.sent if m.type == "sdui"]
    assert len(sdui_frames) == 1
    root = sdui_frames[0].payload["root"]
    assert root["type"] == "tcc_card"
    assert root["permission_key"] == "automation:com.apple.FaceTime"
    assert root["action"] == "desktop.facetime.start"


@pytest.mark.asyncio
async def test_execute_capability_action_emits_ios_permission_card_unchanged(monkeypatch):
    """Phase 6 behaviour must keep working after the Phase 11
    refactor that funnels both card kinds through
    `_maybe_emit_capability_cards`."""
    from agents.tool_runner import ToolRunner

    reg = CapabilityRegistry()
    reg.register_node(
        "iphone-1", node_type="phone", platform="ios",
        skills=[{"actions": [{"name": "phone.contact.lookup"}]}],
    )
    orch = _RecordingOrchestrator(reg)
    orch.daemons["iphone-1"] = object()
    runner = ToolRunner(orch)

    async def fake_dispatch(session_id, node_id, action, args, timeout=30.0):
        return {
            "success": False, "status_code": 403,
            "error": "permission_denied:NSContactsUsageDescription",
        }
    monkeypatch.setattr(runner, "execute_daemon_command_with_ack", fake_dispatch)

    await runner.execute_capability_action(
        session_id="primary",
        action="phone.contact.lookup",
        args={"query": "John"},
    )

    sdui_frames = [m for _, m in orch.sent if m.type == "sdui"]
    assert len(sdui_frames) == 1
    root = sdui_frames[0].payload["root"]
    assert root["type"] == "permission_card"
    assert root["permission_key"] == "NSContactsUsageDescription"


# ─── Phase 13 — POST /api/system/permissions/open ─────────────────


class TestOpenSystemPermission:
    """Phase 13-5: POST /api/system/permissions/open triggers macOS
    deeplink for a known TCC key."""

    @pytest.fixture
    def client(self):
        from fastapi import FastAPI
        from api.routes.system_permissions import router

        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    @patch("api.routes.system_permissions.platform")
    def test_known_key_triggers_open(self, mock_platform, client, monkeypatch):
        # AUDIT-FIXES F-05 changed the mechanism, not the contract: the route
        # spawns `open` with asyncio instead of subprocess.run, because
        # subprocess.run blocked the event loop for its whole 3s timeout.
        # The endpoint's status and body are unchanged.
        import asyncio

        mock_platform.system.return_value = "Darwin"
        spawned: dict = {}

        class _Proc:
            returncode = 0

            async def communicate(self):
                return (b"", b"")

        async def _fake_exec(*argv, **kwargs):
            spawned["argv"] = argv
            return _Proc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

        resp = client.post(
            "/api/system/permissions/open",
            json={"permission_key": "accessibility"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        args = spawned["argv"]
        assert args[0] == "open"
        assert "Privacy_Accessibility" in args[1]

    def test_unknown_key_returns_400(self, client):
        resp = client.post(
            "/api/system/permissions/open",
            json={"permission_key": "nonexistent_permission_xyz"},
        )
        assert resp.status_code == 400
