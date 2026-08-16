"""Installing a third-party skill must be verified and consented to.

Covers WORK-ORDER P0.2 (install asks nothing; uninstall asks) and P0.3
(the UI takes the unsigned install path under a "signed registry"
header).

Four contracts:

1. A tampered tarball is refused and nothing lands on disk.
2. The UI install path and the CLI install path reach the *same*
   verifier (``cli.install._verify``), so there is one signature
   implementation and not two that can drift.
3. ``POST /api/marketplace/install`` without a valid preview token is
   refused, so the permission list the user was shown is the permission
   list of the package that gets installed.
4. A manifest carrying a permission outside the closed vocabulary fails
   to load, so the list rendered in the consent dialog is drawn from a
   fixed set the UI can actually describe.
"""

from __future__ import annotations

import base64
import io
import json
import tarfile
import tempfile
from hashlib import sha256
from pathlib import Path

import pytest

from nacl.encoding import HexEncoder
from nacl.signing import SigningKey

from skills.marketplace import MarketplaceClient


VALID_MANIFEST = {
    "skill_id": "consent_demo",
    "version": "1.2.3",
    "author": "someone",
    "brand": {"name": "Consent Demo"},
    "description": "A demo skill used by the install-consent tests.",
    "permissions": ["filesystem", "network"],
    "endpoints": [],
}


def _bundle_bytes(manifest: dict | None = None, top: str = "consent_demo") -> bytes:
    """A tar.gz holding ``<top>/manifest.json``, as the registry serves."""
    payload = json.dumps(manifest if manifest is not None else VALID_MANIFEST).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name=f"{top}/manifest.json")
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def _signed_item(bundle: bytes, manifest: dict | None = None, kind: str = "skill") -> dict:
    """The registry ``/api/v1/item/{id}`` response for ``bundle``."""
    sk = SigningKey.generate()
    digest_hex = sha256(bundle).hexdigest()
    sig = sk.sign(digest_hex.encode("ascii")).signature
    return {
        "id": "consent_demo",
        "kind": kind,
        "download_url": "https://registry.invalid/bundle.tar.gz",
        "sha256": digest_hex,
        "signature_b64": base64.b64encode(sig).decode(),
        "publisher_pubkey_hex": sk.verify_key.encode(HexEncoder).decode(),
        "publisher": "test-publisher",
        "manifest": manifest if manifest is not None else VALID_MANIFEST,
    }


@pytest.fixture()
def skills_dir(tmp_path, monkeypatch):
    """Point every install target at a throwaway directory."""
    import skills.marketplace as mp
    import skills.package as pkg

    target = tmp_path / "skills"
    target.mkdir()
    monkeypatch.setattr(pkg, "SKILLS_DIR", target)
    monkeypatch.setattr(mp, "SKILLS_DIR", target)
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    return target


def _wire_registry(client: MarketplaceClient, monkeypatch, item: dict, bundle: bytes, tmp_path):
    """Serve ``item`` / ``bundle`` without touching the network."""
    async def fake_fetch(item_id: str):
        return item

    async def fake_download(url: str):
        # A dedicated directory per download, as the real
        # ``_registry_download`` does: the caller owns the staging root
        # and deletes it when the preview is consumed or dropped.
        stage = Path(tempfile.mkdtemp(prefix="feral-test-dl-", dir=tmp_path))
        out = stage / "bundle.tar.gz"
        out.write_bytes(bundle)
        return out

    monkeypatch.setattr(client, "_registry_fetch_item", fake_fetch)
    monkeypatch.setattr(client, "_registry_download", fake_download)


