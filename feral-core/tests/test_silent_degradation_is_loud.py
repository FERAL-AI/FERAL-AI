"""Degrading is allowed. Degrading quietly is not.

Every test here pins one place where the brain used to lose a capability
and say nothing an operator could see: a debug-level swallow, a boolean
return thrown away, a security check fed a value that made it a no-op, or
an ImportError from FERAL's own code filed as a missing optional extra.

Each test asserts the LOUD signal (the log record, its level, and enough
of its text to act on), not that the happy path still works. Against the
code before this change every test in this module fails, and it fails on
the assertion about the warning rather than on an exception, because the
old behaviour was to carry on successfully with less.
"""

from __future__ import annotations

import logging

import pytest


# ── 1. Boot / security: the bind-host resolver failing must not silence
#       the FERAL_LOCAL_BYPASS check ────────────────────────────────────


class TestLocalBypassSafetyCheck:
    """``api.server.check_local_bypass_safety``.

    ``brain_bind_host`` reads ~/.feral/settings.json. When that raised,
    the old code substituted ``""``, and ``warn_if_unsafe_bypass`` treats
    ``""`` as loopback-safe and returns None. So a corrupt settings file
    disabled the one warning that exists to say "this brain is reachable
    from the network with authentication turned off", and logged nothing
    at any level while doing it.
    """

    def test_resolver_failure_is_reported(self, monkeypatch, caplog):
        import api.server as server

        def _boom() -> str:
            raise ValueError("settings.json: Expecting value: line 1 column 1")

        monkeypatch.setattr(server, "brain_bind_host", _boom)
        monkeypatch.setattr(server, "local_bypass_enabled", lambda: False)

        with caplog.at_level(logging.WARNING, logger=server.logger.name):
            server.check_local_bypass_safety()

        records = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert records, (
            "the bind-host resolver raised and nothing was logged: the "
            "FERAL_LOCAL_BYPASS safety check silently did not run"
        )
        text = " ".join(r.getMessage() for r in records)
        assert "bind host" in text
        assert "FERAL_LOCAL_BYPASS" in text
        assert "settings.json" in text, "the operator needs the file to look at"

    def test_unknown_bind_with_bypass_on_is_called_out(self, monkeypatch, caplog):
        """The dangerous combination, reported even though it is unproven.

        Bypass on plus an unknown bind host may be perfectly safe (the
        default bind is loopback). It may also be an open brain. Saying
        nothing resolves that ambiguity in the direction that costs the
        operator their front door.
        """
        import api.server as server

        monkeypatch.setattr(
            server, "brain_bind_host",
            lambda: (_ for _ in ()).throw(OSError("permission denied")),
        )
        monkeypatch.setattr(server, "local_bypass_enabled", lambda: True)

        with caplog.at_level(logging.WARNING, logger=server.logger.name):
            server.check_local_bypass_safety()

        text = " ".join(
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        )
        assert "UNAUTHENTICATED" in text, (
            "bypass is on and the bind host is unknown; that combination has "
            "to be named, not assumed loopback"
        )

    def test_healthy_resolver_still_delegates(self, monkeypatch):
        """No new noise on the normal path: a resolvable loopback bind
        goes through ``warn_if_unsafe_bypass`` and says nothing."""
        import api.server as server

        seen: list[str] = []
        monkeypatch.setattr(server, "brain_bind_host", lambda: "127.0.0.1")
        monkeypatch.setattr(
            server, "warn_if_unsafe_bypass", lambda host: seen.append(host),
        )
        server.check_local_bypass_safety()
        assert seen == ["127.0.0.1"]


# ── 2. Boot / doctor truthfulness: the integration probe sweeper ────────


class _FakeState:
    vault = None

    def register_background_task(self, task):  # pragma: no cover - trivial
        return task


