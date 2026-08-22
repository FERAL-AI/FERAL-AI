"""The wheel shipped a dashboard with no fonts and no icons.

`pip install feral-ai` serves `webui_v2/`, and what reaches a user is
whatever `[tool.setuptools.package-data]` globs plus whatever setuptools
discovered as a package. Measured on a built wheel before this was
fixed:

    webui_v2 files on disk : 69
    webui_v2 files in wheel: 7
    KaTeX fonts            : 0 of 59   (referenced 59 times from the CSS)
    PWA icons              : 0 of 3    (referenced by index.html and
                                        manifest.webmanifest)

Two independent causes. The `webui_v2.assets` glob listed no font
extensions, and `webui_v2/icons/` had no `__init__.py`, so setuptools
never discovered it as a package and no glob could reach it at all.

Nothing caught it. `check_webui_v2_contract.py` reads the SOURCE tree,
where the files obviously exist, and `release_wheel_smoke.py` resolves
only the `assets/*.js|css` that `index.html` names. So every gate agreed
the bundle was fine while the published artifact rendered maths in a
fallback face and 404ed its own icons.

These tests read the packaging config rather than building a wheel:
building takes ~40s and needs network, and the two failure modes are
both statements about configuration.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parent.parent
WEBUI_V2 = CORE / "webui_v2"

pytestmark = pytest.mark.skipif(
    not WEBUI_V2.is_dir(),
    reason="webui_v2 is a build artifact; run scripts/build_webui_v2.sh",
)


def _package_data() -> dict[str, list[str]]:
    with open(CORE / "pyproject.toml", "rb") as fh:
        cfg = tomllib.load(fh)
    return cfg["tool"]["setuptools"]["package-data"]


def _dirs_with_files() -> list[Path]:
    """Every directory under webui_v2 that actually holds a file."""
    out = []
    for path in sorted(WEBUI_V2.rglob("*")):
        if path.is_dir() and any(p.is_file() for p in path.iterdir()):
            out.append(path)
    return [WEBUI_V2] + out


def _pkg_name(directory: Path) -> str:
    rel = directory.relative_to(WEBUI_V2.parent)
    return ".".join(rel.parts)


class TestEveryDirectoryIsReachable:
    def test_each_one_is_a_discovered_package(self):
        """No `__init__.py` means no glob can reach the directory.

        This is what hid `webui_v2/icons/` from the wheel entirely: it
        is not that the icons failed to match a pattern, it is that
        setuptools never looked inside.
        """
        missing = [
            str(d.relative_to(CORE))
            for d in _dirs_with_files()
            if not (d / "__init__.py").exists()
        ]
        assert missing == [], (
            "these bundle directories are not discoverable packages, so "
            "nothing in them reaches the wheel: "
            + ", ".join(missing)
            + ". Add an __init__.py in scripts/build_webui_v2.sh."
        )

    def test_each_one_has_a_package_data_entry(self):
        data = _package_data()
        missing = [
            _pkg_name(d) for d in _dirs_with_files() if _pkg_name(d) not in data
        ]
        assert missing == [], (
            "no [tool.setuptools.package-data] entry for: "
            + ", ".join(missing)
        )


class TestEveryExtensionIsGlobbed:
    def test_no_file_type_in_the_bundle_is_left_out(self):
        """The fonts failed here: 59 files whose extension was in no glob.

        Checks by extension rather than by filename so a new asset type
        (a video, a wasm blob, another font format) fails this the day
        it is added instead of the day someone pip-installs it.
        """
        data = _package_data()
        uncovered: list[str] = []

        for directory in _dirs_with_files():
            pkg = _pkg_name(directory)
            globs = data.get(pkg, [])
            covered = {
                g[1:].lower() for g in globs if g.startswith("*.")
            }
            for child in sorted(directory.iterdir()):
                if not child.is_file() or child.name == "__init__.py":
                    continue
                ext = child.suffix.lower()
                if ext and ext not in covered:
                    uncovered.append(f"{pkg}/{child.name} ({ext})")

        assert uncovered == [], (
            "these bundle files match no package-data glob, so the wheel "
            "ships without them: " + ", ".join(sorted(set(uncovered))[:12])
        )


class TestTheCssAssetsResolve:
    def test_every_font_the_css_asks_for_is_in_the_bundle(self):
        """The reference count is the honest measure of "complete".

        59 `url(...)` references from the built CSS, 59 files on disk.
        A bundle that satisfies the globs but is missing a file the CSS
        names is still a broken dashboard.
        """
        import re

        css_files = list((WEBUI_V2 / "assets").glob("*.css"))
        assert css_files, "no built CSS in the bundle"

        referenced: set[str] = set()
        for css in css_files:
            body = css.read_text(errors="ignore")
            referenced |= set(
                re.findall(r"([A-Za-z0-9_.\-]+\.(?:woff2|woff|ttf|otf|eot))", body)
            )

        present = {p.name for p in (WEBUI_V2 / "assets").iterdir() if p.is_file()}
        missing = sorted(referenced - present)
        assert missing == [], (
            f"the CSS references {len(referenced)} font files and "
            f"{len(missing)} are absent from the bundle: {missing[:8]}"
        )


class TestThePwaIconsSurvive:
    def test_the_icons_the_manifest_names_are_present(self):
        import json
        import re

        manifest = WEBUI_V2 / "manifest.webmanifest"
        if not manifest.exists():
            pytest.skip("this bundle ships no web manifest")

        named = {
            Path(icon.get("src", "")).name
            for icon in json.loads(manifest.read_text()).get("icons", [])
            if icon.get("src")
        }
        # index.html can name icons the manifest does not.
        index = (WEBUI_V2 / "index.html").read_text(errors="ignore")
        named |= {
            Path(m).name for m in re.findall(r'href="([^"]*icons/[^"]+)"', index)
        }
        if not named:
            pytest.skip("this bundle references no icons")

        present = {
            p.name for p in WEBUI_V2.rglob("*") if p.is_file()
        }
        missing = sorted(named - present)
        assert missing == [], f"icons referenced but not bundled: {missing}"
