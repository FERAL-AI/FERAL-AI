"""The ``external_agents`` setup step.

Two properties matter more than the rest and are asserted directly on the
argv rather than on a message the step prints:

1. ``--no-modify-path`` is always passed, so the wizard can never append
   a PATH line to somebody's ``.zshrc``;
2. ``--version <pinned>`` is always passed, so "install opencode" never
   resolves to whatever shipped this morning.

Nothing in this file downloads anything. The installer is invoked through
an injected ``runner`` so the argv is proved without a 45 MB fetch and
without touching the machine running the tests.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CORE))

from bridges import catalog  # noqa: E402
from cli.setup.state import WizardState  # noqa: E402
from cli.setup.steps import external_agents as step  # noqa: E402


@pytest.fixture
def state(tmp_path):
    return WizardState(home=tmp_path)


class Console:
    def __init__(self):
        self.lines: list[str] = []

    def print(self, text="", *args, **kwargs):
        self.lines.append(str(text))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def console(monkeypatch):
    fake = Console()
    monkeypatch.setattr(step, "get_console", lambda: fake)
    return fake


class TestInstallerArgv:
    def test_pins_the_version_and_never_modifies_path(self):
        argv = step._installer_argv("1.18.10")
        assert argv[0] == "bash"
        command = argv[2]
        assert "--version 1.18.10" in command
        assert "--no-modify-path" in command
        assert step.INSTALLER_URL in command

    def test_the_default_version_is_the_catalogue_pin(self):
        assert catalog.DEFAULT_OPENCODE_VERSION == "1.18.10"
        from config.loader import DEFAULT_SETTINGS

        assert (
            DEFAULT_SETTINGS["external_agents"]["opencode_version"]
            == catalog.DEFAULT_OPENCODE_VERSION
        )

    def test_no_shell_profile_is_ever_named(self):
        command = step._installer_argv("1.2.3")[2]
        for profile in (".zshrc", ".bashrc", ".profile", "config.fish", "PATH="):
            assert profile not in command

    def test_a_failed_install_reports_the_last_line_not_a_traceback(self):
        def runner(_argv):
            return subprocess.CompletedProcess(
                _argv, 1, stdout="", stderr="curl: (6) Could not resolve host"
            )

        ok, message = step.install_opencode("1.18.10", runner=runner)
        assert ok is False
        assert "Could not resolve host" in message

    def test_an_exploding_runner_is_reported_not_raised(self):
        def runner(_argv):
            raise subprocess.TimeoutExpired("bash", 600)

        ok, message = step.install_opencode("1.18.10", runner=runner)
        assert ok is False
        assert "TimeoutExpired" in message

    def test_success_is_reported_as_such(self):
        def runner(argv):
            return subprocess.CompletedProcess(argv, 0, stdout="done", stderr="")

        ok, message = step.install_opencode("1.18.10", runner=runner)
        assert ok is True
        assert message == "installed"


class TestStepBehaviour:
    async def test_an_existing_install_is_detected_and_recorded(
        self, state, console, monkeypatch, tmp_path
    ):
        binary = tmp_path / "opencode"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        monkeypatch.setattr(catalog, "find_opencode", lambda *a, **kw: str(binary))
        monkeypatch.setattr(
            step, "confirm", lambda *a, **kw: pytest.fail("must not prompt")
        )

        await step.run(state)

        assert state.get_setting("external_agents", "opencode_bin") == str(binary)
        assert "already installed" in console.text

    async def test_declining_installs_nothing_and_prints_the_command(
        self, state, console, monkeypatch
    ):
        monkeypatch.setattr(catalog, "find_opencode", lambda *a, **kw: "")
        monkeypatch.setattr(step, "confirm", lambda *a, **kw: False)
        monkeypatch.setattr(
            step,
            "install_opencode",
            lambda *a, **kw: pytest.fail("must not install when declined"),
        )

        await step.run(state)

        assert state.get_setting("external_agents", "opencode_bin", "") == ""
        assert "--no-modify-path" in console.text

    async def test_accepting_installs_the_pinned_version(
        self, state, console, monkeypatch, tmp_path
    ):
        state.set_setting("external_agents", "opencode_version", "1.18.10")
        binary = tmp_path / "opencode"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)

        calls: list[str] = []
        found = {"value": ""}

        def fake_find(explicit="", home=None):
            return found["value"]

        def fake_install(version, **_kwargs):
            calls.append(version)
            found["value"] = str(binary)
            return True, "installed"

        monkeypatch.setattr(catalog, "find_opencode", fake_find)
        monkeypatch.setattr(step, "install_opencode", fake_install)
        monkeypatch.setattr(step, "confirm", lambda *a, **kw: True)

        await step.run(state)

        assert calls == ["1.18.10"]
        assert state.get_setting("external_agents", "opencode_bin") == str(binary)

    async def test_a_failed_install_leaves_the_setting_empty(
        self, state, console, monkeypatch
    ):
        monkeypatch.setattr(catalog, "find_opencode", lambda *a, **kw: "")
        monkeypatch.setattr(step, "confirm", lambda *a, **kw: True)
        monkeypatch.setattr(
            step, "install_opencode", lambda *a, **kw: (False, "network is down")
        )

        await step.run(state)

        assert state.get_setting("external_agents", "opencode_bin", "") == ""
        assert "network is down" in console.text

    async def test_claude_code_and_codex_are_named_but_not_installed(
        self, state, console, monkeypatch
    ):
        monkeypatch.setattr(catalog, "find_opencode", lambda *a, **kw: "/usr/bin/oc")
        monkeypatch.setattr(
            step, "confirm", lambda *a, **kw: pytest.fail("must not prompt")
        )
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("no installs"))

        await step.run(state)

        assert "@zed-industries/claude-code-acp" in console.text
        assert "@agentclientprotocol/codex-acp" in console.text
        assert "no ACP mode of their own" in console.text

    async def test_hermes_is_reported_but_never_offered(
        self, state, console, monkeypatch
    ):
        monkeypatch.setattr(catalog, "find_opencode", lambda *a, **kw: "/usr/bin/oc")
        monkeypatch.setattr(
            shutil,
            "which",
            lambda name: "/usr/local/bin/hermes-acp" if name == "hermes-acp" else None,
        )

        await step.run(state)

        assert "Detected hermes" in console.text
        assert "never install" in console.text

    async def test_hermes_is_silent_when_absent(self, state, console, monkeypatch):
        monkeypatch.setattr(catalog, "find_opencode", lambda *a, **kw: "/usr/bin/oc")
        monkeypatch.setattr(shutil, "which", lambda _name: None)

        await step.run(state)

        assert "hermes" not in console.text


class TestWizardWiring:
    def test_the_step_is_in_the_flow_with_a_title(self):
        import inspect

        import cli.setup as setup_pkg
        from cli.setup.state_machine import _STEP_TITLES

        source = inspect.getsource(setup_pkg._run_async)
        assert '("external_agents"' in source
        assert "external_agents" in _STEP_TITLES


def test_no_em_dashes_in_the_step_or_the_bridge():
    """House rule, checked on raw bytes and on the decoded text."""
    targets = [
        CORE / "cli" / "setup" / "steps" / "external_agents.py",
        CORE / "bridges" / "acp.py",
        CORE / "bridges" / "jsonrpc.py",
        CORE / "bridges" / "permissions.py",
        CORE / "bridges" / "catalog.py",
        CORE / "bridges" / "sessions.py",
        CORE / "skills" / "impl" / "external_agent.py",
    ]
    for path in targets:
        raw = path.read_bytes()
        assert "—".encode("utf-8") not in raw, path
        assert b"\\u2014" not in raw, path
        assert "—" not in raw.decode("utf-8"), path
