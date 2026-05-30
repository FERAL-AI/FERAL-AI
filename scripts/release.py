#!/usr/bin/env python3
"""FERAL release driver.

End-to-end release flow built on top of ``scripts/sync_versions.py``:

    1. Compute the next version from the current pyproject value and
       the requested bump kind (major | minor | patch).
    2. Rewrite the canonical literal in ``feral-core/pyproject.toml``.
    3. Run ``scripts/sync_versions.py --write`` to propagate to every
       other declared location.
    4. Insert a templated entry under the ``[Unreleased]`` heading in
       ``CHANGELOG.md`` (preserved for the maintainer to flesh out).
    5. Rebuild/sync the bundled v2 web UI assets and validate the
       model-picker contract tokens in the shipped bundle.
    6. Run the test suites (pytest + vitest, both gated by --skip-tests).
    7. Build the wheel/sdist artifacts (skipped with --skip-build).
    8. Commit, push, and open a release PR via ``gh`` (skipped with
       --no-pr / --dry-run).

Calver shape: YYYY.M.D. The bump kinds are interpreted as:

    major  → YYYY+1 . 1   . 0
    minor  → YYYY   . M+1 . 0
    patch  → YYYY   . M   . D+1

Examples::

    python3 scripts/release.py patch
    python3 scripts/release.py minor --dry-run
    python3 scripts/release.py 2026.5.0   # explicit override

Owned-paths note (): this script is created under  ownership.
It calls into sync_versions.py which knows about every declared
location; that's the explicit purpose of the release flow. No
unowned-path literal is altered until a maintainer actually runs
this script with a real bump.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence

ASOS_ROOT = Path(__file__).resolve().parent.parent
SYNC_VERSIONS = ASOS_ROOT / "scripts" / "sync_versions.py"
BUILD_WEBUI_V2 = ASOS_ROOT / "scripts" / "build_webui_v2.sh"
CHECK_WEBUI_V2 = ASOS_ROOT / "scripts" / "check_webui_v2_contract.py"
CHANGELOG = ASOS_ROOT / "CHANGELOG.md"
PYPROJECT = ASOS_ROOT / "feral-core" / "pyproject.toml"

CALVER_RE = re.compile(r"^(?P<y>\d{4})\.(?P<m>\d{1,2})\.(?P<d>\d{1,2})$")


# ---------------------------------------------------------------------------
# Subprocess helper — print every command before running so the user can
# audit the release pipeline as it goes.
# ---------------------------------------------------------------------------
def _run(
    cmd: Sequence[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess:
    printable = " ".join(str(c) for c in cmd)
    where = f" (in {cwd})" if cwd else ""
    print(f"  $ {printable}{where}")
    return subprocess.run(
        list(cmd),
        cwd=cwd,
        check=check,
        text=True,
        capture_output=capture,
    )


# ---------------------------------------------------------------------------
# Version arithmetic
# ---------------------------------------------------------------------------
def _read_current_version() -> str:
    if not PYPROJECT.exists():
        raise SystemExit(f"✗ {PYPROJECT} not found")
    text = PYPROJECT.read_text(encoding="utf-8")
    in_project = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            in_project = line == "[project]"
            continue
        if in_project:
            m = re.match(r'^version\s*=\s*"([^"]+)"', line)
            if m:
                return m.group(1)
    raise SystemExit(
        "✗ could not find [project] version in feral-core/pyproject.toml"
    )


def _bump(current: str, kind: str) -> str:
    if kind not in {"major", "minor", "patch"}:
        # Allow the caller to pass an explicit calver as the kind.
        if CALVER_RE.match(kind):
            return kind
        raise SystemExit(
            f"✗ unknown bump kind {kind!r}; expected major|minor|patch "
            "or an explicit YYYY.M.D"
        )
    m = CALVER_RE.match(current)
    if not m:
        raise SystemExit(
            f"✗ current version {current!r} is not YYYY.M.D — cannot bump"
        )
    y, mo, d = int(m["y"]), int(m["m"]), int(m["d"])
    if kind == "major":
        return f"{y + 1}.1.0"
    if kind == "minor":
        return f"{y}.{mo + 1}.0"
    return f"{y}.{mo}.{d + 1}"


# ---------------------------------------------------------------------------
# CHANGELOG entry insertion
# ---------------------------------------------------------------------------
_CHANGELOG_TEMPLATE = """## [{version}] - {date}

### Added
- (fill me in — what shipped that did not exist before?)

### Fixed
- (fill me in — what regressions did this release close?)

### Changed
- (fill me in — what user-visible behavior changed?)

### Coverage
- pytest (feral-core): TODO collected, TODO passed, TODO skipped.
- vitest (feral-client-v2): TODO passed.

