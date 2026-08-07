"""
FERAL Dangerous Tool Registry
Centralized policy for which tools are restricted on which surfaces.

Pattern: central deny lists per execution surface.

Usage
-----
1. Gate execution: call ``is_tool_allowed(name, surface)`` before dispatch; if False, refuse.
2. UX / policy: ``get_danger_level`` and ``requires_approval`` drive prompts and exec-approval
   flows (see ``security.exec_approvals``).
3. Extend ``TOOL_DANGER_MAP`` when new tools ship; keep names aligned with MCP / internal registry
   strings so policy stays a single source of truth.

Surfaces
--------
- ``http_api``: remote, minimally trusted — block shell, Docker, and arbitrary JS in browser.
- ``websocket``: interactive channel — still block host-level Docker exec.
- ``local_cli``: operator-controlled — no static denies (policy can still require approval).
- ``cron``: scheduled routines, nobody present. http_api's denies plus arbitrary
  code eval. Device/robot skills stay allowed; that is what routines are for.

Naming compatibility
--------------------
Two naming conventions coexist in the codebase:

* Legacy / external (MCP, providers): dotted ``skill.endpoint`` (e.g. ``shell.exec``).
* Internal skills / LLM tool ids: double-underscore ``skill__endpoint``
  (e.g. ``desktop_control__shell_command``).

Deny-list matching normalises a tool name into multiple candidate forms so a
single policy entry catches both shapes — adding ``shell.exec`` denies
``shell__exec`` too, and vice versa. Bare endpoint names are NOT auto-matched
to keep the policy explicit; if you need that you must add the bare name.
"""

from __future__ import annotations

from enum import Enum
from typing import FrozenSet, Iterable, Mapping, Optional


class DangerLevel(str, Enum):
    """Relative risk of invoking a tool or endpoint."""

    SAFE = "safe"
    WARN = "warn"
    CRITICAL = "critical"


# Explicit map for known tools; anything not listed is treated as SAFE.
TOOL_DANGER_MAP: dict[str, DangerLevel] = {
    # CRITICAL — arbitrary code, container escape surface, or destructive FS
    "system.run": DangerLevel.CRITICAL,
    "browser.evaluate": DangerLevel.CRITICAL,
    "docker.exec": DangerLevel.CRITICAL,
    "fs.delete": DangerLevel.CRITICAL,
    "fs.remove": DangerLevel.CRITICAL,
    "filesystem.delete": DangerLevel.CRITICAL,
    "file.delete": DangerLevel.CRITICAL,
    "shell.exec": DangerLevel.CRITICAL,
    "process.spawn": DangerLevel.CRITICAL,
    # Modern skill__endpoint equivalents — explicit so danger level reads true
    # even when caller passes the LLM-facing tool id directly.
    "desktop_control__shell_command": DangerLevel.CRITICAL,
    "desktop_control__shell": DangerLevel.CRITICAL,
    "computer_use__bash": DangerLevel.CRITICAL,
    "code_interpreter__execute": DangerLevel.CRITICAL,
    # `coding_tools` is the canonical shell + filesystem surface; the
    # duplicate `computer_use` manifest and impl were removed. The
    # `computer_use__*` ids above stay listed so a third-party manifest that
    # still uses the old skill id cannot pick up a weaker classification.
    "coding_tools__bash": DangerLevel.CRITICAL,
    "coding_tools__write_file": DangerLevel.WARN,
    "coding_tools__edit_file": DangerLevel.WARN,
    # The VLM-driven autonomous loop emits `shell` actions in addition to
    # mouse/keyboard. Treat the whole task entry point as CRITICAL so the
    # http_api gateway refuses to drive it from an untrusted surface.
    "agentic_computer_use__execute_task": DangerLevel.CRITICAL,
    # WARN — sensitive automation / network / generation
    "browser.navigate": DangerLevel.WARN,
    "browser.click": DangerLevel.WARN,
    "web_fetch": DangerLevel.WARN,
    "mcp.web_fetch": DangerLevel.WARN,
    "image.generate": DangerLevel.WARN,
    "images.generate": DangerLevel.WARN,
    "generate_image": DangerLevel.WARN,
}

