"""Bounded per-request tool retrieval for HTTP local-model adapters.

Budgets are actual serialized UTF-8 bytes and definition counts, NOT tokenizer
counts or claims about model context capacity. Registry/execution policy is
unchanged; the next call can retrieve other already-authorized definitions.
"""
from __future__ import annotations

import json
import re

DISCOVERY = ("self_introspection__describe_skill", "self_introspection__list_capabilities")
MAX_TOOLS = 12
MAX_SCHEMA_BYTES = 12_000
_WORDS = re.compile(r"[a-z][a-z0-9_]{2,}")
_STOP = {"the", "this", "that", "with", "from", "and", "for", "you", "can", "please", "what", "how", "are", "have", "does", "was", "your", "about", "want", "need"}


def _name(tool):
    return (tool.get("function") or {}).get("name", "")


def retrieve_local_tools(messages, tools, force_tool=None, *, max_tools=MAX_TOOLS, max_schema_bytes=MAX_SCHEMA_BYTES):
    """Return new wire messages/definitions; never mutate policy or latest input.

    Explicit/forced definitions and discovery are mandatory, failing visibly if
    they cannot fit. Remaining slots are ranked by current user wording and a
    preceding describe_skill request. No schema parameters are abbreviated.
    """
    if not tools:
        return messages, tools
    if max_tools < 1 or max_schema_bytes < 2:
        raise ValueError("Local tool retrieval budget is invalid")
    definitions = {}
    for tool in tools:
        name = _name(tool)
        if not isinstance(name, str) or not name:
            raise ValueError("Local tool definition name is invalid")
        if name in definitions:
            raise ValueError("Local tool definitions have duplicate names")
        definitions[name] = tool
    query = next((row.get("content", "") for row in reversed(messages) if row.get("role") == "user"), "")
    query = query if isinstance(query, str) else json.dumps(query, ensure_ascii=False)
    terms = set(_WORDS.findall(query.lower())) - _STOP
    requested_skills = set()
    for row in messages[-8:]:
        for call in row.get("tool_calls", []) or []:
            fn = call.get("function") or {}
            if fn.get("name") == DISCOVERY[0]:
                try:
                    args = fn.get("arguments", "{}")
                    args = json.loads(args) if isinstance(args, str) else args
                    skill = args.get("skill_id")
                    if isinstance(skill, str) and re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", skill):
                        requested_skills.add(skill)
                except (ValueError, TypeError, AttributeError):
                    pass
    mandatory = [name for name in DISCOVERY if name in definitions]
    if force_tool:
        if force_tool not in definitions:
            raise ValueError("The forced local tool is not available")
        mandatory.insert(0, force_tool)
    mandatory += [name for name in definitions if name.lower() in query.lower()]
    mandatory = list(dict.fromkeys(mandatory))
    if len(mandatory) > max_tools:
        raise ValueError("Explicit local tool definitions exceed the bounded retrieval limit")

    def wire_bytes(selected):
        return len(json.dumps(selected, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))

    selected = [definitions[name] for name in mandatory]
    if wire_bytes(selected) > max_schema_bytes:
        raise ValueError("Explicit local tool schemas exceed the bounded retrieval size")
    ranked = []
    for name, tool in definitions.items():
        if name in mandatory:
            continue
        function = tool.get("function") or {}
        words = set(_WORDS.findall((name.replace("_", " ") + " " + str(function.get("description", ""))).lower()))
        score = len(terms & words)
        if name.split("__", 1)[0] in requested_skills:
            score += 100
        if score:
            ranked.append((-score, name))
    for _, name in sorted(ranked):
        if len(selected) == max_tools:
            break
        candidate = selected + [definitions[name]]
        if wire_bytes(candidate) <= max_schema_bytes:
            selected = candidate
    if len(selected) == len(tools):
        return messages, tools
    notice = ("Local tool discovery: this request contains " + str(len(selected)) + " of " + str(len(tools))
              + " available definitions under a schema byte/count limit (not a measured token count). "
              "A missing definition does not mean a capability is unavailable. Use the provided "
              "self_introspection discovery/describe_skill tools when available to inspect a skill; "
              "the next request retrieves matching authorized definitions. Do not claim an action succeeded without its actual tool result.")
    copied = [dict(row) for row in messages]
    for row in copied:
        if row.get("role") == "system" and isinstance(row.get("content"), str):
            row["content"] += "\n\n" + notice
            break
    else:
        copied.insert(0, {"role": "system", "content": notice})
    return copied, selected


MAX_REQUEST_BYTES = 32_000

def validate_local_request(body, provider, *, max_bytes=MAX_REQUEST_BYTES):
    """Bound complete local chat input before transport, without truncating it.

    This is a conservative serialized byte ceiling, not a tokenizer/context
    fit claim. Arbitrary oversized identity/history/latest input is refused,
    never silently rewritten or sent for provider truncation.
    """
    if provider not in ("ollama", "lmstudio"):
        return
    import logging
    rows = body.get("messages", [])
    def size(value):
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
    system_bytes = size([row for row in rows if row.get("role") == "system"])
    history_bytes = size([row for row in rows if row.get("role") != "system"])
    schema_bytes = size(body.get("tools", []))
    total = size({"messages": rows, "tools": body.get("tools", [])})
    logging.getLogger("feral.llm").info(
        "Local request byte budget: system=%d history=%d schemas=%d total=%d limit=%d tokenizer_verified=false",
        system_bytes, history_bytes, schema_bytes, total, max_bytes,
    )
    if total > max_bytes:
        raise ValueError("Local request exceeds the bounded input byte budget; no request was sent. "
                         "Start a shorter conversation or reduce attached context. Identity/policy and latest input were preserved. "
                         "Model tokenizer/context capacity was not measured.")
