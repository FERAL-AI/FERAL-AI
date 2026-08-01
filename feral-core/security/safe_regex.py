"""ReDoS-safe regex compilation for patterns FERAL did not write.

Why this module exists
======================
Python's ``re`` is a backtracking engine with no match timeout. A pattern
like ``(a+)+$`` run against 30 ``a``s and one ``b`` takes exponential
time, and there is no way to interrupt it: ``signal.alarm`` does not fire
inside the C matcher on a worker thread, and ``re`` exposes no step
budget. So the only place to stop a catastrophic pattern is *before*
compiling it.

That matters for exactly one class of pattern: the ones that arrive from
outside the codebase. A sandbox-policy file an operator edited, a skill
manifest, a ``grep_search`` argument the model produced. FERAL's own
hardcoded module-level patterns are reviewed source and must NOT be
routed through here: this compiler deliberately refuses constructs that
are perfectly reasonable in hand-written code (lookarounds are the
obvious one), and forcing them through it would either break them or
push authors to weaken the checker.

What is refused
===============
1. **Backreferences** (``\\1``, ``\\g<name>``, ``(?P=name)``). A
   backreference makes the language non-regular, which defeats any
   static complexity argument and is the classic ReDoS amplifier.
2. **Lookarounds** (``(?=``, ``(?!``, ``(?<=``, ``(?<!``). Same reason,
   plus a lookahead containing a quantifier re-scans its subject at
   every position.
3. **Nested quantifiers** (``(a+)+``, ``(a*)*``, ``(\\d+)*``) and
   **quantified alternations** (``(a|ab)+``). These are the two shapes
   that produce exponential backtracking; every classic ReDoS payload
   is one of them.
4. **Stacked quantifiers** (``a++``, ``a*?*``). Python's possessive
   quantifiers (``a++``) are actually a ReDoS *cure*, not a cause, but
   telling them apart from a typo'd double quantifier needs a real
   parser. A config-supplied pattern that wants one can be rewritten as
   an atomic group; refusing is the safe default and is documented here
   so the refusal message is not mysterious.
5. Patterns longer than :data:`MAX_PATTERN_CHARS`, or empty.

What is NOT claimed
===================
This is a syntactic filter, not a proof of linear-time matching. A
pattern can still be slow without any of the shapes above (a long chain
of independent optional groups, ``(a?){20}b``-style bounded blowup where
the quantifier walker sees only one level). Callers that run untrusted
patterns over untrusted *input* should additionally bound the subject
length, which is why :func:`compile_safe_regex` accepts ``max_chars``
and why callers in this package truncate before matching.

The design is a Python reimplementation of the group-nesting walk in
``src/util/safe-regex.ts`` from yc-software/qm (MIT). No code was
copied; the refusal set and the walk's shape are the borrowed part.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = [
    "MAX_PATTERN_CHARS",
    "UnsafePatternError",
    "compile_safe_regex",
    "is_pattern_safe",
]

# Long enough for a realistic operator deny-rule, short enough that the
# walk below is free and a pathological pattern cannot be hidden in
# noise. qm uses the same ceiling.
MAX_PATTERN_CHARS: Final[int] = 256


class UnsafePatternError(ValueError):
    """Raised when a pattern is rejected before compilation.

    Subclasses ``ValueError`` so a caller that already handles
    ``re.error``/``ValueError`` around ``re.compile`` keeps working
    without a new except clause.
    """


# ``\1``-``\9`` and ``\g<name>`` are backreferences; ``(?P=name)`` is
# Python's named backreference. ``(?=`` / ``(?!`` are lookaheads and
# ``(?<=`` / ``(?<!`` are lookbehinds. ``(?P<name>`` is a *named group*,
# not a lookbehind, so the lookbehind test requires the ``=``/``!``.
_BACKREFERENCE = re.compile(r"\\[1-9]|\\g<|\(\?P=")
_LOOKAROUND = re.compile(r"\(\?<[=!]|\(\?[=!]")

# ``{`` only starts a quantifier when it is a well-formed repeat count.
# ``a{b}`` is a literal brace in Python, and refusing it would reject
# harmless patterns for no safety gain.
_REPEAT_COUNT = re.compile(r"\{\d*(?:,\d*)?\}")


class _Group:
    __slots__ = ("quantified", "alternation")

    def __init__(self) -> None:
        self.quantified = False
        self.alternation = False


def _reject(pattern: str, reason: str) -> UnsafePatternError:
    shown = pattern if len(pattern) <= 80 else pattern[:77] + "..."
    return UnsafePatternError(f"{reason}: {shown!r}")


def _walk(pattern: str) -> None:
    """Refuse nested/ambiguous repetition. Raises :class:`UnsafePatternError`.

    Tracks one flag pair per open group. A quantifier is unsafe when the
    thing it repeats can itself match the same text more than one way:
    either the previous token was already a quantifier, or the group that
    just closed was internally quantified or contained a top-level
    alternation.
    """
    groups: list[_Group] = []
    escaped = False
    in_class = False
    previous_was_quantifier = False
    just_closed: _Group | None = None

    i = 0
    length = len(pattern)
    while i < length:
        ch = pattern[i]

        if escaped:
            escaped = False
            previous_was_quantifier = False
            just_closed = None
            i += 1
            continue

        if ch == "\\":
            escaped = True
            i += 1
            continue

        if in_class:
            if ch == "]":
                in_class = False
            i += 1
            continue

        if ch == "[":
            in_class = True
            previous_was_quantifier = False
            just_closed = None
            i += 1
            continue

        if ch == "(":
            groups.append(_Group())
            previous_was_quantifier = False
            just_closed = None
            i += 1
            # ``(?:``, ``(?i)``, ``(?P<n>``: consume the ``?`` so it is
            # not mistaken for an optional-quantifier on an empty group.
            if i < length and pattern[i] == "?":
                i += 1
            continue

        if ch == "|":
            if groups:
                groups[-1].alternation = True
            previous_was_quantifier = False
            just_closed = None
            i += 1
            continue

        if ch == ")":
            just_closed = groups.pop() if groups else _Group()
            previous_was_quantifier = False
            i += 1
            continue

        quantifier_width = 0
        if ch in "*+?":
            quantifier_width = 1
        elif ch == "{":
            match = _REPEAT_COUNT.match(pattern, i)
            if match:
                quantifier_width = match.end() - i

        if quantifier_width:
            if previous_was_quantifier:
                raise _reject(pattern, "stacked repetition is not supported")
            if just_closed is not None and (just_closed.quantified or just_closed.alternation):
                raise _reject(
                    pattern,
                    "nested repetition or a quantified alternation is not supported",
                )
            if groups:
                groups[-1].quantified = True
            previous_was_quantifier = True
            just_closed = None
            i += quantifier_width
            # ``+?``/``*?`` is a lazy quantifier, one token, not a stack.
            if i < length and pattern[i] == "?":
                i += 1
            continue

        previous_was_quantifier = False
        just_closed = None
        i += 1


def compile_safe_regex(
    pattern: str,
    flags: int = 0,
    *,
    max_chars: int = MAX_PATTERN_CHARS,
) -> re.Pattern[str]:
    """Compile ``pattern``, refusing shapes that can backtrack badly.

    Use this for every regex whose text came from a config file, a skill
    manifest, an API request, or a model-authored tool argument. Do NOT
    use it for hardcoded patterns in this repository.

    Raises :class:`UnsafePatternError` for a refused shape and
    ``re.error`` for a pattern that is merely invalid, so a caller can
    tell "you wrote it wrong" from "you may not write that here".
    """
    if not isinstance(pattern, str):
        raise UnsafePatternError(f"pattern must be a string, got {type(pattern).__name__}")
    if not pattern:
        raise UnsafePatternError("pattern must not be empty")
    if len(pattern) > max_chars:
        raise UnsafePatternError(
            f"pattern must be 1-{max_chars} characters, got {len(pattern)}"
        )
    if _BACKREFERENCE.search(pattern):
        raise _reject(pattern, "backreferences are not supported")
    if _LOOKAROUND.search(pattern):
        raise _reject(pattern, "lookarounds are not supported")

    _walk(pattern)
    return re.compile(pattern, flags)


def is_pattern_safe(pattern: str, flags: int = 0) -> bool:
    """``True`` when :func:`compile_safe_regex` would accept ``pattern``.

    Convenience for validation surfaces that want to report every bad
    rule in a config file rather than dying on the first one.
    """
    try:
        compile_safe_regex(pattern, flags)
    except (UnsafePatternError, re.error):
        return False
    return True