# Per-surface deny: if a tool appears here, it must not run on that surface
# regardless of danger level handling elsewhere.
SURFACE_DENY_LISTS: dict[str, set[str]] = {
    "http_api": {
        # Legacy dotted form — kept for backward-compat with external/MCP callers.
        "system.run",
        "docker.exec",
        "browser.evaluate",
        "shell.exec",
        "process.spawn",
        "fs.delete",
        "fs.remove",
        "filesystem.delete",
        "file.delete",
        # Modern internal tool ids that bypass the dotted lookup. Listing these
        # explicitly means matching does not depend on the candidate-form
        # transform alone — defence in depth against future renames.
        "desktop_control__shell_command",
        "desktop_control__shell",
        "computer_use__bash",
        "code_interpreter__execute",
        # `coding_tools` mirrors `computer_use` — explicit deny so the alias
        # cannot bypass http_api enforcement after the canonical-execution
        # consolidation.
        "coding_tools__bash",
        "coding_tools__write_file",
        "coding_tools__edit_file",
        # VLM-driven autonomous loop entry point — refuses on http_api so
        # remote surfaces can't kick off a desktop-vision agent without
        # operator presence.
        "agentic_computer_use__execute_task",
    },
    "websocket": {
        "docker.exec",
    },
    "local_cli": set(),
    # `cron` is the scheduled-routine surface: `execute_routine_job` in
    # api/server.py pre-flights a routine's skill+endpoint through
    # `resolve_policy(..., surface="cron")`, which calls `is_tool_allowed`
    # first.
    #
    # This key did not exist. `is_tool_allowed` returns True for any surface
    # with no deny entry (see its docstring: "Unknown surfaces are treated as
    # unrestricted"), so the cron pre-flight allowed EVERYTHING:
    # `is_tool_allowed("shell__exec", "cron")` was True, as was
    # `agentic_computer_use__execute_task`. A routine could therefore run
    # tools that are hard-denied on every other remote surface, at 3am, with
    # nobody present to see it happen. An absent key fails open silently, so
    # nothing anywhere logged a refusal.
    #
    # The floor is http_api's list: if a tool is too dangerous for a remote
    # HTTP caller who is at least sitting at the keyboard, it is worse on an
    # unattended timer where no one can intervene mid-run.
    #
    # Deliberately NOT denied: device/robot skills (`cutebot__*`,
    # `smart_home*`), calendar/health reads, and messaging. Scheduling those
    # is the entire point of routines, and the operator's nightly CuteBot
    # jobs (line-follow, spin, lights) depend on them. Per-tool risk on that
    # path is handled by the CONFIRM tier plus the routine's explicit
    # `auto_confirm`, not by surface deny.
    "cron": {
        # Legacy dotted form, kept for backward-compat with external/MCP callers.
        "system.run",
        "docker.exec",
        "browser.evaluate",
        "shell.exec",
        "process.spawn",
        "fs.delete",
        "fs.remove",
        "filesystem.delete",
        "file.delete",
        # Modern internal tool ids that bypass the dotted lookup. Listed
        # explicitly so matching does not depend on the candidate-form
        # transform alone (defence in depth against future renames).
        "desktop_control__shell_command",
        "desktop_control__shell",
        "computer_use__bash",
        "computer_use__write_file",
        "computer_use__edit_file",
        "code_interpreter__execute",
        "coding_tools__bash",
        "coding_tools__write_file",
        "coding_tools__edit_file",
        # VLM-driven autonomous loop: it emits `shell` actions and drives the
        # desktop from screenshots. Nothing about that is safe to start on a
        # schedule with no operator watching the screen.
        "agentic_computer_use__execute_task",
        # The registered code_interpreter endpoints are `run_python` /
        # `run_node`; `code_interpreter__execute` above matches no endpoint in
        # skills/manifests/code_interpreter.json, so denying only that id
        # would have left real arbitrary-code eval reachable from cron.
        "code_interpreter__run_python",
        "code_interpreter__run_node",
        # `workspace_scripts.run` writes a script to
        # ~/.feral/workspace/scripts and executes it; `rerun` replays a saved
        # one with caller-supplied args. Same arbitrary-code-eval class as
        # code_interpreter, so it is denied on the unattended surface too.
        "workspace_scripts__run",
        "workspace_scripts__rerun",
    },
    # PR 11: MCP is a *remote-callable* surface. External MCP clients
    # (Claude Desktop, Cursor, …) can pull FERAL's skill list and invoke
    # any tool we publish. Without surface gating, projecting all skills
    # would smuggle CRITICAL shell tools out of the operator's machine.
    # MCP inherits http_api's strict deny list as a floor; additional
    # MCP-only restrictions can be added here without disturbing
    # http_api callers.
    "mcp": {
        "system.run",
        "docker.exec",
        "browser.evaluate",
        "shell.exec",
        "process.spawn",
        "fs.delete",
        "fs.remove",
        "filesystem.delete",
        "file.delete",
        "desktop_control__shell_command",
        "desktop_control__shell",
        "computer_use__bash",
        "computer_use__write_file",
        "computer_use__edit_file",
        "code_interpreter__execute",
        "coding_tools__bash",
        "coding_tools__write_file",
        "coding_tools__edit_file",
        "agentic_computer_use__execute_task",
    },
    # Phase 1 (audit-r10 overhaul) — `brain_host` is the operator's own
    # Mac that hosts the FERAL brain. iOS chat that targets the brain
    # explicitly (`device_target == "brain"`) gets the same trust
    # envelope as the operator at the local CLI: full desktop control,
    # full shell, full agentic loop. Only the truly destructive
    # primitives stay denied; everything else honors the per-tool
    # CONFIRM tier via the autonomy mode, not surface deny.
    "brain_host": {
        "system.run",
        "docker.exec",
        "shell.exec",
        "process.spawn",
        "fs.delete",
        "fs.remove",
        "filesystem.delete",
        "file.delete",
    },
    # Phase 1 — `phone_actuator` is the surface when the brain wants the
    # PHONE to execute (CallKit / MusicKit / Intents / Location etc.,
    # landing in Phase 4 as `phone.*` skills). Mac-only tools have no
    # meaning here: the LLM should never try `desktop_control__*` /
    # `computer_use__*` / `agentic_computer_use__*` when `device_target
    # == "phone"`. Hard-deny those so the LLM is forced toward the
    # correct `phone.*` action vocabulary.
    "phone_actuator": {
        "system.run",
        "docker.exec",
        "shell.exec",
        "process.spawn",
        "fs.delete",
        "fs.remove",
        "filesystem.delete",
        "file.delete",
        # Mac-host tools — meaningless on the phone surface.
        "desktop_control__shell_command",
        "desktop_control__shell",
        "desktop_control__open_app",
        "desktop_control__screenshot",
        "desktop_control__system_info",
        "desktop_control__set_volume",
        "computer_use__bash",
        "computer_use__write_file",
        "computer_use__edit_file",
        "computer_use__read_file",
        "code_interpreter__execute",
        "coding_tools__bash",
        "coding_tools__write_file",
        "coding_tools__edit_file",
        "coding_tools__read_file",
        "agentic_computer_use__execute_task",
        "gui_computer_use__screenshot",
        "gui_computer_use__mouse_click",
        "gui_computer_use__type_text",
        "desktop_automation__click_screen",
        "desktop_automation__type_text",
    },
}

