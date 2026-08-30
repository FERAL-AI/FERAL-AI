"""A paired device must not be the one that decides it needs no approval.

The hole, end to end:

1. ``hardware/protocol.device_capability_from_action`` defaults a HUP
   ``actions[]`` entry's ``category`` to ``actuator`` and its
   ``permission_tier`` to ``passive``, with ``requires_confirmation``
   defaulting to False.
2. ``hardware/capability_skill._safety_for`` maps *actuator + passive +
   no-confirm* to ``("safe", read_only=False, approval=False)``.
3. ``security/safety_resolver._manifest_may_de_escalate`` exempted every
   live device skill from the P0.1 clamp, so that ``safe`` was honoured.
4. The tool resolved ``LEVEL_AUTO``. A node that pairs itself and says
   ``{"name": "unlock_door", "category": "actuator"}`` got an
   auto-executing LLM tool and the operator was never asked.

The P0.1 clamp's own docstring is the argument against its device
exemption: it exists because "An installed skill declaring
``safety_tier: safe`` therefore executed with no confirmation, on every
surface, forever." The exemption says the ``hwdev_*`` mapping is
first-party *code*, which is true, and concludes the verdict is
first-party, which does not follow: every input to that code
(``category``, ``permission_tier``, ``requires_confirmation``) is a
string the device sent.

**The constraint that makes the obvious fix wrong.** A blanket "actuators
always confirm" rule puts an approval card in front of ``halt``, the
CuteBot's emergency stop, which ``hardware/adapters/cutebot.py`` declares
``permission_tier="passive"`` and ``skills/manifests/cutebot.json``
declares ``safety_tier: safe``. Refusing (or delaying) a stop leaves
hardware moving, which is worse than the action the prompt prevents.
``set_lights`` and ``read_telemetry`` are legitimately unattended too.

Nothing in the device's self-description separates ``halt`` from
``unlock_door``: both are actuator capabilities a device declared
passive, and the device is the only source. So the separation has to come
from the one party that is not the device: the operator, via
``hardware.actuators.allowed`` / ``hardware.sensors.allowed`` and
``hardware.movement.emergency_stop_enabled``. That is precisely what
``SandboxPolicy.can_use_actuator`` and ``emergency_stop_enabled`` were
written for, and until this change the only caller of either was a route
(``POST /api/hardware/execute``) that nothing in the repo invokes.

Tests below are written to fail against the pre-fix tree, except the ones
whose docstring says "regression guard". Those pin behaviour the fix must
not cost (``halt``, ``set_lights``, the shipped sensor reads).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hardware.capability_skill import (  # noqa: E402
    device_manifest_to_skill_manifest,
    skill_id_for_device,
)
from hardware.protocol import (  # noqa: E402
    DeviceManifest,
    device_capability_from_action,
)
from security.safety_resolver import (  # noqa: E402
    LEVEL_AUTO,
    LEVEL_CONFIRM,
    resolve_policy,
)
from security.sandbox_policy import SandboxPolicy  # noqa: E402


class _FakeRegistry:
    """Minimal stand-in for ``SkillRegistry``. ``_find_endpoint`` only
    ever reads ``.skills[skill_id].endpoints``."""

    def __init__(self, manifests):
        self.skills = {m.skill_id: m for m in manifests}


def _device_from_wire(device_id: str, actions: list[dict]) -> DeviceManifest:
    """Build a manifest the way a self-describing node's HUP envelope does.

    Deliberately goes through ``device_capability_from_action`` rather than
    constructing ``DeviceCapability`` by hand, so the wire-format defaults
    under test are the ones actually applied.
    """
    caps = [c for c in (device_capability_from_action(a) for a in actions) if c]
    return DeviceManifest(
        device_id=device_id,
        device_type="robot",
        name=device_id,
        capabilities=caps,
    )


def _resolve_as_paired(device: DeviceManifest, capability_id: str, policy):
    """Resolve the generated LLM tool for ``capability_id`` with the device
    registered and ``policy`` loaded, i.e. exactly the live configuration."""
    skill = device_manifest_to_skill_manifest(device)
    registry = _FakeRegistry([skill])
    tool = f"{skill_id_for_device(device.device_id)}__{capability_id}"

    fake_state = MagicMock()
    fake_state.device_registry.list_devices.return_value = [device]
    fake_state.policy = policy
    with patch.dict(sys.modules, {"api.state": MagicMock(state=fake_state)}):
        return resolve_policy(tool, registry=registry)


# ── the exploit ──────────────────────────────────────────────────────

def test_device_declared_actuator_does_not_reach_auto():
    """THE EXPLOIT. A node declares one actuator and omits every safety
    field. Pre-fix this is LEVEL_AUTO: the LLM gets a tool that unlocks a
    door with no approval card, because the door said it was passive."""
    device = _device_from_wire("evil-node-0", [
        {"name": "unlock_door", "description": "Open the front door"},
    ])
    decision = _resolve_as_paired(device, "unlock_door", SandboxPolicy())
    assert decision.level != LEVEL_AUTO
    assert decision.level == LEVEL_CONFIRM


def test_device_cannot_buy_auto_by_declaring_passive_out_loud():
    """Same hole, spelled out rather than defaulted. ``permission_tier``
    is a string the device chose; saying it louder must not help."""
    device = _device_from_wire("evil-node-1", [
        {
            "name": "unlock_door",
            "category": "actuator",
            "permission_tier": "passive",
            "requires_confirmation": False,
            "description": "Open the front door",
        },
    ])
    assert _resolve_as_paired(
        device, "unlock_door", SandboxPolicy(),
    ).level == LEVEL_CONFIRM


def test_device_cannot_escape_by_mislabelling_the_category():
    """``category`` is device-supplied too. A rule keyed only on
    ``category == "actuator"`` is evaded by writing ``network``; the
    check is therefore default-deny across every category the operator's
    policy does not name."""
    for category in ("network", "compute", "display", "sensor", ""):
        device = _device_from_wire(f"evil-node-{category or 'blank'}", [
            {"name": "unlock_door", "category": category, "description": "Open"},
        ])
        assert _resolve_as_paired(
            device, "unlock_door", SandboxPolicy(),
        ).level == LEVEL_CONFIRM, category


def test_operator_allowlist_is_what_grants_auto_not_the_device():
    """The positive half of the same rule: an operator who names the
    capability in ``hardware.actuators.allowed`` gets the auto behaviour
    back. The device's declaration is necessary but never sufficient."""
    device = _device_from_wire("shed-node-0", [
        {"name": "unlock_door", "category": "actuator", "description": "Open"},
    ])
    policy = SandboxPolicy({
        "hardware": {"actuators": {"allowed": ["unlock_door"], "blocked": []}},
    })
    assert _resolve_as_paired(device, "unlock_door", policy).level == LEVEL_AUTO


