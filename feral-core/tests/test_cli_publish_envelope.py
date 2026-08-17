"""``feral publish`` has to produce something the registry accepts.

It did not. Three independent defects, each of which alone refuses the
upload, and they are hit in this order:

1. ``manifest_json`` was the **domain manifest**, posted where registry
   metadata was expected. ``SkillManifest`` has ``skill_id``, ``brand``
   and ``description`` and carries no ``kind`` and no ``name``, while
   the registry's ``Manifest`` requires both, so a skill publish was
   refused with ``400 invalid manifest``. A daemon publish failed on
   ``kind`` too, and again on the per-kind required key ``node_id``.
2. The detached signature covered the **raw 32-byte SHA-256 digest**.
   ``feral_registry/signing.py`` verifies over the hex digest as ASCII,
   and so does ``cli/install.py::_verify`` on the way back in, so even a
   well-formed publish was refused with ``400 signature verification
   failed``. A comment in ``cli/app_commands.py`` asserted this path had
   already been fixed; it had not, and nobody checked.
3. ``version`` was read off the raw JSON rather than the validated
   model, so a manifest declaring no version published as ``0.1.0``
   while the installed ``SkillManifest`` reported ``1.0.0``.

The consequence is that no third-party developer could publish a skill
at all, which puts the signing, resolution and consent machinery out of
everyone's reach but this repo's.
"""

from __future__ import annotations

import json

import pytest

from cli.publish import registry_envelope

SKILL_MANIFEST = {
    "skill_id": "iot_light",
    "version": "2.3.0",
    "author": "acme",
    # Deliberately different from skill_id: this is the display name and
    # it must not become the registry key.
    "brand": {"name": "Smart Bulb", "primary_color": "#f1c40f"},
    "description": "Standardized control for the connected smart bulb.",
    "permissions": ["smart_home"],
    "endpoints": [],
}

DAEMON_MANIFEST = {
    "id": "feral-wristband",
    # Again a display name, and again not the key.
    "name": "Wristband Bridge",
    "version": "1.1.0",
    "capabilities": ["heart_rate", "haptic"],
    "entrypoint": "run.py",
    "description": "BLE wristband bridge.",
}


# ─────────────────────────────────────────────
# `name` is the stable identifier, not the display name
# ─────────────────────────────────────────────


def test_skill_name_is_skill_id_not_brand_name():
    envelope = registry_envelope("skill", SKILL_MANIFEST)

    assert envelope["kind"] == "skill"
    assert envelope["name"] == "iot_light"
    assert envelope["skill_id"] == "iot_light"
    # `feral install "Smart Bulb"` is not the interface.
    assert envelope["name"] != SKILL_MANIFEST["brand"]["name"]


def test_daemon_name_is_the_identifier_not_the_display_name():
    envelope = registry_envelope("daemon", DAEMON_MANIFEST)

    assert envelope["kind"] == "daemon"
    assert envelope["name"] == "feral-wristband"
    # The registry requires `node_id`; `dispatch_install` reads it to
    # choose ~/.feral/daemons/<id>/. Without it the installer falls
    # through to the registry's UUID.
    assert envelope["node_id"] == "feral-wristband"
    assert envelope["capabilities"] == ["heart_rate", "haptic"]
    assert envelope["name"] != DAEMON_MANIFEST["name"]


def test_daemon_accepts_node_id_directly():
    """The first-party daemon manifests spell it ``node_id``, not ``id``."""
    manifest = {k: v for k, v in DAEMON_MANIFEST.items() if k != "id"}
    manifest["node_id"] = "feral-w300"
    envelope = registry_envelope("daemon", manifest)
    assert envelope["name"] == "feral-w300"
    assert envelope["node_id"] == "feral-w300"


# ─────────────────────────────────────────────
# Shape, version, and the second document
# ─────────────────────────────────────────────


