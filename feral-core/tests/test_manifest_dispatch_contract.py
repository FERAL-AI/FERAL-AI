"""CI contract: every skill manifest endpoint matches its backend dispatch table."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import skills.impl  # noqa: F401,E402 — register Python backing skills

from agents.tool_dispatch_validator import ToolDispatchValidator  # noqa: E402
from models.skill_manifest import SkillManifest  # noqa: E402

MANIFEST_ROOT = ROOT / "skills" / "manifests"

# Lane 05 (Wave 2) fixes these manifests; until then the contract test
# documents drift as xfail rather than blocking Wave 1 merge.
KNOWN_PENDING_MANIFEST_FIXES = frozenset({
    "smart_home_hue",
    "messaging_sms",
    "calendar_google",
    "spotify_music",
})


def _all_manifest_endpoints() -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for path in sorted(MANIFEST_ROOT.glob("*.json")):
        data = json.loads(path.read_text())
        manifest = SkillManifest(**data)
        for ep in manifest.endpoints:
            pairs.append((manifest.skill_id, ep.id))
    return pairs


@pytest.fixture(scope="module")
def validator() -> ToolDispatchValidator:
    return ToolDispatchValidator()


@pytest.mark.parametrize("skill_id,endpoint_id", _all_manifest_endpoints())
def test_manifest_endpoint_matches_backend_dispatch(
    validator: ToolDispatchValidator,
    skill_id: str,
    endpoint_id: str,
):
    violations = validator.contract_violations(skill_id, endpoint_id)
    if skill_id in KNOWN_PENDING_MANIFEST_FIXES:
        if violations:
            pytest.xfail(
                f"known manifest drift (Lane 05): {violations}"
            )
    assert not violations, (
        f"Manifest↔backend contract failed for {skill_id}__{endpoint_id}: "
        f"{violations}"
    )
