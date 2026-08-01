"""Fallback matching for ``coding_tools__edit_file``.

The problem
-----------
The pre-existing ``_edit_file`` did exactly one thing: a byte-exact
``content.replace(old_text, new_text, 1)`` guarded by a 0-or-many
uniqueness count. Frontier models can reproduce a file byte for byte, so
that is enough for them. Local Qwen-class models cannot: they normalise
indentation, drop trailing whitespace, re-wrap, and over-escape. Exact
match fails constantly, the model retries with a slightly different
guess, and the turn burns out in retry loops.

The design
----------
A chain of *strategies*. Each strategy is a pure function
``(content, old_text) -> list[MatchCandidate]`` that proposes spans which
literally exist in ``content``. Strategies only propose; the arbiter
(:func:`find_edit_match`) decides.

Two invariants carry the whole design:

1. **First strategy that produces any candidate decides the outcome.**
   There is no cross-strategy scoring. A looser strategy never gets to
   overrule a stricter one that already spoke. This is why
   :data:`STRATEGY_ORDER` runs strictest-first, and why that ordering is
   load-bearing rather than cosmetic.

2. **More than one candidate is always a hard failure.** Ambiguity is
   never resolved by falling through to a looser strategy. Falling
   through would mean the more we relax the constraints the more
   confident we get, which is backwards. Ambiguity returns 409 and the
   model is told to add context.

``block_anchor`` runs last and is gated (three lines minimum, interior
line count within a slack of the needle's) because it is the only
strategy that can replace text the model never saw: it matches on the
first and last line and takes whatever is between them on trust. Its
candidates carry ``requires_review=True``.

Splicing
--------
Strategies from ``line_trimmed`` onward match spans that are *not*
byte-identical to ``old_text``, so ``content.replace(old_text, ...)`` is
wrong: it would either not find the span or replace a different one.
:func:`splice` cuts by offset instead.

:func:`splice` also normalises the replacement to the file's dominant
line ending. Without that, editing a CRLF file with LF-only content
produces a mixed-ending file, and every later exact match against it
fails for reasons nothing in the tool output explains.

This module imports nothing from FERAL, only the standard library, so it
can be unit-tested without booting a brain.
"""

from __future__ import annotations

import dataclasses
import difflib
import re
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

__all__ = [
    "MatchCandidate",
    "MatchResult",
    "ClosestText",
    "find_edit_match",
    "splice",
    "dominant_eol",
    "STRATEGY_ORDER",
]

# ── tuning ────────────────────────────────────────────────────────────
# Sliding-window strategies are O(file_lines x needle_lines). Above these
# sizes only `exact` runs. The caller passes operator overrides in.
DEFAULT_MAX_CONTENT_LINES = 4_000
DEFAULT_MAX_NEEDLE_LINES = 400

# Anti-clobber. A matched span may legitimately be longer than `old_text`
# (recovered indentation, CRLF, whitespace the model dropped) but not
# carry dramatically more *content*. Measured on whitespace-stripped
# length so a block the model wrote flush-left is not penalised for the
# indentation the file puts back. Mostly a `block_anchor` guard: that is
# the strategy that can swallow an unbounded interior.
SPAN_RATIO_LIMIT = 1.5
SPAN_SLACK_CHARS = 24

# `block_anchor` gating.
BLOCK_ANCHOR_MIN_LINES = 3
BLOCK_ANCHOR_INTERIOR_SLACK = 2

# Bounds on the recovery snippet returned when nothing matched.
CLOSEST_MAX_LINES = 40
CLOSEST_MAX_CHARS = 2_000
CLOSEST_MIN_RATIO = 0.5

_WS_RUN = re.compile(r"\s+")