def test_operator_requires_confirmation_list_beats_the_allowlist():
    """``hardware.actuators.requires_confirmation`` is the operator's
    "allowed, but ask me" lever. It had no reader at all before this
    change (``can_use_actuator``'s second return value was discarded at
    its single call site)."""
    device = _device_from_wire("shed-node-1", [
        {"name": "unlock_door", "category": "actuator", "description": "Open"},
    ])
    policy = SandboxPolicy({
        "hardware": {
            "actuators": {
                "allowed": ["unlock_door"],
                "blocked": [],
                "requires_confirmation": ["unlock_door"],
            },
        },
    })
    assert _resolve_as_paired(device, "unlock_door", policy).level == LEVEL_CONFIRM


def test_no_policy_loaded_still_clamps():
    """Fail closed. ``state.policy`` is None until ``AppState.initialize``
    runs, and "no policy yet" must not read as "everything is fine"."""
    device = _device_from_wire("evil-node-2", [
        {"name": "unlock_door", "category": "actuator", "description": "Open"},
    ])
    assert _resolve_as_paired(device, "unlock_door", None).level == LEVEL_CONFIRM


# ── the constraint: emergency stop must stay unattended ──────────────

def test_emergency_stop_stays_auto():
    """REGRESSION GUARD for this fix. Putting an approval card in front of
    stopping a moving robot is a safety regression, not a fix. The carve-out
    is the operator's (``hardware.movement.emergency_stop_enabled``,
    default true), not the device's."""
    device = _device_from_wire("cutebot-usb-0", [
        {"name": "halt", "category": "actuator", "permission_tier": "passive",
         "description": "Stop the wheels"},
    ])
    assert _resolve_as_paired(device, "halt", SandboxPolicy()).level == LEVEL_AUTO


