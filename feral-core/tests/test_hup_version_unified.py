"""HUP version unification — single canonical version across 5 surfaces.

This is the CI gate for Lane 11 acceptance item "HUP 1.3.0 unified across
spec, Python SDK, TS SDK, Swift SDK, iOS Info.plist". Any drift between
these five surfaces — even a hypothetical "we only forgot to bump the
manifest" — fails this test and the build.

The five surfaces (and the one regex / parse line we use for each):

1. Brain canonical (``feral-core/models/protocol.py``) → ``HUP_VERSION``
   Python constant. Authoritative — every other surface mirrors this.
2. Public spec (``feral-nodes/HUP_SPEC.md``) → ``**Version:** `HUP v<X>``
   bold-version line in the header.
3. Python node SDK (``feral-nodes/python-node-sdk/src/feral_node_sdk/
   schemas.py``) → ``HUP_VERSION`` literal.
4. TypeScript node SDK (``feral-nodes/ts-node-sdk/src/schemas.ts``) →
   ``HUP_VERSION`` string literal.
5. Swift node SDK (``feral-nodes/ios-node-sdk/Sources/FeralNodeSDK/
   Info.swift``) → ``hupVersion`` string literal. The companion iOS app
   re-vendors this same file under ``feral-companion-ios/`` — the test
   walks that file too when the path is present on disk (it is for the
   parallel-repo developer workflow described in
   ``ASOS/AUDIT-r14/phase2/prompts/11-nodes-ios-hardware.md``).

Why a literal-scrape test rather than importing each surface: TS + Swift
cannot be imported into the Python interpreter, so the unification gate
has to live as a text-level coherence check.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from models.protocol import HUP_VERSION as BRAIN_HUP_VERSION


REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract(pattern: str, text: str, *, source: str) -> str:
    m = re.search(pattern, text, flags=re.MULTILINE)
    if not m:
        raise AssertionError(
            f"could not find HUP version literal in {source} using pattern {pattern!r}"
        )
    return m.group(1)


def test_hup_version_is_1_3_0_in_brain():
    """The brain's canonical constant is the source of truth."""
    assert BRAIN_HUP_VERSION == "1.3.0", (
        "Brain HUP_VERSION must be '1.3.0' — every other surface mirrors this. "
        "If you intentionally bumped the protocol, update every surface in this "
        "test and bump the v1.0 release gate evidence."
    )


def test_hup_spec_header_matches_brain():
    spec = _read(REPO_ROOT / "feral-nodes" / "HUP_SPEC.md")
    version = _extract(
        r"\*\*Version:\*\*\s+`HUP\s+v([0-9]+\.[0-9]+\.[0-9]+)`",
        spec,
        source="feral-nodes/HUP_SPEC.md header",
    )
    assert version == BRAIN_HUP_VERSION, (
        f"HUP_SPEC.md header advertises v{version} but brain canonical is "
        f"v{BRAIN_HUP_VERSION}. The spec is the operator-facing source of "
        "truth — drift here is the same kind of bug that motivated this test."
    )


def test_python_node_sdk_version_matches_brain():
    schemas = _read(
        REPO_ROOT
        / "feral-nodes"
        / "python-node-sdk"
        / "src"
        / "feral_node_sdk"
        / "schemas.py"
    )
    version = _extract(
        r'^HUP_VERSION\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"',
        schemas,
        source="feral_node_sdk/schemas.py",
    )
    assert version == BRAIN_HUP_VERSION


def test_python_node_sdk_init_version_matches_brain():
    init = _read(
        REPO_ROOT
        / "feral-nodes"
        / "python-node-sdk"
        / "src"
        / "feral_node_sdk"
        / "__init__.py"
    )
    version = _extract(
        r'^HUP_VERSION\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"',
        init,
        source="feral_node_sdk/__init__.py",
    )
    assert version == BRAIN_HUP_VERSION


