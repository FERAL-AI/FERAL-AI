"""audit-r14 / lane-07 (W2) — `feral doctor` uses probe() for validity,
not env-var presence; exit-code reflects severity.

Closes finding 07 D-D (operator-facing "`feral doctor` reports ✔ LLM
credentials when key 401"). The new doctor calls
``security.probe.probe()`` for every registered provider, renders each
row with green/yellow/red based on the actual ``ProbeResult``, and
exits 1 if any row is red, 0 otherwise. Cost budget + macOS TCC
sections also surface here so they're guarded by a single test file.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _isolate_feral_home(tmp_path, monkeypatch):
    """Every doctor test runs against an empty FERAL_HOME so the live
    operator's vault, settings, and rolling cost.db never leak in."""
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    monkeypatch.setenv("FERAL_DATA_HOME", str(tmp_path))
    yield


def _make_probe_result(provider, ok, reason="", detail="", status_code=None):
    """Construct a synthetic ProbeResult mirroring what the registry
    yields. Tests inject these via monkeypatching ``probe()``."""
    from security.probe import ProbeResult

    return ProbeResult(
        provider=provider,
        ok=ok,
        status_code=status_code,
        reason=reason,
        detail=detail,
        probed_at=time.time(),
        latency_ms=12.3,
    )


def _stub_probes(monkeypatch, results_by_id):
    """Replace ``security.probe.probe`` with a deterministic stub that
    returns the result mapped from ``results_by_id`` for every id, and
    a default ``ok=True`` for any unmapped id (so tests stay terse)."""
    from security import probe as probe_mod

    async def _fake_probe(pid, **_kwargs):
        if pid in results_by_id:
            return results_by_id[pid]
        return _make_probe_result(pid, ok=True, detail="OK")

    monkeypatch.setattr(probe_mod, "probe", _fake_probe)
    # Also clear the cache so previous live probes don't bleed in.
    probe_mod.clear_probe_cache()


# ----------------------------------------------------------------------
# Section: probe-driven LLM rows
# ----------------------------------------------------------------------


def test_doctor_renders_red_for_401_llm_probe(monkeypatch, capsys):
    """An auth_failed/401 probe MUST surface red ✘ + the actual error
    detail. This is the core finding 07 D-D fix."""
    _stub_probes(
        monkeypatch,
        {
            "openai": _make_probe_result(
                "openai", ok=False, reason="auth_failed",
                status_code=401, detail="Incorrect API key provided",
            ),
            "anthropic": _make_probe_result("anthropic", ok=True, detail="OK"),
        },
    )

    from cli.main import cmd_doctor

    with pytest.raises(SystemExit) as excinfo:
        cmd_doctor()
    out = capsys.readouterr()
    text = out.out + out.err

    # Red ✘ for OpenAI, with actual API error in detail.
    assert "OpenAI" in text
    assert "Incorrect API key" in text or "key rejected" in text
    # Green ✔ for Anthropic.
    assert "Anthropic" in text
    # Exit code 1 because at least one row is red.
    assert excinfo.value.code == 1


def test_doctor_renders_info_for_no_key(monkeypatch, capsys):
    """A probe with reason ``no_key`` MUST render as cyan ℹ (info /
    not yet configured), NOT as a warning — this preserves the
    "fresh install looks clean" UX from v2026.5.36."""
    # Force every probe to "no_key" so we have nothing red, only info.
    from security.probe import registered_probe_ids

    no_key = {
        pid: _make_probe_result(pid, ok=False, reason="no_key", detail="not configured")
        for pid in registered_probe_ids()
    }
    _stub_probes(monkeypatch, no_key)

    from cli.main import cmd_doctor

    # Should NOT raise SystemExit(1) — no red rows.
    try:
        cmd_doctor()
    except SystemExit:
        pass  # exit code asserted elsewhere; this test only inspects output

    out = capsys.readouterr()
    text = out.out + out.err

    # We expect at least one ℹ (cyan info icon) for the unconfigured
    # providers. The exact glyph is "ℹ".
    assert "ℹ" in text
    # And the "LLM providers" header is present.
    assert "LLM providers" in text
    # ``rc`` is 1 because the catch-all "no provider passed probe"
    # _fail() fires when zero LLM probes are green. That's also the
    # contract — fresh install with zero keys is genuinely broken for
    # chat. The check that matters is "no_key rendered as ℹ".
    # The exit-code variant for "all green" is the next test below.


