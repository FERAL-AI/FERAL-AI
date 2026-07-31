"""Canonical FTS5 ``MATCH`` string builder.

Incident (v2026.7.x): every FTS5 call site in the memory subsystem
interpolated the user's raw utterance straight into ``... MATCH ?``.
FTS5 does not take free text. It takes a *query expression* with its
own grammar, and the characters that appear in ordinary English and
ordinary technical vocabulary are operators in that grammar::

    don't        -> fts5: syntax error near "'"
    what's       -> fts5: syntax error near "'"
    C++          -> fts5: syntax error near "+"
    AI/ML        -> fts5: syntax error near "/"
    (urgent) ping-> fts5: syntax error near "ping"

Every one of those raised ``sqlite3.OperationalError``. Because the
call sites wrapped the query in a bare ``except Exception: pass``, the
text half of hybrid search silently contributed *nothing* for that
entire class of queries, with no log line, no metric, no error. A user
asking "what's my wallet" got vector-only recall and nobody knew.

The fix is to quote. FTS5 treats a double-quoted run as a *string*,
not as syntax: the tokenizer chews it up and the operator characters
inside are inert. ``"don't"`` becomes the two-token phrase ``don t``,
which is exactly how the indexer stored ``don't`` in the first place,
so quoting preserves recall instead of degrading it.

This module is the single implementation. ``memory.store``,
``memory.notes_legacy`` and ``memory.context_builder`` all route
through it. The previous state of the world had three partial
sanitizers that disagreed (``context_builder._fts_query`` kept the
apostrophe in its character class, so contractions still raised).

It deliberately depends on nothing inside ``memory`` so any module can
import it without risking a cycle.
"""

from __future__ import annotations

# Words that carry no lexical signal but do carry FTS5 weight. Dropping
# them matters for ``OR`` mode: "where is my wallet" OR-ed verbatim
# matches every row containing "is", which is every row.
STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "do", "does", "did", "have", "has", "had", "my", "your", "his", "her",
    "our", "their", "its", "me", "you", "we", "they", "it", "this", "that",
    "these", "those", "and", "or", "but", "to", "of", "in", "on", "at", "for",
    "with", "by", "from", "as", "if", "then", "so", "than", "when", "where",
    "what", "which", "who", "whom", "how", "why", "i",
})


def _quote(term: str) -> str:
    """Wrap one term as an FTS5 string literal.

    Inside a double-quoted FTS5 string the only character with meaning
    is ``"`` itself, escaped by doubling. Everything else (quotes,
    slashes, plus signs, parens, hyphens) is handed to the tokenizer
    verbatim.
    """
    return '"' + term.replace('"', '""') + '"'


# Sanitisation happens exactly once, immediately before the SQL, and
# the *caller* declares which recall behaviour it wants. Do not build
# an expression in one layer and pass it to another that sanitises
# again: the second pass sees "wallet" OR "keys" as three ordinary
# terms and quotes the operator into a literal, turning a widening OR
# into a mandatory AND on the word "or".
STRICT = "strict"  # every term required (FTS5's implicit conjunction)
BROAD = "broad"    # any term matches, stopwords dropped


def fts5_match_query(text: str, *, mode: str = STRICT) -> str:
    """Build a syntactically valid FTS5 ``MATCH`` expression from free text.

    Parameters
    ----------
    text :
        The raw user utterance. May contain anything.
    mode :
        :data:`STRICT` (default) keeps FTS5's implicit conjunction, so a
        call site that previously passed the raw string gets the same
        recall it always had, minus the syntax errors. :data:`BROAD`
        ORs the terms and drops stopwords, which is what the LLM context
        builder needs: a strict AND returns zero rows for "where is my
        wallet" when the episode only says "wallet".

    Returns
    -------
    str
        A quoted expression, or ``""`` when the input contains no
        indexable term at all. Callers must treat ``""`` as "no text
        query" and skip the MATCH. Passing an empty string to FTS5 is
        itself a syntax error.
    """
    if mode not in (STRICT, BROAD):
        raise ValueError(f"unknown fts mode {mode!r}")
    terms: list[str] = []
    for raw in (text or "").split():
        # A term with no alphanumeric character (``+++``, ``--``, ``?``)
        # tokenizes to nothing. FTS5 rejects an empty phrase, so these
        # have to be dropped rather than quoted.
        if not any(ch.isalnum() for ch in raw):
            continue
        if mode == BROAD:
            # Compare on the bare word so "wallet?" and "(urgent)" are
            # not accidentally treated as non-stopwords by punctuation.
            bare = "".join(ch for ch in raw if ch.isalnum() or ch == "'").lower()
            if bare in STOPWORDS or len(bare) < 2:
                continue
        terms.append(_quote(raw))
    if not terms:
        # BROAD dropped everything (the utterance was all stopwords).
        # Retry without the filter rather than returning nothing, so
        # "what is it" still searches for something.
        if mode == BROAD:
            return fts5_match_query(text, mode=STRICT)
        return ""
    joiner = " OR " if mode == BROAD else " AND "
    return joiner.join(terms)
