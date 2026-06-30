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
  * ``/`` with the API key returns 200 and contains the v2 bundle
    markers (``FERAL`` + ``v2``) and does *not* contain the v1
    fallback marker.

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
import sys
from pathlib import Path


def _fail(msg: str) -> "None":
    print(f"✗ release wheel smoke failed: {msg}", file=sys.stderr)
    sys.exit(1)


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
    if "feral" not in lowered:
        _fail("root page is missing the 'FERAL' bundle marker")
    if "v2" not in lowered:
        _fail("root page is missing the 'v2' bundle marker")
    if "leaflet" in lowered:
        # v1 fallback shipped a leaflet asset; catching it means the
        # wheel silently regressed to the legacy UI.
        _fail("root page contains v1-only 'leaflet' asset — UI regressed")

    print(
        "  ✓ wheel serves v2 bundle and passes /health + / contract "
        f"(feral-ai=={version})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
