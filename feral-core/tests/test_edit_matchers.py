"""Contract for the fallback edit-matching chain (skills/edit_matchers.py).

Every test here pins a behaviour that the pre-existing single
``content.replace(old_text, new_text, 1)`` got wrong. The module is pure
stdlib, so none of this needs a brain, a registry or an event loop.
"""

from __future__ import annotations

import pytest

from skills.edit_matchers import (
    DEFAULT_MAX_CONTENT_LINES,
    dominant_eol,
    find_edit_match,
    splice,
)


def apply(content: str, old: str, new: str, **kw) -> str:
    result = find_edit_match(content, old, **kw)
    assert result.ok, result.message
    return splice(content, result.candidates, new)


# ── strategy chain ────────────────────────────────────────────────────


def test_exact_match_wins_and_is_reported():
    content = "def f():\n    return 1\n"
    result = find_edit_match(content, "    return 1")
    assert result.ok
    assert result.strategy == "exact"
    assert result.candidates[0].start_line == 2


def test_fallback_recovers_from_trailing_whitespace():
    """The file has trailing whitespace the model did not reproduce.

    Exact match fails outright here; before the fallback chain this was a
    404 and the model retried with the same text it had already sent.
    """
    content = "def f():   \n    return 1\n"
    result = find_edit_match(content, "def f():\n    return 1")
    assert result.ok
    assert result.strategy != "exact"
    assert apply(content, "def f():\n    return 1", "def f():\n    return 2") == (
        "def f():\n    return 2\n"
    )


def test_whitespace_normalized_recovers_from_internal_spacing():
    content = "a  =   1 + 2\n"
    result = find_edit_match(content, "a = 1 + 2")
    assert result.ok
    assert result.strategy == "whitespace_normalized"


def test_indentation_flexible_runs_before_line_trimmed():
    """Order is load-bearing, not cosmetic.

    ``indentation_flexible`` accepts a strict subset of what
    ``line_trimmed`` accepts (preserving relative indentation is stronger
    than trimming both ends of every line). Under first-strategy-wins,
    running ``line_trimmed`` first would make ``indentation_flexible``
    unreachable: it could never propose a span the earlier strategy had
    not already proposed. Strictest-first is the only order in which it
    can ever be consulted.
    """
    content = "        if x:\n            y = 1\n            z = 2\n"
    shifted = "if x:\n    y = 1\n    z = 2"
    result = find_edit_match(content, shifted)
    assert result.ok
    assert result.strategy == "indentation_flexible"


def test_line_trimmed_handles_what_indentation_flexible_will_not():
    """Interior indentation mangled: relative structure is gone, so
    ``indentation_flexible`` declines and the looser strategy answers."""
    content = "        if x:\n            y = 1\n            z = 2\n"
    mangled = "if x:\n            y = 1\n  z = 2"
    result = find_edit_match(content, mangled)
    assert result.ok
    assert result.strategy == "line_trimmed"


def test_escape_normalized_recovers_an_over_escaped_needle():
    content = 'print("hi")\n'
    result = find_edit_match(content, 'print(\\"hi\\")')
    assert result.ok
    assert result.strategy == "escape_normalized"


def test_block_anchor_runs_last_and_flags_review():
    content = "def h():\n    a = 1\n    b = 2\n    return a\n"
    result = find_edit_match(content, "def h():\n    NOT WHAT IS THERE\n    return a")
    assert result.ok
    assert result.strategy == "block_anchor"
    assert result.requires_review is True


def test_block_anchor_requires_three_lines():
    content = "start\nmiddle\nend\n"
    assert not find_edit_match(content, "start\nend").ok


def test_block_anchor_respects_interior_slack():
    content = "start\n" + "".join(f"line{i}\n" for i in range(20)) + "end\n"
    # Needle claims a 1-line interior; the file has 20. Out of slack.
    assert not find_edit_match(content, "start\nx\nend").ok


# ── invariant 1: first strategy decides ───────────────────────────────


def test_a_looser_strategy_never_overrules_a_stricter_one():
    """Exact matches once; line_trimmed would match twice.

    Cross-strategy "best scoring" would surface the ambiguity and refuse.
    First-strategy-wins gives the exact match, which is the right answer.
    """
    content = "value = 1\nvalue = 1   \n"
    result = find_edit_match(content, "value = 1\n")
    assert result.ok
    assert result.strategy == "exact"
    assert len(result.candidates) == 1


# ── invariant 2: ambiguity is fatal, never a fall-through ─────────────


def test_multiple_matches_are_a_hard_failure():
    content = "x = 1\nx = 1\n"
    result = find_edit_match(content, "x = 1")
    assert not result.ok
    assert result.error_code == "ambiguous"
    assert result.strategy == "exact"


