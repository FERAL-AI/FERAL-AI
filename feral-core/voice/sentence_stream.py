"""Cut a streaming LLM response into speakable chunks.

The chained pipeline used to wait for the whole LLM answer, then hand
it to TTS in one piece. That serialises two of the three slowest
stages in the turn: nothing is synthesised until the last token
lands, and nothing is heard until synthesis finishes.

Splitting the stream on sentence boundaries overlaps them. The first
sentence is usually complete a few hundred milliseconds into
generation, so synthesis of sentence 1 runs while the model is still
writing sentence 2, and the speaker starts almost as soon as the model
has said something worth saying.

The rules are deliberately boring, because a wrong split is audible:

* Only ``.``, ``!``, ``?`` and newline end a chunk. Commas do not.
  Cutting on a comma produces the wrong prosody at the seam.
* A period that is part of an abbreviation, a decimal, an ellipsis or
  an initial does not end a chunk.
* A chunk shorter than ``min_chars`` keeps growing even past a
  boundary. Very short fragments cost a full TTS round trip each and
  most engines pay a fixed per-request overhead, so "Yes." on its own
  is slower to hear than "Yes. Right away." would have been.
* Once the buffer passes ``max_chars`` with no boundary in sight, cut
  at the last space anyway. A model that emits an unpunctuated
  paragraph must not defeat streaming entirely.
"""

from __future__ import annotations

import re

# Terminators that may end a spoken chunk.
_TERMINATORS = ".!?\n"

# Tokens whose trailing period is never a sentence end.
_ABBREVIATIONS = frozenset(
    {
        "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "mt",
        "vs", "etc", "eg", "ie", "approx", "dept", "est", "fig",
        "inc", "ltd", "no", "vol", "p", "pp", "al", "ca", "cf",
        "a.m", "p.m", "am", "pm", "u.s", "u.k", "e.g", "i.e",
    }
)

_WORD_BEFORE_DOT = re.compile(r"([A-Za-z][A-Za-z.]*)\.$")

DEFAULT_MIN_CHARS = 24
DEFAULT_MAX_CHARS = 240


def _is_hard_boundary(buffer: str, index: int) -> bool:
    """Is ``buffer[index]`` a real end of sentence?"""
    char = buffer[index]
    if char == "\n":
        return True
    if char not in ".!?":
        return False

    following = buffer[index + 1: index + 2]
    if char == ".":
        # Decimal point: 3.5, v1.2.
        prev = buffer[index - 1] if index else ""
        if prev.isdigit() and following.isdigit():
            return False
        # Ellipsis: treat the run as one boundary, at its end.
        if following == ".":
            return False
        head = buffer[: index + 1]
        match = _WORD_BEFORE_DOT.search(head)
        if match:
            token = match.group(1).lower().rstrip(".")
            if token in _ABBREVIATIONS:
                return False
            # A single letter before a period is an initial (J. Smith),
            # not a sentence end.
            if len(token) == 1 and token.isalpha():
                return False
    # A terminator must be followed by whitespace or end of buffer.
    # Mid-token punctuation ("feral.io", "3.5") is not a boundary.
    if following and not following.isspace() and following not in "\"')]}":
        return False
    return True


class SentenceAccumulator:
    """Feed it deltas, take speakable chunks out.

    Stateful and single-threaded, one per turn. ``push`` returns the
    chunks that became complete because of that delta, which is
    usually zero and occasionally one.
    """

    def __init__(
        self,
        *,
        min_chars: int = DEFAULT_MIN_CHARS,
        max_chars: int = DEFAULT_MAX_CHARS,
    ):
        self.min_chars = int(min_chars)
        self.max_chars = int(max_chars)
        self._buffer = ""
        self._emitted: list[str] = []

    @property
    def buffer(self) -> str:
        return self._buffer

    @property
    def emitted(self) -> list[str]:
        """Every chunk handed out so far, in order."""
        return list(self._emitted)

    def push(self, delta: str) -> list[str]:
        if not delta:
            return []
        self._buffer += delta
        return self._drain()

    def _drain(self) -> list[str]:
        out: list[str] = []
        while True:
            chunk = self._take_one()
            if chunk is None:
                break
            out.append(chunk)
            self._emitted.append(chunk)
        return out

    def _take_one(self) -> str | None:
        buffer = self._buffer
        for index, char in enumerate(buffer):
            if char not in _TERMINATORS:
                continue
            if not _is_hard_boundary(buffer, index):
                continue
            candidate = buffer[: index + 1].strip()
            if len(candidate) < self.min_chars:
                # Too short to be worth its own synthesis request.
                # Keep accumulating; the next boundary carries both.
                continue
            self._buffer = buffer[index + 1:]
            return candidate

        # No usable boundary. Cut on length so an unpunctuated wall of
        # text still streams.
        if len(buffer) >= self.max_chars:
            cut = buffer.rfind(" ", 0, self.max_chars)
            if cut <= 0:
                cut = self.max_chars
            candidate = buffer[:cut].strip()
            if candidate:
                self._buffer = buffer[cut:]
                return candidate
        return None

    def flush(self) -> str | None:
        """Return whatever is left, once the stream is closed."""
        remainder = self._buffer.strip()
        self._buffer = ""
        if not remainder:
            return None
        self._emitted.append(remainder)
        return remainder

    def full_text(self) -> str:
        """Everything that has passed through, chunks plus remainder."""
        parts = [*self._emitted]
        tail = self._buffer.strip()
        if tail:
            parts.append(tail)
        return " ".join(p for p in parts if p)


def split_sentences(
    text: str,
    *,
    min_chars: int = DEFAULT_MIN_CHARS,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> list[str]:
    """One-shot split, for the non-streaming fallback path."""
    acc = SentenceAccumulator(min_chars=min_chars, max_chars=max_chars)
    out = acc.push(text)
    tail = acc.flush()
    if tail:
        out.append(tail)
    return out
