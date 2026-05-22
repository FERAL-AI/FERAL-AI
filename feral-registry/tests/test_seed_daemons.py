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


def test_daemon_seed_loader_returns_both():
    sys.path.insert(0, str(REPO_ROOT / "feral-registry"))
    from scripts import seed_first_party as seeder  # type: ignore

    daemon_seeds = seeder._load_daemon_seeds()
    names = {s.name for s in daemon_seeds}
    for expected in EXPECTED_DAEMONS:
        assert expected in names, (
            f"_load_daemon_seeds() did not pick up {expected!r}; got {sorted(names)}"
        )
    assert all(s.kind == "daemon" for s in daemon_seeds)
