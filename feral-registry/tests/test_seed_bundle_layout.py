"""What the seed scripts put inside a skill tarball.

Both seeders used to write the registry metadata envelope into the
bundle as ``manifest.json``. That is not the document FERAL loads:
``SkillPackage`` expects a ``SkillManifest``, and the envelope has no
``brand``, so every seeded skill failed at install and nobody could tell
because nothing exercised the install side of a seeded bundle.

These tests run the seeders' own bundle builders over the real
first-party manifests and put the result through the same validator
``/publish`` uses.
"""

from __future__ import annotations

import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

from feral_registry.bundle import validate_bundle_for_kind

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFESTS_DIR = REPO_ROOT / "feral-core" / "skills" / "manifests"
SCRIPTS_DIR = REPO_ROOT / "feral-registry" / "scripts"

pytestmark = pytest.mark.skipif(
    not MANIFESTS_DIR.is_dir(),
    reason="feral-core skill manifests are not present in this checkout",
)


def _import_seeder(module_name: str):
    if str(SCRIPTS_DIR.parent) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR.parent))
    return __import__(f"scripts.{module_name}", fromlist=["*"])


def _tar_members(data: bytes) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar:
            if member.isfile():
                handle = tar.extractfile(member)
                if handle is not None:
                    out[member.name] = handle.read()
    return out


def _sample_manifest_path() -> Path:
    path = MANIFESTS_DIR / "robot_ext.json"
    if path.exists():
        return path
    return sorted(MANIFESTS_DIR.glob("*.json"))[0]


def test_seed_remote_ships_the_skill_manifest_not_the_envelope():
    seed_remote = _import_seeder("seed_remote")
    envelope, tarball = seed_remote._build_skill_bundle(_sample_manifest_path())

    members = _tar_members(tarball)
    assert "manifest.json" in members
    in_bundle = json.loads(members["manifest.json"])

    assert "brand" in in_bundle, "the bundle must carry the SkillManifest"
    assert "original" not in in_bundle, "the envelope must not be in the bundle"
    # The envelope is still what gets posted as metadata; the two stay
    # distinct rather than one replacing the other.
    assert envelope["kind"] == "skill"
    assert envelope["original"]["brand"] == in_bundle["brand"]

    assert validate_bundle_for_kind("skill", tarball) == []


def test_seed_first_party_ships_the_skill_manifest_at_the_root():
    seed_first_party = _import_seeder("seed_first_party")
    seeds = [s for s in seed_first_party._load_skill_seeds() if s.kind == "skill"]
    assert seeds, "no first-party skill seeds were discovered"

    for seed in seeds:
        tarball = seed_first_party._build_tarball(seed)
        members = _tar_members(tarball)
        assert "manifest.json" in members, seed.name
        in_bundle = json.loads(members["manifest.json"])
        assert "brand" in in_bundle, seed.name
        assert validate_bundle_for_kind("skill", tarball) == [], seed.name


def test_seeder_version_default_matches_the_model_that_loads_the_bundle():
    """One version for one set of bytes.

    While the seeders defaulted to ``0.1.0`` and ``SkillManifest.version``
    defaulted to ``1.0.0``, ``robot_action.json`` (which declares no
    version) published as 0.1.0 and reported 1.0.0 once installed. The
    catalog and the running skill disagreed about the same tarball.
    """
    if str(REPO_ROOT / "feral-core") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "feral-core"))
    try:
        from models.skill_manifest import SkillManifest
    except Exception as exc:  # pragma: no cover - feral-core not installed here
        pytest.skip(f"feral-core is not importable: {exc}")

    model_default = SkillManifest.model_fields["version"].default
    seed_first_party = _import_seeder("seed_first_party")
    seed_remote = _import_seeder("seed_remote")

    assert seed_first_party.DEFAULT_SKILL_VERSION == model_default
    assert seed_remote.DEFAULT_SKILL_VERSION == model_default


def test_a_versionless_manifest_seeds_at_the_model_default():
    seed_first_party = _import_seeder("seed_first_party")
    versionless = [
        path.stem
        for path in sorted(MANIFESTS_DIR.glob("*.json"))
        if "version" not in json.loads(path.read_text())
    ]
    if not versionless:
        pytest.skip("every first-party manifest declares a version")

    seeds = {s.name: s for s in seed_first_party._load_skill_seeds()}
    for stem in versionless:
        skill_id = json.loads((MANIFESTS_DIR / f"{stem}.json").read_text()).get("skill_id", stem)
        assert seeds[skill_id].version == seed_first_party.DEFAULT_SKILL_VERSION


def test_seed_first_party_puts_impl_beside_the_manifest():
    """``SkillPackage`` reads ``impl.py`` next to ``manifest.json``.

    The old layout wrote ``impl/<name>.py``, one directory away from
    where the loader looks.
    """
    seed_first_party = _import_seeder("seed_first_party")
    seeds = {s.name: s for s in seed_first_party._load_skill_seeds()}
    with_impl = [
        name
        for name, seed in seeds.items()
        if any(arc == "impl.py" for arc, _ in seed.files)
    ]
    assert with_impl, "no seeded skill shipped an impl.py"
    for name in with_impl:
        members = _tar_members(seed_first_party._build_tarball(seeds[name]))
        assert "impl.py" in members
        assert not any(m.startswith("impl/") for m in members), name
