"""
Manifest-aware safety resolution for FERAL tools.

Before this module, ``tool_runner.classify_safety`` was a tower of
substring heuristics: a tool whose name contained "create" or "delete"
was CONFIRM, anything with "search" or "read" was AUTO, etc. Two real
failures fell out of that:

1. **Wrong-direction matches.** ``feral_reminders__create`` is benign
   (writes to FERAL's own DB), but ``smart_home__delete_device`` is
   destructive — both end up tagged CONFIRM with no way to override.
2. **No coupling to the canonical map.** We have
   ``security/dangerous_tools.TOOL_DANGER_MAP`` already pinning the
   real safety tier for shell, file-write, and computer-use endpoints,
   but ``enforce_safety`` never consults it.

This resolver is the explicit, manifest-first replacement. Lookup
order:

1. **Manifest metadata** — ``SkillEndpoint.safety_tier`` /
   ``read_only_hint`` / ``requires_user_approval`` set by the skill
   author.
2. **Per-tool danger map** — ``get_danger_level(tool_name)`` (the
   centralized policy table that ``dangerous_tools`` already
   maintains).
3. **Substring heuristic** — preserved as a *last resort* so existing
   third-party manifests that omit safety metadata keep their current
   behaviour rather than getting silently demoted to AUTO.

The output is a :class:`PolicyDecision` rather than a bare string so
callers can render an explainable approval card ("Why is this CONFIRM?
Because the manifest declared safety_tier=confirm and danger_map said
WARN").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, TYPE_CHECKING

from security.dangerous_tools import (
    DangerLevel,
    get_danger_level,
    is_tool_allowed,
)


if TYPE_CHECKING:
    from skills.registry import SkillRegistry


# Mirror the strings ToolRunner already emits so external callers
# (REST, SDUI, tests) do not have to learn a new vocabulary.
LEVEL_AUTO = "auto"
LEVEL_CONFIRM = "confirm"
LEVEL_DENY = "deny"


@dataclass
class PolicyDecision:
    """The single source of truth for a per-call safety verdict."""

    tool_name: str
    surface: str
    level: str                              # auto | confirm | deny
    sources: dict = field(default_factory=dict)
    deny_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "tool_name": self.tool_name,
            "surface": self.surface,
            "level": self.level,
            "sources": dict(self.sources),
            "deny_reason": self.deny_reason,
        }


# Pre-existing fallback heuristics kept verbatim so legacy manifests
# without explicit safety metadata behave the same as before.
_LEGACY_DENY_TOKENS = ("format", "erase_all", "factory_reset", "self_destruct")
_LEGACY_CONFIRM_TOKENS = (
    "send", "post", "create", "delete", "update", "move", "grip",
    "play", "pause", "skip", "volume", "lock", "message", "order",
    "schedule", "daemon", "execute", "robot", "actuator", "motor",
)
_LEGACY_AUTO_TOKENS = (
    "search", "query", "get", "list", "current", "now_playing",
    "forecast", "status", "read", "notes_memory", "web_search",
)
_LEGACY_READ_ONLY_TOKENS = (
    "search", "get", "list", "query", "read", "current", "status", "forecast",
)


def _is_drive_tool(tool_name: str) -> bool:
    """True for any robot wheel-drive tool, legacy OR generic-HUP.

    The legacy hand-written CuteBot skill emits ``cutebot__drive``; the
    generic self-describing path emits ``hwdev_<device>__drive`` (skill id is
    ``hwdev_<device>`` and the endpoint id is the device's ``drive``
    capability). Both reach the same physical wheels, so the same speed cap
    must cover both — additive coverage, nothing removed.
    """
    if not tool_name:
        return False
    if tool_name == "cutebot__drive":
        return True
    return tool_name.startswith("hwdev_") and tool_name.endswith("__drive")


def _cutebot_drive_speed_deny(tool_name: str, args: dict) -> Optional[PolicyDecision]:
    """Deny dangerous robot wheel speeds before manifest metadata wins.

    Mirrors the legacy ``robot_move`` speed cap (``safety_resolver`` lines
    103–104) but applies to the ``drive`` left/right parameters of BOTH the
    legacy ``cutebot__drive`` tool and any generic ``hwdev_*__drive`` tool.
    """
    if not _is_drive_tool(tool_name):
        return None
    try:
        left = abs(int((args or {}).get("left", 0) or 0))
        right = abs(int((args or {}).get("right", 0) or 0))
    except (TypeError, ValueError):
        left = right = 0
    if left > 80 or right > 80:
        return PolicyDecision(
            tool_name=tool_name,
            surface="",
            level=LEVEL_DENY,
            sources={"cutebot_speed_limit": True},
            deny_reason=(
                f"Robot wheel speed exceeds safe limit (max 80): "
                f"left={left}, right={right}"
            ),
        )
    return None


def _legacy_substring_level(tool_name: str, args: dict) -> tuple[str, str]:
    """Return ``(level, source_label)`` from the legacy heuristic so the
    resolver can fall back transparently when nothing more authoritative
    is available."""
    name_lower = (tool_name or "").lower()
    if any(d in name_lower for d in _LEGACY_DENY_TOKENS):
        return LEVEL_DENY, "legacy_substring:deny_token"
    if ("robot_move" in name_lower or "actuator" in name_lower) and (args or {}).get("speed", 0) > 80:
        return LEVEL_DENY, "legacy_substring:robot_speed"
    if any(p in name_lower for p in _LEGACY_CONFIRM_TOKENS):
        return LEVEL_CONFIRM, "legacy_substring:confirm_token"
    if any(p in name_lower for p in _LEGACY_AUTO_TOKENS):
        return LEVEL_AUTO, "legacy_substring:auto_token"
    return LEVEL_CONFIRM, "legacy_substring:unknown_default"


def is_read_only(
    tool_name: str,
    *,
    registry: Optional["SkillRegistry"] = None,
) -> bool:
    """Manifest-aware read-only check used by strict-mode autonomy.

    Prefers the manifest's ``read_only_hint`` when set; otherwise falls
    back to the substring heuristic the legacy classifier used so we
    don't regress existing behaviour for unannotated third-party
    skills."""
    endpoint = _find_endpoint(tool_name, registry)
    if endpoint is not None and endpoint.read_only_hint:
        return True
    name_lower = (tool_name or "").lower()
    return any(p in name_lower for p in _LEGACY_READ_ONLY_TOKENS)


def _find_endpoint(tool_name: str, registry: Optional["SkillRegistry"]):
    """Best-effort lookup of the SkillEndpoint object for ``tool_name``.

    Both ``skill__endpoint`` (LLM tool ids) and ``skill.endpoint``
    (dotted) shapes are accepted. Returns ``None`` when the registry
    isn't wired (tests / legacy callers)."""
    if registry is None or not tool_name:
        return None
    if "__" in tool_name:
        skill_id, _, endpoint_id = tool_name.partition("__")
    elif "." in tool_name:
        skill_id, _, endpoint_id = tool_name.partition(".")
    else:
        return None
    skill = getattr(registry, "skills", {}).get(skill_id) if registry else None
    if skill is None:
        return None
    for ep in getattr(skill, "endpoints", []) or []:
        if ep.id == endpoint_id:
            return ep
    return None


