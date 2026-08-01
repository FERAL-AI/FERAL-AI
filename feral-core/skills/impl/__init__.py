"""
FERAL Skill Implementations
============================
Concrete Python backing implementations for JSON skill schemas.
"""
from typing import Dict, Type
from skills.base import BaseSkill

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

# Auto-load standard implementations below
try:
    import skills.impl.web_search
except ImportError:
    pass

try:
    import skills.impl.image_gen
except ImportError:
    pass

try:
    import skills.impl.weather
except ImportError:
    pass

try:
    import skills.impl.pdf_reader
except ImportError:
    pass

try:
    import skills.impl.screen_capture
except ImportError:
    pass

try:
    import skills.impl.subagent
except ImportError:
    pass

try:
    import skills.impl.code_interpreter
except ImportError:
    pass

try:
    import skills.impl.desktop_automation
except ImportError:
    pass

try:
    import skills.impl.system_settings
except ImportError:
    pass

try:
    import skills.impl.coding_tools
except ImportError:
    pass

try:
    import skills.impl.gui_computer_use
except ImportError:
    pass

try:
    import skills.impl.agentic_computer_use
except ImportError:
    pass

try:
    import skills.impl.web_actions
except ImportError:
    pass

try:
    import skills.impl.browser_use
except ImportError:
    pass

try:
    import skills.impl.messaging_channels
except ImportError:
    pass

try:
    import skills.impl.self_introspection
except ImportError:
    pass

try:
    import skills.impl.workspace_scripts
except ImportError:
    pass

try:
    import skills.impl.perception_query
except ImportError:
    pass

try:
    import skills.impl.feral_reminders
except ImportError:
    pass

try:
    import skills.impl.feral_routines
except ImportError:
    pass

try:
    import skills.impl.feral_workflows
except ImportError:
    pass

try:
    import skills.impl.notes_memory
except ImportError:
    pass

try:
    import skills.impl.plan
except ImportError:
    pass

try:
    import skills.impl.cutebot_skill  # noqa: F401 (imported for @register_skill side effect)
except ImportError:
    pass

try:
    import skills.impl.external_agent  # noqa: F401 (drives opencode / Claude Code / Codex over ACP)
except ImportError:
    pass


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
