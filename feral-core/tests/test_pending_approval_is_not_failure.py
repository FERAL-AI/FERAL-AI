"""An approval prompt is not a failed tool call.

Live evidence (``~/.feral/memory.db``, execution_log, read 2026-08-12):

    workspace_scripts / run     0 successes, 9 failures
    agentic_computer_use        0 successes, 5 failures

Of those 14 "failures", 9 are ``{"status": "pending_approval", ...}``
envelopes. Nothing failed: FERAL asked the operator to approve a call.
Three consecutive ``workspace_scripts__rerun`` rows carry identical args
and three different ``request_id`` values, which is the model being told
its call failed and re-issuing it. The reported 0/9 success rate for
``workspace_scripts`` is therefore wrong in both directions: it counts
questions as failures, and it makes a working skill look dead.

Two places produced that: ``result_status="success" if tool_success else
"failure"``, and feeding ``tool_success=False`` to the no-progress guard.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.iteration_budget import GUARD_STOP, NoProgressGuard  # noqa: E402
from memory.execution_audit import status_of  # noqa: E402

PENDING = {
    "status": "pending_approval",
    "tool_name": "workspace_scripts__rerun",
    "args": {"script_id": "0b737753"},
    "request_id": "6fb79ffe",
    "session_id": "665d26fc",
    "safety_level": "confirm",
}


def test_a_pending_approval_is_classified_as_pending():
    assert status_of(PENDING) == "pending_approval"


def test_a_pending_approval_is_not_success_and_not_failure():
    status = status_of(PENDING)
    assert status != "success"
    assert status != "failure"


def test_a_real_failure_is_still_a_failure():
    assert status_of({
        "success": False,
        "status_code": 503,
        "error": "Sandbox required for 'workspace_scripts__run' but unavailable",
    }) == "failure"


def test_a_real_success_is_still_a_success():
    assert status_of({"success": True, "status_code": 200, "data": {}}) == "success"


def test_the_no_progress_guard_would_stop_on_repeated_pending_approvals():
    """The guard behaviour the orchestrator must now bypass.

    Three identical calls answered with a pending-approval envelope trip
    the stop threshold when they are reported as failures. The
    orchestrator no longer reports them that way, which is the fix; this
    pins why the fix is needed rather than cosmetic.
    """
    guard = NoProgressGuard(warn_threshold=2, stop_threshold=3)
    levels = [
        guard.observe(
            "workspace_scripts__rerun", {"script_id": "0b737753"}, False, PENDING,
        )
        for _ in range(3)
    ]
    assert GUARD_STOP in levels, (
        "if pending approvals are reported as failures the guard withdraws "
        "the toolset, which is what the orchestrator used to do"
    )


def test_the_guard_is_untouched_by_pending_calls_when_they_are_not_failures():
    guard = NoProgressGuard(warn_threshold=2, stop_threshold=3)
    levels = [
        guard.observe(
            "workspace_scripts__rerun", {"script_id": "0b737753"}, True, PENDING,
        )
        for _ in range(3)
    ]
    assert GUARD_STOP not in levels