def _level_from_danger(level: DangerLevel) -> str:
    if level == DangerLevel.CRITICAL:
        # CRITICAL tools that are *not* surface-denied still demand
        # explicit confirmation; the deny verdict belongs to the
        # surface deny list, not to the manifest entry.
        return LEVEL_CONFIRM
    if level == DangerLevel.WARN:
        return LEVEL_CONFIRM
    return LEVEL_AUTO


def _safety_from_manifest(endpoint) -> Optional[str]:
    """Translate the manifest's three-state ``safety_tier`` into the
    canonical level. Returns ``None`` when the manifest is silent."""
    if endpoint is None:
        return None
    if endpoint.requires_user_approval:
        return LEVEL_CONFIRM
    tier = (getattr(endpoint, "safety_tier", None) or "").strip().lower()
    if tier == "safe":
        return LEVEL_AUTO
    if tier == "confirm":
        return LEVEL_CONFIRM
    if tier == "deny":
        return LEVEL_DENY
    if endpoint.read_only_hint:
        return LEVEL_AUTO
    return None


def resolve_policy(
    tool_name: str,
    args: Optional[dict] = None,
    *,
    surface: str = "websocket",
    registry: Optional["SkillRegistry"] = None,
) -> PolicyDecision:
    """Compute the authoritative policy decision for ``tool_name``.

    The order of operations mirrors the docstring:

    1. Surface deny list — non-negotiable hard block.
    2. Manifest metadata — declared intent of the skill author.
    3. Danger map — centralized policy.
    4. Substring heuristic — last-resort fallback for unannotated
       manifests so we don't regress existing third-party skills.
    """
    args = args or {}
    sources: dict[str, Any] = {}

    if not is_tool_allowed(tool_name, surface):
        return PolicyDecision(
            tool_name=tool_name, surface=surface, level=LEVEL_DENY,
            sources={"surface_deny": True},
            deny_reason=f"Tool '{tool_name}' is denied on surface '{surface}'.",
        )

    speed_deny = _cutebot_drive_speed_deny(tool_name, args)
    if speed_deny is not None:
        speed_deny.surface = surface
        return speed_deny

    endpoint = _find_endpoint(tool_name, registry)
    manifest_level = _safety_from_manifest(endpoint)
    if endpoint is not None:
        sources["manifest"] = {
            "safety_tier": getattr(endpoint, "safety_tier", None),
            "read_only_hint": bool(getattr(endpoint, "read_only_hint", False)),
            "requires_user_approval": bool(getattr(endpoint, "requires_user_approval", False)),
        }

    danger_level = get_danger_level(tool_name)
    sources["danger_map"] = danger_level.value if hasattr(danger_level, "value") else str(danger_level)

    legacy_level, legacy_source = _legacy_substring_level(tool_name, args)
    sources["legacy_substring"] = legacy_source

    # 2. Manifest wins outright.
    if manifest_level is not None:
        return PolicyDecision(
            tool_name=tool_name, surface=surface, level=manifest_level, sources=sources,
        )

    # 3. Danger map: CRITICAL/WARN -> CONFIRM, SAFE -> defer to legacy
    # (because SAFE in the danger map means "we haven't told the policy
    # anything about this tool", not "this is definitely auto-able").
    if danger_level in (DangerLevel.WARN, DangerLevel.CRITICAL):
        return PolicyDecision(
            tool_name=tool_name, surface=surface,
            level=_level_from_danger(danger_level),
            sources=sources,
        )

    # 4. Legacy substring heuristic.
    return PolicyDecision(
        tool_name=tool_name, surface=surface, level=legacy_level, sources=sources,
    )


__all__ = [
    "LEVEL_AUTO",
    "LEVEL_CONFIRM",
    "LEVEL_DENY",
    "PolicyDecision",
    "is_read_only",
    "resolve_policy",
]