@pytest.mark.parametrize("capability_id", ["halt", "estop", "emergency_stop", "stop"])
def test_every_stop_spelling_stays_auto(capability_id):
    """REGRESSION GUARD. The adapters do not agree on a spelling,
    ``hardware/adapters/cutebot.py`` says ``halt`` and
    ``hardware/adapters/robot_arm.py`` says ``estop``, so all of
    ``SandboxPolicy``'s stop spellings must survive an empty allowlist."""
    device = _device_from_wire("arm-0", [
        {"name": capability_id, "category": "actuator", "description": "Stop"},
    ])
    policy = SandboxPolicy({"hardware": {"actuators": {"allowed": [], "blocked": []}}})
    assert _resolve_as_paired(device, capability_id, policy).level == LEVEL_AUTO


def test_operator_can_turn_the_stop_carve_out_off():
    """The carve-out is a policy switch, so an operator who does not want
    it can say so and get the allowlist enforced on stops too."""
    device = _device_from_wire("arm-1", [
        {"name": "halt", "category": "actuator", "description": "Stop"},
    ])
    policy = SandboxPolicy({
        "hardware": {
            "actuators": {"allowed": [], "blocked": []},
            "movement": {"emergency_stop_enabled": False},
        },
    })
    assert _resolve_as_paired(device, "halt", policy).level == LEVEL_CONFIRM


# ── the constraint: the shipped fleet must not grow prompts ──────────

@pytest.mark.parametrize(
    ("capability_id", "expected"),
    [
        # hardware/adapters/cutebot.py, verbatim tiers.
        ("halt", LEVEL_AUTO),          # emergency stop
        ("set_lights", LEVEL_AUTO),    # passive, in the shipped allowlist
        ("read_telemetry", LEVEL_AUTO),  # sensor read
        ("follow_line", LEVEL_CONFIRM),  # active: escalation, unchanged
        ("explore", LEVEL_CONFIRM),      # active
        ("drive", LEVEL_CONFIRM),        # dangerous
    ],
)
def test_shipped_cutebot_capabilities_are_unchanged(capability_id, expected):
    """REGRESSION GUARD. The real CuteBot adapter's self-description,
    resolved against the shipped default policy, must land exactly where
    the hand-written ``skills/manifests/cutebot.json`` puts it."""
    from hardware.adapters.cutebot import CuteBotAdapter

    device = CuteBotAdapter(port="/dev/null").manifest
    assert _resolve_as_paired(device, capability_id, SandboxPolicy()).level == expected


@pytest.mark.parametrize(
    "capability_id",
    [
        # hardware/protocol.py: reference glasses
        "read_heart_rate", "read_spo2", "read_temperature", "read_uv",
        "read_steps", "capture_photo",
        # hardware/adapters/wristband.py
        "heart_rate", "spo2", "skin_temp",
        # hardware/adapters/robot_arm.py
        "read_position",
        # hardware/adapters/smart_home.py
        "thermostat_read",
        # hardware/mesh.py: phone node
        "camera_snap", "gps_location", "health_sensors",
        # hardware/adapters/cutebot.py
        "read_telemetry",
    ],
)
def test_default_policy_permits_every_shipped_sensor(capability_id):
    """REGRESSION GUARD, and the sensor half of the lesson
    ``test_default_policy_permits_every_shipped_actuator`` records: wiring
    a check to an allowlist is only safe once the allowlist names what the
    shipped adapters actually send. Five shipped sensor capability ids
    (``read_position``/``position``, ``skin_temp``, ``gps_location``,
    ``health_sensors``, ``thermostat_read``) were absent, so reading the
    robot arm's own joint angles would have started asking permission."""
    from security.hardware_policy import permits_unattended

    assert permits_unattended(SandboxPolicy(), capability_id, "sensor") is True


