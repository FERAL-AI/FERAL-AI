"""
Tests for the manifest-aware PolicyDecision resolver and its integration
through ToolRunner.

The matrix we lock in:

* Surface deny wins outright — http_api on `desktop_control__shell_command`
  returns DENY with `surface_deny=True` and a distinct error label.
* Manifest `safety_tier="safe"` upgrades a tool whose name would have
  matched a confirm-token substring to AUTO (this is the case the legacy
  classifier got wrong for benign `*_create` endpoints).
* Manifest `safety_tier="deny"` blocks a tool whose substring would
  have classified it AUTO (forcing the policy author's intent through).
* Manifest `requires_user_approval=True` forces CONFIRM regardless of
  substring or danger-map shape.
* `dangerous_tools.TOOL_DANGER_MAP` entries route to CONFIRM when no
  manifest metadata is present (proving the danger map is finally wired
  into approval, not just deny lists).
* Legacy substring heuristic remains for tools with no manifest metadata
  and no danger-map entry, preserving the pre-PR6 behaviour for
  unannotated third-party skills.
* `coding_tools__write_file` now triggers the same permission card path
  as `computer_use__write_file` did in PR 2.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from models.skill_manifest import (  # noqa: E402
    AuthConfig, BrandProfile, EndpointParam, SkillEndpoint, SkillManifest,
)
from security.safety_resolver import (  # noqa: E402
    LEVEL_AUTO,
    LEVEL_CONFIRM,
    LEVEL_DENY,
    is_read_only,
    resolve_policy,
)


# audit P0.1: a manifest may only DE-ESCALATE (safety_tier "safe",
# read_only_hint) when it ships in this repo, so the fixtures below that
# exercise de-escalation have to use a real shipped skill id. The ones that
# exercise escalation keep their arbitrary names, because escalation is
# honoured from any author.
BUILTIN_SKILL_ID = "notes_memory"


class _FakeRegistry:
    """In-memory skill registry sufficient for the resolver. We avoid
    standing up the full SkillRegistry to keep tests pure unit-tests."""

    def __init__(self, manifests: list[SkillManifest]):
        self.skills = {m.skill_id: m for m in manifests}


def _make_skill(skill_id: str, endpoint_overrides: dict) -> SkillManifest:
    return SkillManifest(
        skill_id=skill_id,
        version="1.0.0",
        author="test",
        brand=BrandProfile(name=skill_id),
        description="fixture",
        auth=AuthConfig(type="none"),
        endpoints=[
            SkillEndpoint(
                id="do_thing",
                method="PYTHON",
                url="",
                description="fixture endpoint",
                params=[EndpointParam(name="x", required=False)],
                **endpoint_overrides,
            ),
        ],
    )


def _resolve(tool_name, args=None, *, surface="websocket", registry=None):
    return resolve_policy(tool_name, args, surface=surface, registry=registry)


# ── Surface deny ──────────────────────────────────────────────────────


def test_surface_deny_overrides_everything():
    """The http_api surface refuses `desktop_control__shell_command`
    irrespective of what any manifest claims."""
    decision = _resolve("desktop_control__shell_command", surface="http_api")
    assert decision.level == LEVEL_DENY
    assert decision.sources.get("surface_deny") is True
    assert decision.deny_reason


def test_surface_deny_does_not_fire_on_allowed_surface():
    decision = _resolve("desktop_control__shell_command", surface="websocket")
    # Not surface-denied; danger-map elevates it to CONFIRM.
    assert decision.level == LEVEL_CONFIRM
    assert decision.sources.get("surface_deny") is None


# ── Manifest wins over substring ──────────────────────────────────────


def test_manifest_safe_overrides_confirm_substring():
    """A `*_create` endpoint declared safe in its manifest should not
    be confirm-walled by the legacy substring rule.

    audit P0.1: the skill id is now a FIRST-PARTY one. De-escalation is
    only honoured from a manifest that ships in this repo; the third-party
    half of this matrix lives in
    ``tests/test_p0_security_clamps_and_gates.py``.
    """
    skill = _make_skill(BUILTIN_SKILL_ID, {"safety_tier": "safe"})
    registry = _FakeRegistry([skill])
    decision = _resolve(f"{BUILTIN_SKILL_ID}__do_thing", registry=registry)
    # Without manifest this would have been CONFIRM via "create"/"unknown"
    # tokens; with manifest=safe it must be AUTO.
    assert decision.level == LEVEL_AUTO
    assert decision.sources["manifest"]["safety_tier"] == "safe"


def test_manifest_deny_overrides_auto_substring():
    skill = _make_skill("nuclear_skill", {"safety_tier": "deny"})
    registry = _FakeRegistry([skill])
    decision = _resolve("nuclear_skill__do_thing", registry=registry)
    assert decision.level == LEVEL_DENY


def test_manifest_requires_user_approval_forces_confirm():
    skill = _make_skill("status_skill", {"requires_user_approval": True})
    registry = _FakeRegistry([skill])
    decision = _resolve("status_skill__do_thing", registry=registry)
    assert decision.level == LEVEL_CONFIRM


def test_manifest_read_only_hint_promotes_to_auto():
    """An endpoint named `update_thing` is normally CONFIRM via the
    substring rule. A `read_only_hint=True` manifest claim should
    promote it to AUTO."""
    skill = SkillManifest(
        skill_id=BUILTIN_SKILL_ID,
        version="1.0.0",
        author="test",
        brand=BrandProfile(name="readonly"),
        description="fixture",
        auth=AuthConfig(type="none"),
        endpoints=[
            SkillEndpoint(
                id="update_thing",  # substring "update" -> CONFIRM legacy
                method="PYTHON",
                url="",
                description="fixture",
                read_only_hint=True,
            ),
        ],
    )
    registry = _FakeRegistry([skill])
    decision = _resolve(f"{BUILTIN_SKILL_ID}__update_thing", registry=registry)
    assert decision.level == LEVEL_AUTO


# ── Danger map elevates without manifest ─────────────────────────────


def test_danger_map_routes_warn_to_confirm():
    """`coding_tools__write_file` is WARN in TOOL_DANGER_MAP (extended in
    PR 2). Resolver must elevate it to CONFIRM purely from the danger
    map — no manifest metadata required."""
    decision = _resolve("coding_tools__write_file")
    assert decision.level == LEVEL_CONFIRM
    assert decision.sources["danger_map"] in ("WARN", "warn")


def test_danger_map_critical_to_confirm_when_surface_allows():
    """`computer_use__bash` is CRITICAL. On websocket it should produce
    CONFIRM (not DENY); only the surface deny list produces DENY."""
    decision = _resolve("computer_use__bash", surface="websocket")
    assert decision.level == LEVEL_CONFIRM
    assert decision.sources.get("surface_deny") is None


# ── Legacy substring fallback preserved ──────────────────────────────


def test_substring_format_token_denies_when_unannotated():
    """`desktop__format_disk` is not in TOOL_DANGER_MAP and has no
    manifest; the legacy `format` token should still produce DENY so
    pre-PR6 behaviour is preserved for third-party skills."""
    decision = _resolve("desktop__format_disk")
    assert decision.level == LEVEL_DENY


def test_substring_auto_token_remains_auto():
    decision = _resolve("foo_skill__search_things")
    assert decision.level == LEVEL_AUTO


def test_unknown_tool_defaults_to_confirm():
    decision = _resolve("foo_skill__bar_thing")
    assert decision.level == LEVEL_CONFIRM


# ── is_read_only ──────────────────────────────────────────────────────


def test_is_read_only_uses_manifest_when_present():
    skill = _make_skill(BUILTIN_SKILL_ID, {"read_only_hint": True})
    registry = _FakeRegistry([skill])
    assert is_read_only(f"{BUILTIN_SKILL_ID}__do_thing", registry=registry) is True


def test_is_read_only_falls_back_to_substring():
    assert is_read_only("foo__search_x") is True
    assert is_read_only("foo__delete_x") is False


# ── ToolRunner integration ───────────────────────────────────────────


class _StubOrchestrator:
    def __init__(self, registry=None):
        self.skills = registry

    # ToolRunner only touches `.skills` for the safety resolver.


def test_tool_runner_enforce_safety_routes_through_resolver():
    from agents.tool_runner import ToolRunner

    skill = _make_skill(BUILTIN_SKILL_ID, {"safety_tier": "safe"})
    runner = ToolRunner(_StubOrchestrator(_FakeRegistry([skill])), autonomy_mode="hybrid")
    result = runner.enforce_safety(f"{BUILTIN_SKILL_ID}__do_thing", args={})
    # AUTO -> no gating dict returned
    assert result is None


def test_tool_runner_enforce_safety_uses_danger_map():
    from agents.tool_runner import ToolRunner

    runner = ToolRunner(_StubOrchestrator(None), autonomy_mode="hybrid")
    result = runner.enforce_safety(
        "coding_tools__write_file", args={"path": "/tmp/x"},
    )
    # CONFIRM -> pending dict with explainability sources
    assert isinstance(result, dict)
    assert result.get("status") == "pending_approval"
    assert result.get("safety_level") == LEVEL_CONFIRM
    sources = result.get("policy_sources") or {}
    assert sources.get("danger_map") in ("WARN", "warn")


def test_tool_runner_enforce_safety_surface_deny_labels_distinctly():
    from agents.tool_runner import ToolRunner

    runner = ToolRunner(_StubOrchestrator(None), autonomy_mode="hybrid")
    result = runner.enforce_safety(
        "desktop_control__shell_command", args={"command": "ls"},
        surface="http_api",
    )
    assert result is not None
    assert result["safety_level"] == LEVEL_DENY
    assert "Surface Policy" in result["error"]
    assert result["policy_sources"]["surface_deny"] is True


# ── coding_tools permission parity ───────────────────────────────────


def test_coding_tools_now_in_permission_remediation_set():
    """PR 2 wired computer_use__* through the `permission_request` card.
    PR 6 must extend that to coding_tools__* so the alias surface uses
    the same Allow/Deny UI and workspace_grants path."""
    from agents.tool_runner import _COMPUTER_USE_PERMISSION_TOOLS

    for tool in (
        "coding_tools__read_file", "coding_tools__write_file",
        "coding_tools__edit_file", "coding_tools__grep_search",
        "coding_tools__glob_search", "coding_tools__index_folder",
    ):
        assert tool in _COMPUTER_USE_PERMISSION_TOOLS, (
            f"{tool} must trigger the same permission_request flow as its "
            "computer_use__* twin."
        )


_ = pytest  # quiet unused-import lint when pytest plugins are autoloaded
