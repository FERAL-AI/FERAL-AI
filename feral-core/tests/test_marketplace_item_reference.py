"""What the marketplace client asks the registry for, and how it reports back.

``MarketplaceClient._registry_fetch_item`` calls ``GET
/api/v1/item/{ref}``. Every caller holds a **name**: an ``AppManifest``
declares ``skill_dependencies: ["robot_ext"]``, a user runs ``feral
install robot_ext``, the catalog lists ``robot_ext``. Nobody holds the
UUID primary key, so the reference the client sends has to be the name,
and the registry has to be told which ``kind`` is meant (a name is
unique per ``(kind, version)``, not globally).

These pin the request shape and the error copy. The resolution rules
themselves live in ``feral-registry`` and are tested there;
``tests/test_app_dependency_end_to_end.py`` runs both halves together
against a real registry instance.
"""

from __future__ import annotations

import base64
import io
import json
import tarfile
from hashlib import sha256
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from nacl.encoding import HexEncoder
from nacl.signing import SigningKey

from skills.marketplace import MarketplaceClient, MarketplaceError

REGISTRY = "https://registry.test"

SKILL_MANIFEST = {
    "skill_id": "robot_ext",
    "version": "0.1.0",
    "author": "feral",
    "brand": {"name": "Robot Node", "primary_color": "#ff3838"},
    "description": "Standardized control capabilities for a robot actuator.",
    "permissions": ["hardware"],
    "endpoints": [],
}


@pytest.fixture(autouse=True)
def _pin_registry(monkeypatch):
    """One base URL, so a request list is unambiguous.

    Patched at the function rather than through ``FERAL_REGISTRY_URL``:
    ``_registry_fetch_item`` imports ``registry_base_urls`` at call time,
    and this keeps the test out of ``os.environ`` entirely.
    """
    import cli.publish

    monkeypatch.setattr(cli.publish, "registry_base_urls", lambda *a, **k: [REGISTRY])


def _bundle(manifest: dict) -> bytes:
    payload = json.dumps(manifest).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def _item_record(bundle: bytes, name: str = "robot_ext", manifest: dict | None = None) -> dict:
    sk = SigningKey.generate()
    digest_hex = sha256(bundle).hexdigest()
    return {
        "id": "b47d0005-231b-4603-bd87-58cb247f3cd6",
        "kind": "skill",
        "name": name,
        "version": "0.1.0",
        "manifest": manifest if manifest is not None else SKILL_MANIFEST,
        "publisher": "feral",
        "publisher_pubkey": sk.verify_key.encode(HexEncoder).decode(),
        "sha256": digest_hex,
        "signature_b64": base64.b64encode(
            sk.sign(digest_hex.encode("ascii")).signature
        ).decode(),
        "download_url": f"{REGISTRY}/api/v1/blobs/{digest_hex}",
    }


