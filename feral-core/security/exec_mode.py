"""
Where a shell command is allowed to run.
=======================================

The incident: FERAL advertised ``bash`` / ``read_file`` / ``edit_file`` /
``grep_search`` as developer tools, but ``coding_tools.json`` and
``computer_use.json`` both declared ``requires_sandbox: true`` on ``bash``.
``skills/executor.py`` reads that boolean before dispatch and returns HTTP
503 whenever Docker is missing, which is the default on macOS, where the
brain boots with ``DockerSandbox: degraded (Docker not installed)``. So the
advertised capability never executed. Worse, the Docker path would not have
helped even with Docker running: ``DockerSandbox.execute_shell`` mounts
nothing from the host, so a "developer shell" there cannot see the user's
project at all. That container was built for LLM-generated code, not for a
shell the operator asked for.

The old design had exactly two outcomes for a shell request: run it in
Docker, or refuse. This module adds the third, properly-scoped one, and
turns the decision into a function of *(command, resolved cwd, operator
autonomy mode, grant state)* rather than a per-endpoint boolean.

Decision table (see :func:`resolve_execution_mode`):

===========================  ===============  =================  ==========  ==================
caller                       docker healthy   cwd resolves to    autonomy    mode
===========================  ===============  =================  ==========  ==================
generated code               yes              (not consulted)    any         ``docker``
generated code               no               (not consulted)    any         ``refused`` (docker)
developer shell + prefer     yes              (not consulted)    any         ``docker``
developer shell              any              operator grant     any         ``host_workspace``
developer shell              any              policy read path   strict      ``refused`` (grant)
developer shell              any              policy read path   hybrid/loose ``host_workspace``
developer shell              any              outside all paths  any         ``refused`` (grant)
developer shell              any              blocked path       any         ``refused`` (grant)
any                          any              (not consulted)    any         ``refused`` (policy)
                                                                             when shell is
                                                                             disabled by policy
===========================  ===============  =================  ==========  ==================

"generated code" means an endpoint that declares ``requires_sandbox: true``
or a skill in :data:`GENERATED_CODE_SKILL_IDS`: ``code_interpreter``,
``workspace_scripts``, and by extension tool-genesis output, which executes
through ``code_interpreter._run_sandboxed``. Those keep the hard Docker
requirement. Nothing in this module weakens them.

Why host execution inside a grant is in the threat model: ``SECURITY.md``
states there is exactly one trusted operator per brain, who owns the host,
the vault, and every paired device, and that FERAL's boundaries protect
that operator from prompt injection and untrusted skill code, "not
designed to keep the operator from acting against their own machine".
A folder the operator explicitly granted is inside that trust envelope.
A folder they did not grant is not, and this module refuses there.
"""

from __future__ import annotations

import logging
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from security.command_unwrap import (
    KIND_ENCODED_PAYLOAD_TO_SHELL,
    obfuscation_findings,
    scannable_command,
)
from security.sandbox_policy import SandboxPolicy

logger = logging.getLogger("feral.exec_mode")

# Execution modes.
MODE_DOCKER = "docker"
MODE_HOST_WORKSPACE = "host_workspace"
MODE_REFUSED = "refused"

# What a refusal says it needed. Surfaced to the operator so the message is
# actionable ("grant the folder" vs "start Docker") instead of a bare 503.
NEEDS_DOCKER = "docker"
NEEDS_WORKSPACE_GRANT = "workspace_grant"
NEEDS_POLICY = "policy"

# Skills whose input is code the model wrote, not a command the operator
# typed. These never run on the host, grant or no grant. The grant
# authorises the operator's own shell, not arbitrary generated programs.
# Tool-genesis output is covered transitively: it executes through
# ``skills.impl.code_interpreter._run_sandboxed``.
GENERATED_CODE_SKILL_IDS = frozenset({"code_interpreter", "workspace_scripts"})

# Autonomy modes that accept a policy-declared read path (``~/.feral/``,
# ``/tmp/feral/``) as sufficient scope for a host shell. ``strict`` demands
# an explicit operator grant from ``~/.feral/workspace_grants.json``.
_GRANT_ONLY_AUTONOMY = "strict"


