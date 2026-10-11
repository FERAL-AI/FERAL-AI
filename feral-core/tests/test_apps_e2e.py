"""End-to-end tests for the two example apps.

Mounts a real AppRegistry + HybridGenerator against the actual
``examples/apps/feral-messages`` and ``examples/apps/feral-rides``
manifests on disk. Exercises the full happy paths (install, open
entry surface, hydrate with data, navigate via action contract,
fetch publisher-default for hybrid surface) without mocking the apps.

The orchestrator is the only thing faked, since we don't need the
LLM round trip — the goal is to prove the AppManifest + AppRegistry +
HybridGenerator + ui_handlers wiring is real, not to validate prompt
quality.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.app_registry import AppRegistry, HybridGenerator
from agents.ui_handlers import handle_ui_event


EXAMPLES = Path(__file__).resolve().parent.parent.parent / "examples" / "apps"


@pytest.fixture
def registry(tmp_path):
    db = tmp_path / "apps.db"
    apps_dir = tmp_path / "apps"
    cache_dir = tmp_path / "cache"
    reg = AppRegistry(db_path=str(db), apps_dir=apps_dir)
    hybrid = HybridGenerator(cache_dir=cache_dir)
    reg.set_hybrid_generator(hybrid)
    return reg


def test_examples_dir_present():
    assert (EXAMPLES / "feral-messages" / "manifest.yaml").is_file()
    assert (EXAMPLES / "feral-rides" / "manifest.yaml").is_file()
    assert (EXAMPLES / "feral-reminders" / "manifest.yaml").is_file()


class TestMessagesApp:
    def test_installs_from_examples_dir(self, registry):
        app = registry.install_from_dir(EXAMPLES / "feral-messages")
        assert app.app_id == "feral-messages"
        assert app.manifest.brand.name == "Messages"
        assert app.manifest.entry_surface_id == "inbox"
        assert {s.surface_id for s in app.manifest.surfaces} == {"inbox", "thread"}

    @pytest.mark.asyncio
    async def test_opens_inbox_with_hydrated_contacts(self, registry):
        registry.install_from_dir(EXAMPLES / "feral-messages")
        contacts = [
            {"contact_id": "amy", "name": "Amy", "unread": 1, "preview": "see you at 7"},
            {"contact_id": "sam", "name": "Sam", "unread": 0, "preview": "thanks!"},
        ]
        out = await registry.open_surface(
            "feral-messages",
            "inbox",
            data={"contacts": contacts},
        )
        assert out["app_id"] == "feral-messages"
        assert out["surface_id"] == "inbox"
        # Contact preview hydrates from $data.contacts.0.preview / .1.preview
        rendered = out["root"]
        # Walk the rendered tree and assert each contact's preview made it in.
        text_values = _collect_text_values(rendered)
        assert any("see you at 7" in v for v in text_values)
        assert any("thanks!" in v for v in text_values)

    @pytest.mark.asyncio
    async def test_opens_thread(self, registry):
        registry.install_from_dir(EXAMPLES / "feral-messages")
        out = await registry.open_surface(
            "feral-messages",
            "thread",
            data={"contact_id": "amy", "contact_name": "Amy", "messages": []},
        )
        assert out["surface_id"] == "thread"
        text_values = _collect_text_values(out["root"])
        assert "Amy" in text_values

    def test_action_contract_validates_open_thread(self, registry):
        registry.install_from_dir(EXAMPLES / "feral-messages")
        spec = registry.validate_action("feral-messages", "inbox", "open_thread")
        assert spec.handler == "navigate"
        assert spec.target == "thread"

    def test_action_contract_rejects_unknown(self, registry):
        registry.install_from_dir(EXAMPLES / "feral-messages")
        with pytest.raises(Exception):
            registry.validate_action("feral-messages", "inbox", "delete_universe")

    @pytest.mark.asyncio
    async def test_send_message_action_routes_through_app_event(self, registry):
        registry.install_from_dir(EXAMPLES / "feral-messages")
        orchestrator = MagicMock()
        orchestrator._send_text = AsyncMock()
        orchestrator.handle_command = AsyncMock()
        orchestrator.send = AsyncMock()
        mock_state = MagicMock()
        mock_state.app_registry = registry
        with patch("api.state.state", mock_state):
            await handle_ui_event(
                orchestrator,
                session_id="sess-1",
                action_id="send_message",
                event="tap",
                value={"values": {"text": "hi Amy"}},
                app_id="feral-messages",
                screen_id="feral-messages:thread:sess-1",
            )
        orchestrator.handle_command.assert_awaited_once()
        prompt = orchestrator.handle_command.await_args.args[1]
        assert "send_message" in prompt
        assert "feral-messages" in prompt


class TestRidesApp:
    def test_installs_from_examples_dir(self, registry):
        app = registry.install_from_dir(EXAMPLES / "feral-rides")
        assert app.app_id == "feral-rides"
        assert app.manifest.brand.primary_color == "#2563EB"
        assert {s.surface_id for s in app.manifest.surfaces} == {"request", "confirm", "status"}

    @pytest.mark.asyncio
    async def test_opens_request_form(self, registry):
        registry.install_from_dir(EXAMPLES / "feral-rides")
        out = await registry.open_surface("feral-rides", "request")
        assert out["surface_id"] == "request"
        text_values = _collect_text_values(out["root"])
        assert any("Where to" in v for v in text_values)

    @pytest.mark.asyncio
    async def test_hybrid_confirm_uses_publisher_default(self, registry):
        registry.install_from_dir(EXAMPLES / "feral-rides")
        # Force regenerate=True to opt into the LLM/default branch
        # rather than the authored template. With no LLM wired, the
        # generator falls back to the publisher's shipped default.
        out = await registry.open_surface(
            "feral-rides",
            "confirm",
            data={
                "pickup_summary": "Pickup: Mission & 24th",
                "destination_summary": "Destination: SFO",
                "fare_estimate": "$24.50",
            },
            regenerate=True,
        )
        text_values = _collect_text_values(out["root"])
        # Default surface includes both summaries + the fare estimate.
        assert any("Mission & 24th" in v for v in text_values)
        assert any("SFO" in v for v in text_values)
        assert any("24.50" in v or "$24.50" in v for v in text_values)

    @pytest.mark.asyncio
    async def test_authored_confirm_renders_when_no_regenerate(self, registry):
        registry.install_from_dir(EXAMPLES / "feral-rides")
        out = await registry.open_surface(
            "feral-rides",
            "confirm",
            data={
                "pickup_summary": "Pickup: Home",
                "destination_summary": "Destination: Office",
                "fare_estimate": "$8.00",
            },
        )
        text_values = _collect_text_values(out["root"])
        assert any("Home" in v for v in text_values)

    def test_destructive_cancel_is_marked(self, registry):
        registry.install_from_dir(EXAMPLES / "feral-rides")
        spec = registry.validate_action("feral-rides", "status", "cancel_ride")
        assert spec.requires_confirmation is True

    @pytest.mark.asyncio
    async def test_request_ride_navigate_pushes_confirm_sdui(self, registry):
        registry.install_from_dir(EXAMPLES / "feral-rides")
        orchestrator = MagicMock()
        orchestrator._send_text = AsyncMock()
        orchestrator.handle_command = AsyncMock()
        orchestrator.send = AsyncMock()
        orchestrator._execute_tool_call = AsyncMock()
        orchestrator._pending_confirmations = {}
        mock_state = MagicMock()
        mock_state.app_registry = registry
        with patch("api.state.state", mock_state):
            await handle_ui_event(
                orchestrator,
                session_id="sess-1",
                action_id="request_ride",
                event="tap",
                value={
                    "values": {
                        "pickup": "Mission & 24th",
                        "destination": "SFO",
                    },
                    "pickup_summary": "Pickup: Mission & 24th",
                    "destination_summary": "Destination: SFO",
                    "fare_estimate": "$24.50",
                },
                app_id="feral-rides",
                screen_id="feral-rides:request:sess-1",
            )
        orchestrator.send.assert_awaited_once()
        msg = orchestrator.send.await_args.args[1]
        assert msg.type == "sdui"
        assert msg.payload["screen_id"].startswith("feral-rides:confirm:")

    @pytest.mark.asyncio
    async def test_cancel_ride_waits_for_confirmation(self, registry):
        registry.install_from_dir(EXAMPLES / "feral-rides")
        orchestrator = MagicMock()
        orchestrator._send_text = AsyncMock()
        orchestrator.handle_command = AsyncMock()
        orchestrator.send = AsyncMock(return_value=True)
        orchestrator._execute_tool_call = AsyncMock()
        orchestrator._pending_confirmations = {}
        mock_state = MagicMock()
        mock_state.app_registry = registry
        with patch("api.state.state", mock_state):
            await handle_ui_event(
                orchestrator,
                session_id="sess-1",
                action_id="cancel_ride",
                event="tap",
                app_id="feral-rides",
                screen_id="feral-rides:status:sess-1",
            )
            orchestrator.handle_command.assert_not_called()
            confirm_msg = orchestrator.send.await_args.args[1]
            confirm_action_id = confirm_msg.payload["root"]["children"][2]["children"][0]["action_id"]
            await handle_ui_event(
                orchestrator,
                session_id="sess-1",
                action_id=confirm_action_id,
                event="tap",
                app_id="feral-rides",
                screen_id="feral-rides:status:sess-1",
            )
        orchestrator.handle_command.assert_awaited_once()


class TestHybridCacheReuse:
    @pytest.mark.asyncio
    async def test_publisher_default_caches_for_subsequent_opens(self, registry, tmp_path):
        # Use a fresh AppRegistry/HybridGenerator pair in this test so
        # we can spy on the cache directory.
        reg = AppRegistry(
            db_path=str(tmp_path / "apps2.db"),
            apps_dir=tmp_path / "apps2",
        )
        cache_dir = tmp_path / "cache2"
        hybrid = HybridGenerator(cache_dir=cache_dir)
        reg.set_hybrid_generator(hybrid)
        reg.install_from_dir(EXAMPLES / "feral-rides")

        await reg.open_surface(
            "feral-rides",
            "confirm",
            data={"pickup_summary": "Home", "destination_summary": "Office", "fare_estimate": "$8"},
            regenerate=True,
            user_fingerprint="user-1",
        )
        cached_files = list(cache_dir.rglob("*.json"))
        assert len(cached_files) >= 1, "Expected at least one cache file after regenerate"

        # Re-open without regenerate; the cache should drive the render.
        await reg.open_surface(
            "feral-rides",
            "confirm",
            data={"pickup_summary": "Home", "destination_summary": "Office", "fare_estimate": "$8"},
            user_fingerprint="user-1",
        )
        # No new cache file added.
        assert len(list(cache_dir.rglob("*.json"))) == len(cached_files)


class TestRemindersStarterApp:
    def test_installs_from_examples_dir(self, registry):
        app = registry.install_from_dir(EXAMPLES / "feral-reminders")
        assert app.app_id == "feral-reminders"
        assert app.manifest.entry_surface_id == "list"
        assert "feral_reminders" in app.manifest.skill_dependencies

    def test_compose_action_schema_is_enforced(self, registry):
        registry.install_from_dir(EXAMPLES / "feral-reminders")
        with pytest.raises(Exception):
            registry.validate_action(
                "feral-reminders",
                "compose",
                "create_reminder",
                value={"values": {}},
            )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _collect_text_values(node) -> list[str]:
    """Recursively pull every Text/Button/MetricCard `value` out of a tree."""
    out: list[str] = []
    if isinstance(node, dict):
        if node.get("type") in ("Text", "Button", "MetricCard"):
            v = node.get("value")
            if v is not None:
                out.append(str(v))
            label = node.get("label")
            if label is not None:
                out.append(str(label))
        for value in node.values():
            out.extend(_collect_text_values(value))
    elif isinstance(node, list):
        for item in node:
            out.extend(_collect_text_values(item))
    return out
