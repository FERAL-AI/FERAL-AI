"""Regression tests for the ``daemon://local/shell`` execution path.

Lane 8 §A — replaces the legacy substring blocklist + ``shell=True`` runner
with ``SandboxPolicy.validate_shell_command`` + argv exec. These tests pin
the contract end-to-end at the ``SkillExecutor._execute_local_daemon``
boundary:

  * allowlisted commands run with ``shell=False`` and the parsed argv list,
  * metacharacter injections (``$(rm -rf /)``) are rejected with 403,
  * binaries previously caught only by substring (``rm``) are now caught by
    the allowlist,
  * operator-flipped ``allow_shell_commands=False`` blocks even allowlisted
    commands,
  * unknown binaries are rejected even when free of metacharacters.
"""

from unittest.mock import MagicMock

import pytest

from security.sandbox_policy import SandboxPolicy
from skills.executor import SkillExecutor


@pytest.mark.asyncio
async def test_daemon_shell_allowlisted_command_succeeds(monkeypatch):
    """``open -a Safari`` is on the desktop allowlist; runs with shell=False."""
    captured = {}

    async def fake_to_thread(fn, *args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        result = MagicMock()
        result.stdout = "ok"
        result.stderr = ""
        result.returncode = 0
        return result

    monkeypatch.setattr("asyncio.to_thread", fake_to_thread)
    result = await SkillExecutor()._execute_local_daemon("shell", "open -a Safari")
    assert result["success"] is True
    assert captured["kwargs"].get("shell") is False
    assert captured["args"][0] == ["open", "-a", "Safari"]


@pytest.mark.asyncio
async def test_daemon_shell_metachar_injection_rejected():
    """Old blocklist passed ``$(rm -rf /)``; validator rejects it."""
    result = await SkillExecutor()._execute_local_daemon("shell", "$(rm -rf /)")
    assert result["success"] is False
    assert result.get("status_code") == 403


@pytest.mark.asyncio
async def test_daemon_shell_rm_rejected_for_real_now():
    result = await SkillExecutor()._execute_local_daemon("shell", "rm -rf /tmp/anything")
    assert result["success"] is False
    assert result.get("status_code") == 403


@pytest.mark.asyncio
async def test_daemon_shell_disabled_when_policy_off(monkeypatch):
    """Operator-flipped ``allow_shell_commands=False`` blocks even allowlisted commands."""
    policy = SandboxPolicy.load_default()
    monkeypatch.setattr(policy, "can_execute_shell", lambda: False)
    monkeypatch.setattr(SandboxPolicy, "load_default", classmethod(lambda cls: policy))
    result = await SkillExecutor()._execute_local_daemon("shell", "open -a Safari")
    assert result["success"] is False
    assert result.get("status_code") == 403


@pytest.mark.asyncio
async def test_daemon_shell_unknown_binary_rejected():
    """Binaries outside ``daemon_shell_allowlist`` are rejected."""
    result = await SkillExecutor()._execute_local_daemon("shell", "curl https://example.com")
    assert result["success"] is False
    assert result.get("status_code") == 403