# ─────────────────────────────────────────────
# 1. A tampered tarball is refused
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tampered_tarball_is_refused(tmp_path, monkeypatch, skills_dir):
    bundle = _bundle_bytes()
    item = _signed_item(bundle)
    # Same package id, different bytes: the archive was modified in
    # transit (or by the registry) after the publisher signed it.
    tampered = _bundle_bytes(
        {**VALID_MANIFEST, "description": "Backdoored after signing."}
    )
    assert sha256(tampered).hexdigest() != item["sha256"]

    client = MarketplaceClient()
    _wire_registry(client, monkeypatch, item, tampered, tmp_path)

    result = await client.preview_from_registry("skill", "consent_demo")

    assert result.get("success") is False, result
    assert "signature" in (result.get("error") or "").lower()
    assert not (skills_dir / "consent_demo").exists()
    await client.close()


@pytest.mark.asyncio
async def test_bad_signature_is_refused(tmp_path, monkeypatch, skills_dir):
    """Correct sha256, signature from the wrong key."""
    bundle = _bundle_bytes()
    item = _signed_item(bundle)
    other = SigningKey.generate()
    item["publisher_pubkey_hex"] = other.verify_key.encode(HexEncoder).decode()

    client = MarketplaceClient()
    _wire_registry(client, monkeypatch, item, bundle, tmp_path)

    result = await client.preview_from_registry("skill", "consent_demo")

    assert result.get("success") is False, result
    assert not (skills_dir / "consent_demo").exists()
    await client.close()


# ─────────────────────────────────────────────
# 2. UI path and CLI path reach the same verifier
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ui_and_cli_install_reach_the_same_verifier(tmp_path, monkeypatch, skills_dir):
    import cli.install as cli_install

    calls: list[str] = []
    real_verify = cli_install._verify

    def spy(item, tarball):
        calls.append(str(tarball))
        return real_verify(item, tarball)

    monkeypatch.setattr(cli_install, "_verify", spy)

    bundle = _bundle_bytes()
    item = _signed_item(bundle)

    # UI path: MarketplaceClient (what /api/marketplace/* drives).
    client = MarketplaceClient()
    _wire_registry(client, monkeypatch, item, bundle, tmp_path)
    ui_result = await client.preview_from_registry("skill", "consent_demo")
    assert ui_result.get("success") is True, ui_result
    assert len(calls) == 1, "UI install path did not call cli.install._verify"
    await client.close()

    # CLI path: `feral install <id>`.
    tarball = tmp_path / "cli-bundle.tar.gz"
    tarball.write_bytes(bundle)
    monkeypatch.setattr(cli_install, "_fetch_item", lambda base, item_id: item)
    monkeypatch.setattr(cli_install, "_download_bundle", lambda url: tarball)
    monkeypatch.setattr(cli_install, "dispatch_install", lambda *a, **k: None)
    cli_install.cmd_install("consent_demo")

    assert len(calls) == 2, "CLI install path did not call cli.install._verify"


# ─────────────────────────────────────────────
# 3. Install without a valid preview token is refused
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_install_without_preview_token_is_refused(tmp_path, monkeypatch, skills_dir):
    from api.routes import marketplace_browser as route

    bundle = _bundle_bytes()
    item = _signed_item(bundle)
    client = MarketplaceClient()
    _wire_registry(client, monkeypatch, item, bundle, tmp_path)
    monkeypatch.setattr(route.state, "marketplace", client)

    resp = await route.marketplace_install({"kind": "skill", "id": "consent_demo"})
    body = json.loads(resp.body) if hasattr(resp, "body") else resp

    assert body.get("success") is False, body
    assert "preview" in (body.get("error") or "").lower()
    assert not (skills_dir / "consent_demo").exists()
    await client.close()


@pytest.mark.asyncio
async def test_preview_token_cannot_be_replayed_for_another_package(
    tmp_path, monkeypatch, skills_dir
):
    bundle = _bundle_bytes()
    item = _signed_item(bundle)
    client = MarketplaceClient()
    _wire_registry(client, monkeypatch, item, bundle, tmp_path)

    preview = await client.preview_from_registry("skill", "consent_demo")
    token = preview["install_token"]

    # Same token, different item id.
    other = await client.install_from_registry("skill", "something_else", install_token=token)
    assert other.get("success") is False, other

    # The token still works for the package it was issued for...
    ok = await client.install_from_registry("skill", "consent_demo", install_token=token)
    assert ok.get("success") is True, ok

    # ...exactly once.
    replay = await client.install_from_registry("skill", "consent_demo", install_token=token)
    assert replay.get("success") is False, replay
    await client.close()


