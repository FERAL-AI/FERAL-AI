"""
FERAL Tool-Result Budgets — how much of a tool result the model gets to see
==========================================================================

WHY THIS MODULE EXISTS
----------------------
Until v2026.7.30 the amount of a tool result that reached the LLM was a
*global constant*, applied twice:

1. ``SkillExecutor._sanitize_response`` recursively cut **every** string to
   2 000 chars and **every** list to 20 elements, for every skill, on every
   lane. It was written for third-party HTTP responses, where the tight
   bound is a real defence (prompt injection + context flooding via an API
   you do not control).
2. Every conversation-history append then did
   ``json.dumps(result_data)[:2000]`` — a blind byte slice that frequently
   handed the model *syntactically invalid JSON* cut mid-token, with no
   marker saying anything had been removed.

The consequence was that FERAL's own first-party read tools were
decorative. ``coding_tools__read_file`` builds a line-numbered string at
~65 chars/line, so 2 000 chars is ~30 lines of source regardless of the
caller's ``limit``; the 2 MB file ceiling and the ``offset``/``limit``
params could never be reached. ``grep_search`` computed up to
``GREP_DEFAULT_HEAD_LIMIT`` (250) matches and returned 20. ``glob_search``
computed 100 and returned 20. ``bash`` computed ``MAX_OUTPUT`` (50 000)
chars and ~96% was thrown away before the model saw it. The careful
pagination work in ``skills/impl/coding_tools.py`` (``_paginate``,
``truncated`` flags, ``next_offset`` hints) was destroyed one layer up.

THE FIX: BUDGET IS A PROPERTY OF THE TOOL, NOT A GLOBAL CONSTANT
----------------------------------------------------------------
A tool that reads the operator's own filesystem and a tool that relays an
arbitrary vendor's HTTP body have nothing in common risk-wise, so they do
not share a budget. Budgets are declared as *named tiers*:

* ``standard`` — the historical bound (2 000 chars / 20 items). This stays
  the DEFAULT for everything that does not declare otherwise, which is
  every third-party HTTP skill. Nothing about the injection/flooding
  defence is relaxed.
* ``feed``     — inbox/timeline style endpoints that are useless at 20 rows.
* ``workspace``— first-party local read/search/shell tools, sized to what
  the tool itself already computes (see ``test_result_budget.py``, which
  binds these numbers to ``coding_tools.MAX_OUTPUT`` /
  ``GREP_DEFAULT_HEAD_LIMIT`` / ``GLOB_DEFAULT_HEAD_LIMIT`` so raising a
  tool's own limit without raising its budget fails CI).

Tiers are **declared in the skill manifest** (``result_budget`` on the
manifest and/or on an individual endpoint), not hardcoded per skill_id in
this file. That keeps the numbers in one registry and the *policy* next to
the tool it describes.

TRUST CLAMP
-----------
A manifest is untrusted input: a marketplace skill could otherwise declare
``result_budget: "workspace"`` and flood the context with attacker-chosen
text. A declared tier is therefore only honoured for skills that ship in
``feral-core/skills/manifests/`` — i.e. manifests that are part of this
repository. Anything installed at runtime (``~/.feral/skills``) is clamped
to ``standard`` no matter what it declares. The check is derived by
scanning the shipped directory, so it does not go stale the way a
hardcoded allowlist would.

Note that the tier is resolved PER ENDPOINT, which matters inside a
first-party skill: ``coding_tools__read_file`` reads the operator's disk
and gets ``workspace``; ``coding_tools__web_fetch`` returns a stranger's
HTML through the same skill and stays on ``standard``.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("feral.skills.result_budget")


@dataclass(frozen=True)
class ResultBudget:
    """How much of one tool result may reach the model.

    ``max_result_chars`` bounds the serialized JSON that lands in the
    conversation history; the other four bound the structure itself so an
    adversarial payload cannot spend the whole budget on one pathological
    axis (a million-key dict, a 100-deep nest, ...).
    """

    name: str
    max_depth: int
    max_str_len: int
    max_list_len: int
    max_dict_keys: int
    max_result_chars: int


# The tier registry. `standard` MUST stay at the pre-2026.7.30 numbers:
# it is what every third-party HTTP skill gets, and the tight bound there
# is the original, legitimate reason the sanitizer exists.
BUILTIN_BUDGET_TIERS: dict[str, ResultBudget] = {
    "standard": ResultBudget(
        name="standard",
        max_depth=5,
        max_str_len=2_000,
        max_list_len=20,
        max_dict_keys=50,
        max_result_chars=2_000,
    ),
    # Inbox / timeline / message-history endpoints. Third-party content,
    # so strings stay short — but 20 rows is not an inbox, and the caller
    # asked for a page of rows on purpose.
    "feed": ResultBudget(
        name="feed",
        max_depth=6,
        max_str_len=4_000,
        max_list_len=50,
        max_dict_keys=50,
        max_result_chars=24_000,
    ),
    # First-party local read/search/shell. Sized so the tool's own limits
    # are the binding constraint, not this layer:
    #   coding_tools.MAX_OUTPUT              = 50_000  (bash, per stream)
    #   coding_tools.GREP_DEFAULT_HEAD_LIMIT =    250  (grep rows)
    #   coding_tools.GLOB_DEFAULT_HEAD_LIMIT =    100  (glob rows)
    # `max_result_chars` covers stdout + stderr + envelope for a worst-case
    # bash call (2 x MAX_OUTPUT) with headroom.
    "workspace": ResultBudget(
        name="workspace",
        max_depth=8,
        max_str_len=60_000,
        max_list_len=1_000,
        max_dict_keys=200,
        max_result_chars=120_000,
    ),
}

DEFAULT_TIER = "standard"
# Ceiling applied to anything whose manifest did not ship in this repo.
UNTRUSTED_TIER = "standard"

_tier_cache: Optional[dict[str, ResultBudget]] = None
_builtin_skill_ids: Optional[frozenset[str]] = None


def _settings_overrides() -> dict:
    """Operator overrides from ``settings.json`` (``skills.result_budgets``).

    Same mechanism as ``agents.max_tool_iterations``: an operator running
    on a small-context local model can shrink ``workspace`` without
    patching source. Shape:

        {"skills": {"result_budgets": {"workspace": {"max_str_len": 20000}}}}
    """
    try:
        from config.loader import load_settings
        settings = load_settings() or {}
    except ImportError:
        return {}
    raw = (settings.get("skills") or {}).get("result_budgets") or {}
    return raw if isinstance(raw, dict) else {}


def resolve_tiers() -> dict[str, ResultBudget]:
    """Tier table with operator overrides applied. Cached; call
    ``reset_budget_cache()`` after mutating settings (tests do)."""
    global _tier_cache
    if _tier_cache is not None:
        return _tier_cache

    tiers = dict(BUILTIN_BUDGET_TIERS)
    for tier_name, overrides in _settings_overrides().items():
        base = tiers.get(tier_name)
        if base is None or not isinstance(overrides, dict):
            logger.warning("Ignoring result-budget override for unknown tier %r", tier_name)
            continue
        fields = {}
        for key, value in overrides.items():
            if key in ("max_depth", "max_str_len", "max_list_len",
                       "max_dict_keys", "max_result_chars"):
                try:
                    fields[key] = max(1, int(value))
                except (TypeError, ValueError):
                    logger.warning(
                        "Ignoring non-integer result-budget override %s.%s=%r",
                        tier_name, key, value,
                    )
        if fields:
            tiers[tier_name] = replace(base, **fields)

    _tier_cache = tiers
    return tiers


def reset_budget_cache() -> None:
    """Drop the memoised tier table and built-in skill set."""
    global _tier_cache, _builtin_skill_ids
    _tier_cache = None
    _builtin_skill_ids = None


def forced_tier() -> Optional[str]:
    """Env escape hatch for ops debugging a context blow-up in the field:
    ``FERAL_RESULT_BUDGET_TIER=standard`` pins every tool back to the
    pre-2026.7.30 behaviour without editing settings.json or source."""
    tier = os.environ.get("FERAL_RESULT_BUDGET_TIER", "").strip()
    return tier or None


def get_budget(tier: Optional[str]) -> ResultBudget:
    """Look a tier up by name; unknown/blank names fall back to the default."""
    tiers = resolve_tiers()
    pin = forced_tier()
    if pin:
        if pin in tiers:
            return tiers[pin]
        logger.warning("Ignoring unknown FERAL_RESULT_BUDGET_TIER=%r", pin)
    if tier and tier in tiers:
        return tiers[tier]
    if tier:
        logger.warning("Unknown result_budget tier %r — using %r", tier, DEFAULT_TIER)
    return tiers[DEFAULT_TIER]


def builtin_skill_ids() -> frozenset[str]:
    """skill_ids whose manifest ships in this repo.

    Derived by reading ``skills/manifests/*.json`` rather than kept as a
    literal list, so adding a manifest does not require touching this file.
    """
    global _builtin_skill_ids
    if _builtin_skill_ids is not None:
        return _builtin_skill_ids

    ids: set[str] = set()
    manifests_dir = Path(__file__).parent / "manifests"
    if manifests_dir.is_dir():
        for path in sorted(manifests_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError) as exc:
                logger.warning("Unreadable built-in manifest %s: %s", path, exc)
                continue
            skill_id = data.get("skill_id")
            if skill_id:
                ids.add(str(skill_id))

    _builtin_skill_ids = frozenset(ids)
    return _builtin_skill_ids


def budget_for(
    skill_id: str,
    endpoint_id: str = "",
    manifest: Any = None,
) -> ResultBudget:
    """Resolve the budget for one skill endpoint.

    Precedence: endpoint declaration → manifest declaration → default.
    Any declaration coming from a manifest that does not ship in this repo
    is ignored in favour of ``UNTRUSTED_TIER`` (see module docstring).
    """
    declared: Optional[str] = None
    if manifest is not None:
        if endpoint_id:
            for endpoint in getattr(manifest, "endpoints", None) or []:
                if getattr(endpoint, "id", None) == endpoint_id:
                    declared = getattr(endpoint, "result_budget", None)
                    break
        if not declared:
            declared = getattr(manifest, "result_budget", None)

    if not declared:
        return get_budget(DEFAULT_TIER)

    if skill_id not in builtin_skill_ids():
        logger.info(
            "Skill %r is not a built-in manifest; ignoring its declared "
            "result_budget=%r and clamping to %r",
            skill_id, declared, UNTRUSTED_TIER,
        )
        return get_budget(UNTRUSTED_TIER)

    return get_budget(declared)


def budget_for_tool(tool_name: str, registry: Any = None) -> ResultBudget:
    """Resolve the budget from an LLM-facing ``skill__endpoint`` tool name.

    Used on the conversation-history side, which only has the tool name.
    ``registry`` is a ``SkillRegistry``; without one (or for ``mcp_*`` /
    ``daemon_*`` names that have no manifest) the default tier applies.
    """
    skill_id, _, endpoint_id = (tool_name or "").partition("__")
    if not skill_id or not endpoint_id:
        return get_budget(DEFAULT_TIER)
    manifest = None
    if registry is not None:
        manifest = getattr(registry, "skills", {}).get(skill_id)
    return budget_for(skill_id, endpoint_id, manifest)


class TruncationReport:
    """Accumulates what ``clamp`` removed so the caller can tell the model.

    A silent cut is the worst outcome: the model assumes it saw the whole
    file and answers confidently about code it never read.
    """

    def __init__(self) -> None:
        self.chars_dropped = 0
        self.items_dropped = 0
        self.keys_dropped = 0
        self.depth_clipped = 0

    def __bool__(self) -> bool:
        return bool(
            self.chars_dropped or self.items_dropped
            or self.keys_dropped or self.depth_clipped
        )

    def summary(self) -> str:
        parts = []
        if self.chars_dropped:
            parts.append(f"{self.chars_dropped} chars of text")
        if self.items_dropped:
            parts.append(f"{self.items_dropped} list items")
        if self.keys_dropped:
            parts.append(f"{self.keys_dropped} object keys")
        if self.depth_clipped:
            parts.append(f"{self.depth_clipped} nested branches")
        if not parts:
            return ""
        return (
            "This tool result was truncated to fit the "
            f"'{self._tier}' budget: dropped " + ", ".join(parts)
            + ". You have NOT seen the whole result — re-run the tool with "
            "offset/limit (or a narrower query) to see the rest."
        )

    _tier = DEFAULT_TIER


def clamp(
    data: Any,
    budget: ResultBudget,
    report: Optional[TruncationReport] = None,
    _depth: Optional[int] = None,
) -> Any:
    """Bound a decoded tool result to ``budget``.

    This is the structural half of the defence and it is NOT optional: it
    caps nesting depth, dict width, list length and string length so an
    unbounded or adversarial payload cannot exhaust the context regardless
    of which tier is in play. Raising a tier raises the numbers; it never
    removes the bound.
    """
    if report is not None:
        report._tier = budget.name
    depth = budget.max_depth if _depth is None else _depth

    if depth <= 0:
        if report is not None:
            report.depth_clipped += 1
        return "[truncated: nesting deeper than the result budget allows]"

    if isinstance(data, dict):
        items = list(data.items())
        if len(items) > budget.max_dict_keys and report is not None:
            report.keys_dropped += len(items) - budget.max_dict_keys
        return {
            key: clamp(value, budget, report, depth - 1)
            for key, value in items[: budget.max_dict_keys]
        }

    if isinstance(data, list):
        if len(data) > budget.max_list_len and report is not None:
            report.items_dropped += len(data) - budget.max_list_len
        return [
            clamp(item, budget, report, depth - 1)
            for item in data[: budget.max_list_len]
        ]

    if isinstance(data, str):
        if len(data) > budget.max_str_len:
            dropped = len(data) - budget.max_str_len
            if report is not None:
                report.chars_dropped += dropped
            return (
                data[: budget.max_str_len]
                + f"\n...[truncated {dropped} of {len(data)} chars — "
                "re-run with offset/limit to read the rest]"
            )
        return data

    return data


# ── conversation-history serialization ────────────────────────────────

_HINT_KEYS = (
    "pagination", "next_offset", "truncated", "total", "total_lines",
    "total_files", "offset", "limit",
)


def extract_pagination_hint(data: Any, _depth: int = 4) -> dict:
    """Pull the pagination breadcrumbs the tool already produced.

    ``skills/impl/coding_tools.py`` emits ``truncated`` /
    ``pagination{limit,offset,next_offset}`` / ``total_lines`` precisely so
    the caller can page. Those keys are worthless if the truncation layer
    above drops them, so we lift them to the top of the truncation notice.
    """
    found: dict = {}
    if _depth <= 0:
        return found
    if isinstance(data, dict):
        for key, value in data.items():
            if key in _HINT_KEYS and not isinstance(value, (dict, list)):
                found.setdefault(key, value)
            elif key == "pagination" and isinstance(value, dict):
                found.setdefault(key, value)
            elif isinstance(value, (dict, list)):
                for hint_key, hint_value in extract_pagination_hint(value, _depth - 1).items():
                    found.setdefault(hint_key, hint_value)
    elif isinstance(data, list):
        for item in data[:5]:
            for hint_key, hint_value in extract_pagination_hint(item, _depth - 1).items():
                found.setdefault(hint_key, hint_value)
    return found


def _dumps(value: Any) -> str:
    return json.dumps(value, default=str)


def serialize_tool_result(
    tool_name: str,
    result_data: Any,
    *,
    registry: Any = None,
    budget: Optional[ResultBudget] = None,
) -> str:
    """Serialize one tool result for the ``role: "tool"`` history message.

    Replaces ``json.dumps(result_data, default=str)[:2000]``, which had two
    independent failure modes:

      * it cut mid-token, so the model was routinely handed JSON that does
        not parse (``{"data": {"content": "def foo(ba``), and
      * it said nothing about the cut, so the model treated a 30-line
        window as the whole file.

    Guarantees here: the returned string always parses as JSON, is bounded
    by the tool's own budget, and — when anything was removed — carries an
    explicit ``_truncated`` marker plus whatever pagination hint the tool
    produced.
    """
    if budget is None:
        budget = budget_for_tool(tool_name, registry)

    blob = _dumps(result_data)
    if len(blob) <= budget.max_result_chars:
        return blob

    original_chars = len(blob)
    hint = extract_pagination_hint(result_data)

    # Shrink structurally rather than slicing bytes: halve the string and
    # list allowances until the serialized form fits. Every intermediate is
    # a real Python object, so the output is always valid JSON.
    str_len = budget.max_str_len
    list_len = budget.max_list_len
    shrunk = None
    for _ in range(24):
        str_len = max(200, str_len // 2)
        list_len = max(3, list_len // 2)
        candidate = clamp(
            result_data,
            replace(budget, max_str_len=str_len, max_list_len=list_len),
        )
        envelope = _annotate(candidate, budget, original_chars, hint)
        blob = _dumps(envelope)
        if len(blob) <= budget.max_result_chars:
            return blob
        shrunk = blob
        if str_len == 200 and list_len == 3:
            break

    # Last resort: a bounded, still-valid-JSON preview. Reached only when
    # the payload is pathological (e.g. tens of thousands of tiny keys).
    overhead = 600
    preview_len = max(200, budget.max_result_chars - overhead)
    return _dumps({
        "_truncated": True,
        "_truncation_note": (
            f"Tool result was {original_chars} chars, far over the "
            f"'{budget.name}' budget of {budget.max_result_chars}, and could "
            "not be shrunk structurally. Only a prefix is shown below and it "
            "is NOT valid JSON on its own. Re-run the tool with a narrower "
            "query or with offset/limit."
        ),
        "_pagination_hint": hint or None,
        "_preview": (shrunk or blob)[:preview_len],
    })


def _annotate(
    candidate: Any,
    budget: ResultBudget,
    original_chars: int,
    hint: dict,
) -> Any:
    """Attach the truncation marker without disturbing the result's shape.

    The model has been told what ``success`` / ``data`` / ``error`` mean, so
    we add sibling keys rather than nesting the whole envelope under a new
    one.
    """
    note = (
        f"Tool result exceeded the '{budget.name}' budget "
        f"({budget.max_result_chars} chars) and was shrunk from "
        f"{original_chars} chars. You have NOT seen the whole result — "
        "re-run with offset/limit (or a narrower query) to see the rest."
    )
    if isinstance(candidate, dict):
        annotated = dict(candidate)
        annotated["_truncated"] = True
        annotated["_truncation_note"] = note
        if hint:
            annotated["_pagination_hint"] = hint
        return annotated
    return {
        "_truncated": True,
        "_truncation_note": note,
        "_pagination_hint": hint or None,
        "data": candidate,
    }
