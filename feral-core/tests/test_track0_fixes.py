"""Track 0 regressions — ambient briefing vault path + SkillManifest CUSTOM.

These two fixes came out of live-reproducing the bugs on the running Brain:
1. /api/ambient/briefing was 500ing because the route called
   ``state.vault.get(...)`` but ``BlindVault`` exposes ``retrieve(...)``.
2. Three first-party skill manifests (``workspace_scripts``,
   ``messaging_channels``, ``self_introspection``) use ``method: CUSTOM``
   which the Pydantic Literal validator rejected, so they were silently
   dropped at every Brain boot.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.no_auto_feral_home


REPO = Path(__file__).resolve().parents[1]
MANIFEST_DIR = REPO / "skills" / "manifests"


def test_skill_manifest_accepts_custom_method():
    from models.skill_manifest import SkillEndpoint

    ep = SkillEndpoint(id="x", method="CUSTOM", url="/x", description="x")
    assert ep.method == "CUSTOM"


@pytest.mark.parametrize(
    "manifest_name",
    ["workspace_scripts.json", "messaging_channels.json", "self_introspection.json"],
)
def test_first_party_custom_manifests_now_load(manifest_name: str):
    """The three manifests that were failing must parse cleanly."""
    from models.skill_manifest import SkillManifest

    path = MANIFEST_DIR / manifest_name
    if not path.exists():
        pytest.skip(f"{manifest_name} not in this tree")
    data = json.loads(path.read_text())
    manifest = SkillManifest(**data)
    assert manifest.skill_id


def test_ambient_briefing_does_not_500_without_vault_key(monkeypatch):
    """Simulate the code path that used to raise AttributeError."""
    from api.routes import ambient as mod

    # Stand-in state object shaped like BrainState for the subset the route touches.
    class FakeVault:
        def retrieve(self, key_name):
            return None

    class FakeBaselineEngine:
        def get_all_baselines(self):
            return []

    # These names are the ones IntentCompiler actually exposes. The fakes
    # here used to implement today() and list_active(), which exist on no
    # class in the tree: they were shaped like the route's bug rather than
    # like the collaborator, so this test passed for as long as the endpoint
    # was broken. tests/test_ambient_briefing_real_apis.py now drives the
    # real engines for the same reason.
    class FakeIntentCompiler:
        def get_today_actions(self, tz_name=None):
            return []

        def list_plans(self):
            return []

    class FakeOrchestrator:
        pass

    state = type("S", (), {})()
    state.orchestrator = FakeOrchestrator()
    state.baseline_engine = FakeBaselineEngine()
    state.intent_compiler = FakeIntentCompiler()
    state.vault = FakeVault()

    monkeypatch.setattr(mod, "state", state)
    monkeypatch.delenv("OPENWEATHER_API_KEY", raising=False)

    import asyncio
    result = asyncio.run(mod.get_briefing())
    assert isinstance(result, dict)
    assert "greeting" in result
    # Weather stays None when no key is present; we never raised.
    assert result.get("weather") is None
    # And nothing swallowed an error on the way. Not raising was never
    # sufficient: every block caught its own failure, so the endpoint could
    # not 500 no matter how broken it was.
    assert result.get("degraded") == []
