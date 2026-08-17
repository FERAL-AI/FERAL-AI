"""Seed a live registry instance over HTTP.

Unlike ``seed_first_party.py`` which talks to the registry database
directly, this script treats the registry as a black-box HTTP service —
exactly like a real publisher would. It:

1. Issues a publisher token via the registry's internal bootstrap
   endpoint (or skips and asks the operator to paste a token).
2. Generates or loads a ``~/.feral/publisher.key`` Ed25519 keypair.
3. Registers the public key with the registry.
4. For every manifest in ``ASOS/feral-core/skills/manifests/*.json``
   (plus the matching ``ASOS/feral-core/skills/impl/<name>.py`` if
   present):
      * Builds a tarball with manifest.json at the root + impl.py.
      * Signs SHA-256 of the tarball with the private key.
      * POSTs to ``/api/v1/publish`` as a multipart form.
5. Prints the registry item id for each published bundle.

Run:

    python scripts/seed_remote.py --registry https://registry.feral.sh \\
        --publisher-token $FERAL_REGISTRY_ADMIN_TOKEN

The admin token is produced out-of-band by running
``flyctl ssh console -C "python -m feral_registry.scripts.mint_admin_token"``
on the live instance (see the matching script below). Once seeded,
every first-party skill is live in the catalog.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import sys
import tarfile
from pathlib import Path

import httpx
from nacl.signing import SigningKey
from nacl.encoding import HexEncoder

ASOS_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = ASOS_ROOT / "feral-core" / "skills"
PUBLISHER_KEY = Path.home() / ".feral" / "publisher.key"


def _load_or_create_key() -> SigningKey:
    """Return an Ed25519 signing key, generating + saving one if needed."""
    if PUBLISHER_KEY.exists():
        return SigningKey(PUBLISHER_KEY.read_bytes())
    PUBLISHER_KEY.parent.mkdir(parents=True, exist_ok=True)
    sk = SigningKey.generate()
    PUBLISHER_KEY.write_bytes(bytes(sk))
    PUBLISHER_KEY.chmod(0o600)
    print(f"  + generated new publisher key at {PUBLISHER_KEY}")
    return sk


def _build_skill_bundle(manifest_path: Path) -> tuple[dict, bytes]:
    """Return (registry_metadata, tarball_bytes) for a single skill manifest.

    Two documents, and conflating them is what broke every published
    skill: the returned dict is **registry metadata** posted as the
    ``manifest_json`` form field (kind/name/version, what the catalog
    shows), while the tarball's ``manifest.json`` is the **SkillManifest
    itself**, which is what FERAL loads at install. This used to write
    the metadata envelope into the tarball, so every published bundle
    failed ``SkillPackage.load()`` with "brand field required" and the
    happy path had never run.
    """
    manifest = json.loads(manifest_path.read_text())
    stem = manifest_path.stem
    name = manifest.get("skill_id") or manifest.get("name") or stem
    version = str(manifest.get("version", "0.1.0"))

    pub_manifest = {
        "kind": "skill",
        "name": name,
        "version": version,
        "description": manifest.get("description"),
        "author": manifest.get("author", "feral-core"),
        "skill_id": name,
        "original": manifest,
    }

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _add_bytes(tar, "manifest.json", json.dumps(manifest, indent=2).encode())
        impl = SKILLS_DIR / "impl" / f"{stem}.py"
        if impl.exists():
            _add_bytes(tar, "impl.py", impl.read_bytes())
    return pub_manifest, buf.getvalue()


def _add_bytes(tar: tarfile.TarFile, arcname: str, data: bytes) -> None:
    info = tarfile.TarInfo(name=arcname)
    info.size = len(data)
    info.mode = 0o644
    tar.addfile(info, io.BytesIO(data))


def _sign(sk: SigningKey, data: bytes) -> str:
    sha_hex = hashlib.sha256(data).hexdigest()
    sig = sk.sign(sha_hex.encode("ascii")).signature
    return base64.b64encode(sig).decode("ascii")


def _register_pubkey(registry: str, token: str, pubkey_hex: str) -> None:
    resp = httpx.post(
        f"{registry}/api/v1/auth/github/register_pubkey",
        headers={"Authorization": f"Bearer {token}"},
        json={"pubkey_hex": pubkey_hex},
        timeout=30.0,
    )
    if resp.status_code >= 400:
        print(f"  ! pubkey register failed ({resp.status_code}): {resp.text[:300]}")
        sys.exit(1)
    print(f"  + pubkey registered for {resp.json().get('github_login')}")


def _publish_one(
    registry: str,
    token: str,
    manifest: dict,
    tarball: bytes,
    signature: str,
) -> str | None:
    resp = httpx.post(
        f"{registry}/api/v1/publish",
        headers={"Authorization": f"Bearer {token}"},
        data={
            "signature": signature,
            "manifest_json": json.dumps(manifest),
        },
        files={"bundle": ("bundle.tar.gz", tarball, "application/gzip")},
        timeout=60.0,
    )
    if resp.status_code == 409:
        print(f"  = already published: {manifest['name']} v{manifest['version']}")
        return None
    if resp.status_code >= 400:
        print(f"  ! publish failed for {manifest['name']}: {resp.status_code} {resp.text[:300]}")
        return None
    body = resp.json()
    print(f"  + published: {manifest['name']} v{manifest['version']} → {body.get('id')} (verified={body.get('verified')})")
    return body.get("id")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", default=os.environ.get("FERAL_REGISTRY_URL", "https://registry.feral.sh"))
    ap.add_argument("--publisher-token", default=os.environ.get("FERAL_PUBLISHER_TOKEN"))
    args = ap.parse_args()

    if not args.publisher_token:
        print("  ! set --publisher-token or FERAL_PUBLISHER_TOKEN env. Mint one with:")
        print("    fly ssh console -C \"python -m feral_registry.scripts.mint_admin_token --login feral\"")
        sys.exit(2)

    registry = args.registry.rstrip("/")
    print(f"  seeding {registry}")

    sk = _load_or_create_key()
    pubkey_hex = sk.verify_key.encode(encoder=HexEncoder).decode("ascii")
    _register_pubkey(registry, args.publisher_token, pubkey_hex)

    manifests_dir = SKILLS_DIR / "manifests"
    manifest_paths = sorted(manifests_dir.glob("*.json"))
    if not manifest_paths:
        print(f"  ! no manifests found in {manifests_dir}")
        sys.exit(1)

    n_ok = n_skip = n_fail = 0
    for manifest_path in manifest_paths:
        try:
            pub_manifest, tarball = _build_skill_bundle(manifest_path)
        except Exception as exc:
            print(f"  ! bundle build failed for {manifest_path.name}: {exc}")
            n_fail += 1
            continue
        signature = _sign(sk, tarball)
        item_id = _publish_one(registry, args.publisher_token, pub_manifest, tarball, signature)
        if item_id:
            n_ok += 1
        else:
            n_skip += 1

    print(f"\nseeded {n_ok} new item(s), skipped {n_skip}, failed {n_fail}")


if __name__ == "__main__":
    main()
