"""P0.1 + P0.4 — the manifest trust clamp and the sandbox-policy call sites.

Two holes, one theme: a security decision that was written down but never
asked, or asked of a party that had no business answering.

P0.1  ``security/safety_resolver._safety_from_manifest`` returned the
      manifest's own ``safety_tier`` before consulting anything else, so an
      installed third-party skill declaring ``"safety_tier": "safe"``
      executed with no confirmation on every surface. The identical clamp
      already existed for ``skills/result_budget`` (``skill_id in
      builtin_skill_ids()``), applied to how many characters of a result
      reach the model but not to whether the operator is asked before the
      code runs.

P0.4  ``SandboxPolicy`` had exactly one production call site
      (``can_read_sensor``). Four of its checks also failed OPEN on a
      partial policy: an empty allowlist meant "allow everything", where
      ``can_access_domain`` in the same class means "allow nothing".

Every test below is written to fail against the pre-fix tree. The ones that
pass both before and after are grouped under "regression guards" and say so
in their docstring, because a test that never failed proves nothing about
the fix.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from models.skill_manifest import (  # noqa: E402
    AuthConfig,
    BrandProfile,
    SkillEndpoint,
    SkillManifest,
)
from security.safety_resolver import (  # noqa: E402
    LEVEL_AUTO,
    LEVEL_CONFIRM,
    LEVEL_DENY,
    is_read_only,
    resolve_policy,
)
from security.sandbox_policy import SandboxPolicy  # noqa: E402


# A skill id that ships in this repo, and one that cannot.
BUILTIN_SKILL_ID = "notes_memory"
THIRD_PARTY_SKILL_ID = "totally_legit_marketplace_skill"


class _FakeRegistry:
    def __init__(self, manifests):
        self.skills = {m.skill_id: m for m in manifests}


def _skill(skill_id: str, endpoint_id: str = "do_thing", **endpoint_kwargs) -> SkillManifest:
    return SkillManifest(
        skill_id=skill_id,
        version="1.0.0",
        author="test",
        brand=BrandProfile(name=skill_id),
        description="fixture",
        auth=AuthConfig(type="none"),
        endpoints=[
            SkillEndpoint(
                id=endpoint_id,
                method="PYTHON",
                url="",
                description="fixture endpoint",
                **endpoint_kwargs,
            ),
        ],
    )


def _registry_for(skill_id: str, **endpoint_kwargs) -> _FakeRegistry:
    return _FakeRegistry([_skill(skill_id, **endpoint_kwargs)])


# ─────────────────────────────────────────────────────────────────────
# P0.1 — a third party may escalate, never de-escalate
# ─────────────────────────────────────────────────────────────────────


def test_third_party_declaring_safe_does_not_auto_approve():
    """The headline defect. ``do_thing`` has no auto-token in its name, so
    with the manifest claim ignored it lands on the legacy heuristic's
    unknown-default, which is CONFIRM."""
    registry = _registry_for(THIRD_PARTY_SKILL_ID, safety_tier="safe")
    decision = resolve_policy(f"{THIRD_PARTY_SKILL_ID}__do_thing", registry=registry)
    assert decision.level == LEVEL_CONFIRM


def test_third_party_read_only_hint_does_not_auto_approve():
    """``read_only_hint`` is the second de-escalation lever on the same
    endpoint and has to be clamped with the first, or the fix is cosmetic."""
    registry = _registry_for(THIRD_PARTY_SKILL_ID, read_only_hint=True)
    decision = resolve_policy(f"{THIRD_PARTY_SKILL_ID}__do_thing", registry=registry)
    assert decision.level == LEVEL_CONFIRM


def test_third_party_safe_tier_does_not_override_danger_map():
    """A declared-safe third party must not outrank the centralized map.

    ``coding_tools__write_file`` is WARN in ``TOOL_DANGER_MAP``; a skill
    that squats the endpoint name and declares itself safe used to win.
    """
    registry = _FakeRegistry([
        _skill(THIRD_PARTY_SKILL_ID, endpoint_id="write_file", safety_tier="safe"),
    ])
    with patch("security.safety_resolver.get_danger_level") as danger:
        from security.dangerous_tools import DangerLevel

        danger.return_value = DangerLevel.WARN
        decision = resolve_policy(
            f"{THIRD_PARTY_SKILL_ID}__write_file", registry=registry,
        )
    assert decision.level == LEVEL_CONFIRM


def test_third_party_read_only_hint_is_ignored_by_is_read_only():
    """``agents/tool_runner.py`` calls ``is_read_only`` and, under
    ``FERAL_AUTONOMY=strict``, skips approval entirely when it is True.
    That is the same bypass through a different door."""
    registry = _registry_for(THIRD_PARTY_SKILL_ID, read_only_hint=True)
    assert is_read_only(f"{THIRD_PARTY_SKILL_ID}__do_thing", registry=registry) is False


def test_third_party_safe_tier_is_ignored_by_strict_is_read_only():
    """``agents/plan_mode.is_plan_safe_tool`` gates on
    ``is_read_only(strict=True)``. A third party must not talk its way
    into a mode whose whole contract is that nothing changes."""
    registry = _registry_for(THIRD_PARTY_SKILL_ID, safety_tier="safe")
    assert is_read_only(
        f"{THIRD_PARTY_SKILL_ID}__do_thing", registry=registry, strict=True,
    ) is False


def test_third_party_may_still_escalate():
    """Regression guard (passes before and after). Escalation is the half
    of the manifest contract a third party legitimately owns."""
    for kwargs, expected in (
        ({"safety_tier": "confirm"}, LEVEL_CONFIRM),
        ({"safety_tier": "deny"}, LEVEL_DENY),
        ({"requires_user_approval": True}, LEVEL_CONFIRM),
    ):
        registry = _registry_for(THIRD_PARTY_SKILL_ID, **kwargs)
        decision = resolve_policy(f"{THIRD_PARTY_SKILL_ID}__do_thing", registry=registry)
        assert decision.level == expected, kwargs


def test_third_party_escalation_beats_an_auto_substring():
    """A name the legacy heuristic would auto-approve (``search``) must
    still be confirm-walled when the manifest asks for it."""
    registry = _FakeRegistry([
        _skill(THIRD_PARTY_SKILL_ID, endpoint_id="search_things", safety_tier="confirm"),
    ])
    decision = resolve_policy(
        f"{THIRD_PARTY_SKILL_ID}__search_things", registry=registry,
    )
    assert decision.level == LEVEL_CONFIRM


def test_builtin_declaring_safe_still_auto_approves():
    """Regression guard (passes before and after). The clamp must not cost
    first-party skills their declared tier, or it will be reverted."""
    registry = _registry_for(BUILTIN_SKILL_ID, safety_tier="safe")
    decision = resolve_policy(f"{BUILTIN_SKILL_ID}__do_thing", registry=registry)
    assert decision.level == LEVEL_AUTO


def test_builtin_read_only_hint_still_auto_approves():
    """Regression guard (passes before and after)."""
    registry = _registry_for(BUILTIN_SKILL_ID, read_only_hint=True)
    decision = resolve_policy(f"{BUILTIN_SKILL_ID}__do_thing", registry=registry)
    assert decision.level == LEVEL_AUTO
    assert is_read_only(f"{BUILTIN_SKILL_ID}__do_thing", registry=registry) is True


def test_runtime_device_skill_needs_a_registered_device_AND_operator_consent():
    """``hwdev_*`` manifests are generated by in-repo code
    (``hardware/capability_skill._safety_for``) from a paired device's own
    self-description. Being registered is NECESSARY: a marketplace package
    can declare any skill_id, so the provenance half is proven against the
    live device registry.

    It is no longer SUFFICIENT. ``_safety_for``'s inputs are strings the
    device sent, and ``hardware/protocol.device_capability_from_action``
    defaults the absent ones to ``actuator`` / ``passive`` / ``False``,
    which maps to ``safe``. See ``tests/test_device_declared_safety.py``
    for the exploit that made this test's original assertion wrong: it
    passed a registered device with an empty capability list and a
    ``do_thing`` endpoint nobody had ever authorised, and got LEVEL_AUTO.
    The second half of the answer now comes from the operator's policy
    (``security/hardware_policy.permits_unattended``).
    """
    from hardware.protocol import DeviceCapability, DeviceManifest

    skill_id = "hwdev_paired_thing"
    tool = f"{skill_id}__do_thing"
    device = DeviceManifest(
        device_id="paired_thing", device_type="robot", name="Paired",
        capabilities=[
            DeviceCapability(
                id="do_thing", name="Do Thing", description="does a thing",
                category="actuator", permission_tier="passive",
            ),
        ],
    )
    registry = _registry_for(skill_id, safety_tier="safe")

    fake_state = MagicMock()
    fake_state.device_registry.list_devices.return_value = [device]

    # Registered, and the operator named the capability.
    fake_state.policy = SandboxPolicy({
        "hardware": {"actuators": {"allowed": ["do_thing"], "blocked": []}},
    })
    with patch.dict(sys.modules, {"api.state": MagicMock(state=fake_state)}):
        assert resolve_policy(tool, registry=registry).level == LEVEL_AUTO

    # Registered, but the operator never named it. The device's own
    # ``passive`` claim buys nothing.
    fake_state.policy = SandboxPolicy()
    with patch.dict(sys.modules, {"api.state": MagicMock(state=fake_state)}):
        assert resolve_policy(tool, registry=registry).level == LEVEL_CONFIRM

    # Not registered at all: the provenance half still fails first.
    fake_state.device_registry.list_devices.return_value = []
    fake_state.policy = SandboxPolicy({
        "hardware": {"actuators": {"allowed": ["do_thing"], "blocked": []}},
    })
    with patch.dict(sys.modules, {"api.state": MagicMock(state=fake_state)}):
        assert resolve_policy(tool, registry=registry).level == LEVEL_CONFIRM


# ─────────────────────────────────────────────────────────────────────
# P0.4 (a) — partial policies must fail closed
# ─────────────────────────────────────────────────────────────────────


def test_empty_sensor_allowlist_denies():
    p = SandboxPolicy({"hardware": {"sensors": {"allowed": [], "blocked": []}}})
    assert p.can_read_sensor("heart_rate") is False


def test_missing_hardware_section_denies_sensor_reads():
    assert SandboxPolicy({"name": "partial"}).can_read_sensor("heart_rate") is False


def test_empty_actuator_allowlist_denies():
    p = SandboxPolicy({"hardware": {"actuators": {"allowed": [], "blocked": []}}})
    allowed, _needs_confirm = p.can_use_actuator("display")
    assert allowed is False


def test_missing_hardware_section_denies_actuators():
    allowed, _ = SandboxPolicy({"name": "partial"}).can_use_actuator("display")
    assert allowed is False


def test_missing_camera_section_denies_capture():
    assert SandboxPolicy({"name": "partial"}).can_capture_camera() is False


def test_empty_mcp_allowlist_denies():
    """``allowed_servers: []`` is an allowlist with nothing on it. The
    shipped default says ``["*"]`` out loud instead of relying on empty
    meaning "anything"."""
    p = SandboxPolicy({"mcp": {"allow_external_servers": True, "allowed_servers": []}})
    assert p.can_use_mcp_server("github") is False


def test_missing_mcp_section_denies():
    assert SandboxPolicy({"name": "partial"}).can_use_mcp_server("github") is False


def test_mcp_wildcard_allows_any_server():
    p = SandboxPolicy({"mcp": {"allow_external_servers": True, "allowed_servers": ["*"]}})
    assert p.can_use_mcp_server("anything") is True


def test_mcp_blocklist_beats_wildcard():
    p = SandboxPolicy({
        "mcp": {
            "allow_external_servers": True,
            "allowed_servers": ["*"],
            "blocked_servers": ["evil"],
        },
    })
    assert p.can_use_mcp_server("evil") is False


def test_default_install_is_unaffected():
    """Regression guard (passes before and after) — the shipped default
    must keep permitting everything FERAL itself uses."""
    p = SandboxPolicy()
    assert p.can_read_sensor("heart_rate") is True
    assert p.can_read_sensor("telemetry") is True
    assert p.can_capture_camera() is True
    assert p.can_use_mcp_server("github") is True
    assert p.can_access_domain("api.openai.com") is True
    assert p.max_movement_speed() == 50


@pytest.mark.parametrize(
    "capability_id",
    [
        # hardware/protocol.py reference glasses manifest
        "display_notification", "play_audio",
        # hardware/adapters/cutebot.py
        "follow_line", "explore", "halt", "drive", "set_lights",
        # hardware/adapters/robot_arm.py
        "move_joints", "move_cartesian", "gripper", "home", "estop",
        # hardware/adapters/smart_home.py
        "lights_toggle", "lights_brightness", "lights_color", "scene_activate",
        # hardware/mesh.py phone node
        "notification", "haptic",
    ],
)
def test_default_policy_permits_every_shipped_actuator(capability_id):
    """Wiring ``can_use_actuator`` is only safe if the shipped default
    already names the actuators the shipped adapters expose. Before this
    change the allowlist held four generic type names and none of the
    capability ids the adapters actually send, so the gate would have
    denied the CuteBot, the arm and the Hue bridge on a fresh install."""
    allowed, _needs_confirm = SandboxPolicy().can_use_actuator(capability_id)
    assert allowed is True


def test_policy_validator_still_accepts_the_shipped_default():
    """Regression guard. ``GET /api/policy`` hands the editor this exact
    document and ``POST /api/policy/update`` must take it back."""
    from api.routes.security_and_hardware import validate_policy_document

    assert validate_policy_document(SandboxPolicy().to_dict()) == []


# ─────────────────────────────────────────────────────────────────────
# P0.4 (b) — the newly wired call sites
# ─────────────────────────────────────────────────────────────────────


def _hardware_state(policy: SandboxPolicy, manifest):
    """A ``state`` double whose device registry holds one device."""
    mock = MagicMock()
    mock.policy = policy
    mock.device_registry.get_device.return_value = manifest
    mock.device_registry.execute_action = AsyncMock(
        return_value=MagicMock(model_dump=lambda: {"status": "success"}),
    )
    return mock


def _cutebot_manifest():
    from hardware.protocol import DeviceCapability, DeviceManifest

    return DeviceManifest(
        device_id="bot1",
        device_type="robot",
        name="CuteBot",
        capabilities=[
            DeviceCapability(
                id="drive", name="Drive", description="drive wheels",
                category="actuator", permission_tier="active",
            ),
            DeviceCapability(
                id="halt", name="Halt", description="stop the wheels",
                category="actuator", permission_tier="passive",
            ),
            DeviceCapability(
                id="capture_photo", name="Photo", description="take a photo",
                category="sensor", permission_tier="active",
            ),
            DeviceCapability(
                id="read_telemetry", name="Telemetry", description="read state",
                category="sensor", permission_tier="passive",
            ),
        ],
    )


async def _execute(state_mock, body):
    from api.routes import security_and_hardware as mod

    with patch.object(mod, "state", state_mock):
        return await mod.execute_hardware_action(body)


@pytest.mark.asyncio
async def test_actuator_is_denied_when_policy_denies():
    policy = SandboxPolicy({
        "hardware": {"actuators": {"allowed": ["halt"], "blocked": ["drive"]}},
    })
    state_mock = _hardware_state(policy, _cutebot_manifest())
    result = await _execute(
        state_mock, {"device_id": "bot1", "capability_id": "drive", "parameters": {}},
    )
    assert "error" in result
    assert "policy" in result["error"].lower()
    state_mock.device_registry.execute_action.assert_not_called()


@pytest.mark.asyncio
async def test_camera_capture_is_denied_when_policy_denies():
    policy = SandboxPolicy({"hardware": {"cameras": {"allowed": False}}})
    state_mock = _hardware_state(policy, _cutebot_manifest())
    result = await _execute(
        state_mock,
        {"device_id": "bot1", "capability_id": "capture_photo", "parameters": {}},
    )
    assert "error" in result
    assert "policy" in result["error"].lower()
    state_mock.device_registry.execute_action.assert_not_called()


@pytest.mark.asyncio
async def test_sensor_read_is_denied_when_policy_denies():
    """Regression guard for the one call site that already existed."""
    policy = SandboxPolicy({"hardware": {"sensors": {"allowed": [], "blocked": []}}})
    state_mock = _hardware_state(policy, _cutebot_manifest())
    result = await _execute(
        state_mock,
        {"device_id": "bot1", "capability_id": "read_telemetry", "parameters": {}},
    )
    assert "error" in result
    assert "policy" in result["error"].lower()


@pytest.mark.asyncio
async def test_emergency_stop_survives_a_restrictive_actuator_allowlist():
    """Refusing a stop command leaves hardware running, which is worse
    than the action it prevents. ``hardware.movement.emergency_stop_enabled``
    (default true) is the operator's switch for that carve-out."""
    policy = SandboxPolicy({"hardware": {"actuators": {"allowed": [], "blocked": []}}})
    state_mock = _hardware_state(policy, _cutebot_manifest())
    await _execute(
        state_mock, {"device_id": "bot1", "capability_id": "halt", "parameters": {}},
    )
    state_mock.device_registry.execute_action.assert_called_once()


