"""Credential validation + UX tests for the setup wizard.

The wizard registered 31 probes in ``security.probe`` and called
exactly one of them (the voice preflight table). Home Assistant tokens,
channel bot tokens, and any key typed *during* the voice step were
persisted with zero verification: setup reported success and the
operator discovered the bad credential when the integration silently
did nothing.

Also covers the secret-prompt one-way door: ``ask_text(secret=True)``
let a bare ``KeyboardInterrupt`` escape to the top of the wizard, so
Ctrl+C on any API-key prompt discarded the run instead of stepping back.
"""

from __future__ import annotations

import pytest

from cli.setup import helpers
from cli.setup.state import WizardState
from cli.setup.steps import channels as channels_step
from cli.setup.steps import home_assistant as ha_step


class _FakeProbeResult:
    def __init__(self, ok, *, status_code=200, reason="", detail=""):
        self.ok = ok
        self.status_code = status_code
        self.reason = reason
        self.detail = detail


def _install_probe(monkeypatch, results):
    """Patch ``security.probe.probe`` with a scripted result queue."""
    calls = []

    async def _fake_probe(provider_id, *, vault=None, force=False):
        calls.append(provider_id)
        outcome = results.pop(0) if isinstance(results, list) else results
        return outcome

    import security.probe as probe_mod
    monkeypatch.setattr(probe_mod, "probe", _fake_probe)
    return calls


# ---------------------------------------------------------------------------
# Home Assistant
# ---------------------------------------------------------------------------


class TestHomeAssistantValidation:
    @pytest.mark.asyncio
    async def test_token_is_probed(self, tmp_path, monkeypatch):
        state = WizardState.load(tmp_path / "feral")
        calls = _install_probe(monkeypatch, _FakeProbeResult(True))

        monkeypatch.setattr(ha_step, "confirm", lambda *a, **kw: True)
        answers = iter(["http://ha.local:8123", "tok-abc"])
        monkeypatch.setattr(ha_step, "ask_text", lambda *a, **kw: next(answers))

        await ha_step.run(state)

        assert calls == ["home_assistant"], "the HA probe was never called"

    @pytest.mark.asyncio
    async def test_bad_token_offers_retry(self, tmp_path, monkeypatch):
        state = WizardState.load(tmp_path / "feral")
        _install_probe(monkeypatch, [
            _FakeProbeResult(False, status_code=401, reason="unauthorized"),
            _FakeProbeResult(True),
        ])

        confirms = iter([True, True])  # connect? -> yes; re-enter? -> yes
        monkeypatch.setattr(ha_step, "confirm", lambda *a, **kw: next(confirms))
        answers = iter([
            "http://ha.local:8123", "wrong",
            "http://ha.local:8123", "right",
        ])
        monkeypatch.setattr(ha_step, "ask_text", lambda *a, **kw: next(answers))

        await ha_step.run(state)

        assert state.credentials["HA_TOKEN"] == "right"

    @pytest.mark.asyncio
    async def test_writes_the_env_names_the_runtime_reads(self, tmp_path, monkeypatch):
        """integrations/home_assistant.py reads HA_URL / HA_TOKEN. The
        wizard used to write HOME_ASSISTANT_* — a namespace nothing
        reads, so a correct token still left the integration dark."""
        state = WizardState.load(tmp_path / "feral")
        _install_probe(monkeypatch, _FakeProbeResult(True))

        monkeypatch.setattr(ha_step, "confirm", lambda *a, **kw: True)
        answers = iter(["http://ha.local:8123", "tok-abc"])
        monkeypatch.setattr(ha_step, "ask_text", lambda *a, **kw: next(answers))

        await ha_step.run(state)

        assert state.credentials["HA_URL"] == "http://ha.local:8123"
        assert state.credentials["HA_TOKEN"] == "tok-abc"
        assert "HOME_ASSISTANT_TOKEN" not in state.credentials


# ---------------------------------------------------------------------------
# Messaging channels
# ---------------------------------------------------------------------------