def test_ambiguity_does_not_fall_through_to_a_looser_strategy():
    """The whole point. Relaxing constraints must not raise confidence.

    ``line_trimmed`` would match these two lines just as well, so a
    fall-through design would keep going and eventually pick something.
    The deciding strategy owns the verdict, and the verdict is 'refuse'.
    """
    content = "  x = 1\n\tx = 1\n"
    result = find_edit_match(content, "x = 1")
    assert not result.ok
    assert result.error_code == "ambiguous"
    assert result.strategy == "exact"


def test_replace_all_opts_into_multiple():
    content = "x = 1\nx = 1\n"
    result = find_edit_match(content, "x = 1", replace_all=True)
    assert result.ok
    assert len(result.candidates) == 2
    assert splice(content, result.candidates, "x = 2") == "x = 2\nx = 2\n"


def test_expected_replacements_is_a_checksum():
    content = "x = 1\nx = 1\n"
    ok = find_edit_match(content, "x = 1", replace_all=True, expected_replacements=2)
    assert ok.ok
    bad = find_edit_match(content, "x = 1", replace_all=True, expected_replacements=3)
    assert not bad.ok
    assert bad.error_code == "unexpected_replacement_count"


# ── splicing ──────────────────────────────────────────────────────────


def test_splice_is_by_offset_not_str_replace():
    """A non-exact span is not byte-identical to old_text.

    ``content.replace(old_text, new_text, 1)`` finds nothing here, so the
    pre-fix code would have written the file back unchanged while
    reporting a successful replacement.
    """
    content = "def f():\t\n    return 1\n"
    old = "def f():"
    result = find_edit_match(content, "def f():\n    return 1")
    assert result.ok and result.strategy != "exact"
    out = splice(content, result.candidates, "def g():\n    return 1")
    assert "def g():" in out
    assert content.replace(old, "def g():", 1) != out


def test_crlf_file_stays_crlf():
    """Silent-corruption guard: an LF replacement in a CRLF file would
    produce mixed endings, after which every later exact match fails for
    reasons nothing in the tool output explains."""
    content = "a = 1\r\nb = 2\r\n"
    out = apply(content, "a = 1", "a = 9\nc = 3")
    assert "\r\n" in out
    assert "\n" not in out.replace("\r\n", "")
    assert dominant_eol(content) == "\r\n"


def test_crlf_needle_matches_an_lf_file():
    content = "a = 1\nb = 2\n"
    out = apply(content, "a = 1\r\n", "a = 9\r\n")
    assert out == "a = 9\nb = 2\n"


def test_replacement_is_reindented_to_the_matched_block():
    """The model wrote the needle flush-left. Replacing whole lines with
    flush-left text would de-indent the file."""
    content = "class A:\n    def m(self):\n        value = 1\n"
    out = apply(content, "value = 1", "value = 2")
    assert out == "class A:\n    def m(self):\n        value = 2\n"


# ── anti-clobber ──────────────────────────────────────────────────────


def test_oversized_span_is_refused():
    content = "start\n" + "a" * 200 + "\n" + "b" * 200 + "\nend\n"
    result = find_edit_match(content, "start\nx\nend")
    assert not result.ok
    assert result.error_code == "oversized_span"


def test_indentation_alone_does_not_trip_the_clobber_guard():
    body = "\n".join(f"            line_{i:02d} = {i}" for i in range(20))
    content = f"class A:\n    def m(self):\n        if x:\n{body}\n"
    needle = "\n".join(f"line_{i:02d} = {i}" for i in range(20))
    result = find_edit_match(content, needle)
    assert result.ok, result.message


# ── recovery ──────────────────────────────────────────────────────────


def test_not_found_returns_the_closest_real_file_text():
    content = "def f():\n    return 1\n\ndef g():\n    return 2\n"
    result = find_edit_match(content, "    return 42")
    assert not result.ok
    assert result.error_code == "not_found"
    assert result.closest is not None
    # Real text from the file, not a paraphrase.
    assert result.closest.text in content
    assert result.closest.start_line >= 1


# ── cost guard ────────────────────────────────────────────────────────


def test_fuzzy_strategies_are_skipped_above_the_size_limit():
    content = "".join(f"line {i}   \n" for i in range(50))
    needle = "line 10"          # exact-matchable
    assert find_edit_match(content, needle, max_content_lines=10).ok

    fuzzy_only = "line 10"      # needs trimming to match "line 10   "
    result = find_edit_match(content, fuzzy_only + "\nline 11", max_content_lines=10)
    assert not result.ok
    assert result.fuzzy_skipped is True


def test_default_size_limits_are_sane():
    assert DEFAULT_MAX_CONTENT_LINES >= 1000


@pytest.mark.parametrize("needle", ["", "   ", "\t\n \n"])
def test_whitespace_only_needles_do_not_match_via_a_fallback(needle):
    """A needle that normalises to nothing would match every blank run in
    the file, so no fallback strategy proposes for one.

    A byte-exact whitespace span is still a legal edit (collapsing two
    blank lines is a real thing to ask for); that path is protected by
    exact matching plus the uniqueness check instead.
    """
    result = find_edit_match("a\n\nb\n", needle)
    assert not result.ok or result.strategy == "exact"