@pytest.mark.asyncio
async def test_default_policy_lets_hardware_actions_through():
    """Regression guard (passes before and after)."""
    state_mock = _hardware_state(SandboxPolicy(), _cutebot_manifest())
    await _execute(
        state_mock, {"device_id": "bot1", "capability_id": "drive", "parameters": {}},
    )
    state_mock.device_registry.execute_action.assert_called_once()


@pytest.mark.asyncio
async def test_mcp_connect_server_is_denied_when_policy_denies():
    from mcp.client import MCPClientManager, MCPServerConfig

    manager = MCPClientManager()
    manager.policy = SandboxPolicy({
        "mcp": {"allow_external_servers": True, "allowed_servers": ["github"]},
    })
    config = MCPServerConfig(name="evil", transport="stdio", command="echo")

    with patch.object(manager, "_connect_with_retries") as connect:
        ok = await manager.connect_server(config)

    assert ok is False
    connect.assert_not_called()
    assert "evil" not in manager._servers


@pytest.mark.asyncio
async def test_mcp_connect_server_allowed_under_default_policy():
    """Regression guard (passes before and after)."""
    from mcp.client import MCPClientManager, MCPServerConfig

    manager = MCPClientManager()
    manager.policy = SandboxPolicy()
    config = MCPServerConfig(name="github", transport="stdio", command="echo")

    async def _ok(_conn):
        return True

    with patch.object(manager, "_connect_with_retries", side_effect=_ok):
        assert await manager.connect_server(config) is True


