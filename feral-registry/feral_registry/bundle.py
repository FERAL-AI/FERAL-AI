"""Publish-time inspection of the bundle itself, not just its metadata.

A publish carries two separate documents that are easy to confuse:

* the ``manifest_json`` **form field** — registry metadata (``kind``,
  ``name``, ``version``, description, author). This is what the catalog
  and ``GET /item/{ref}`` serve, and it is *not* covered by the bundle
  signature.
* the bundle's own ``manifest.json`` — the document FERAL loads at
  install time. For a skill that is a ``SkillManifest``: it is what the
  install dialog reads permissions out of, and what the SkillRegistry
  registers.

Until this module existed the registry validated the first and never
looked at the second, so a bundle whose ``manifest.json`` FERAL cannot
load published successfully and then failed at install with "invalid
package". A validator that accepts at publish and rejects at install is
its own defect: the publisher gets a green light and the user gets the
error.

Scope and the drift question
----------------------------
This checks **structure**, not vocabulary. It requires the keys
``SkillManifest`` declares with no default (``brand.name``,
``description``) plus a stable ``skill_id``, because those are what make
a bundle loadable and nameable. It deliberately does **not** re-implement
``SkillPermission``: that enum grows in feral-core, a copy here would go
stale, and a stale copy would reject a valid new skill at upload, which
is the same defect pointing the other way. The registry does not import
feral-core (separate deployable, separate dependency set), so the line is
drawn at what cannot drift.

Being stricter than install is fine (``skill_id`` is required here even
though ``SkillManifest`` defaults it to a UUID, because a skill with a
random id can never be the target of a ``skill_dependencies`` entry).
Being looser is the bug.
"""

from __future__ import annotations

import io
import json
import tarfile

# A manifest is a small JSON document. The cap bounds what a
# decompression bomb can make us hold: we read one member and stop.
MAX_MANIFEST_BYTES = 512 * 1024

# Bounds the member scan on a hostile archive.
MAX_MEMBERS_SCANNED = 20_000


def _member_sort_key(name: str) -> tuple[str, ...]:
    """Order members the way ``Path.rglob`` + ``sorted`` orders them.

    ``MarketplaceClient.preview_from_registry`` picks
    ``sorted(extract_dir.rglob("manifest.json"))[0]``, which compares
    path-part tuples, so a root ``manifest.json`` beats a nested one.
    This validator has to inspect the same file install will, or it
    validates something nobody runs.
    """
    return tuple(part for part in name.split("/") if part not in ("", "."))


def read_bundle_manifest(data: bytes) -> tuple[dict | None, str]:
    """Return ``(manifest, error)`` for the bundle's own ``manifest.json``.

    ``manifest`` is None whenever ``error`` is non-empty.
    """
    try:
        tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except tarfile.TarError as exc:
        return None, f"bundle is not a readable .tar.gz archive: {exc}"

    with tar:
        candidates: list[tarfile.TarInfo] = []
        for index, member in enumerate(tar):
            if index >= MAX_MEMBERS_SCANNED:
                return None, "bundle contains too many entries to inspect"
            if not member.isfile():
                continue
            if member.name.rsplit("/", 1)[-1] == "manifest.json":
                candidates.append(member)
        if not candidates:
            return None, "bundle contains no manifest.json"

        chosen = min(candidates, key=lambda m: _member_sort_key(m.name))
        if chosen.size > MAX_MANIFEST_BYTES:
            return None, (
                f"{chosen.name} is {chosen.size} bytes; the limit is "
                f"{MAX_MANIFEST_BYTES}"
            )
        try:
            handle = tar.extractfile(chosen)
            if handle is None:
                return None, f"{chosen.name} could not be read from the bundle"
            raw = handle.read(MAX_MANIFEST_BYTES + 1)
        except tarfile.TarError as exc:
            return None, f"{chosen.name} could not be read from the bundle: {exc}"

    if len(raw) > MAX_MANIFEST_BYTES:
        return None, f"manifest.json exceeds {MAX_MANIFEST_BYTES} bytes"
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"manifest.json in the bundle is not valid JSON: {exc}"
    if not isinstance(parsed, dict):
        return None, "manifest.json in the bundle is not a JSON object"
    return parsed, ""


def _skill_problems(manifest: dict) -> list[str]:
    problems: list[str] = []

    skill_id = manifest.get("skill_id")
    if not isinstance(skill_id, str) or not skill_id.strip():
        problems.append(
            "manifest.json is missing a non-empty 'skill_id'; without one the "
            "skill installs under a generated directory name and no app can "
            "name it in skill_dependencies"
        )

    brand = manifest.get("brand")
    if not isinstance(brand, dict):
        problems.append(
            "manifest.json is missing 'brand'; FERAL's SkillManifest requires it "
            "and the install dialog shows brand.name as the skill's name"
        )
    elif not isinstance(brand.get("name"), str) or not brand["name"].strip():
        problems.append("manifest.json 'brand' is missing a non-empty 'name'")

    description = manifest.get("description")
    if not isinstance(description, str) or not description.strip():
        problems.append(
            "manifest.json is missing a non-empty 'description'; FERAL's "
            "SkillManifest requires it and the model reads it to decide when to "
            "use the skill"
        )

    if "original" in manifest and "brand" not in manifest:
        problems.append(
            "manifest.json looks like a registry metadata envelope (it has an "
            "'original' key) rather than the skill manifest itself; the bundle "
            "must ship the SkillManifest at manifest.json"
        )

    return problems


def validate_bundle_for_kind(kind: str, data: bytes) -> list[str]:
    """Problems that would make this bundle fail at install, or ``[]``.

    Only ``skill`` bundles are inspected today. The other kinds extract
    to their own directories and are read by their own loaders; adding
    each one is a separate, deliberate act with its own contract, and
    silently half-checking them would be worse than not checking.
    """
    if kind != "skill":
        return []

    manifest, error = read_bundle_manifest(data)
    if error:
        return [error]
    assert manifest is not None
    return _skill_problems(manifest)
