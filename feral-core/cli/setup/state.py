"""Shared state passed through every setup step.

Each step reads + mutates three plain dicts (``settings``,
``credentials``, ``identity``) so a step can be invoked in isolation
under tests without the full wizard running.

audit-r14 / lane-07  — wizard contract changes
-----------------------------------------------

Pre-Lane-07 the wizard had two tightly-coupled bugs:

1. ``state.save()`` unconditionally set
   ``settings.meta.setup_complete = True`` even when the user quit
   the wizard at step 2, so the dashboard "you're done!" gate fired
   on a half-completed install (finding 09 — quit semantics: 1/5).
2. There was no resume-from-last-step path; a user who Ctrl+C'd on
   the home_assistant step had to re-walk every previous step on
   the next ``feral setup`` invocation (finding 09 — re-run/resume:
   3/5).

The fix:

* ``save()`` no longer sets ``setup_complete``. The wizard's finish
  step explicitly calls :meth:`mark_complete` after the summary
  renders. Quit / Ctrl+C / crash mid-flow → ``meta.setup_complete``
  stays False (or whatever value it had before the wizard ran).
* New ``setup_state.json`` sidecar (``~/.feral/setup_state.json``)
  tracks ``last_step`` + ``completed_steps`` + ``ts``. The state
  machine rewrites it after each successful step. Quit / crash
  leaves the file in place so the next ``feral setup`` invocation
  can offer "resume from <step>?" without losing partial input.
* The finish step's :meth:`mark_complete` deletes the sidecar and
  rewrites ``settings.json`` with ``meta.setup_complete = True``.

Credentials persistence (A7)
----------------------------
Credentials are written to the  encrypted ``BlindVault`` — NEVER to
a plaintext ``credentials.json``. The vault maps ``credentials.json``
→ ``credentials.enc`` internally, so anchoring it at the wizard's
``home / credentials.json`` path keeps the encrypted payload inside
the same directory without leaving a cleartext file behind.

Backwards compatibility:

- ``load()`` still reads any existing legacy ``credentials.json`` that
  predates the vault. Instantiating the vault during ``save()``
  triggers its built-in auto-migration (``credentials.json`` →
  ``credentials.enc`` with the original moved to
  ``credentials.json.bak.legacy`` at chmod 0600) so returning users'
  keys are preserved.
- ``settings.json`` and ``identity.json`` remain plain JSON — they are
  not secret material.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("feral.cli.setup.state")


@dataclass
class WizardState:
    """Single mutable object threaded through every step."""

    home: Path
    settings: dict[str, Any] = field(default_factory=dict)
    credentials: dict[str, Any] = field(default_factory=dict)
    identity: dict[str, Any] = field(default_factory=dict)
    completed_steps: set[str] = field(default_factory=set)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, home: Path) -> "WizardState":
        home.mkdir(parents=True, exist_ok=True)
        settings = _read_json(home / "settings.json")
        credentials = _read_credentials(home)
        identity = _read_json(home / "identity.json")
        return cls(
            home=home, settings=settings, credentials=credentials, identity=identity
        )

    def save(self) -> None:
        """Persist settings / credentials / identity to disk.

        audit-r14 / lane-07  — this method NO LONGER sets
        ``setup_complete``. Use :meth:`mark_complete` from the
        wizard's finish step. Mid-flow quits and Ctrl+C save partial
        settings + credentials but leave ``setup_complete`` alone, so
        the brain dashboard's "you're done" gate doesn't fire on a
        half-finished wizard.
        """
        self.home.mkdir(parents=True, exist_ok=True)
        # Preserve any existing ``meta`` block (including a pre-
        # existing ``setup_complete=True`` from a previous successful
        # run) — never downgrade to False here. Only the finish step
        # via :meth:`mark_complete` flips this flag to True.
        self.settings.setdefault("meta", {})
        self._flush_settings()
        _persist_credentials(self.home, self.credentials)
        if self.identity:
            _write_json(self.home / "identity.json", self.identity)

    def mark_complete(self) -> None:
        """Called by the wizard's finish step after the summary renders.

        Sets ``meta.setup_complete = True`` in ``settings.json`` and
        deletes the resume sidecar at ``~/.feral/setup_state.json``.
        """
        self.home.mkdir(parents=True, exist_ok=True)
        self.settings.setdefault("meta", {})
        self.settings["meta"]["setup_complete"] = True
        self._flush_settings()
        # Best-effort cleanup of the resume sidecar.
        sidecar = self.home / "setup_state.json"
        try:
            if sidecar.is_file():
                sidecar.unlink()
        except OSError:
            pass

    def _flush_settings(self) -> None:
        """Deep-merge the in-memory settings onto the on-disk file.

        The wizard is NOT the only writer of ``settings.json`` during a
        single ``feral setup`` run: the network step calls into
        :mod:`cli.setup.network`, which persists ``access.*`` and
        ``network.bind_host`` straight to disk (the same code path
        ``feral access remote-up`` uses, so the persistence rules live
        in one place). Writing ``self.settings`` wholesale here would
        replay a snapshot captured at wizard start over the top of
        those writes and silently delete them — picking Tailscale in
        the wizard used to leave zero trace in ``settings.json``.

        Re-reading the file and merging key-by-key makes the wizard a
        cooperative writer instead of a last-writer-wins one. The
        merged result is written back into ``self.settings`` so the
        in-memory view (used by the finish summary) matches disk.
        """
        path = self.home / "settings.json"
        merged = _deep_merge(_read_json(path), self.settings)
        _write_json(path, merged)
        self.settings.clear()
        self.settings.update(merged)

    # ------------------------------------------------------------------
    # Resume sidecar (~/.feral/setup_state.json) — Lane 07
    # ------------------------------------------------------------------

    def write_setup_state(self, *, last_step: str, completed_steps: list[str]) -> None:
        """Persist `last_step` + `completed_steps` so the next
        ``feral setup`` invocation can resume from where this one
        stopped (or crashed). Atomic write via parent-dir rename so
        a crash mid-write can't leave a half-flushed JSON behind.
        """
        import time as _time

        self.home.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_step": last_step,
            "completed_steps": list(completed_steps),
            "ts": _time.time(),
            "schema": 1,
        }
        path = self.home / "setup_state.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        tmp.replace(path)

    @classmethod
    def read_setup_state(cls, home: Path) -> dict:
        """Read the resume sidecar (if any). Returns an empty dict on
        missing / corrupt file."""
        path = home / "setup_state.json"
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text())
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def set_setting(self, section: str, key: str, value: Any) -> None:
        self.settings.setdefault(section, {})[key] = value

    def get_setting(self, section: str, key: str, default: Any = None) -> Any:
        return (self.settings.get(section) or {}).get(key, default)

    def set_credential(self, key: str, value: str) -> None:
        if value:
            self.credentials[key] = value

    def has_credential(self, key: str) -> bool:
        return bool(self.credentials.get(key))


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Return ``base`` with ``overlay`` merged in, nested dicts included.

    Scalars and lists in ``overlay`` win outright; nested dicts recurse
    so writing ``{"network": {"bind_host": ...}}`` never drops a sibling
    ``network.port`` another writer put there. Never mutates ``overlay``.
    """
    out = dict(base)
    for key, value in overlay.items():
        existing = out.get(key)
        if isinstance(value, dict) and isinstance(existing, dict):
            out[key] = _deep_merge(existing, value)
        else:
            out[key] = value
    return out


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _write_json(path: Path, data: dict, *, secure: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True))
    if secure:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _read_credentials(home: Path) -> dict:
    """Return the current credential map.

    Priority order:

    1. Encrypted vault (``credentials.enc``) — authoritative .
    2. Legacy plaintext ``credentials.json`` — only present on machines
       that have not yet booted the brain since the vault migration.
       The plaintext file will be rewritten as encrypted and removed
       the next time a vault is instantiated (e.g. on ``save()``).

    Any failure to decrypt surfaces as an empty dict so the wizard can
    still complete; the user will see the usual "please re-enter your
    keys" flow rather than a traceback.
    """
    try:
        from security.vault import BlindVault
    except Exception as exc:  # pragma: no cover — import-time failure
        logger.warning(
            "setup.state: vault import failed (%s); falling back to "
            "legacy plaintext read.",
            exc,
        )
        return _read_json(home / "credentials.json")

    try:
        vault = BlindVault(vault_path=str(home / "credentials.json"))
    except Exception as exc:
        logger.warning(
            "setup.state: vault init failed (%s); returning empty creds "
            "so the wizard can proceed without leaking to plaintext.",
            exc,
        )
        return {}

    try:
        return {k: vault.retrieve(k) or "" for k in vault.list_keys()}
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("setup.state: vault read failed (%s)", exc)
        return {}


def _persist_credentials(home: Path, credentials: dict) -> None:
    """Write every credential to the encrypted vault.

    Empty values are skipped so a half-filled wizard step doesn't
    clobber an existing key with ``""``. If the vault is unavailable
    (keychain broken, cryptography missing, etc.) we refuse to fall
    back to plaintext — the credentials stay in memory for this
    process and the user is instructed to fix the vault on next boot.
    """
    flat = {k: v for k, v in credentials.items() if isinstance(v, str) and v}
    if not flat:
        return

    try:
        from security.vault import BlindVault
    except Exception as exc:  # pragma: no cover — import-time failure
        logger.error(
            "setup.state: vault import failed (%s); refusing to persist "
            "plaintext credentials. Keys remain in memory only.",
            exc,
        )
        return

    try:
        vault = BlindVault(vault_path=str(home / "credentials.json"))
    except Exception as exc:
        logger.error(
            "setup.state: vault init failed (%s); refusing to persist "
            "plaintext credentials. Keys remain in memory only.",
            exc,
        )
        return

    for key, value in flat.items():
        try:
            vault.set_credential(key, value)
        except Exception as exc:
            logger.error("setup.state: vault write for %s failed: %s", key, exc)
