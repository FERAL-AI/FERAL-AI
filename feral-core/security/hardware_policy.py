"""The operator's answer to "may this hardware capability run, and unattended?"

One module, because three layers need the same answer and were each free
to invent their own (or, in two of the three cases, to not ask at all):

* ``security/safety_resolver``: will the operator SEE an approval card
  before an auto-generated device tool fires?
* ``hardware/protocol.DeviceRegistry.execute_action``: may the action
  reach the adapter at all? Six call sites reach this and none consulted
  a policy.
* ``api/routes/security_and_hardware._policy_verdict``: the pre-existing
  check on ``POST /api/hardware/execute``, which is the only place any of
  this was wired and which nothing in the repo invokes.

**Why the operator and not the device.** A HUP capability arrives as a
JSON object the device wrote: ``{"name": ..., "category": ...,
"permission_tier": ..., "requires_confirmation": ...}``.
``hardware/protocol.device_capability_from_action`` fills the missing
fields with ``category="actuator"``, ``permission_tier="passive"``,
``requires_confirmation=False``, and
``hardware/capability_skill._safety_for`` maps that to ``safety_tier:
safe``. Every field in that chain is device-supplied, so no rule written
against those fields can distinguish a device telling the truth from a
device that wants to skip the prompt. The one party in the system that is
not the device is the operator, and they already have the levers:
``hardware.sensors.allowed``, ``hardware.actuators.allowed``,
``hardware.actuators.requires_confirmation``, ``hardware.cameras.allowed``
and ``hardware.movement.emergency_stop_enabled``.

**The constraint that shapes this.** The obvious rule, "actuators always
confirm", is wrong. ``halt`` is the CuteBot's emergency stop
(``hardware/adapters/cutebot.py`` declares it ``permission_tier="passive"``;
``skills/manifests/cutebot.json`` declares it ``safety_tier: safe``).
Prompting before a stop leaves hardware moving while the operator reads a
dialog, which is a worse outcome than the action the prompt was meant to
prevent. ``SandboxPolicy.emergency_stop_enabled`` already exists to say
so, and :data:`EMERGENCY_STOP_IDS` is the spelling list, so the carve-out
is an operator switch rather than something a device can assert. That is
the whole trade: a device may *name* a capability ``halt`` and get the
carve-out, but naming it ``unlock_door`` and calling it passive buys
nothing. Trusting a name is a much smaller surface than trusting a
self-declared tier, and it fails in the safe direction. The worst case
is an extra unattended stop, not an extra unattended actuation.

Nothing here imports from the rest of FERAL: ``hardware.protocol`` and
``security.safety_resolver`` both sit near the bottom of the import graph
and a repo-level import here would close a cycle. ``policy`` is duck-typed
as "something with the ``SandboxPolicy`` methods" for the same reason.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Optional

logger = logging.getLogger("feral.security.hardware_policy")

# Capability ids that mean "take a picture". ``hardware/protocol.py``'s
# reference glasses manifest calls it ``capture_photo`` and
# ``hardware/mesh.py``'s phone node calls it ``camera_snap``; both declare
# ``category="sensor"`` but neither uses the ``read_`` prefix, so the
# sensor allowlist never saw them. The suffix/substring forms cover
# self-describing third-party devices that pick their own spelling.
CAMERA_CAPTURE_IDS: frozenset = frozenset({
    "capture_photo", "camera_snap", "capture_image", "take_photo", "snapshot",
})
CAMERA_CAPTURE_TOKENS: tuple = ("camera", "photo")

# Capability categories that drive something physical. ``sensor`` is read
# only and handled by the sensor allowlist; ``network`` / ``compute`` are
# not actuation.
ACTUATOR_CATEGORIES: frozenset = frozenset({"actuator", "display", "audio"})

# Capability ids that stop hardware. See ``SandboxPolicy.emergency_stop_enabled``.
EMERGENCY_STOP_IDS: frozenset = frozenset({"halt", "estop", "emergency_stop", "stop"})


def is_camera_capture(capability_id: str) -> bool:
    cap = (capability_id or "").lower()
    if cap in CAMERA_CAPTURE_IDS:
        return True
    if cap.startswith("camera_") or cap.endswith("_camera"):
        return True
    return "capture" in cap and any(token in cap for token in CAMERA_CAPTURE_TOKENS)


def is_emergency_stop(capability_id: str, policy: Any) -> bool:
    """Whether this capability is a stop the operator has exempted.

    Both halves are required: the id has to be one of the recognised stop
    spellings AND ``hardware.movement.emergency_stop_enabled`` has to be
    on (it is by default). An operator who does not want the carve-out
    turns it off and the allowlist governs stops like anything else.
    """
    if (capability_id or "").lower() not in EMERGENCY_STOP_IDS:
        return False
    try:
        return bool(policy.emergency_stop_enabled())
    except Exception:                                   # duck-typed policy
        return False


def _sensor_allowed(policy: Any, capability_id: str) -> bool:
    """``hardware.sensors.allowed`` membership, tolerating the ``read_`` prefix.

    The shipped allowlist is written in bare sensor names (``heart_rate``)
    while adapters send both bare (``hardware/adapters/wristband.py``) and
    prefixed (``read_heart_rate``, ``hardware/protocol.py``) capability
    ids. Accept either spelling rather than making the operator guess
    which adapter they are configuring.
    """
    cap = capability_id or ""
    if policy.can_read_sensor(cap):
        return True
    if cap.startswith("read_") and policy.can_read_sensor(cap[len("read_"):]):
        return True
    return False


def capability_refusal(
    policy: Any, capability_id: str, category: str = "",
) -> Optional[str]:
    """``None`` when the operator's policy permits this capability to RUN.

    Otherwise an operator-facing reason naming the policy key to edit.
    "Blocked by sandbox policy" with no further detail was the original
    message and gave nobody anything to act on, when the fix is always a
    named list in ``~/.feral/policies/default.yaml``.

    This is the "may it happen at all?" question, and it is deliberately
    scoped to the categories that actuate. ``network`` / ``compute``
    capabilities have no allowlist of their own, so gating them here would
    deny things an operator has no key to permit; a device that mislabels
    an actuator as ``network`` to slip past this is caught by
    :func:`permits_unattended` instead, which asks the *stricter* question
    and so can afford to be default-deny.

    Lifted verbatim (bar the ``read_`` tolerance noted in
    :func:`_sensor_allowed`, which only ever permits more) out of
    ``api/routes/security_and_hardware._policy_verdict``, where it was the
    sole reader of the hardware half of ``SandboxPolicy`` and sat behind an
    endpoint nothing in the repo calls.
    """
    if policy is None:
        return None
    capability_id = capability_id or ""
    cat = (category or "").lower()

    if capability_id.startswith("read_") or cat == "sensor":
        if is_camera_capture(capability_id):
            if not policy.can_capture_camera():
                return "hardware.cameras.allowed is false"
            return None
        if not _sensor_allowed(policy, capability_id):
            return "sensor is not in hardware.sensors.allowed"
        return None

    if is_camera_capture(capability_id):
        if not policy.can_capture_camera():
            return "hardware.cameras.allowed is false"
        return None

    if cat and cat not in ACTUATOR_CATEGORIES:
        return None

    if is_emergency_stop(capability_id, policy):
        return None

    allowed, _needs_confirm = policy.can_use_actuator(capability_id)
    if not allowed:
        return "actuator is not in hardware.actuators.allowed"
    return None


def permits_unattended(policy: Any, capability_id: str, category: str = "") -> bool:
    """Whether this capability may run with NO approval card.

    Strictly stronger than :func:`capability_refusal`, in two ways.

    First it is default-deny across categories. ``category`` is a string
    the device chose, so a rule keyed on ``category == "actuator"`` is
    evaded by writing ``network``; here anything the operator has not
    named in *some* allowlist is a prompt.

    Second, allowed is not the same as unattended:
    ``hardware.actuators.requires_confirmation`` is the operator's
    "allowed, but ask me" lever, carried by the second element of
    ``can_use_actuator``'s return value, which its one call site discarded.

    Fails closed on a missing policy. ``AppState.policy`` is ``None`` until
    ``AppState.initialize`` runs, and "the operator has not answered yet"
    must not read as "the operator said yes". An extra prompt during
    startup is the cheap failure.
    """
    if policy is None:
        return False
    if capability_refusal(policy, capability_id, category) is not None:
        return False

    capability_id = capability_id or ""
    cat = (category or "").lower()
    if capability_id.startswith("read_") or cat == "sensor" or is_camera_capture(capability_id):
        # A read the operator's sensor/camera allowlist already permitted.
        return True
    if is_emergency_stop(capability_id, policy):
        return True

    allowed, needs_confirm = policy.can_use_actuator(capability_id)
    return bool(allowed) and not needs_confirm


def live_policy() -> Any:
    """The running brain's ``SandboxPolicy``, or ``None`` outside one.

    ``sys.modules`` rather than an import, for the reason
    ``security/safety_resolver._is_live_device_skill`` gives for the same
    trick: ``api.state`` imports the orchestrator, which imports the
    modules that call this, and importing it here would both invert the
    dependency and stand up the whole brain inside unit tests.
    """
    state_module = sys.modules.get("api.state")
    if state_module is None:
        return None
    try:
        return getattr(getattr(state_module, "state", None), "policy", None)
    except Exception as exc:                            # state mid-swap
        logger.debug("live policy lookup failed: %s", exc)
        return None


__all__ = [
    "ACTUATOR_CATEGORIES",
    "CAMERA_CAPTURE_IDS",
    "EMERGENCY_STOP_IDS",
    "capability_refusal",
    "is_camera_capture",
    "is_emergency_stop",
    "live_policy",
    "permits_unattended",
]
