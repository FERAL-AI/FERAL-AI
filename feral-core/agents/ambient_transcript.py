"""Summarize an ambient conversation and lift the promises out of it.

The glasses are the microphone, the phone is the recorder, and the phone
transcribes on device. This module is everything after the transcript
reaches the brain: reduce it to something the user can find later, and
extract anything they committed to out loud.

Two consumers, and they read different places, which is why both writes
happen:

* chat recall ("what did I discuss with Noah") reads ``episodes``
* the briefing ("brief me") reads ``IntentCompiler``, which does not
  look at memory at all

Design notes that are not obvious from the code:

**Map then reduce.** ``memory/context_builder.py:288`` is the closest
prior art and it is map-only: it chunks, summarizes each chunk, and
joins. An hour of speech is roughly 55k characters, so that produces a
ten paragraph concatenation rather than a summary. The reduce pass is
what makes the output usable, and it is also where the JSON comes from.

**Chunking follows sentences, not character offsets.** Splitting mid
sentence loses the clause that carried the promise.

**The transcript is untrusted.** It is other people talking, and the
summary it produces is injected into the model's context on later turns.
It is wrapped with the same boundary fencing used for web content before
it reaches any prompt.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from agents.prompt_refiner import _parse_json
from agents.token_estimate import estimate_tokens
from security.content_defense import injection_matches, wrap_external_content

logger = logging.getLogger("feral.ambient")

# Characters per map chunk. context_builder uses 6000 and it is a
# reasonable working size for every provider in the failover chain.
CHUNK_CHARS = 6000

# FailoverReason.CONTEXT_OVERFLOW re-raises rather than failing over
# (agents/llm_provider.py:5192), so overflow is a hard error to size
# around rather than something to recover from. Map summaries are short,
# but a very long conversation can still produce many of them.
MAX_REDUCE_TOKENS = 12000

EVENT_TYPE = "ambient_conversation"

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass
class TranscriptOutcome:
    """What the brain made of one conversation."""

    summary: str = ""
    detail: str = ""
    people: list[str] = field(default_factory=list)
    commitments: list[dict] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    injection_flags: list[str] = field(default_factory=list)

    physiological_note: str = ""
    """What the body did, in one sentence, or "".

    Deliberately separate from ``summary`` rather than folded into it.
    A client must be able to render or suppress the physiological claim
    on its own, and a reader must be able to tell which part of the
    record is what people said and which part is what a heart rate did.

    Never describes an emotional state, and never derives from a
    movement-confounded moment. See ``sanitise_physiological_note``.
    """

    moments_considered: int = 0
    """How many moments survived the confound and confidence filters.

    0 with a non-empty ``physiological_note`` is impossible by
    construction; it is reported so a client can say "no physiological
    signal" rather than showing an empty field of unknown meaning.
    """


def chunk_transcript(text: str, chunk_chars: int = CHUNK_CHARS) -> list[str]:
    """Split on sentence boundaries, packing up to ``chunk_chars``.

    A promise usually lives in one sentence ("I'll send Noah the SDK by
    Friday"). Slicing at a raw character offset can cut that sentence in
    half and lose it from both chunks, which is why this does not use the
    ``text[i:i+n]`` approach in context_builder.
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= chunk_chars:
        return [text]

    chunks: list[str] = []
    current = ""
    for sentence in _SENTENCE_END.split(text):
        if not sentence:
            continue
        # A single sentence longer than the budget is split hard; there is
        # no boundary left to respect.
        while len(sentence) > chunk_chars:
            if current:
                chunks.append(current.strip())
                current = ""
            chunks.append(sentence[:chunk_chars])
            sentence = sentence[chunk_chars:]
        if len(current) + len(sentence) + 1 > chunk_chars:
            chunks.append(current.strip())
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current.strip():
        chunks.append(current.strip())
    return [c for c in chunks if c]


def _heuristic_outcome(text: str, when: Optional[float], speakers: list[str]) -> TranscriptOutcome:
    """What to store when no model is available.

    Degrading to the raw opening of the transcript keeps the conversation
    findable by full-text search even though nothing was summarized. The
    alternative, storing nothing, loses the conversation entirely.
    """
    head = " ".join((text or "").split())[:480]
    who = ", ".join(speakers) if speakers else "unknown participants"
    stamp = time.strftime("%Y-%m-%d", time.localtime(when or time.time()))
    return TranscriptOutcome(
        summary=f"Conversation on {stamp} with {who}: {head}"[:500],
        detail=text or "",
        people=list(speakers),
        degraded=["no_llm"],
    )


_MAP_PROMPT = (
    "Summarize this segment of a spoken conversation concisely. Preserve:\n"
    "- who said what, by name where a name is spoken\n"
    "- decisions, and anything either side agreed to do\n"
    "- dates, deadlines and numbers, verbatim\n"
    "- open questions\n\n"
    "This is a transcript of speech, so expect disfluency and mis-hearings. "
    "Do not invent detail that is not present.\n\n"
)

_REDUCE_PROMPT = (
    "Below are ordered summaries of consecutive segments of ONE spoken "
    "conversation. Produce a single consolidated record of it.\n\n"
    "A commitment is something THE USER said THEY would do. Something "
    "another person promised is not a commitment; it belongs in summary. "
    "Quote each commitment close to the words actually spoken, and give "
    "due_iso only when a date or day was actually said.\n\n"
    "Schema:\n"
    "{\n"
    '  "summary": "3-5 sentences naming every person and what was discussed",\n'
    '  "people": ["names spoken in the conversation"],\n'
    '  "topics": ["short topic labels"],\n'
    '  "commitments": [{"text": "what the user promised", "due_iso": "YYYY-MM-DD or null"}],\n'
    '  "physiological_note": "one sentence, or empty string. See the rules."\n'
    "}\n\n"
    "Output ONLY valid JSON. No prose. No markdown.\n\n"
)

# Appended to the reduce prompt only when the phone sent moments. Kept
# separate so a transcript without physiology is summarized by the exact
# prompt it was summarized by before, rather than one carrying rules
# about data that is not there.
_PHYSIOLOGY_RULES = (
    "\n\nPHYSIOLOGICAL SIGNAL\n"
    "A wearable recorded the speaker's heart rate during this "
    "conversation. Points where it deviated from their baseline are "
    "listed below. Use them for `physiological_note` ONLY.\n\n"
    "RULES, IN ORDER OF IMPORTANCE:\n"
    "1. A moment marked CONFOUNDED means MOVEMENT explains the rise: "
    "they stood up, walked, took stairs. You must NEVER describe a "
    "confounded moment as an emotional or stress response, and never "
    "connect it to what was being discussed. If every moment is "
    "confounded, return an empty `physiological_note`. Inventing a "
    "feeling from a flight of stairs is the worst thing you can do "
    "here.\n"
    "2. Describe only what was measured. \"His heart rate rose 14 bpm "
    "above baseline while the investor update was discussed\" is a "
    "fact. \"He was anxious about the investors\" is a diagnosis, and "
    "you are not permitted to make one. No emotion words: not anxious, "
    "stressed, nervous, upset, afraid, excited.\n"
    "3. Anchor to the conversation using the QUOTE or TIME given with "
    "each moment. The segment numbers below are the RECORDING DEVICE'S "
    "numbering and do NOT correspond to the [segment N] labels above. "
    "Never match them to each other. A moment with no quote and no time "
    "is not anchored to anything; mention it only as something that "
    "happened during the conversation, with no claim about when.\n"
    "4. Say nothing about health, diagnosis or medical significance.\n"
    "5. `physiological_note` is at most one sentence, and an empty "
    "string is the right answer whenever the signal is weak, entirely "
    "confounded, or you cannot anchor it honestly.\n\n"
)


#: Words that assert an inner state. A physiological note may report
#: what the body did; it may not say what the person felt, and it may
#: never do so on the strength of a movement artefact. Checked after the
#: model returns, because a prompt rule is a request and this is a
#: guarantee.
_EMOTION_WORDS = frozenset({
    "anxious", "anxiety", "stressed", "stress", "stressful", "nervous",
    "nerves", "upset", "afraid", "fear", "fearful", "scared", "panic",
    "panicked", "worried", "worry", "worrying", "tense", "agitated",
    "distressed", "uneasy", "excited", "excitement", "angry", "anger",
    "frustrated", "frustration", "emotional", "emotionally", "triggered",
    "defensive", "uncomfortable", "alarmed", "dread", "rattled",
})

# Below this the phone is not confident it saw a real reaction, and a
# summary should not narrate a maybe.
_MOMENT_MIN_SCORE = 0.5


def usable_moments(moments: Any) -> list[dict]:
    """The moments a summary is allowed to reason about.

    Drops confounded moments outright rather than passing them to the
    model with a warning attached. A rule in a prompt is a request; not
    sending the data is a guarantee, and the guarantee is the one worth
    having when the failure mode is telling somebody they were anxious
    about their investors because they climbed the stairs.

    Also drops low-confidence moments: the phone's own score is the only
    evidence that a deviation was a reaction at all.
    """
    if not isinstance(moments, list):
        return []
    out: list[dict] = []
    for item in moments:
        if not isinstance(item, dict):
            continue
        if item.get("confounded"):
            continue
        try:
            score = float(item.get("score") or 0.0)
        except (TypeError, ValueError):
            continue
        if score < _MOMENT_MIN_SCORE:
            continue
        try:
            delta = float(item.get("delta_bpm") or 0.0)
        except (TypeError, ValueError):
            delta = 0.0
        if delta == 0:
            continue
        out.append(item)
    return out


def render_moments(
    moments: list[dict],
    *,
    baseline_hr: Optional[float] = None,
    respiratory_bpm: Optional[float] = None,
) -> str:
    """Format moments for the reduce prompt, anchored honestly.

    The anchor priority is quote, then time, then nothing. It is never
    the segment index: that indexes the PHONE's segmentation, while the
    prompt above labels the brain's 6000-character map chunks
    ``[segment N]``. Handing the model both numberings and letting it
    match them would attach physiology to the wrong part of the
    conversation while looking precise, which is worse than saying "at
    some point during this conversation".
    """
    if not moments:
        return ""
    lines = []
    if baseline_hr:
        lines.append(f"Baseline heart rate: {float(baseline_hr):.0f} bpm.")
    if respiratory_bpm:
        lines.append(f"Respiration: {float(respiratory_bpm):.0f} breaths/min.")
    for item in moments:
        delta = float(item.get("delta_bpm") or 0.0)
        score = float(item.get("score") or 0.0)
        direction = "above" if delta > 0 else "below"
        quote = str(item.get("quote") or "").strip()
        offset = item.get("t_offset_s")
        if quote:
            where = f'while these words were spoken: "{quote[:300]}"'
        elif isinstance(offset, (int, float)):
            where = f"{int(offset)}s into the conversation"
        else:
            where = "at an unanchored point in the conversation"
        lines.append(
            f"- {abs(delta):.0f} bpm {direction} baseline, {where} "
            f"(confidence {score:.2f})"
        )
    return "\n".join(lines)


def sanitise_physiological_note(note: Any, had_usable_moments: bool) -> str:
    """Enforce the confound and diagnosis rules on what the model wrote.

    The prompt already forbids all of this. This exists because the
    prompt cannot guarantee it, and the thing being guaranteed is that
    the system never tells someone what they felt on the strength of a
    heart rate. Dropping a good sentence occasionally is the correct
    trade against emitting a bad one once.

    Returns "" rather than an edited sentence. A note with the emotion
    word removed still carries the causal claim that made it wrong.
    """
    text = str(note or "").strip()
    if not text:
        return ""
    if not had_usable_moments:
        # Nothing survivable was sent, so anything here was invented.
        logger.warning(
            "ambient: dropping physiological_note produced with no usable "
            "moments: %r", text[:200],
        )
        return ""
    words = set(re.findall(r"[a-z]+", text.lower()))
    offending = words & _EMOTION_WORDS
    if offending:
        logger.warning(
            "ambient: dropping physiological_note asserting an inner state "
            "(%s): %r", ", ".join(sorted(offending)), text[:200],
        )
        return ""
    return text[:300]


async def summarize_transcript(
    text: str,
    *,
    llm: Any,
    started_at: Optional[float] = None,
    speakers: Optional[list[str]] = None,
    source: str = "ambient_transcript",
    moments: Optional[list[dict]] = None,
    baseline_hr: Optional[float] = None,
    respiratory_bpm: Optional[float] = None,
) -> TranscriptOutcome:
    """Map every chunk, reduce once to JSON, degrade rather than fail.

    Never raises for a provider error: ``LLMProvider.chat`` returns the
    error in the response dict rather than raising, and a transcript that
    cannot be summarized must still be stored so the conversation remains
    findable.
    """
    speakers = list(speakers or [])
    text = (text or "").strip()
    if not text:
        return TranscriptOutcome(degraded=["empty"])

    flags = injection_matches(text)
    if flags:
        # Recorded, not refused. Somebody saying "ignore your
        # instructions" in a meeting is a thing that happened and the
        # user may want to know about it. The fencing below is what makes
        # it safe to summarize.
        logger.warning("ambient transcript matched injection patterns: %s", flags)

    if llm is None or not getattr(llm, "available", False):
        out = _heuristic_outcome(text, started_at, speakers)
        out.injection_flags = flags
        return out

    fenced = wrap_external_content(text, source=source)
    chunks = chunk_transcript(fenced)
    degraded: list[str] = []

    # ── map ──
    partials: list[str] = []
    for index, chunk in enumerate(chunks):
        try:
            response = await llm.chat(
                messages=[{"role": "user", "content": _MAP_PROMPT + chunk}],
                temperature=0.2,
                max_tokens=800,
                call_site="compaction",
            )
            partial, _ = llm.extract_response(response)
            partials.append((partial or "").strip() or chunk[:400])
        except Exception as exc:  # provider errors surface in the dict, not here
            logger.warning("ambient map chunk %d failed: %s", index, exc)
            degraded.append(f"map_chunk_{index}")
            partials.append(chunk[:400])

    joined = "\n\n".join(f"[segment {i + 1}] {p}" for i, p in enumerate(partials))

    # A conversation long enough to overflow the reduce is summarized from
    # its map summaries, oldest dropped first, because the end of a
    # conversation is where the commitments usually are.
    while estimate_tokens(joined) > MAX_REDUCE_TOKENS and len(partials) > 1:
        partials.pop(0)
        degraded.append("reduce_truncated")
        joined = "\n\n".join(f"[segment {i + 1}] {p}" for i, p in enumerate(partials))

    # ── reduce ──
    #
    # Physiology is appended only when the phone actually sent usable
    # moments, so a transcript without it is summarized by the identical
    # prompt it was summarized by before this feature existed.
    kept_moments = usable_moments(moments)
    dropped = len(moments or []) - len(kept_moments)
    if dropped > 0:
        logger.info(
            "ambient: %d of %d moments not used (confounded or below "
            "confidence)", dropped, len(moments or []),
        )
    reduce_prompt = _REDUCE_PROMPT
    if kept_moments:
        reduce_prompt = reduce_prompt + _PHYSIOLOGY_RULES + render_moments(
            kept_moments,
            baseline_hr=baseline_hr,
            respiratory_bpm=respiratory_bpm,
        ) + "\n\n"

    try:
        response = await llm.chat(
            messages=[{"role": "user", "content": reduce_prompt + joined}],
            temperature=0.2,
            max_tokens=1200,
            call_site="compaction",
        )
        raw, _ = llm.extract_response(response)
    except Exception as exc:
        logger.warning("ambient reduce failed: %s", exc)
        out = _heuristic_outcome(text, started_at, speakers)
        out.degraded = degraded + ["reduce_failed"]
        out.injection_flags = flags
        return out

    parsed = _parse_json(raw or "")
    if parsed is None:
        # None, not {}, so "the model returned no JSON" is distinguishable
        # from "the model returned empty JSON". There is no
        # retry-on-malformed convention in this codebase; degrade instead.
        logger.warning("ambient reduce returned no parseable JSON")
        out = _heuristic_outcome(text, started_at, speakers)
        out.detail = (raw or text)[:20000]
        out.degraded = degraded + ["reduce_unparseable"]
        out.injection_flags = flags
        return out

    return TranscriptOutcome(
        summary=str(parsed.get("summary") or "").strip()[:500],
        detail=text,
        people=_string_list(parsed.get("people"), speakers),
        topics=_string_list(parsed.get("topics"), []),
        commitments=_commitment_list(parsed.get("commitments")),
        degraded=degraded,
        injection_flags=flags,
        # Checked here, after the model, not only asked for in the
        # prompt. The prompt states the confound and diagnosis rules;
        # this is what makes them hold when the model ignores them.
        physiological_note=sanitise_physiological_note(
            parsed.get("physiological_note"), bool(kept_moments),
        ),
        moments_considered=len(kept_moments),
    )


def _string_list(value: Any, fallback: list[str]) -> list[str]:
    """Shape validation, not schema validation: keep what is usable."""
    if not isinstance(value, list):
        return list(fallback)
    out = [str(v).strip() for v in value if isinstance(v, (str, int, float)) and str(v).strip()]
    return out or list(fallback)


def _commitment_list(value: Any) -> list[dict]:
    """Filter to dicts carrying the one required key and skip the rest.

    Mirrors memory/knowledge_graph.py:919-928: a model that returns nine
    good rows and one malformed one should give nine commitments, not an
    exception.
    """
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        due = item.get("due_iso")
        due_s = str(due).strip() if isinstance(due, str) and str(due).strip().lower() != "null" else ""
        out.append({"text": text[:500], "due_iso": due_s})
    return out


def build_episode_fields(
    outcome: TranscriptOutcome,
    *,
    started_at: Optional[float],
    source: str,
    speakers: list[str],
) -> dict:
    """Shape the episode so it is actually findable.

    ``participants`` and ``location`` are NOT searchable: neither is in
    ``episodes_fts`` (memory/store.py:946, which indexes summary and
    detail only), neither is embedded, and the FTS leg of the hybrid
    search does not select them. So every name, the date and the source
    go into the prose fields, and are repeated at the FRONT of detail,
    because fused_timeline renders only ``content[:500]``.

    ``summary`` is also the only field that reaches the model in the
    per-turn context block, which renders "- [event_type] summary" and
    drops detail, participants, location and created_at entirely.
    """
    when = started_at or time.time()
    stamp = time.strftime("%A %Y-%m-%d", time.localtime(when))
    people = outcome.people or speakers
    who = ", ".join(people) if people else "unknown participants"

    summary = outcome.summary or "Conversation with no summary available."
    headline = f"Conversation on {stamp} with {who}. {summary}"

    lead = f"Conversation on {stamp} with {who}. Source: {source}."
    if outcome.topics:
        lead += f" Topics: {', '.join(outcome.topics)}."
    if outcome.commitments:
        promised = "; ".join(c["text"] for c in outcome.commitments)
        lead += f" Promised: {promised}."

    return {
        "event_type": EVENT_TYPE,
        "summary": headline[:500],
        "detail": f"{lead}\n\n{outcome.detail}".strip(),
        "participants": people,
        # Above forget_threshold after decay so it survives every read
        # path; a conversation the user chose to record is not incidental.
        "importance": 0.65,
        "created_at": when,
    }
