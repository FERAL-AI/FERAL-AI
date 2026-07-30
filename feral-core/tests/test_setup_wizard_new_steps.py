"""The wizard steps added to reach capabilities it never exposed.

The pre-existing wizard configured ~7 things. Everything asserted here
is a settings key or file the brain already read at boot; the only
thing that was missing was a way to set it without hand-editing
``~/.feral/settings.json``.
"""

from __future__ import annotations

import pytest

from cli import ui_kit
from cli.setup.state import WizardState
from cli.setup.steps import capabilities as caps_step
from cli.setup.steps import personality as personality_step
from cli.setup.steps import tool_keys as tool_keys_step


# ---------------------------------------------------------------------------
# ui_kit.multi_select — a real multi-pick (``select`` allows exactly one)
# ---------------------------------------------------------------------------


class TestMultiSelect:
    def test_fallback_toggles_by_index(self, monkeypatch, capsys):
        import io

        monkeypatch.setattr(ui_kit, "_INQUIRER_AVAILABLE", False)
        monkeypatch.setattr("sys.stdin", io.StringIO("1,3\n"))
        out = ui_kit.multi_select("pick", [
            {"name": "alpha", "value": "a", "enabled": False},
            {"name": "beta", "value": "b", "enabled": True},
            {"name": "gamma", "value": "g", "enabled": False},
        ])
        assert out == ["a", "b", "g"]

    def test_blank_input_keeps_current_state(self, monkeypatch):
        import io

        monkeypatch.setattr(ui_kit, "_INQUIRER_AVAILABLE", False)
        monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
        out = ui_kit.multi_select("pick", [
            {"name": "on", "value": "on", "enabled": True},
            {"name": "off", "value": "off", "enabled": False},
        ])
        assert out == ["on"]

    def test_toggling_an_enabled_row_turns_it_off(self, monkeypatch):
        import io

        monkeypatch.setattr(ui_kit, "_INQUIRER_AVAILABLE", False)
        monkeypatch.setattr("sys.stdin", io.StringIO("1\n"))
        out = ui_kit.multi_select("pick", [
            {"name": "on", "value": "on", "enabled": True},
        ])
        assert out == []


# ---------------------------------------------------------------------------
# capabilities
# ---------------------------------------------------------------------------


