"""A name in a manifest, through a real registry, into ``to_install``.

This is the path that was broken end to end. An ``AppManifest`` declares
``skill_dependencies: ["robot_ext"]``; ``_describe_dependencies``
previews each one through ``MarketplaceClient``; the client called
``GET /api/v1/item/robot_ext``; and the registry matched only
``Item.id``, a UUID. So every name-declared dependency landed in
``unavailable`` with "not published in the registry" while the same item
sat in the catalog, and the ``to_install`` bucket was unreachable in
practice.

Nothing here is stubbed between the two halves. The registry is the real
``feral_registry`` FastAPI app over a temp SQLite database, reached over
an in-process ASGI transport; the client is a real ``MarketplaceClient``;
the bundle is really signed, really verified through
``cli.install._verify``, and really unpacked and installed.

Skipped when ``feral_registry`` is not importable: it is a separate
deployable and feral-core's CI installs only ``feral-core[all,dev]``.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile

import pytest
from unittest.mock import MagicMock, patch

pytest.importorskip("feral_registry", reason="feral-registry is not installed here")

import httpx  # noqa: E402
from httpx import ASGITransport  # noqa: E402
from nacl.signing import SigningKey  # noqa: E402

REGISTRY_BASE = "http://registry.test"
REVIEWER_SECRET = "e2e-reviewer-secret"

ROBOT_EXT_MANIFEST = {
    "skill_id": "robot_ext",
    "version": "0.1.0",
    "author": "feral-core",
    "brand": {"name": "Robot Node", "primary_color": "#ff3838"},
    "description": "Standardized control capabilities for a robot actuator.",
    "permissions": ["hardware"],
    "endpoints": [
        {
            "id": "robot_move",
            "method": "WS_EXECUTE",
            "url": "local_daemon",
            "description": "Moves the robotic chassis.",
        },
    ],
}


def _bundle(manifest: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for arcname, data in (
            ("manifest.json", json.dumps(manifest).encode()),
            ("impl.py", b"def run():\n    return 'ok'\n"),
        ):
            info = tarfile.TarInfo(arcname)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
async def registry_client(tmp_path, monkeypatch):
    """A live in-process registry with ``robot_ext`` published and approved."""
    monkeypatch.setenv("FERAL_REGISTRY_BLOB_DIR", str(tmp_path / "blobs"))
    monkeypatch.setenv(
        "FERAL_REGISTRY_DB_URL", f"sqlite+aiosqlite:///{tmp_path / 'registry.db'}"
    )
    monkeypatch.setenv("JWT_SECRET", "e2e-secret")
    monkeypatch.setenv("FEATURED_PUBLISHERS", "feral")
    monkeypatch.setenv("FERAL_REGISTRY_PUBLIC_URL", REGISTRY_BASE)
    monkeypatch.setenv("FERAL_REGISTRY_REVIEWER_SECRET", REVIEWER_SECRET)

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from feral_registry import config as config_mod

    config_mod.get_settings.cache_clear()  # type: ignore[attr-defined]
    settings = config_mod.get_settings()

    from feral_registry import db as db_mod
    from feral_registry import main as main_mod
    from feral_registry import models as models_mod

    engine = create_async_engine(settings.db_url, echo=False, future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, class_=db_mod.AsyncSession
    )
    monkeypatch.setattr(db_mod, "engine", engine, raising=False)
    monkeypatch.setattr(db_mod, "SessionLocal", session_factory, raising=False)

    app = main_mod.create_app()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=REGISTRY_BASE) as http:
        async with engine.begin() as conn:
            await conn.run_sync(db_mod.Base.metadata.create_all)

        sk = SigningKey.generate()
        async with session_factory() as session:
            session.add(
                models_mod.Publisher(
                    github_login="feral",
                    github_id=1,
                    pubkey_hex=sk.verify_key.encode().hex(),
                )
            )
            await session.commit()

        from feral_registry.auth import issue_publisher_token

        token, _ = issue_publisher_token("feral", settings)

        bundle = _bundle(ROBOT_EXT_MANIFEST)
        sha = hashlib.sha256(bundle).hexdigest()
        sig = base64.b64encode(sk.sign(sha.encode("ascii")).signature).decode()
        envelope = {
            "kind": "skill",
            "name": "robot_ext",
            "version": "0.1.0",
            "description": ROBOT_EXT_MANIFEST["description"],
            "author": "feral-core",
            "skill_id": "robot_ext",
        }
        resp = await http.post(
            "/api/v1/publish",
            headers={"Authorization": f"Bearer {token}"},
            files={"bundle": ("robot_ext.tar.gz", bundle, "application/gzip")},
            data={"signature": sig, "manifest_json": json.dumps(envelope)},
        )
        assert resp.status_code == 200, resp.text
        item_id = resp.json()["id"]

        resp = await http.post(
            f"/api/v1/review/{item_id}/approve",
            json={},
            headers={
                "Authorization": f"Bearer {REVIEWER_SECRET}",
                "X-Reviewer-Actor": "e2e",
            },
        )
        assert resp.status_code == 200, resp.text

        yield http, item_id

    await engine.dispose()


@pytest.fixture
def skills_dir(tmp_path, monkeypatch):
    import skills.marketplace as mp
    import skills.package as pkg

    target = tmp_path / "installed-skills"
    target.mkdir()
    monkeypatch.setattr(pkg, "SKILLS_DIR", target)
    monkeypatch.setattr(mp, "SKILLS_DIR", target)
    return target


@pytest.fixture
def marketplace(registry_client, monkeypatch):
    """A real MarketplaceClient pointed at the in-process registry."""
    import cli.publish
    from skills.marketplace import MarketplaceClient
    from skills.registry import SkillRegistry

    http, _ = registry_client
    monkeypatch.setattr(cli.publish, "registry_base_urls", lambda *a, **k: [REGISTRY_BASE])

    skill_registry = SkillRegistry()
    client = MarketplaceClient(skill_registry=skill_registry)
    client._client = http
    return client


# ─────────────────────────────────────────────
# The registry half, exercised by the real client
# ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_name_resolves_and_a_uuid_still_does(marketplace, registry_client):
    _, item_id = registry_client

    by_name = await marketplace._registry_fetch_item("robot_ext", kind="skill")
    by_id = await marketplace._registry_fetch_item(item_id, kind="skill")

    assert by_name["id"] == item_id
    assert by_name["name"] == "robot_ext"
    assert by_id == by_name


@pytest.mark.asyncio
async def test_an_unpublished_name_is_still_reported_as_unpublished(marketplace):
    from skills.marketplace import MarketplaceError

    with pytest.raises(MarketplaceError) as exc:
        await marketplace._registry_fetch_item("no_such_skill", kind="skill")
    assert "not published in the registry" in str(exc.value)


@pytest.mark.asyncio
async def test_preview_by_name_verifies_the_signed_bundle(marketplace, skills_dir):
    preview = await marketplace.preview_from_registry("skill", "robot_ext")

    assert preview.get("success") is True, preview
    assert preview["name"] == "Robot Node"
    assert preview["version"] == "0.1.0"
    assert preview["permissions"] == ["hardware"]
    # Read out of the verified archive, not out of the registry's own
    # metadata field, which the signature does not cover.
    assert preview["permissions_source"] == "package"
    assert preview["signature"]["verified"] is True
    assert preview["install_token"]
    # Nothing on disk yet.
    assert list(skills_dir.iterdir()) == []

    marketplace.release_preview(preview["install_token"])


# ─────────────────────────────────────────────
# The app half: manifest -> preview -> to_install -> installed
# ─────────────────────────────────────────────


def _app_manifest(deps: list[str]) -> dict:
    return {
        "app_id": "robot-console",
        "version": "1.0.0",
        "description": "Drives a robot.",
        "brand": {"name": "Robot Console"},
        "skill_dependencies": list(deps),
        "entry_surface_id": "home",
        "surfaces": [
            {
                "surface_id": "home",
                "kind": "authored",
                "template_root": {"type": "Text", "value": "hi"},
                "action_contract": [
                    {
                        "action_id": "move",
                        "handler": "skill_call",
                        "target": f"{deps[0]}/robot_move",
                        "description": "Move the robot",
                    },
                ],
            },
        ],
    }


@pytest.mark.asyncio
async def test_declared_dependency_reaches_to_install_not_unavailable(
    marketplace, skills_dir
):
    from api.routes import apps as apps_route
    from skills.registry import SkillRegistry

    brain = MagicMock()
    brain.marketplace = marketplace
    brain.skill_registry = SkillRegistry()
    brain.skill_registry.skills = {}

    with patch("api.routes.apps.state", brain):
        disclosure, dep_tokens = await apps_route._describe_dependencies(
            _app_manifest(["robot_ext"])
        )

    assert disclosure["declared"] == ["robot_ext"]
    assert disclosure["unavailable"] == [], disclosure["unavailable"]
    assert [d["skill_id"] for d in disclosure["to_install"]] == ["robot_ext"]

    row = disclosure["to_install"][0]
    assert row["name"] == "Robot Node"
    assert row["version"] == "0.1.0"
    assert row["publisher"] == "feral"
    assert row["permissions"] == ["hardware"]
    assert row["signature"]["verified"] is True
    assert [d["id"] for d in row["permission_details"]] == ["hardware"]

    assert set(dep_tokens) == {"robot_ext"}
    marketplace.release_preview(dep_tokens["robot_ext"])


@pytest.mark.asyncio
async def test_the_disclosed_dependency_installs_and_loads(marketplace, skills_dir):
    from api.routes import apps as apps_route
    from skills.registry import SkillRegistry

    skill_registry = SkillRegistry()
    skill_registry.skills = {}
    marketplace._skill_registry = skill_registry

    brain = MagicMock()
    brain.marketplace = marketplace
    brain.skill_registry = skill_registry

    with patch("api.routes.apps.state", brain):
        _disclosure, dep_tokens = await apps_route._describe_dependencies(
            _app_manifest(["robot_ext"])
        )

    result = await marketplace.install_from_registry(
        "skill", "robot_ext", install_token=dep_tokens["robot_ext"]
    )

    assert result.get("success") is True, result
    assert result["skill_id"] == "robot_ext"
    assert result["permissions"] == ["hardware"]

    installed = skills_dir / "robot_ext"
    assert (installed / "manifest.json").is_file()
    assert (installed / "impl.py").is_file()

    # It loads: the manifest on disk is a SkillManifest, and the brain's
    # registry holds it.
    from skills.package import SkillPackage

    pkg = SkillPackage(installed)
    assert pkg.load(), pkg.errors
    assert pkg.manifest is not None
    assert pkg.manifest.brand.name == "Robot Node"
    assert "robot_ext" in skill_registry.skills


@pytest.mark.asyncio
async def test_an_undeclared_name_still_lands_in_unavailable(marketplace, skills_dir):
    """The degraded path stays honest for a name that really is missing."""
    from api.routes import apps as apps_route
    from skills.registry import SkillRegistry

    brain = MagicMock()
    brain.marketplace = marketplace
    brain.skill_registry = SkillRegistry()
    brain.skill_registry.skills = {}

    with patch("api.routes.apps.state", brain):
        disclosure, dep_tokens = await apps_route._describe_dependencies(
            _app_manifest(["ghost_skill"])
        )

    assert disclosure["to_install"] == []
    assert dep_tokens == {}
    assert [d["skill_id"] for d in disclosure["unavailable"]] == ["ghost_skill"]
    assert "not published in the registry" in disclosure["unavailable"][0]["reason"]
