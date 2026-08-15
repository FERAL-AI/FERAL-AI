"""``POST /api/policy/update`` must validate before it persists.

The pre-fix handler was three lines with no validation at all::

    state.policy = SandboxPolicy(body); state.policy.save(); return {"ok": True}

Any JSON object replaced the live sandbox policy and the caller was told
it worked. ``SandboxPolicy`` reads every field through ``.get(default)``,
so a wrong type never raises: it silently selects a default, and several
of those defaults are permissive. The cases below pin the ones that
matter, each named for the enforcement it silently disables.

Scope note: these tests assert the *route's* contract. They do not require
a policy document to be complete, because ``tests/test_sandbox_policy.py::
test_custom_policy`` builds a valid partial policy and the validator must
not contradict the class.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from config.loader import feral_home
from security.sandbox_policy import SandboxPolicy


@pytest.fixture
def client(isolate_feral_home):
    """A TestClient with ``state`` mocked and a policy already installed.

    ``isolate_feral_home`` (autouse in conftest) already points FERAL_HOME
    at a tmp dir, so ``SandboxPolicy.save()`` writes there. Naming it as a
    parameter rather than setting FERAL_HOME again keeps this fixture out
    of the conftest env-leak warning.
    """
    mock = MagicMock()
    mock.policy = SandboxPolicy()
    with patch("api.routes.security_and_hardware.state", mock):
        from api.server import app
        yield TestClient(app, raise_server_exceptions=False), mock


def _post(c, body):
    return c.post("/api/policy/update", json=body)


def _policy_file() -> "object":
    return feral_home() / "policies" / "default.json"


# ── the happy path is a real round-trip, not a rubber stamp ──────


def test_shipped_default_policy_is_accepted(client):
    """The document GET /api/policy hands the editor must save unchanged.

    If the validator rejected the brain's own default, the editor would
    be unusable and the schema would be wrong rather than the document.
    """
    # Imported inside the test on purpose: the rest of this module asserts
    # HTTP behaviour and must stay collectable against a build that has no
    # validator, so a missing one shows up as failing tests rather than one
    # collection error that hides them all.
    from api.routes.security_and_hardware import validate_policy_document

    c, _ = client
    assert validate_policy_document(SandboxPolicy().to_dict()) == []
    r = _post(c, SandboxPolicy().to_dict())
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}


def test_valid_policy_is_persisted_and_swapped_in(client):
    c, state_mock = client
    doc = SandboxPolicy().to_dict()
    doc["network"]["allowed_domains"] = ["api.example.com"]

    r = _post(c, doc)
    assert r.status_code == 200, r.text

    saved = json.loads(_policy_file().read_text())
    assert saved["network"]["allowed_domains"] == ["api.example.com"]
    # The live policy is the one that was written, not a half-applied dict.
    assert state_mock.policy.to_dict()["network"]["allowed_domains"] == ["api.example.com"]


def test_partial_policy_still_accepted(client):
    """tests/test_sandbox_policy.py::test_custom_policy shape must pass.

    Omitting ``filesystem`` / ``memory`` / ``daemon`` / ``wasm`` is legal
    at the class level, so the route must not invent a required-section
    schema that contradicts it.
    """
    c, _ = client
    r = _post(c, {
        "version": "1.0",
        "name": "restrictive",
        "permissions": {"max_tier": "passive", "require_confirmation_above": "passive"},
        "network": {"mode": "denylist", "blocked_domains": ["evil.com"]},
        "hardware": {
            "sensors": {"allowed": ["heart_rate"], "blocked": ["gps"]},
            "actuators": {"allowed": [], "blocked": [], "requires_confirmation": []},
            "cameras": {"allowed": False},
            "movement": {"max_speed_pct": 10},
        },
        "skills": {"allow_generation": False, "require_approval": True, "blocked_skill_ids": []},
        "mcp": {"allow_external_servers": False},
        "execution": {"allow_shell_commands": False, "max_tool_calls_per_turn": 5},
    })
    assert r.status_code == 200, r.text


# ── rejections, each naming the check it would have disabled ─────


@pytest.mark.parametrize(
    "body,field",
    [
        # A truthy string read through ``.get()`` turns the shell gate ON.
        ({"execution": {"allow_shell_commands": "false"}}, "execution.allow_shell_commands"),
        ({"execution": {"allow_shell_commands": 1}}, "execution.allow_shell_commands"),
        # ``can_access_domain`` enforces the allowlist only when mode is
        # exactly "allowlist"; any other string means allow-everything.
        ({"network": {"mode": "allow-list"}}, "network.mode"),
        ({"network": {"mode": "allowlist!"}}, "network.mode"),
        # ``applescript_denied_phrases()`` returns [] for a non-list, which
        # re-enables ``do shell script`` on daemon://local/applescript.
        ({"daemon": {"applescript": {"denied_phrases": "do shell script"}}},
         "daemon.applescript.denied_phrases"),
        # ``daemon_shell_allowlist()`` returns [] for a non-list.
        ({"daemon": {"shell": {"allowed_commands": "open"}}}, "daemon.shell.allowed_commands"),
        # ``tier_level()`` maps an unknown tier to 0 rather than raising.
        ({"permissions": {"max_tier": "root"}}, "permissions.max_tier"),
        ({"permissions": {"require_confirmation_above": "ACTIVE"}},
         "permissions.require_confirmation_above"),
        # Path("").expanduser().resolve() is the process CWD, so a blank
        # entry silently grants the working directory.
        ({"filesystem": {"read_paths": [""]}}, "filesystem.read_paths[0]"),
        ({"filesystem": {"write_paths": ["~/ok", "   "]}}, "filesystem.write_paths[1]"),
        # Lists that are not lists read back empty.
        ({"network": {"allowed_domains": "api.openai.com"}}, "network.allowed_domains"),
        ({"hardware": {"sensors": {"blocked": "gps"}}}, "hardware.sensors.blocked"),
        # Sections must be objects, or ``.get`` blows up at enforcement time.
        ({"network": "allowlist"}, "network"),
        ({"hardware": {"sensors": []}}, "hardware.sensors"),
        # Ranges the class documents in its own defaults.
        ({"hardware": {"movement": {"max_speed_pct": 400}}}, "hardware.movement.max_speed_pct"),
        ({"hardware": {"movement": {"max_speed_pct": -1}}}, "hardware.movement.max_speed_pct"),
        ({"execution": {"max_tool_calls_per_turn": 0}}, "execution.max_tool_calls_per_turn"),
        # A typo'd key is the realistic way an operator disables a check.
        ({"netwrok": {"mode": "allowlist"}}, "netwrok"),
        ({"network": {"allowd_domains": []}}, "network.allowd_domains"),
    ],
)
def test_rejects_with_400_naming_the_field(client, body, field):
    c, _ = client
    r = _post(c, body)
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "invalid_policy"
    assert detail["field"] == field
    assert field.split("[")[0] in detail["message"]


def test_rejects_a_deny_pattern_the_safe_compiler_refuses(client):
    """``denied_command_patterns()`` drops a bad rule with a log line.

    An operator who saves ``(a+)+`` therefore gets a green chip and a deny
    rule that is not running. Refuse at the door instead.
    """
    c, _ = client
    r = _post(c, {"execution": {"denied_command_patterns": ["(a+)+$"]}})
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["field"] == "execution.denied_command_patterns[0]"


def test_error_lists_every_problem_not_just_the_first(client):
    c, _ = client
    r = _post(c, {
        "network": {"mode": "nope"},
        "execution": {"allow_shell_commands": "yes"},
    })
    assert r.status_code == 400
    detail = r.json()["detail"]
    fields = {e["field"] for e in detail["errors"]}
    assert fields == {"network.mode", "execution.allow_shell_commands"}
    assert "1 more problem" in detail["message"]


# ── a rejected document must not touch anything ──────────────────


def test_rejected_policy_is_neither_persisted_nor_applied(client):
    c, state_mock = client
    before = state_mock.policy
    r = _post(c, {"execution": {"allow_shell_commands": "false"}})
    assert r.status_code == 400
    assert state_mock.policy is before
    assert not _policy_file().exists()


def test_a_document_the_class_would_silently_widen_is_refused(client):
    """End-to-end statement of the defect.

    ``{"hardware": {"sensors": {"allowed": "heart_rate"}}}`` used to save
    with ``{"ok": true}``. ``can_read_sensor`` then read ``allowed`` back
    as a string, ``sensor_type in allowed`` did a substring test, and
    ``not allowed`` was False, so the sensor allowlist silently changed
    meaning. The route must refuse it.
    """
    c, state_mock = client
    before = state_mock.policy
    r = _post(c, {"hardware": {"sensors": {"allowed": "heart_rate"}}})
    assert r.status_code == 400
    assert r.json()["detail"]["field"] == "hardware.sensors.allowed"
    assert state_mock.policy is before
