"""Tests for cli/ui_kit — the shared InquirerPy + Rich primitives.

Covers:

* The non-interactive fallback path (numeric select, plain text,
  silent password) which is what pytest itself drives because the
  test runner is never a TTY.
* The brand chrome (raccoon emoji + brand colour) renders even when
  Rich is missing — it must never raise.
* The masked-character path is gated on TTY + InquirerPy availability;
  we only assert the behaviour we can verify here (the silent fallback
  reads from getpass when stdin is non-tty).
* The asyncio nested-loop shim (``_run_inquirer_safely``) — this is
  what was broken in 2026.5.22: every prompt silently fell back to
  the typed numeric path because ``prompt_toolkit.Application.run()``
  detected a running asyncio loop and returned a coroutine rather
  than blocking.
"""

from __future__ import annotations

import asyncio
import io
import sys
import warnings

import pytest

from cli import ui_kit


# ---------------------------------------------------------------------------
# Brand chrome
# ---------------------------------------------------------------------------


class TestBrandChrome:
    def test_brand_emoji_is_raccoon(self):
        assert ui_kit.BRAND_EMOJI == "🦝"
        assert ui_kit.BRAND_COLOR == "cyan"

    def test_brand_panel_renders_without_rich(self, monkeypatch, capsys):
        monkeypatch.setattr(ui_kit, "_RICH_AVAILABLE", False)
        monkeypatch.setattr(ui_kit, "Panel", None)
        ui_kit.brand_panel("hello", body="world")
        captured = capsys.readouterr()
        assert "🦝" in captured.out
        assert "hello" in captured.out
        assert "world" in captured.out

    def test_brand_panel_rich_path_does_not_raise(self):
        # Just exercise the Rich path so it's covered.
        ui_kit.brand_panel("smoke", body="ok")

    def test_banner_line_includes_emoji(self, capsys):
        ui_kit.banner_line("hello world")
        captured = capsys.readouterr()
        assert "🦝" in captured.out
        assert "hello world" in captured.out


# ---------------------------------------------------------------------------
# Non-interactive fallback (the path pytest itself takes)
# ---------------------------------------------------------------------------


class TestMaskKey:
    """``security.vault_keys.mask_key`` is the shared "sk-…XYZW"
    display helper the wizard's "key already exists" UX uses to
    confirm the stored value without leaking it. Pin the contract."""

    def test_mask_key_short_secret(self):
        from security.vault_keys import mask_key

        assert mask_key("abc") == "***"
        assert mask_key("") == ""
        assert mask_key("   ") == ""

    def test_mask_key_normal_secret(self):
        from security.vault_keys import mask_key

        # 3-char head + 4-char tail.
        assert mask_key("sk-test-1234567890") == "sk-…7890"

    def test_mask_key_strips_whitespace(self):
        from security.vault_keys import mask_key

        assert mask_key("  sk-pasted-with-spaces-7890  ") == "sk-…7890"


class TestSelectFallback:
    def test_numeric_index(self, monkeypatch):
        monkeypatch.setattr(ui_kit, "_INQUIRER_AVAILABLE", False)
        monkeypatch.setattr(sys, "stdin", io.StringIO("2\n"))
        result = ui_kit.select(
            "pick one", ["alpha", "beta", "gamma"]
        )
        assert result == "beta"

    def test_default_on_empty_line(self, monkeypatch):
        monkeypatch.setattr(ui_kit, "_INQUIRER_AVAILABLE", False)
        monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))
        result = ui_kit.select(
            "pick", ["a", "b", "c"], default="b"
        )
        assert result == "b"

    def test_value_match_by_name(self, monkeypatch):
        monkeypatch.setattr(ui_kit, "_INQUIRER_AVAILABLE", False)
        monkeypatch.setattr(sys, "stdin", io.StringIO("gamma\n"))
        result = ui_kit.select("pick", ["alpha", "beta", "gamma"])
        assert result == "gamma"

    def test_dict_choices_resolve_to_value(self, monkeypatch):
        monkeypatch.setattr(ui_kit, "_INQUIRER_AVAILABLE", False)
        monkeypatch.setattr(sys, "stdin", io.StringIO("1\n"))
        result = ui_kit.select(
            "pick",
            [{"name": "First option", "value": "first"},
             {"name": "Second", "value": "second"}],
        )
        assert result == "first"

    def test_invalid_then_valid(self, monkeypatch):
        monkeypatch.setattr(ui_kit, "_INQUIRER_AVAILABLE", False)
        monkeypatch.setattr(sys, "stdin", io.StringIO("99\nbeta\n"))
        result = ui_kit.select("pick", ["alpha", "beta"])
        assert result == "beta"


