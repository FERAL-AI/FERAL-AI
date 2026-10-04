"""Absence of data must never become a durable fact.

On 2026-09-12 the knowledge graph held 44 triples like "Theora glasses
does_not_provide activity data" and "Feral does_not_measure blood
pressure". Each was extracted from an assistant turn that was accurate
when it was said -- the phone was on an older build, or nothing had
streamed yet that hour. Stored as triples they outlive the condition
that made them true and then make the brain deny capabilities it has.
The blood-pressure ones were contradicted by working code the same day.

What a thing cannot do BY DESIGN is a different claim and must survive.
"""
import pytest

from memory.knowledge_graph import is_transient_absence


@pytest.mark.parametrize("subject,predicate,obj", [
    # The exact triples found in the operator's graph.
    ("Theora glasses", "does_not_provide", "activity data"),
    ("Theora glasses", "does_not_provide", "SpO2"),
    ("Theora glasses", "does_not_provide", "HRV data"),
    ("Feral", "does_not_measure", "blood pressure"),
    ("Feral", "does not have", "blood-pressure sensor"),
    ("assistant", "does not yet have", "HRV data"),
    ("assistant", "currently receives no", "activity data"),
    ("connected health glasses", "does not provide", "blood-pressure data"),
    ("this week's available vitals data", "did not include", "activity data"),
    ("user", "has no currently available data for", "current heart rate"),
])
def test_momentary_absence_is_dropped(subject, predicate, obj):
    assert is_transient_absence(subject, predicate, obj)


@pytest.mark.parametrize("subject,predicate,obj", [
    # Design guarantees. These ARE durable and are the federation answer.
    ("shared note", "does not grant access to", "filesystem"),
    ("shared note", "does not grant access to", "credentials"),
    ("shared note", "does not grant access to", "rest of memory"),
    # Ordinary positive facts.
    ("user", "has_device", "Theora glasses"),
    ("glasses", "provides", "heart rate data"),
    ("user", "prefers", "dark mode"),
    ("CuteBot", "has", "line sensors"),
])
def test_durable_facts_survive(subject, predicate, obj):
    assert not is_transient_absence(subject, predicate, obj)


def test_a_substring_match_does_not_eat_north_star():
    """The cleanup that prompted this guard used `"has no" in predicate`.

    That matches the middle of "has north-star", and it deleted the
    Theora north-star from the operator's graph before it was restored.
    Word boundaries, not substrings.
    """
    assert not is_transient_absence(
        "Theora", "has north-star", "Build what the future will call obvious.",
    )


def test_underscore_predicates_are_matched():
    """`\\b` never fires inside "does_not_provide": `_` is a word char.

    The extractor emits both spellings, so separators are normalised
    before matching. Missing this let every underscore predicate through.
    """
    assert is_transient_absence("x", "does_not_provide", "activity data")
    assert is_transient_absence("x", "does-not-provide", "HRV data")
    assert is_transient_absence("x", "does not provide", "sleep data")
