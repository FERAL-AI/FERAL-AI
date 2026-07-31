"""The shell + filesystem manifests must be truthful and singular.

Three defects pinned here:

1. **Duplicate skills.** ``computer_use`` and ``coding_tools`` exposed
   identical endpoints with identical trigger phrases, so the model saw two
   indistinguishable ``bash`` tools and picked arbitrarily.
   ``computer_use`` was the one wired into ``ALWAYS_INCLUDE_SKILLS``;
   ``coding_tools`` was the better implementation. Consolidated onto
   ``coding_tools``.

2. **Missing safety annotations.** Neither manifest declared
   ``safety_tier`` / ``read_only_hint`` / ``requires_user_approval``, so
   every endpoint fell through ``security/safety_resolver.py`` to a
   substring heuristic whose unknown-tool default is CONFIRM. Approval
   friction landed on read-only lookups by accident rather than by
   decision.

3. **Impossible instructions.** ``desktop_control``'s ``shell_command``
   told the model to run ``echo 'content' > ~/Desktop/file.txt``. ``echo``
   is not in the 14-program allowlist and ``>`` is a rejected
   metacharacter, so that recipe could only ever 403.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from security.safety_resolver import LEVEL_AUTO, LEVEL_CONFIRM, resolve_policy
from security.sandbox_policy import SandboxPolicy
from skills.registry import SkillRegistry

MANIFEST_DIR = Path(__file__).resolve().parent.parent / "skills" / "manifests"


def _manifest(skill_id: str) -> dict:
    return json.loads((MANIFEST_DIR / f"{skill_id}.json").read_text())


def _endpoint(skill_id: str, endpoint_id: str) -> dict:
    for endpoint in _manifest(skill_id)["endpoints"]:
        if endpoint["id"] == endpoint_id:
            return endpoint
    raise AssertionError(f"{skill_id}__{endpoint_id} not found")


@pytest.fixture()
def registry() -> SkillRegistry:
    reg = SkillRegistry()
    reg.load_from_directory(MANIFEST_DIR)
    return reg


# ── 1. one shell toolset ──────────────────────────────────────────


def test_computer_use_manifest_and_impl_are_gone():
    assert not (MANIFEST_DIR / "computer_use.json").exists()
    assert not (MANIFEST_DIR.parent / "impl" / "computer_use.py").exists()


def test_only_one_manifest_exposes_a_bash_endpoint():
    owners = [
        json.loads(path.read_text())["skill_id"]
        for path in MANIFEST_DIR.glob("*.json")
        if any(e["id"] == "bash" for e in json.loads(path.read_text()).get("endpoints", []))
    ]
    assert owners == ["coding_tools"]


def test_no_two_manifests_share_the_shell_trigger_phrases():
    """The duplicate pair shared its entire trigger list, which is what made
    the two ``bash`` tools indistinguishable to the router."""
    coding = set(_manifest("coding_tools")["trigger_phrases"])
    for path in MANIFEST_DIR.glob("*.json"):
        data = json.loads(path.read_text())
        if data["skill_id"] == "coding_tools":
            continue
        assert set(data.get("trigger_phrases", [])) != coding, data["skill_id"]


def test_orchestrator_always_includes_coding_tools_not_computer_use():
    from agents.orchestrator import Orchestrator

    assert "coding_tools" in Orchestrator.ALWAYS_INCLUDE_SKILLS
    assert "computer_use" not in Orchestrator.ALWAYS_INCLUDE_SKILLS


def test_coding_tools_impl_is_registered():
    from skills.impl import get_implementation

    assert get_implementation("coding_tools") is not None
    assert get_implementation("computer_use") is None


# ── 2. the bash endpoint contract ─────────────────────────────────


def test_bash_no_longer_declares_requires_sandbox():
    """The per-endpoint boolean is what returned 503 before dispatch on
    every Docker-less machine. The decision now lives in
    ``security/exec_mode.py``."""
    assert _endpoint("coding_tools", "bash").get("requires_sandbox") is not True


def test_bash_declares_a_cwd_parameter():
    params = {p["name"] for p in _endpoint("coding_tools", "bash")["params"]}
    assert "cwd" in params


def test_generated_code_skills_still_declare_requires_sandbox():
    """Nothing here weakens the runners that execute generated code."""
    for skill_id in ("code_interpreter", "workspace_scripts"):
        data = _manifest(skill_id)
        declared = bool(data.get("requires_sandbox")) or any(
            e.get("requires_sandbox") for e in data.get("endpoints", [])
        )
        from skills.executor import SANDBOX_REQUIRED_SKILL_IDS

        assert declared or skill_id in SANDBOX_REQUIRED_SKILL_IDS, skill_id


# ── 3. safety annotations ─────────────────────────────────────────


@pytest.mark.parametrize(
    "tool_name",
    [
        "coding_tools__read_file",
        "coding_tools__grep_search",
        "coding_tools__glob_search",
        "coding_tools__web_fetch",
        "desktop_control__system_info",
    ],
)
def test_read_only_endpoints_resolve_to_auto(tool_name, registry):
    decision = resolve_policy(tool_name, {}, surface="websocket", registry=registry)
    assert decision.level == LEVEL_AUTO, decision.to_dict()
    assert decision.sources["manifest"]["safety_tier"] == "safe"


@pytest.mark.parametrize(
    "tool_name",
    [
        "coding_tools__bash",
        "coding_tools__write_file",
        "coding_tools__edit_file",
        "desktop_control__shell_command",
        "desktop_control__open_app",
        "desktop_control__set_volume",
    ],
)
def test_consequential_endpoints_still_confirm(tool_name, registry):
    decision = resolve_policy(tool_name, {}, surface="websocket", registry=registry)
    assert decision.level == LEVEL_CONFIRM, decision.to_dict()


def test_read_only_hint_is_set_where_it_is_claimed(registry):
    from security.safety_resolver import is_read_only

    assert is_read_only("coding_tools__read_file", registry=registry) is True
    assert is_read_only("coding_tools__grep_search", registry=registry) is True


# ── 4. no impossible instructions ─────────────────────────────────


def test_desktop_control_no_longer_teaches_a_redirect_recipe():
    shell = _endpoint("desktop_control", "shell_command")
    blob = json.dumps(shell)
    assert "echo 'content' >" not in blob
    assert "echo 'hello' >" not in blob


def test_every_desktop_control_default_command_actually_validates():
    """A default the validator rejects is a documented lie. ``screenshot``
    shipped ``… && echo 'Screenshot saved…'`` and ``system_info`` shipped an
    ``echo '{...}'$(top -l 1 | grep …)`` pipeline; both were 403s."""
    policy = SandboxPolicy()
    for endpoint in _manifest("desktop_control")["endpoints"]:
        for param in endpoint["params"]:
            default = param.get("default")
            if not default or param["name"] != "command":
                continue
            ok, reason = policy.validate_shell_command(default)
            assert ok is True, f"{endpoint['id']}: {reason}"


def test_desktop_control_description_does_not_promise_file_writes():
    """The skill cannot create files: no ``echo``, no redirect. It used to
    advertise "create/read/write files" anyway, and carried the matching
    trigger phrases, which routed file requests to a dead end."""
    data = _manifest("desktop_control")
    assert "create/read/write files" not in data["description"]
    for phrase in ("create a file", "write a file", "create a note on my desktop"):
        assert phrase not in data["trigger_phrases"]
