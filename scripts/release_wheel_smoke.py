#!/usr/bin/env python3
"""Runtime smoke test for an installed ``feral-ai`` wheel.

Shared by two stages of the release pipeline:

1. ``publish.yml`` pre-publish gate: runs against the freshly built wheel
   inside an ephemeral virtualenv before anything leaves the build job.
2. ``publish.yml`` canary stage: runs after ``pip install`` from the
   staging index (e.g. TestPyPI) so we prove the *uploaded* artifact
   installs cleanly and answers HTTP the same way it did at build time.

Contract (fail loudly on any of these):

* ``feral-ai`` is importable and advertises a version via
  ``importlib.metadata``.
* The bundled ``webui_v2`` directory is present as a site-packages
  sibling of ``api/`` (this is the regression that shipped 2026.4.17
  when a hyphenated dir got silently dropped from the wheel).
* ``webui_v2/index.html`` and at least one ``assets/*.js`` +
  ``assets/*.css`` are present.
* The FastAPI app boots under ``TestClient`` and:
  * ``/health`` returns 200,
  * ``/`` with the API key returns 200 and serves the *bundle*: the
    body must reference every ``./assets/...`` entry point that the
    on-disk ``index.html`` references, and each of those asset URLs
    must itself answer 200 with a non-HTML body,
  * ``/`` does *not* contain the v1 fallback marker.

Why the root check is written against asset references and not
against words: the previous version asserted the body contained
``FERAL`` and ``v2`` and lacked ``leaflet``. All three of those hold
for ``api/server.py``'s ``_PACKAGING_FAULT_HTML``, the page the brain
serves *when the wheel shipped without* ``webui_v2/``, because that
page names ``webui_v2/`` and ``the v2 dashboard`` in its own prose. The
check was therefore satisfied by the exact failure it was written to
catch. ``./assets/index-<hash>.js`` appears only in a real built
bundle, so that is what is asserted now, and the asset is fetched to
prove the static mount is live rather than merely named.

This script is intentionally dependency-light: it relies only on what
the installed wheel already brings in (``fastapi``'s ``TestClient``
ships via ``httpx`` in the wheel's runtime deps).

The caller is expected to ``pip install`` the wheel with
``--constraint feral-core/requirements.lock`` so the resolution is
reproducible (see ``.github/workflows/publish.yml``). Without that
constraint, the smoke is at the mercy of whatever transitive PyPI
publishes between CI green and the build job — see the
``fastapi==0.137`` regression that surfaced as the v2026.6.13 admin
merge.

Usage::

    python scripts/release_wheel_smoke.py [--expected-version X.Y.Z]

Exit code is 0 on success, 1 on any contract violation.
"""
from __future__ import annotations

import argparse
import importlib.metadata as md
import os
import re
import sys
from pathlib import Path


def _fail(msg: str) -> "None":
    print(f"✗ release wheel smoke failed: {msg}", file=sys.stderr)
    sys.exit(1)


#: ``<script src="./assets/x.js">`` and ``<link href="./assets/x.css">``
#: as emitted by Vite. Anchored on ``assets/`` so a favicon or manifest
#: reference cannot stand in for the bundle entry points.
_ASSET_REF = re.compile(r'(?:src|href)="((?:\./)?assets/[^"]+\.(?:js|css))"')

#: Substrings that only ever appear on a page the brain serves *instead*
#: of the bundle. ``api/server.py`` renders ``_PACKAGING_FAULT_HTML`` when
#: ``webui_v2/`` is missing and ``_FALLBACK_HTML`` when no UI was ever
#: built; both are 200 responses that mention FERAL, and the first also
#: mentions "v2".
_NOT_THE_BUNDLE = (
    "this install shipped without the v2 dashboard",
    "the web dashboard is not bundled in this install",
    "you are looking at the superseded v1 feral client",
)


