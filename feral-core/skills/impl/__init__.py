"""
FERAL Skill Implementations
============================
Concrete Python backing implementations for JSON skill schemas.
"""
import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Type

from skills.base import BaseSkill

logger = logging.getLogger("feral.skills.impl")

# Registry mapping skill_id -> Python Class implementation
SKILL_IMPLEMENTATIONS: Dict[str, Type[BaseSkill]] = {}

def register_skill(skill_class: Type[BaseSkill]):
    """Decorator to register a python skill implementation."""
    def wrapper():
        # Instantiate it to get the ID, or read standard class property
        instance = skill_class()
        SKILL_IMPLEMENTATIONS[instance.skill_id] = instance
        return skill_class

    wrapper()
    return skill_class

def register_instance(skill_id: str, instance):
    """Register a pre-built integration instance as a skill implementation."""
    SKILL_IMPLEMENTATIONS[skill_id] = instance

def get_implementation(skill_id: str) -> BaseSkill | None:
    """Retrieve the instantiated python logic instance for a skill."""
    return SKILL_IMPLEMENTATIONS.get(skill_id)


# Auto-load standard implementations below.
#
# Every entry here used to be its own ``try: import ... except
# ImportError: pass``, twenty-six of them. A missing optional dependency
# removed the skill from the process with no log line, no metric and no
# way for the operator to find out: ``feral doctor`` reported nothing,
# the model was simply never offered the tool, and the user was told
# FERAL could not do the thing. ``agentic_computer_use`` pulls the VLM
# path and ``external_agent`` needs the ACP bridge, so either one going
# missing is a capability disappearing from a running install.
#
# The loop below imports the same modules and records why each one
# failed, so ``load_report()`` can answer "which skills are not here and
# what is missing" instead of that answer existing nowhere.
AUTOLOAD_MODULES: tuple[str, ...] = (
    "web_search",
    "image_gen",
    "weather",
    "pdf_reader",
    "screen_capture",
    "subagent",
    "code_interpreter",
    "desktop_automation",
    "system_settings",
    "coding_tools",
    "gui_computer_use",
    "agentic_computer_use",
    "web_actions",
    # Registers nothing itself: the `browser` manifest and instance are
    # built at boot by api.state._register_browser_skill. Imported here
    # so an import-time break in the CDP driver surfaces at boot.
    "browser_use",
    "messaging_channels",
    "self_introspection",
    "workspace_scripts",
    "perception_query",
    "feral_reminders",
    "feral_routines",
    "feral_workflows",
    "notes_memory",
    "plan",
    # imported for the @register_skill side effect
    "cutebot_skill",
    # drives opencode / Claude Code / Codex over ACP
    "external_agent",
    # Per-domain browser knowledge (recall/remember). Deliberately NOT part
    # of skills/impl/browser_use.py: the store must be loadable without a
    # browser.
    "browser_memory",
)

# module name -> reason it is not loaded. Empty on a healthy install.
FAILED_IMPLEMENTATIONS: Dict[str, str] = {}


def _autoload() -> None:
    """Import each backing implementation, reporting the ones that fail.

    ``ImportError`` is the expected failure (an optional dependency is
    not installed) and is logged at warning, because a skill being gone
    is exactly what nobody could see before. Anything else is a bug in
    the module itself and is logged with a traceback.
    """
    import importlib

    for name in AUTOLOAD_MODULES:
        try:
            importlib.import_module(f"skills.impl.{name}")
        except ImportError as exc:
            FAILED_IMPLEMENTATIONS[name] = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "skill implementation '%s' is NOT loaded (%s). The skill will "
                "not be callable and the model will not be offered it. Install "
                "the missing dependency, or check 'failed' in "
                "skills.impl.load_report().", name, exc,
            )
        except Exception as exc:
            FAILED_IMPLEMENTATIONS[name] = f"{type(exc).__name__}: {exc}"
            logger.error(
                "skill implementation '%s' raised at import and is NOT loaded: "
                "%s", name, exc, exc_info=True,
            )