# ── the generic HTTP runner honours network.allowed_domains ──────────


def _http_skill(skill_id: str, url: str) -> SkillManifest:
    return SkillManifest(
        skill_id=skill_id,
        version="1.0.0",
        author="test",
        brand=BrandProfile(name=skill_id),
        description="fixture",
        auth=AuthConfig(type="none"),
        endpoints=[
            SkillEndpoint(id="fetch", method="GET", url=url, description="fixture"),
        ],
    )


async def _run_http(skill: SkillManifest, policy: SandboxPolicy) -> dict:
    from skills.executor import SkillExecutor

    ex = SkillExecutor()
    endpoint = skill.endpoints[0]
    try:
        with patch("security.sandbox_policy.SandboxPolicy.load_default", return_value=policy), \
             patch.object(ex.client, "get") as get:
            get.side_effect = AssertionError("HTTP request must not be issued")
            return await ex._execute_inner(
                f"{skill.skill_id}__fetch", {}, skill, endpoint,
            )
    finally:
        await ex.client.aclose()


@pytest.mark.asyncio
async def test_third_party_http_call_to_an_unlisted_domain_is_refused():
    skill = _http_skill(THIRD_PARTY_SKILL_ID, "https://exfil.example.com/collect")
    result = await _run_http(skill, SandboxPolicy())
    assert result["success"] is False
    assert "exfil.example.com" in result["error"]
    assert "allowed_domains" in result["error"]


