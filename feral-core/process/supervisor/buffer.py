"""Bounded line buffer for supervised process output.

A background process can print forever. The supervisor's original
``RunHandle`` accumulated every line into a plain ``list[str]``, which is
correct for the short-lived runs it was written for and a memory leak for
anything that streams (``tail -f``, a dev server, a long build).

:class:`BoundedLineBuffer` keeps the LAST ``max_lines`` lines, truncates
individual lines to ``max_line_chars``, and counts what it dropped so a
reader is TOLD about the loss instead of silently receiving a hole. It
also supports incremental reads through an absolute cursor, which is what
lets a poller ask "what is new since I last looked".

``max_lines=0`` and ``max_line_chars=0`` mean unbounded, which is the
default so the existing (short-lived, fully-buffered) supervisor callers
keep byte-identical behaviour.
"""

from __future__ import annotations

from collections import deque
from typing import Deque, List, Tuple

__all__ = ["BoundedLineBuffer"]

TRUNCATION_MARK = "…[line truncated]"


class BoundedLineBuffer:
    """Ring buffer of decoded output lines with absolute-cursor reads.

    ``total_appended`` counts every line ever appended (not just the
    retained ones), so cursors stay meaningful across evictions.
    ``dropped_lines`` counts lines evicted by the ring, and
    ``truncated_lines`` counts lines shortened by ``max_line_chars``.
    """

    def __init__(self, max_lines: int = 0, max_line_chars: int = 0) -> None:
        if max_lines < 0 or max_line_chars < 0:
            raise ValueError("max_lines and max_line_chars must be >= 0")
        self._max_lines = max_lines
        self._max_line_chars = max_line_chars
        self._lines: Deque[str] = deque()
        self.total_appended = 0
        self.dropped_lines = 0
        self.truncated_lines = 0

    # ── writing ──────────────────────────────────────────────────────

    def append(self, line: str) -> None:
        if self._max_line_chars and len(line) > self._max_line_chars:
            line = line[: self._max_line_chars] + TRUNCATION_MARK
            self.truncated_lines += 1
        self._lines.append(line)
        self.total_appended += 1
        if self._max_lines and len(self._lines) > self._max_lines:
            self._lines.popleft()
            self.dropped_lines += 1

    # ── reading ──────────────────────────────────────────────────────

    def lines(self) -> List[str]:
        """Every line still retained, oldest first."""
        return list(self._lines)

    def text(self) -> str:
        return "\n".join(self._lines)

    @property
    def first_retained_index(self) -> int:
        """Absolute index of the oldest line still held."""
        return self.total_appended - len(self._lines)

    def read_since(self, cursor: int, max_lines: int = 0) -> Tuple[List[str], int, int]:
        """Return ``(lines, next_cursor, skipped)`` for everything after
        ``cursor``.

        ``skipped`` is how many lines the caller will never see because
        the ring evicted them before this read. ``max_lines`` (0 =
        unlimited) caps one read; ``next_cursor < total_appended`` then
        tells the caller more is already waiting.
        """
        if cursor < 0:
            cursor = 0
        start = max(cursor, self.first_retained_index)
        skipped = max(0, self.first_retained_index - cursor)
        offset = start - self.first_retained_index
        window = list(self._lines)[offset:]
        if max_lines and len(window) > max_lines:
            window = window[:max_lines]
        return window, start + len(window), skipped

    def __len__(self) -> int:
        return len(self._lines)
