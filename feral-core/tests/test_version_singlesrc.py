""" regression: pin the single-source-of-truth version contract.

These tests fail loudly the moment any of the  invariants slips:

  * mDNS no longer carries a literal version string — it must import
    ``VERSION`` from ``feral_core.version`` (currently re-exported as
    ``version`` because ``feral-core/pyproject.toml`` declares
    ``py-modules = ["version"]``).
  * The mDNS announce payload's ``version`` property MUST equal the
    runtime ``VERSION`` value, regardless of what the python file
    declares textually.
  * ``scripts/sync_versions.py`` declares the canonical pyproject
    location, has a working ``--check`` mode, and reports drift = 0
    against the current checkout.
  * The README badge marker block is present so the release sync
    flow can find and update it.
  * The README test-count marker is present so the version-coherence
    workflow can compare to live counts.
  * The CHANGELOG carries the ``<!-- feral-version: X -->`` marker so
    sync_versions.py can keep it in lockstep with pyproject.

"""
from __future__ import annotations

import importlib.util
import re
import socket
import sys
from pathlib import Path

import pytest

ASOS_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# mDNS — no literal, runtime-equal-to-VERSION
# ---------------------------------------------------------------------------
def test_mdns_does_not_carry_a_literal_version_string():
    """The  refactor removed the hardcoded ``"2026.4.32"`` from
    ``services/mdns.py``. Reintroducing one undoes the single-source
    fix. We allow the calver to appear in a comment, but never as a
    real Python string literal."""
    mdns = ASOS_ROOT / "feral-core" / "services" / "mdns.py"
    text = mdns.read_text(encoding="utf-8")
    # Strip any ``# ...`` trailing comments line by line, then search.
    code_only_lines: list[str] = []
    for raw in text.splitlines():
        # naive but adequate: strip after the first '#' that is not
        # inside a string literal. mDNS is small and doesn't use '#' in
        # its strings.
        if "#" in raw:
            raw = raw.split("#", 1)[0]
        code_only_lines.append(raw)
    code = "\n".join(code_only_lines)
    matches = re.findall(r"\d{4}\.\d{1,2}\.\d{1,2}", code)
    assert not matches, (
        "feral-core/services/mdns.py contains a literal version string "
        f"({matches!r}). W7 mandates importing VERSION from "
        "feral_core.version instead."
    )


def test_mdns_announce_payload_uses_runtime_VERSION(monkeypatch):
    """The mDNS ``advertise_brain`` call must put the runtime VERSION
    into the announce payload's ``properties['version']`` field. We
    inject a fake ``zeroconf`` module to capture the ``ServiceInfo``
    invocation without touching the network."""
    captured: dict = {}

    class _FakeZeroconf:
        def __init__(self, *a, **kw):
            pass

        def register_service(self, info):
            captured["info"] = info

        def close(self):
            pass

        def unregister_service(self, info):
            pass

    class _FakeServiceInfo:
        def __init__(self, *args, properties=None, **kwargs):
            self.args = args
            self.properties = properties or {}
            self.kwargs = kwargs

    fake_zc_module = type(sys)("zeroconf")
    fake_zc_module.Zeroconf = _FakeZeroconf
    fake_zc_module.ServiceInfo = _FakeServiceInfo
    monkeypatch.setitem(sys.modules, "zeroconf", fake_zc_module)
    monkeypatch.setattr(socket, "gethostbyname", lambda _: "127.0.0.1")
    monkeypatch.setattr(socket, "gethostname", lambda: "fakehost")
    monkeypatch.setattr(socket, "inet_aton", lambda _: b"\x7f\x00\x00\x01")

    # Reload the module so the patched zeroconf is used.
    import services.mdns as mdns_module  # type: ignore

    try:
        from version import VERSION as runtime_version  # type: ignore
    except ImportError:
        pytest.skip("feral-core 'version' module not importable in this env")

    # Reset module-level state defensively.
    mdns_module._registration = None
    ok = mdns_module.advertise_brain(port=9090, name="W7 test")
    assert ok is True

    info = captured["info"]
    assert info.properties.get("version") == runtime_version, (
        f"mDNS announce reported version={info.properties.get('version')!r} "
        f"but runtime VERSION is {runtime_version!r} — single-source "
        "contract broken."
    )