class TestFuzzySelectFallback:
    def test_falls_back_to_select(self, monkeypatch):
        # Same fallback as plain select when InquirerPy or TTY missing.
        monkeypatch.setattr(ui_kit, "_INQUIRER_AVAILABLE", False)
        monkeypatch.setattr(sys, "stdin", io.StringIO("2\n"))
        result = ui_kit.fuzzy_select(
            "pick", ["alpha", "beta", "gamma"]
        )
        assert result == "beta"


class TestPasswordFallback:
    def test_silent_fallback_uses_getpass(self, monkeypatch):
        monkeypatch.setattr(ui_kit, "_INQUIRER_AVAILABLE", False)

        seen_label: dict[str, str] = {}

        def fake_getpass(label):
            seen_label["label"] = label
            return "sk-test-1234"

        monkeypatch.setattr("getpass.getpass", fake_getpass)
        result = ui_kit.password("Enter your key")
        assert result == "sk-test-1234"
        # The fallback annotates the label so the operator can see they
        # are NOT in the masked path — truthfulness rule.
        assert "non-interactive" in seen_label["label"]

    def test_empty_value_rejected_when_required(self, monkeypatch):
        monkeypatch.setattr(ui_kit, "_INQUIRER_AVAILABLE", False)

        responses = iter(["", "real-key"])
        monkeypatch.setattr("getpass.getpass", lambda _label: next(responses))
        result = ui_kit.password("k", allow_empty=False)
        assert result == "real-key"


class TestConfirmFallback:
    @pytest.mark.parametrize(
        "answer,expected",
        [("y\n", True), ("yes\n", True), ("n\n", False), ("no\n", False)],
    )
    def test_y_n(self, monkeypatch, answer, expected):
        monkeypatch.setattr(ui_kit, "_INQUIRER_AVAILABLE", False)
        monkeypatch.setattr(sys, "stdin", io.StringIO(answer))
        assert ui_kit.confirm("ok?", default=False) is expected

    def test_default_on_empty(self, monkeypatch):
        monkeypatch.setattr(ui_kit, "_INQUIRER_AVAILABLE", False)
        monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))
        assert ui_kit.confirm("ok?", default=True) is True


class TestTextFallback:
    def test_default_on_empty(self, monkeypatch):
        monkeypatch.setattr(ui_kit, "_INQUIRER_AVAILABLE", False)
        monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))
        result = ui_kit.text("name", default="anonymous")
        assert result == "anonymous"

    def test_returns_typed_value(self, monkeypatch):
        monkeypatch.setattr(ui_kit, "_INQUIRER_AVAILABLE", False)
        monkeypatch.setattr(sys, "stdin", io.StringIO("Mahmoud\n"))
        result = ui_kit.text("name", default="anonymous")
        assert result == "Mahmoud"


# ---------------------------------------------------------------------------
# TTY hint
# ---------------------------------------------------------------------------


class TestTTYHint:
    def test_no_op_when_interactive(self, monkeypatch, capsys):
        monkeypatch.setattr(ui_kit, "_is_interactive", lambda: True)
        ui_kit.warn_non_interactive_setup_hint()
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_prints_ssh_t_hint_when_non_tty(self, monkeypatch, capsys):
        monkeypatch.setattr(ui_kit, "_is_interactive", lambda: False)
        ui_kit.warn_non_interactive_setup_hint()
        captured = capsys.readouterr()
        # Either Rich path (with [cyan]🦝[/]) or plain — both must mention
        # the ssh -t invocation truthfully.
        assert "ssh -t" in captured.out
        assert "🦝" in captured.out


# ---------------------------------------------------------------------------
# Asyncio nested-loop shim — the actual P0 fix
# ---------------------------------------------------------------------------


class _FakePrompt:
    """Fake InquirerPy prompt object — just exposes ``execute()``."""

    def __init__(self, value):
        self._value = value
        self.execute_called = False

    def execute(self):
        self.execute_called = True
        return self._value


class TestRunInquirerSafely:
    """Direct tests of the worker-thread shim."""

    def test_no_running_loop_calls_directly(self):
        prompt = _FakePrompt("direct")
        result = ui_kit._run_inquirer_safely(prompt.execute)
        assert result == "direct"
        assert prompt.execute_called is True

    def test_inside_running_loop_runs_in_worker(self):
        prompt = _FakePrompt("via-worker")

        async def driver():
            return ui_kit._run_inquirer_safely(prompt.execute)

        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            result = asyncio.run(driver())
        assert result == "via-worker"
        assert prompt.execute_called is True

    def test_worker_exception_propagates(self):
        def boom():
            raise RuntimeError("from worker")

        async def driver():
            return ui_kit._run_inquirer_safely(boom)

        with pytest.raises(RuntimeError, match="from worker"):
            asyncio.run(driver())


