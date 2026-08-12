"""F-15 — one Swift source per name under feral-nodes.

``FeralBrainClient.swift`` existed twice and the copies had diverged:

    feral-nodes/ios-bridge/FeralBrainClient.swift                604 lines
    feral-nodes/ios-app/Sources/FeralBridge/FeralBrainClient.swift  774 lines

``FeralSensorBridge.swift`` existed twice and was byte-identical, which is the
same problem one commit earlier.

**The divergence is entirely one-way.** `diff -u` reports 161 added lines and a
single removed line, and that line is a `// MARK:` comment that was reworded.
So `ios-bridge/` is a strict subset, missing three things that matter:

* ``UnifiedPairPayload`` / ``parsePairingPayload``. Brains at or above
  2026.5.8 emit the unified v1 QR payload; the stale copy only decodes the
  legacy ``{host, port, apiKey, nodeName}`` shape, so anything built from it
  cannot pair with a current brain.
* TLS certificate pinning via ``FERAL_BRAIN_CERT_HASH``. The stale copy has no
  ``didReceive challenge`` handler at all.
* The ``sendAudioChunk(_ data: Data)`` overload.

**Nothing built `ios-bridge/`.** ``feral-nodes/ios-app/Package.swift`` declares
one target with ``path: "Sources/FeralBridge"``, and no Xcode project, manifest
or script referenced the other directory. Its only referents were three
documentation files, which pointed developers and the Android bridge's own
README at the stale copy as the reference implementation. `git log` shows
`ios-app/Sources/FeralBridge` gained "phase 5: mobile consolidation — one iOS
app, one Android app"; `ios-bridge/` did not, and was left behind by it.

Checked and *not* affected: the separate iOS app at
``~/Desktop/Theora-backend-ML`` vendors neither file. It carries its own
``ios/Theora/Feral/`` implementation, and that one already decodes the unified
v1 payload.
"""

from __future__ import annotations

import subprocess
from collections import defaultdict
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
NODES = REPO_ROOT / "feral-nodes"

# One SwiftPM manifest per package is correct by design, not a duplicate.
ALLOWED_DUPLICATE_NAMES = {"Package.swift"}


def _tracked_swift_files() -> list[Path]:
    """Swift files git knows about, so build outputs cannot register as copies."""
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "feral-nodes/**/*.swift"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [REPO_ROOT / line for line in result.stdout.split("\n") if line.strip()]


def test_git_listing_is_not_empty():
    """A guard whose input is empty would pass forever."""
    files = _tracked_swift_files()
    assert len(files) >= 20, f"only found {len(files)} Swift files; the glob is wrong"


def test_no_swift_source_name_appears_twice():
    """The class guard.

    Two files with one name is drift waiting to happen whether or not they
    currently differ: FeralSensorBridge.swift was byte-identical and
    FeralBrainClient.swift, its neighbour, was already 170 lines apart.
    """
    by_name: dict[str, list[Path]] = defaultdict(list)
    for path in _tracked_swift_files():
        if path.name in ALLOWED_DUPLICATE_NAMES:
            continue
        by_name[path.name].append(path)

    duplicates = {
        name: paths for name, paths in by_name.items() if len(paths) > 1
    }
    assert not duplicates, "\n".join(
        f"{name} exists {len(paths)} times: "
        + ", ".join(str(p.relative_to(REPO_ROOT)) for p in paths)
        for name, paths in sorted(duplicates.items())
    )


def test_the_surviving_copy_is_the_one_that_gets_built():
    """Package.swift's target path must contain the client we kept."""
    manifest = (NODES / "ios-app" / "Package.swift").read_text(encoding="utf-8")
    assert 'path: "Sources/FeralBridge"' in manifest, (
        "the FeralBridge target no longer points at Sources/FeralBridge; "
        "re-derive which copy is authoritative before trusting this file"
    )
    built = NODES / "ios-app" / "Sources" / "FeralBridge"
    assert (built / "FeralBrainClient.swift").exists()
    assert (built / "FeralSensorBridge.swift").exists()


def test_the_surviving_copy_keeps_what_the_stale_one_lacked():
    """Pins the three capabilities that made the choice of copy matter."""
    source = (
        NODES / "ios-app" / "Sources" / "FeralBridge" / "FeralBrainClient.swift"
    ).read_text(encoding="utf-8")
    assert "UnifiedPairPayload" in source, "lost unified v1 QR pairing"
    assert "parsePairingPayload" in source, "lost the unified pairing entry point"
    assert "FERAL_BRAIN_CERT_HASH" in source, "lost TLS certificate pinning"
    assert "func sendAudioChunk(_ data: Data)" in source, (
        "lost the Data overload of sendAudioChunk"
    )


@pytest.mark.parametrize(
    "doc",
    [
        "docs/HARDWARE_ECOSYSTEM.md",
        "feral-nodes/android-bridge/README.md",
        "feral-nodes/V2_MOBILE_PORTING.md",
    ],
)
def test_docs_do_not_point_at_a_path_that_does_not_exist(doc: str):
    """These three files sent readers to the stale copy.

    The Android bridge README in particular quoted it as the cross-language
    contract, which is exactly how an SDK reimplements a superseded protocol.
    """
    path = REPO_ROOT / doc
    if not path.exists():
        pytest.skip(f"{doc} not present")
    text = path.read_text(encoding="utf-8")
    assert "ios-bridge/" not in text, (
        f"{doc} references feral-nodes/ios-bridge/, which is not built and no "
        f"longer exists; point it at ios-app/Sources/FeralBridge/"
    )
