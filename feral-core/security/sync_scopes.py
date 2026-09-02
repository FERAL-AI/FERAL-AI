"""Sync scope vocabulary: the one place a scope name is judged.

Replication used to be all-or-nothing. Every row of every table in
``SyncEngine._SYNC_ALLOWED_TABLES`` went to any peer that got past the
handshake, so two operators who wanted to pool robot events had to pool
their whole personal memory. A scope is the unit that makes partial
sharing expressible: an operation carries one, a peer is granted a set,
and an operation crosses the wire only if its scope is in that set.

Fail closed, and mean it
------------------------
There is exactly one reserved scope, :data:`PRIVATE`, and it is the
answer to every question this module cannot answer confidently:

* an operation written before this module existed (the ``sync_wal``
  column defaults to it, so every legacy row is private forever);
* an operation whose ``scope`` field is missing, empty, ``None``, of the
  wrong type, or arrives from a peer running an older build;
* a scope name that does not match :data:`SCOPE_PATTERN`;
* a scope name longer than :data:`MAX_SCOPE_LENGTH`.

:data:`PRIVATE` is never grantable and never replicates. That ordering
is deliberate: a bug anywhere in the scope pipeline degrades to "shared
too little", never "shared too much". Sharing too little is a support
ticket; sharing too much cannot be undone, because a peer brain is
owned by somebody else and data on their disk is beyond recall.

Why the grammar is narrow
-------------------------
Scope names travel over the wire from a brain this one does not own,
are matched against grants, and are printed in operator-facing CLI
output. A permissive grammar would let a peer ship control characters,
whitespace that makes two different names render identically, or
case variants that compare unequal but read the same. The pattern
below admits lowercase ASCII, digits and three separators, and
normalisation lowercases and strips before matching so that an
operator who types ``Robot-Events`` grants the same thing the writer
named ``robot-events``. Anything else is not "close enough": it is
:data:`PRIVATE`.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

#: The reserved scope. Never replicates, never grantable, and the value
#: every ambiguous input resolves to.
PRIVATE = "private"

#: Longest scope name accepted. Long enough for a descriptive name,
#: short enough that a peer cannot use the field as a data channel.
MAX_SCOPE_LENGTH = 64

#: Lowercase ASCII, digits, and ``-``, ``_``, ``.`` as separators. Must
#: start and end with a letter or digit so a name cannot be padded into
#: something that renders as another name.
SCOPE_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")


class InvalidScopeError(ValueError):
    """Raised by :func:`require_shareable_scope` on a name that could
    never replicate. Used by the grant path, where an operator typing a
    bad name deserves an error instead of a grant that silently does
    nothing."""


def normalise_scope(value: Any) -> str:
    """Coerce anything at all into a scope name.

    Returns :data:`PRIVATE` for every input this module cannot vouch
    for. It never raises: the callers are the WAL read path and the
    wire decode path, and an exception there would let one malformed
    operation abort a whole batch. The safe answer is always available,
    so this returns it rather than failing.
    """
    if not isinstance(value, str):
        return PRIVATE
    candidate = value.strip().lower()
    if not candidate or len(candidate) > MAX_SCOPE_LENGTH:
        return PRIVATE
    if candidate == PRIVATE:
        return PRIVATE
    if not SCOPE_PATTERN.match(candidate):
        return PRIVATE
    return candidate


def is_shareable(value: Any) -> bool:
    """True only for a scope that could ever cross the wire.

    Being shareable is necessary, not sufficient: the operation still
    has to land in a scope the specific peer was granted.
    """
    return normalise_scope(value) != PRIVATE


def require_shareable_scope(value: Any) -> str:
    """Normalise, or raise :class:`InvalidScopeError`.

    The grant and write APIs use this so a typo surfaces immediately.
    ``private`` is refused by name: granting it would read as "share my
    private memory with this peer", which is exactly the sentence this
    module exists to make unrepresentable.
    """
    if isinstance(value, str) and value.strip().lower() == PRIVATE:
        raise InvalidScopeError(
            f"'{PRIVATE}' is the reserved never-replicate scope. It cannot be "
            "granted and cannot be written to. Name a scope of your own."
        )
    normalised = normalise_scope(value)
    if normalised == PRIVATE:
        raise InvalidScopeError(
            f"invalid scope name {value!r}. Scope names are 1-{MAX_SCOPE_LENGTH} "
            "characters of lowercase ASCII, digits, '-', '_' or '.', starting "
            "and ending with a letter or digit."
        )
    return normalised


def normalise_scope_set(values: Iterable[Any] | None) -> frozenset[str]:
    """Normalise a collection of scope names into a grant set.

    :data:`PRIVATE` is dropped rather than carried through, so a grant
    set can never authorise the never-replicate scope no matter what
    was stored or what a caller passes. ``None`` yields the empty set,
    which is the correct default for a peer nobody has granted
    anything.

    A bare string is treated as ONE scope, not as an iterable of
    characters. Python would happily iterate ``"robot-events"`` into
    twelve single-character names, every one of which passes the
    grammar, and a caller who wrote ``allowed_scopes="robot-events"``
    would silently get a grant set that authorises a scope literally
    named ``r``. That is a widening bug, which is the direction this
    module exists to make impossible.
    """
    if not values:
        return frozenset()
    if isinstance(values, (str, bytes)):
        values = [values]
    out = set()
    for value in values:
        candidate = normalise_scope(value)
        if candidate != PRIVATE:
            out.add(candidate)
    return frozenset(out)


#: The grant set for a peer with no grants, and the value every
#: enforcement point defaults to when it cannot resolve a peer.
DENY_ALL: frozenset[str] = frozenset()

#: Sentinel for a ``scope=`` argument meaning "take the scope of the
#: row's newest existing WAL operation". Delete emitters pass this so a
#: removal replicates exactly as far as the write it undoes, and no
#: further. It is not a scope name: it fails :data:`SCOPE_PATTERN` on
#: the underscores, so a value that reaches storage or the wire by
#: mistake normalises to :data:`PRIVATE` like any other junk.
#: :meth:`SyncEngine._resolve_scope` is the only code that interprets
#: it.
INHERIT = "__inherit__"
