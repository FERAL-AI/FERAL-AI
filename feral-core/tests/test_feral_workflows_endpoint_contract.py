"""An unknown endpoint must say so, not blame a subsystem.

``execute`` reached the TaskFlow runtime check before it validated the
endpoint name, so a typo returned ``503 TaskFlow runtime is not
available``. On macOS, where the runtime is absent by default, every
unknown endpoint reported the wrong cause and sent whoever was debugging
it to look at Docker.

This also pins the declared/routed sets against each other in both
directions, because a declared-but-unrouted endpoint is a runtime 404
that no test would otherwise catch.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from skills.impl.feral_workflows import FeralWorkflowsSkill

MANIFEST = Path(__file__).resolve().parents[1] / "skills" / "manifests" / "feral_workflows.json"


@pytest.fixture(scope="module")
def declared() -> set[str]:
    return {e["id"] for e in json.loads(MANIFEST.read_text())["endpoints"]}


@pytest.fixture
def skill() -> FeralWorkflowsSkill:
    return FeralWorkflowsSkill()


class TestTheEndpointNameIsCheckedFirst:
    def test_an_unknown_endpoint_is_404_not_503(self, skill):
        """The bug: this returned 503 whenever the runtime was absent,
        which on macOS is always."""
        result = asyncio.run(skill.execute("definitely_not_real", {}, {}))
        assert result["status_code"] == 404, result

    def test_the_error_names_what_is_available(self, skill):
        """A refusal that does not say what to do instead is a dead end."""
        result = asyncio.run(skill.execute("definitely_not_real", {}, {}))
        for endpoint in ("list_commitments", "todo_write"):
            assert endpoint in result["error"]

    def test_the_error_does_not_blame_the_runtime(self, skill):
        result = asyncio.run(skill.execute("definitely_not_real", {}, {}))
        assert "TaskFlow runtime" not in result["error"]


class TestDeclaredAndRoutedAgree:
    def test_every_declared_endpoint_is_routed(self, skill, declared):
        """A declared-but-unrouted endpoint is a runtime 404 the model
        discovers by calling it."""
        unrouted = sorted(declared - FeralWorkflowsSkill.ALL_ENDPOINTS)
        assert unrouted == [], f"declared in the manifest, not routed: {unrouted}"

    def test_every_routed_endpoint_is_declared(self, declared):
        """The reverse: an endpoint the code answers but the manifest
        never mentions is invisible to the model."""
        undeclared = sorted(FeralWorkflowsSkill.ALL_ENDPOINTS - declared)
        assert undeclared == [], f"routed, not declared: {undeclared}"

    @pytest.mark.parametrize("endpoint_id", sorted(
        json.loads(MANIFEST.read_text())["endpoints"][i]["id"]
        for i in range(len(json.loads(MANIFEST.read_text())["endpoints"]))
    ))
    def test_no_declared_endpoint_reports_unknown(self, skill, endpoint_id):
        """Probed with empty args so nothing executes: a routed endpoint
        refuses on its own terms, an unrouted one says Unknown endpoint."""
        result = asyncio.run(skill.execute(endpoint_id, {}, {}))
        assert "Unknown endpoint" not in str(result.get("error") or "")


class TestTheCommitmentEndpointsDoNotNeedTheRuntime:
    """They read IntentCompiler, not TaskFlows, so like todo_write they
    must not 503 on an install where the runtime is absent."""

    def test_list_commitments_never_blames_taskflows(self, skill):
        """Asserts the REASON, not the code.

        A 503 here is legitimate when no brain is running: the intent
        compiler really is absent in a bare process. What must never
        happen is these endpoints reporting the TaskFlow runtime, which
        they do not use.
        """
        result = asyncio.run(skill.execute("list_commitments", {}, {}))
        assert "TaskFlow" not in str(result.get("error") or "")

    def test_complete_commitment_validates_before_it_needs_the_compiler(self, skill):
        """A missing required argument is a caller error and should be
        reported as one, whether or not a brain is running."""
        result = asyncio.run(skill.execute("complete_commitment", {}, {}))
        assert "TaskFlow" not in str(result.get("error") or "")
        assert result["status_code"] in (400, 503)
