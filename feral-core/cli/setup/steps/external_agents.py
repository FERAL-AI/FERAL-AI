"""Offer to install opencode so FERAL can drive an external coding agent.

What this step will and will not do
===================================
* It installs **opencode only**, at the pinned
  ``external_agents.opencode_version``, by piping the vendor's official
  installer to bash with ``--no-modify-path``. That flag is the whole
  point: the installer's default behaviour is to append a ``PATH`` line
  to ``.zshrc`` / ``.bashrc`` / ``config.fish``, and a setup wizard has
  no business rewriting an operator's shell profile. FERAL records the
  absolute path in ``external_agents.opencode_bin`` instead and launches
  the binary by that path, so nothing needs to be on ``PATH`` at all.
* It **detects** an existing opencode (PATH first, then
  ``~/.opencode/bin``) and offers to leave it alone.
* It **detects hermes** and never offers to install it. hermes speaks
  ACP through ``hermes-acp``, but it is a source checkout rather than a
  distributable binary, so "install hermes" is not a thing this wizard
  can honestly offer.
* It does **not** install the Claude Code or Codex ACP shims. Neither CLI
  speaks ACP natively; both need a Node package
  (``@zed-industries/claude-code-acp`` and
  ``@agentclientprotocol/codex-acp``). Installing global npm packages
  behind a "set up FERAL" prompt is a bigger footprint than this step
  should take, so it prints the one-line command and moves on.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Callable, Optional

from ..helpers import confirm, get_console
from ..state import WizardState

INSTALLER_URL = "https://opencode.ai/install"

# Long enough for a 45 MB download on a slow link, short enough that a
# hung installer does not wedge the wizard forever.
INSTALL_TIMEOUT_SECONDS = 600


def _installer_argv(version: str) -> list[str]:
    """The exact command line. Kept as a function so tests can assert it."""
    return [
        "bash",
        "-c",
        (
            f"set -o pipefail; curl -fsSL {INSTALLER_URL} | "
            f"bash -s -- --version {version} --no-modify-path"
        ),
    ]


def install_opencode(
    version: str,
    *,
    runner: Optional[Callable[[list[str]], subprocess.CompletedProcess]] = None,
) -> tuple[bool, str]:
    """Run the vendor installer. Returns ``(ok, message)``.

    ``runner`` exists so tests can prove the argv without downloading a
    45 MB binary into the machine running them.
    """
    argv = _installer_argv(version)
    call = runner or (
        lambda cmd: subprocess.run(
            cmd, capture_output=True, text=True, timeout=INSTALL_TIMEOUT_SECONDS
        )
    )
    try:
        result = call(argv)
    except Exception as exc:  # network failure, timeout, missing curl
        return False, f"{type(exc).__name__}: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        return False, detail[-1] if detail else f"exit {result.returncode}"
    return True, "installed"


async def run(state: WizardState) -> None:
    console = get_console()
    from bridges import catalog

    version = str(
        state.get_setting("external_agents", "opencode_version", "")
        or catalog.DEFAULT_OPENCODE_VERSION
    )
    configured = str(state.get_setting("external_agents", "opencode_bin", "") or "")

    console.print(
        "FERAL can hand a coding task to an external coding agent and watch "
        "it work, over ACP (the Agent Client Protocol). opencode is the only "
        "one that ships as a single binary and speaks ACP natively."
    )

    existing = catalog.find_opencode(configured)
    if existing:
        console.print(f"  opencode is already installed at {existing}")
        state.set_setting("external_agents", "opencode_bin", existing)
    else:
        console.print(
            f"  Not installed. FERAL would fetch opencode {version} with "
            f"--no-modify-path, so your shell profile is left alone; the "
            f"binary lands in ~/.opencode/bin and FERAL calls it by full path."
        )
        if confirm(f"  Install opencode {version} now?", default=False):
            ok, message = install_opencode(version)
            if ok:
                found = catalog.find_opencode("")
                if found:
                    state.set_setting("external_agents", "opencode_bin", found)
                    console.print(f"  [green]Installed:[/] {found}")
                else:
                    console.print(
                        "  [yellow]Installer reported success but no binary "
                        "turned up in ~/.opencode/bin.[/]"
                    )
            else:
                console.print(f"  [yellow]Install failed: {message}[/]")
                console.print(f"  You can retry by hand: {_shell_hint(version)}")
        else:
            console.print(f"  Skipped. To do it later: {_shell_hint(version)}")

    _report_shims(console)
    _report_hermes(console, catalog)


def _shell_hint(version: str) -> str:
    return (
        f"curl -fsSL {INSTALLER_URL} | bash -s -- --version {version} "
        f"--no-modify-path"
    )


def _report_shims(console) -> None:
    """Say plainly that Claude Code and Codex need a shim."""
    console.print()
    console.print(
        "  Claude Code and Codex have no ACP mode of their own. Each needs a "
        "Node shim, which FERAL does not install for you:"
    )
    console.print("    Claude Code:  npm install -g @zed-industries/claude-code-acp")
    console.print("    Codex CLI:    npm install -g @agentclientprotocol/codex-acp")
    for label, binary in (("Claude Code", "claude-code-acp"), ("Codex", "codex-acp")):
        if shutil.which(binary):
            console.print(f"    [green]{label} shim already on PATH.[/]")


def _report_hermes(console, catalog) -> None:
    hermes = catalog.detect_hermes()
    if not hermes["installed"]:
        return
    console.print()
    console.print(
        f"  Detected hermes at {hermes['binary_path']}. FERAL can drive it "
        f"over ACP but will never install or upgrade it: it is a source "
        f"checkout, not a distributable binary."
    )


__all__ = ["run", "install_opencode", "_installer_argv", "INSTALLER_URL"]