def _wire(client: MarketplaceClient, handler) -> list[httpx.Request]:
    """Route the client's HTTP through ``handler``, recording requests."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client._client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return seen


# ─────────────────────────────────────────────
# The reference the client sends
# ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_name_is_sent_verbatim_with_its_kind():
    client = MarketplaceClient()
    record = _item_record(_bundle(SKILL_MANIFEST))
    seen = _wire(client, lambda req: httpx.Response(200, json=record))

    got = await client._registry_fetch_item("robot_ext", kind="skill")

    assert got["name"] == "robot_ext"
    assert len(seen) == 1
    url = urlparse(str(seen[0].url))
    assert url.path == "/api/v1/item/robot_ext"
    assert parse_qs(url.query) == {"kind": ["skill"]}


@pytest.mark.asyncio
async def test_a_uuid_is_still_sent_verbatim():
    client = MarketplaceClient()
    record = _item_record(_bundle(SKILL_MANIFEST))
    seen = _wire(client, lambda req: httpx.Response(200, json=record))

    await client._registry_fetch_item("b47d0005-231b-4603-bd87-58cb247f3cd6", kind="skill")

    assert urlparse(str(seen[0].url)).path == (
        "/api/v1/item/b47d0005-231b-4603-bd87-58cb247f3cd6"
    )


@pytest.mark.asyncio
async def test_no_kind_sends_no_kind_parameter():
    client = MarketplaceClient()
    record = _item_record(_bundle(SKILL_MANIFEST))
    seen = _wire(client, lambda req: httpx.Response(200, json=record))

    await client._registry_fetch_item("robot_ext")

    assert urlparse(str(seen[0].url)).query == ""


@pytest.mark.asyncio
async def test_preview_passes_its_kind_through():
    """The kind is known at the call site, so ambiguity never arises."""
    bundle = _bundle(SKILL_MANIFEST)
    record = _item_record(bundle)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/v1/blobs/"):
            return httpx.Response(200, content=bundle)
        return httpx.Response(200, json=record)

    client = MarketplaceClient()
    seen = _wire(client, handler)

    result = await client.preview_from_registry("skill", "robot_ext")
    assert result.get("success") is True, result
    client.release_preview(result["install_token"])

    item_requests = [r for r in seen if "/api/v1/item/" in str(r.url)]
    assert item_requests, seen
    assert parse_qs(urlparse(str(item_requests[0].url)).query) == {"kind": ["skill"]}


# ─────────────────────────────────────────────
# What the user is told
# ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_404_names_the_reference_that_did_not_resolve():
    client = MarketplaceClient()
    _wire(client, lambda req: httpx.Response(404, json={"detail": "item not found"}))

    with pytest.raises(MarketplaceError) as exc:
        await client._registry_fetch_item("robot_ext", kind="skill")
    assert "robot_ext" in str(exc.value)


@pytest.mark.asyncio
async def test_409_relays_the_registrys_own_candidate_list():
    """An ambiguous name must not become a bare HTTP status.

    The registry refuses to pick between candidates and says which ones
    it saw; replacing that with "registry returned 409" would leave the
    user with nothing to act on.
    """
    detail = (
        "'robot_ext' matches 2 items by version (1.0.0, 1.0.0-beta); "
        "pass version= to choose one"
    )
    client = MarketplaceClient()
    _wire(client, lambda req: httpx.Response(409, json={"detail": detail}))

    with pytest.raises(MarketplaceError) as exc:
        await client._registry_fetch_item("robot_ext", kind="skill")
    assert str(exc.value) == detail


@pytest.mark.asyncio
async def test_409_without_a_detail_still_says_ambiguous():
    client = MarketplaceClient()
    _wire(client, lambda req: httpx.Response(409, content=b"nope"))

    with pytest.raises(MarketplaceError) as exc:
        await client._registry_fetch_item("robot_ext", kind="skill")
    assert "ambiguous" in str(exc.value)


# ─────────────────────────────────────────────
# `brand` is required, and install is where that is enforced
# ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_bundle_without_brand_is_refused_at_preview(tmp_path, monkeypatch):
    """The decision: ``brand`` stays required for a published skill.

    ``SkillManifest.brand`` has no default, ``SkillPackage.get_metadata``
    serves ``brand.name`` as the skill's display name, and the install
    dialog renders it. The registry now refuses such a bundle at publish
    too, so this and ``feral-registry``'s
    ``test_publish_refuses_the_bundle_that_shipped`` are the two ends of
    one contract.
    """
    import skills.marketplace as mp
    import skills.package as pkg

    target = tmp_path / "skills"
    target.mkdir()
    monkeypatch.setattr(pkg, "SKILLS_DIR", target)
    monkeypatch.setattr(mp, "SKILLS_DIR", target)

    brandless = {k: v for k, v in SKILL_MANIFEST.items() if k != "brand"}
    bundle = _bundle(brandless)
    record = _item_record(bundle, manifest=brandless)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/v1/blobs/"):
            return httpx.Response(200, content=bundle)
        return httpx.Response(200, json=record)

    client = MarketplaceClient()
    _wire(client, handler)

    result = await client.preview_from_registry("skill", "robot_ext")

    assert result.get("success") is False
    assert "invalid package" in result["error"]
    assert "brand" in result["error"]
    assert list(target.iterdir()) == []
