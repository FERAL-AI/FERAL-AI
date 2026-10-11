"""Compact real Accessibility records into a contiguous, actionable wire page."""
from __future__ import annotations

import json
import re
from typing import Any, Callable, cast


def compact_ax_snapshot(data: Any, max_chars: int,
                        redact_text: Callable[[str], str]) -> dict | None:
    if type(data) is not dict or type(data.get("nodes")) is not list:
        return None
    nodes = data["nodes"]
    offset, total, returned = (data.get(key) for key in ("offset", "total_matched", "returned"))
    if any(type(value) is not int or value < 0 for value in (offset, total, returned)):
        return None
    offset, total, returned = cast(int, offset), cast(int, total), cast(int, returned)
    if (not nodes or len(nodes) > 1000
            or returned != len(nodes) or total < offset + returned
            or type(data.get("truncated")) is not bool
            or data.get("next_offset") != (offset + returned if data["truncated"] else None)
            or data["truncated"] != (offset + returned < total)):
        return None
    limits = data.get("limits_hit", [])
    if (type(limits) is not list or len(limits) > 3
            or any(item not in {"timeout", "visit_cap", "max_depth"} for item in limits)):
        return None
    metadata: dict[str, Any] = {"offset": offset, "total_matched": total, "source_returned": returned,
                "limits_hit": list(limits), "nodes_omitted": True}
    for key in ("app", "bundle_id", "filter"):
        value = data.get(key)
        if isinstance(value, str):
            metadata[key] = redact_text(value[:376])[:120]
    lines: list[str] = []
    seen: set[str] = set()
    page: dict | None = None
    for node in nodes:
        if type(node) is not dict:
            break
        ref, role = node.get("ref"), node.get("role")
        if (not isinstance(ref, str) or re.fullmatch(r"ax[0-9]{1,16}", ref) is None
                or ref in seen or not isinstance(role, str)
                or re.fullmatch(r"AX[A-Za-z0-9]{1,80}", role) is None):
            break
        label = node.get("label", "")
        if not isinstance(label, str):
            break
        secure = node.get("secure") is True or role == "AXSecureTextField"
        label = "(secure value withheld)" if secure else redact_text(label[:376])[:120]
        line = f"[{ref}] {role} " + json.dumps(label, ensure_ascii=False)
        if node.get("enabled") is False:
            line += " (disabled)"
        count = len(lines) + 1
        candidate = {**metadata, "tree": "\n".join([*lines, line]), "returned": count,
                     "next_offset": offset + count if offset + count < total else None,
                     "truncated": offset + count < total}
        if len(json.dumps(candidate, allow_nan=False)) > max_chars:
            break
        lines.append(line)
        seen.add(ref)
        page = candidate
    return page
