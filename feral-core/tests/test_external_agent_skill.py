"""The ``external_agent`` skill, its catalogue, and its settings readers.

The skill is exercised end to end against the same real ACP subprocess
the client tests use, so ``run_task`` -> ``awaiting_permission`` ->
``respond_permission`` -> ``completed`` is proved on a live pipe rather
than asserted against a stub session.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CORE))

from bridges import catalog  # noqa: E402
from bridges.continuity import SessionIndex  # noqa: E402
from bridges.sessions import SessionRegistry  # noqa: E402
from config.loader import DEFAULT_SETTINGS  # noqa: E402
from skills.impl.external_agent import ExternalAgentSkill  # noqa: E402

from tests.test_bridges_acp_client import FAKE_AGENT  # noqa: E402

MANIFEST = CORE / "skills" / "manifests" / "external_agent.json"


@pytest.fixture
def skill():
    return ExternalAgentSkill()


@pytest.fixture
def isolated_registry(monkeypatch, tmp_path):
    """A registry per test, so no session leaks between them.

    The continuity index is pointed at ``tmp_path``. Without that, merely
    opening a session in a test would write into the operator's real
    ``~/.feral``, which is exactly the kind of side effect a unit test
    must not have.
    """
    registry = SessionRegistry(
        index=SessionIndex(tmp_path / "sessions.json")
    )
    monkeypatch.setattr(
        "skills.impl.external_agent._registry", lambda: registry
    )
    monkeypatch.setattr(
        "skills.impl.external_agent._approval_manager", lambda: None
    )
    monkeypatch.setattr(
        "skills.impl.external_agent._memory", lambda: None
    )
    yield registry


@pytest.fixture
def fake_agent(tmp_path, monkeypatch):
    """Point the catalogue's ``opencode`` entry at the fake ACP agent."""
    script = tmp_path / "fake_acp_agent.py"
    script.write_text(FAKE_AGENT)

    real_resolve = catalog.resolve

    def fake_resolve(agent_id, settings=None):
        if agent_id != "opencode":
            return real_resolve(agent_id, settings)
        return catalog.ResolvedAgent(
            spec=catalog.CATALOG["opencode"],
            available=True,
            binary_path=sys.executable,
            command=[sys.executable, "-u", str(script)],
        )

    monkeypatch.setattr(catalog, "resolve", fake_resolve)
    return script


class TestManifest:
    def test_manifest_and_dispatch_agree_on_endpoint_ids(self, skill):
        manifest = json.loads(MANIFEST.read_text())
        declared = {ep["id"] for ep in manifest["endpoints"]}
        assert declared == {
            "list_agents", "run_task", "respond_permission", "close_session",
            "recall_activity",
        }

    def test_the_skill_is_not_pinned_into_every_turn(self):
        """60 tools already ship per turn; this one must be retrieved."""
        from agents.orchestrator import Orchestrator

        assert "external_agent" not in Orchestrator.ALWAYS_INCLUDE_SKILLS

    def test_the_dangerous_endpoints_demand_confirmation(self):
        manifest = json.loads(MANIFEST.read_text())
        by_id = {ep["id"]: ep for ep in manifest["endpoints"]}
        for endpoint_id in ("run_task", "respond_permission"):
            assert by_id[endpoint_id]["safety_tier"] == "confirm"
            assert by_id[endpoint_id]["requires_user_approval"] is True
        assert by_id["list_agents"]["read_only_hint"] is True

    def test_no_em_dashes_anywhere_in_the_manifest(self):
        raw = MANIFEST.read_bytes()
        assert "—".encode() not in raw
        assert b"\\u2014" not in raw
        # And after a JSON round-trip, where an escape would decode.
        assert "—" not in json.dumps(json.loads(raw.decode()))


class TestCatalogueSettings:
    def test_default_settings_carry_the_external_agents_block(self):
        block = DEFAULT_SETTINGS["external_agents"]
        assert block["default_agent"] == "opencode"
        assert block["opencode_version"] == catalog.DEFAULT_OPENCODE_VERSION
        assert block["opencode_bin"] == ""
        assert block["permission_timeout_seconds"] == 120

    def test_every_key_is_actually_read(self):
        conf = catalog.external_agent_settings(
            {
                "external_agents": {
                    "default_agent": "codex",
                    "opencode_bin": "/opt/oc",
                    "opencode_version": "9.9.9",
                    "permission_timeout_seconds": 45,
                }
            }
        )
        assert conf == {
            "default_agent": "codex",
            "opencode_bin": "/opt/oc",
            "opencode_version": "9.9.9",
            "permission_timeout_seconds": 45.0,
        }

    def test_a_missing_block_falls_back_to_defaults(self):
        conf = catalog.external_agent_settings({})
        assert conf["default_agent"] == "opencode"
        assert conf["opencode_version"] == catalog.DEFAULT_OPENCODE_VERSION

    def test_the_permission_timeout_can_never_be_set_to_zero(self):
        """Zero would mean 'reject instantly', which is a footgun."""
        for value in (0, -5, "nonsense", None):
            conf = catalog.external_agent_settings(
                {"external_agents": {"permission_timeout_seconds": value}}
            )
            assert conf["permission_timeout_seconds"] >= 5.0


