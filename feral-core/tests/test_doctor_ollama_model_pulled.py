"""`feral doctor` flags a configured-but-unpulled Ollama model.

Operator report: a brain configured with ``llm.provider=ollama`` +
``llm.model=<not-pulled>`` produced ``Switched LLM to ollama/<name>
(available=True)`` immediately followed by a 404 on
``/v1/chat/completions``. The Ollama probe row in doctor only checked
that ``/api/tags`` was reachable, so the install reported green while
chat was guaranteed to fail.

These tests assert the post-fix behaviour:
  * configured + pulled  -> green ✔
  * configured + missing -> red ✘ with a clear "pull the model OR pick
    an installed one" remediation
  * provider != ollama   -> no row at all (cloud paths are covered by
    the existing probe section)
  * Ollama unreachable   -> no row (the LLM probe row above already
    explains the outage; doctor does not double-warn)
"""

from __future__ import annotations

import time

import pytest


@pytest.fixture(autouse=True)
def _clear_llm_env(monkeypatch):
    """``FERAL_LLM_PROVIDER`` / ``FERAL_LLM_MODEL`` env overrides would
    win over our settings.json fixtures and skew the check. The
    project-wide ``isolate_feral_home`` autouse fixture already points
    FERAL_HOME at a throwaway directory, so we only need to clear the
    env layer here."""
    monkeypatch.delenv("FERAL_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("FERAL_LLM_MODEL", raising=False)
    yield


def _make_probe_result(provider, ok, reason="", detail="", status_code=None):
    from security.probe import ProbeResult

    return ProbeResult(
        provider=provider, ok=ok, status_code=status_code,
        reason=reason, detail=detail,
        probed_at=time.time(), latency_ms=12.3,
    )


def _stub_probes_all_ok(monkeypatch):
    from security import probe as probe_mod

    async def _fake_probe(pid, **_kwargs):
        return _make_probe_result(pid, ok=True, detail="OK")

    monkeypatch.setattr(probe_mod, "probe", _fake_probe)
    probe_mod.clear_probe_cache()


def _write_settings(*, provider: str, model: str) -> None:
    """Write a synthetic ``settings.json`` into the resolved FERAL_HOME.

    The project-wide ``isolate_feral_home`` autouse fixture sets
    ``FERAL_HOME`` to a unique tmp directory and creates it. We resolve
    the path via :func:`config.loader.feral_home` so the settings land
    exactly where ``load_settings`` looks for them, regardless of
    whether the autouse fixture used ``tmp_path`` or a nested
    subdirectory.
    """
    import json
    from config.loader import feral_home

    home = feral_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        json.dumps({"llm": {"provider": provider, "model": model}}),
    )


def test_doctor_passes_when_configured_ollama_model_is_pulled(
    monkeypatch, capsys,
):
    _stub_probes_all_ok(monkeypatch)
    _write_settings(provider="ollama", model="llama3.1")

    from cli import main as cli_main

    monkeypatch.setattr(
        cli_main, "_ollama_pulled_models_sync",
        lambda *a, **kw: ["llama3.1:8b", "mistral:latest"],
    )

    try:
        cli_main.cmd_doctor()
    except SystemExit:
        pass

    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert "Ollama model 'llama3.1'" in text, text[:4000]
    assert "pulled and ready" in text


def test_doctor_fails_when_configured_ollama_model_not_pulled(
    monkeypatch, capsys,
):
    _stub_probes_all_ok(monkeypatch)
    _write_settings(provider="ollama", model="gemma4")

    from cli import main as cli_main

    monkeypatch.setattr(
        cli_main, "_ollama_pulled_models_sync",
        lambda *a, **kw: ["llama3.1:8b", "mistral:latest"],
    )

    with pytest.raises(SystemExit) as excinfo:
        cli_main.cmd_doctor()

    text = capsys.readouterr().out
    assert "Ollama model 'gemma4'" in text
    assert "not pulled" in text
    assert "ollama pull gemma4" in text
    # Installed-model hint so the operator can pick something that exists.
    assert "llama3.1" in text or "mistral" in text
    assert excinfo.value.code == 1


def test_doctor_skips_check_when_provider_is_not_ollama(
    monkeypatch, capsys,
):
    _stub_probes_all_ok(monkeypatch)
    _write_settings(provider="openai", model="gpt-5")

    from cli import main as cli_main

    called: list[bool] = []

    def _fake_lookup(*_args, **_kwargs):
        called.append(True)
        return ["llama3.1"]

    monkeypatch.setattr(cli_main, "_ollama_pulled_models_sync", _fake_lookup)

    try:
        cli_main.cmd_doctor()
    except SystemExit:
        pass

    text = capsys.readouterr().out
    # No Ollama-model row should appear.
    assert "Ollama model" not in text
    # And we never even hit /api/tags because the active provider isn't ollama.
    assert called == []


def test_doctor_quiet_when_ollama_unreachable(
    monkeypatch, capsys,
):
    _stub_probes_all_ok(monkeypatch)
    _write_settings(provider="ollama", model="llama3.1")

    from cli import main as cli_main

    monkeypatch.setattr(
        cli_main, "_ollama_pulled_models_sync",
        lambda *a, **kw: None,
    )

    try:
        cli_main.cmd_doctor()
    except SystemExit:
        pass

    text = capsys.readouterr().out
    # When Ollama is down, the LLM probe row above already surfaces it
    # (status 0). The configured-model check stays quiet to avoid a
    # second yellow / red row for the same root cause.
    assert "Ollama model 'llama3.1'" not in text