def _bundle_asset_refs(index_html: str) -> list[str]:
    """Asset entry points the built ``index.html`` declares.

    Returned as bundle-root-relative paths (no leading ``./``), which is
    both what appears in the served HTML and, with a leading ``/``, the
    URL the static mount answers on.
    """
    seen: list[str] = []
    for ref in _ASSET_REF.findall(index_html):
        rel = ref[2:] if ref.startswith("./") else ref
        if rel not in seen:
            seen.append(rel)
    return seen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Runtime smoke for an installed feral-ai wheel."
    )
    parser.add_argument(
        "--expected-version",
        default=None,
        help=(
            "If set, assert that importlib.metadata reports this exact "
            "version. Useful for canary stages where we want to prove we "
            "installed the just-uploaded artifact (and not a stale one)."
        ),
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    # A non-empty API key is required so the auth middleware does not
    # force startup into a degraded mode — we want the real serving path.
    os.environ.setdefault("FERAL_API_KEY", "release-wheel-smoke-key")

    try:
        version = md.version("feral-ai")
    except md.PackageNotFoundError:
        _fail("feral-ai is not installed in this interpreter")
        return 1  # unreachable; keeps type-checkers happy

    print(f"  · installed feral-ai=={version}")

    if args.expected_version and args.expected_version != version:
        _fail(
            "installed version mismatch: "
            f"expected {args.expected_version!r}, got {version!r}"
        )

    try:
        import api  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        _fail(f"could not import api package from installed wheel: {exc}")
        return 1

    site = Path(api.__file__).resolve().parent.parent
    v2 = site / "webui_v2"
    index = v2 / "index.html"
    assets = v2 / "assets"

    if not v2.is_dir():
        _fail(f"webui_v2/ missing from installed wheel at {site}")
    if not index.exists():
        _fail(f"webui_v2/index.html missing at {v2}")
    if not assets.is_dir():
        _fail(f"webui_v2/assets/ missing at {v2}")

    js = list(assets.glob("*.js"))
    css = list(assets.glob("*.css"))
    if not js:
        _fail(f"no webui_v2 JS bundle found in {assets}")
    if not css:
        _fail(f"no webui_v2 CSS bundle found in {assets}")

    print(
        f"  · webui_v2 bundle OK at {v2} "
        f"({len(js)} js / {len(css)} css)"
    )

    # The markers the served page must carry, read from the bundle that
    # is actually on disk rather than hard-coded: Vite renames these on
    # every build, so a literal here would rot into a check that passes
    # because it stopped meaning anything.
    index_html = index.read_text(encoding="utf-8", errors="replace")
    asset_refs = _bundle_asset_refs(index_html)
    if not asset_refs:
        _fail(
            f"{index} declares no assets/*.js or assets/*.css entry point; "
            "this is not a built v2 bundle"
        )
    print(f"  · bundle entry points: {', '.join(asset_refs)}")

    try:
        from api.server import app  # type: ignore[import-not-found]
        from fastapi.testclient import TestClient  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        _fail(
            "installed wheel is missing runtime deps for smoke "
            f"(api.server / fastapi.testclient): {exc}"
        )
        return 1

    client = TestClient(app, raise_server_exceptions=False)

    health = client.get("/health")
    if health.status_code != 200:
        _fail(f"/health returned {health.status_code}, expected 200")

    root = client.get(
        "/",
        headers={"Authorization": f"Bearer {os.environ['FERAL_API_KEY']}"},
    )
    if root.status_code != 200:
        _fail(f"/ returned {root.status_code}, expected 200")

    body = root.text
    lowered = body.lower()

    # Named first, because it produces the actionable message. Any of
    # these means the brain decided it had no bundle to serve and
    # answered 200 with an explanation instead.
    for marker in _NOT_THE_BUNDLE:
        if marker in lowered:
            _fail(
                "/ served one of api/server.py's no-bundle pages, not the v2 "
                f"bundle (matched {marker!r}). The wheel installed a "
                "webui_v2/ that the running app did not serve."
            )

    # The load-bearing assertion. Only a built bundle's index.html
    # references its own hashed asset entry points; no fallback page does.
    missing = [ref for ref in asset_refs if ref not in body]
    if missing:
        _fail(
            "/ did not serve the v2 bundle: the response is missing asset "
            f"reference(s) {missing} that {index} declares. The served page "
            "is something other than the bundled index.html."
        )

    if "leaflet" in lowered:
        # v1 fallback shipped a leaflet asset; catching it means the
        # wheel silently regressed to the legacy UI.
        _fail("root page contains v1-only 'leaflet' asset — UI regressed")

    # Referencing an asset is not serving it. The static mount is
    # registered separately (api/server.py mounts /assets only when the
    # bundle's assets/ directory exists), so a bundle whose index.html
    # shipped without its assets/ would satisfy every check above and
    # still render a blank page in a browser.
    for ref in asset_refs:
        asset = client.get(
            "/" + ref,
            headers={"Authorization": f"Bearer {os.environ['FERAL_API_KEY']}"},
        )
        if asset.status_code != 200:
            _fail(
                f"/{ref} returned {asset.status_code}, expected 200; the v2 "
                "static mount is not serving the bundle's own assets"
            )
        # The SPA catch-all answers index.html for unknown paths, so a
        # 200 alone does not prove the asset exists. HTML back from a
        # .js/.css URL means it fell through.
        if asset.text.lstrip()[:9].lower().startswith("<!doctype"):
            _fail(
                f"/{ref} returned an HTML document; the request fell through "
                "to the SPA catch-all, so the asset is not actually bundled"
            )

    print(
        f"  ✓ wheel serves the v2 bundle at / (entry points {', '.join(asset_refs)} "
        f"referenced and fetchable) and passes /health (feral-ai=={version})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
