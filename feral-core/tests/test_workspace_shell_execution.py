"""Workspace-scoped host execution for the developer shell.

The bug this pins: FERAL advertised ``bash`` / ``read_file`` / ``edit_file``
/ ``grep_search`` but ``coding_tools.json`` declared ``requires_sandbox:
true`` on ``bash``, so ``skills/executor.py`` returned HTTP 503 before
dispatch on every default macOS install (no Docker). The advertised
capability never ran. And the Docker path would not have helped: the
container mounts nothing from the host, so a developer shell there cannot
see the operator's project.

``security/exec_mode.resolve_execution_mode`` replaces that per-endpoint
boolean with a decision over (command, resolved cwd, autonomy mode, grant
state). These tests prove both directions:

  * inside a granted workspace the agent runs ``git status`` / ``ls`` /
    ``pytest`` on the host,
  * a command whose cwd or path arguments resolve outside every grant is
    refused,
  * generated-code skills still require Docker, grant or no grant.
"""

from __future__ import annotations

import subprocess

import pytest

from security.exec_mode import (
    MODE_DOCKER,
    MODE_HOST_WORKSPACE,
    MODE_REFUSED,
    NEEDS_DOCKER,
    NEEDS_POLICY,
    NEEDS_WORKSPACE_GRANT,
    resolve_execution_mode,
)
from security.sandbox_policy import SandboxPolicy
from skills.impl.coding_tools import CodingToolsSkill


def _granted_policy(path) -> SandboxPolicy:
    policy = SandboxPolicy.load_default()
    policy.grant_folder(str(path), mode="readwrite")
    return policy


# ── generated code keeps the hard Docker requirement ──────────────


def test_generated_code_requires_docker_even_inside_a_grant(tmp_path):
    """A grant authorises the operator's own shell, not generated programs."""
    policy = _granted_policy(tmp_path)

    decision = resolve_execution_mode(
        "print('hi')",
        policy=policy,
        cwd=str(tmp_path),
        skill_id="code_interpreter",
        docker_available=False,
    )

    assert decision.mode == MODE_REFUSED
    assert decision.needs == NEEDS_DOCKER
    assert MODE_DOCKER in decision.reason
    assert "grant" in decision.reason


def test_generated_code_uses_docker_when_available(tmp_path):
    decision = resolve_execution_mode(
        "print('hi')",
        policy=_granted_policy(tmp_path),
        cwd=str(tmp_path),
        skill_id="workspace_scripts",
        docker_available=True,
    )
    assert decision.mode == MODE_DOCKER


def test_requires_sandbox_declaration_forces_docker(tmp_path):
    """Any manifest may still declare ``requires_sandbox: true`` and get the
    old behaviour; the declaration only ever tightens."""
    decision = resolve_execution_mode(
        "ls",
        policy=_granted_policy(tmp_path),
        cwd=str(tmp_path),
        skill_id="coding_tools",
        requires_sandbox=True,
        docker_available=False,
    )
    assert decision.mode == MODE_REFUSED
    assert decision.needs == NEEDS_DOCKER


# ── the developer shell inside a grant ────────────────────────────


def test_developer_shell_inside_a_grant_runs_on_host(tmp_path):
    decision = resolve_execution_mode(
        "git status",
        policy=_granted_policy(tmp_path),
        cwd=str(tmp_path),
        skill_id="coding_tools",
        docker_available=False,
    )
    assert decision.mode == MODE_HOST_WORKSPACE
    assert decision.workspace_source == "grant"
    assert decision.cwd == str(tmp_path)


def test_subdirectory_of_a_grant_is_still_inside_it(tmp_path):
    nested = tmp_path / "src" / "pkg"
    nested.mkdir(parents=True)
    decision = resolve_execution_mode(
        "ls",
        policy=_granted_policy(tmp_path),
        cwd=str(nested),
        skill_id="coding_tools",
    )
    assert decision.mode == MODE_HOST_WORKSPACE
    assert decision.workspace == str(tmp_path.resolve())


# ── outside every grant ───────────────────────────────────────────