@pytest.mark.asyncio
async def test_install_with_preview_token_reports_permissions(tmp_path, monkeypatch, skills_dir):
    bundle = _bundle_bytes()
    item = _signed_item(bundle)
    client = MarketplaceClient()
    _wire_registry(client, monkeypatch, item, bundle, tmp_path)

    preview = await client.preview_from_registry("skill", "consent_demo")
    assert preview["permissions"] == ["filesystem", "network"]
    assert preview["signature"]["verified"] is True
    assert {d["id"] for d in preview["permission_details"]} == {"filesystem", "network"}
    assert all(d["description"] for d in preview["permission_details"])

    result = await client.install_from_registry(
        "skill", "consent_demo", install_token=preview["install_token"]
    )
    assert result.get("success") is True, result
    assert result["permissions"] == ["filesystem", "network"]
    assert (skills_dir / "consent_demo" / "manifest.json").exists()
    await client.close()


# ─────────────────────────────────────────────
# 4. The permission vocabulary is closed
# ─────────────────────────────────────────────

def test_out_of_vocabulary_permission_fails_to_load():
    from pydantic import ValidationError

    from models.skill_manifest import SkillManifest

    bad = {**VALID_MANIFEST, "permissions": ["filesystem", "root_everything"]}
    with pytest.raises(ValidationError):
        SkillManifest(**bad)


def test_package_with_out_of_vocabulary_permission_does_not_load(tmp_path):
    from skills.package import SkillPackage

    pkg_dir = tmp_path / "bad_pkg"
    pkg_dir.mkdir()
    (pkg_dir / "manifest.json").write_text(
        json.dumps({**VALID_MANIFEST, "permissions": ["root_everything"]})
    )

    pkg = SkillPackage(pkg_dir)
    assert pkg.load() is False
    assert any("permission" in e.lower() or "root_everything" in e for e in pkg.errors), pkg.errors


def test_every_shipped_manifest_still_loads():
    from models.skill_manifest import SkillManifest

    root = Path(__file__).resolve().parents[1] / "skills" / "manifests"
    failures = []
    for path in sorted(root.glob("*.json")):
        data = json.loads(path.read_text())
        try:
            SkillManifest(**data)
        except Exception as exc:  # noqa: BLE001 - collected and reported below
            failures.append(f"{path.name}: {exc}")
    assert not failures, "shipped manifests no longer load:\n" + "\n".join(failures)


def test_get_metadata_reports_permissions(tmp_path):
    from skills.package import SkillPackage

    pkg_dir = tmp_path / "consent_demo"
    pkg_dir.mkdir()
    (pkg_dir / "manifest.json").write_text(json.dumps(VALID_MANIFEST))

    pkg = SkillPackage(pkg_dir)
    assert pkg.load() is True
    meta = pkg.get_metadata()
    assert meta["permissions"] == ["filesystem", "network"]
    assert [d["id"] for d in meta["permission_details"]] == ["filesystem", "network"]


# ─────────────────────────────────────────────
# 5. Validation happens before extraction
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_invalid_archive_never_lands_in_the_skills_dir(skills_dir):
    """An archive whose manifest is rejected must not leave a directory."""
    bad_manifest = {**VALID_MANIFEST, "permissions": ["root_everything"]}
    client = MarketplaceClient()

    result = await client._install_from_archive("consent_demo", _bundle_bytes(bad_manifest))

    assert result["success"] is False, result
    assert not (skills_dir / "consent_demo").exists()
    await client.close()
