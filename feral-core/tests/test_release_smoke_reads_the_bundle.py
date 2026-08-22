"""The release gate encoded a vite setting as an assumption.

`scripts/release_wheel_smoke.py` finds the bundle's entry points by
regex over `index.html`, and that regex accepted only `assets/x.js` and
`./assets/x.js`. `vite.config.js` then changed `base` from `'./'` to
`'/'` for a real, measured reason (a relative ref resolves against the
current URL's directory, so a hard load of `/memory/context` fetched
`/memory/assets/index-<hash>.js`, got index.html back from the SPA
fallback at 200, and executed HTML as JavaScript). The smoke script was
not changed with it.

The result was a release that could not be published at all. The gate
reported both of these, two lines apart:

    ✗ index.html declares no assets/*.js or assets/*.css entry point;
      this is not a built v2 bundle
    · webui_v2 bundle OK at .../webui_v2 (1 js / 1 css)

A gate that contradicts itself is worse than no gate, because the
failure looks like the artifact is broken when the artifact is fine.

These tests pin the parser against both forms, so whichever base a
future build uses, the gate keeps working. They are cheap: no wheel is
built, only the module's own function is exercised.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
SMOKE = REPO / "scripts" / "release_wheel_smoke.py"
BUNDLE = REPO / "feral-core" / "webui_v2"


@pytest.fixture(scope="module")
def smoke():
    if not SMOKE.exists():
        pytest.skip("release_wheel_smoke.py not in this checkout")
    spec = importlib.util.spec_from_file_location("release_wheel_smoke", SMOKE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestItReadsEveryBaseViteCanEmit:
    @pytest.mark.parametrize("prefix,label", [
        ("/", "base '/' (current: fixes deep-route hard loads)"),
        ("./", "base './' (what shipped up to 2026.8.12)"),
        ("", "bare, which some bundlers emit"),
    ])
    def test_entry_points_are_found(self, smoke, prefix, label):
        html = (
            f'<script type="module" src="{prefix}assets/index-abc123.js"></script>'
            f'<link rel="stylesheet" href="{prefix}assets/index-def456.css">'
        )
        refs = smoke._bundle_asset_refs(html)
        assert refs == ["assets/index-abc123.js", "assets/index-def456.css"], label

    def test_the_result_is_always_bundle_root_relative(self, smoke):
        """Downstream joins these onto the bundle directory.

        A leading slash surviving here would make that join absolute and
        silently point outside the bundle.
        """
        for prefix in ("/", "./", ""):
            for ref in smoke._bundle_asset_refs(
                f'<script src="{prefix}assets/x.js"></script>'
            ):
                assert not ref.startswith(("/", "."))


class TestItStillRefusesThingsThatAreNotEntryPoints:
    def test_a_favicon_or_icon_cannot_stand_in_for_the_bundle(self, smoke):
        html = (
            '<link rel="icon" href="/favicon.ico">'
            '<link rel="apple-touch-icon" href="/icons/icon-192.png">'
            '<link rel="manifest" href="/manifest.webmanifest">'
        )
        assert smoke._bundle_asset_refs(html) == []

    def test_an_unbuilt_page_declares_nothing(self, smoke):
        assert smoke._bundle_asset_refs("<html><body>no bundle here</body></html>") == []

    def test_a_repeated_reference_is_counted_once(self, smoke):
        html = '<script src="/assets/a.js"></script><script src="/assets/a.js"></script>'
        assert smoke._bundle_asset_refs(html) == ["assets/a.js"]


class TestAgainstTheBundleActuallyInThisCheckout:
    def test_the_gate_can_read_the_bundle_we_ship(self, smoke):
        """The check that would have caught this before the tag.

        Everything above is synthetic. This one asks the real question:
        can the release gate read the real built index.html, and does
        every entry point it names exist on disk?
        """
        index = BUNDLE / "index.html"
        if not index.exists():
            pytest.skip("webui_v2 is a build artifact; run scripts/build_webui_v2.sh")

        refs = smoke._bundle_asset_refs(index.read_text(errors="replace"))
        assert refs, (
            "the release gate cannot find an entry point in the bundle this "
            "checkout would publish. It reads index.html by regex, so this "
            "usually means vite's `base` changed and the pattern in "
            "scripts/release_wheel_smoke.py did not."
        )
        missing = [r for r in refs if not (BUNDLE / r).is_file()]
        assert missing == [], f"index.html names files not in the bundle: {missing}"
