"""Read-only endpoints resolve to AUTO, and no writer sneaks in with them.

``security/safety_resolver`` defaults an endpoint carrying neither
``safety_tier`` nor ``read_only_hint`` to CONFIRM, which is the right
default. It meant, though, that eight endpoints that only read were
refused 412 "requires confirmation" during the 2026-09-04 skills audit:
``browser_memory`` stats/recall/genesis_candidates and
``self_introspection`` describe_skill/active_channels/connected_devices,
plus current_session. Nothing about them mutates anything.

The second half of this file is the part that matters more: proof that
the sweep for other read-only endpoints did not hint anything that writes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from security.safety_resolver import LEVEL_AUTO, is_read_only, resolve_policy  # noqa: E402
from skills.registry import SkillRegistry  # noqa: E402


#: Endpoints that were refused for a read during the audit, plus the ones
#: the follow-up sweep found in the same state.
NEWLY_HINTED = (
    "browser_memory__recall",
    "browser_memory__search",
    "browser_memory__list_domains",
    "browser_memory__stats",
    "browser_memory__genesis_candidates",
    "self_introspection__describe_skill",
    "self_introspection__active_channels",
    "self_introspection__connected_devices",
    "self_introspection__current_session",
    "github_api__user_profile",
    "github_api__notifications",
    "pdf_reader__extract_text",
    "pdf_reader__metadata",
    "pdf_reader__extract_images",
    "perception_query__what_do_i_see",
)

#: Sitting right beside the hinted ones in the same manifests, and all of
#: them mutate. If a sweep ever hints by name pattern alone, this catches it.
MUST_NOT_BE_HINTED = (
    "browser_memory__remember",
    "browser_memory__forget",
    "github_api__create_issue",
    "code_interpreter__run_python",
    "messaging_sms__telegram_send",
    "smart_home_hue__set_light",
    "calendar_google__create_event",
)


@pytest.fixture(scope="module")
def registry():
    reg = SkillRegistry()
    reg.load_from_directory(ROOT / "skills" / "manifests")
    return reg


def _endpoint(registry, tool_name):
    skill_id, endpoint_id = tool_name.split("__", 1)
    manifest = registry.skills.get(skill_id)
    assert manifest is not None, f"{skill_id} is not a shipped manifest"
    for endpoint in manifest.endpoints:
        if endpoint.id == endpoint_id:
            return endpoint
    return None


@pytest.mark.parametrize("tool_name", NEWLY_HINTED)
def test_hinted_endpoint_declares_the_hint_and_resolves_to_auto(registry, tool_name):
    endpoint = _endpoint(registry, tool_name)
    if endpoint is None:
        pytest.skip(f"{tool_name} is not shipped in this build")
    assert endpoint.read_only_hint is True
    assert endpoint.requires_user_approval is False
    assert is_read_only(tool_name, registry=registry, strict=True) is True
    decision = resolve_policy(tool_name, {}, registry=registry)
    assert decision.level == LEVEL_AUTO, decision.sources


def test_the_six_endpoints_the_audit_saw_refused_are_no_longer_confirm(registry):
    """The exact tools that answered 412 during the audit."""
    refused_for_a_read = (
        "browser_memory__stats",
        "browser_memory__recall",
        "browser_memory__genesis_candidates",
        "self_introspection__describe_skill",
        "self_introspection__active_channels",
        "self_introspection__connected_devices",
    )
    for tool_name in refused_for_a_read:
        assert resolve_policy(tool_name, {}, registry=registry).level == LEVEL_AUTO


@pytest.mark.parametrize("tool_name", MUST_NOT_BE_HINTED)
def test_writers_were_not_hinted(registry, tool_name):
    endpoint = _endpoint(registry, tool_name)
    if endpoint is None:
        pytest.skip(f"{tool_name} is not shipped in this build")
    assert endpoint.read_only_hint is False, (
        f"{tool_name} mutates state and must never carry read_only_hint"
    )
    assert is_read_only(tool_name, registry=registry, strict=True) is False


def test_no_shipped_endpoint_hints_read_only_and_demands_approval(registry):
    """A contradiction the resolver would have to break a tie on."""
    for skill_id, manifest in registry.skills.items():
        for endpoint in manifest.endpoints:
            if endpoint.read_only_hint:
                assert not endpoint.requires_user_approval, (
                    f"{skill_id}__{endpoint.id} claims read-only AND demands "
                    "approval"
                )
                assert endpoint.safety_tier in (None, "safe"), (
                    f"{skill_id}__{endpoint.id} claims read-only under tier "
                    f"{endpoint.safety_tier!r}"
                )
