"""Clear the dead temporary folders out of ``workspace_grants.json``.

``workspace_grants.json`` is the list of everywhere the brain may read
and write. Nothing ever removed a row from it, and nothing needed to
until the test suite started writing into a real ``~/.feral``.

Measured on a live install: 876 grants, 174,897 bytes. 870 of them were
pytest sandboxes of the form
``/private/var/folders/bn/.../T/pytest-of-<user>/pytest-184/test_a_running_background_job_0/work``,
4 were other directories under the system temp root, and exactly 2 were
real folders the operator had granted, ``~/Desktop`` and ``~``. The
"Folders FERAL can use" page rendered 2,665 lines. A security surface
that only grows, mostly out of paths that no longer exist, is not a
surface anyone can review, which is the whole point of having it.

The rule lives in ``security.sandbox_policy`` so that this one-time
cleanup and the ongoing prune in ``SandboxPolicy._save_grants`` cannot
drift apart. It is narrow on purpose: a grant is removed only when it is
BOTH missing from disk AND under a directory the operating system hands
out and reclaims (the system temp root, or a ``pytest-of-*`` sandbox). A
grant that is merely missing right now is kept, because an external
drive that is unplugged and a network share that is not mounted look
exactly like a deleted folder from here, and quietly dropping a grant the
operator made is worse than keeping a dead row.

Not RECURRING. ``_save_grants`` prunes on every grant and revoke, so the
file cannot grow back to this state once this has run; a standing sweep
would be a second stat() pass at every boot for nothing.

Re-running is harmless: an already-pruned file has nothing left to drop
and is not rewritten.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("feral.migrations")


def _grants_path() -> Path:
    from config.loader import feral_home
    from security.sandbox_policy import _GRANTS_FILE
    return feral_home() / _GRANTS_FILE


def migrate():
    from security.sandbox_policy import prune_grants_file

    path = _grants_path()
    if not path.exists():
        return ("no workspace grants file to prune", False)

    dropped, remaining = prune_grants_file(path)
    if not dropped:
        return (f"workspace grants already clean ({remaining} kept)", False)

    logger.info(
        "pruned %d dead temporary workspace grant(s) from %s, %d remain",
        len(dropped), path.name, remaining,
    )
    return (
        f"removed {len(dropped)} workspace grants for temporary folders that "
        f"no longer exist, keeping {remaining}",
        True,
    )