class TestCapabilitiesStep:
    def test_writes_the_keys_the_runtime_reads(self, tmp_path, monkeypatch):
        state = WizardState.load(tmp_path / "feral")

        monkeypatch.setattr(
            caps_step.ui_kit, "multi_select",
            lambda *a, **kw: ["features.proactive", "vision.enabled"],
        )
        monkeypatch.setattr(
            caps_step, "ask_choice",
            lambda p, opts, **kw: next(o for o in opts if o.id == "loose"),
        )
        monkeypatch.setattr(caps_step, "confirm", lambda *a, **kw: False)

        caps_step.run(state)

        # Flags the ConfigLoader exports to the runtime env.
        assert state.get_setting("features", "proactive") is True
        assert state.get_setting("features", "streaming") is False
        assert state.get_setting("vision", "enabled") is True
        # security.autonomy_mode is what api/state.py reads at boot.
        assert state.get_setting("security", "autonomy_mode") == "loose"

    def test_autonomy_modes_match_the_tool_runner(self):
        from agents.tool_runner import VALID_AUTONOMY_MODES

        offered = {mode for mode, _label in caps_step._AUTONOMY_MODES}
        assert offered == set(VALID_AUTONOMY_MODES)

    def test_toggle_targets_are_real_settings_keys(self):
        from config.loader import DEFAULT_SETTINGS

        for section, key, _label, _blurb in caps_step._TOGGLES:
            assert section in DEFAULT_SETTINGS, section
            assert key in DEFAULT_SETTINGS[section], f"{section}.{key}"

    def test_workspace_grant_delegates_to_sandbox_policy(self, tmp_path, monkeypatch):
        state = WizardState.load(tmp_path / "feral")
        target = tmp_path / "Projects"
        target.mkdir()

        monkeypatch.setattr(caps_step.ui_kit, "multi_select", lambda *a, **kw: [])
        confirms = iter([True, False])  # grant a folder? yes; another? no
        monkeypatch.setattr(caps_step, "confirm", lambda *a, **kw: next(confirms))
        monkeypatch.setattr(caps_step, "ask_text", lambda *a, **kw: str(target))

        # autonomy first, then the access level
        choices = iter(["hybrid", "readwrite"])

        def _pick(_prompt, opts, **_kw):
            wanted = next(choices)
            return next(o for o in opts if o.id == wanted)

        monkeypatch.setattr(caps_step, "ask_choice", _pick)

        granted = {}

        class _Policy:
            def grant_folder(self, path, mode="read"):
                granted["path"] = path
                granted["mode"] = mode
                return {"ok": True, "path": path, "mode": mode}

        monkeypatch.setattr(
            "security.sandbox_policy.SandboxPolicy.load_default",
            classmethod(lambda cls: _Policy()),
        )

        caps_step.run(state)

        assert granted["path"] == str(target)
        assert granted["mode"] == "readwrite"

    def test_nonexistent_grant_path_is_refused(self, tmp_path, monkeypatch):
        """The grant CLI refuses to fabricate a grant for a path the OS
        doesn't have; the wizard must not be a way around that."""
        state = WizardState.load(tmp_path / "feral")

        monkeypatch.setattr(caps_step.ui_kit, "multi_select", lambda *a, **kw: [])
        # grant? yes -> bad path -> try again? no
        confirms = iter([True, False])
        monkeypatch.setattr(caps_step, "confirm", lambda *a, **kw: next(confirms))
        monkeypatch.setattr(
            caps_step, "ask_text", lambda *a, **kw: str(tmp_path / "nope"),
        )
        monkeypatch.setattr(
            caps_step, "ask_choice",
            lambda p, opts, **kw: next(o for o in opts if o.id == "hybrid"),
        )

        called = []
        monkeypatch.setattr(
            caps_step, "_grant", lambda *a, **kw: called.append(a),
        )

        caps_step.run(state)
        assert called == []


# ---------------------------------------------------------------------------
# personality
# ---------------------------------------------------------------------------


class TestPersonalityStep:
    def test_writes_soul_md(self, tmp_path, monkeypatch):
        home = tmp_path / "feral"
        state = WizardState.load(home)

        monkeypatch.setattr(personality_step, "confirm", lambda *a, **kw: True)
        monkeypatch.setattr(
            personality_step, "ask_choice",
            lambda p, opts, **kw: next(o for o in opts if o.id == "engineer"),
        )
        monkeypatch.setattr(personality_step, "ask_text", lambda *a, **kw: "FERAL")

        personality_step.run(state)

        soul = (home / "SOUL.md").read_text()
        assert soul.startswith("# FERAL")
        assert "senior engineer partner" in soul
        assert state.get_setting("identity", "personality") == "engineer"

    def test_existing_soul_is_not_replaced_without_consent(self, tmp_path, monkeypatch):
        home = tmp_path / "feral"
        home.mkdir(parents=True)
        (home / "SOUL.md").write_text("# Mine\n\nhand written\n")
        state = WizardState.load(home)

        monkeypatch.setattr(personality_step, "confirm", lambda *a, **kw: False)

        personality_step.run(state)

        assert (home / "SOUL.md").read_text() == "# Mine\n\nhand written\n"

    def test_custom_preset_uses_typed_text(self, tmp_path, monkeypatch):
        home = tmp_path / "feral"
        state = WizardState.load(home)

        monkeypatch.setattr(personality_step, "confirm", lambda *a, **kw: True)
        monkeypatch.setattr(
            personality_step, "ask_choice",
            lambda p, opts, **kw: next(o for o in opts if o.id == "custom"),
        )
        answers = iter(["Talk like a pirate.", "Polly"])
        monkeypatch.setattr(personality_step, "ask_text", lambda *a, **kw: next(answers))

        personality_step.run(state)

        soul = (home / "SOUL.md").read_text()
        assert "Talk like a pirate." in soul
        assert soul.startswith("# Polly")

    def test_presets_are_the_shared_catalogue(self):
        """Mined from the monolith rather than duplicated, so the two
        can't drift."""
        from cli.setup_wizard import PERSONALITY_PRESETS

        assert personality_step._presets() == dict(PERSONALITY_PRESETS)


