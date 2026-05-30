"""Per-parent subagent allowlist policy.

Conservative, default-deny. The orchestrator may spawn the small set of
child kinds enumerated in ``_DEFAULT_ALLOWLIST`` unless the operator
overrides via ``~/.feral/subagent_policy.json``.

The policy is intentionally tiny — it answers exactly one question:
"Is this *parent_kind* allowed to spawn this *child_kind*?"

Scope, cancellation, model overrides, and audit logging live in
``subagent_spawner``; this module is pure data + a couple of helpers
so that tests and routes can reason about the rule set without
touching asyncio.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Iterable

logger = logging.getLogger("feral.subagent_policy")


_DEFAULT_ALLOWLIST: dict[str, frozenset[str]] = {
    "orchestrator": frozenset({"tool_runner", "research", "memory_query"}),
}


_lock = threading.Lock()
_runtime_allowlist: dict[str, set[str]] = {}
_loaded_from_disk = False


def _policy_path() -> Path:
    home = os.environ.get("FERAL_HOME") or str(Path.home() / ".feral")
    return Path(home) / "subagent_policy.json"


def _load_disk_policy_into(target: dict[str, set[str]]) -> None:
    """Best-effort merge of ``~/.feral/subagent_policy.json`` into *target*.

    Missing file → no-op. Malformed file → log + ignore (we keep the
    conservative defaults rather than collapse to a deny-all surprise).
    """
    path = _policy_path()
    try:
        if not path.exists():
            return
        data = json.loads(path.read_text())
    except Exception as exc:
        logger.warning("subagent_policy.json unreadable (%s); using defaults", exc)
        return

    if not isinstance(data, dict):
        logger.warning("subagent_policy.json malformed (not an object); ignoring")
        return

    for parent_kind, kinds in data.items():
        if not isinstance(parent_kind, str):
            continue
        if isinstance(kinds, list) and all(isinstance(k, str) for k in kinds):
            target[parent_kind] = set(kinds)
        else:
            logger.warning(
                "subagent_policy.json key=%s ignored (value must be list[str])",
                parent_kind,
            )


def _ensure_loaded() -> None:
    global _loaded_from_disk
    if _loaded_from_disk:
        return
    with _lock:
        if _loaded_from_disk:
            return
        for parent_kind, kinds in _DEFAULT_ALLOWLIST.items():
            _runtime_allowlist[parent_kind] = set(kinds)
        _load_disk_policy_into(_runtime_allowlist)
        _loaded_from_disk = True


def is_allowed(parent_kind: str, child_kind: str) -> bool:
    """Return True iff *parent_kind* may spawn a subsession of *child_kind*.

    Defaults to DENY for any (parent_kind, child_kind) pair not in the
    runtime allowlist.
    """
    _ensure_loaded()
    with _lock:
        allowed = _runtime_allowlist.get(parent_kind)
        if not allowed:
            return False
        return child_kind in allowed


def register_allowed(parent_kind: str, child_kinds: Iterable[str]) -> None:
    """Add *child_kinds* to the allowlist for *parent_kind*.

    Idempotent. Used by the boot sequence (or tests) to extend policy
    without writing to disk.
    """
    _ensure_loaded()
    with _lock:
        bucket = _runtime_allowlist.setdefault(parent_kind, set())
        for k in child_kinds:
            if isinstance(k, str) and k:
                bucket.add(k)


def clear() -> None:
    """Reset runtime state — test helper.

    Drops the in-memory allowlist *and* the cached "loaded from disk"
    flag so the next ``is_allowed`` call re-reads the conservative
    defaults (plus any disk override).
    """
    global _loaded_from_disk
    with _lock:
        _runtime_allowlist.clear()
        _loaded_from_disk = False


def snapshot() -> dict[str, set[str]]:
    """Return a deep copy of the current allowlist (debug / inspection)."""
    _ensure_loaded()
    with _lock:
        return {k: set(v) for k, v in _runtime_allowlist.items()}
