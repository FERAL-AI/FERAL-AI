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
    '  "commitments": [{"text": "what the user promised", "due_iso": "YYYY-MM-DD or null"}]\n'
    "}\n\n"
    "Output ONLY valid JSON. No prose. No markdown.\n\n"
)


async def summarize_transcript(
    text: str,
    *,
    llm: Any,
    started_at: Optional[float] = None,
    speakers: Optional[list[str]] = None,
    source: str = "ambient_transcript",
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
    try:
        response = await llm.chat(
            messages=[{"role": "user", "content": _REDUCE_PROMPT + joined}],
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
