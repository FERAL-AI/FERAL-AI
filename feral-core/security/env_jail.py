"""Build a child-process environment from empty instead of inheriting one.

The problem
===========
``subprocess`` and ``asyncio.create_subprocess_*`` inherit ``os.environ``
by default. For a spawned coding agent that means the child sees the
operator's entire environment, and in particular::

    HOME=/Users/<operator>

from which it can read ``~/.claude``, ``~/.codex``, ``~/.aws``,
``~/.ssh``, ``~/.gitconfig`` and the operator's shell history. None of
that is the child's business, and a prompt-injected agent reading
``~/.claude/settings.json`` to discover which MCP servers and
credentials exist is a realistic first move.

``bridges/acp.py`` already accepts a full environment replacement
(``AcpAgentProcess.spawn(..., env=...)``, which replaces rather than
merges, and inherits everything when omitted); what was missing was a
sensible thing to pass. This module is that default. It is a helper, not
a policy: nothing here is applied automatically, and callers opt in.

What this DOES contain
======================
* A small allowlist (:data:`PASSTHROUGH`) of names a POSIX toolchain
  genuinely needs: ``PATH``, ``LANG``, ``TERM``, ``TZ``, ``SHELL`` and
  friends. Everything not on it is dropped.
* A fresh ``mkdtemp`` as ``HOME``, so ``~`` expansion inside the child
  lands in an empty directory it owns rather than the operator's. The
  same directory backs ``XDG_CONFIG_HOME``, ``XDG_CACHE_HOME`` and
  ``TMPDIR`` so tools that honour those do not fall back to the real
  ones.
* Deletion of the jail directory on exit from the context manager.

What this does NOT contain
==========================
**This is not credential isolation.** Read that again before relying on
it. The jail's purpose is to keep a child out of the operator's *config
and history directories*; it does nothing about secrets you deliberately
hand it.

Specifically:

* ``allow`` lets a caller pass any name straight through, including
  ``ANTHROPIC_API_KEY``. A spawned agent that needs to call a model
  needs a key, and this module will not pretend otherwise. qm's own
  passthrough list bakes provider API keys and proxy variables in, and
  its documentation is explicit that the jail therefore does not imply
  credential isolation; FERAL's list does not bake them in, but any
  caller that adds them lands in the same place.
* Proxy variables (``HTTP_PROXY`` and friends) are NOT passed through by
  default, because a proxy URL frequently carries embedded credentials
  and because a child that respects an operator's proxy is a child whose
  traffic an injected instruction can redirect. Callers that need them
  must name them in ``allow``.
* A fresh ``HOME`` does not stop a child from reading an absolute path.
  ``cat /Users/<operator>/.ssh/id_rsa`` still works if the process has
  filesystem permission. Only an OS-level sandbox (the Docker path in
  ``security.docker_sandbox``, or macOS ``sandbox-exec``) stops that.
  This module raises the cost of a *generic* config sweep; it is not a
  boundary.
* Nothing here restricts network, CPU, or filesystem writes.

Environment overrides
=====================
``FERAL_ENV_JAIL_ALLOW``: comma-separated extra names to pass through,
for an operator whose toolchain needs one this module did not predict.
Default: empty. Read by :func:`_operator_allowlist`.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Final, Iterable, Iterator, Mapping, Optional

logger = logging.getLogger("feral.security.env_jail")

__all__ = [
    "PASSTHROUGH",
    "EnvJail",
    "build_child_env",
    "env_jail",
]

# Names a POSIX toolchain needs to behave normally. Kept short on
# purpose: every addition is a channel from the operator's session into
# the child's.
#
# Not here, deliberately: HOME (replaced), USER/LOGNAME (identify the
# operator), SSH_AUTH_SOCK (agent forwarding is a credential),
# *_PROXY (see the module docstring), every *_API_KEY / *_TOKEN, and
# every CLAUDE_*/CODEX_*/npm_* variable that points at operator config.
PASSTHROUGH: Final[tuple[str, ...]] = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TZ",
    "SHELL",
    "COLORTERM",
    "TERM_PROGRAM",
    "SYSTEMROOT",  # Windows: Python's socket module fails without it
    "COMSPEC",
    "PATHEXT",
    "NUMBER_OF_PROCESSORS",
)

# Used when the operator's PATH is itself absent, so a jailed child can
# still find the standard tools.
_FALLBACK_PATH: Final[str] = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def _operator_allowlist() -> tuple[str, ...]:
    """Extra passthrough names from ``FERAL_ENV_JAIL_ALLOW``.

    Comma-separated, whitespace tolerant. Defaults to empty, so the jail
    is exactly :data:`PASSTHROUGH` unless an operator widens it.
    """
    raw = os.environ.get("FERAL_ENV_JAIL_ALLOW", "") or ""
    return tuple(name.strip() for name in raw.split(",") if name.strip())


@dataclass
class EnvJail:
    """A built child environment plus the temporary ``HOME`` behind it.

    ``env`` is ready to hand to ``subprocess`` or to
    ``AcpAgentProcess.spawn(env=...)``. ``home`` is the throwaway directory
    that ``env["HOME"]`` points at. Call :meth:`cleanup` when the child
    is gone, or use the :func:`env_jail` context manager, which does it.
    """

    env: dict[str, str]
    home: str
    dropped: tuple[str, ...] = field(default=())

    def cleanup(self) -> None:
        """Delete the throwaway ``HOME``. Safe to call more than once."""
        if not self.home:
            return
        shutil.rmtree(self.home, ignore_errors=True)
        self.home = ""


def build_child_env(
    *,
    allow: Iterable[str] = (),
    extra: Optional[Mapping[str, str]] = None,
    source: Optional[Mapping[str, str]] = None,
    home: Optional[str] = None,
) -> EnvJail:
    """Build a child environment from empty.

    ``allow`` names additional variables to copy from the parent, on top
    of :data:`PASSTHROUGH` and ``FERAL_ENV_JAIL_ALLOW``. Use it for the
    one credential a child genuinely needs, and read the module
    docstring about what that costs.

    ``extra`` sets values directly, without reading the parent. This is
    the right way to pass a scoped, short-lived token: it never has to
    exist in the operator's own environment.

    ``home`` reuses an existing directory instead of creating one; the
    caller then owns its lifetime and :meth:`EnvJail.cleanup` will still
    remove it, so pass a directory you are happy to lose.

    The returned jail records what it ``dropped`` so a caller can log the
    difference rather than discover it as a mysterious child failure.
    """
    parent = os.environ if source is None else source
    names = tuple(PASSTHROUGH) + _operator_allowlist() + tuple(allow)

    env: dict[str, str] = {}
    for name in names:
        value = parent.get(name)
        if value is not None:
            env[name] = value

    if not env.get("PATH"):
        env["PATH"] = _FALLBACK_PATH

    jail_home = home or tempfile.mkdtemp(prefix="feral-env-jail-")
    env["HOME"] = jail_home
    env["USERPROFILE"] = jail_home  # Windows equivalent of HOME
    env["TMPDIR"] = jail_home
    env["XDG_CONFIG_HOME"] = os.path.join(jail_home, ".config")
    env["XDG_CACHE_HOME"] = os.path.join(jail_home, ".cache")
    env["XDG_DATA_HOME"] = os.path.join(jail_home, ".local", "share")
    env["XDG_STATE_HOME"] = os.path.join(jail_home, ".local", "state")

    for key in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        with contextlib.suppress(OSError):
            os.makedirs(env[key], exist_ok=True)

    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})

    dropped = tuple(sorted(set(parent) - set(env)))
    logger.debug(
        "env jail: %d passed through, %d dropped, HOME=%s",
        len(env),
        len(dropped),
        jail_home,
    )
    return EnvJail(env=env, home=jail_home, dropped=dropped)


@contextlib.contextmanager
def env_jail(
    *,
    allow: Iterable[str] = (),
    extra: Optional[Mapping[str, str]] = None,
    source: Optional[Mapping[str, str]] = None,
) -> Iterator[EnvJail]:
    """:func:`build_child_env` with the throwaway ``HOME`` cleaned up.

    ::

        with env_jail(allow=("ANTHROPIC_API_KEY",)) as jail:
            proc = await AcpAgentProcess.spawn(argv, env=jail.env)
            ...

    The directory is removed on exit, so hold the context open for as
    long as the child runs.
    """
    jail = build_child_env(allow=allow, extra=extra, source=source)
    try:
        yield jail
    finally:
        jail.cleanup()
