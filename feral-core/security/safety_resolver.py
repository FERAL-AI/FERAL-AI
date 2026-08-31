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
   author, *asymmetrically trusted* (see below).
2. **Per-tool danger map** — ``get_danger_level(tool_name)`` (the
   centralized policy table that ``dangerous_tools`` already
   maintains).
3. **Substring heuristic** — preserved as a *last resort* so existing
   third-party manifests that omit safety metadata keep their current
   behaviour rather than getting silently demoted to AUTO.

**The manifest does not win outright.** It used to, and that was audit
finding P0.1: a skill installed from the marketplace into
``~/.feral/skills/`` could declare ``"safety_tier": "safe"`` and run with
no confirmation on every surface, forever. Escalation
(``confirm`` / ``deny`` / ``requires_user_approval``) is honoured from
anyone; **de-escalation** (``safe`` / ``read_only_hint``) is honoured only
from a manifest that ships in this repo (:func:`_manifest_may_de_escalate`).
The same trust boundary already governed a strictly less important field,
the declared ``result_budget`` in ``skills/result_budget.py``.

A manifest generated at runtime from a paired device
(``hwdev_*``) is a third case and gets a narrower answer: the generating
code is first-party but its inputs are strings the device sent, so
de-escalation there additionally requires the OPERATOR's sandbox policy to
permit that capability unattended. See :func:`_device_may_de_escalate`.

