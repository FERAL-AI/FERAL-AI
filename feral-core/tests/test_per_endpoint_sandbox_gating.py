"""Sandbox requirements belong to endpoints, not whole skills.

`workspace_scripts` declared `requires_sandbox` at skill level, so all
four of its endpoints were refused with HTTP 503 whenever Docker was not
running. Two of them execute nothing:

    list_catalog  reads ~/.feral/workspace/scripts catalog JSON
    delete        removes one catalog entry

Both are synchronous, touch only a JSON file, and never reach the
sandbox. An operator without Docker therefore could not list or delete
their own saved scripts, and both manifest descriptions documented that
refusal as expected behaviour.

`SkillEndpoint.requires_sandbox` has existed the whole time and no
manifest used it.

The hardcoded `SANDBOX_REQUIRED_SKILL_IDS` floor still protects any
manifest that declares nothing, but stops applying once an author has
marked specific endpoints. Otherwise the fallback would override the
per-endpoint declaration it exists to stand in for.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skills.executor import SANDBOX_REQUIRED_SKILL_IDS, SkillExecutor
from skills.registry import SkillRegistry

MANIFESTS = Path(__file__).resolve().parent.parent / "skills" / "manifests"


@pytest.fixture(scope="module")
def registry():
    reg = SkillRegistry()
    reg.load_builtin_skills()
    return reg


def _required(registry, skill_id, endpoint_id) -> bool:
    skill = registry.skills[skill_id]
    ep = next(e for e in skill.endpoints if e.id == endpoint_id)
    return SkillExecutor._is_sandbox_required(skill, ep)


class TestCodeStillNeedsTheSandbox:
    """The half that must not regress."""

    @pytest.mark.parametrize("endpoint_id", ["run", "rerun"])
    def test_workspace_scripts_execution_is_gated(self, registry, endpoint_id):
        assert _required(registry, "workspace_scripts", endpoint_id) is True

    @pytest.mark.parametrize("endpoint_id", ["run_python", "run_node"])
    def test_code_interpreter_is_gated(self, registry, endpoint_id):
        assert _required(registry, "code_interpreter", endpoint_id) is True


class TestReadOnlyEndpointsAreNotGated:
    @pytest.mark.parametrize("endpoint_id", ["list_catalog", "delete"])
    def test_no_code_endpoints_work_without_docker(self, registry, endpoint_id):
        assert _required(registry, "workspace_scripts", endpoint_id) is False, (
            f"{endpoint_id} runs no code but is still gated on the sandbox; "
            "without Docker the operator cannot reach their own catalog"
        )

    def test_those_endpoints_really_do_not_execute_anything(self):
        """The premise: they are plain synchronous file operations.

        If either ever grows a code path, ungating it becomes wrong and
        this fails rather than quietly widening the hole.
        """
        import inspect

        from skills.impl.workspace_scripts import WorkspaceScriptsSkill

        for name in ("_list_catalog", "_delete"):
            fn = getattr(WorkspaceScriptsSkill, name)
            assert not inspect.iscoroutinefunction(fn), f"{name} became async"
            src = inspect.getsource(fn)
            for forbidden in ("docker", "subprocess", "Popen", "sandbox", "exec("):
                assert forbidden not in src, (
                    f"{name} now references {forbidden!r}; it may need gating again"
                )


class TestTheLegacyFloorStillHolds:
    def test_a_manifest_that_declares_nothing_is_still_protected(self):
        class _Endpoint:
            requires_sandbox = False

        class _Skill:
            skill_id = "code_interpreter"
            requires_sandbox = False
            endpoints: list = []

        assert SkillExecutor._is_sandbox_required(_Skill(), _Endpoint()) is True

    def test_the_floor_defers_to_explicit_endpoint_marks(self):
        class _Gated:
            id = "run"
            requires_sandbox = True

        class _Open:
            id = "list"
            requires_sandbox = False

        class _Skill:
            skill_id = "workspace_scripts"
            requires_sandbox = False
            endpoints = [_Gated(), _Open()]

        skill = _Skill()
        assert SkillExecutor._is_sandbox_required(skill, _Gated()) is True
        assert SkillExecutor._is_sandbox_required(skill, _Open()) is False

    def test_the_floor_set_is_not_silently_empty(self):
        """A typo emptying this set would remove the protection quietly."""
        assert "code_interpreter" in SANDBOX_REQUIRED_SKILL_IDS


class TestTheManifestSaysWhatHappens:
    def test_the_manifest_no_longer_promises_a_503_for_no_code_endpoints(self):
        data = json.loads((MANIFESTS / "workspace_scripts.json").read_text())
        by_id = {e["id"]: e for e in data["endpoints"]}
        for eid in ("list_catalog", "delete"):
            desc = by_id[eid]["description"]
            assert "still refused with HTTP 503" not in desc, (
                f"{eid} still documents the refusal that was just removed"
            )

    def test_the_executing_endpoints_are_marked_in_the_manifest(self):
        data = json.loads((MANIFESTS / "workspace_scripts.json").read_text())
        by_id = {e["id"]: e for e in data["endpoints"]}
        assert by_id["run"].get("requires_sandbox") is True
        assert by_id["rerun"].get("requires_sandbox") is True
        assert not by_id["list_catalog"].get("requires_sandbox", False)
        assert not by_id["delete"].get("requires_sandbox", False)
