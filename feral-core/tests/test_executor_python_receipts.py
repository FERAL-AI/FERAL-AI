"""Actual executor normalization retains bounded typed backing receipts."""

from collections import deque
import copy
import json
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from models.skill_manifest import SkillManifest
from skills.call_context import bind_context
from skills.executor import SkillExecutor
from skills.impl.agentic_computer_use import AgenticComputerUseSkill
from tests.test_agentic_computer_use_authority import authority as _authority_fixture

authority = _authority_fixture


def _wire_executor(authority, monkeypatch, implementation):
    executor = authority.state.skill_executor
    executor._execute_inner = MethodType(SkillExecutor._execute_inner, executor)
    executor._sandbox_port = SimpleNamespace()
    executor._vault_for = lambda _: {}
    monkeypatch.setattr("skills.impl.get_implementation", lambda _: implementation)
    manifest = SkillManifest.model_validate(
        json.loads((Path("skills/manifests/agentic_computer_use.json")).read_text())
    )
    authority.state.skill_registry.register(manifest)
    executor._skill_call_times[manifest.skill_id] = deque()
    return executor, manifest, manifest.endpoints[0]


async def test_actual_registered_agentic_vision_refusal_retains_code(
    authority, monkeypatch
):
    implementation = AgenticComputerUseSkill()
    executor, manifest, endpoint = _wire_executor(
        authority, monkeypatch, implementation
    )
    with bind_context(
        session_id="exact-owner",
        surface="voice",
        tool_name="agentic_computer_use__execute_task",
    ):
        output = await executor.execute(
            "agentic_computer_use__execute_task",
            {"task": "inert synthetic task"},
            manifest,
            endpoint,
        )
    assert output == {
        "success": False,
        "status_code": 503,
        "data": None,
        "error": "The shared vision runtime is unavailable.",
        "error_code": "vision_binding_unavailable",
    }
    assert authority.effects == []
    authority.raw.assert_not_awaited()
    assert any(row[0] == "plan" for row in authority.policies)


@pytest.mark.parametrize("data", [None, False, 0, "", [], {}])
@pytest.mark.parametrize(
    "receipt",
    [
        {
            "success": False,
            "status_code": 503,
            "error": "Unavailable",
            "error_code": "vision_request_unavailable",
        },
        {
            "success": False,
            "status_code": 202,
            "error": None,
            "pending_approval": True,
            "status": "pending_approval",
            "approval_request_id": "fixture-review",
        },
        {
            "success": False,
            "status_code": 503,
            "error": "Unknown",
            "outcome_unknown": True,
            "outcome": "unknown",
            "verified": False,
            "code": "dispatch_unknown",
        },
    ],
)
async def test_public_executor_preserves_typed_receipts_and_falsey_data(
    authority, monkeypatch, data, receipt
):
    result = {
        **receipt,
        "data": copy.deepcopy(data),
        "request_id": "fixture-request",
        "tool_name": "agentic_computer_use__execute_task",
        "session_id": "exact-owner",
    }
    original = copy.deepcopy(result)
    implementation = SimpleNamespace(execute=AsyncMock(return_value=result))
    executor, manifest, endpoint = _wire_executor(
        authority, monkeypatch, implementation
    )
    with bind_context(session_id="exact-owner", surface="voice"):
        output = await executor.execute(
            "agentic_computer_use__execute_task", {"task": "inert"}, manifest, endpoint
        )
    assert output == result
    assert result == original
    implementation.execute.assert_awaited_once()


@pytest.mark.parametrize("success", ["false", "true", 0, 1, None, [], {}])
async def test_invalid_success_never_becomes_execution_success(
    authority, monkeypatch, success
):
    implementation = SimpleNamespace(
        execute=AsyncMock(
            return_value={
                "success": success,
                "status_code": 200,
                "data": False,
                "error": None,
            }
        )
    )
    executor, manifest, endpoint = _wire_executor(
        authority, monkeypatch, implementation
    )
    with bind_context(session_id="exact-owner", surface="voice"):
        output = await executor.execute(
            "agentic_computer_use__execute_task", {"task": "inert"}, manifest, endpoint
        )
    assert output["success"] is False
    assert output["status_code"] == 500
    assert output["error_code"] == "invalid_skill_result"
    assert output["outcome_unknown"] is True