def test_ts_node_sdk_version_matches_brain():
    schemas = _read(
        REPO_ROOT / "feral-nodes" / "ts-node-sdk" / "src" / "schemas.ts"
    )
    version = _extract(
        r'export\s+const\s+HUP_VERSION\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"',
        schemas,
        source="ts-node-sdk schemas.ts",
    )
    assert version == BRAIN_HUP_VERSION


def test_swift_node_sdk_version_matches_brain():
    info = _read(
        REPO_ROOT
        / "feral-nodes"
        / "ios-node-sdk"
        / "Sources"
        / "FeralNodeSDK"
        / "Info.swift"
    )
    version = _extract(
        r'hupVersion\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"',
        info,
        source="ios-node-sdk Info.swift",
    )
    assert version == BRAIN_HUP_VERSION


def test_seed_manifests_match_brain():
    """Track-B daemon manifests (wristband + w300) must mirror the brain."""
    for manifest in [
        REPO_ROOT / "feral-nodes" / "wristband_daemon" / "manifest.json",
        REPO_ROOT / "feral-nodes" / "w300_daemon" / "manifest.json",
    ]:
        raw = _read(manifest)
        version = _extract(
            r'"hup_version"\s*:\s*"([0-9]+\.[0-9]+\.[0-9]+)"',
            raw,
            source=str(manifest.relative_to(REPO_ROOT)),
        )
        assert version == BRAIN_HUP_VERSION, (
            f"{manifest.relative_to(REPO_ROOT)}: hup_version is v{version} "
            f"but brain canonical is v{BRAIN_HUP_VERSION}"
        )


@pytest.mark.parametrize(
    "rel_path",
    [
        # Co-located companion-ios checkout used during the parallel-repo
        # dev workflow described in
        # ASOS/AUDIT-r14/phase2/prompts/11-nodes-ios-hardware.md.
        Path("..") / "feral-companion-ios" / "Sources" / "FeralNodeSDK" / "Info.swift",
        Path("..") / ".." / "feral-companion-ios" / "Sources" / "FeralNodeSDK" / "Info.swift",
    ],
)
def test_companion_ios_swift_version_matches_brain_when_present(rel_path: Path):
    """If the companion-ios checkout is next to ASOS, its vendored SDK
    must mirror the brain.

    Skipped when the directory isn't present so CI runners without the
    companion repo cloned alongside don't fail. The parallel-PR
    coordination in WORK_LOG ensures the iOS PR's CI re-asserts this
    against its own canonical Info.swift in the iOS repo's tests.
    """
    candidate = (REPO_ROOT / rel_path).resolve()
    if not candidate.is_file():
        pytest.skip(f"companion-ios checkout not present at {candidate}")
    info = _read(candidate)
    version = _extract(
        r'hupVersion\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"',
        info,
        source=str(candidate),
    )
    assert version == BRAIN_HUP_VERSION


def test_companion_ios_info_plist_when_present():
    """If the companion-ios Info.plist is co-located, the FERALHUPVersion
    key MUST match the brain. The key is added by Lane 11 to make the
    iOS app self-describe the protocol it speaks so operators can verify
    a mismatched build without launching the app.
    """
    for candidate in [
        REPO_ROOT.parent / "feral-companion-ios" / "App" / "Info.plist",
        REPO_ROOT.parent.parent / "feral-companion-ios" / "App" / "Info.plist",
    ]:
        if candidate.is_file():
            plist = _read(candidate)
            m = re.search(
                r"<key>FERALHUPVersion</key>\s*<string>([0-9]+\.[0-9]+\.[0-9]+)</string>",
                plist,
            )
            assert m is not None, (
                f"{candidate}: FERALHUPVersion key missing — Lane 11 contract "
                "is that the iOS app self-describes the HUP version it speaks."
            )
            assert m.group(1) == BRAIN_HUP_VERSION
            return
    pytest.skip("companion-ios Info.plist not present co-located with ASOS")