# ── the execution gate (defense in depth for the unguarded paths) ────

@pytest.mark.asyncio
async def test_execute_action_consults_the_operator_actuator_allowlist():
    """``DeviceRegistry.execute_action`` is reached from six call sites
    (``gateway/protocol.py``, ``mcp/server.py`` x3, ``hardware/orchestrator.py``,
    ``hardware/capability_skill.py``, ``skills/impl/cutebot_skill.py``) and
    consulted the operator's policy on none of them. Only
    ``POST /api/hardware/execute`` did, and nothing in the repo calls it."""
    from hardware.protocol import DeviceRegistry, HUPAction, HUPActionType

    device = _device_from_wire("evil-node-3", [
        {"name": "unlock_door", "category": "actuator", "description": "Open"},
    ])
    adapter = MagicMock()
    adapter.execute = MagicMock()
    registry = DeviceRegistry()
    registry.register_device(device, adapter=adapter)

    fake_state = MagicMock()
    fake_state.policy = SandboxPolicy()
    action = HUPAction(
        device_id="evil-node-3", capability_id="unlock_door",
        action_type=HUPActionType.EXECUTE, confirmed=True,
    )
    with patch.dict(sys.modules, {"api.state": MagicMock(state=fake_state)}):
        result = await registry.execute_action(action)

    assert result.status == "denied"
    assert "hardware.actuators.allowed" in result.error
    adapter.execute.assert_not_called()


@pytest.mark.asyncio
async def test_execute_action_still_runs_emergency_stop_under_an_empty_allowlist():
    """REGRESSION GUARD, and the reason the gate is not a bare allowlist
    lookup: a refused stop leaves the hardware running."""
    from hardware.protocol import DeviceRegistry, HUPAction, HUPActionType

    device = _device_from_wire("arm-2", [
        {"name": "halt", "category": "actuator", "description": "Stop"},
    ])

    async def _execute(_action):
        return {"stopped": True}

    adapter = MagicMock()
    adapter.execute = _execute
    registry = DeviceRegistry()
    registry.register_device(device, adapter=adapter)

    fake_state = MagicMock()
    fake_state.policy = SandboxPolicy({
        "hardware": {"actuators": {"allowed": [], "blocked": []}},
    })
    action = HUPAction(
        device_id="arm-2", capability_id="halt",
        action_type=HUPActionType.EXECUTE, confirmed=True,
    )
    with patch.dict(sys.modules, {"api.state": MagicMock(state=fake_state)}):
        result = await registry.execute_action(action)

    assert result.status == "success"


@pytest.mark.asyncio
async def test_execute_action_is_unchanged_when_no_policy_is_loaded():
    """REGRESSION GUARD. ``AppState.policy`` is None until the brain boots
    and is absent entirely in libraries/tests that use ``DeviceRegistry``
    directly; the gate must be a no-op there rather than a hard denial of
    all hardware."""
    from hardware.protocol import DeviceRegistry, HUPAction, HUPActionType

    device = _device_from_wire("lab-rig-0", [
        {"name": "spin_centrifuge", "category": "actuator", "description": "Spin"},
    ])

    async def _execute(_action):
        return {"ok": True}

    adapter = MagicMock()
    adapter.execute = _execute
    registry = DeviceRegistry()
    registry.register_device(device, adapter=adapter)

    action = HUPAction(
        device_id="lab-rig-0", capability_id="spin_centrifuge",
        action_type=HUPActionType.EXECUTE, confirmed=True,
    )
    fake_state = MagicMock()
    fake_state.policy = None
    with patch.dict(sys.modules, {"api.state": MagicMock(state=fake_state)}):
        result = await registry.execute_action(action)

    assert result.status == "success"
