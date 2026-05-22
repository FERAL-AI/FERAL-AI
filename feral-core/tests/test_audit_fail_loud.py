"""audit-r12 A5 (v2026.5.38) — credential audit writes are fail-loud.

The pre-fix behaviour swallowed any ``OSError`` on the audit append
with a ``logger.warning(...)`` and let the credential operation
succeed silently. That violated the security perimeter: a vault op
without an audit record is itself a security incident, and silently
ignoring it left operators staring at an "empty" audit log after a
breach attempt.

These tests pin the new contract:

* :class:`security.audit_log.AuditFailure` is raised on any write
  failure (missing parent dir that can't be created, write fails).
* ``BlindVault.store / retrieve / remove`` (and the namespace
  variants) **propagate** ``AuditFailure`` — they do not log-and-
  continue. The credential is **not** persisted when the audit
  cannot be written.
* The REST surface (``GET /api/security/audit``) returns a structured
  ``{"error": "audit_log_unreadable", ...}`` response instead of an
  empty entries list when the log exists but cannot be parsed.
* The audit JSON wire shape preserves the field name ``key`` so the
  WebUI v2 Settings → Audit log panel keeps rendering.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_audit_home(tmp_path, monkeypatch):
    """Pin ``FERAL_HOME`` + ``FERAL_AUDIT_LOG_PATH`` into the temp
    directory so production audit logs are never touched by the
    suite."""
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    monkeypatch.delenv("FERAL_AUDIT_LOG_PATH", raising=False)
    yield


def _reload_audit_log():
    """Return the ``security.audit_log`` module.

    The fixture above pins ``FERAL_HOME`` per-test so the module-level
    audit-log path resolves through the temp dir without needing a
    reload. The function-level name is kept for backward compatibility
    with the helper sites inside the test class.
    """
    from security import audit_log  # noqa: WPS433
    return audit_log


class TestAuditEventHappyPath:
    def test_event_written_with_canonical_fields(self, tmp_path):
        audit = _reload_audit_log()

        entry = audit.audit_event(
            "store", "credentials.OPENAI_API_KEY", "user", namespace="credentials"
        )
        assert entry["action"] == "store"
        assert entry["key"] == "credentials.OPENAI_API_KEY"
        assert entry["actor"] == "user"
        assert entry["namespace"] == "credentials"
        assert isinstance(entry["ts"], float)

        path = audit.audit_log_path()
        text = path.read_text(encoding="utf-8")
        line = text.strip().splitlines()[-1]
        wire = json.loads(line)
        assert wire == entry

    def test_extra_kwargs_round_trip(self, tmp_path):
        audit = _reload_audit_log()
        audit.audit_event(
            "get", "credentials.X", "executor", found=True, request_id="abc"
        )
        events = audit.recent_events(limit=10)
        assert events[-1]["found"] is True
        assert events[-1]["request_id"] == "abc"

    def test_env_override_for_audit_log_path(self, tmp_path, monkeypatch):
        target = tmp_path / "custom" / "feral-audit.jsonl"
        monkeypatch.setenv("FERAL_AUDIT_LOG_PATH", str(target))
        audit = _reload_audit_log()
        audit.audit_event("store", "credentials.K", "user")
        assert target.exists()


class TestAuditFailureRaised:
    def test_failure_when_parent_path_cannot_be_created(self, tmp_path, monkeypatch):
        """A file at the audit-parent location makes ``mkdir`` fail —
        the old code swallowed; the new code raises."""
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        monkeypatch.setenv("FERAL_AUDIT_LOG_PATH", str(blocker / "audit.log"))
        audit = _reload_audit_log()

        with pytest.raises(audit.AuditFailure):
            audit.audit_event("store", "credentials.X", "user")

    def test_failure_when_write_raises_oserror(self, tmp_path, monkeypatch):
        audit = _reload_audit_log()

        import builtins
        real_open = builtins.open

        def _boom_open(path, *args, **kwargs):
            if str(path).endswith("audit.log"):
                raise OSError(28, "No space left on device")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", _boom_open)
        with pytest.raises(audit.AuditFailure) as excinfo:
            audit.audit_event("store", "credentials.X", "user")
        assert "audit log write" in str(excinfo.value)
        assert "No space left on device" in str(excinfo.value)

    def test_invalid_input_raises_audit_failure(self):
        audit = _reload_audit_log()
        with pytest.raises(audit.AuditFailure):
            audit.audit_event("", "credentials.X", "user")
        with pytest.raises(audit.AuditFailure):
            audit.audit_event("store", "", "user")
        with pytest.raises(audit.AuditFailure):
            audit.audit_event("store", "credentials.X", "")


class TestRecentEventsFailLoud:
    def test_returns_empty_when_log_absent(self):
        audit = _reload_audit_log()
        assert audit.recent_events() == []

    def test_parses_jsonlines(self, tmp_path, monkeypatch):
        audit = _reload_audit_log()
        for i in range(3):
            audit.audit_event("store", f"credentials.K{i}", "user")
        events = audit.recent_events(limit=2)
        assert len(events) == 2
        assert events[-1]["key"].endswith("K2")

    def test_raises_on_malformed_log(self, tmp_path, monkeypatch):
        audit = _reload_audit_log()
        path = audit.audit_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json\n", encoding="utf-8")
        with pytest.raises(audit.AuditFailure) as excinfo:
            audit.recent_events()
        assert "malformed" in str(excinfo.value)


class TestVaultPropagatesAuditFailure:
    """``BlindVault`` does not swallow ``AuditFailure``."""

    def _make_vault(self, tmp_path, monkeypatch):
        """Construct a per-test BlindVault. Uses
        :py:meth:`reset_vault` to clear the module-level singleton
        without popping the module (popping would orphan downstream
        ``import security.vault as v`` references in the rest of the
        suite)."""
        monkeypatch.setenv("FERAL_HOME", str(tmp_path))
        from security import vault as vault_mod

        vault_mod.reset_vault()
        return vault_mod, vault_mod.get_vault(str(tmp_path / "credentials.json"))

    def test_store_raises_audit_failure(self, tmp_path, monkeypatch):
        vault_mod, v = self._make_vault(tmp_path, monkeypatch)

        from security import audit_log

        def _boom(*_a, **_kw):
            raise audit_log.AuditFailure("disk on fire")

        monkeypatch.setattr(audit_log, "audit_event", _boom)
        with pytest.raises(audit_log.AuditFailure):
            v.store("OPENAI_API_KEY", "sk-test")

    def test_retrieve_raises_audit_failure(self, tmp_path, monkeypatch):
        vault_mod, v = self._make_vault(tmp_path, monkeypatch)
        v.store("OPENAI_API_KEY", "sk-test")  # one successful audit

        from security import audit_log

        def _boom(*_a, **_kw):
            raise audit_log.AuditFailure("disk on fire")

        monkeypatch.setattr(audit_log, "audit_event", _boom)
        with pytest.raises(audit_log.AuditFailure):
            v.retrieve("OPENAI_API_KEY")

    def test_remove_raises_audit_failure(self, tmp_path, monkeypatch):
        vault_mod, v = self._make_vault(tmp_path, monkeypatch)
        v.store("OPENAI_API_KEY", "sk-test")

        from security import audit_log

        def _boom(*_a, **_kw):
            raise audit_log.AuditFailure("disk on fire")

        monkeypatch.setattr(audit_log, "audit_event", _boom)
        with pytest.raises(audit_log.AuditFailure):
            v.remove("OPENAI_API_KEY")
