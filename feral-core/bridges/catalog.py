"""Which external coding agents exist, how to launch them, are they installed.

This is the only module in ``bridges/`` that knows about FERAL settings.
The protocol layers below it stay dependency-free so they can be tested
without a configured brain.

Why the launch commands look the way they do
============================================
Only ONE of the three agents in this catalogue speaks ACP itself:

* **opencode** ships ``opencode acp`` in the binary. Nothing else needed.
* **Claude Code** does not. ``claude --help`` on 2.1.220 has no ``acp``
  subcommand and no stdio JSON-RPC flag at all. Zed publishes an adapter,
  ``@zed-industries/claude-code-acp``, which is a Node process that
  speaks ACP on stdio and drives Claude Code through the Claude Agent
  SDK. So the launch command is that adapter, not ``claude``, and the
  cost is a Node runtime plus one npm package.
* **Codex** is the same story with ``@agentclientprotocol/codex-acp``.

Neither adapter is bundled or auto-installed here. ``launch_command``
returns what to run; ``resolve`` reports honestly whether it is present.
An agent the operator has not installed shows up as unavailable with the
install hint, rather than being spawned and failing with ENOENT halfway
into a turn.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# Pinned by the setup step. Bump deliberately: a coding agent is a large
# binary with its own permission engine, and "whatever is latest today"
# is not a thing to resolve at install time on a user's machine.
DEFAULT_OPENCODE_VERSION = "1.18.10"

# The installer hard-codes this; it is not overridable by an env var.
OPENCODE_INSTALL_SUBPATH = os.path.join(".opencode", "bin", "opencode")


@dataclass
class AgentSpec:
    """A coding agent FERAL knows how to drive."""

    agent_id: str
    display_name: str
    # Argv template. The first element is resolved against PATH (or an
    # explicit override) before use.
    command: list[str]
    native_acp: bool
    install_hint: str
    # Non-empty when the agent needs a separate shim to speak ACP at all.
    shim_package: str = ""
    notes: str = ""


CATALOG: dict[str, AgentSpec] = {
    "opencode": AgentSpec(
        agent_id="opencode",
        display_name="opencode",
        command=["opencode", "acp"],
        native_acp=True,
        install_hint=(
            "curl -fsSL https://opencode.ai/install | bash -s -- "
            f"--version {DEFAULT_OPENCODE_VERSION} --no-modify-path"
        ),
        notes=(
            "Single compiled binary, no Node or Bun at runtime. Speaks ACP "
            "directly via the 'acp' subcommand."
        ),
    ),
    "claude_code": AgentSpec(
        agent_id="claude_code",
        display_name="Claude Code",
        command=["claude-code-acp"],
        native_acp=False,
        shim_package="@zed-industries/claude-code-acp",
        install_hint="npm install -g @zed-industries/claude-code-acp",
        notes=(
            "Claude Code itself has no ACP mode. Zed's adapter wraps the "
            "Claude Agent SDK and speaks ACP on stdio; it needs Node and a "
            "working Claude Code auth."
        ),
    ),
    "codex": AgentSpec(
        agent_id="codex",
        display_name="Codex CLI",
        command=["codex-acp"],
        native_acp=False,
        shim_package="@agentclientprotocol/codex-acp",
        install_hint="npm install -g @agentclientprotocol/codex-acp",
        notes=(
            "Codex CLI has no ACP mode either. The adapter starts the Codex "
            "app server and translates ACP onto it; it needs Node."
        ),
    ),
}

# hermes is deliberately absent from CATALOG. It implements ACP
# (``hermes-acp``) but it is a source checkout, not a binary: its
# setup.py blocks wheel builds, it rewrites sys.path globally, and it
# pins openai / Pillow / cryptography against FERAL's own pins. FERAL
# detects it if the operator already runs it and otherwise leaves it
# alone. See ``detect_hermes``.
HERMES_ENTRYPOINT = "hermes-acp"


@dataclass
class ResolvedAgent:
    """A catalogue entry checked against this machine."""

    spec: AgentSpec
    available: bool
    command: list[str] = field(default_factory=list)
    binary_path: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.spec.agent_id,
            "display_name": self.spec.display_name,
            "available": self.available,
            "native_acp": self.spec.native_acp,
            "binary_path": self.binary_path,
            "command": list(self.command),
            "shim_package": self.spec.shim_package,
            "install_hint": self.spec.install_hint,
            "notes": self.spec.notes,
            "reason": self.reason,
        }


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------

def _as_bool(value: Any, default: bool = True) -> bool:
    """Coerce a settings value to bool, tolerating JSON and string forms.

    A settings file hand-edited to ``"false"`` must not read as True,
    which is what a bare ``bool()`` would do with a non-empty string.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() not in ("false", "0", "no", "off", "")