# Frozen snapshots for introspection / tests (optional).
SURFACE_DENY_LISTS_FROZEN: dict[str, FrozenSet[str]] = {
    k: frozenset(v) for k, v in SURFACE_DENY_LISTS.items()
}


# Map ``handle_command`` context["source"] values to the matching execution
# surface. Anything unknown / missing is conservatively treated as websocket
# (interactive operator channel) which preserves the prior default behaviour.
#
# Phase 1 note (audit-r10 overhaul): `phone_surface` historically mapped to
# `http_api` unconditionally — that hard-deny was the root cause of the
# operator's "iOS chat says no access to my Mac" complaint. The mapping
# below is now the DEFAULT only when `device_target` is missing/auto;
# `resolve_surface_from_context` consults `device_target` first.
_SOURCE_TO_SURFACE: dict[str, str] = {
    "webhook": "http_api",
    "phone_surface": "http_api",
    "channel": "http_api",
    "cron": "http_api",
    "proactive": "http_api",
    "http_api": "http_api",
    "rest": "http_api",
    "voice": "websocket",
    "voice_text": "websocket",
    "voice_chained": "websocket",
    "voice_realtime": "websocket",
    "node_text": "websocket",
    "gesture": "websocket",
    "vision_ask": "websocket",
    "websocket": "websocket",
    "ws": "websocket",
    "cli": "local_cli",
    "local_cli": "local_cli",
    "operator_cli": "local_cli",
}


# Phase 1 — device_target → surface mapping. When the iOS chat (or any
# client) tells the brain WHERE the requested action should run, that
# overrides the source→surface fallback. Result:
#   - `device_target == "brain"`  →  `brain_host` (Mac tools allowed)
#   - `device_target == "phone"`  →  `phone_actuator` (iOS skills only)
#   - `device_target == "glasses"` → `phone_actuator` (glasses are
#                                    bridged through the phone, so
#                                    same security envelope as phone)
#   - `device_target == "auto"` / None → fall through to source→surface
#                                    (today's behavior, conservative)
_DEVICE_TARGET_TO_SURFACE: dict[str, str] = {
    "brain": "brain_host",
    "phone": "phone_actuator",
    "glasses": "phone_actuator",
}


def known_surfaces() -> tuple[str, ...]:
    """Registered execution surfaces that may have deny lists."""
    return tuple(sorted(SURFACE_DENY_LISTS.keys()))


def denied_tools_for_surface(surface: str) -> FrozenSet[str]:
    """Return the deny set for ``surface``, or empty if unknown."""
    return SURFACE_DENY_LISTS_FROZEN.get(surface, frozenset())


def iter_tools_by_level(level: DangerLevel) -> Iterable[str]:
    """Yield tool names registered at the given danger level."""
    for name, lv in TOOL_DANGER_MAP.items():
        if lv == level:
            yield name