async def test_private_extensions_are_not_added_to_normalized_receipt(
    authority, monkeypatch
):
    raw = {
        "success": True,
        "status_code": 200,
        "data": 0,
        "error": None,
        "password": "private-canary",
        "extension": {"token": "private-canary"},
        "__repr_canary": object(),
    }
    implementation = SimpleNamespace(execute=AsyncMock(return_value=raw))
    executor, manifest, endpoint = _wire_executor(
        authority, monkeypatch, implementation
    )
    with bind_context(session_id="exact-owner", surface="voice"):
        output = await executor.execute(
            "agentic_computer_use__execute_task", {"task": "inert"}, manifest, endpoint
        )
    assert output == {"success": True, "status_code": 200, "data": 0, "error": None}


@pytest.mark.parametrize(
    "bad_field",
    [
        {"pending_approval": "true"},
        {"outcome_unknown": 1},
        {"verified": "false"},
        {"error_code": object()},
        {"session_id": "x" * 1025},
        {"status_code": True},
        {"status_code": 700},
        {"error": object()},
        {"success": True, "status_code": 503},
        {
            "success": "true",
            "verified": True,
            "outcome": "executed",
            "effects_committed": True,
        },
    ],
)
async def test_malformed_receipt_fields_fail_closed_without_private_repr(
    authority, monkeypatch, bad_field
):
    result = {
        "success": False,
        "status_code": 503,
        "data": 0,
        "error": None,
        **bad_field,
    }
    implementation = SimpleNamespace(execute=AsyncMock(return_value=result))
    executor, manifest, endpoint = _wire_executor(
        authority, monkeypatch, implementation
    )
    with bind_context(session_id="exact-owner", surface="voice"):
        output = await executor.execute(
            "agentic_computer_use__execute_task", {"task": "inert"}, manifest, endpoint
        )
    assert output["success"] is False and output["status_code"] == 500
    assert (
        output["error_code"] == "invalid_skill_result"
        and output["outcome_unknown"] is True
    )
    assert "object at" not in json.dumps(output)
    assert output["outcome"] == "unknown"
    assert "verified" not in output and "effects_committed" not in output


@pytest.mark.parametrize(
    "raw", [False, 0, "", [], {}, {"status": "ready", "items": []}]
)
async def test_existing_raw_data_result_compatibility(authority, monkeypatch, raw):
    implementation = SimpleNamespace(execute=AsyncMock(return_value=raw))
    executor, manifest, endpoint = _wire_executor(
        authority, monkeypatch, implementation
    )
    with bind_context(session_id="exact-owner", surface="voice"):
        output = await executor.execute(
            "agentic_computer_use__execute_task", {"task": "inert"}, manifest, endpoint
        )
    assert output == {"success": True, "status_code": 200, "data": raw, "error": None}


async def test_valid_nullable_verification_and_truncation_metadata_survive(
    authority, monkeypatch
):
    result = {
        "success": True,
        "status_code": 200,
        "data": 0,
        "error": "",
        "verified": None,
        "_truncated": True,
        "_truncation_note": "Known partial receipt",
        "effects_committed": False,
        "processing_state": "not_asserted",
    }
    implementation = SimpleNamespace(execute=AsyncMock(return_value=result))
    executor, manifest, endpoint = _wire_executor(
        authority, monkeypatch, implementation
    )
    with bind_context(session_id="exact-owner", surface="voice"):
        output = await executor.execute(
            "agentic_computer_use__execute_task", {"task": "inert"}, manifest, endpoint
        )
    assert output == result
