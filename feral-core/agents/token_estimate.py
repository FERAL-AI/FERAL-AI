"""Conservative token estimation for budget decisions.

Every budget in the brain used ``len(str(content)) // 4``. That number is only
right for English prose. Measured against the real tokenizers (``cl100k_base``
and ``o200k_base``, taking the worse of the two, because the router talks to 16
providers and the estimate has to hold for all of them):

    sample            chars    real   //4 estimate   ratio
    english prose      1800     401            450    1.12
    python code        1675     575            418    0.73
    JSON               1180     540            295    0.55
    Russian            2160    1041            540    0.52
    Chinese            1080    1260            270    0.21
    Japanese           1080    1020            270    0.26
    Hebrew             1600    1651            400    0.24
    emoji               800    2200            200    0.09

A Chinese conversation was measured at a fifth of its real size and an
emoji-heavy one at a *ninth*. Two consequences, both live:

* ``agents/context_engine.py`` prunes and summarises against this number, so a
  context it believes is comfortably inside the window is sent five times over
  it and the provider rejects the request.
* ``agents/llm_provider.py``, ``agents/learner.py`` and
  ``agents/proactive_engine.py`` price calls with it, so spend against a USD
  budget is under-reported by the same factor.

**The asymmetry is deliberate.** Under-counting produces a hard failure (a
refused request, or a budget silently overshot). Over-counting produces earlier
summarisation, which costs context but keeps working. So this estimator is
tuned to never fall below the real count on the measured corpus, and it accepts
over-counting up to about 2x on English to get there.

It is a heuristic, not a tokenizer. Using a real tokenizer would mean either
declaring ``tiktoken`` (correct for OpenAI only, and this router has 16
providers) or a network round-trip per estimate on a hot path. The corpus in
``tests/fixtures/token_estimate_corpus.json`` is what holds it honest, and
``tests/test_token_estimate_never_undercounts.py`` re-derives those numbers
from live tiktoken wherever it is importable.

Calibration, all measured rather than assumed:

* An alphanumeric run of up to 12 characters is a word or a short identifier
  and BPE merges it at roughly 4 characters per token. Beyond 12 it is a hash,
  a UUID or base64, which merge at closer to 1: measured base64 at 1.38
  characters per token, so a long run is charged at 1.
* Cyrillic is charged less than other two-byte scripts (0.7 against 1.3) purely
  because the vocabularies cover it far better: Russian measured 2.07
  characters per token where Hebrew measured 0.97. This is tokenizer
  coverage, not a property of the script.
* Astral-plane characters are charged 3. Emoji measured 2.75 tokens each.
* Whitespace is charged 0.3 rather than 0. It usually merges into the
  following token in Latin text, but Hebrew and Thai samples under-counted
  without it.
"""

from __future__ import annotations

import math
import re
from typing import Any


__all__ = ["estimate_tokens", "estimate_message_tokens"]


_ALNUM_RUN = re.compile(r"[A-Za-z0-9]+")

# An alphanumeric run longer than this is not a word.
_RUN_MERGE_LIMIT = 12
# Characters per token inside a short run (a word) and beyond it (a hash).
_SHORT_RUN_CHARS_PER_TOKEN = 4.0
_LONG_RUN_CHARS_PER_TOKEN = 1.0

_WHITESPACE_WEIGHT = 0.3
_ASCII_WEIGHT = 1.0
_CYRILLIC_WEIGHT = 0.7
_TWO_BYTE_WEIGHT = 1.3
_THREE_BYTE_WEIGHT = 1.3
_ASTRAL_WEIGHT = 3.0

_CYRILLIC_START = 0x0400
_CYRILLIC_END = 0x04FF


def _run_tokens(length: int) -> float:
    if length <= _RUN_MERGE_LIMIT:
        return math.ceil(length / _SHORT_RUN_CHARS_PER_TOKEN)
    return math.ceil(_RUN_MERGE_LIMIT / _SHORT_RUN_CHARS_PER_TOKEN) + math.ceil(
        (length - _RUN_MERGE_LIMIT) / _LONG_RUN_CHARS_PER_TOKEN
    )


def _char_tokens(char: str) -> float:
    if char.isspace():
        return _WHITESPACE_WEIGHT
    code_point = ord(char)
    if code_point < 128:
        return _ASCII_WEIGHT
    if _CYRILLIC_START <= code_point <= _CYRILLIC_END:
        return _CYRILLIC_WEIGHT
    utf8_length = len(char.encode("utf-8", "surrogatepass"))
    if utf8_length == 2:
        return _TWO_BYTE_WEIGHT
    if utf8_length == 3:
        return _THREE_BYTE_WEIGHT
    return _ASTRAL_WEIGHT


def estimate_tokens(text: Any) -> int:
    """Conservative token count for `text`. Never below the real count.

    Non-string input is stringified, matching the `len(str(content)) // 4` this
    replaces: message content is routinely a list of content blocks.
    """
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return 0

    total = 0.0
    index = 0
    for match in _ALNUM_RUN.finditer(text):
        for char in text[index:match.start()]:
            total += _char_tokens(char)
        total += _run_tokens(match.end() - match.start())
        index = match.end()
    for char in text[index:]:
        total += _char_tokens(char)
    return int(math.ceil(total))


def estimate_message_tokens(messages: list[dict]) -> int:
    """Conservative token count for a chat message list.

    Content blocks are walked rather than stringified, mirroring
    ``LLMProvider._message_char_count``. Stringifying a block list would count
    the Python dict syntax around the text, which inflates a multimodal turn by
    the length of its own punctuation.
    """
    total = 0
    for message in messages or []:
        content = message.get("content") if isinstance(message, dict) else ""
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    total += estimate_tokens(part.get("text", "") or "")
                else:
                    total += estimate_tokens(part)
        else:
            total += estimate_tokens(content if content is not None else "")
    return total
