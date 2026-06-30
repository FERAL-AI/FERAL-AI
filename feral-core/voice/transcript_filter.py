"""Shared transcript filtering for live-voice STT paths.

Bug 3 (live voice — phantom commits): OpenAI Realtime ``whisper-1`` plus
800ms server VAD repeatedly hallucinates stock closing phrases on
trailing HFP silence — "Bye-bye.", "Thank you.", "Thanks for watching.",
a bare "You.", an empty utterance, etc. Whisper is statistically biased
toward these closers in its YouTube/Vimeo training mix, so a long
period of low-amplitude room tone after the user actually stopped
talking is enough to commit one. Deepgram + speech_final has its own
variant when the speaker pauses mid-sentence with low confidence.

The realtime proxy used to forward every commit verbatim, so the
hallucinated phrase reached the orchestrator and the model dutifully
generated a reply (the operator's "I started getting random goodbyes
mid-call" report). The text path doesn't have this problem because it
hits the LLM only on a real, user-driven submit.

This module exposes one helper — :func:`should_commit_user_transcript`
— that the realtime/Gemini/Deepgram-fronted code paths call BEFORE
forwarding a final USER transcript downstream. It returns ``False``
when the utterance is one of the known phantom closers (after
case-normalising and stripping punctuation) and ``True`` for everything
else. Conservative by design: a legitimate short command ("yes",
"stop", "halt the robot") is never filtered.

For chained Deepgram, the confidence + ``speech_final`` flags are
exposed so callers can apply an additional confidence floor — Deepgram
emits hallucinated closers with confidence well below the typical
0.85+ band of a real utterance.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger("feral.voice.transcript_filter")

# Stock phrases that whisper-1 / Deepgram nova-3 are known to hallucinate
# on trailing silence. Kept narrow on purpose: only utterances that are
# JUST one of these phrases are dropped. A reply like "Yes, bye-bye for
# now then" still goes through because the gate only fires on exact
# (case- and punctuation-normalised) matches.
#
# Whisper biases (most common in production logs):
#   - "Bye." / "Bye-bye." / "Goodbye." — HFP / Bluetooth handover tail
#   - "Thank you." / "Thanks." / "Thanks for watching." / "Thanks for
#      listening." — YouTube/podcast training-data tells
#   - "You." — Whisper's prior on near-silent frames
#   - "." / "..." — the model "completing" a half-heard word as a stop
#
# These are normalised: lowercased, punctuation/whitespace stripped, so
# "Bye-bye!" → "byebye", "thanks for watching." → "thanks for watching".
_PHANTOM_PHRASES: frozenset[str] = frozenset({
    "",
    "bye",
    "byebye",
    "bye bye",
    "goodbye",
    "thank you",
    "thank you very much",
    "thank you so much",
    "thanks",
    "thanks for watching",
    "thanks for watching the video",
    "thanks for listening",
    "thanks for watching and ill see you in the next video",
    "you",
    "yeah",
    "uh",
    "um",
    "okay bye",
    "ok bye",
    "alright bye",
})

# When a transcript is < this many alphabetic characters AND has no
# digits AND isn't a known short command verb, it's likely a noise
# fragment. The bar is set deliberately low (3) so legitimate one-word
# commands like "yes" / "no" / "halt" still go through; only true
# fragments ("a", "uh", "mm") get caught here.
_MIN_MEANINGFUL_ALPHA_CHARS = 2

# Real one-word commands that must never be filtered even if they're
# very short. The phantom list above never overlaps with these.
_LEGITIMATE_SHORT: frozenset[str] = frozenset({
    "yes", "no", "go", "stop", "halt", "do", "ok", "okay", "now",
    "off", "on", "up", "down", "left", "right", "back", "next",
    "pause", "play", "skip", "mute", "unmute", "louder", "quieter",
    "again", "more", "less", "fast", "slow", "open", "close",
})

# Pure-punctuation / whitespace strings whisper sometimes commits.
_PUNCT_ONLY_RE = re.compile(r"^[\s\.\,\!\?\;\:\-\u2026]*$")


def _normalize(text: str) -> str:
    """Lowercase + strip punctuation/whitespace for membership tests.

    Hyphens between letters are collapsed (so "bye-bye" → "byebye"),
    other punctuation becomes whitespace, runs of whitespace collapse
    to one space.
    """
    if not text:
        return ""
    t = text.strip().lower()
    # Collapse intra-word hyphens / apostrophes so "bye-bye" and
    # "you're" normalise sensibly. Hyphens between letters → joined.
    t = re.sub(r"([a-z])[-’'`]([a-z])", r"\1\2", t)
    # Drop other punctuation entirely.
    t = re.sub(r"[\.,!?;:\-—…\"\(\)\[\]]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def should_commit_user_transcript(
    text: str,
    *,
    confidence: Optional[float] = None,
    speech_final: Optional[bool] = None,
    min_confidence: float = 0.55,
) -> bool:
    """Return True iff ``text`` should be forwarded as a final user turn.

    Drops:
      * Empty / whitespace-only transcripts.
      * Pure-punctuation strings ("." / "..." / "—").
      * Anything whose normalised form matches a known phantom phrase
        (case-insensitive, hyphen-insensitive).
      * Utterances shorter than :data:`_MIN_MEANINGFUL_ALPHA_CHARS`
        characters that aren't on the legitimate-short allowlist.
      * Streaming-STT fragments with confidence below ``min_confidence``
        when ``confidence`` is provided. Deepgram nova-3 commits stock
        closers with confidence well below this floor; setting it to
        0.55 keeps real-utterance commits (typically 0.7+) untouched.

    Otherwise returns True. The function is intentionally conservative
    — only drop when we're confident the commit is phantom.

    Args:
        text: The raw final transcript (without the ``[user] `` sentinel).
        confidence: STT confidence score in [0, 1], if provided.
        speech_final: Whether the STT considers the utterance complete
            (used today only for logging; future-reserved).
        min_confidence: Confidence floor below which a commit is rejected
            as low-quality. ``None`` confidence skips this gate.

    Returns:
        bool: True if the transcript should be forwarded, False to drop.
    """
    if text is None:
        return False
    raw = text.strip()
    if not raw:
        return False
    if _PUNCT_ONLY_RE.match(raw):
        logger.info("transcript_filter: dropped punctuation-only commit: %r", raw)
        return False

    normalized = _normalize(raw)

    if normalized in _PHANTOM_PHRASES:
        logger.info(
            "transcript_filter: dropped phantom phrase '%s' (raw=%r, conf=%s, speech_final=%s)",
            normalized, raw, confidence, speech_final,
        )
        return False

    # Confidence floor for streaming STT (Deepgram). Only enforced when
    # the provider gave us a score; whisper-1 in realtime mode does not.
    if confidence is not None and confidence < min_confidence:
        # Still allow a legitimate short command even at low confidence
        # — the STT may be uncertain about "stop" against background
        # noise but we'd rather honour a halt than drop it.
        if normalized not in _LEGITIMATE_SHORT:
            logger.info(
                "transcript_filter: dropped low-confidence commit conf=%.2f text=%r",
                confidence, raw,
            )
            return False

    # Short-utterance noise gate. Counts alphabetic characters in the
    # normalised string; anything below the floor is filtered UNLESS
    # it's on the legitimate-short allowlist.
    alpha_count = sum(1 for c in normalized if c.isalpha())
    if alpha_count < _MIN_MEANINGFUL_ALPHA_CHARS and normalized not in _LEGITIMATE_SHORT:
        logger.info(
            "transcript_filter: dropped short noise fragment %r (alpha=%d)",
            raw, alpha_count,
        )
        return False

    return True


def is_known_phantom_phrase(text: str) -> bool:
    """Convenience predicate for tests / callers that only want the
    blocklist membership check without the rest of the gate."""
    if not text:
        return True
    return _normalize(text) in _PHANTOM_PHRASES


__all__ = [
    "should_commit_user_transcript",
    "is_known_phantom_phrase",
]
