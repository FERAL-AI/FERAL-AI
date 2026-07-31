"""Per-turn attribution: which model answered, and what the turn cost.

Lives in its own module because both the single-agent orchestrator and the
multi-agent orchestrator need it, and ``agents.orchestrator`` imports
``agents.multi_agent`` (lazily, in ``_init_multi_agent``). Putting the
helpers in either one would make the other import its importer.

The contract these functions exist to protect: **an unreported cost is
reported as unknown, never as zero.** Providers differ in whether they
send usage at all, so a turn with no numbers is a normal outcome, not an
error. Rendering it as ``0 tokens`` would look exactly like a measurement
and would quietly make the meter untrustworthy in the one case where it is
already blind. Callers distinguish the two by whether the dict is empty.
"""

from __future__ import annotations

from typing import Any


def model_of_llm_response(response: Any) -> str:
    """The model that actually served one LLM call.

    Providers echo the resolved model back, which is not always the one
    that was asked for: the failover chain can hop, and OpenAI expands
    aliases to dated snapshots. That echoed value is what the user needs
    to see, so it is preferred over the configured name everywhere.
    """
    if not isinstance(response, dict):
        return ""
    return str(response.get("model") or "")


def accumulate_turn_usage(total: dict, response: Any) -> dict:
    """Fold one LLM call's token usage into a running per-turn total.

    Accepts both provider dialects (``prompt_tokens``/``completion_tokens``
    from chat-completions, ``input_tokens``/``output_tokens`` from the
    Responses API) and normalises to the latter.

    A call that reports no usage contributes nothing and, critically, does
    not create the keys, so an empty ``total`` still means "nothing was
    reported" after any number of such calls.
    """
    if not isinstance(response, dict):
        return total
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return total
    inp = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    out = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    if not inp and not out:
        return total
    total["input_tokens"] = total.get("input_tokens", 0) + inp
    total["output_tokens"] = total.get("output_tokens", 0) + out
    # Recomputed from the parts rather than summing the provider's own
    # ``total_tokens``: on reasoning models that field sometimes includes
    # reasoning tokens already counted in ``output_tokens``, so trusting
    # it would double-count them.
    total["total_tokens"] = total["input_tokens"] + total["output_tokens"]
    return total


def merge_turn_usage(total: dict, other: dict) -> dict:
    """Fold an already-accumulated total into another one.

    Used where sub-agents each return their own tally (the multi-agent
    path runs workers in parallel and merges their answers, and the user
    pays for every worker, not just the one whose text is shown).
    """
    if not isinstance(other, dict) or not other:
        return total
    return accumulate_turn_usage(total, {"usage": other})