def summarize_policy() -> dict[str, object]:
    """Compact dict for logging or admin UI (counts only, not full lists)."""
    return {
        "surfaces": list(SURFACE_DENY_LISTS.keys()),
        "critical_count": sum(
            1 for v in TOOL_DANGER_MAP.values() if v == DangerLevel.CRITICAL
        ),
        "warn_count": sum(1 for v in TOOL_DANGER_MAP.values() if v == DangerLevel.WARN),
        "mapped_tools": len(TOOL_DANGER_MAP),
    }


def _candidate_forms(tool_name: str) -> set[str]:
    """Return every form of ``tool_name`` that should hit the same policy.

    Modern internal LLM tools use ``skill__endpoint``; legacy/external (MCP,
    providers) use dotted ``skill.endpoint``. Tests and gateway code already
    pass either shape, so deny-list matching needs to recognise both for the
    same logical tool.

    Bare endpoint names are intentionally NOT generated here — promoting
    ``foo`` from ``svc__foo`` would broaden enforcement across unrelated
    skills.
    """
    if not tool_name:
        return set()
    cands = {tool_name}
    if "__" in tool_name:
        skill, _, endpoint = tool_name.partition("__")
        if skill and endpoint:
            cands.add(f"{skill}.{endpoint}")
    elif "." in tool_name:
        skill, _, endpoint = tool_name.partition(".")
        if skill and endpoint:
            cands.add(f"{skill}__{endpoint}")
    return cands


def get_danger_level(tool_name: str) -> DangerLevel:
    """Return configured danger level, defaulting to SAFE for unknown tools.

    Honours both naming conventions: ``shell.exec`` and ``shell__exec``
    resolve to the same entry.
    """
    for cand in _candidate_forms(tool_name):
        level = TOOL_DANGER_MAP.get(cand)
        if level is not None:
            return level
    return DangerLevel.SAFE


def requires_approval(tool_name: str) -> bool:
    """True when the tool is WARN or CRITICAL (needs explicit approval flow)."""
    level = get_danger_level(tool_name)
    return level in (DangerLevel.WARN, DangerLevel.CRITICAL)


def is_tool_allowed(tool_name: str, surface: str) -> bool:
    """
    False if the tool is denied on this surface; True otherwise.

    Unknown surfaces are treated as unrestricted (no deny list entry). The
    matcher tests both ``skill.endpoint`` and ``skill__endpoint`` candidate
    forms so policy stays naming-agnostic.
    """
    denied = SURFACE_DENY_LISTS.get(surface)
    if denied is None:
        return True
    if not denied:
        return True
    candidates = _candidate_forms(tool_name)
    return candidates.isdisjoint(denied)


def resolve_surface_from_context(
    context: Optional[Mapping[str, object]],
    *,
    default: str = "websocket",
) -> str:
    """Map a ``handle_command`` context dict to an execution surface.

    Resolution order (Phase 1 — audit-r10 overhaul):
      1. ``context["surface"]`` — explicit override if the caller already
         knows the surface.
      2. ``context["device_target"]`` — Phase 1 wire field
         (`brain`/`phone`/`glasses`). When present and non-``auto``,
         dispatches to the matching surface so the orchestrator can
         enforce per-device deny lists. This is what unlocks the
         operator's "do X on my Mac" path from iOS chat:
         ``device_target == "brain"`` → ``brain_host`` (Mac tools
         allowed) instead of the legacy ``phone_surface → http_api``
         hard-deny.
      3. ``context["source"]`` (lowercased) in the source→surface
         table — historical fallback.
      4. ``default`` (websocket) when nothing matches.
    """
    if not context:
        return default
    surface = context.get("surface") if isinstance(context, Mapping) else None
    if isinstance(surface, str) and surface:
        return surface
    device_target = (
        context.get("device_target") if isinstance(context, Mapping) else None
    )
    if isinstance(device_target, str) and device_target:
        normalised = device_target.strip().lower()
        if normalised and normalised != "auto":
            mapped = _DEVICE_TARGET_TO_SURFACE.get(normalised)
            if mapped:
                return mapped
    source = context.get("source") if isinstance(context, Mapping) else None
    if isinstance(source, str) and source:
        mapped = _SOURCE_TO_SURFACE.get(source.strip().lower())
        if mapped:
            return mapped
    return default


__all__ = [
    "DangerLevel",
    "TOOL_DANGER_MAP",
    "SURFACE_DENY_LISTS",
    "SURFACE_DENY_LISTS_FROZEN",
    "known_surfaces",
    "denied_tools_for_surface",
    "iter_tools_by_level",
    "summarize_policy",
    "get_danger_level",
    "requires_approval",
    "is_tool_allowed",
    "resolve_surface_from_context",
]
