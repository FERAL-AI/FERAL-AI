#!/usr/bin/env python3
"""Single-source-of-truth FERAL version syncer.

The single source is ``feral-core/version.py::VERSION`` which proxies
``importlib.metadata.version("feral-ai")``. The package metadata is fed
from ``feral-core/pyproject.toml::[project] version`` at install time,
so the literal in ``feral-core/pyproject.toml`` is the *upstream* of
the runtime ``VERSION`` string. We sync from that pyproject literal to
every other place where a FERAL version literal appears in the repo.

Two modes:

    python3 scripts/sync_versions.py --check
        # Read VERSION; verify every declared location matches.
        # Exit non-zero with a per-file diff if drift is found.
        # Mutates nothing.

    python3 scripts/sync_versions.py --write
        # Read VERSION; rewrite every declared location to match.
        # Used by scripts/release.py during a real bump.

This file is the W7 successor to scripts/bump_version.py. The legacy
script still works (``test_version_consistency.py`` imports it), but
new release tooling and the ``version-coherence`` CI gate call THIS
script. Locations live below in ``VERSION_LOCATIONS`` — each entry
declares a relative path, a regex with a named ``version`` group, and
a replacement template using ``{version}``.

Source-of-truth resolution order (first hit wins):

  1. ``importlib.metadata.version("feral-ai")`` — works post-install.
  2. ``feral-core/pyproject.toml`` ``[project] version = "..."`` —
     works in a fresh checkout where the package isn't installed yet
     (this is what CI sees before ``pip install -e .`` runs in the
     ``brain-tests`` job; the ``version-coherence`` job runs without
     installing anything).

Owned-paths note (workstream W7, see docs/AGENT_PROMPTS.md §C.2):
the SCRIPT is owned by W7 and may sync to any path it knows about.
Editing the literal in unowned paths during a *release* (driven by
this script) is the explicit purpose of the script. The PR that
introduces THIS script does not bump the version, so no unowned-path
literal is altered by this commit.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ASOS_ROOT = Path(__file__).resolve().parent.parent

CALVER_RE = re.compile(r"^\d{4}\.\d{1,2}\.\d{1,2}$")
VERSION_PATTERN = r"(?P<version>\d{4}\.\d{1,2}\.\d{1,2})"


@dataclass(frozen=True)
class VersionLocation:
    """A single file × regex × replacement tuple.

    The regex MUST expose a named ``version`` capture group. The
    ``replacement`` template uses ``{version}``; any other named groups
    in the regex (e.g. ``indent``) are forwarded to ``str.format`` so
    a write doesn't silently reformat a file's leading whitespace.
    """

    path: str
    pattern: re.Pattern
    replacement: str
    description: str = ""
    owned_by_w7: bool = False


def _p(raw: str, flags: int = 0) -> re.Pattern:
    return re.compile(raw, flags)


# ---------------------------------------------------------------------------
# Declarative location list. Order matters only for human-readable output.
# Each location's regex MUST contain a named ``version`` capture group.
# ---------------------------------------------------------------------------
VERSION_LOCATIONS: tuple[VersionLocation, ...] = (
    # ---- Single source of truth (W7-owned) ---------------------------------
    VersionLocation(
        path="feral-core/pyproject.toml",
        pattern=_p(rf'(?m)^version = "{VERSION_PATTERN}"'),
        replacement='version = "{version}"',
        description="[project] version in feral-core/pyproject.toml — the upstream",
        owned_by_w7=True,
    ),
    # ---- Other W7-owned literals -------------------------------------------
    VersionLocation(
        path="desktop/src-tauri/tauri.conf.json",
        pattern=_p(rf'(?m)^\s*"version":\s*"{VERSION_PATTERN}"'),
        replacement='"version": "{version}"',
        description="FERAL desktop Tauri app version",
        owned_by_w7=True,
    ),
    VersionLocation(
        path="feral-ha-addon/config.yaml",
        pattern=_p(rf'(?m)^version:\s*"{VERSION_PATTERN}"'),
        replacement='version: "{version}"',
        description="HA add-on config.yaml version",
        owned_by_w7=True,
    ),
    VersionLocation(
        path="feral-ha-addon/Dockerfile",
        pattern=_p(rf"ARG FERAL_VERSION={VERSION_PATTERN}"),
        replacement="ARG FERAL_VERSION={version}",
        description="HA add-on Dockerfile FERAL_VERSION build arg",
        owned_by_w7=True,
    ),
    # NOTE: feral-core/services/mdns.py used to carry a literal version
    # string here. After the W7 refactor it imports VERSION from
    # ``feral_core.version`` at module load and embeds the result in the
    # mDNS ServiceInfo properties dict. There is no version literal to
    # sync there anymore; the regression test in test_version_singlesrc.py
    # asserts the literal stays gone.

    # ---- W7-owned docs (read-only context per §C.2 — this script's
    # ----  patcher will only touch the badge marker block; the rest of
    # ----  the README is left alone).
    VersionLocation(
        path="README.md",
        pattern=_p(
            r"(?ms)<!-- sync-versions:badge -->\n"
            r"\s*<img src=\"https://img\.shields\.io/badge/version-"
            rf"{VERSION_PATTERN}"
            r"-06b6d4\?style=flat-square\" alt=\"Version\" />\n"
            r"\s*<!-- /sync-versions:badge -->"
        ),
        replacement=(
            "<!-- sync-versions:badge -->\n"
            "  <img src=\"https://img.shields.io/badge/version-"
            "{version}"
            "-06b6d4?style=flat-square\" alt=\"Version\" />\n"
            "  <!-- /sync-versions:badge -->"
        ),
        description="README shields.io version badge (between sync-versions:badge markers)",
        owned_by_w7=True,
    ),
    VersionLocation(
        path="CHANGELOG.md",
        pattern=_p(rf"<!--\s*feral-version:\s*{VERSION_PATTERN}\s*-->"),
        replacement="<!-- feral-version: {version} -->",
        description="CHANGELOG current-version comment marker",
        owned_by_w7=True,
    ),
    # ---- Outside W7 ownership but synced by the same release flow.
    # ---- These are the places the legacy scripts/bump_version.py
    # ---- already syncs, so this script subsumes them. The W7 PR does
    # ---- not bump the version, so no literal here changes in this
    # ---- commit; CI's --check just confirms drift = 0 against the
    # ---- canonical pyproject value (currently 2026.4.32).
    VersionLocation(
        path="desktop/package.json",
        pattern=_p(rf'(?m)^\s*"version":\s*"{VERSION_PATTERN}"'),
        replacement='"version": "{version}"',
        description="FERAL desktop npm package version",
    ),
    VersionLocation(
        path="feral-extension/manifest.json",
        pattern=_p(rf'"version":\s*"{VERSION_PATTERN}"'),
        replacement='"version": "{version}"',
        description="Chrome extension manifest version",
    ),
    VersionLocation(
        path="feral-extension/popup.html",
        pattern=_p(rf"v{VERSION_PATTERN}"),
        replacement="v{version}",
        description="Popup footer version label",
    ),
    VersionLocation(
        path="feral-ha-addon/UPGRADE.md",
        pattern=_p(rf"ARG FERAL_VERSION={VERSION_PATTERN}"),
        replacement="ARG FERAL_VERSION={version}",
        description="UPGRADE.md FERAL_VERSION example pin",
    ),
    VersionLocation(
        path=".github/workflows/ha-addon.yml",
        pattern=_p(rf'(?m)^(?P<indent>\s*)default:\s*"{VERSION_PATTERN}"'),
        replacement='{indent}default: "{version}"',
        description="HA Add-on workflow_dispatch feral_version input default",
    ),
    VersionLocation(
        path=".github/workflows/ha-addon.yml",
        pattern=_p(rf'(?m)^(?P<indent>\s*)FERAL_VERSION:\s*"{VERSION_PATTERN}"'),
        replacement='{indent}FERAL_VERSION: "{version}"',
        description="HA Add-on workflow env default feral-ai PyPI version",
    ),
    VersionLocation(
        path="feral-core/agents/self_model.py",
        pattern=_p(rf"version={VERSION_PATTERN}"),
        replacement="version={version}",
        description="Runtime: line example in build_runtime_line docstring",
    ),
    # ---- v1 web client fallback strings (not in W7 ownership but
    # ----  carry a bare CalVer literal that drifts on every bump).
    # ----  Folded in here to retire the legacy
    # ----  ``scripts/bump_version.py`` mirror list — the CI gate
    # ----  ``test_single_calver_across_all_files`` walks this list,
    # ----  not bump_version's, after the consolidation in
    # ----  phase-1/truthfulness-sweep.
    VersionLocation(
        path="feral-client/src/components/AppShell.jsx",
        pattern=_p(rf"'{VERSION_PATTERN}'"),
        replacement="'{version}'",
        description="v1 client AppShell version fallback literals",
    ),
    VersionLocation(
        path="feral-client/src/pages/Dashboard.jsx",
        pattern=_p(rf"'{VERSION_PATTERN}'"),
        replacement="'{version}'",
        description="v1 client Dashboard version fallback literal",
    ),
    VersionLocation(
        path="feral-client/src/pages/SetupWizard.jsx",
        pattern=_p(rf"FERAL v{VERSION_PATTERN}"),
        replacement="FERAL v{version}",
        description="v1 client setup wizard footer version label",
    ),
    # ---- Top-level pyproject (not present in every checkout, optional).
    VersionLocation(
        path="pyproject.toml",
        pattern=_p(rf'(?m)^version = "{VERSION_PATTERN}"'),
        replacement='version = "{version}"',
        description="[project] version in top-level pyproject.toml (optional)",
    ),
)


# ---------------------------------------------------------------------------
# Source-of-truth resolution
# ---------------------------------------------------------------------------
def _read_pyproject_version(pyproject_path: Path) -> str | None:
    """Parse ``[project] version = "X"`` from a pyproject.toml without
    importing tomllib (so this works on bare CPython without project
    install). Returns the version string or None on miss."""
    if not pyproject_path.exists():
        return None
    text = pyproject_path.read_text(encoding="utf-8")
    # Be strict: only match the [project] section's version field.
    # We don't want to grab a tool.poetry.version etc.
    in_project = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            in_project = line == "[project]"
            continue
        if in_project:
            m = re.match(r'^version\s*=\s*"(?P<v>[^"]+)"', line)
            if m:
                return m.group("v")
    return None


def resolve_source_version(
    *, prefer_metadata: bool = True
) -> tuple[str, str]:
    """Return (version, source_label).

    Tries importlib.metadata first (works post-install), then falls back
    to feral-core/pyproject.toml's [project] version literal. We always
    cross-check the two when both are available and warn on mismatch.
    """
    pyproject_path = ASOS_ROOT / "feral-core" / "pyproject.toml"
    pyproject_version = _read_pyproject_version(pyproject_path)

    metadata_version: str | None = None
    if prefer_metadata:
        try:
            from importlib.metadata import PackageNotFoundError, version

            try:
                metadata_version = version("feral-ai")
            except PackageNotFoundError:
                metadata_version = None
        except Exception:
            metadata_version = None

    if metadata_version and pyproject_version:
        if metadata_version != pyproject_version:
            print(
                "  ⚠ WARNING: importlib.metadata says feral-ai=="
                f"{metadata_version} but feral-core/pyproject.toml says "
                f"{pyproject_version}. Using pyproject (the build-time "
                "upstream).",
                file=sys.stderr,
            )
        return pyproject_version, "feral-core/pyproject.toml"
    if pyproject_version:
        return pyproject_version, "feral-core/pyproject.toml"
    if metadata_version:
        return metadata_version, "importlib.metadata(feral-ai)"
    raise SystemExit(
        "✗ could not resolve VERSION from importlib.metadata OR "
        "feral-core/pyproject.toml — is the repo intact?"
    )


# ---------------------------------------------------------------------------
# Core sync engine
# ---------------------------------------------------------------------------
@dataclass
class SyncReport:
    source_version: str
    source_label: str
    in_sync: list[VersionLocation]
    drifted: list[tuple[VersionLocation, set[str]]]
    rewritten: list[VersionLocation]
    missing_files: list[VersionLocation]
    pattern_not_found: list[VersionLocation]

    @property
    def has_drift(self) -> bool:
        return bool(self.drifted)


def _observed_versions(text: str, loc: VersionLocation) -> set[str]:
    return {m.group("version") for m in loc.pattern.finditer(text)}


def _apply_write(text: str, loc: VersionLocation, new_version: str) -> str:
    def _sub(match: re.Match) -> str:
        old_version = match.group("version")
        if old_version == new_version:
            return match.group(0)
        groupdict = dict(match.groupdict() or {})
        groupdict["version"] = new_version
        return loc.replacement.format(**groupdict)

    return loc.pattern.sub(_sub, text)


def sync(*, write: bool, prefer_metadata: bool = True) -> SyncReport:
    source_version, source_label = resolve_source_version(
        prefer_metadata=prefer_metadata
    )
    if not CALVER_RE.match(source_version):
        raise SystemExit(
            f"✗ source version {source_version!r} from {source_label} is "
            "not a valid YYYY.M.D calver"
        )

    in_sync: list[VersionLocation] = []
    drifted: list[tuple[VersionLocation, set[str]]] = []
    rewritten: list[VersionLocation] = []
    missing_files: list[VersionLocation] = []
    pattern_not_found: list[VersionLocation] = []

    for loc in VERSION_LOCATIONS:
        abs_path = ASOS_ROOT / loc.path
        if not abs_path.exists():
            missing_files.append(loc)
            continue

        original = abs_path.read_text(encoding="utf-8")
        observed = _observed_versions(original, loc)
        if not observed:
            pattern_not_found.append(loc)
            continue

        if observed == {source_version}:
            in_sync.append(loc)
            continue

        drifted.append((loc, observed))

        if write:
            new_text = _apply_write(original, loc, source_version)
            if new_text != original:
                abs_path.write_text(new_text, encoding="utf-8")
                rewritten.append(loc)

    return SyncReport(
        source_version=source_version,
        source_label=source_label,
        in_sync=in_sync,
        drifted=drifted,
        rewritten=rewritten,
        missing_files=missing_files,
        pattern_not_found=pattern_not_found,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _print_report(report: SyncReport, *, write: bool) -> None:
    print(
        f"VERSION = {report.source_version} (source: {report.source_label})"
    )
    print()
    print(
        f"  in-sync: {len(report.in_sync)} location(s)"
    )
    for loc in report.in_sync:
        print(f"    ✓ {loc.path}")
    print()

    if report.drifted:
        print(f"  drifted: {len(report.drifted)} location(s)")
        for loc, observed in report.drifted:
            tag = " (W7-owned)" if loc.owned_by_w7 else ""
            print(
                f"    ✗ {loc.path}{tag}: observed {sorted(observed)} "
                f"≠ source {report.source_version}"
            )
        print()

    if report.rewritten:
        print(f"  rewritten: {len(report.rewritten)} location(s)")
        for loc in report.rewritten:
            print(f"    → {loc.path}")
        print()

    if report.pattern_not_found:
        print(
            f"  pattern-not-found: {len(report.pattern_not_found)} "
            "location(s) (file exists but regex did not match — usually "
            "fine if the literal was already removed)"
        )
        for loc in report.pattern_not_found:
            print(f"    · {loc.path} ({loc.description})")
        print()

    if report.missing_files:
        print(
            f"  missing-files: {len(report.missing_files)} location(s) "
            "(declared but not in this checkout — e.g. partial publish "
            "tarball)"
        )
        for loc in report.missing_files:
            print(f"    · {loc.path}")
        print()

    summary = (
        f"drift = {len(report.drifted)} location(s)"
        if report.drifted
        else "drift = 0"
    )
    if write and report.rewritten:
        summary += f" — wrote {len(report.rewritten)} file(s)"
    print(f"  summary: {summary}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sync_versions",
        description=(
            "Single-source-of-truth FERAL version syncer. The source "
            "is feral-core/version.py::VERSION (proxied from "
            "feral-core/pyproject.toml). --check reports drift; "
            "--write resyncs every declared location. Used by "
            "scripts/release.py and the version-coherence CI gate."
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check",
        action="store_true",
        help="Read-only: exit non-zero with a diff if any declared "
        "location's version literal differs from the source.",
    )
    mode.add_argument(
        "--write",
        action="store_true",
        help="Rewrite every declared location to match the source.",
    )
    parser.add_argument(
        "--no-metadata",
        action="store_true",
        help="Skip importlib.metadata; read the source from "
        "feral-core/pyproject.toml only. Useful for CI runs that "
        "do NOT install the package first.",
    )

    args = parser.parse_args(list(argv) if argv is not None else None)

    report = sync(write=args.write, prefer_metadata=not args.no_metadata)
    _print_report(report, write=args.write)

    if args.check and report.has_drift:
        print(
            "\n✗ version drift detected. Run "
            "`python3 scripts/sync_versions.py --write` to resync, or "
            "`python3 scripts/release.py <bump>` to bump and resync as "
            "part of a release.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
