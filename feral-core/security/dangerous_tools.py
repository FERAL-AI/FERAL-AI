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
    # `coding_tools` is the renamed-but-still-registered duplicate of
    # `computer_use`; it exposes the same shell + write surface and must
    # carry the same danger classification so http_api callers don't
    # bypass the deny list by selecting the alias.
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
        "agentic_computer_use__execute_task",
        "gui_computer_use__screenshot",
        "gui_computer_use__mouse_click",
        "gui_computer_use__type_text",
        "desktop_automation__click_screen",
        "desktop_automation__type_text",
    },
}

# Voice-realtime and autonomous multi-agent surfaces have NO inline
# human-approval loop: a hands-free realtime voice turn and a spawned
# subagent worker cannot render a CONFIRM card and wait. So they inherit
# http_api's CRITICAL deny floor here, and the call sites
# (voice/realtime_proxy.py, voice/gemini_realtime.py,
# agents/multi_agent.py) additionally refuse any tool that does not
# resolve to AUTO. Declared centrally so the floor shows up in
# ``known_surfaces()`` and is a single source of truth.
SURFACE_DENY_LISTS["voice"] = set(SURFACE_DENY_LISTS["http_api"])
SURFACE_DENY_LISTS["agent"] = set(SURFACE_DENY_LISTS["http_api"])


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


# Surfaces that grant MORE than the minimally-trusted default and must
# therefore never be selectable by an untrusted/remote client via a raw
# ``context["surface"]`` claim. ``local_cli`` has an empty deny list
# (fully unrestricted) and ``brain_host`` grants Mac desktop/shell
# control. The legitimate way to reach these is the trusted transport
# (loopback CLI) or an explicit, separately-authorized ``device_target``
# — not a client-declared surface string.
_EXPLICIT_PRIVILEGED_SURFACES: FrozenSet[str] = frozenset({"local_cli", "brain_host"})


def is_privileged_surface(surface: str) -> bool:
    """True when ``surface`` is unrestricted/privileged and so must not be
    selectable by an untrusted client.

    Privileged means any of:
      * unknown / unregistered — ``is_tool_allowed`` treats these as
        unrestricted, so honoring an unknown claim would bypass all deny
        lists;
      * an empty deny list (``local_cli``) — nothing is blocked;
      * an explicitly privileged surface (``brain_host``).
    """
    if surface in _EXPLICIT_PRIVILEGED_SURFACES:
        return True
    denies = SURFACE_DENY_LISTS.get(surface)
    if denies is None:          # unknown surface -> unrestricted
        return True
    if not denies:              # empty deny list -> unrestricted
        return True
    return False


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

    Resolution order:
      1. ``context["surface"]`` — an explicit override, but honored ONLY
         when it is an *un-privileged* surface (Batch 1 trust-boundary
         fix #6). A client / paired remote node must not be able to
         escalate by declaring ``surface="local_cli"`` (or any unknown /
         unrestricted surface) to escape the deny lists — such claims are
         ignored and resolution falls through to the transport-derived
         signals below. Un-privileged claims (e.g. the strict
         ``http_api``) only ever *restrict*, so they stay honored for the
         trusted internal callers that set them.
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
    if isinstance(surface, str) and surface and not is_privileged_surface(surface):
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
    "is_privileged_surface",
    "resolve_surface_from_context",
]
