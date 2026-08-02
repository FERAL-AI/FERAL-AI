"""A declined tool call must not reach the UI looking like a crash.

Three gates refuse a call, each with its own envelope shape, and all
three arrive at the client as ``success: False``. Before
``Orchestrator._refusal_code`` existed, the web UI rendered every one of
them as a red "failed" tool card, identical to a tool that threw. A user
in plan mode saw what looked like a broken FERAL rather than FERAL
holding a boundary they had asked for.

The classifier is deliberately a closed set. A refusal shape it does not
recognise stays a failure, so a future gate that invents a fourth
envelope shows up loudly instead of being quietly softened into a
friendly amber card.
"""

import pytest

from agents.orchestrator import Orchestrator


REFUSALS = [
    pytest.param(
        {"success": False, "is_error": True, "error_code": "plan_mode_blocked",
         "error": "Plan mode is active for this session."},
        "plan_mode_blocked",
        id="plan-mode-carries-its-own-code",
    ),
    pytest.param(
        {"status": "PermissionOutcome::Deny", "safety_level": "deny",
         "error": "Safety Protocol: Action Blocked"},
        "policy_denied",
        id="surface-or-policy-deny",
    ),
    pytest.param(
        {"status": "pending_approval", "request_id": "abc",
         "safety_level": "confirm"},
        "pending_approval",
        id="strict-autonomy-awaiting-approval",
    ),
]

NOT_REFUSALS = [
    pytest.param({"success": True}, id="plain-success"),
    pytest.param({"success": False, "error": "connection reset"}, id="real-failure"),
    pytest.param({"success": False, "error_code": "rate_limited"},
                 id="an-unrecognised-code-stays-a-failure"),
    pytest.param({}, id="empty"),
    pytest.param({"status": "ok"}, id="unrelated-status"),
]


@pytest.mark.parametrize("result_data,expected", REFUSALS)
def test_a_refusal_is_classified(result_data, expected):
    assert Orchestrator._refusal_code(result_data) == expected


@pytest.mark.parametrize("result_data", NOT_REFUSALS)
def test_anything_else_is_not_a_refusal(result_data):
    """Empty string means "this ran", so the client keeps error styling."""
    assert Orchestrator._refusal_code(result_data) == ""


def test_deny_is_caught_from_either_field():
    """The deny envelope sets both; neither alone should be missed."""
    assert Orchestrator._refusal_code({"status": "PermissionOutcome::Deny"}) == "policy_denied"
    assert Orchestrator._refusal_code({"safety_level": "deny"}) == "policy_denied"


def test_the_wire_payload_carries_the_code():
    """A field the client reads is worthless if the model drops it."""
    from models.protocol import ToolResultPayload

    dumped = ToolResultPayload(
        tool="feral_reminders__create",
        success=False,
        error="Plan mode is active.",
        error_code="plan_mode_blocked",
    ).model_dump()
    assert dumped["error_code"] == "plan_mode_blocked"


def test_the_code_defaults_to_empty_so_old_callers_are_unchanged():
    from models.protocol import ToolResultPayload

    assert ToolResultPayload(tool="t").model_dump()["error_code"] == ""