# ── data ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MatchCandidate:
    """A span in ``content`` that a strategy claims corresponds to
    ``old_text``. ``start``/``end`` are character offsets; the line
    numbers are 1-based and inclusive."""

    start: int
    end: int
    start_line: int
    end_line: int
    strategy: str
    requires_review: bool = False
    #: Leading whitespace on the matched span's first line, and on the
    #: needle's first line. :func:`splice` uses the delta to re-indent the
    #: replacement so a needle the model wrote without indentation does
    #: not de-indent the file.
    matched_indent: str = ""
    needle_indent: str = ""


@dataclass(frozen=True)
class ClosestText:
    """The nearest real text in the file, for a not-found response."""

    text: str
    start_line: int
    end_line: int
    similarity: float


@dataclass(frozen=True)
class MatchResult:
    ok: bool
    strategy: str = ""
    candidates: tuple[MatchCandidate, ...] = ()
    error_code: str = ""
    message: str = ""
    closest: Optional[ClosestText] = None
    requires_review: bool = False
    #: True when the size guard suppressed every non-exact strategy.
    fuzzy_skipped: bool = False


# ── line index helpers ────────────────────────────────────────────────


@dataclass
class _Index:
    content: str
    lines: list[str] = field(default_factory=list)     # with line endings
    starts: list[int] = field(default_factory=list)    # char offset of each line
    bodies: list[str] = field(default_factory=list)    # line ending stripped


def _index(content: str) -> _Index:
    lines = content.splitlines(keepends=True)
    starts: list[int] = []
    bodies: list[str] = []
    pos = 0
    for line in lines:
        starts.append(pos)
        pos += len(line)
        bodies.append(_strip_eol(line))
    return _Index(content=content, lines=lines, starts=starts, bodies=bodies)


def _strip_eol(line: str) -> str:
    if line.endswith("\r\n"):
        return line[:-2]
    if line.endswith("\n") or line.endswith("\r"):
        return line[:-1]
    return line


def dominant_eol(content: str) -> str:
    """The line ending the file mostly uses. Ties and empty files give LF."""
    crlf = content.count("\r\n")
    lf = content.count("\n") - crlf
    cr = content.count("\r") - crlf
    if crlf >= lf and crlf >= cr and crlf > 0:
        return "\r\n"
    if cr > lf and cr > 0:
        return "\r"
    return "\n"


def _to_eol(text: str, eol: str) -> str:
    """Rewrite every line ending in ``text`` to ``eol``."""
    if not text:
        return text
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if eol == "\n":
        return normalized
    return normalized.replace("\n", eol)


def _significant_len(text: str) -> int:
    """Length ignoring whitespace. Used by the anti-clobber guard so a
    block that only gained indentation is not mistaken for a block that
    gained content."""
    return len(_WS_RUN.sub("", text))