def test_developer_shell_outside_every_grant_is_refused(tmp_path):
    outside = tmp_path / "ungranted"
    outside.mkdir()
    decision = resolve_execution_mode(
        "ls",
        policy=SandboxPolicy.load_default(),
        cwd=str(outside),
        skill_id="coding_tools",
    )
    assert decision.mode == MODE_REFUSED
    assert decision.needs == NEEDS_WORKSPACE_GRANT
    assert decision.denied_path == str(outside)
    assert "outside every granted workspace" in decision.reason


def test_blocked_path_is_refused_even_when_a_read_path_covers_it(tmp_path):
    """``blocked_paths`` wins over every other scope, as it does for
    ``can_read_path``."""
    secret = tmp_path / "secrets"
    secret.mkdir()
    policy = SandboxPolicy({
        "filesystem": {
            "read_paths": [str(tmp_path)],
            "write_paths": [],
            "blocked_paths": [str(secret)],
        },
        "execution": {"allow_shell_commands": True},
    })
    decision = resolve_execution_mode("ls", policy=policy, cwd=str(secret))
    assert decision.mode == MODE_REFUSED
    assert decision.needs == NEEDS_WORKSPACE_GRANT
    assert "blocked_paths" in decision.reason


def test_shell_disabled_by_policy_refuses_before_anything_else(tmp_path):
    policy = SandboxPolicy({
        "filesystem": {"read_paths": [str(tmp_path)], "write_paths": [], "blocked_paths": []},
        "execution": {"allow_shell_commands": False},
    })
    decision = resolve_execution_mode("ls", policy=policy, cwd=str(tmp_path))
    assert decision.mode == MODE_REFUSED
    assert decision.needs == NEEDS_POLICY
    assert "allow_shell_commands" in decision.reason


# ── autonomy mode is part of the decision ─────────────────────────


def test_strict_autonomy_demands_an_explicit_grant(tmp_path):
    """A policy read path (``~/.feral/``) is enough for hybrid/loose but not
    for strict, which requires the operator to have named the folder."""
    policy = SandboxPolicy({
        "filesystem": {"read_paths": [str(tmp_path)], "write_paths": [], "blocked_paths": []},
        "execution": {"allow_shell_commands": True},
    })

    hybrid = resolve_execution_mode("ls", policy=policy, cwd=str(tmp_path), autonomy_mode="hybrid")
    assert hybrid.mode == MODE_HOST_WORKSPACE
    assert hybrid.workspace_source == "policy_read_path"

    strict = resolve_execution_mode("ls", policy=policy, cwd=str(tmp_path), autonomy_mode="strict")
    assert strict.mode == MODE_REFUSED
    assert strict.needs == NEEDS_WORKSPACE_GRANT
    assert "strict" in strict.reason


def test_strict_autonomy_accepts_an_explicit_grant(tmp_path):
    decision = resolve_execution_mode(
        "ls", policy=_granted_policy(tmp_path), cwd=str(tmp_path), autonomy_mode="strict",
    )
    assert decision.mode == MODE_HOST_WORKSPACE


# ── one filesystem policy for the whole toolset ───────────────────


def test_path_argument_denied_to_read_file_is_denied_to_bash_too(tmp_path):
    """The two halves of the toolset used to disagree: ``read_file`` called
    ``_check_read`` and ``bash`` called nothing, so a path denied to the
    file tool was reachable by naming it on the command line."""
    policy = _granted_policy(tmp_path)
    assert policy.can_read_path("/etc/passwd") is False

    decision = resolve_execution_mode(
        "cat /etc/passwd", policy=policy, cwd=str(tmp_path), skill_id="coding_tools",
    )
    assert decision.mode == MODE_REFUSED
    assert decision.denied_path == "/etc/passwd"
    assert "filesystem policy denies" in decision.reason


def test_relative_paths_inside_the_workspace_are_allowed(tmp_path):
    (tmp_path / "tests").mkdir()
    decision = resolve_execution_mode(
        "python3 -m pytest tests/ -q",
        policy=_granted_policy(tmp_path),
        cwd=str(tmp_path),
        skill_id="coding_tools",
    )
    assert decision.mode == MODE_HOST_WORKSPACE


def test_option_flags_are_not_mistaken_for_paths(tmp_path):
    decision = resolve_execution_mode(
        "rg --glob=*.py --color=never pattern .",
        policy=_granted_policy(tmp_path),
        cwd=str(tmp_path),
        skill_id="coding_tools",
    )
    assert decision.mode == MODE_HOST_WORKSPACE