class TestProbeSweeperStart:
    """``api.server.start_probe_sweeper``.

    Without this loop running, ``integration.connected`` falls back to
    "a token string exists", which is what the sweeper module was written
    to replace. The old call site logged a failure at debug and discarded
    ``ensure_started``'s return value entirely, so "the sweeper is not
    running" was indistinguishable from "the sweeper is running".
    """

    def test_exception_is_a_warning_naming_the_consequence(
        self, monkeypatch, caplog,
    ):
        import api.server as server
        from integrations import probe_sweeper

        def _boom(**_kwargs):
            raise RuntimeError("no running event loop")

        monkeypatch.setattr(probe_sweeper, "ensure_started", _boom)

        with caplog.at_level(logging.DEBUG, logger=server.logger.name):
            assert server.start_probe_sweeper(_FakeState()) is False

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "sweeper failure was not reported above debug"
        text = " ".join(r.getMessage() for r in warnings)
        assert "token presence" in text, (
            "the message has to say what the badges now mean, not just that "
            "something failed"
        )
        assert "revoked" in text

    def test_refusal_without_an_exception_is_still_reported(
        self, monkeypatch, caplog,
    ):
        """``ensure_started`` returns False without raising when it has no
        loop to attach to. That return used to be dropped on the floor."""
        import api.server as server
        from integrations import probe_sweeper

        monkeypatch.setattr(probe_sweeper, "ensure_started", lambda **_k: False)
        monkeypatch.setattr(probe_sweeper, "sweep_interval_seconds", lambda: 45.0)

        with caplog.at_level(logging.DEBUG, logger=server.logger.name):
            assert server.start_probe_sweeper(_FakeState()) is False

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, (
            "ensure_started returned False and the caller said nothing"
        )
        assert "token presence" in " ".join(r.getMessage() for r in warnings)

    def test_deliberate_disable_is_info_not_warning(self, monkeypatch, caplog):
        """Switching the sweeper off is a supported choice, so it is not a
        warning. It is still stated, because the consequence is identical
        and nothing else in the process mentions it."""
        import api.server as server
        from integrations import probe_sweeper

        monkeypatch.setattr(probe_sweeper, "ensure_started", lambda **_k: False)
        monkeypatch.setattr(probe_sweeper, "sweep_interval_seconds", lambda: 0.0)

        with caplog.at_level(logging.INFO, logger=server.logger.name):
            assert server.start_probe_sweeper(_FakeState()) is False

        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        infos = [r for r in caplog.records if r.levelno == logging.INFO]
        assert infos and "token presence" in " ".join(
            r.getMessage() for r in infos
        )

    def test_success_is_quiet(self, monkeypatch, caplog):
        import api.server as server
        from integrations import probe_sweeper

        monkeypatch.setattr(probe_sweeper, "ensure_started", lambda **_k: True)
        with caplog.at_level(logging.INFO, logger=server.logger.name):
            assert server.start_probe_sweeper(_FakeState()) is True
        assert not caplog.records


# ── 3. A refresher that stops refreshing ────────────────────────────────


class _RaisingCatalog:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def refresh_async(self):
        raise self._exc


class _OKCatalog:
    def __init__(self) -> None:
        self.calls = 0

    async def refresh_async(self):
        self.calls += 1


class TestProviderCatalogRefresh:
    """``api.server.refresh_provider_catalog_once``.

    The whole point of the loop is that the Settings model picker does not
    go stale. It logged its own failure at debug, so a refresher that had
    not succeeded once since boot looked exactly like one that had.
    """

    @pytest.mark.asyncio
    async def test_failure_warns_and_counts(self, caplog):
        import api.server as server

        catalog = _RaisingCatalog(RuntimeError("OPENAI_API_KEY is not set"))
        with caplog.at_level(logging.DEBUG, logger=server.logger.name):
            count = await server.refresh_provider_catalog_once(catalog, 0)

        assert count == 1
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "a failed catalog refresh stayed at debug"
        text = warnings[-1].getMessage()
        assert "OPENAI_API_KEY is not set" in text, "the cause must survive"
        assert "stale" in text or "old" in text

    @pytest.mark.asyncio
    async def test_repeated_failure_reports_growing_staleness(self, caplog):
        """Third failure in a row means the served list is at least 18h
        old. A flat per-cycle message could not say that."""
        import api.server as server

        catalog = _RaisingCatalog(TimeoutError("connect timeout"))
        with caplog.at_level(logging.WARNING, logger=server.logger.name):
            count = await server.refresh_provider_catalog_once(catalog, 2)

        assert count == 3
        assert "18h" in caplog.records[-1].getMessage()

    @pytest.mark.asyncio
    async def test_success_resets_and_stays_quiet(self, caplog):
        import api.server as server

        catalog = _OKCatalog()
        with caplog.at_level(logging.INFO, logger=server.logger.name):
            count = await server.refresh_provider_catalog_once(catalog, 4)

        assert count == 0
        assert catalog.calls == 1
        assert not caplog.records


# ── 4. Credentials at boot: an unreadable vault ─────────────────────────