def test_envelope_carries_the_keys_the_registry_requires():
    envelope = registry_envelope("skill", SKILL_MANIFEST)
    for key in ("kind", "name", "version", "description", "author"):
        assert key in envelope, key
    assert envelope["version"] == "2.3.0"
    assert envelope["description"] == SKILL_MANIFEST["description"]
    assert envelope["author"] == "acme"


def test_the_domain_manifest_rides_along_under_original():
    """Two documents, both present, neither pretending to be the other."""
    envelope = registry_envelope("skill", SKILL_MANIFEST)
    assert envelope["original"] == SKILL_MANIFEST
    assert envelope["original"]["brand"]["name"] == "Smart Bulb"
    # The envelope itself is not the SkillManifest.
    assert "brand" not in envelope
    assert "endpoints" not in envelope


def test_version_comes_from_the_validated_model():
    """A versionless manifest publishes as the model's default.

    Read off the raw file it was ``0.1.0``; the model says ``1.0.0``,
    and the model is what loads the installed bundle.
    """
    from cli.publish import _validate_skill_manifest

    raw = {k: v for k, v in SKILL_MANIFEST.items() if k != "version"}
    validated = _validate_skill_manifest(raw)
    envelope = registry_envelope("skill", validated)

    from models.skill_manifest import SkillManifest

    assert envelope["version"] == SkillManifest.model_fields["version"].default


# ─────────────────────────────────────────────
# Refusals happen locally, before an upload
# ─────────────────────────────────────────────


def test_a_skill_without_skill_id_is_refused_by_name():
    raw = {k: v for k, v in SKILL_MANIFEST.items() if k != "skill_id"}
    with pytest.raises(ValueError) as exc:
        registry_envelope("skill", raw)
    assert "skill_id" in str(exc.value)
    assert "install it by" in str(exc.value)


def test_a_manifest_without_a_version_is_refused():
    raw = {k: v for k, v in SKILL_MANIFEST.items() if k != "version"}
    with pytest.raises(ValueError) as exc:
        registry_envelope("skill", raw)
    assert "version" in str(exc.value)


def test_an_unpublishable_kind_names_the_ones_that_work():
    with pytest.raises(ValueError) as exc:
        registry_envelope("workflow", {"workflow_id": "x", "version": "1.0.0"})
    assert "workflow" in str(exc.value)
    assert "skill" in str(exc.value) and "daemon" in str(exc.value)


# ─────────────────────────────────────────────
# Against the registry's own schema
# ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "kind,manifest",
    [("skill", SKILL_MANIFEST), ("daemon", DAEMON_MANIFEST)],
)
def test_envelope_satisfies_the_registrys_validator(kind, manifest):
    """The contract, checked against the code that enforces it."""
    pytest.importorskip("feral_registry", reason="feral-registry is not installed here")
    from feral_registry.schemas import Manifest, validate_manifest_for_kind

    envelope = registry_envelope(kind, manifest)
    parsed = Manifest.model_validate_json(json.dumps(envelope))
    assert validate_manifest_for_kind(parsed) == []


def test_the_identity_table_agrees_with_the_registrys_required_keys():
    """Pins the two packages' vocabularies to each other.

    ``_IDENTITY_FIELDS`` says which key the registry indexes each kind
    under. If ``_REQUIRED_PER_KIND`` renames one, this fails here rather
    than in a publisher's terminal.
    """
    pytest.importorskip("feral_registry", reason="feral-registry is not installed here")
    from feral_registry.schemas import _REQUIRED_PER_KIND

    from cli.publish import _IDENTITY_FIELDS

    for kind, (registry_key, _domain_fields) in _IDENTITY_FIELDS.items():
        assert kind in _REQUIRED_PER_KIND, kind
        assert registry_key in _REQUIRED_PER_KIND[kind], (
            f"kind={kind}: cli/publish.py writes {registry_key!r} but the registry "
            f"requires {_REQUIRED_PER_KIND[kind]}"
        )