# ---------------------------------------------------------------------------
# tool keys
# ---------------------------------------------------------------------------


class TestToolKeysStep:
    def test_selected_key_is_stored(self, tmp_path, monkeypatch):
        state = WizardState.load(tmp_path / "feral")
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)

        monkeypatch.setattr(tool_keys_step, "confirm", lambda *a, **kw: True)
        monkeypatch.setattr(
            tool_keys_step.ui_kit, "multi_select",
            lambda *a, **kw: ["TAVILY_API_KEY"],
        )
        monkeypatch.setattr(tool_keys_step, "ask_text", lambda *a, **kw: "tvly-123")

        tool_keys_step.run(state)

        assert state.credentials["TAVILY_API_KEY"] == "tvly-123"

    def test_unselected_keys_are_not_prompted(self, tmp_path, monkeypatch):
        state = WizardState.load(tmp_path / "feral")

        monkeypatch.setattr(tool_keys_step, "confirm", lambda *a, **kw: True)
        monkeypatch.setattr(tool_keys_step.ui_kit, "multi_select", lambda *a, **kw: [])
        monkeypatch.setattr(
            tool_keys_step, "ask_text",
            lambda *a, **kw: pytest.fail("nothing was selected; must not prompt"),
        )

        tool_keys_step.run(state)
        assert state.credentials == {}

    def test_companion_key_is_prompted(self, tmp_path, monkeypatch):
        """Spotify needs a client secret; storing the id alone yields a
        half-credential that fails at call time."""
        state = WizardState.load(tmp_path / "feral")
        monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
        monkeypatch.delenv("SPOTIFY_CLIENT_SECRET", raising=False)

        monkeypatch.setattr(tool_keys_step, "confirm", lambda *a, **kw: True)
        monkeypatch.setattr(
            tool_keys_step.ui_kit, "multi_select",
            lambda *a, **kw: ["SPOTIFY_CLIENT_ID"],
        )
        answers = iter(["id-abc", "secret-xyz"])
        monkeypatch.setattr(tool_keys_step, "ask_text", lambda *a, **kw: next(answers))

        tool_keys_step.run(state)

        assert state.credentials["SPOTIFY_CLIENT_ID"] == "id-abc"
        assert state.credentials["SPOTIFY_CLIENT_SECRET"] == "secret-xyz"

    def test_catalogue_is_the_shared_one(self):
        from cli.setup_wizard import TOOL_KEYS

        assert tool_keys_step._catalogue() == list(TOOL_KEYS)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


class TestStepWiring:
    def test_every_step_has_a_title(self):
        """The 'Step N of M' indicator falls back to a title-cased step
        name; every step we ship should have a real one."""
        import inspect

        import cli.setup as setup_pkg
        from cli.setup.state_machine import _STEP_TITLES, _NO_INDICATOR_STEPS

        source = inspect.getsource(setup_pkg._run_async)
        names = {
            line.split('("')[1].split('"')[0]
            for line in source.splitlines()
            if line.strip().startswith('("')
        }
        missing = names - set(_STEP_TITLES) - _NO_INDICATOR_STEPS
        assert not missing, f"steps with no title: {sorted(missing)}"

    def test_new_steps_are_in_the_flow(self):
        import inspect

        import cli.setup as setup_pkg

        source = inspect.getsource(setup_pkg._run_async)
        for name in ("capabilities", "personality", "tool_keys", "integrations"):
            assert f'("{name}"' in source, f"{name} is not wired into the wizard"