@dataclass(frozen=True)
class ExecutionDecision:
    """The verdict for one shell request.

    ``mode`` is one of :data:`MODE_DOCKER`, :data:`MODE_HOST_WORKSPACE`,
    :data:`MODE_REFUSED`. On a refusal ``needs`` names the mode that would
    have been required, so the caller can render a fix rather than a
    dead end.
    """

    mode: str
    reason: str = ""
    cwd: Optional[str] = None
    workspace: Optional[str] = None
    workspace_source: str = ""
    needs: str = ""
    denied_path: Optional[str] = None

    @property
    def allowed(self) -> bool:
        return self.mode != MODE_REFUSED

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "reason": self.reason,
            "cwd": self.cwd,
            "workspace": self.workspace,
            "workspace_source": self.workspace_source,
            "needs": self.needs,
            "denied_path": self.denied_path,
        }


def unwrap_enabled() -> bool:
    """Is command unwrapping on? ``FERAL_SHELL_UNWRAP``, default on.

    The escape hatch exists because unwrapping adds refusals, and an
    operator who hits a false positive on a legitimate build script needs
    a way to keep working while it is fixed rather than a reason to turn
    the whole shell path off. Set ``FERAL_SHELL_UNWRAP=0`` (or ``false``
    / ``off`` / ``no``) to disable. Any other value, including unset,
    leaves it on.
    """
    raw = (os.environ.get("FERAL_SHELL_UNWRAP", "") or "").strip().lower()
    return raw not in ("0", "false", "off", "no")


def current_autonomy_mode() -> str:
    """Operator autonomy mode, same source ``ToolRunner`` reads.

    ``agents/tool_runner.py`` resolves ``FERAL_AUTONOMY`` and falls back to
    ``hybrid``; we mirror that here instead of importing ToolRunner so the
    security layer does not depend on the agent layer.
    """
    raw = (os.environ.get("FERAL_AUTONOMY", "") or "").strip().lower()
    return raw if raw in ("strict", "hybrid", "loose") else "hybrid"


def resolve_cwd(explicit: Optional[str] = None) -> str:
    """Resolve the working directory a shell request should run in.

    Precedence: the caller's explicit ``cwd`` argument, then ``FERAL_CWD``
    (the process-level pin the old hardcoded ``cwd=os.environ.get(...)``
    used), then ``FERAL_WORKSPACE`` (the same root ``coding_tools``
    relativizes output against), then the brain's own cwd.
    """
    for candidate in (explicit, os.environ.get("FERAL_CWD"), os.environ.get("FERAL_WORKSPACE")):
        if candidate and str(candidate).strip():
            return str(Path(str(candidate)).expanduser())
    return str(Path.cwd())


def command_argument_paths(command: str, cwd: str) -> list[str]:
    """Path-shaped arguments in ``command``, resolved against ``cwd``.

    This is what lets a single filesystem policy cover the whole toolset:
    ``read_file`` refuses ``~/.ssh/id_rsa``, so ``cat ~/.ssh/id_rsa`` must
    refuse too. We only reason about arguments that *look* like paths:
    a token containing ``/`` or starting with ``~``. Option strings
    (``-l``, ``--glob=*.py``) are skipped because treating them as paths
    produces false denials, and ``argv[0]`` of the first command is
    skipped because it resolves through ``PATH``, not through the
    workspace.

    This is deliberately not a shell interpreter: a command that *computes*
    a path at runtime (``cat $(cat pointer.txt)``) is not caught here. The
    load-bearing boundary is the cwd containment check in
    :func:`resolve_execution_mode`; this scan closes the common, direct
    case where the model simply names a file it was denied elsewhere.
    """
    try:
        tokens = shlex.split(command, comments=False, posix=True)
    except ValueError:
        # Unbalanced quotes. The callers reject those separately with a
        # syntax error. Fall back to whitespace split so we still scan.
        tokens = command.split()
    if not tokens:
        return []

    base = Path(cwd)
    found: list[str] = []
    for token in tokens[1:]:
        if not token or token.startswith("-"):
            continue
        if not (token.startswith("~") or "/" in token):
            continue
        expanded = Path(token).expanduser()
        resolved = expanded if expanded.is_absolute() else (base / expanded)
        found.append(str(resolved))
    return found


