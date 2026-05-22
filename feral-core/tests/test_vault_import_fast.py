"""R2-001 (round-2 of Wave 1, v2026.5.38) — vault import is fast and
side-effect-free.

Round-2 of Wave 1 surfaced that ``python -c "from security import vault"``
blocked >60s on a cold venv. The cause was a chain of eager imports:

  ``security/__init__.py`` → ``security.fetch_guard`` → ``httpx`` (~85ms)
  ``security/vault.py``    → ``cryptography.hazmat...`` (~150ms)
                           → ``config.loader`` cascade (~20ms)

Worse, the first ``BlindVault()`` construction blocked on
``securityd`` without a bound, so a frozen macOS keychain hung the
entire brain boot.

These tests pin the v2026.5.38 contract:

1. ``import security.vault`` (or ``from security import vault``) is
   side-effect-free: no Keychain I/O, no network, no disk reads.
   The module load itself is bounded by ``IMPORT_BUDGET_MS``.
2. ``BlindVault()`` construction logs ``vault.unlock.start`` before
   touching the keychain and bounds the unlock at 5s (or
   ``FERAL_VAULT_UNLOCK_TIMEOUT_S``). A hanging keychain raises
   ``VaultKeyUnavailableError`` instead of hanging forever.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

# 200ms is the audit-r14 R2-001 acceptance budget for in-process module
# import. Subprocess wall-clock includes the ~150ms Python interpreter
# startup itself, so we measure module-import-only time inside a
# subprocess that times the import explicitly with ``time.perf_counter``.
IMPORT_BUDGET_MS = 200
SUBPROCESS_WALLCLOCK_BUDGET_S = 2.5  # generous bound to absorb cold-venv variance


# Capture the production ``_keyring_get_password`` (the daemon-thread
# wrapped variant from ``security/vault.py``) BEFORE conftest's
# ``isolate_os_keychain`` autouse fixture monkeypatches it. We need
# the real implementation to test the timeout machinery; conftest's
# dict-shim has no timeout. Using a module-level constant keeps the
# vault module identity stable so sibling tests that
# ``pytest.raises(VaultError)`` keep working.
from security import vault as _vault_for_capture  # noqa: E402
_ORIGINAL_KEYRING_GET = _vault_for_capture._keyring_get_password
del _vault_for_capture


def _run_timed_import(spec: str) -> float:
    """Spawn a child interpreter, measure how long *spec* takes to
    import, return the elapsed milliseconds. Uses a fresh subprocess
    so module caches are cold (mirrors the audit-r14 R2-001 spec).
    """
    snippet = (
        "import time, sys\n"
        f"t = time.perf_counter()\n"
        f"{spec}\n"
        "print(f'{(time.perf_counter() - t) * 1000:.2f}', flush=True)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", snippet],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_WALLCLOCK_BUDGET_S,
    )
    assert proc.returncode == 0, f"import failed: {proc.stderr}"
    return float(proc.stdout.strip())


class TestImportSpeed:
    def test_import_security_vault_under_budget(self):
        elapsed_ms = _run_timed_import("import security.vault")
        assert elapsed_ms < IMPORT_BUDGET_MS, (
            f"`import security.vault` took {elapsed_ms:.1f}ms on a cold "
            f"interpreter; R2-001 budget is {IMPORT_BUDGET_MS}ms. The most "
            f"likely cause is a new eager import in security/__init__.py "
            f"or security/vault.py — use the PEP-562 ``__getattr__`` "
            f"facade or defer to first use."
        )

    def test_from_security_import_vault_under_budget(self):
        elapsed_ms = _run_timed_import("from security import vault")
        assert elapsed_ms < IMPORT_BUDGET_MS, (
            f"`from security import vault` took {elapsed_ms:.1f}ms; "
            f"R2-001 budget is {IMPORT_BUDGET_MS}ms."
        )

    def test_subprocess_wallclock_under_2500ms(self):
        """The subprocess wall-clock includes Python startup (~150ms
        baseline) + the import work above. We assert a generous 2.5s
        bound here so a regression that re-introduces the 60s hang
        from the bug report is caught even when in-process budget
        bookkeeping is broken."""
        started = time.perf_counter()
        proc = subprocess.run(
            [sys.executable, "-c", "import security.vault"],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_WALLCLOCK_BUDGET_S,
        )
        elapsed = time.perf_counter() - started
        assert proc.returncode == 0, f"import failed: {proc.stderr}"
        assert elapsed < SUBPROCESS_WALLCLOCK_BUDGET_S, (
            f"subprocess wall-clock {elapsed:.2f}s exceeded "
            f"{SUBPROCESS_WALLCLOCK_BUDGET_S}s — possible re-introduction "
            f"of the R2-001 hang."
        )


class TestImportSideEffectFree:
    def test_import_does_not_touch_keychain(self, tmp_path):
        """Run a subprocess with a synthetic ``keyring`` shim that
        explodes on use; ``import security.vault`` must succeed. Any
        regression that re-introduces a top-level keychain call will
        crash the subprocess."""
        shim_dir = tmp_path / "shim"
        shim_dir.mkdir()
        (shim_dir / "keyring.py").write_text(
            "def get_password(*a, **kw):\n"
            "    raise RuntimeError('keychain access during import is forbidden by R2-001')\n"
            "def set_password(*a, **kw):\n"
            "    raise RuntimeError('keychain write during import is forbidden by R2-001')\n"
            "def delete_password(*a, **kw):\n"
            "    raise RuntimeError('keychain delete during import is forbidden by R2-001')\n",
            encoding="utf-8",
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = f"{shim_dir}{os.pathsep}{env.get('PYTHONPATH', '')}"
        proc = subprocess.run(
            [sys.executable, "-c", "import security.vault; import security; print('ok')"],
            capture_output=True, text=True, timeout=5, env=env,
        )
        assert proc.returncode == 0, (
            f"importing security.vault accessed the keychain "
            f"(R2-001 violation): {proc.stderr}"
        )
        assert "ok" in proc.stdout

    def test_import_does_not_open_files_or_network(self):
        """``security/__init__.py`` and ``security/vault.py`` must not
        read ``~/.feral/audit.log``, ``~/.feral/credentials.enc``,
        ``~/.feral/session_token``, or any other on-disk artefact at
        import. We mock the relevant ``open``/``socket`` calls by
        environment marker and verify import succeeds with no hits."""
        snippet = (
            "import builtins, socket, sys\n"
            "hits = []\n"
            "_real_open = builtins.open\n"
            "def _spy_open(p, *a, **k):\n"
            "    if 'feral' in str(p) or '.feral' in str(p):\n"
            "        hits.append(('open', str(p)))\n"
            "    return _real_open(p, *a, **k)\n"
            "builtins.open = _spy_open\n"
            "_real_sock = socket.socket\n"
            "def _spy_sock(*a, **k):\n"
            "    hits.append(('socket', a))\n"
            "    return _real_sock(*a, **k)\n"
            "socket.socket = _spy_sock\n"
            "import security.vault  # noqa\n"
            "print('|'.join(f'{kind}:{x}' for kind, x in hits))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", snippet],
            capture_output=True, text=True, timeout=5,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "", (
            f"import security.vault touched the disk / network "
            f"(R2-001 violation): {proc.stdout!r}"
        )


@pytest.fixture
def restore_real_keyring_wrapper(monkeypatch):
    """Re-install the production ``_keyring_get_password`` for the
    test that opts in. The conftest ``isolate_os_keychain``
    autouse fixture replaces the function with a synchronous
    dict shim for every other test in the suite; this fixture
    flips it back to the daemon-thread-wrapped variant so the
    timeout machinery is exercised.

    Opt-in (not autouse) because every test that doesn't actually
    exercise the keychain unlock path stays under the conftest
    isolation — autouse'ing this would re-install the production
    function for ``test_unlock_timeout_env_overrides_default``
    (which only reads env vars) and leave a sliver of state that
    sibling encryption tests pick up at teardown.
    """
    from security import vault as vault_mod
    monkeypatch.setattr(
        vault_mod, "_keyring_get_password", _ORIGINAL_KEYRING_GET,
    )
    yield


class TestUnlockTimeout:
    def test_unlock_timeout_env_overrides_default(self, monkeypatch):
        # Deliberately do NOT pop ``sys.modules["security.vault"]``
        # here: popping orphans the module so subsequent tests that
        # imported ``security.vault`` at collection time keep using
        # the orphaned reference, while conftest's per-test
        # ``isolate_os_keychain`` monkeypatches the NEW module — the
        # two diverge and the encryption tests can no longer decrypt
        # their own .enc files.
        from security import vault as vault_mod

        monkeypatch.setenv("FERAL_VAULT_UNLOCK_TIMEOUT_S", "1.25")
        assert vault_mod._unlock_timeout_seconds() == 1.25

        monkeypatch.setenv("FERAL_VAULT_UNLOCK_TIMEOUT_S", "")
        assert vault_mod._unlock_timeout_seconds() == vault_mod._DEFAULT_UNLOCK_TIMEOUT_S

        monkeypatch.setenv("FERAL_VAULT_UNLOCK_TIMEOUT_S", "garbage")
        assert vault_mod._unlock_timeout_seconds() == vault_mod._DEFAULT_UNLOCK_TIMEOUT_S

        monkeypatch.setenv("FERAL_VAULT_UNLOCK_TIMEOUT_S", "-3")
        assert vault_mod._unlock_timeout_seconds() == vault_mod._DEFAULT_UNLOCK_TIMEOUT_S

    def test_keychain_unlock_timeout_raises(
        self, monkeypatch, caplog, restore_real_keyring_wrapper,
    ):
        """Simulate a frozen ``securityd``: ``keyring.get_password``
        blocks indefinitely. The worker thread bound must trip the
        configured timeout and raise ``VaultKeyUnavailableError``."""
        import types

        from security import vault as vault_mod  # noqa: WPS433

        frozen = types.ModuleType("keyring")

        def _hang(*_a, **_kw):
            time.sleep(60)  # would hang the boot without the bound

        frozen.get_password = _hang
        frozen.set_password = lambda *a, **kw: None
        frozen.delete_password = lambda *a, **kw: None

        monkeypatch.setitem(sys.modules, "keyring", frozen)
        monkeypatch.setenv("FERAL_VAULT_UNLOCK_TIMEOUT_S", "0.2")

        with caplog.at_level(logging.INFO, logger="feral.vault"):
            started = time.perf_counter()
            with pytest.raises(vault_mod.VaultKeyUnavailableError) as excinfo:
                vault_mod._keyring_get_password(
                    vault_mod.KEYRING_SERVICE, vault_mod.KEYRING_USERNAME,
                )
            elapsed = time.perf_counter() - started

        assert "did not complete" in str(excinfo.value)
        assert elapsed < 2.0, (
            f"timeout did not fire — keychain call ran for {elapsed:.2f}s"
        )
        assert any("vault.unlock.start" in r.message for r in caplog.records), (
            "vault.unlock.start log line missing — operators need it to "
            "see when the keychain is being touched"
        )

    def test_keychain_unlock_success_logs_done(
        self, monkeypatch, caplog, restore_real_keyring_wrapper,
    ):
        import types

        from security import vault as vault_mod  # noqa: WPS433

        good = types.ModuleType("keyring")
        good.get_password = lambda service, user: "deadbeef" * 8
        good.set_password = lambda *a, **kw: None
        good.delete_password = lambda *a, **kw: None

        monkeypatch.setitem(sys.modules, "keyring", good)
        with caplog.at_level(logging.INFO, logger="feral.vault"):
            result = vault_mod._keyring_get_password(
                vault_mod.KEYRING_SERVICE, vault_mod.KEYRING_USERNAME,
            )
        assert result == "deadbeef" * 8
        messages = [r.message for r in caplog.records]
        assert any("vault.unlock.start" in m for m in messages)
        assert any("vault.unlock.done" in m for m in messages)
