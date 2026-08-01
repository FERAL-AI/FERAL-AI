"""Child-process environment jails.

The defect this addresses: ``subprocess`` inherits ``os.environ``, so a
spawned coding agent gets the operator's real ``HOME`` and can read
``~/.claude``, ``~/.codex``, ``~/.aws`` and ``~/.ssh`` without doing
anything clever. ``bridges/acp.py`` already accepts a full environment
replacement; what was missing was a sane default to hand it.

These tests also pin the *limits*, because a jail that is believed to do
more than it does is worse than no jail. In particular they assert that
an explicitly allowed API key does pass through, so nobody mistakes this
for credential isolation.
"""

from __future__ import annotations

import os
from pathlib import Path

from security.env_jail import (
    CODING_AGENT_ALLOW,
    PASSTHROUGH,
    build_child_env,
    build_coding_agent_env,
    env_jail,
)


FAKE_PARENT = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/Users/operator",
    "LANG": "en_US.UTF-8",
    "TERM": "xterm-256color",
    "USER": "operator",
    "LOGNAME": "operator",
    "SSH_AUTH_SOCK": "/tmp/ssh-agent.sock",
    "ANTHROPIC_API_KEY": "sk-ant-secret",
    "OPENAI_API_KEY": "sk-openai-secret",
    "AWS_SECRET_ACCESS_KEY": "aws-secret",
    "HTTPS_PROXY": "http://user:pass@proxy.internal:8080",
    "GITHUB_TOKEN": "ghp_secret",
    "CLAUDE_CONFIG_DIR": "/Users/operator/.claude",
    "NODE_OPTIONS": "--require /Users/operator/evil.js",
}


def _jail(**kwargs):
    return build_child_env(source=FAKE_PARENT, **kwargs)


# ── what the jail keeps out ───────────────────────────────────────


def test_home_is_replaced_with_a_fresh_directory():
    """The whole point: ``~`` must not resolve to the operator's home."""
    jail = _jail()
    try:
        assert jail.env["HOME"] != FAKE_PARENT["HOME"]
        assert jail.env["HOME"] == jail.home
        assert Path(jail.home).is_dir()
        # Nothing in it but the XDG scaffolding this module created.
        assert {p.name for p in Path(jail.home).iterdir()} <= {".config", ".cache", ".local"}
    finally:
        jail.cleanup()


def test_agent_config_directories_are_not_reachable_via_home():
    jail = _jail()
    try:
        assert not (Path(jail.env["HOME"]) / ".claude").exists()
        assert not (Path(jail.env["HOME"]) / ".codex").exists()
        assert not (Path(jail.env["HOME"]) / ".ssh").exists()
    finally:
        jail.cleanup()


def test_xdg_and_tmpdir_point_inside_the_jail():
    """A tool that honours XDG must not fall back to the real config."""
    jail = _jail()
    try:
        for key in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
                    "XDG_STATE_HOME", "TMPDIR"):
            assert jail.env[key].startswith(jail.home)
    finally:
        jail.cleanup()


def test_secrets_are_dropped_unless_named():
    jail = _jail()
    try:
        for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY",
                     "GITHUB_TOKEN", "SSH_AUTH_SOCK"):
            assert name not in jail.env, f"{name} leaked into the child env"
    finally:
        jail.cleanup()


def test_proxy_variables_are_dropped_by_default():
    """A proxy URL often carries credentials, and redirects child traffic."""
    jail = _jail()
    try:
        assert "HTTPS_PROXY" not in jail.env
    finally:
        jail.cleanup()


def test_operator_identity_is_dropped():
    jail = _jail()
    try:
        assert "USER" not in jail.env
        assert "LOGNAME" not in jail.env
    finally:
        jail.cleanup()


def test_injection_vectors_like_node_options_are_dropped():
    jail = _jail()
    try:
        assert "NODE_OPTIONS" not in jail.env
        assert "CLAUDE_CONFIG_DIR" not in jail.env
    finally:
        jail.cleanup()


def test_dropped_names_are_reported():
    """So a caller can log the difference rather than debug a child crash."""
    jail = _jail()
    try:
        assert "ANTHROPIC_API_KEY" in jail.dropped
        assert "PATH" not in jail.dropped
    finally:
        jail.cleanup()


# ── what the jail keeps in ────────────────────────────────────────


def test_toolchain_basics_survive():
    jail = _jail()
    try:
        assert jail.env["PATH"] == FAKE_PARENT["PATH"]
        assert jail.env["LANG"] == FAKE_PARENT["LANG"]
        assert jail.env["TERM"] == FAKE_PARENT["TERM"]
    finally:
        jail.cleanup()


def test_path_falls_back_when_the_parent_has_none():
    jail = build_child_env(source={})
    try:
        assert "/usr/bin" in jail.env["PATH"]
    finally:
        jail.cleanup()


def test_the_passthrough_list_stays_small():
    """Every addition is a channel from the operator's session inward.

    This is a review tripwire, not a hard limit: if it fires, the right
    move is to justify the additions, not to raise the number.
    """
    assert len(PASSTHROUGH) <= 20


# ── the documented limitation ─────────────────────────────────────


