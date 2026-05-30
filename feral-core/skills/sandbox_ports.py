"""-A14: narrow sandbox facade.

This module defines a tiny port (protocol + default adapter) so code paths
that previously reached into the global ``BrainState`` to fetch
``docker_sandbox`` / ``wasm_sandbox`` can depend on a focused interface
instead.

Design goals:
- One responsibility: hand back the sandbox engines, nothing else.
- Production-safe: default adapter lazy-imports ``api.state.state`` so import
  order/tests that don't construct a BrainState still work.
- Replaceable in tests by injecting a custom adapter (no ``monkeypatch`` of
  ``api.state``).

Behavior is intentionally identical to the previous inline lookups in
``skills.executor`` and ``skills.impl.workspace_scripts``: returning ``None``
when the sandbox isn't wired yet.
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Protocol, runtime_checkable


logger = logging.getLogger("feral.skills.sandbox_ports")


@runtime_checkable
class SandboxPort(Protocol):
    """Narrow facade for sandbox engines used by skills execution.

    Implementations must be safe to call repeatedly; callers may invoke
    these methods on every skill execution.
    """

    def get_docker_sandbox(self) -> Optional[Any]:
        """Return the Docker sandbox engine, or None when unavailable."""

    def get_wasm_sandbox(self) -> Optional[Any]:
        """Return the WASM sandbox engine, or None when unavailable."""


class BrainStateSandboxPort:
    """Default :class:`SandboxPort` adapter backed by ``api.state.state``.

    The state module is imported lazily on each call so this adapter works
    even if it is constructed before BrainState boot, and so tests that
    swap out ``api.state.state`` continue to observe the swap.
    """

    def get_docker_sandbox(self) -> Optional[Any]:
        try:
            import api.state as state_module
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("api.state import failed: %s", exc)
            return None
        state_obj = getattr(state_module, "state", None)
        if state_obj is None:
            return None
        return getattr(state_obj, "docker_sandbox", None)

    def get_wasm_sandbox(self) -> Optional[Any]:
        try:
            import api.state as state_module
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("api.state import failed: %s", exc)
            return None
        state_obj = getattr(state_module, "state", None)
        if state_obj is None:
            return None
        return getattr(state_obj, "wasm_sandbox", None)


_DEFAULT_PORT: SandboxPort = BrainStateSandboxPort()


def default_sandbox_port() -> SandboxPort:
    """Return the process-wide default sandbox port.

    Callers that want test isolation should inject their own
    :class:`SandboxPort` rather than mutating this default.
    """

    return _DEFAULT_PORT


__all__ = [
    "SandboxPort",
    "BrainStateSandboxPort",
    "default_sandbox_port",
]
