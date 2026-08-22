"""Physiological moments carried with an ambient transcript.

Asked for by the Theora iOS client on 2026-08-22. The phone computes,
per transcript segment, the heart-rate deviation from baseline and
whether movement explains it; the brain reasons over that alongside the
words.

The whole feature turns on ONE rule, and these tests exist mostly to
pin it: a moment marked ``confounded`` means movement explains the
rise, and the summary must never describe it as an emotional response.
Telling somebody they were anxious about their investors because they
climbed a flight of stairs is the difference between a health product
and a liability.

That rule is enforced twice on purpose. Confounded moments are never
put in front of the model, AND the sentence the model returns is
checked again afterwards. A prompt rule is a request; the filter and
the post-check are what make it a guarantee.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from agents.ambient_transcript import (
    render_moments,
    sanitise_physiological_note,
    summarize_transcript,
    usable_moments,
)


TEXT = (
    "We talked about the investor update. Noah said the round closes "
    "Friday. I said I would send the deck over."
)

CONFOUNDED_QUOTE = "lets head upstairs"

MOMENTS = [
    {
        "segment_index": 2, "delta_bpm": 14, "score": 0.82,
        "confounded": False, "quote": "the investor update",
    },
    {
        "segment_index": 5, "delta_bpm": 22, "score": 0.91,
        "confounded": True, "quote": CONFOUNDED_QUOTE,
    },
    {
        "segment_index": 7, "delta_bpm": 3, "score": 0.20,
        "confounded": False, "quote": "anyway",
    },
]


class _FakeLLM:
    """Returns a fixed physiological_note and records every prompt."""

    available = True

    def __init__(self, note: str = ""):
        self.note = note
        self.prompts: list[str] = []

    async def chat(self, *, messages, **kwargs):
        content = messages[0]["content"]
        self.prompts.append(content)
        if "Schema:" in content:
            body = json.dumps({
                "summary": "Discussed the investor update with Noah.",
                "people": ["Noah"],
                "topics": ["investors"],
                "commitments": [{"text": "send the deck", "due_iso": None}],
                "physiological_note": self.note,
            })
        else:
            body = "segment summary"
        return {"choices": [{"message": {"content": body}}]}

    def extract_response(self, response):
        return response["choices"][0]["message"]["content"], None

    @property
    def reduce_prompt(self) -> str:
        return next(p for p in self.prompts if "Schema:" in p)


def _run(llm, **kwargs):
    return asyncio.run(summarize_transcript(TEXT, llm=llm, **kwargs))


# ── the wire contract ───────────────────────────────────────────────────


class TestWireContract:

    def test_transcript_accepts_moments(self):
        from models.protocol import AmbientTranscriptPayload

        payload = AmbientTranscriptPayload(
            text=TEXT, moments=MOMENTS, baseline_hr=58, respiratory_bpm=14,
        )
        assert len(payload.moments) == 3
        assert payload.moments[1].confounded is True
        assert payload.baseline_hr == 58
        assert payload.respiratory_bpm == 14

    def test_all_of_it_is_optional(self):
        """A phone that computes none of this must be unaffected."""
        from models.protocol import AmbientTranscriptPayload

        payload = AmbientTranscriptPayload(text=TEXT)
        assert payload.moments == []
        assert payload.baseline_hr is None
        assert payload.respiratory_bpm is None

    def test_digest_carries_the_note_back(self):
        from models.protocol import AmbientDigestPayload

        digest = AmbientDigestPayload(
            transcript_id="t1",
            physiological_note="Heart rate rose 14 bpm above baseline.",
            moments_considered=1,
        )
        assert digest.moments_considered == 1
        assert AmbientDigestPayload(transcript_id="t1").physiological_note == ""


# ── Ask 5: the confound rule ────────────────────────────────────────────


class TestConfoundedMomentsAreNeverEmotional:

    def test_confounded_moments_are_filtered_out(self):
        kept = usable_moments(MOMENTS)
        assert len(kept) == 1
        assert kept[0]["quote"] == "the investor update"

    def test_the_model_never_sees_a_confounded_moment(self):
        """Not sent, not sent-with-a-warning.

        A rule in a prompt is a request. Withholding the data is a
        guarantee, and the guarantee is the one worth having here.
        """
        llm = _FakeLLM("Heart rate rose 14 bpm above baseline.")
        _run(llm, moments=MOMENTS, baseline_hr=58)
        assert CONFOUNDED_QUOTE not in llm.reduce_prompt

    def test_a_note_from_only_confounded_moments_is_dropped(self):
        llm = _FakeLLM("His heart raced during the meeting.")
        out = _run(llm, moments=[MOMENTS[1]], baseline_hr=58)
        assert out.physiological_note == ""
        assert out.moments_considered == 0

    @pytest.mark.parametrize("note", [
        "He was clearly anxious when the investors came up.",
        "He seemed stressed by the round closing.",
        "The topic made him visibly nervous.",
        "He was upset about the deadline.",
        "This was an emotional moment for him.",
    ])
    def test_a_diagnosis_is_dropped_even_if_the_model_writes_one(self, note):
        """The prompt forbids this. This is what makes it hold."""
        llm = _FakeLLM(note)
        out = _run(llm, moments=MOMENTS, baseline_hr=58)
        assert out.physiological_note == "", (
            "an inner state must never be asserted from a heart rate"
        )

    def test_the_note_is_dropped_whole_not_edited(self):
        """A sentence with the emotion word removed still carries the
        causal claim that made it wrong."""
        assert sanitise_physiological_note(
            "He was anxious about the round.", True,
        ) == ""

    def test_a_factual_note_survives(self):
        note = "Heart rate rose 14 bpm above baseline while the investor update was discussed."
        assert sanitise_physiological_note(note, True) == note

    def test_a_note_invented_with_no_moments_is_dropped(self):
        assert sanitise_physiological_note("His heart raced.", False) == ""


# ── Ask 4: reasoning over what survives ─────────────────────────────────


class TestTheDigestReasonsOverMoments:

    def test_low_confidence_moments_are_dropped(self):
        """The phone's own score is the only evidence a deviation was a
        reaction at all. A summary should not narrate a maybe."""
        kept = usable_moments([MOMENTS[2]])
        assert kept == []

    def test_a_factual_note_reaches_the_outcome(self):
        note = "Heart rate rose 14 bpm above baseline while the investor update was discussed."
        out = _run(_FakeLLM(note), moments=MOMENTS, baseline_hr=58)
        assert out.physiological_note == note
        assert out.moments_considered == 1

    def test_baseline_and_respiration_reach_the_model(self):
        llm = _FakeLLM("")
        _run(llm, moments=MOMENTS, baseline_hr=58, respiratory_bpm=14)
        prompt = llm.reduce_prompt
        assert "58 bpm" in prompt
        assert "14 breaths/min" in prompt

    def test_the_note_is_separate_from_the_summary(self):
        """So a client can render or suppress the physiological claim on
        its own, and a reader can tell what people said from what a
        heart rate did."""
        out = _run(
            _FakeLLM("Heart rate rose 14 bpm above baseline."),
            moments=MOMENTS, baseline_hr=58,
        )
        assert out.physiological_note not in out.summary


class TestAnchoring:
    """`segment_index` indexes the PHONE's segmentation.

    `agents/ambient_transcript` chunks into 6000-character map segments
    labelled `[segment N]`. Those are a different partition of the same
    conversation, and joining on the bare index would attach physiology
    to the wrong part of it while looking precise.
    """

    def test_a_quote_anchors_the_moment(self):
        rendered = render_moments([MOMENTS[0]], baseline_hr=58)
        assert "the investor update" in rendered
        assert "14 bpm above baseline" in rendered

    def test_time_anchors_when_there_is_no_quote(self):
        rendered = render_moments([
            {"delta_bpm": 9, "score": 0.7, "confounded": False, "t_offset_s": 132},
        ])
        assert "132s into the conversation" in rendered

    def test_an_unanchored_moment_says_so(self):
        rendered = render_moments([
            {"segment_index": 4, "delta_bpm": 9, "score": 0.7, "confounded": False},
        ])
        assert "unanchored" in rendered

    def test_the_phone_segment_number_is_never_rendered_as_an_anchor(self):
        rendered = render_moments([
            {"segment_index": 4, "delta_bpm": 9, "score": 0.7, "confounded": False},
        ])
        assert "segment 4" not in rendered.lower()

    def test_the_model_is_told_the_numberings_do_not_correspond(self):
        llm = _FakeLLM("")
        _run(llm, moments=MOMENTS, baseline_hr=58)
        assert "do NOT correspond" in llm.reduce_prompt


# ── nothing changes for a phone that sends no physiology ────────────────


class TestNoPhysiologyIsUnchanged:

    def test_the_reduce_prompt_is_untouched(self):
        llm = _FakeLLM("")
        _run(llm)
        assert "PHYSIOLOGICAL SIGNAL" not in llm.reduce_prompt

    def test_the_summary_still_works(self):
        out = _run(_FakeLLM(""))
        assert out.summary
        assert out.commitments
        assert out.physiological_note == ""
        assert out.moments_considered == 0

    def test_malformed_moments_do_not_break_anything(self):
        out = _run(
            _FakeLLM(""),
            moments=["not a dict", {"score": "abc"}, {}, None],
        )
        assert out.summary
        assert out.moments_considered == 0