def test_an_explicitly_allowed_key_does_pass_through():
    """This is NOT credential isolation, and the test says so out loud.

    An agent that must call a model needs a key. ``allow`` hands it one.
    qm's own passthrough bakes provider keys in by default; FERAL's does
    not, but a caller that names one lands in exactly the same place.
    """
    jail = _jail(allow=("ANTHROPIC_API_KEY",))
    try:
        assert jail.env["ANTHROPIC_API_KEY"] == "sk-ant-secret"
    finally:
        jail.cleanup()


def test_extra_sets_values_without_reading_the_parent():
    """The better way to pass a scoped token: it never exists upstream."""
    jail = build_child_env(source={}, extra={"FERAL_SESSION_TOKEN": "scoped-abc"})
    try:
        assert jail.env["FERAL_SESSION_TOKEN"] == "scoped-abc"
    finally:
        jail.cleanup()


def test_absolute_paths_are_still_reachable(tmp_path):
    """A fresh HOME does not stop ``cat /Users/op/.ssh/id_rsa``.

    Pinning the limitation so nobody later treats the jail as a
    filesystem boundary. Only an OS sandbox does that.
    """
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATE KEY")
    jail = _jail()
    try:
        assert Path(secret).read_text() == "PRIVATE KEY"
        assert str(secret) not in str(jail.env)
    finally:
        jail.cleanup()


# ── operator override and lifecycle ───────────────────────────────


def test_operator_allowlist_env_var_widens_the_jail(monkeypatch):
    monkeypatch.setenv("FERAL_ENV_JAIL_ALLOW", "GITHUB_TOKEN, NODE_OPTIONS")
    jail = _jail()
    try:
        assert jail.env["GITHUB_TOKEN"] == "ghp_secret"
        assert jail.env["NODE_OPTIONS"] == FAKE_PARENT["NODE_OPTIONS"]
    finally:
        jail.cleanup()


def test_allowlist_env_var_defaults_to_empty(monkeypatch):
    monkeypatch.delenv("FERAL_ENV_JAIL_ALLOW", raising=False)
    jail = _jail()
    try:
        assert "GITHUB_TOKEN" not in jail.env
    finally:
        jail.cleanup()


def test_context_manager_removes_the_jail_directory():
    with env_jail(source=FAKE_PARENT) as jail:
        home = jail.home
        assert Path(home).is_dir()
    assert not Path(home).exists()


def test_cleanup_is_idempotent():
    jail = _jail()
    jail.cleanup()
    jail.cleanup()


def test_real_environ_is_the_default_source():
    """``source`` is a test seam; production reads the real environment."""
    jail = build_child_env()
    try:
        assert jail.env.get("PATH") == os.environ.get("PATH", jail.env["PATH"])
        assert jail.env["HOME"] != os.environ.get("HOME")
    finally:
        jail.cleanup()


# ── the coding-agent profile ──────────────────────────────────────
#
# ``build_child_env`` is the general jail; ``build_coding_agent_env`` is
# what ``AcpAgentProcess.spawn`` actually uses, and it has to answer
# "does the agent still work". The line it draws: reaching a model is
# in, everything else is out.


def test_a_coding_agent_keeps_its_model_keys():
    """An agent that cannot call a model is not an agent."""
    jail = build_coding_agent_env(source=FAKE_PARENT)
    try:
        assert jail.env["ANTHROPIC_API_KEY"] == "sk-ant-secret"
        assert jail.env["OPENAI_API_KEY"] == "sk-openai-secret"
    finally:
        jail.cleanup()


def test_a_coding_agent_still_loses_its_home_and_everything_else():
    """The profile widens the allowlist. It does not weaken the jail."""
    jail = build_coding_agent_env(source=FAKE_PARENT)
    try:
        assert jail.env["HOME"] != FAKE_PARENT["HOME"]
        for name in ("GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "SSH_AUTH_SOCK",
                     "HTTPS_PROXY", "NODE_OPTIONS", "CLAUDE_CONFIG_DIR",
                     "USER", "LOGNAME"):
            assert name not in jail.env, f"{name} leaked to the coding agent"
    finally:
        jail.cleanup()


def test_the_coding_allowlist_is_only_model_access():
    """A review tripwire on scope creep.

    If something lands here that is not a model endpoint or its key, the
    jail has quietly become a passthrough. Widen via
    ``FERAL_ENV_JAIL_ALLOW`` instead.
    """
    for name in CODING_AGENT_ALLOW:
        assert name.endswith(("_API_KEY", "_AUTH_TOKEN", "_BASE_URL")), name
    assert "GITHUB_TOKEN" not in CODING_AGENT_ALLOW
    assert "SSH_AUTH_SOCK" not in CODING_AGENT_ALLOW


def test_the_coding_profile_composes_with_an_extra_allow():
    jail = build_coding_agent_env(source=FAKE_PARENT, allow=("GITHUB_TOKEN",))
    try:
        assert jail.env["GITHUB_TOKEN"] == "ghp_secret"
        assert jail.env["ANTHROPIC_API_KEY"] == "sk-ant-secret"
    finally:
        jail.cleanup()
