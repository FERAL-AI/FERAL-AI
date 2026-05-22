"""audit-r14 / lane-07 (W3) — `feral key add/list/remove/rotate
--provider --label` wrap Wave 2 Lane 09's ``security.vault_keys``
overlay and run a probe immediately so the operator sees green/red
before the prompt returns.

Pre-Lane-07 the only ``feral key`` actions were ``status``, ``rotate``
(master), and ``recover``. Finding 08 documents the multi-key gap:
operators had no CLI path to add a labeled provider key — they had to
edit the vault by hand or run the wizard. This file pins:

1. ``feral key add --provider X --label Y`` writes via vault_keys,
   probes, persists ``last_probe_ok``, and emits a green/red line.
2. ``feral key list`` renders the per-provider table without ever
   exposing the secret value.
3. ``feral key remove`` deletes the labeled secret + meta and clears
   the active pointer when it referenced the removed label.
4. ``feral key rotate --provider X --label Y`` swaps the secret,
   preserves active selection, re-probes.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def feral_home(tmp_path, monkeypatch):
    """Isolated FERAL_HOME so vault writes don't touch the operator's
    real keychain entry."""
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    yield tmp_path


@pytest.fixture
def stubbed_vault(monkeypatch, feral_home):
    """An in-memory vault matching the BlindVault surface vault_keys
    relies on (``put``/``get``/``remove_from``/``list_namespace``).

    Real vault construction reads from the OS keychain; tests that
    only need to verify CLI dispatch should not require keychain
    access. We patch ``security.vault.get_vault`` to return this
    stub for the duration of the test.
    """
    class _StubVault:
        def __init__(self):
            self.store: dict[tuple[str, str], str] = {}

        def put(self, namespace, key, value, *, stored_by="user"):
            self.store[(namespace, key)] = value

        def get(self, namespace, key, *, requester="executor"):
            return self.store.get((namespace, key))

        def remove_from(self, namespace, key, *, removed_by="user"):
            return self.store.pop((namespace, key), None) is not None

        def list_namespace(self, namespace):
            return [k for (ns, k) in self.store if ns == namespace]

    stub = _StubVault()
    monkeypatch.setattr("security.vault.get_vault", lambda: stub)
    return stub


@pytest.fixture
def stub_probe_ok(monkeypatch):
    """Make every probe return ok so cmd_key_add's post-write check passes."""
    import time as _t
    from security import probe as probe_mod

    async def _fake_probe(pid, **_kw):
        return probe_mod.ProbeResult(
            provider=pid, ok=True, status_code=200,
            reason="ok", detail="OK",
            probed_at=_t.time(), latency_ms=10.0,
        )

    monkeypatch.setattr(probe_mod, "probe", _fake_probe)
    probe_mod.clear_probe_cache()


@pytest.fixture
def stub_probe_fail(monkeypatch):
    """Make every probe return ok=False (simulates a 401)."""
    import time as _t
    from security import probe as probe_mod

    async def _fake_probe(pid, **_kw):
        return probe_mod.ProbeResult(
            provider=pid, ok=False, status_code=401,
            reason="auth_failed", detail="Incorrect API key provided",
            probed_at=_t.time(), latency_ms=42.0,
        )

    monkeypatch.setattr(probe_mod, "probe", _fake_probe)
    probe_mod.clear_probe_cache()


# ----------------------------------------------------------------------
# add
# ----------------------------------------------------------------------