def external_agent_settings(settings: Optional[dict] = None) -> dict[str, Any]:
    """Read the ``external_agents`` block out of merged FERAL settings.

    Reads (this is the reader ``tests/test_settings_keys_have_readers.py``
    looks for): ``external_agents.default_agent``,
    ``external_agents.opencode_bin``, ``external_agents.opencode_version``,
    ``external_agents.permission_timeout_seconds`` and
    ``external_agents.env_jail``.
    """
    if settings is None:
        try:
            from config.loader import load_settings

            settings = load_settings()
        except Exception:
            settings = {}
    block = (settings or {}).get("external_agents") or {}
    if not isinstance(block, dict):
        block = {}

    try:
        timeout = float(block.get("permission_timeout_seconds", 120) or 120)
    except (TypeError, ValueError):
        timeout = 120.0

    return {
        "default_agent": str(block.get("default_agent") or "opencode"),
        "opencode_bin": str(block.get("opencode_bin") or ""),
        "opencode_version": str(
            block.get("opencode_version") or DEFAULT_OPENCODE_VERSION
        ),
        # Floored, not clamped to a default: 0 would mean "reject every
        # permission instantly", which is a footgun disguised as a value.
        "permission_timeout_seconds": max(5.0, timeout),
        # Spawn agents in ``security.env_jail`` rather than the
        # operator's environment. On by default. The escape hatch exists
        # because the jail replaces HOME, and an operator authenticated
        # by a Claude or ChatGPT subscription rather than an API key has
        # their login in ``~/.claude`` / ``~/.codex`` where a jailed
        # child cannot see it. Turning it off restores the old
        # behaviour: the agent inherits everything, keys included.
        "env_jail": _as_bool(block.get("env_jail", True)),
    }


# ----------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------

def opencode_install_dir(home: Optional[str] = None) -> Path:
    """Where the official installer puts the binary. Not configurable there."""
    return Path(home or os.path.expanduser("~")) / ".opencode" / "bin"


def find_opencode(explicit: str = "", home: Optional[str] = None) -> str:
    """Absolute path to an opencode binary, or "" when there is none.

    Order: explicit setting, then PATH, then the installer's own
    directory (which ``--no-modify-path`` deliberately leaves off PATH,
    so looking there is the whole point).
    """
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        return ""

    found = shutil.which("opencode")
    if found:
        return found

    candidate = opencode_install_dir(home) / "opencode"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return ""


def detect_hermes() -> dict[str, Any]:
    """Report whether hermes is on this machine. Never offers to install it."""
    path = shutil.which(HERMES_ENTRYPOINT)
    return {
        "agent_id": "hermes",
        "display_name": "hermes",
        "installed": bool(path),
        "binary_path": path or "",
        "installable_by_feral": False,
        "reason": (
            "hermes is a source checkout, not a distributable binary: its "
            "setup.py blocks wheel builds, it rewrites sys.path globally, and "
            "its openai / Pillow / cryptography pins conflict with FERAL's. "
            "FERAL detects an existing install and drives it over ACP, but "
            "will not install it."
        ),
    }


def resolve(agent_id: str, settings: Optional[dict] = None) -> ResolvedAgent:
    """Check one catalogue entry against this machine."""
    spec = CATALOG.get(agent_id)
    if spec is None:
        placeholder = AgentSpec(
            agent_id=agent_id,
            display_name=agent_id,
            command=[],
            native_acp=False,
            install_hint="",
        )
        return ResolvedAgent(
            spec=placeholder, available=False, reason=f"unknown agent {agent_id!r}"
        )

    conf = external_agent_settings(settings)

    if spec.agent_id == "opencode":
        binary = find_opencode(conf["opencode_bin"])
        if not binary:
            return ResolvedAgent(
                spec=spec,
                available=False,
                reason=(
                    "opencode is not installed. Run `feral setup` or: "
                    + spec.install_hint
                ),
            )
        return ResolvedAgent(
            spec=spec,
            available=True,
            binary_path=binary,
            command=[binary] + spec.command[1:],
        )

    binary = shutil.which(spec.command[0]) if spec.command else ""
    if not binary:
        return ResolvedAgent(
            spec=spec,
            available=False,
            reason=(
                f"{spec.display_name} has no native ACP mode; it needs the "
                f"{spec.shim_package} shim, which is not on PATH. Install it "
                f"with: {spec.install_hint}"
            ),
        )
    return ResolvedAgent(
        spec=spec,
        available=True,
        binary_path=binary,
        command=[binary] + spec.command[1:],
    )


def resolve_all(settings: Optional[dict] = None) -> list[ResolvedAgent]:
    return [resolve(agent_id, settings) for agent_id in CATALOG]


def launch_command(agent_id: str, settings: Optional[dict] = None) -> list[str]:
    """Argv for spawning ``agent_id``. Raises when it is not installed."""
    resolved = resolve(agent_id, settings)
    if not resolved.available:
        raise FileNotFoundError(resolved.reason)
    return resolved.command