class TestBootCredentialVaultFallback:
    """``api.state.BrainState._load_stored_credentials``.

    The default-namespace vault is where ``/api/config/credentials`` and
    ``/api/llm/providers/{id}/configure`` write, so it holds every key the
    operator entered through the UI. The fallback that reads it back at
    boot swallowed every failure into one debug line, and the observable
    result was a brain that boots clean and then answers "no API key
    configured" to a user looking at their key in Settings.
    """

    def test_vault_construction_failure_is_a_warning(self, monkeypatch, caplog):
        import api.state as state_mod
        import security.vault as vault_mod

        for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            monkeypatch.delenv(name, raising=False)

        class _UnopenableVault:
            def __init__(self, *a, **kw):
                raise OSError("keychain item not found")

        monkeypatch.setattr(vault_mod, "BlindVault", _UnopenableVault)

        with caplog.at_level(logging.DEBUG, logger=state_mod.logger.name):
            state_mod.BrainState._load_stored_credentials()

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "an unopenable vault produced no warning at boot"
        text = " ".join(r.getMessage() for r in warnings)
        assert "keychain item not found" in text, "the cause must survive"
        assert "OPENAI_API_KEY" in text, (
            "the operator needs to know WHICH credentials are now unset"
        )

    def test_one_unreadable_entry_does_not_strand_the_rest(
        self, monkeypatch, caplog,
    ):
        """A single undecryptable entry used to abort the whole loop
        through one outer handler, so every key after it in the list was
        silently skipped as well."""
        import api.state as state_mod
        import security.vault as vault_mod

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        class _PartialVault:
            def __init__(self, *a, **kw):
                pass

            def retrieve(self, key):
                if key == "OPENAI_API_KEY":
                    raise ValueError("InvalidTag: ciphertext failed AEAD check")
                if key == "ANTHROPIC_API_KEY":
                    return "sk-ant-recovered"
                return None

        monkeypatch.setattr(vault_mod, "BlindVault", _PartialVault)

        import os

        with caplog.at_level(logging.DEBUG, logger=state_mod.logger.name):
            state_mod.BrainState._load_stored_credentials()

        try:
            assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-recovered", (
                "a failure on an earlier key stranded a readable later one"
            )
        finally:
            os.environ.pop("ANTHROPIC_API_KEY", None)

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        text = " ".join(r.getMessage() for r in warnings)
        assert "OPENAI_API_KEY" in text
        assert "InvalidTag" in text or "ValueError" in text


# ── 5. Boot report: "Missing dependency" is a claim about a third party ──


class TestBootReportImportClassification:
    """``api.boot_report.boot_subsystem``.

    An ImportError was always recorded as SKIPPED / "Missing dependency"
    with no log line at all. A renamed helper or a circular import inside
    feral-core raises ImportError too, and produced a boot report saying
    an optional extra was not installed, for a subsystem no pip install
    could ever restore.
    """

    def test_third_party_import_is_a_skip_and_is_logged(self, caplog):
        from api.boot_report import BootReport, SubsystemStatus, boot_subsystem

        report = BootReport()
        with caplog.at_level(logging.INFO, logger="feral.boot"):
            with boot_subsystem(report, "LocalSTT", optional=True):
                raise ImportError(
                    "No module named 'faster_whisper'", name="faster_whisper",
                )

        entry = report.subsystems[-1]
        assert entry.status is SubsystemStatus.SKIPPED
        assert "faster_whisper" in entry.message
        assert [r for r in caplog.records if "LocalSTT" in r.getMessage()], (
            "a subsystem was dropped at boot and no line named it at the "
            "moment it was lost"
        )

    def test_first_party_import_is_a_failure_not_a_missing_extra(self, caplog):
        from api.boot_report import BootReport, SubsystemStatus, boot_subsystem

        report = BootReport()
        with caplog.at_level(logging.WARNING, logger="feral.boot"):
            with boot_subsystem(report, "DigitalTwin", optional=True):
                # What a renamed symbol inside feral-core actually raises.
                raise ImportError(
                    "cannot import name 'TwinPolicy' from 'agents.twin'",
                    name="agents.twin",
                )

        entry = report.subsystems[-1]
        assert entry.status is SubsystemStatus.FAILED, (
            "a broken first-party import was filed as a missing optional "
            "dependency, so the boot report advised an install that cannot fix it"
        )
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "a first-party import failure was not warned about"
        text = " ".join(r.getMessage() for r in warnings)
        assert "agents" in text
        assert "no install will fix it" in text

    def test_first_party_import_still_propagates_when_required(self):
        from api.boot_report import BootReport, boot_subsystem

        report = BootReport()
        with pytest.raises(ImportError):
            with boot_subsystem(report, "MemoryStore", optional=False):
                raise ImportError("cannot import name 'x' from 'memory.store'",
                                  name="memory.store")

    def test_first_party_roots_are_read_off_the_tree(self):
        """Derived, not written down, so adding a package cannot silently
        move it into the "third party" bucket."""
        from api.boot_report import first_party_roots

        roots = first_party_roots()
        for expected in ("api", "memory", "agents", "security", "skills"):
            assert expected in roots
        for foreign in ("fastapi", "httpx", "faster_whisper", "build"):
            assert foreign not in roots