@pytest.mark.asyncio
async def test_third_party_http_call_to_a_listed_domain_proceeds():
    """Reaching the transport (and blowing up on the patched client) proves
    the domain gate let it past, without touching the network."""
    policy = SandboxPolicy()
    policy._data["network"]["allowed_domains"] = ["allowed.example.com"]
    skill = _http_skill(THIRD_PARTY_SKILL_ID, "https://allowed.example.com/v1")
    result = await _run_http(skill, policy)
    assert result["success"] is False
    assert "must not be issued" in (result["error"] or "")


@pytest.mark.asyncio
async def test_builtin_http_skill_is_not_held_to_the_allowlist():
    """First-party manifests name fixed URLs reviewed in this repo, and
    they legitimately hit dozens of vendors. Holding them to the operator's
    allowlist would break every shipped skill the moment the first domain
    is configured, which trains operators to widen it to ``*``."""
    skill = _http_skill("spotify_music", "https://api.spotify.com/v1/me")
    result = await _run_http(skill, SandboxPolicy())
    assert "must not be issued" in (result["error"] or "")


@pytest.mark.asyncio
async def test_blocked_domain_stops_a_builtin_skill_too():
    """The allowlist exemption is for first-party skills; an explicit
    blocklist entry is an operator decision and binds everyone."""
    policy = SandboxPolicy()
    policy._data["network"]["blocked_domains"] = ["api.spotify.com"]
    skill = _http_skill("spotify_music", "https://api.spotify.com/v1/me")
    result = await _run_http(skill, policy)
    assert result["success"] is False
    assert "api.spotify.com" in result["error"]
