"""Seed the registry with first-party Feral skills and daemons.

This script is intended to be run **manually on the deployed instance** after
its first boot:

    fly ssh console -C "python -m scripts.seed_first_party"

It reads manifests from ``ASOS/feral-core/skills/manifests/*.json``, builds a
tarball containing the manifest plus the matching implementation module from
``ASOS/feral-core/skills/impl/``, signs each tarball with a deterministic
"first_party" seed key, and inserts them as verified items under the ``feral``
publisher. It also looks for ``ASOS/feral-nodes/w300_daemon/`` and
``ASOS/feral-nodes/wristband_daemon/`` directories and seeds them as daemon
entries with manifest inferred from ``manifest.json`` / ``daemon.json`` if
present.

The script is idempotent: `(kind, name, version)` tuples already in the
database are skipped.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path

from nacl.signing import SigningKey
from sqlalchemy import select

# Allow running as `python scripts/seed_first_party.py` from the package root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from feral_registry.config import get_settings  # noqa: E402
from feral_registry.db import Base, SessionLocal, engine  # noqa: E402
from feral_registry.models import Item, Publisher  # noqa: E402

FIRST_PARTY_LOGIN = "feral"
FIRST_PARTY_GH_ID = 1
SEED_KEY_ENV = "FERAL_REGISTRY_SEED_KEY_HEX"

# The version a skill manifest with no explicit `version` publishes as.
#
# This has to equal the default on `feral-core`'s
# `models/skill_manifest.py::SkillManifest.version`, because that model
# is what the brain loads the installed bundle with. While this said
# "0.1.0" and the model said "1.0.0", `robot_action.json` (which
# declares no version) published as 0.1.0 and then reported 1.0.0 once
# installed: the catalog and the running skill disagreed about the same
# bytes. Pinned by tests/test_seed_bundle_layout.py.
DEFAULT_SKILL_VERSION = "1.0.0"


@dataclass
class SeedItem:
    kind: str  # skill | daemon | mcp
    name: str
    version: str
    manifest: dict
    files: list[tuple[str, bytes]]  # (arcname, bytes)
    # The document written to the tarball as ``manifest.json``, when it
    # differs from the registry metadata in ``manifest``.
    #
    # These are two different things and conflating them is what broke
    # every published skill: ``manifest`` is registry metadata (kind /
    # name / version, what the catalog shows), while the tarball's
    # ``manifest.json`` is the document FERAL loads at install. For a
    # skill that is the SkillManifest, and shipping the envelope in its
    # place made every bundle fail ``SkillPackage.load()``.
    #
    # None keeps the historical behaviour (envelope in the tarball) for
    # the kinds whose loaders have not been re-verified against this
    # distinction yet: app, daemon, agent, workflow.
    bundle_manifest: dict | None = None


def _repo_root() -> Path:
    # Resolve ASOS/ relative to this script.
    return Path(__file__).resolve().parents[2]


def _load_skill_seeds() -> list[SeedItem]:
    root = _repo_root() / "feral-core" / "skills"
    manifests_dir = root / "manifests"
    impl_dir = root / "impl"
    seeds: list[SeedItem] = []
    if not manifests_dir.exists():
        return seeds
    for manifest_path in sorted(manifests_dir.glob("*.json")):
        try:
            manifest = json.loads(manifest_path.read_text())
        except json.JSONDecodeError:
            continue
        name = manifest.get("skill_id") or manifest.get("name") or manifest_path.stem
        version = str(manifest.get("version") or DEFAULT_SKILL_VERSION)
        # Root-level manifest.json + impl.py: the layout SkillPackage
        # reads. The old nested `manifests/x.json` + `impl/x.py` layout
        # put no manifest.json where the installer looks for one.
        files: list[tuple[str, bytes]] = []
        impl_py = impl_dir / f"{manifest_path.stem}.py"
        if impl_py.exists():
            files.append(("impl.py", impl_py.read_bytes()))
        seeds.append(
            SeedItem(
                kind="skill",
                name=name,
                version=version,
                manifest={
                    "kind": "skill",
                    "name": name,
                    "version": version,
                    "description": manifest.get("description"),
                    "author": manifest.get("author", "feral-core"),
                    "skill_id": name,
                    "original": manifest,
                },
                files=files,
                bundle_manifest=manifest,
            )
        )
    return seeds


def _load_agent_seeds() -> list[SeedItem]:
    """Pick up every JSON persona in ``feral-core/agents/personas/``.

    Each file is the manifest itself; the file body becomes ``manifest.json``
    in the bundle. Non-JSON files (README.md etc.) are skipped.
    """
    root = _repo_root() / "feral-core" / "agents" / "personas"
    seeds: list[SeedItem] = []
    if not root.exists():
        return seeds
    for path in sorted(root.glob("*.json")):
        try:
            manifest = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        name = manifest.get("agent_id") or manifest.get("name") or path.stem
        version = str(manifest.get("version", "1.0.0"))
        seeds.append(
            SeedItem(
                kind="agent",
                name=name,
                version=version,
                manifest={
                    "kind": "agent",
                    "name": name,
                    "version": version,
                    "description": manifest.get("description"),
                    "author": "feral-core",
                    "original": manifest,
                },
                files=[(f"personas/{path.name}", path.read_bytes())],
            )
        )
    return seeds


def _load_workflow_seeds() -> list[SeedItem]:
    """Pick up every JSON workflow pack in ``feral-core/workflows/``."""
    root = _repo_root() / "feral-core" / "workflows"
    seeds: list[SeedItem] = []
    if not root.exists():
        return seeds
    for path in sorted(root.glob("*.json")):
        try:
            manifest = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        name = manifest.get("workflow_id") or manifest.get("name") or path.stem
        version = str(manifest.get("version", "1.0.0"))
        seeds.append(
            SeedItem(
                kind="workflow",
                name=name,
                version=version,
                manifest={
                    "kind": "workflow",
                    "name": name,
                    "version": version,
                    "description": manifest.get("description"),
                    "author": "feral-core",
                    "original": manifest,
                },
                files=[(f"workflows/{path.name}", path.read_bytes())],
            )
        )
    return seeds


def _load_app_seeds() -> list[SeedItem]:
    """Pick up every example GenUI app under ``examples/apps/<app_id>/``.

    Each subdirectory must contain a ``manifest.yaml`` (or
    ``manifest.json``) parseable by AppManifest. We wrap the raw
    manifest in a registry envelope (kind=app + name=app_id +
    version) so the registry's per-kind validator is happy. Surface
    template files referenced by relative path inside the manifest
    are inlined the same way ``feral app build`` does, so the
    resulting bundle matches what a publisher would upload.
    """
    apps_root = _repo_root() / "examples" / "apps"
    seeds: list[SeedItem] = []
    if not apps_root.exists():
        return seeds
    for app_dir in sorted(apps_root.iterdir()):
        if not app_dir.is_dir():
            continue
        manifest_path = None
        for cand in ("manifest.yaml", "manifest.yml", "manifest.json"):
            if (app_dir / cand).exists():
                manifest_path = app_dir / cand
                break
        if manifest_path is None:
            continue
        try:
            if manifest_path.suffix in (".yaml", ".yml"):
                import yaml  # type: ignore
                raw = yaml.safe_load(manifest_path.read_text()) or {}
            else:
                raw = json.loads(manifest_path.read_text())
        except Exception:
            continue
        # Inline template_root file refs (mirror app_registry behaviour).
        surfaces = raw.get("surfaces")
        if isinstance(surfaces, list):
            for surface in surfaces:
                if isinstance(surface, dict):
                    template_root = surface.get("template_root")
                    if isinstance(template_root, str):
                        candidate = (app_dir / template_root).resolve()
                        if candidate.is_file():
                            try:
                                surface["template_root"] = json.loads(candidate.read_text())
                            except Exception:
                                pass
        app_id = raw.get("app_id") or app_dir.name
        version = str(raw.get("version", "1.0.0"))
        # Bundle every file under the app dir (skipping dist/).
        files: list[tuple[str, bytes]] = []
        for path in sorted(app_dir.rglob("*")):
            rel = path.relative_to(app_dir)
            if str(rel).startswith("dist"):
                continue
            if path.is_file() and path.stat().st_size < 1_000_000:
                files.append((str(rel), path.read_bytes()))
        manifest_envelope: dict = {
            "kind": "app",
            "name": app_id,
            "version": version,
            "description": raw.get("description"),
            "author": raw.get("author", "feral-team"),
            "app_id": app_id,
            "brand": raw.get("brand", {}),
            "entry_surface_id": raw.get("entry_surface_id"),
            "surfaces": raw.get("surfaces", []),
        }
        seeds.append(
            SeedItem(
                kind="app",
                name=app_id,
                version=version,
                manifest=manifest_envelope,
                files=files,
            )
        )
    return seeds


def _load_daemon_seeds() -> list[SeedItem]:
    nodes_root = _repo_root() / "feral-nodes"
    seeds: list[SeedItem] = []
    for dirname in ("w300_daemon", "wristband_daemon"):
        daemon_dir = nodes_root / dirname
        if not daemon_dir.exists():
            continue
        manifest_file = None
        for cand in ("manifest.json", "daemon.json", "package.json"):
            if (daemon_dir / cand).exists():
                manifest_file = daemon_dir / cand
                break
        raw_manifest: dict = {}
        if manifest_file:
            try:
                raw_manifest = json.loads(manifest_file.read_text())
            except json.JSONDecodeError:
                raw_manifest = {}
        # `node_id` is the daemon's stable identifier: it is the key the
        # registry requires for kind=daemon, and it is what
        # `cli.install.dispatch_install` reads to pick
        # `~/.feral/daemons/<id>/`. `manifest["name"]` is the package's
        # display name (`wristband_daemon`), which is a different thing.
        # Seeding under the display name and omitting node_id meant
        # dispatch_install fell through to the registry's UUID and
        # installed the daemon into a directory named after a database
        # row. See cli/publish.py::registry_envelope for the same rule on
        # the publish side.
        name = raw_manifest.get("node_id") or raw_manifest.get("id") or dirname
        version = str(raw_manifest.get("version", "0.1.0"))
        files: list[tuple[str, bytes]] = []
        for p in sorted(daemon_dir.rglob("*")):
            if p.is_file() and p.stat().st_size < 1_000_000:
                rel = p.relative_to(daemon_dir)
                files.append((str(rel), p.read_bytes()))
        seeds.append(
            SeedItem(
                kind="daemon",
                name=name,
                version=version,
                manifest={
                    "kind": "daemon",
                    "name": name,
                    "version": version,
                    "node_id": name,
                    "capabilities": list(raw_manifest.get("capabilities") or []),
                    "description": raw_manifest.get(
                        "description", f"First-party {dirname} daemon."
                    ),
                    "author": "feral-core",
                    "original": raw_manifest,
                },
                files=files,
            )
        )
    return seeds


def _build_tarball(seed: SeedItem) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        in_bundle = seed.bundle_manifest if seed.bundle_manifest is not None else seed.manifest
        manifest_bytes = json.dumps(in_bundle, sort_keys=True).encode("utf-8")
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest_bytes)
        tar.addfile(info, io.BytesIO(manifest_bytes))
        for arcname, data in seed.files:
            info = tarfile.TarInfo(arcname)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _get_seed_signing_key() -> SigningKey:
    hex_key = os.environ.get(SEED_KEY_ENV)
    if hex_key:
        return SigningKey(bytes.fromhex(hex_key))
    # Deterministic dev seed. Override in production via FERAL_REGISTRY_SEED_KEY_HEX.
    return SigningKey(b"feral-first-party-seed-key-32byt")


async def _ensure_first_party_publisher(session, pubkey_hex: str) -> Publisher:
    row = await session.execute(
        select(Publisher).where(Publisher.github_login == FIRST_PARTY_LOGIN)
    )
    pub = row.scalar_one_or_none()
    if pub is None:
        pub = Publisher(
            github_login=FIRST_PARTY_LOGIN,
            github_id=FIRST_PARTY_GH_ID,
            pubkey_hex=pubkey_hex,
        )
        session.add(pub)
        await session.flush()
    else:
        pub.pubkey_hex = pubkey_hex
    return pub


async def seed() -> int:
    settings = get_settings()
    settings.blob_dir.mkdir(parents=True, exist_ok=True)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    sk = _get_seed_signing_key()
    pubkey_hex = sk.verify_key.encode().hex()

    seeds = (
        _load_skill_seeds()
        + _load_app_seeds()
        + _load_daemon_seeds()
        + _load_agent_seeds()
        + _load_workflow_seeds()
    )
    inserted = 0

    async with SessionLocal() as session:
        pub = await _ensure_first_party_publisher(session, pubkey_hex)

        for seed_item in seeds:
            existing = await session.execute(
                select(Item.id).where(
                    Item.kind == seed_item.kind,
                    Item.name == seed_item.name,
                    Item.version == seed_item.version,
                )
            )
            if existing.scalar_one_or_none() is not None:
                continue

            bundle = _build_tarball(seed_item)
            sha = hashlib.sha256(bundle).hexdigest()
            sig = base64.b64encode(sk.sign(sha.encode("ascii")).signature).decode("ascii")
            blob_path = settings.blob_dir / f"{sha}.tar.gz"
            blob_path.write_bytes(bundle)

            session.add(
                Item(
                    kind=seed_item.kind,
                    name=seed_item.name,
                    version=seed_item.version,
                    author_id=pub.id,
                    manifest_json=json.dumps(seed_item.manifest, sort_keys=True),
                    sha256=sha,
                    blob_path=str(blob_path),
                    size_bytes=len(bundle),
                    signature_b64=sig,
                    verified=True,
                )
            )
            inserted += 1

        await session.commit()

    print(f"Seeded {inserted} first-party item(s). Publisher pubkey: {pubkey_hex}")
    return inserted


def main() -> None:
    asyncio.run(seed())


if __name__ == "__main__":
    main()