def _manifest_skill_ids() -> set:
    """skill_ids declared by the first-party manifest directory."""
    manifest_dir = Path(__file__).resolve().parent.parent / "manifests"
    ids: set = set()
    try:
        for path in manifest_dir.glob("*.json"):
            ids.add(path.stem)
            try:
                ids.add(
                    json.loads(path.read_text(encoding="utf-8")).get("skill_id", path.stem)
                )
            except Exception as exc:
                logger.warning("skill manifest %s is not readable JSON: %s", path, exc)
    except Exception as exc:
        logger.warning("could not read skill manifests from %s: %s", manifest_dir, exc)
    return ids


def load_report(known_skill_ids: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """What loaded, what did not, and what is registered but unreachable.

    ``unreachable_no_manifest`` lists implementations that registered
    successfully but that no manifest names.
    ``SkillRegistry.get_skill`` returns None for a skill_id it holds no
    manifest for, and ``SkillExecutor`` looks the implementation up by
    the manifest's ``skill_id``, so such an implementation can never be
    dispatched: the code is loaded, the capability is not.

    Pass ``known_skill_ids`` (``registry.skills.keys()``) whenever a live
    registry exists. Without it this falls back to the shipped manifest
    directory alone, which over-reports: ``weather_current`` comes from
    the hardcoded ``WEATHER_SKILL`` constant and ``browser`` is built at
    boot by ``api.state._register_browser_skill``, so both look
    unreachable from the directory and are not.
    """
    reachable = (
        {str(s) for s in known_skill_ids}
        if known_skill_ids is not None
        else _manifest_skill_ids()
    )
    unreachable = sorted(
        skill_id for skill_id in SKILL_IMPLEMENTATIONS
        if reachable and skill_id not in reachable
    )
    return {
        "loaded": sorted(SKILL_IMPLEMENTATIONS),
        "failed": dict(FAILED_IMPLEMENTATIONS),
        "unreachable_no_manifest": unreachable,
    }


def report_unreachable_implementations(known_skill_ids: Iterable[str]) -> list:
    """Log every registered implementation that no manifest can reach.

    Called from boot once the registry is fully populated, because that
    is the only point where the answer is true: manifests arrive from the
    shipped directory, from a hardcoded constant, from ``~/.feral/skills``
    and from ``api.state._register_browser_skill``.

    ``image_gen`` is in this state today. ``skills/impl/image_gen.py`` is
    a complete DALL-E 3 implementation with provider failover and it
    registers itself on import, but no manifest names ``image_gen``, so
    ``registry.get_skill("image_gen")`` returns None, the model is never
    offered the tool, and nothing anywhere said so.
    """
    unreachable = load_report(known_skill_ids)["unreachable_no_manifest"]
    for skill_id in unreachable:
        logger.warning(
            "skill implementation '%s' is registered but no manifest names it, "
            "so SkillRegistry.get_skill never returns it and nothing can "
            "dispatch it. Either add skills/manifests/%s.json or drop the "
            "implementation.", skill_id, skill_id,
        )
    return unreachable


_autoload()


# robot_action uses WS_EXECUTE — handled natively by SkillExecutor via the
# daemon WebSocket, which is the only path that reaches real hardware and
# which correctly errors "No connected daemon" when nothing is attached.
#
# There is deliberately no RobotActionSkill bridge here. The old
# robot_action.py built a RobotArmAdapter() with no serial port and returned
# status="success" with a fabricated joint array for every call, and its
# _map_endpoint sent robot_move to the adapter's `move_joints`, which reads
# `joints`/`speed_pct` — so the manifest's `direction` was silently dropped
# and the arm "moved" to all-zeros. Because SkillExecutor.execute checks
# Python backing implementations BEFORE WS_EXECUTE, merely importing that
# module would have shadowed the working daemon path with the fabricating
# one. The manifest's `direction`/`speed` and `action` params match what the
# node SDK's robot daemon actually consumes (feral-nodes/python-node-sdk/
# robot_template.py), so nothing else needs to change.