def resolve_execution_mode(
    command: str,
    *,
    policy: Optional[SandboxPolicy] = None,
    cwd: Optional[str] = None,
    skill_id: str = "",
    requires_sandbox: bool = False,
    prefer_sandbox: bool = False,
    autonomy_mode: Optional[str] = None,
    docker_available: bool = False,
) -> ExecutionDecision:
    """Decide where ``command`` runs. See the module docstring's table.

    ``requires_sandbox`` carries the manifest's declaration through;
    ``prefer_sandbox`` is the operator's ``FERAL_SANDBOX_BASH`` opt-in,
    which upgrades a developer shell to Docker when Docker is healthy but
    never turns a refusal into an execution.
    """
    policy = policy or SandboxPolicy.load_default()
    autonomy = (autonomy_mode or current_autonomy_mode()).strip().lower()

    if not command or not command.strip():
        return ExecutionDecision(
            mode=MODE_REFUSED,
            reason="empty command",
            needs=NEEDS_POLICY,
        )

    # 1. Generated / untrusted code. Docker or nothing: a workspace grant
    #    does not authorise running code the model wrote.
    is_generated = requires_sandbox or skill_id in GENERATED_CODE_SKILL_IDS
    if is_generated:
        if docker_available:
            return ExecutionDecision(
                mode=MODE_DOCKER,
                reason=(
                    f"'{skill_id or 'endpoint'}' runs generated code; "
                    f"executing in the Docker sandbox"
                ),
            )
        if not policy.full_authority():
            return ExecutionDecision(
                mode=MODE_REFUSED,
                needs=NEEDS_DOCKER,
                reason=(
                    f"'{skill_id or 'endpoint'}' runs generated code and "
                    f"requires the '{MODE_DOCKER}' execution mode, but the "
                    f"Docker sandbox is not available. A workspace grant does "
                    f"not substitute: grants authorise the operator's own "
                    f"shell, not generated programs."
                ),
            )
        # execution.full_authority. Docker is still preferred above when it
        # is there; this is the operator saying that its absence is not a
        # reason to refuse. Falls through to the host path below.
        #
        # This is the sharpest thing the key does. A refusal here is not a
        # prompt the operator can answer, it is a hard no, so an operator
        # on a machine without Docker could not run generated code at any
        # autonomy tier by any means. That is a real capability, and
        # somebody who set this key asked for it.

    # 2. Operator-level kill switch, shared with ``daemon://local/shell``.
    if not policy.can_execute_shell():
        return ExecutionDecision(
            mode=MODE_REFUSED,
            needs=NEEDS_POLICY,
            reason=(
                "shell execution is disabled by policy "
                "(execution.allow_shell_commands=false)"
            ),
        )

    # 3. Operator opt-in to run even the developer shell under Docker.
    if prefer_sandbox and docker_available:
        return ExecutionDecision(
            mode=MODE_DOCKER,
            reason="FERAL_SANDBOX_BASH is set and the Docker sandbox is healthy",
        )

    # 4. Developer shell: the cwd must land inside something the operator
    #    scoped. This is the boundary SECURITY.md draws.
    resolved_cwd = resolve_cwd(cwd)
    if not Path(resolved_cwd).is_dir():
        return ExecutionDecision(
            mode=MODE_REFUSED,
            needs=NEEDS_WORKSPACE_GRANT,
            cwd=resolved_cwd,
            denied_path=resolved_cwd,
            reason=f"working directory does not exist: {resolved_cwd}",
        )

    workspace, source = policy.resolve_workspace(resolved_cwd)
    if workspace is None and source != "blocked" and policy.full_authority():
        # The operator declared the whole machine their workspace. Report
        # the cwd as the workspace so downstream callers, which use this
        # field to decide where to run, are not handed None.
        #
        # ``blocked_paths`` is excluded deliberately: it is the operator's
        # own list, not part of the floor, and someone who wrote both a
        # block list and this key meant both. It is also the one scope
        # that stays meaningful under the key, so silently overriding it
        # would leave them with no way to carve anything out at all.
        workspace, source = resolved_cwd, "full_authority"
    if workspace is None:
        detail = (
            "it is on the policy's blocked_paths list"
            if source == "blocked"
            else "it is outside every granted workspace"
        )
        return ExecutionDecision(
            mode=MODE_REFUSED,
            needs=NEEDS_WORKSPACE_GRANT,
            cwd=resolved_cwd,
            denied_path=resolved_cwd,
            reason=(
                f"refusing to run a shell in {resolved_cwd}: {detail}. "
                f"Grant the folder to run here."
            ),
        )

    # ``full_authority`` counts as the explicit act strict is asking for.
    # Strict demands the operator have named the folder rather than
    # inheriting it from a policy read path; someone who wrote
    # ``full_authority`` into the policy file has named all of them, and
    # more deliberately than a grant.
    if (
        autonomy == _GRANT_ONLY_AUTONOMY
        and source != "grant"
        and not policy.full_authority()
    ):
        return ExecutionDecision(
            mode=MODE_REFUSED,
            needs=NEEDS_WORKSPACE_GRANT,
            cwd=resolved_cwd,
            workspace=workspace,
            workspace_source=source,
            denied_path=resolved_cwd,
            reason=(
                f"autonomy mode 'strict' requires an explicit operator grant "
                f"for a host shell; {resolved_cwd} is only covered by the "
                f"policy's {source.replace('policy_', '').replace('_', ' ')}"
            ),
        )

    # 5. Pre-normalisation. Every check below reads command *text*, and
    #    text is the attacker's choice of encoding:
    #    ``echo cm0gLXJmIC8K | base64 -d | sh`` names no path and matches
    #    no deny pattern while running ``rm -rf /``. Unwrap once here and
    #    run the same checks over both forms; see
    #    ``security.command_unwrap``.
    scannable = scannable_command(command) if unwrap_enabled() else ""
    forms = [command] + ([scannable] if scannable and scannable != command else [])

    #    Deliberate obfuscation is refused outright rather than scanned.
    #    A base64 or hex blob decoded straight into a shell has no
    #    legitimate use from a tool call: an operator who wants that
    #    writes the command, and a model that produces it is either
    #    working around the policy or repeating something that was
    #    injected into it. Other findings (a plain ``$(...)``, a
    #    heredoc) are ordinary and only feed the scan.
    if scannable:
        for finding in obfuscation_findings(command):
            if finding.kind != KIND_ENCODED_PAYLOAD_TO_SHELL:
                continue
            logger.warning("refusing obfuscated shell command: %s", finding.detail)
            return ExecutionDecision(
                mode=MODE_REFUSED,
                needs=NEEDS_POLICY,
                cwd=resolved_cwd,
                workspace=workspace,
                workspace_source=source,
                reason=(
                    f"refusing an obfuscated command: {finding.detail}. "
                    f"It decodes to: {finding.revealed!r}. Run the decoded "
                    f"command directly if that is what you meant."
                ),
            )

    #    Operator deny rules plus the reviewed floor, matched against the
    #    unwrapped text so an encoding does not walk past them.
    for pattern in policy.denied_command_patterns():
        for form in forms:
            match = pattern.search(form)
            if not match:
                continue
            return ExecutionDecision(
                mode=MODE_REFUSED,
                needs=NEEDS_POLICY,
                cwd=resolved_cwd,
                workspace=workspace,
                workspace_source=source,
                reason=(
                    f"command matches a denied pattern "
                    f"({match.group(0)[:60]!r}); refusing"
                ),
            )

    # 6. One filesystem policy for the whole toolset: a path the file tools
    #    refuse must not be reachable by naming it on the command line.
    #    Run over the unwrapped form too, so ``cat $(echo ~/.ssh/id_rsa)``
    #    and its ANSI-C spelling are caught alongside the direct one.
    #    Left running under ``full_authority``. It asks ``can_read_path``,
    #    which is the same method the file tools ask, and that method
    #    answers True under the key for everything except
    #    ``blocked_paths``. So the two sides still agree, and an operator
    #    who carved something out with ``blocked_paths`` keeps it carved
    #    out from the shell as well. Skipping the loop here instead would
    #    have made ``cat`` reach what ``read_file`` refuses, which is the
    #    exact inconsistency this check was written to prevent.
    for form in forms:
        for candidate in command_argument_paths(form, resolved_cwd):
            if not policy.can_read_path(candidate):
                return ExecutionDecision(
                    mode=MODE_REFUSED,
                    needs=NEEDS_WORKSPACE_GRANT,
                    cwd=resolved_cwd,
                    workspace=workspace,
                    workspace_source=source,
                    denied_path=candidate,
                    reason=(
                        f"command references {candidate}, which the filesystem "
                        f"policy denies to the file tools as well. Grant the "
                        f"folder or drop the argument."
                    ),
                )

    return ExecutionDecision(
        mode=MODE_HOST_WORKSPACE,
        cwd=resolved_cwd,
        workspace=workspace,
        workspace_source=source,
        reason=f"cwd is inside {workspace} ({source})",
    )


__all__ = [
    "ExecutionDecision",
    "GENERATED_CODE_SKILL_IDS",
    "MODE_DOCKER",
    "MODE_HOST_WORKSPACE",
    "MODE_REFUSED",
    "NEEDS_DOCKER",
    "NEEDS_POLICY",
    "NEEDS_WORKSPACE_GRANT",
    "command_argument_paths",
    "current_autonomy_mode",
    "resolve_cwd",
    "resolve_execution_mode",
    "unwrap_enabled",
]
