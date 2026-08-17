"""Installing a GenUI app must be a verified, informed decision.

``POST /api/apps/install`` used to ask for nothing: no token, no
signature confirmation, no dialog. Worse, an ``AppManifest`` may declare
``skill_dependencies``, and the route resolved them by calling
``MarketplaceClient.install(skill_id, "latest", None)`` -- the developer
path whose own log line reads ``UNVERIFIED INSTALL``. A skill executes
Python in this process at load time (``skills/registry.py``
``_try_load_dynamic_impl`` calls ``spec.loader.exec_module``), so
installing an app silently installed code-executing skills with no
signature check and nothing on screen.

Five contracts:

1. An app install without consent is refused.
2. The unverified ``MarketplaceClient.install`` is unreachable from any
   HTTP route, statically and at runtime.
3. The skill set the preview discloses is the skill set that gets
   installed, and it is installed over the verified path.
4. A dependency FERAL cannot verify does not dead-end the user: it is
   named with the brain's own reason, what it will break, and what to do
   about it, and the app installs in a state that keeps saying so.
5. A dependency that is already installed is disclosed as already
   installed, because the new code is the only new decision.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch

from agents.app_registry import AppRegistry, HybridGenerator
from models.app_manifest import ActionSpec, AppManifest, SurfaceSpec
from models.skill_manifest import BrandProfile, SkillManifest


pytestmark = pytest.mark.no_auto_feral_home


# ─────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────


def _write_app(root: Path, *, app_id: str = "notes-app", deps: list[str] | None = None) -> Path:
    """A minimal but real app bundle whose actions call into its skills."""
    src = root / f"src-{app_id}"
    src.mkdir(parents=True)
    manifest = AppManifest(
        app_id=app_id,
        brand=BrandProfile(name="Notes"),
        description="Keeps notes.",
        skill_dependencies=list(deps or []),
        surfaces=[
            SurfaceSpec(
                surface_id="home",
                kind="authored",
                template_root={"type": "VStack", "children": [{"type": "Text", "value": "hi"}]},
                action_contract=[
                    ActionSpec(action_id="open", handler="app_event"),
                    *[
                        ActionSpec(
                            action_id=f"sync_{i}",
                            handler="skill_call",
                            target=f"{dep}/sync",
                            description=f"Sync with {dep}",
                        )
                        for i, dep in enumerate(deps or [])
                    ],
                ],
            ),
        ],
        entry_surface_id="home",
    )
    (src / "manifest.json").write_text(manifest.model_dump_json())
    return src


class FakeMarketplace:
    """Stands in for MarketplaceClient over the two methods a route may use.

    ``install`` is the unverified developer path. It raises here: any
    test that reaches it has found the bypass this module exists to keep
    closed.
    """

    def __init__(self, published: dict[str, dict] | None = None):
        self.published = published or {}
        self.installed: list[str] = []
        self.released: list[str] = []
        self.previewed: list[str] = []

    async def install(self, skill_id, version="latest", source_url=None):  # noqa: D102
        raise AssertionError(
            "the unverified MarketplaceClient.install path was reached from an "
            f"HTTP route for {skill_id!r}"
        )

    async def preview_from_registry(self, kind, item_id):
        self.previewed.append(item_id)
        entry = self.published.get(item_id)
        if entry is None:
            return {
                "success": False,
                "error": f"'{item_id}' is not published in the registry at https://registry.feral.sh",
            }
        if entry.get("signature_fails"):
            return {
                "success": False,
                "error": "signature verification failed: sha256 mismatch",
            }
        return {
            "success": True,
            "kind": "skill",
            "id": item_id,
            "name": entry.get("name", item_id),
            "version": entry.get("version", "1.0.0"),
            "publisher": entry.get("publisher", "acme"),
            "permissions": list(entry.get("permissions") or []),
            "permission_details": [
                {"id": p, "label": p, "description": "…", "known": True}
                for p in (entry.get("permissions") or [])
            ],
            "signature": {"verified": True, "sha256": "a" * 64},
            "install_token": f"token-for-{item_id}",
        }

    async def install_from_registry(self, kind, item_id, install_token=""):
        if install_token != f"token-for-{item_id}":
            return {"success": False, "error": "preview token does not match"}
        if self.published.get(item_id, {}).get("install_fails"):
            return {"success": False, "error": "install failed: disk full"}
        self.installed.append(item_id)
        return {"success": True, "kind": "skill", "skill_id": item_id}

    def release_preview(self, token):
        self.released.append(token)
        return True


def _skill(skill_id: str, permissions: list[str]) -> SkillManifest:
    return SkillManifest(
        skill_id=skill_id,
        brand=BrandProfile(name=skill_id),
        description="An installed skill.",
        permissions=permissions,
    )


@pytest.fixture()
def app_client(tmp_path):
    """A TestClient wired to a real AppRegistry and a controllable brain state."""
    from api.routes import apps as apps_route

    # getattr, so that against a build without the consent gate these
    # tests fail on the contract they assert rather than erroring in
    # setup on a missing attribute.
    getattr(apps_route, "_pending_app_previews", {}).clear()

    registry = AppRegistry(db_path=str(tmp_path / "apps.db"), apps_dir=tmp_path / "apps")
    registry.set_hybrid_generator(HybridGenerator(cache_dir=tmp_path / "cache"))

    skill_registry = MagicMock()
    skill_registry.skills = {}
    skill_registry.reload_skill = MagicMock(return_value=True)

    mock = MagicMock()
    mock.app_registry = registry
    mock.skill_registry = skill_registry
    mock.marketplace = None
    mock.vault = None
    mock.supervisor = None
    mock.sessions = {}

    with patch("api.state.state", mock), patch("api.routes.apps.state", mock):
        from api.server import app

        yield TestClient(app, raise_server_exceptions=False), registry, mock, tmp_path

    # getattr, so that against a build without the consent gate these
    # tests fail on the contract they assert rather than erroring in
    # setup on a missing attribute.
    getattr(apps_route, "_pending_app_previews", {}).clear()


# ─────────────────────────────────────────────
# 1. An app install without consent is refused
# ─────────────────────────────────────────────


def test_app_install_without_a_token_is_refused(app_client):
    c, registry, _state, tmp = app_client
    src = _write_app(tmp)

    r = c.post("/api/apps/install", json={"path": str(src), "unsigned": True})

    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "preview_required"
    assert "preview" in detail["message"].lower()
    assert registry.get("notes-app") is None, "an unconsented install still landed"


def test_a_forged_token_is_refused(app_client):
    c, registry, _state, tmp = app_client
    src = _write_app(tmp)

    r = c.post(
        "/api/apps/install",
        json={"path": str(src), "install_token": "not.a-real-token"},
    )

    assert r.status_code == 403, r.text
    assert registry.get("notes-app") is None


def test_a_token_cannot_be_spent_twice(app_client):
    c, registry, _state, tmp = app_client
    src = _write_app(tmp)

    preview = c.post("/api/apps/preview", json={"path": str(src), "unsigned": True}).json()
    token = preview["install_token"]

    first = c.post("/api/apps/install", json={"install_token": token})
    assert first.status_code == 200, first.text
    assert registry.get("notes-app") is not None

    registry.uninstall("notes-app")
    replay = c.post("/api/apps/install", json={"install_token": token})
    assert replay.status_code == 403, replay.text
    assert registry.get("notes-app") is None


def test_a_token_cannot_be_redirected_to_another_source(app_client):
    c, registry, _state, tmp = app_client
    src = _write_app(tmp)
    other = _write_app(tmp, app_id="other-app")

    preview = c.post("/api/apps/preview", json={"path": str(src), "unsigned": True}).json()

    r = c.post(
        "/api/apps/install",
        json={"path": str(other), "install_token": preview["install_token"]},
    )
    assert r.status_code == 403, r.text
    assert registry.get("other-app") is None


def test_preview_installs_nothing(app_client):
    c, registry, _state, tmp = app_client
    src = _write_app(tmp)

    r = c.post("/api/apps/preview", json={"path": str(src), "unsigned": True})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["app"]["app_id"] == "notes-app"
    assert body["install_token"]
    assert registry.get("notes-app") is None, "preview must not install"


# ─────────────────────────────────────────────
# 2. The unverified install path is unreachable over HTTP
# ─────────────────────────────────────────────


def test_no_http_route_calls_the_unverified_marketplace_install():
    """Static proof, so a future edit re-opening the hole fails the build.

    ``MarketplaceClient.install`` takes whatever the URL or the GitHub
    index hands back and checks no signature. ``install_from_registry``
    is the verified one. No module under ``api/routes/`` may call the
    former on anything that looks like a marketplace client.
    """
    routes_dir = Path(__file__).resolve().parents[1] / "api" / "routes"
    offenders: list[str] = []

    for path in sorted(routes_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "install":
                continue
            receiver = ast.unparse(func.value).lower()
            if "marketplace" in receiver:
                offenders.append(f"{path.name}:{node.lineno}: {ast.unparse(node)[:100]}")

    assert not offenders, (
        "an HTTP route reaches the unverified MarketplaceClient.install:\n"
        + "\n".join(offenders)
    )


def test_dependencies_install_over_the_verified_path(app_client):
    """Runtime proof: the unverified method raises if anything calls it."""
    c, registry, brain, tmp = app_client
    market = FakeMarketplace({"trail_notes": {"permissions": ["filesystem"]}})
    brain.marketplace = market
    src = _write_app(tmp, deps=["trail_notes"])

    preview = c.post("/api/apps/preview", json={"path": str(src), "unsigned": True}).json()
    r = c.post("/api/apps/install", json={"install_token": preview["install_token"]})

    assert r.status_code == 200, r.text
    # Reached install_from_registry, spending the preview token.
    assert market.installed == ["trail_notes"]
    assert registry.get("notes-app") is not None


# ─────────────────────────────────────────────
# 3. The disclosed skill set is the installed skill set
# ─────────────────────────────────────────────


def test_disclosed_skill_set_matches_what_gets_installed(app_client):
    c, registry, brain, tmp = app_client
    market = FakeMarketplace({
        "trail_notes": {"permissions": ["filesystem", "network"], "name": "Trail Notes"},
        "map_tiles": {"permissions": ["network"], "name": "Map Tiles"},
    })
    brain.marketplace = market
    src = _write_app(tmp, deps=["trail_notes", "map_tiles"])

    preview = c.post("/api/apps/preview", json={"path": str(src), "unsigned": True}).json()
    disclosed = preview["skill_dependencies"]

    assert [d["skill_id"] for d in disclosed["to_install"]] == ["trail_notes", "map_tiles"]
    # Named, with what each one reaches, before the user commits.
    by_id = {d["skill_id"]: d for d in disclosed["to_install"]}
    assert by_id["trail_notes"]["name"] == "Trail Notes"
    assert by_id["trail_notes"]["permissions"] == ["filesystem", "network"]
    assert by_id["map_tiles"]["permissions"] == ["network"]
    assert all(d["signature"]["verified"] for d in disclosed["to_install"])
    assert preview["degraded"] is False

    r = c.post("/api/apps/install", json={"install_token": preview["install_token"]})
    assert r.status_code == 200, r.text
    body = r.json()

    assert sorted(market.installed) == ["map_tiles", "trail_notes"]
    assert sorted(body["skill_dependencies"]["installed"]) == ["map_tiles", "trail_notes"]
    assert sorted(d["skill_id"] for d in disclosed["to_install"]) == sorted(market.installed)


def test_app_permissions_are_described_before_install(app_client):
    c, _registry, _brain, tmp = app_client
    src = tmp / "src-wide"
    src.mkdir()
    manifest = json.loads(
        AppManifest(
            app_id="wide-app",
            brand=BrandProfile(name="Wide"),
            surfaces=[
                SurfaceSpec(
                    surface_id="home",
                    kind="authored",
                    template_root={"type": "Text", "value": "x"},
                    action_contract=[ActionSpec(action_id="open", handler="app_event")],
                )
            ],
            entry_surface_id="home",
        ).model_dump_json()
    )
    manifest["permissions"] = {"network": ["api.acme.com"]}
    (src / "manifest.json").write_text(json.dumps(manifest))

    preview = c.post("/api/apps/preview", json={"path": str(src), "unsigned": True}).json()

    rows = preview["permission_details"]
    assert [r["id"] for r in rows] == ["network:api.acme.com"]
    assert "api.acme.com" in rows[0]["description"]
    assert all(r["label"] and r["description"] for r in rows)


def test_an_app_with_no_network_says_so(app_client):
    c, _registry, _brain, tmp = app_client
    src = _write_app(tmp)

    preview = c.post("/api/apps/preview", json={"path": str(src), "unsigned": True}).json()

    rows = preview["permission_details"]
    assert [r["id"] for r in rows] == ["network:none"]
    assert "cannot contact any server" in rows[0]["description"]


# ─────────────────────────────────────────────
# 4. An unverifiable dependency is a signpost, not a dead end
# ─────────────────────────────────────────────


def test_unverifiable_dependency_is_disclosed_with_reason_impact_and_remediation(app_client):
    c, _registry, brain, tmp = app_client
    brain.marketplace = FakeMarketplace({})  # nothing published
    src = _write_app(tmp, deps=["ghost_skill"])

    preview = c.post("/api/apps/preview", json={"path": str(src), "unsigned": True}).json()

    assert preview["success"] is True
    assert preview["degraded"] is True
    # A token still issues: the user gets to make the call.
    assert preview["install_token"]

    unavailable = preview["skill_dependencies"]["unavailable"]
    assert [d["skill_id"] for d in unavailable] == ["ghost_skill"]
    row = unavailable[0]

    # (1) the brain's own reason, not a generic string
    assert "not published in the registry" in row["reason"]
    assert row["reason"] != "Dependency resolution failed"

    # (2) how to get it, concretely
    assert row["remediation"]["code"] == "not_published"
    assert "ghost_skill" in row["remediation"]["message"]
    assert "feral publish --skill" in row["remediation"]["action"]

    # (3) what the app cannot do without it
    assert row["impact"], "the actions that break must be named"
    assert row["impact"][0]["action_id"] == "sync_0"
    assert row["impact"][0]["surface_id"] == "home"
    assert row["impact"][0]["target"] == "ghost_skill/sync"


def test_a_signature_failure_does_not_advise_a_command_that_would_fail(app_client):
    c, _registry, brain, tmp = app_client
    brain.marketplace = FakeMarketplace({"bad_skill": {"signature_fails": True}})
    src = _write_app(tmp, deps=["bad_skill"])

    preview = c.post("/api/apps/preview", json={"path": str(src), "unsigned": True}).json()

    row = preview["skill_dependencies"]["unavailable"][0]
    assert row["remediation"]["code"] == "signature_failed"
    # `feral install` runs the same verifier, so offering it would send
    # the user in a circle.
    assert row["remediation"]["command"] == ""
    assert "retrying will not help" in row["remediation"]["action"].lower()


def test_app_installs_degraded_and_keeps_saying_what_is_missing(app_client):
    c, registry, brain, tmp = app_client
    brain.marketplace = FakeMarketplace({})
    src = _write_app(tmp, deps=["ghost_skill"])

    preview = c.post("/api/apps/preview", json={"path": str(src), "unsigned": True}).json()
    r = c.post("/api/apps/install", json={"install_token": preview["install_token"]})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["degraded"] is True
    assert registry.get("notes-app") is not None, (
        "refusing outright leaves the user with nothing and no next step"
    )
    assert [d["skill_id"] for d in body["skill_dependencies"]["unavailable"]] == ["ghost_skill"]

    # And the Apps listing keeps saying it, rather than the fact living
    # only in an install response nobody kept.
    listing = c.get("/api/apps").json()["apps"][0]
    missing = listing["missing_skill_dependencies"]
    assert [m["skill_id"] for m in missing] == ["ghost_skill"]
    assert missing[0]["remediation"]["action"]
    assert missing[0]["impact"][0]["action_id"] == "sync_0"


def test_a_dependency_that_verified_then_failed_rolls_the_app_back(app_client):
    """The consented degraded path is one thing; a broken invariant is another."""
    c, registry, brain, tmp = app_client
    brain.marketplace = FakeMarketplace({
        "flaky_skill": {"permissions": ["network"], "install_fails": True},
    })
    src = _write_app(tmp, deps=["flaky_skill"])

    preview = c.post("/api/apps/preview", json={"path": str(src), "unsigned": True}).json()
    assert preview["degraded"] is False, "it verified at preview time"

    r = c.post("/api/apps/install", json={"install_token": preview["install_token"]})

    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "skill_dependency_install_failed"
    assert detail["failed"][0]["skill_id"] == "flaky_skill"
    assert detail["failed"][0]["remediation"]["action"]
    assert registry.get("notes-app") is None, "a half-working app must not claim to be installed"


# ─────────────────────────────────────────────
# 5. Already installed is a different decision from newly installed
# ─────────────────────────────────────────────


def test_already_installed_dependency_is_not_presented_as_new(app_client):
    c, _registry, brain, tmp = app_client
    market = FakeMarketplace({"map_tiles": {"permissions": ["network"], "name": "Map Tiles"}})
    brain.marketplace = market
    brain.skill_registry.skills = {"trail_notes": _skill("trail_notes", ["filesystem"])}
    src = _write_app(tmp, deps=["trail_notes", "map_tiles"])

    preview = c.post("/api/apps/preview", json={"path": str(src), "unsigned": True}).json()
    disclosed = preview["skill_dependencies"]

    assert [d["skill_id"] for d in disclosed["already_installed"]] == ["trail_notes"]
    assert [d["skill_id"] for d in disclosed["to_install"]] == ["map_tiles"]
    # The one already present is still described, so the app's total
    # reach is visible, but it is not the new decision.
    assert disclosed["already_installed"][0]["permissions"] == ["filesystem"]
    # It is never re-downloaded or re-previewed.
    assert market.previewed == ["map_tiles"]

    r = c.post("/api/apps/install", json={"install_token": preview["install_token"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skill_dependencies"]["installed"] == ["map_tiles"]
    assert body["skill_dependencies"]["already_present"] == ["trail_notes"]
    assert market.installed == ["map_tiles"]