The output is a :class:`PolicyDecision` rather than a bare string so
callers can render an explainable approval card ("Why is this CONFIRM?
Because the manifest declared safety_tier=confirm and danger_map said
WARN").
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from typing import Any, Optional, TYPE_CHECKING

from security.dangerous_tools import (
    DangerLevel,
    get_danger_level,
    is_tool_allowed,
)
from security.hardware_policy import live_policy, permits_unattended
from skills.result_budget import builtin_skill_ids


if TYPE_CHECKING:
    from skills.registry import SkillRegistry


logger = logging.getLogger("feral.security.safety_resolver")


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
    # An MCP tool never earns AUTO from its name.
    #
    # No `mcp_*` name is in TOOL_DANGER_MAP, so get_danger_level returns
    # SAFE, meaning "the table says nothing", and resolution falls here.
    # The unknown default below is CONFIRM, which is fail-closed and
    # right. The auto-token branch is the hole: it hands AUTO to any name
    # containing read/get/list/status/query/search, so a tool called
    # `clipboard_read` executes with no approval in every autonomy mode
    # including strict, and returns whatever the operator last copied.
    #
    # For a native skill those substrings sit behind a manifest that was
    # reviewed. For an MCP tool there is no manifest, no review, and the
    # name is chosen by whoever wrote the server. A name is not evidence.
    #
    # `agents/plan_mode.py` already draws exactly this line, refusing
    # `mcp_*` by name because it "fails closed on everything that has no
    # FERAL manifest behind it". This makes the safety resolver agree
    # with plan mode instead of contradicting it.
    #
    # Deliberately narrow: only the AUTO shortcut is withdrawn. DENY and
    # CONFIRM tokens above still fire, and the default is unchanged, so
    # nothing that was refused becomes permitted.
    if name_lower.startswith("mcp_"):
        return LEVEL_CONFIRM, "mcp_tool:no_manifest_to_trust"
    if any(p in name_lower for p in _LEGACY_AUTO_TOKENS):
        return LEVEL_AUTO, "legacy_substring:auto_token"
    return LEVEL_CONFIRM, "legacy_substring:unknown_default"


def is_read_only(
    tool_name: str,
    *,
    registry: Optional["SkillRegistry"] = None,
    strict: bool = False,
) -> bool:
    """Manifest-aware read-only check used by strict-mode autonomy.

    Default (``strict=False``) prefers the manifest's ``read_only_hint``
    when set and otherwise falls back to the substring heuristic the
    legacy classifier used, so we don't regress existing behaviour for
    unannotated third-party skills.

    ``strict=True`` skips the substring fallback entirely and answers
    only from declared metadata: ``read_only_hint is True`` OR
    ``safety_tier == "safe"``, and never when the endpoint also demands
    user approval. Absence of metadata means False.

    Strict mode exists because the substring list is far too generous to
    be a security boundary. ``_LEGACY_READ_ONLY_TOKENS`` admits any name
    containing ``read``, ``status``, ``list`` or ``current``, which in
    the manifests shipping in this repo alone lets through
    ``messaging_sms__slack_reply_to_thread`` (matches ``read`` inside
    "thread"), ``messaging_sms__slack_set_status`` and
    ``spotify_music__play_playlist``. All three mutate remote state.
    Anything asking "is this endpoint incapable of mutation?" (plan
    mode) must pass ``strict=True``; the graduated-autonomy caller keeps
    the lenient default so unannotated third-party skills behave as
    before.

    audit P0.1: BOTH modes apply the same first-party clamp as
    :func:`_safety_from_manifest`. This function is the second door into
    the same room — ``agents/tool_runner.py`` skips approval entirely when
    it returns True under ``FERAL_AUTONOMY=strict``, and
    ``agents/plan_mode.is_plan_safe_tool`` gates on the strict form — so
    clamping only the resolver would have left the hole open.
    """
    endpoint = _find_endpoint(tool_name, registry)
    trusted = endpoint is not None and _manifest_may_de_escalate(
        *_split_tool_name(tool_name),
    )
    if strict:
        if endpoint is None or not trusted:
            return False
        if getattr(endpoint, "requires_user_approval", False):
            return False
        if endpoint.read_only_hint is True:
            return True
        tier = (getattr(endpoint, "safety_tier", None) or "").strip().lower()
        return tier == "safe"
    if trusted and endpoint.read_only_hint:
        return True
    name_lower = (tool_name or "").lower()
    # Same reasoning as the auto-token guard in
    # `_legacy_substring_level`, and this one is the sharper edge:
    # `ToolRunner` skips the approval prompt outright in STRICT mode
    # when this returns True, so a lenient answer here defeats the
    # strictest setting an operator can choose. An MCP name is not
    # evidence that the tool only reads.
    if name_lower.startswith("mcp_"):
        return False
    return any(p in name_lower for p in _LEGACY_READ_ONLY_TOKENS)


def _split_tool_name(tool_name: str) -> tuple[str, str]:
    """``("skill_id", "endpoint_id")`` for either accepted tool-name shape.

    Both ``skill__endpoint`` (LLM tool ids) and ``skill.endpoint`` (dotted)
    are accepted, splitting on the FIRST separator. Returns empty strings
    when the name carries no skill prefix at all.
    """
    if not tool_name:
        return "", ""
    if "__" in tool_name:
        skill_id, _, endpoint_id = tool_name.partition("__")
        return skill_id, endpoint_id
    if "." in tool_name:
        skill_id, _, endpoint_id = tool_name.partition(".")
        return skill_id, endpoint_id
    return "", ""


# Runtime-generated hardware skills (``hardware/capability_skill.py``).
_DEVICE_SKILL_PREFIX = "hwdev_"


def _is_live_device_skill(skill_id: str) -> bool:
    """True when ``skill_id`` belongs to a currently registered device.

    The generic HUP path turns any paired ``DeviceManifest`` into a
    ``SkillManifest`` at runtime, so no manifest file ships for these and
    ``builtin_skill_ids()`` cannot see them.

    This answers PROVENANCE only, and used to be the whole de-escalation
    test on the reasoning that ``hardware/capability_skill._safety_for`` is
    in-repo code. It is, but every value it reads
    (``category`` / ``permission_tier`` / ``requires_confirmation``) is a
    string the device sent, so a True here means "a real paired device said
    this", not "this is safe". :func:`_device_may_de_escalate` is the rest
    of the answer.

    The prefix alone is NOT the test. A marketplace package declares its own
    ``skill_id`` and could simply call itself ``hwdev_anything``, so the
    exemption is proven against the live ``DeviceRegistry``: the id must be
    the one ``skill_id_for_device`` derives for a device that is actually
    registered right now. When no brain is running there are no devices and
    this is False, which is the fail-closed answer.

    ``sys.modules`` rather than an import: this runs on every tool call, and
    importing ``api.state`` from here would both invert the dependency
    (``api.state`` imports the orchestrator, which imports this module) and
    stand up the whole brain inside unit tests.
    """
    if not skill_id.startswith(_DEVICE_SKILL_PREFIX):
        return False
    state_module = sys.modules.get("api.state")
    if state_module is None:
        return False
    device_registry = getattr(getattr(state_module, "state", None), "device_registry", None)
    if device_registry is None:
        return False
    try:
        from hardware.capability_skill import skill_id_for_device

        devices = device_registry.list_devices()
    except Exception as exc:                      # registry mid-swap, import cycle
        logger.debug("device-skill provenance check failed for %r: %s", skill_id, exc)
        return False
    return any(
        skill_id == skill_id_for_device(getattr(d, "device_id", "") or "")
        for d in devices or []
    )


def _device_capability(skill_id: str, endpoint_id: str):
    """The live ``DeviceCapability`` behind a ``hwdev_*`` tool, or ``None``.

    ``hardware/capability_skill._capability_to_endpoint`` uses ``cap.id``
    verbatim as the endpoint id, so the endpoint id IS the HUP capability
    id and the manifest can be looked up by it.
    """
    state_module = sys.modules.get("api.state")
    if state_module is None:
        return None
    device_registry = getattr(getattr(state_module, "state", None), "device_registry", None)
    if device_registry is None:
        return None
    try:
        from hardware.capability_skill import skill_id_for_device

        for device in device_registry.list_devices() or []:
            if skill_id != skill_id_for_device(getattr(device, "device_id", "") or ""):
                continue
            for cap in getattr(device, "capabilities", None) or []:
                if getattr(cap, "id", None) == endpoint_id:
                    return cap
            return None
    except Exception as exc:                      # registry mid-swap, import cycle
        logger.debug("device capability lookup failed for %r: %s", skill_id, exc)
    return None


def _device_may_de_escalate(skill_id: str, endpoint_id: str) -> bool:
    """Whether a live device's own declaration may lower ITS verdict.

    The original exemption here was ``_is_live_device_skill(skill_id)`` and
    nothing else, on the reasoning that ``hwdev_*`` manifests come out of
    in-repo code (``hardware/capability_skill._safety_for``) and are
    therefore first-party. The code is first-party. The *inputs* are not:
    ``_safety_for`` reads ``category``, ``permission_tier`` and
    ``requires_confirmation`` straight off the capability, and
    ``hardware/protocol.device_capability_from_action`` fills the absent
    ones in with ``actuator`` / ``passive`` / ``False``, which
    ``_safety_for`` maps to ``safe``. So a node that paired itself and sent
    ``{"name": "unlock_door", "category": "actuator"}`` produced an
    auto-executing LLM tool, from the same three lines of trust this
    function's own docstring was written to close.

    Nothing in the self-description separates that from the CuteBot's
    ``halt``, which is declared ``permission_tier="passive"`` for the good
    reason that prompting before an emergency stop leaves the hardware
    moving. Both are actuator capabilities a device called passive, and the
    device is the only source. So the answer cannot come from the device at
    all: it comes from the operator, via
    ``security/hardware_policy.permits_unattended`` reading
    ``hardware.actuators.allowed`` /
    ``hardware.actuators.requires_confirmation`` /
    ``hardware.sensors.allowed`` /
    ``hardware.movement.emergency_stop_enabled``. ``halt`` keeps its auto
    verdict from the operator's stop carve-out, ``set_lights`` and
    ``read_telemetry`` from the shipped allowlists, and ``unlock_door``
    gets an approval card because no operator ever named it.

    The trade this makes: a device that names a capability ``halt`` still
    gets the stop carve-out without proving it stops anything. That is
    deliberate. Trusting a name is a far smaller surface than trusting a
    self-declared tier, and it fails in the direction where the worst case
    is an unattended stop rather than an unattended actuation.

    The provenance check is unchanged and still comes first: the skill id
    must be the one ``skill_id_for_device`` derives for a device that is
    registered right now, because a marketplace package can call itself
    ``hwdev_anything``.
    """
    if not _is_live_device_skill(skill_id):
        return False
    cap = _device_capability(skill_id, endpoint_id)
    if cap is None:
        # Registered device, unknown capability. Not something to guess at.
        return False
    return permits_unattended(
        live_policy(), endpoint_id, getattr(cap, "category", "") or "",
    )


def _manifest_may_de_escalate(skill_id: str, endpoint_id: str = "") -> bool:
    """Whether ``skill_id``'s manifest is allowed to LOWER a safety verdict.

    audit P0.1. ``_safety_from_manifest`` used to return the manifest's own
    ``safety_tier`` before consulting the danger map or the heuristic, and
    the only gate above it was a deny list of hardcoded first-party tool
    names that a third-party skill id cannot match. An installed skill
    declaring ``"safety_tier": "safe"`` therefore executed with no
    confirmation, on every surface, forever.

    This is the same trust boundary ``skills/result_budget.budget_for``
    already applies to a declared ``result_budget`` — a manifest that does
    not ship in this repo does not get to set it. That clamp governs how
    many characters of a result reach the model; this one governs whether
    the operator is asked before the code runs.

    A manifest that ships in this repo was written by someone who could
    have changed this file instead, so it still de-escalates outright. A
    live device's manifest was written by the device, and goes through
    :func:`_device_may_de_escalate`.
    """
    if not skill_id:
        return False
    if skill_id in builtin_skill_ids():
        return True
    return _device_may_de_escalate(skill_id, endpoint_id)


def _find_endpoint(tool_name: str, registry: Optional["SkillRegistry"]):
    """Best-effort lookup of the SkillEndpoint object for ``tool_name``.

    Both ``skill__endpoint`` (LLM tool ids) and ``skill.endpoint``
    (dotted) shapes are accepted. Returns ``None`` when the registry
    isn't wired (tests / legacy callers)."""
    if registry is None or not tool_name:
        return None
    skill_id, endpoint_id = _split_tool_name(tool_name)
    if not skill_id:
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


def _safety_from_manifest(endpoint, skill_id: str = "") -> Optional[str]:
    """Translate the manifest's three-state ``safety_tier`` into the
    canonical level. Returns ``None`` when the manifest is silent, or when
    it made a claim it is not trusted to make.

    A manifest may ESCALATE freely: ``requires_user_approval``,
    ``safety_tier: confirm`` and ``safety_tier: deny`` are honoured no
    matter who wrote them, because the worst case is an extra prompt.

    De-escalation (``safety_tier: safe``, ``read_only_hint: true``) is
    first-party only — see :func:`_manifest_may_de_escalate`. A clamped
    claim returns ``None`` rather than a level, so the tool falls through
    to the danger map and then the substring heuristic and ends up exactly
    where an unannotated third-party skill would: it loses the benefit of
    the declaration, it is not punished for having made one.
    """
    if endpoint is None:
        return None

    # ── escalation: anyone may ask for more friction ──────────────────
    if endpoint.requires_user_approval:
        return LEVEL_CONFIRM
    tier = (getattr(endpoint, "safety_tier", None) or "").strip().lower()
    if tier == "confirm":
        return LEVEL_CONFIRM
    if tier == "deny":
        return LEVEL_DENY

    # ── de-escalation: first-party manifests only ─────────────────────
    claims_auto = tier == "safe" or bool(getattr(endpoint, "read_only_hint", False))
    if not claims_auto:
        return None
    endpoint_id = str(getattr(endpoint, "id", "") or "")
    if not _manifest_may_de_escalate(skill_id, endpoint_id):
        logger.info(
            "Skill %r is not a built-in manifest; ignoring its declared "
            "safety_tier=%r / read_only_hint=%r and resolving %r from policy",
            skill_id, tier or None, bool(getattr(endpoint, "read_only_hint", False)),
            endpoint_id,
        )
        # A live device's clamped claim is CONFIRM, not "fall through".
        # For an arbitrary third-party skill, None is right: we genuinely
        # do not know what the endpoint does, so it deserves exactly the
        # treatment an unannotated manifest gets. For a device capability
        # we DO know: it is hardware the operator has not permitted to run
        # unattended. Falling through would hand that verdict to
        # ``_LEGACY_READ_ONLY_TOKENS``, which auto-approves any name
        # containing "status", "get" or "current" and would let
        # ``get_door_open`` back through the hole this closes.
        if _is_live_device_skill(skill_id):
            return LEVEL_CONFIRM
        return None
    return LEVEL_AUTO


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
    2. Manifest metadata — declared intent of the skill author, able to
       escalate from any source and to de-escalate only from a
       first-party one.
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
    manifest_level = _safety_from_manifest(endpoint, _split_tool_name(tool_name)[0])
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

    # 2. Manifest, within the limits of who wrote it. A clamped
    #    de-escalation arrives here as None and falls through.
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
