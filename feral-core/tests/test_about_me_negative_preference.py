"""A negative preference must not be stored as a preference.

Found on 2026-08-23 in a real operator profile. The stored fact read:

    Prefers: know been working on the demo so we'll see how it's gonna go

The speech it came from was:

    I don't know been working on the demo so we'll see how it's gonna go

The extractor has two patterns that share the `preference` kind, one
positive (`I prefer/like/love/enjoy X`) and one negative (`I don't/do
not/never X`), and the text template was keyed on the kind alone. So
every negative preference was written down as its own opposite.

The `negative` tag WAS recorded, and recording it is not enough:
`render_for_prompt` emits `f.text` and nothing else, so the tag never
reaches the model and the inverted sentence is what lands in the system
prompt. "I don't drink coffee" became "Prefers: drink coffee", and the
agent would act on it.
"""
from __future__ import annotations

import pytest

from agents.about_me import AboutMeStore


@pytest.fixture()
def store(tmp_path):
    return AboutMeStore(db_path=str(tmp_path / "about_me.db"))


class TestNegativePreferences:

    @pytest.mark.parametrize("sentence, payload", [
        ("I don't drink coffee at all.", "drink coffee at all"),
        ("I do not use social media.", "use social media"),
        ("I never work on weekends.", "work on weekends"),
    ])
    def test_a_negation_is_never_recorded_as_a_preference(
        self, store, sentence, payload,
    ):
        store.extract_from_text(sentence)
        texts = [f.text for f in store.list()]
        assert texts, f"nothing extracted from {sentence!r}"
        assert not any(t.startswith("Prefers:") for t in texts), (
            f"{sentence!r} was stored as a positive preference: {texts}"
        )
        assert any(payload in t for t in texts), (
            f"the negated thing was lost entirely: {texts}"
        )

    def test_the_stored_text_reads_as_a_negation(self, store):
        store.extract_from_text("I don't drink coffee.")
        text = store.list()[0].text
        assert text.startswith("Does not:")
        assert "drink coffee" in text

    def test_positive_preferences_are_untouched(self, store):
        """The fix must not cost the case the extractor is actually for."""
        store.extract_from_text("I prefer tea in the morning.")
        texts = [f.text for f in store.list()]
        assert any(t.startswith("Prefers:") for t in texts), texts

    def test_the_negative_tag_is_still_recorded(self, store):
        """The tag was never the problem; losing it would be a
        regression in the other direction."""
        store.extract_from_text("I don't drink coffee.")
        fact = store.list()[0]
        assert "negative" in fact.tags

    def test_the_prompt_block_no_longer_inverts_the_meaning(self, store):
        """The end of the chain, which is the thing that mattered.

        `system_prompt_chunk` emits `f.text` alone, so whatever
        inversion is in the text is what the model is told.
        """
        store.extract_from_text("I don't drink coffee.")
        rendered = store.system_prompt_chunk()
        assert "Prefers: drink coffee" not in rendered
        assert "drink coffee" in rendered

    def test_the_exact_sentence_from_the_real_profile(self, store):
        """Regression pin for the fact this was found in."""
        store.extract_from_text(
            "Spoke to know I don't know been working on the demo so "
            "we'll see how it's gonna go"
        )
        texts = [f.text for f in store.list()]
        assert not any(
            t.startswith("Prefers: know been working") for t in texts
        ), texts


class TestNegativeCaptureShape:
    """Rendering the negation correctly was half the fix.

    The live row on the operator's brain (GET /api/about-me, audited
    2026-08-23) was ``Prefers: know been working on the demo so we'll see
    how it's gonna go`` at confidence 0.5. With the "Does not:" template
    alone the same sentence would still be extracted today, as ``Does
    not: know been working on the demo so we'll see how it's gonna go``,
    which is not a preference either. "I don't know" is a disfluency and
    the old ``.{4,120}`` capture ran to the end of the utterance.
    """

    EXACT = (
        "Spoke to know I don't know been working on the demo so "
        "we'll see how it's gonna go"
    )

    def test_the_exact_sentence_produces_no_preference_at_all(self, store):
        store.extract_from_text(self.EXACT)
        facts = store.list()
        assert not [f for f in facts if f.kind == "preference"], [
            f.text for f in facts
        ]

    def test_capture_stops_at_the_clause_boundary(self, store):
        store.extract_from_text("I don't eat pork, it makes me sick")
        texts = [f.text for f in store.list()]
        assert texts == ["Does not: eat pork"], texts

    def test_dark_mode_negation_still_extracts(self, store):
        store.extract_from_text("I never use dark mode")
        texts = [f.text for f in store.list()]
        assert texts == ["Does not: use dark mode"], texts

    @pytest.mark.parametrize("sentence", [
        "I don't know been working on the demo",
        "I don't think that's the right approach for us",
        "I don't really have an opinion on that",
        "I don't remember where I parked the car",
        "I don't get why it keeps failing",
        "I don't understand the question",
        "I don't see the point of that",
        "I don't mean to be rude about it",
        "I never guess at these things",
        # A single word after the negation is never a usable preference.
        "I don't care.",
    ])
    def test_disfluencies_and_verbs_of_thinking_are_not_preferences(
        self, store, sentence,
    ):
        store.extract_from_text(sentence)
        texts = [f.text for f in store.list()]
        assert not any(t.startswith("Does not:") for t in texts), (
            f"{sentence!r} was stored as a preference: {texts}"
        )

    @pytest.mark.parametrize("sentence, expected", [
        ("I don't drink coffee after 4pm.", "Does not: drink coffee after 4pm"),
        ("I do not use social media; it wastes my time", "Does not: use social media"),
        ("I never work on weekends, and I don't check email then",
         "Does not: work on weekends"),
    ])
    def test_real_negative_preferences_survive_the_gate(
        self, store, sentence, expected,
    ):
        store.extract_from_text(sentence)
        texts = [f.text for f in store.list()]
        assert expected in texts, texts
