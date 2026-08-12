"""Behavioral contracts for the shared prompt-side tooling catalog."""

from __future__ import annotations

import json

from agents.self_model import _skill_line, build_tooling_catalog
from models.skill_manifest import BrandProfile, SkillEndpoint, SkillManifest
from skills.registry import SkillRegistry


def _manifest(
    skill_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    endpoint_count: int = 2,
) -> SkillManifest:
    return SkillManifest(
        skill_id=skill_id,
        brand=BrandProfile(name=name or skill_id.replace("_", " ").title()),
        description=description or f"Use {skill_id} for its installed capability.",
        endpoints=[
            SkillEndpoint(
                id=f"action_{index}",
                method="GET",
                url=f"https://example.test/{skill_id}/{index}",
                description=f"Endpoint detail {index} for {skill_id}.",
            )
            for index in range(endpoint_count)
        ],
    )


def _legacy_catalog(active: list[SkillManifest], full: list[SkillManifest]) -> str:
    """Reproduce the pre-change catalog for a stable size comparison."""
    active_ids = {skill.skill_id for skill in active}
    parts = ["## Tooling"]
    if active:
        parts.extend([
            "### Active this turn",
            (
                "These skills are loaded as tools right now. You can call them "
                "directly via `skill_id__endpoint_id` as usual."
            ),
            *[_skill_line(skill) for skill in active],
        ])
    else:
        parts.append(
            "### Active this turn\n"
            "(none routed — rely on the always-include fallback set)"
        )
    if full:
        parts.extend([
            "\n### Available (full catalog)",
            (
                "Every skill registered on this FERAL instance. If a capability "
                "here isn't active right now, say so explicitly — never claim "
                "the skill does not exist."
            ),
        ])
        parts.extend(
            _skill_line(skill)
            for skill in full
            if skill.skill_id not in active_ids
        )
    return "\n".join(parts)


def test_inactive_skill_keeps_identity_and_description_without_endpoints() -> None:
    skill = _manifest(
        "archive_search",
        name="Archive Search",
        description="Find material in the archive.\nInternal implementation detail.",
    )

    catalog = build_tooling_catalog([], [skill])

    assert "**Archive Search** (`archive_search`)" in catalog
    assert "Find material in the archive." in catalog
    assert "Internal implementation detail" not in catalog
    assert "Registered, but not callable this turn." in catalog
    assert "archive_search__action_0" not in catalog
    assert "Endpoint detail 0" not in catalog


def test_active_skill_retains_every_endpoint_and_is_not_repeated() -> None:
    active = _manifest("active_skill", endpoint_count=3)
    inactive = _manifest("inactive_skill", endpoint_count=2)

    catalog = build_tooling_catalog([active], [active, inactive])
    active_block, inactive_block = catalog.split("### Available (full catalog)")

    for endpoint in active.endpoints:
        assert f"active_skill__{endpoint.id}" in active_block
        assert endpoint.description in active_block
    assert "(`active_skill`)" not in inactive_block
    assert "(`inactive_skill`)" in inactive_block
    assert "inactive_skill__action_0" not in inactive_block


def test_promoting_a_skill_restores_endpoint_details() -> None:
    skill = _manifest("promoted_skill")

    inactive_catalog = build_tooling_catalog([], [skill])
    active_catalog = build_tooling_catalog([skill], [skill])

    assert "promoted_skill__action_0" not in inactive_catalog
    assert "promoted_skill__action_0" in active_catalog
    assert "Registered, but not callable this turn." not in active_catalog


def test_catalog_handles_empty_active_only_and_duplicate_inputs() -> None:
    skill = _manifest("single_skill", endpoint_count=1)

    assert build_tooling_catalog([], []) == (
        "## Tooling\n### Active this turn\n"
        "(none routed — rely on the always-include fallback set)"
    )
    active_only = build_tooling_catalog([skill], [])
    assert active_only.count("single_skill__action_0") == 1
    duplicated_active = build_tooling_catalog([skill], [skill, skill])
    assert duplicated_active.count("single_skill__action_0") == 1
    assert "Registered, but not callable this turn." not in duplicated_active
    duplicated_inactive = build_tooling_catalog([], [skill, skill])
    assert duplicated_inactive.count("(`single_skill`)") == 1


def test_full_catalog_cap_and_overflow_summary_are_preserved() -> None:
    skills = [_manifest(f"skill_{index}", endpoint_count=1) for index in range(5)]

    catalog = build_tooling_catalog([], skills, max_full=3)

    for index in range(3):
        assert f"(`skill_{index}`)" in catalog
    assert "(`skill_3`)" not in catalog
    assert "(`skill_4`)" not in catalog
    assert "…and 2 more skills. Ask the user to be specific." in catalog


def test_catalog_rendering_does_not_mutate_tool_schemas() -> None:
    registry = SkillRegistry()
    skill = _manifest("schema_guard", endpoint_count=2)
    registry.register(skill)
    before = json.dumps(
        registry.get_tools_for_skills([skill]),
        sort_keys=True,
        separators=(",", ":"),
    )

    build_tooling_catalog([], [skill])

    after = json.dumps(
        registry.get_tools_for_skills([skill]),
        sort_keys=True,
        separators=(",", ":"),
    )
    assert after == before


def test_shipped_inactive_catalog_retains_all_skills_and_shrinks_by_65_percent() -> None:
    registry = SkillRegistry()
    registry.load_builtin_skills()
    skills = list(registry.skills.values())

    legacy = _legacy_catalog([], skills)
    compact = build_tooling_catalog([], skills)

    assert skills
    assert all(f"(`{skill.skill_id}`)" in compact for skill in skills)
    assert len(compact) <= len(legacy) * 0.35
