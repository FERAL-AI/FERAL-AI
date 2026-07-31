"""User-facing labels for tool activity.

Tool IDs are stable wire identifiers. Chat and voice surfaces should show
intent-level labels while keeping raw identifiers available only in expanded
technical details.
"""

from __future__ import annotations


def split_tool_name(
    tool_name: str = "",
    *,
    skill_id: str = "",
    endpoint_id: str = "",
) -> tuple[str, str]:
    """Return ``(skill_id, endpoint_id)`` from either explicit fields or a tool id."""
    if skill_id or endpoint_id:
        return str(skill_id or "").strip(), str(endpoint_id or "").strip()
    raw = str(tool_name or "").strip()
    parts = raw.split("__", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return raw, ""


def _humanize(value: str) -> str:
    text = str(value or "").replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else "Work"


def friendly_tool_label(
    tool_name: str = "",
    *,
    skill_id: str = "",
    endpoint_id: str = "",
) -> str:
    """Short label for compact UI tool traces."""
    skill, endpoint = split_tool_name(
        tool_name,
        skill_id=skill_id,
        endpoint_id=endpoint_id,
    )
    if skill == "web_search":
        return "Search web"
    if skill == "weather_current":
        return "Check weather"
    if skill == "browser":
        return f"Browser: {_humanize(endpoint)}" if endpoint else "Use browser"
    # ``computer_use`` was consolidated into ``coding_tools``; keep the old
    # id so any daemon or marketplace skill still using it keeps its label.
    if skill in {"coding_tools", "computer_use"}:
        if endpoint == "bash":
            return "Run local command"
        if endpoint == "write_file":
            return "Write file"
        if endpoint == "read_file":
            return "Read file"
        if endpoint == "edit_file":
            return "Edit file"
        if endpoint in {"grep_search", "glob_search"}:
            return "Search files"
    if skill in {"gui_computer_use", "agentic_computer_use", "desktop_automation"}:
        return "Use computer"
    if endpoint:
        return _humanize(endpoint)
    if skill:
        return _humanize(skill)
    return "Use tool"


def tool_feedback_text(tool_name: str) -> str:
    """Natural spoken feedback while a tool is executing."""
    skill, endpoint = split_tool_name(tool_name)
    if not endpoint:
        return "Working on that now."
    if skill == "web_search":
        return "Searching the web now."
    if skill == "weather_current":
        return "Checking the weather."
    if skill == "browser":
        return f"Using the browser to {endpoint.replace('_', ' ')}."
    if skill in {"coding_tools", "computer_use"} and endpoint == "bash":
        return "Running a command on your computer."
    return f"Running {endpoint.replace('_', ' ')}."