class TestKeyAdd:
    def test_add_writes_via_vault_keys_and_probes(
        self, stubbed_vault, stub_probe_ok, capsys,
    ):
        from cli.key_commands import cmd_key_add

        rc = cmd_key_add(
            provider_id="openai",
            label="prod",
            api_key="sk-test-1234567890",
            set_active=True,
            probe=True,
        )
        out = capsys.readouterr().out

        assert rc == 0
        assert "Saved openai:prod" in out
        assert "[active]" in out
        assert "Probe OK" in out
        # The raw secret is NEVER printed.
        assert "sk-test-1234567890" not in out
        # Vault namespace contains the entry.
        assert ("provider_keys", "openai:prod") in stubbed_vault.store

    def test_add_with_probe_fail_returns_nonzero(
        self, stubbed_vault, stub_probe_fail, capsys,
    ):
        from cli.key_commands import cmd_key_add

        rc = cmd_key_add(
            provider_id="openai", label="dev",
            api_key="sk-bad", probe=True,
        )
        out = capsys.readouterr().out
        assert rc == 1
        assert "Probe FAILED" in out
        assert "Incorrect API key" in out
        # Key is still saved — probe failure doesn't roll back.
        assert ("provider_keys", "openai:dev") in stubbed_vault.store

    def test_add_no_probe_skips_round_trip(
        self, stubbed_vault, capsys, monkeypatch,
    ):
        """`--no-probe` MUST not call security.probe.probe at all."""
        from cli.key_commands import cmd_key_add
        from security import probe as probe_mod

        called = {"n": 0}

        async def _spy(pid, **_kw):
            called["n"] += 1
            return probe_mod.ProbeResult(
                provider=pid, ok=True, status_code=200,
                reason="ok", detail="ok", probed_at=0.0, latency_ms=0.0,
            )

        monkeypatch.setattr(probe_mod, "probe", _spy)
        rc = cmd_key_add(
            provider_id="anthropic", label="default",
            api_key="sk-ant-xyz", probe=False,
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert called["n"] == 0
        assert "Saved anthropic:default" in out
        assert "skipping validity check" in out

    def test_add_rejects_empty_api_key(
        self, stubbed_vault, capsys, monkeypatch,
    ):
        """When --api-key is empty, the prompt is invoked. If the
        operator hits Ctrl+D / Ctrl+C, we MUST cancel cleanly with
        rc=1 — no traceback, no half-written entry."""
        from cli import key_commands as kc

        # Simulate the interactive prompt raising EOFError (Ctrl+D).
        def _abort(_prompt, allow_empty=True):
            raise EOFError()

        monkeypatch.setattr(kc.ui_kit, "password", _abort)

        rc = kc.cmd_key_add(
            provider_id="openai", label="default", api_key="", probe=False,
        )
        out = capsys.readouterr().out
        assert rc == 1
        assert "Cancelled" in out
        assert ("provider_keys", "openai:default") not in stubbed_vault.store


# ----------------------------------------------------------------------
# list
# ----------------------------------------------------------------------


class TestKeyList:
    def test_list_renders_label_and_active_marker_no_secret(
        self, stubbed_vault, stub_probe_ok, capsys,
    ):
        from cli.key_commands import cmd_key_add, cmd_key_list

        cmd_key_add("anthropic", "prod", "sk-ant-prod", set_active=True, probe=False) \
            if False else cmd_key_add(
                provider_id="anthropic", label="prod",
                api_key="sk-ant-prod", set_active=True, probe=False,
            )
        cmd_key_add(
            provider_id="anthropic", label="dev",
            api_key="sk-ant-dev", probe=False,
        )
        capsys.readouterr()  # discard add output
        rc = cmd_key_list(provider_id="anthropic")
        out = capsys.readouterr().out
        assert rc == 0
        assert "anthropic" in out
        assert "prod" in out
        assert "dev" in out
        # Secrets must never be rendered.
        assert "sk-ant-prod" not in out
        assert "sk-ant-dev" not in out

    def test_list_all_groups_by_provider(
        self, stubbed_vault, capsys,
    ):
        from cli.key_commands import cmd_key_add, cmd_key_list

        cmd_key_add("openai", "prod", api_key="sk-1", probe=False) \
            if False else cmd_key_add(
                provider_id="openai", label="prod", api_key="sk-1", probe=False,
            )
        cmd_key_add(
            provider_id="anthropic", label="default",
            api_key="sk-2", probe=False,
        )
        capsys.readouterr()
        rc = cmd_key_list(provider_id="")
        out = capsys.readouterr().out
        assert rc == 0
        assert "openai" in out
        assert "anthropic" in out

    def test_list_empty_emits_help_text(self, stubbed_vault, capsys):
        from cli.key_commands import cmd_key_list

        rc = cmd_key_list(provider_id="")
        out = capsys.readouterr().out
        assert rc == 0
        assert "No labeled provider keys" in out


# ----------------------------------------------------------------------
# remove
# ----------------------------------------------------------------------


class TestKeyRemove:
    def test_remove_deletes_and_clears_active_pointer(
        self, stubbed_vault, capsys,
    ):
        from cli.key_commands import cmd_key_add, cmd_key_remove

        cmd_key_add(
            provider_id="openai", label="prod",
            api_key="sk-x", set_active=True, probe=False,
        )
        capsys.readouterr()
        rc = cmd_key_remove(provider_id="openai", label="prod", skip_confirm=True)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Removed openai:prod" in out
        assert ("provider_keys", "openai:prod") not in stubbed_vault.store
        # active pointer for openai is cleared
        assert ("provider_keys_active", "openai") not in stubbed_vault.store

    def test_remove_unknown_label_returns_1(self, stubbed_vault, capsys):
        from cli.key_commands import cmd_key_remove

        rc = cmd_key_remove(
            provider_id="openai", label="never", skip_confirm=True,
        )
        out = capsys.readouterr().out
        assert rc == 1
        assert "No labeled key found" in out


# ----------------------------------------------------------------------
# rotate (per-provider)
# ----------------------------------------------------------------------


class TestKeyRotateProvider:
    def test_rotate_replaces_secret_preserves_active_and_reprobes(
        self, stubbed_vault, stub_probe_ok, capsys,
    ):
        from cli.key_commands import cmd_key_add, cmd_key_rotate_provider

        cmd_key_add(
            provider_id="openai", label="prod",
            api_key="sk-old", set_active=True, probe=False,
        )
        capsys.readouterr()
        rc = cmd_key_rotate_provider(
            provider_id="openai", label="prod",
            api_key="sk-new", skip_confirm=True,
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "Rotated openai:prod" in out
        assert "Probe OK" in out
        # The new value is what's stored.
        assert stubbed_vault.store[("provider_keys", "openai:prod")] == "sk-new"
        # active pointer still points at prod
        assert stubbed_vault.store[("provider_keys_active", "openai")] == "prod"

    def test_rotate_unknown_label_refuses(self, stubbed_vault, capsys):
        from cli.key_commands import cmd_key_rotate_provider

        rc = cmd_key_rotate_provider(
            provider_id="openai", label="never",
            api_key="sk-anything", skip_confirm=True,
        )
        out = capsys.readouterr().out
        assert rc == 1
        assert "No labeled key" in out