class TestChannelValidation:
    @pytest.mark.asyncio
    async def test_telegram_token_is_probed(self, tmp_path, monkeypatch):
        state = WizardState.load(tmp_path / "feral")
        calls = _install_probe(monkeypatch, _FakeProbeResult(True))

        # connect channels? yes; telegram? yes; everything else no.
        confirms = iter([True, True, False, False, False])
        monkeypatch.setattr(channels_step, "confirm", lambda *a, **kw: next(confirms))
        monkeypatch.setattr(channels_step, "ask_text", lambda *a, **kw: "123:ABC")

        await channels_step.run(state)

        assert calls == ["telegram"]
        assert state.get_setting("channels", "configured") == ["telegram"]

    @pytest.mark.asyncio
    async def test_rejected_token_can_be_re_entered(self, tmp_path, monkeypatch):
        state = WizardState.load(tmp_path / "feral")
        _install_probe(monkeypatch, [
            _FakeProbeResult(False, status_code=401, reason="unauthorized"),
            _FakeProbeResult(True),
        ])

        # connect? yes; telegram? yes; re-enter? yes; discord/slack/wa no.
        confirms = iter([True, True, True, False, False, False])
        monkeypatch.setattr(channels_step, "confirm", lambda *a, **kw: next(confirms))
        tokens = iter(["bad-token", "good-token"])
        monkeypatch.setattr(channels_step, "ask_text", lambda *a, **kw: next(tokens))

        await channels_step.run(state)

        assert state.credentials["FERAL_TELEGRAM_BOT_TOKEN"] == "good-token"

    @pytest.mark.asyncio
    async def test_whatsapp_reports_no_probe_rather_than_faking_one(
        self, tmp_path, monkeypatch,
    ):
        state = WizardState.load(tmp_path / "feral")
        calls = _install_probe(monkeypatch, _FakeProbeResult(True))

        # connect? yes; telegram/discord/slack no; whatsapp yes.
        confirms = iter([True, False, False, False, True])
        monkeypatch.setattr(channels_step, "confirm", lambda *a, **kw: next(confirms))
        monkeypatch.setattr(channels_step, "ask_text", lambda *a, **kw: "wa-value")

        await channels_step.run(state)

        assert calls == [], "whatsapp has no registered probe; none should run"
        assert state.get_setting("channels", "configured") == ["whatsapp"]


# ---------------------------------------------------------------------------
# probe_and_report
# ---------------------------------------------------------------------------


class TestProbeAndReport:
    @pytest.mark.asyncio
    async def test_unknown_provider_is_reported_honestly(self, monkeypatch):
        _install_probe(monkeypatch, _FakeProbeResult(
            False, status_code=None, reason="unknown_provider",
        ))
        ok, detail = await helpers.probe_and_report("nope")
        assert ok is False
        assert detail == "unknown_provider"

    @pytest.mark.asyncio
    async def test_ok_result_returns_true(self, monkeypatch):
        _install_probe(monkeypatch, _FakeProbeResult(True, detail="200 OK"))
        ok, _detail = await helpers.probe_and_report("telegram")
        assert ok is True


# ---------------------------------------------------------------------------
# Secret-prompt navigation
# ---------------------------------------------------------------------------


class TestSecretPromptNavigation:
    def test_ctrl_c_maps_to_back_not_a_crash(self, monkeypatch):
        def _boom(*_a, **_kw):
            raise KeyboardInterrupt()

        monkeypatch.setattr(helpers.ui_kit, "password", _boom)
        with pytest.raises(helpers.BackNavigation):
            helpers.ask_text("API key", secret=True, allow_empty=False)

    def test_eof_maps_to_quit(self, monkeypatch):
        def _boom(*_a, **_kw):
            raise EOFError()

        monkeypatch.setattr(helpers.ui_kit, "password", _boom)
        with pytest.raises(helpers.QuitNavigation):
            helpers.ask_text("API key", secret=True, allow_empty=False)

    def test_typed_back_sentinel(self, monkeypatch):
        monkeypatch.setattr(helpers.ui_kit, "password", lambda *a, **kw: ":back")
        with pytest.raises(helpers.BackNavigation):
            helpers.ask_text("API key", secret=True, allow_empty=False)

    def test_typed_quit_sentinel(self, monkeypatch):
        monkeypatch.setattr(helpers.ui_kit, "password", lambda *a, **kw: ":quit")
        with pytest.raises(helpers.QuitNavigation):
            helpers.ask_text("API key", secret=True, allow_empty=False)

    def test_a_real_key_is_returned_untouched(self, monkeypatch):
        monkeypatch.setattr(helpers.ui_kit, "password", lambda *a, **kw: "sk-back-quit")
        assert helpers.ask_text("API key", secret=True) == "sk-back-quit"