"""


def _insert_changelog_entry(new_version: str, *, date: str) -> bool:
    """Insert a templated entry under ``## [Unreleased]``.

    Returns True if CHANGELOG was modified, False if the entry for this
    version already exists.
    """
    if not CHANGELOG.exists():
        print(f"  ⚠ {CHANGELOG} not present — skipping CHANGELOG entry")
        return False
    text = CHANGELOG.read_text(encoding="utf-8")

    if f"## [{new_version}]" in text:
        print(
            f"  · CHANGELOG already has a [{new_version}] entry — skipping"
        )
        return False

    target = "## [Unreleased]\n"
    if target not in text:
        print(
            "  ⚠ CHANGELOG.md has no '## [Unreleased]' section. Add one "
            "above the latest released entry, then re-run."
        )
        return False

    entry = _CHANGELOG_TEMPLATE.format(version=new_version, date=date)
    new_text = text.replace(target, target + "\n" + entry, 1)
    CHANGELOG.write_text(new_text, encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------
def _write_canonical_version(new_version: str) -> None:
    """Rewrite feral-core/pyproject.toml's [project] version literal."""
    text = PYPROJECT.read_text(encoding="utf-8")
    new_text, n = re.subn(
        r'(?m)^version = "[^"]+"',
        f'version = "{new_version}"',
        text,
        count=1,
    )
    if n != 1:
        raise SystemExit(
            "✗ failed to rewrite feral-core/pyproject.toml — no match"
        )
    PYPROJECT.write_text(new_text, encoding="utf-8")
    print(f"  → wrote feral-core/pyproject.toml version = {new_version}")


def _run_sync_versions() -> None:
    _run(
        [sys.executable, str(SYNC_VERSIONS), "--write", "--no-metadata"],
        cwd=ASOS_ROOT,
    )


def _sync_webui_v2_bundle() -> None:
    """Rebuild feral-client-v2 and sync artifacts into webui_v2.

    This keeps the bundled wheel assets aligned with source changes so
    PyPI releases cannot ship a stale Settings/Setup contract.
    """
    if not BUILD_WEBUI_V2.exists():
        raise SystemExit(f"✗ missing bundle script: {BUILD_WEBUI_V2}")
    if not CHECK_WEBUI_V2.exists():
        raise SystemExit(f"✗ missing bundle contract check: {CHECK_WEBUI_V2}")
    _run(["bash", str(BUILD_WEBUI_V2)], cwd=ASOS_ROOT)
    _run([sys.executable, str(CHECK_WEBUI_V2)], cwd=ASOS_ROOT)


def _run_tests(*, pytest_args: list[str], vitest_args: list[str]) -> None:
    pytest_target = ASOS_ROOT / "feral-core"
    print("  → running feral-core pytest")
    _run(
        [sys.executable, "-m", "pytest", *pytest_args],
        cwd=pytest_target,
    )
    vitest_target = ASOS_ROOT / "feral-client-v2"
    if vitest_target.exists():
        npm = shutil.which("npm")
        if npm is None:
            print("  ⚠ npm not found on PATH — skipping vitest")
            return
        print("  → running feral-client-v2 vitest")
        _run(
            [npm, "run", "test", "--", *vitest_args],
            cwd=vitest_target,
        )


def _build_artifacts() -> None:
    pytest_target = ASOS_ROOT / "feral-core"
    print("  → building wheel + sdist for feral-core")
    _run(
        [sys.executable, "-m", "build", "."],
        cwd=pytest_target,
    )


def _git_commit_push(new_version: str, *, branch: str | None) -> str:
    branch = branch or f"release/{new_version}"
    print(f"  → committing release artifacts on branch {branch}")
    _run(["git", "checkout", "-B", branch], cwd=ASOS_ROOT)
    _run(["git", "add", "-A"], cwd=ASOS_ROOT)
    _run(
        [
            "git",
            "commit",
            "-m",
            f"release: v{new_version}",
        ],
        cwd=ASOS_ROOT,
    )
    _run(["git", "push", "-u", "origin", branch], cwd=ASOS_ROOT)
    return branch