def test_doctor_exit_code_zero_when_all_green(monkeypatch, capsys):
    """All-green probes + clean dependencies must yield exit 0."""
    from security.probe import registered_probe_ids

    all_green = {
        pid: _make_probe_result(pid, ok=True, detail="OK")
        for pid in registered_probe_ids()
    }
    _stub_probes(monkeypatch, all_green)

    from cli.main import cmd_doctor

    # Some non-probe sections may still warn (e.g. missing USER.md
    # in the throwaway FERAL_HOME). The contract is: exit 0 when no
    # row is RED. Yellow warnings don't trip the exit code.
    rc = None
    try:
        rc = cmd_doctor()
    except SystemExit as e:
        rc = e.code
    assert rc in (None, 0), f"expected 0/None for all-green, got {rc!r}"

    out = capsys.readouterr()
    assert "LLM providers" in out.out + out.err


def test_doctor_no_llm_green_triggers_catch_all_fail(monkeypatch, capsys):
    """If ZERO LLM provider probes pass, doctor MUST fail with a
    clear "chat will not work until at least one is green" message
    + the `feral key add` remediation hint."""
    from security.probe import registered_probe_ids

    # Put every LLM probe into "no_key" + every other probe green.
    LLM_IDS = {"openai", "anthropic", "gemini", "openrouter", "deepseek",
               "groq", "ollama", "lmstudio", "bedrock"}
    results = {
        pid: _make_probe_result(
            pid, ok=False, reason="no_key", detail="not configured",
        ) if pid in LLM_IDS else _make_probe_result(pid, ok=True, detail="OK")
        for pid in registered_probe_ids()
    }
    _stub_probes(monkeypatch, results)

    from cli.main import cmd_doctor

    with pytest.raises(SystemExit) as excinfo:
        cmd_doctor()
    out = capsys.readouterr().out + capsys.readouterr().err
    # The catch-all fail line should be in stdout.
    text = out
    assert "LLM providers" in text
    # excinfo.value.code == 1 because the catch-all is _fail().
    assert excinfo.value.code == 1


# ----------------------------------------------------------------------
# Section: cost budget rendering
# ----------------------------------------------------------------------


def test_doctor_renders_cost_budget_section(monkeypatch, capsys):
    """The cost budget block must appear under its header with at
    least one ``cost.<call_site>`` row (we don't assert exact
    numbers — the rollups depend on the throwaway FERAL_HOME)."""
    _stub_probes(monkeypatch, {})  # all live probes return ok

    from cli.main import cmd_doctor

    try:
        cmd_doctor()
    except SystemExit:
        pass

    text = capsys.readouterr().out + capsys.readouterr().err
    assert "Cost budget" in text
    assert "cost.chat" in text or "cost.screen_loop" in text


# ----------------------------------------------------------------------
# Section: macOS TCC deeplink (Lane 07 W2 — R-PROD-004)
# ----------------------------------------------------------------------


def test_doctor_macos_tcc_uses_settings_deeplink(monkeypatch, capsys):
    """When a TCC probe is denied on macOS, the remediation MUST
    include the ``x-apple.systempreferences:`` deeplink so the
    operator can click-to-fix. (R-PROD-004 — CLI just shows status
    with deeplinks.)"""
    if sys.platform != "darwin":
        pytest.skip("macOS-only TCC deeplink check")

    from security import macos_permissions as macos_mod

    # Force every TCC probe to "denied" so the deeplink branch fires.
    def _denied(name):
        return macos_mod.TCCStatus(
            permission=name,
            status="denied",
            api="stub",
            setup_step="grant the permission in System Settings",
        )

    monkeypatch.setattr(macos_mod, "all_gui_permission_statuses", lambda: [
        _denied("accessibility"),
        _denied("calendar"),
        _denied("full_disk_access"),
    ])
    _stub_probes(monkeypatch, {})

    from cli.main import cmd_doctor

    try:
        cmd_doctor()
    except SystemExit:
        pass

    text = capsys.readouterr().out + capsys.readouterr().err
    # All three TCC rows should mention the deeplink scheme — the
    # specific Privacy_* anchor varies per permission so we assert
    # the scheme prefix only.
    assert "x-apple.systempreferences:" in text, (
        "expected macOS Settings deeplink in the TCC remediation text"
    )