# ---------------------------------------------------------------------------
# scripts/sync_versions.py contract
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def sync_versions_module():
    path = ASOS_ROOT / "scripts" / "sync_versions.py"
    if not path.exists():
        pytest.skip(f"{path} not in this checkout")
    spec = importlib.util.spec_from_file_location(
        "feral_sync_versions", path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_sync_versions_declares_pyproject_as_first_location(
    sync_versions_module,
):
    locs = sync_versions_module.VERSION_LOCATIONS
    assert locs, "VERSION_LOCATIONS is empty"
    first = locs[0]
    assert first.path == "feral-core/pyproject.toml", (
        "feral-core/pyproject.toml must be the first declared location "
        f"(got {first.path!r}) so it visibly anchors the source-of-truth."
    )
    assert first.owned_by_w7 is True


def test_sync_versions_check_mode_is_clean(sync_versions_module):
    """The current checkout MUST have drift = 0 for the version-coherence
    workflow to pass on this PR. If this fails, run
    ``python3 scripts/sync_versions.py --write --no-metadata`` and
    re-commit."""
    report = sync_versions_module.sync(write=False, prefer_metadata=False)
    if report.has_drift:
        per_loc = "\n".join(
            f"  - {loc.path}: observed {sorted(versions)} "
            f"≠ source {report.source_version}"
            for loc, versions in report.drifted
        )
        raise AssertionError(
            "version drift detected. Source = "
            f"{report.source_version}.\n{per_loc}\n"
            "Run `python3 scripts/sync_versions.py --write --no-metadata` "
            "to resync."
        )


def test_sync_versions_resolves_source_from_pyproject(sync_versions_module):
    """``--no-metadata`` must resolve the source from the pyproject
    literal, not from importlib. This is what the CI gate uses."""
    version, label = sync_versions_module.resolve_source_version(
        prefer_metadata=False
    )
    assert label.endswith("pyproject.toml")
    assert sync_versions_module.CALVER_RE.match(version), (
        f"resolved version {version!r} from {label} is not calver"
    )


# ---------------------------------------------------------------------------
# README + CHANGELOG marker contract
# ---------------------------------------------------------------------------
def test_readme_has_sync_versions_badge_marker():
    readme = ASOS_ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    assert "<!-- sync-versions:badge -->" in text and \
        "<!-- /sync-versions:badge -->" in text, (
        "README.md is missing the `<!-- sync-versions:badge -->` ... "
        "`<!-- /sync-versions:badge -->` block around the version "
        "shields.io badge. The release flow needs that marker to find "
        "and patch the version on each release."
    )


def test_readme_has_test_count_marker():
    readme = ASOS_ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    m = re.search(
        r"<!--\s*sync-versions:test-counts\s+"
        r"pytest=(\d+)\s+vitest=(\d+)\s*-->",
        text,
    )
    assert m is not None, (
        "README.md is missing the `<!-- sync-versions:test-counts "
        "pytest=N vitest=M -->` marker. The version-coherence CI gate "
        "uses it as the source of truth for the published test counts."
    )
    pytest_count, vitest_count = int(m.group(1)), int(m.group(2))
    assert pytest_count > 0 and vitest_count > 0, (
        "test-count marker has zeroes — that always means the marker "
        "was never updated after a refactor."
    )


def test_changelog_has_feral_version_marker():
    changelog = ASOS_ROOT / "CHANGELOG.md"
    text = changelog.read_text(encoding="utf-8")
    assert re.search(
        r"<!--\s*feral-version:\s*\d{4}\.\d{1,2}\.\d{1,2}\s*-->", text
    ), (
        "CHANGELOG.md is missing the `<!-- feral-version: X -->` "
        "marker (expected near the top). sync_versions.py uses this "
        "marker to keep CHANGELOG's current-version note in lockstep "
        "with feral-core/pyproject.toml."
    )