# ── 6. `feral doctor`: a key that exists is not a key that works ────────


@pytest.fixture
def doctor_with_rejected_voice_key(monkeypatch, tmp_path):
    """An install whose OpenAI key has been rotated out from under it.

    Everything else is the fresh-install shape from
    ``tests/test_doctor_severity.py``: the only thing being modelled here
    is a configured realtime credential that the provider refuses.
    """
    import time as _t

    from security import probe as _probe_mod

    home = tmp_path / "doctor-rotated-key-home"
    home.mkdir()
    monkeypatch.setenv("FERAL_HOME", str(home))
    (home / "USER.md").write_text("Test operator running doctor.\n")

    for k in (
        "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY",
        "DEEPSEEK_API_KEY", "GROQ_API_KEY",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-rotated-away")

    def _fake_urlopen(url, timeout=2):
        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    async def _fake_probe(pid, **_kw):
        if pid == "ollama":
            return _probe_mod.ProbeResult(
                provider="ollama", ok=True, status_code=200, reason="ok",
                detail="Ollama running locally", probed_at=_t.time(),
                latency_ms=5.0,
            )
        if pid in ("openai", "openai_realtime"):
            return _probe_mod.ProbeResult(
                provider=pid, ok=False, status_code=401, reason="auth_failed",
                detail="Incorrect API key provided", probed_at=_t.time(),
                latency_ms=40.0,
            )
        return _probe_mod.ProbeResult(
            provider=pid, ok=False, status_code=None, reason="no_key",
            detail="not configured", probed_at=_t.time(), latency_ms=0.0,
        )

    monkeypatch.setattr(_probe_mod, "probe", _fake_probe)
    _probe_mod.clear_probe_cache()

    from memory.vector_index_backends import sqlite_vec as _sqlite_vec_mod

    monkeypatch.setattr(_sqlite_vec_mod, "sqlite_vec_available", lambda: True)
    return home


def _doctor_line(plain: str, label: str) -> str:
    for line in plain.splitlines():
        if label in line:
            return line.strip()
    raise AssertionError(f"doctor printed no {label!r} row:\n{plain}")


class TestDoctorVoiceRuntimeRow:
    """``cli.main.cmd_doctor`` — the "Voice runtime" row.

    It answered "is voice ready" from the presence of a key in env or the
    vault. So an install whose OpenAI key had been rotated printed a red
    "OpenAI Realtime: key rejected by API" in the Voice providers section
    and, four blocks later, a green "Voice runtime — key set: OpenAI
    Realtime". The green row is the one named after the feature, and it
    is the one an operator scanning for the cause reads.
    """

    def test_rejected_key_does_not_render_green(
        self, doctor_with_rejected_voice_key, monkeypatch, tmp_path,
    ):
        import io
        import re

        from rich.console import Console as _RichConsole

        buf = io.StringIO()
        console = _RichConsole(
            file=buf, force_terminal=False, color_system=None, width=160,
        )
        monkeypatch.setattr("rich.console.Console", lambda *a, **kw: console)

        from cli.main import cmd_doctor

        # doctor exits 1 when anything is red, which is itself part of the
        # fix: this install IS red, and the shell contract says so.
        with pytest.raises(SystemExit) as exit_info:
            cmd_doctor()
        assert exit_info.value.code == 1
        plain = re.compile(r"\x1b\[[0-9;]*m").sub("", buf.getvalue())

        row = _doctor_line(plain, "Voice runtime")
        assert not row.startswith("✔"), (
            "doctor called the voice runtime healthy on the strength of a key "
            f"the realtime probe rejected: {row!r}"
        )
        assert row.startswith("✘"), row
        assert "auth_failed" in row or "rejected" in row, (
            f"the row does not say why voice is not working: {row!r}"
        )