def _open_pr(new_version: str, branch: str) -> None:
    gh = shutil.which("gh")
    if gh is None:
        print(
            "  ⚠ gh CLI not found on PATH — skipping PR creation. Open "
            f"the PR manually for branch {branch}."
        )
        return
    body = (
        "## What\n"
        f"Release v{new_version}.\n\n"
        "## Why\n"
        "Driven by `scripts/release.py`. Version literal updated in "
        "`feral-core/pyproject.toml`; `scripts/sync_versions.py --write` "
        "propagated to every other declared location.\n\n"
        "## Test evidence\n"
        "Paste the pytest + vitest summary lines from this branch.\n\n"
        "## Risk\n"
        "Release scope. Rollback by reverting the merge commit.\n\n"
        "## Owned paths edited\n"
        "- `feral-core/pyproject.toml`\n"
        "- every location declared in `scripts/sync_versions.py::"
        "VERSION_LOCATIONS`\n\n"
        "## Roadmap diff\n"
        "Update CHANGELOG.md with this run's test counts before merging.\n"
    )
    _run(
        [
            gh,
            "pr",
            "create",
            "--title",
            f"release: v{new_version}",
            "--body",
            body,
            "--label",
            "release-impact:behavior",
        ],
        cwd=ASOS_ROOT,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="release",
        description=(
            "FERAL release driver: bump version, sync every literal, "
            "stub a CHANGELOG entry, run tests, build artifacts, open "
            "a release PR."
        ),
    )
    parser.add_argument(
        "bump",
        help="major | minor | patch  (or an explicit YYYY.M.D override)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not run tests, build, commit, push, or open PR. "
        "Implies --skip-tests --skip-build --no-pr.",
    )
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="Skip pytest + vitest steps.",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Skip wheel/sdist build step.",
    )
    parser.add_argument(
        "--no-pr",
        action="store_true",
        help="Skip git commit / push / gh pr create.",
    )
    parser.add_argument(
        "--branch",
        help="Override the release branch name (default: release/X.Y.Z).",
    )
    parser.add_argument(
        "--date",
        help="Override the CHANGELOG date (default: today, YYYY-MM-DD).",
    )
    parser.add_argument(
        "--pytest-arg",
        action="append",
        default=[],
        help="Extra arg to forward to pytest (repeatable).",
    )
    parser.add_argument(
        "--vitest-arg",
        action="append",
        default=[],
        help="Extra arg to forward to vitest (repeatable).",
    )

    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.dry_run:
        args.skip_tests = True
        args.skip_build = True
        args.no_pr = True

    current = _read_current_version()
    new_version = _bump(current, args.bump)
    if not CALVER_RE.match(new_version):
        raise SystemExit(
            f"✗ computed new version {new_version!r} is not valid calver"
        )
    if new_version == current:
        raise SystemExit(
            f"✗ computed new version {new_version!r} equals current — "
            "no-op release"
        )

    from datetime import date as _date

    iso_today = args.date or _date.today().isoformat()

    print(f"\n  current version : {current}")
    print(f"  new version     : {new_version}")
    print(f"  changelog date  : {iso_today}\n")

    if args.dry_run:
        print(
            "[1/7] writing canonical version literal — DRY-RUN (no write)"
        )
        print("[2/7] propagating with sync_versions.py — DRY-RUN")
        print(
            "      (would run: scripts/sync_versions.py --write --no-metadata)"
        )
        print("[3/7] inserting CHANGELOG template entry — DRY-RUN")
        print(f"      (would insert ## [{new_version}] - {iso_today})")
        print("[4/7] rebuilding/syncing webui_v2 bundle — DRY-RUN")
        print("      (would run: scripts/build_webui_v2.sh + check_webui_v2_contract.py)")
    else:
        print("[1/7] writing canonical version literal")
        _write_canonical_version(new_version)

        print("\n[2/7] propagating with sync_versions.py")
        _run_sync_versions()

        print("\n[3/7] inserting CHANGELOG template entry")
        _insert_changelog_entry(new_version, date=iso_today)

        print("\n[4/7] rebuilding + validating bundled v2 web UI")
        _sync_webui_v2_bundle()

    if args.skip_tests:
        print("\n[5/7] tests SKIPPED (--skip-tests / --dry-run)")
    else:
        print("\n[5/7] running test suites")
        _run_tests(
            pytest_args=args.pytest_arg or [
                "-q",
                "--no-cov",
            ],
            vitest_args=args.vitest_arg or [
                "--run",
            ],
        )

    if args.skip_build:
        print("\n[6/7] artifact build SKIPPED (--skip-build / --dry-run)")
    else:
        print("\n[6/7] building artifacts")
        _build_artifacts()

    if args.no_pr:
        print(
            "\n[7/7] git commit / push / PR SKIPPED (--no-pr / --dry-run)"
        )
        print(
            "\n✓ release prepared. Review the diff with "
            "`git -C "
            f"{ASOS_ROOT} diff` then commit, push, and open a PR by hand."
        )
        return 0

    print("\n[7/7] committing, pushing, opening PR")
    branch = _git_commit_push(new_version, branch=args.branch)
    _open_pr(new_version, branch)
    print(f"\n✓ release v{new_version} opened on branch {branch}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
