"""Contract tests for first-party daemon seeds.

Track B daemons (``wristband_daemon`` + ``w300_daemon``) live under
``feral-nodes/`` and are picked up by
``feral-registry/scripts/seed_first_party.py::_load_daemon_seeds``.

Asserts:
1. Both directories exist with a parsable ``manifest.json``.
2. The registry loader returns both as ``kind=daemon`` seeds.
3. Each manifest declares the HUP v1.3.0 hookup (``hup_version == "1.3.0"``)
   and the ``live_test_env`` gate name so the docs never drift from
   what the tests expect.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
NODES_ROOT = REPO_ROOT / "feral-nodes"

EXPECTED_DAEMONS = ["wristband_daemon", "w300_daemon"]


def _daemon_dir(name: str) -> Path:
    return NODES_ROOT / name


@pytest.mark.parametrize("name", EXPECTED_DAEMONS)
def test_daemon_manifest_exists_and_is_parsable(name: str):
    d = _daemon_dir(name)
    assert d.is_dir(), f"Missing daemon directory: {d}"
    manifest_path = d / "manifest.json"
    assert manifest_path.is_file(), f"Missing manifest.json for {name}"
    data = json.loads(manifest_path.read_text())
    assert data.get("name") == name, f"manifest.name != {name!r}"
    assert data.get("hup_version") == "1.3.0", (
        f"{name}: hup_version must be pinned to 1.3.0 in the manifest"
    )
    assert data.get("live_test_env"), f"{name}: missing live_test_env"


def _daemon_seeds():
    sys.path.insert(0, str(REPO_ROOT / "feral-registry"))
    from scripts import seed_first_party as seeder  # type: ignore

    return seeder._load_daemon_seeds()


def test_daemon_seed_loader_returns_both():
    """Both directories are picked up, keyed on ``node_id``.

    This used to assert the seed name was the *directory* name
    (``wristband_daemon``). That is the package's display name.
    ``node_id`` (``feral-wristband``) is the daemon's stable identifier:
    it is the key the registry requires for ``kind=daemon`` and the one
    ``cli.install.dispatch_install`` reads to choose
    ``~/.feral/daemons/<id>/``.
    """
    daemon_seeds = _daemon_seeds()
    assert all(s.kind == "daemon" for s in daemon_seeds)

    by_node_id = {s.name: s for s in daemon_seeds}
    expected_node_ids = set()
    for dirname in EXPECTED_DAEMONS:
        manifest = json.loads((_daemon_dir(dirname) / "manifest.json").read_text())
        node_id = manifest["node_id"]
        expected_node_ids.add(node_id)
        assert node_id in by_node_id, (
            f"_load_daemon_seeds() did not pick up {dirname!r} under its node_id "
            f"{node_id!r}; got {sorted(by_node_id)}"
        )
    assert expected_node_ids <= set(by_node_id)


def test_daemon_seed_envelope_carries_node_id_and_capabilities():
    """Without these the installer cannot name the daemon it installed.

    ``dispatch_install`` falls back to the registry's UUID when
    ``node_id`` is absent, so the daemon landed in
    ``~/.feral/daemons/<uuid>/`` -- a directory named after a database
    row. ``capabilities`` is required by the registry's per-kind
    validator and has no fallback at all.
    """
    for seed in _daemon_seeds():
        envelope = seed.manifest
        assert envelope["node_id"] == seed.name, seed.name
        assert envelope["kind"] == "daemon"
        assert envelope["capabilities"], f"{seed.name} declared no capabilities"