class TestCatalogueResolution:
    def test_opencode_uses_the_explicit_binary_when_set(self, tmp_path):
        binary = tmp_path / "opencode"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        assert catalog.find_opencode(str(binary)) == str(binary)

    def test_an_explicit_path_that_is_not_executable_is_not_used(self, tmp_path):
        binary = tmp_path / "opencode"
        binary.write_text("not executable")
        binary.chmod(0o644)
        assert catalog.find_opencode(str(binary)) == ""

    def test_the_installer_directory_is_searched_because_path_is_not_modified(
        self, tmp_path, monkeypatch
    ):
        install_dir = tmp_path / ".opencode" / "bin"
        install_dir.mkdir(parents=True)
        binary = install_dir / "opencode"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        assert catalog.find_opencode("", home=str(tmp_path)) == str(binary)

    def test_a_missing_opencode_reports_how_to_install_it(self, monkeypatch):
        monkeypatch.setattr(catalog, "find_opencode", lambda *a, **kw: "")
        resolved = catalog.resolve("opencode", {})
        assert resolved.available is False
        assert "--no-modify-path" in resolved.reason

    def test_claude_code_and_codex_are_shim_backed_not_native(self):
        for agent_id in ("claude_code", "codex"):
            spec = catalog.CATALOG[agent_id]
            assert spec.native_acp is False
            assert spec.shim_package
        assert catalog.CATALOG["opencode"].native_acp is True

    def test_a_missing_shim_says_so_rather_than_failing_at_spawn(
        self, monkeypatch
    ):
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        resolved = catalog.resolve("claude_code", {})
        assert resolved.available is False
        assert "no native ACP mode" in resolved.reason
        assert "@zed-industries/claude-code-acp" in resolved.reason

    def test_unknown_agents_are_refused(self):
        assert catalog.resolve("emacs", {}).available is False

    def test_launch_command_raises_when_not_installed(self, monkeypatch):
        monkeypatch.setattr(catalog, "find_opencode", lambda *a, **kw: "")
        with pytest.raises(FileNotFoundError):
            catalog.launch_command("opencode", {})

    def test_hermes_is_detected_but_never_installable(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/hermes-acp")
        hermes = catalog.detect_hermes()
        assert hermes["installed"] is True
        assert hermes["installable_by_feral"] is False
        assert "hermes" not in catalog.CATALOG


class TestApprovalManagerLookup:
    """A regression guard for a bug that silently disabled the integration.

    ``approval_manager`` hangs off the ``BrainState`` INSTANCE, so reading
    it off the ``api.state`` module returns ``None`` forever and the
    ApprovalManagerBroker never gets wired in. Nothing would fail loudly:
    permissions would still be answered by a human, and an
    ``allow_always`` would just never be remembered.
    """

    def test_it_reads_through_the_brain_state_instance(self, monkeypatch):
        import types

        from skills.impl.external_agent import _approval_manager

        sentinel = object()
        module = types.ModuleType("api.state")
        module.state = types.SimpleNamespace(approval_manager=sentinel)
        # The attribute also exists on the real module namespace in the
        # buggy reading, so put a decoy there to catch a regression.
        module.approval_manager = "wrong: this is the module, not the state"
        monkeypatch.setitem(sys.modules, "api.state", module)

        assert _approval_manager() is sentinel

    def test_no_brain_means_no_manager_and_no_heavy_import(self, monkeypatch):
        from skills.impl.external_agent import _approval_manager

        monkeypatch.delitem(sys.modules, "api.state", raising=False)

        def explode(*_args, **_kwargs):
            raise AssertionError("a skill call must not import the brain")

        monkeypatch.setattr("builtins.__import__", explode)
        assert _approval_manager() is None


class TestSkillDispatch:
    async def test_unknown_endpoint_is_a_404(self, skill):
        result = await skill.execute("teleport", {}, {})
        assert result["success"] is False
        assert result["status_code"] == 404

    async def test_list_agents_reports_availability(self, skill, isolated_registry):
        result = await skill.execute("list_agents", {}, {})
        assert result["success"] is True
        ids = {a["agent_id"] for a in result["data"]["agents"]}
        assert ids == {"opencode", "claude_code", "codex"}
        assert result["data"]["hermes"]["installable_by_feral"] is False

    async def test_run_task_requires_a_prompt(self, skill, isolated_registry):
        result = await skill.execute("run_task", {"prompt": "  "}, {})
        assert result["success"] is False
        assert "prompt is required" in result["error"]

    async def test_run_task_refuses_a_missing_workspace(self, skill, isolated_registry):
        result = await skill.execute(
            "run_task", {"prompt": "go", "workspace_dir": "/no/such/dir"}, {}
        )
        assert result["success"] is False
        assert "does not exist" in result["error"]

    async def test_an_uninstalled_agent_returns_a_dependency_error(
        self, skill, isolated_registry, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(catalog, "find_opencode", lambda *a, **kw: "")
        result = await skill.execute(
            "run_task", {"prompt": "go", "workspace_dir": str(tmp_path)}, {}
        )
        assert result["success"] is False
        assert result["status_code"] == 424

    async def test_respond_permission_validates_the_decision(
        self, skill, isolated_registry
    ):
        result = await skill.execute(
            "respond_permission", {"request_id": "x", "decision": "maybe"}, {}
        )
        assert result["success"] is False
        assert "allow_once" in result["error"]

    async def test_respond_permission_on_an_unknown_request_is_a_404(
        self, skill, isolated_registry
    ):
        result = await skill.execute(
            "respond_permission",
            {"request_id": "nope", "decision": "allow_once"},
            {},
        )
        assert result["status_code"] == 404

    async def test_close_session_on_an_unknown_handle_is_a_404(
        self, skill, isolated_registry
    ):
        result = await skill.execute(
            "close_session", {"session_handle": "ext-nope"}, {}
        )
        assert result["status_code"] == 404


class TestFullTurnAgainstARealSubprocess:
    """run_task -> awaiting_permission -> respond_permission -> completed."""

    async def test_the_permission_round_trip(
        self, skill, isolated_registry, fake_agent, tmp_path
    ):
        started = await skill.execute(
            "run_task",
            {"prompt": "write hello.txt", "workspace_dir": str(tmp_path),
             "wait_seconds": 30},
            {},
        )
        assert started["success"] is True
        payload = started["data"]
        assert payload["status"] == "awaiting_permission"
        assert payload["session_handle"].startswith("ext-")
        assert len(payload["pending_permissions"]) == 1

        pending = payload["pending_permissions"][0]
        assert pending["tool_name"] == "write"
        assert {o["kind"] for o in pending["options"]} == {
            "allow_once", "allow_always", "reject_once"
        }
        # The agent already streamed a message and opened a tool call.
        assert "thinking about it." in payload["text"]
        assert any(tc["tool_call_id"] == "tc-1" for tc in payload["tool_calls"])

        answered = await skill.execute(
            "respond_permission",
            {"request_id": pending["request_id"], "decision": "allow_once",
             "wait_seconds": 30},
            {},
        )
        assert answered["success"] is True
        done = answered["data"]
        assert done["status"] == "completed"
        assert done["stop_reason"] == "end_turn"
        assert "wrote it." in done["text"]
        assert done["answered"]["decision"] == "allow_once"

        closed = await skill.execute(
            "close_session", {"session_handle": payload["session_handle"]}, {}
        )
        assert closed["data"]["closed"] is True

    async def test_rejecting_lets_the_turn_finish_without_the_action(
        self, skill, isolated_registry, fake_agent, tmp_path
    ):
        started = await skill.execute(
            "run_task",
            {"prompt": "write hello.txt", "workspace_dir": str(tmp_path),
             "wait_seconds": 30},
            {},
        )
        pending = started["data"]["pending_permissions"][0]
        answered = await skill.execute(
            "respond_permission",
            {"request_id": pending["request_id"], "decision": "reject_once",
             "wait_seconds": 30},
            {},
        )
        assert "refused." in answered["data"]["text"]
        await skill.execute(
            "close_session",
            {"session_handle": started["data"]["session_handle"]},
            {},
        )

    async def test_a_decision_the_agent_did_not_offer_is_refused(
        self, skill, isolated_registry, fake_agent, tmp_path
    ):
        """The fake agent never offers reject_always."""
        started = await skill.execute(
            "run_task",
            {"prompt": "write hello.txt", "workspace_dir": str(tmp_path),
             "wait_seconds": 30},
            {},
        )
        pending = started["data"]["pending_permissions"][0]
        result = await skill.execute(
            "respond_permission",
            {"request_id": pending["request_id"], "decision": "reject_always"},
            {},
        )
        assert result["success"] is False
        assert result["status_code"] == 409
        await skill.execute(
            "close_session",
            {"session_handle": started["data"]["session_handle"],
             "cancel_first": True},
            {},
        )

    async def test_a_second_turn_on_a_busy_session_is_refused(
        self, skill, isolated_registry, fake_agent, tmp_path
    ):
        started = await skill.execute(
            "run_task",
            {"prompt": "write hello.txt", "workspace_dir": str(tmp_path),
             "wait_seconds": 30},
            {},
        )
        handle = started["data"]["session_handle"]
        again = await skill.execute(
            "run_task", {"prompt": "and again", "session_handle": handle}, {}
        )
        assert again["success"] is False
        assert again["status_code"] == 409
        await skill.execute(
            "close_session", {"session_handle": handle, "cancel_first": True}, {}
        )

    async def test_closing_kills_the_subprocess(
        self, skill, isolated_registry, fake_agent, tmp_path
    ):
        started = await skill.execute(
            "run_task",
            {"prompt": "write hello.txt", "workspace_dir": str(tmp_path),
             "wait_seconds": 30},
            {},
        )
        handle = started["data"]["session_handle"]
        managed = isolated_registry.get(handle)
        assert managed.process.alive
        await skill.execute(
            "close_session", {"session_handle": handle, "cancel_first": True}, {}
        )
        assert isolated_registry.get(handle) is None
        assert not managed.process.alive
