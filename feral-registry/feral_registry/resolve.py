"""Turn an item *reference* into an item row.

A reference is what a human or a manifest actually writes down. Every
one of them is a name:

* ``feral install robot_ext``
* an ``AppManifest`` declaring ``skill_dependencies: ["robot_ext"]``
* the catalog listing, whose ``name`` column reads ``robot_ext``

``Item.id`` is a UUID the database minted at publish time. Nobody types
a UUID, and a manifest that pinned dependencies by UUID would be
unreadable and unmaintainable, so the name has to resolve. It legally
can: ``Item`` carries ``UniqueConstraint("kind", "name", "version")``,
which makes ``(kind, name, version)`` a natural key.

Resolution order
----------------
1. **By id.** An exact primary-key hit wins outright. A published item's
   id is a permalink, and a later item that happens to take the same
   string as its ``name`` must never shadow it.
2. **By name**, over exactly the rows the caller is allowed to see. The
   visibility filter is the caller's, not the resolver's: an anonymous
   caller resolves against ``approved`` + ``public`` only, so a name can
   never confirm the existence of a pending or rejected submission.

Ambiguity
---------
A name is unique per ``(kind, version)``, not globally, so a bare name
can match more than one row. Two different situations, two different
answers:

* **Several kinds.** There is no defensible rule for choosing between a
  ``skill`` called ``robot_ext`` and a ``daemon`` called ``robot_ext``,
  so this is a hard error (HTTP 409) that names the kinds it found.
  Callers that already know the kind should pass ``kind=``; the
  marketplace client does.
* **Several versions of one kind.** This is not ambiguous, it is
  ordinary: ``feral install robot_ext`` means "the current one", the
  same as every other package manager. It resolves to the **highest**
  version. The one exception is a version set this resolver cannot
  order (anything that is not dot-separated integers, e.g. ``1.0-beta``
  next to ``1.0.0``). Picking one of those would be an arbitrary pick,
  so that is a hard error too, naming the versions and pointing at
  ``version=``.

Nothing here is a guess: every branch either returns the one row the
rule selects or refuses and says what it saw.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import ITEM_STATUS_APPROVED, ITEM_VISIBILITY_PUBLIC, Item, Publisher

# A version this resolver can order: dot-separated integers, nothing
# else. Deliberately narrow. Guessing an order for "1.0.0-rc.2" against
# "1.0.0" is exactly the arbitrary pick this module refuses to make.
_ORDERABLE_VERSION = re.compile(r"^\d+(\.\d+)*$")


class ItemNotFound(LookupError):
    """No visible row matches the reference."""


@dataclass
class AmbiguousReference(Exception):
    """The reference matches several rows and no rule picks one.

    ``field`` is the dimension that could not be collapsed (``"kind"`` or
    ``"version"``) and ``candidates`` is the sorted set of values seen,
    so the caller can render a message that names real options instead of
    telling the user to guess.
    """

    ref: str
    field: str
    candidates: list[str]

    def __str__(self) -> str:
        joined = ", ".join(self.candidates)
        return (
            f"'{self.ref}' matches {len(self.candidates)} items by {self.field} "
            f"({joined}); pass {self.field}= to choose one"
        )


def version_sort_key(version: str) -> tuple[int, ...] | None:
    """Ordering key for a version string, or None if it is not orderable."""
    if not _ORDERABLE_VERSION.match(version or ""):
        return None
    return tuple(int(part) for part in version.split("."))


def _visible_only(stmt):
    """Restrict a select to what an unauthenticated caller may see."""
    return stmt.where(
        Item.status == ITEM_STATUS_APPROVED,
        Item.visibility == ITEM_VISIBILITY_PUBLIC,
    )


async def resolve_item(
    session: AsyncSession,
    ref: str,
    *,
    kind: str | None = None,
    version: str | None = None,
    include_hidden: bool = False,
) -> tuple[Item, Publisher, str]:
    """Resolve ``ref`` to ``(item, publisher, resolved_by)``.

    ``resolved_by`` is ``"id"`` or ``"name"``. ``include_hidden`` is the
    reviewer's view: it drops the approved+public filter, and it must
    only ever be set from an authenticated reviewer dependency.

    Raises :class:`ItemNotFound` or :class:`AmbiguousReference`.
    """
    base = select(Item, Publisher).join(Publisher, Item.author_id == Publisher.id)
    if not include_hidden:
        base = _visible_only(base)
    if kind:
        base = base.where(Item.kind == kind)
    if version:
        base = base.where(Item.version == version)

    by_id = (await session.execute(base.where(Item.id == ref))).first()
    if by_id is not None:
        return by_id[0], by_id[1], "id"

    rows = (await session.execute(base.where(Item.name == ref))).all()
    if not rows:
        raise ItemNotFound(ref)

    kinds = sorted({row[0].kind for row in rows})
    if len(kinds) > 1:
        raise AmbiguousReference(ref=ref, field="kind", candidates=kinds)

    if len(rows) == 1:
        return rows[0][0], rows[0][1], "name"

    keyed: list[tuple[tuple[int, ...], Item, Publisher]] = []
    for item, publisher in rows:
        key = version_sort_key(item.version)
        if key is None:
            raise AmbiguousReference(
                ref=ref,
                field="version",
                candidates=sorted({row[0].version for row in rows}),
            )
        keyed.append((key, item, publisher))

    # Highest version wins. `created_at` breaks a tie between two rows
    # whose versions compare equal ("1.0" vs "1.0.0"), which the unique
    # constraint permits because it compares the strings.
    keyed.sort(key=lambda entry: (entry[0], entry[1].created_at))
    _, item, publisher = keyed[-1]
    return item, publisher, "name"