# ── end-to-end through the skill ──────────────────────────────────


@pytest.mark.asyncio
async def test_bash_runs_git_ls_and_pytest_inside_a_granted_workspace(monkeypatch, tmp_path):
    """The headline regression: no Docker, and the developer shell works."""
    skill = CodingToolsSkill()
    monkeypatch.setattr(skill, "_resolve_docker_sandbox", lambda: None)
    _granted_policy(tmp_path)

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("hello\n")

    for command, expected in (
        ("git status --porcelain", "README.md"),
        ("ls", "README.md"),
        ("python3 -m pytest --version", "pytest"),
    ):
        result = await skill.execute("bash", {"command": command, "cwd": str(tmp_path)}, vault={})
        assert result["success"] is True, (command, result)
        data = result["data"]
        assert data["execution_mode"] == MODE_HOST_WORKSPACE
        assert data["workspace"] == str(tmp_path.resolve())
        assert expected in data["stdout"], (command, data)


@pytest.mark.asyncio
async def test_bash_outside_every_grant_asks_for_permission(monkeypatch, tmp_path):
    """The refusal reuses the ``permission_needed`` contract the file tools
    speak, so ``ToolRunner`` raises the same Allow/Deny folder card."""
    skill = CodingToolsSkill()
    monkeypatch.setattr(skill, "_resolve_docker_sandbox", lambda: None)
    outside = tmp_path / "not-granted"
    outside.mkdir()

    result = await skill.execute("bash", {"command": "ls", "cwd": str(outside)}, vault={})

    assert result["success"] is False
    assert result["status_code"] == 403
    data = result["data"]
    assert data["permission_needed"] is True
    assert data["needs"] == NEEDS_WORKSPACE_GRANT
    assert data["path"] == str(outside)
    assert data["operation"] == "read"
    assert "stdout" not in data


@pytest.mark.asyncio
async def test_bash_honours_the_cwd_parameter(monkeypatch, tmp_path):
    """``cwd`` used to be hardcoded to ``os.environ.get("FERAL_CWD")``, so
    the model could not point the shell at a directory at all."""
    skill = CodingToolsSkill()
    monkeypatch.setattr(skill, "_resolve_docker_sandbox", lambda: None)
    monkeypatch.delenv("FERAL_CWD", raising=False)
    _granted_policy(tmp_path)

    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "left-marker").touch()
    (right / "right-marker").touch()

    for target, marker in ((left, "left-marker"), (right, "right-marker")):
        result = await skill.execute("bash", {"command": "ls", "cwd": str(target)}, vault={})
        assert result["success"] is True
        assert marker in result["data"]["stdout"]
        assert result["data"]["cwd"] == str(target)


@pytest.mark.asyncio
async def test_bash_falls_back_to_feral_cwd_env(monkeypatch, tmp_path):
    """Without an explicit ``cwd`` the old ``FERAL_CWD`` pin still applies."""
    skill = CodingToolsSkill()
    monkeypatch.setattr(skill, "_resolve_docker_sandbox", lambda: None)
    monkeypatch.setenv("FERAL_CWD", str(tmp_path))
    _granted_policy(tmp_path)
    (tmp_path / "env-marker").touch()

    result = await skill.execute("bash", {"command": "ls"}, vault={})
    assert result["success"] is True
    assert "env-marker" in result["data"]["stdout"]


@pytest.mark.asyncio
async def test_bash_cannot_read_a_file_the_file_tool_refuses(monkeypatch, tmp_path):
    """Same denial from both halves of the toolset."""
    skill = CodingToolsSkill()
    monkeypatch.setattr(skill, "_resolve_docker_sandbox", lambda: None)
    _granted_policy(tmp_path)

    outside_file = tmp_path.parent / "outside-secret.txt"
    outside_file.write_text("classified\n")

    read = await skill.execute("read_file", {"path": str(outside_file)}, vault={})
    assert read["success"] is False
    assert read["status_code"] == 403

    shell = await skill.execute(
        "bash", {"command": f"cat {outside_file}", "cwd": str(tmp_path)}, vault={},
    )
    assert shell["success"] is False
    assert shell["status_code"] == 403
    assert "classified" not in str(shell.get("data"))