def _leading_ws(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _needle_lines(old_text: str) -> list[str]:
    return [_strip_eol(line) for line in old_text.splitlines()]


def _span_for_window(
    idx: _Index, i: int, count: int, *, include_trailing_eol: bool,
) -> tuple[int, int]:
    """Char span covering content lines ``[i, i+count)``."""
    start = idx.starts[i]
    last = i + count - 1
    end = idx.starts[last] + len(idx.lines[last])
    if not include_trailing_eol:
        end -= len(idx.lines[last]) - len(idx.bodies[last])
    return start, end


def _window_candidate(
    idx: _Index,
    i: int,
    count: int,
    old_text: str,
    strategy: str,
    needle_first: str,
    *,
    requires_review: bool = False,
) -> MatchCandidate:
    start, end = _span_for_window(
        idx, i, count, include_trailing_eol=old_text.endswith(("\n", "\r")),
    )
    return MatchCandidate(
        start=start,
        end=end,
        start_line=i + 1,
        end_line=i + count,
        strategy=strategy,
        requires_review=requires_review,
        matched_indent=_leading_ws(idx.bodies[i]),
        needle_indent=_leading_ws(needle_first),
    )


def _sliding(
    content: str,
    old_text: str,
    strategy: str,
    normalize: Callable[[str], str],
) -> list[MatchCandidate]:
    """Shared body for the line-wise strategies: compare a sliding window
    of content lines against the needle's lines under ``normalize``."""
    needle = _needle_lines(old_text)
    if not needle:
        return []
    norm_needle = [normalize(line) for line in needle]
    # A needle that normalises to nothing at all (pure whitespace) would
    # match every blank run in the file. Refuse to propose.
    if not any(norm_needle):
        return []
    idx = _index(content)
    n = len(needle)
    if n > len(idx.bodies):
        return []
    norm_body = [normalize(line) for line in idx.bodies]
    out: list[MatchCandidate] = []
    for i in range(len(norm_body) - n + 1):
        if norm_body[i:i + n] == norm_needle:
            out.append(
                _window_candidate(idx, i, n, old_text, strategy, needle[0]),
            )
    return out


# ── strategies ────────────────────────────────────────────────────────


def match_exact(content: str, old_text: str) -> list[MatchCandidate]:
    """Byte-exact occurrences, after normalising the needle's line endings
    to the file's dominant ending.

    The normalisation is not a relaxation: a needle copied out of a CRLF
    file through a JSON round-trip routinely arrives LF-only, and treating
    that as "not found" sends the model into a retry loop over an edit
    that is character-for-character correct.
    """
    if not old_text:
        return []
    needle = _to_eol(old_text, dominant_eol(content))
    idx = _index(content)
    out: list[MatchCandidate] = []
    pos = content.find(needle)
    while pos != -1:
        start_line = _line_of(idx, pos)
        end_line = _line_of(idx, max(pos, pos + len(needle) - 1))
        out.append(
            MatchCandidate(
                start=pos,
                end=pos + len(needle),
                start_line=start_line + 1,
                end_line=end_line + 1,
                strategy="exact",
                matched_indent="",
                needle_indent="",
            )
        )
        pos = content.find(needle, pos + max(1, len(needle)))
    return out


def _line_of(idx: _Index, offset: int) -> int:
    lo, hi = 0, len(idx.starts) - 1
    if hi < 0:
        return 0
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if idx.starts[mid] <= offset:
            lo = mid
        else:
            hi = mid - 1
    return lo


def match_line_trimmed(content: str, old_text: str) -> list[MatchCandidate]:
    """Whole-line match ignoring leading and trailing whitespace.

    Catches the single most common local-model failure: the block is
    right, the indentation or the trailing whitespace is not.
    """
    return _sliding(content, old_text, "line_trimmed", str.strip)


def match_whitespace_normalized(content: str, old_text: str) -> list[MatchCandidate]:
    """Whole-line match with every internal whitespace run collapsed.

    Catches tabs-versus-spaces and re-aligned columns inside a line.
    """
    return _sliding(
        content, old_text, "whitespace_normalized",
        lambda line: _WS_RUN.sub(" ", line).strip(),
    )


def match_indentation_flexible(content: str, old_text: str) -> list[MatchCandidate]:
    """Match after removing the common leading indentation from both
    sides, so a block the model shifted uniformly still matches while a
    block whose *relative* indentation was mangled does not.

    Strictly narrower than ``line_trimmed`` on interior structure and
    strictly wider on the shared prefix, which is why it is worth having
    as its own step rather than folding into the one above.
    """
    needle = _needle_lines(old_text)
    if not needle:
        return []
    needle_dedent = _dedent(needle)
    if not any(line.strip() for line in needle_dedent):
        return []
    idx = _index(content)
    n = len(needle)
    if n > len(idx.bodies):
        return []
    out: list[MatchCandidate] = []
    for i in range(len(idx.bodies) - n + 1):
        window = idx.bodies[i:i + n]
        if _dedent(window) == needle_dedent:
            out.append(
                _window_candidate(
                    idx, i, n, old_text, "indentation_flexible", needle[0],
                ),
            )
    return out


def _dedent(lines: Sequence[str]) -> list[str]:
    indents = [
        len(_leading_ws(line)) for line in lines if line.strip()
    ]
    cut = min(indents) if indents else 0
    return [line[cut:].rstrip() if line.strip() else "" for line in lines]


_ESCAPES = (
    ("\\r\\n", "\n"),
    ("\\n", "\n"),
    ("\\r", "\n"),
    ("\\t", "\t"),
    ('\\"', '"'),
    ("\\'", "'"),
)


def _unescape(text: str) -> str:
    out = text
    for src, dst in _ESCAPES:
        out = out.replace(src, dst)
    return out.replace("\\\\", "\\")


def match_escape_normalized(content: str, old_text: str) -> list[MatchCandidate]:
    """Decode a needle the model over-escaped (a literal two-character
    ``\\n`` where the file has a newline, ``\\"`` where the file has a
    quote) and retry exact, then line-trimmed, against the decoded form.

    Only runs when decoding actually changes the needle, so it can never
    duplicate the work of the strategies above.
    """
    decoded = _unescape(old_text)
    if decoded == old_text:
        return []
    out = [
        dataclasses.replace(c, strategy="escape_normalized")
        for c in match_exact(content, decoded)
    ]
    if out:
        return out
    return _sliding(content, decoded, "escape_normalized", str.strip)


def match_block_anchor(content: str, old_text: str) -> list[MatchCandidate]:
    """Match on the first and last line only, taking the interior on
    trust.

    This is the only strategy that can replace text the model never saw,
    so it is gated three ways: the needle must be at least
    ``BLOCK_ANCHOR_MIN_LINES`` lines, both anchors must be non-blank, and
    the matched interior must be within ``BLOCK_ANCHOR_INTERIOR_SLACK``
    lines of the needle's interior. Every candidate it yields is marked
    ``requires_review``.
    """
    needle = _needle_lines(old_text)
    if len(needle) < BLOCK_ANCHOR_MIN_LINES:
        return []
    first, last = needle[0].strip(), needle[-1].strip()
    if not first or not last:
        return []
    interior = len(needle) - 2
    idx = _index(content)
    bodies = [line.strip() for line in idx.bodies]
    out: list[MatchCandidate] = []
    for i, line in enumerate(bodies):
        if line != first:
            continue
        lo = i + 1 + max(0, interior - BLOCK_ANCHOR_INTERIOR_SLACK)
        hi = min(len(bodies) - 1, i + 1 + interior + BLOCK_ANCHOR_INTERIOR_SLACK)
        for j in range(lo, hi + 1):
            if bodies[j] != last:
                continue
            out.append(
                _window_candidate(
                    idx, i, j - i + 1, old_text, "block_anchor", needle[0],
                    requires_review=True,
                ),
            )
            break  # nearest closing anchor only
    return out


#: Strictest first. The order is load-bearing, because the first
#: strategy to produce a candidate owns the outcome.
#:
#: The match sets nest: exact ⊂ indentation_flexible ⊂ line_trimmed ⊂
#: whitespace_normalized. Anything ``indentation_flexible`` accepts,
#: ``line_trimmed`` also accepts (trimming both ends of every line is
#: weaker than preserving relative indentation), so running
#: ``line_trimmed`` first would make ``indentation_flexible`` unreachable:
#: it could never propose a span the previous strategy had not already
#: proposed, and under first-strategy-wins it would never be consulted.
#: Ordering strictest-to-loosest is also the only order consistent with
#: the invariant that relaxing constraints must not increase confidence.
#:
#: ``escape_normalized`` is orthogonal (it transforms the needle rather
#: than the comparison) and only fires when decoding changes the needle,
#: so it sits after the comparison-based strategies. ``block_anchor`` is
#: last because it is the only one that can replace unseen text.
STRATEGY_ORDER: tuple[tuple[str, Callable[[str, str], list[MatchCandidate]]], ...] = (
    ("exact", match_exact),
    ("indentation_flexible", match_indentation_flexible),
    ("line_trimmed", match_line_trimmed),
    ("whitespace_normalized", match_whitespace_normalized),
    ("escape_normalized", match_escape_normalized),
    ("block_anchor", match_block_anchor),
)


# ── arbiter ───────────────────────────────────────────────────────────


def find_edit_match(
    content: str,
    old_text: str,
    *,
    replace_all: bool = False,
    expected_replacements: Optional[int] = None,
    max_content_lines: int = DEFAULT_MAX_CONTENT_LINES,
    max_needle_lines: int = DEFAULT_MAX_NEEDLE_LINES,
) -> MatchResult:
    """Run the strategy chain and decide.

    ``replace_all`` opts into replacing every candidate the deciding
    strategy found. ``expected_replacements`` is a checksum: when given,
    a count that does not match is a hard failure even under
    ``replace_all``.

    There is deliberately no ``fuzzy`` toggle. A model that can opt into
    looser matching always will, on the very call where it should have
    read the file again instead.
    """
    if not old_text:
        return MatchResult(
            ok=False, error_code="empty_needle",
            message="old_text is required.",
        )

    content_lines = content.count("\n") + 1
    needle_lines = old_text.count("\n") + 1
    fuzzy_ok = (
        content_lines <= max_content_lines and needle_lines <= max_needle_lines
    )

    for name, strategy in STRATEGY_ORDER:
        if name != "exact" and not fuzzy_ok:
            break
        try:
            candidates = strategy(content, old_text)
        except Exception:  # pragma: no cover - a strategy must never be fatal
            continue
        if not candidates:
            continue

        # Invariant 1: this strategy owns the outcome from here. No
        # later strategy runs, whatever the verdict below turns out to be.
        return _arbitrate(
            content, old_text, name, candidates,
            replace_all=replace_all,
            expected_replacements=expected_replacements,
        )

    return MatchResult(
        ok=False,
        error_code="not_found",
        message=(
            "old_text was not found in the file, byte-exactly or under any "
            "fallback match."
        ),
        closest=closest_text(content, old_text),
        fuzzy_skipped=not fuzzy_ok,
    )


def _arbitrate(
    content: str,
    old_text: str,
    strategy: str,
    candidates: list[MatchCandidate],
    *,
    replace_all: bool,
    expected_replacements: Optional[int],
) -> MatchResult:
    count = len(candidates)

    # Invariant 2: ambiguity is fatal. It is never resolved by trying a
    # looser strategy, because relaxing constraints must not increase
    # confidence.
    if count > 1 and not replace_all:
        lines = ", ".join(str(c.start_line) for c in candidates[:10])
        return MatchResult(
            ok=False,
            strategy=strategy,
            candidates=tuple(candidates),
            error_code="ambiguous",
            message=(
                f"old_text matches {count} locations (strategy={strategy}; "
                f"lines {lines}). Add surrounding context to make it unique, "
                f"or pass replace_all=true if every occurrence should change."
            ),
        )

    if expected_replacements is not None and count != expected_replacements:
        return MatchResult(
            ok=False,
            strategy=strategy,
            candidates=tuple(candidates),
            error_code="unexpected_replacement_count",
            message=(
                f"Expected {expected_replacements} replacement(s) but found "
                f"{count} (strategy={strategy})."
            ),
        )

    needle_weight = _significant_len(old_text)
    limit = int(needle_weight * SPAN_RATIO_LIMIT) + SPAN_SLACK_CHARS
    for cand in candidates:
        span_weight = _significant_len(content[cand.start:cand.end])
        if span_weight > limit:
            return MatchResult(
                ok=False,
                strategy=strategy,
                candidates=tuple(candidates),
                error_code="oversized_span",
                message=(
                    f"Refusing to edit: the {strategy} match at lines "
                    f"{cand.start_line}-{cand.end_line} carries {span_weight} "
                    f"characters of content for a {needle_weight}-character "
                    f"old_text. That much unseen text would be clobbered. "
                    f"Re-read the file and pass the exact block."
                ),
            )

    return MatchResult(
        ok=True,
        strategy=strategy,
        candidates=tuple(candidates),
        requires_review=any(c.requires_review for c in candidates),
    )


# ── splice ────────────────────────────────────────────────────────────


def splice(content: str, candidates: Sequence[MatchCandidate], new_text: str) -> str:
    """Replace every candidate span with ``new_text``, by offset.

    ``content.replace(old_text, ...)`` cannot be used here: from
    ``line_trimmed`` onward the matched span is not byte-identical to
    ``old_text``, so ``str.replace`` would find nothing or, worse, find a
    different occurrence.

    The replacement is rewritten to the file's dominant line ending, and
    re-indented by the difference between the matched span's indentation
    and the needle's, so a needle the model wrote flush-left does not
    silently de-indent the block it replaces.
    """
    eol = dominant_eol(content)
    out = content
    for cand in sorted(candidates, key=lambda c: c.start, reverse=True):
        text = _to_eol(new_text, eol)
        text = _reindent(text, cand.needle_indent, cand.matched_indent, eol)
        out = out[:cand.start] + text + out[cand.end:]
    return out


def _reindent(text: str, needle_indent: str, matched_indent: str, eol: str) -> str:
    if needle_indent == matched_indent or not text:
        return text
    lines = text.split(eol)
    if len(matched_indent) > len(needle_indent):
        extra = matched_indent[len(needle_indent):]
        return eol.join(
            (extra + line) if line.strip() else line for line in lines
        )
    drop = needle_indent[len(matched_indent):]
    out = []
    for line in lines:
        out.append(line[len(drop):] if line.startswith(drop) else line)
    return eol.join(out)


# ── recovery ──────────────────────────────────────────────────────────


def closest_text(content: str, old_text: str) -> Optional[ClosestText]:
    """The nearest real block of file text to ``old_text``.

    Returned on a not-found so the model can see what is actually in the
    file at the place it was aiming for, instead of guessing again from
    the same stale memory.
    """
    needle = [line for line in _needle_lines(old_text)]
    if not needle or not content:
        return None
    anchor = next((line.strip() for line in needle if line.strip()), "")
    if not anchor:
        return None

    idx = _index(content)
    n = len(needle)
    if not idx.bodies:
        return None

    # Score candidate anchor lines first so the expensive block compare
    # runs a handful of times, not once per line of the file.
    scored: list[tuple[float, int]] = []
    matcher = difflib.SequenceMatcher(a=anchor, autojunk=False)
    for i, body in enumerate(idx.bodies):
        stripped = body.strip()
        if not stripped:
            continue
        matcher.set_seq2(stripped)
        if matcher.real_quick_ratio() < CLOSEST_MIN_RATIO:
            continue
        scored.append((matcher.ratio(), i))
    if not scored:
        return None
    scored.sort(reverse=True)

    needle_block = "\n".join(line.strip() for line in needle)
    best: Optional[ClosestText] = None
    for _, i in scored[:5]:
        end = min(len(idx.bodies), i + n)
        window = idx.bodies[i:end]
        ratio = difflib.SequenceMatcher(
            None, needle_block, "\n".join(line.strip() for line in window),
            autojunk=False,
        ).ratio()
        if best is not None and ratio <= best.similarity:
            continue
        text = "".join(idx.lines[i:min(end, i + CLOSEST_MAX_LINES)])
        best = ClosestText(
            text=text[:CLOSEST_MAX_CHARS],
            start_line=i + 1,
            end_line=min(end, i + CLOSEST_MAX_LINES),
            similarity=round(ratio, 3),
        )
    return best