class TestAsyncioNested:
    """End-to-end: drive the public ``ui_kit.X()`` prompts inside an
    ``asyncio.run`` and assert no ``RuntimeWarning`` leaks and the
    return value flows back correctly. Mocks InquirerPy to return a
    known value without needing a real TTY.
    """

    def _patch_interactive(self, monkeypatch):
        monkeypatch.setattr(ui_kit, "_INQUIRER_AVAILABLE", True)
        monkeypatch.setattr(ui_kit, "_is_interactive", lambda: True)

    def test_select_inside_running_loop_does_not_warn(self, monkeypatch):
        self._patch_interactive(monkeypatch)
        # ui_kit.select uses inquirer.checkbox under the hood — return a
        # one-element list to satisfy the validator.
        captured: dict[str, object] = {}

        def fake_checkbox(**kwargs):
            captured["kwargs"] = kwargs
            return _FakePrompt(["openai"])

        monkeypatch.setattr(ui_kit.inquirer, "checkbox", fake_checkbox)

        async def driver():
            return ui_kit.select(
                "Pick one",
                [{"name": "OpenAI", "value": "openai"},
                 {"name": "Ollama", "value": "ollama"}],
            )

        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            result = asyncio.run(driver())
        assert result == "openai"
        # The picker was called with the brand emoji + a single-selection
        # validator (so the legacy enter-on-cursor pattern can't slip back in).
        assert captured["kwargs"]["qmark"] == ui_kit.BRAND_EMOJI
        assert captured["kwargs"]["validate"]([{"x"}]) is True
        assert captured["kwargs"]["validate"]([]) is False
        assert captured["kwargs"]["validate"](["a", "b"]) is False

    def test_password_inside_running_loop_does_not_warn(self, monkeypatch):
        self._patch_interactive(monkeypatch)

        def fake_secret(**kwargs):
            return _FakePrompt("sk-xxxx")

        monkeypatch.setattr(ui_kit.inquirer, "secret", fake_secret)

        async def driver():
            return ui_kit.password("API key")

        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            result = asyncio.run(driver())
        assert result == "sk-xxxx"

    def test_confirm_inside_running_loop_does_not_warn(self, monkeypatch):
        self._patch_interactive(monkeypatch)

        def fake_confirm(**kwargs):
            return _FakePrompt(True)

        monkeypatch.setattr(ui_kit.inquirer, "confirm", fake_confirm)

        async def driver():
            return ui_kit.confirm("ok?", default=False)

        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            result = asyncio.run(driver())
        assert result is True


class TestSpaceMarkSemantics:
    """Verify the new space-to-mark + enter-to-confirm contract."""

    def test_select_unwraps_single_item_from_checkbox(self, monkeypatch):
        monkeypatch.setattr(ui_kit, "_INQUIRER_AVAILABLE", True)
        monkeypatch.setattr(ui_kit, "_is_interactive", lambda: True)

        def fake_checkbox(**kwargs):
            return _FakePrompt(["chosen-id"])

        monkeypatch.setattr(ui_kit.inquirer, "checkbox", fake_checkbox)
        result = ui_kit.select("pick", ["a", "b", "chosen-id"])
        assert result == "chosen-id"

    def test_fuzzy_select_unwraps_single_item(self, monkeypatch):
        monkeypatch.setattr(ui_kit, "_INQUIRER_AVAILABLE", True)
        monkeypatch.setattr(ui_kit, "_is_interactive", lambda: True)

        def fake_fuzzy(**kwargs):
            assert kwargs.get("multiselect") is True, "fuzzy_select must be multiselect"
            return _FakePrompt(["gpt-4o"])

        monkeypatch.setattr(ui_kit.inquirer, "fuzzy", fake_fuzzy)
        result = ui_kit.fuzzy_select(
            "pick model", ["gpt-3.5", "gpt-4o", "claude-3-opus"]
        )
        assert result == "gpt-4o"

    def test_default_pre_marks_choice(self, monkeypatch):
        """``default`` should land the user on the option already enabled
        so they can press enter immediately to accept it."""
        monkeypatch.setattr(ui_kit, "_INQUIRER_AVAILABLE", True)
        monkeypatch.setattr(ui_kit, "_is_interactive", lambda: True)

        captured = {}

        def fake_checkbox(**kwargs):
            captured["choices"] = kwargs["choices"]
            return _FakePrompt(["beta"])

        monkeypatch.setattr(ui_kit.inquirer, "checkbox", fake_checkbox)
        ui_kit.select("pick", ["alpha", "beta", "gamma"], default="beta")

        # Each Choice exposes .value + .enabled in InquirerPy's API.
        enabled_values = [
            c.value for c in captured["choices"] if getattr(c, "enabled", False)
        ]
        assert enabled_values == ["beta"]
