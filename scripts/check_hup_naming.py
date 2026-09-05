#!/usr/bin/env python3
"""CI linter: HUP has one expansion, and it is the one the spec uses.

``feral-nodes/HUP_SPEC.md`` is normative and titles the protocol
**Hardware Unification Protocol**. Both shipped node SDKs describe
themselves the same way in their package metadata, which is what a
developer sees on PyPI and npm.

The published documentation site had drifted to two other expansions:

* "Hardware Unified Protocol" in five pages under ``docs/mintlify/``
* "Hardware Use Protocol" in three more

All eight arrived with the Mintlify import (commit 2ba6d8360, "add
Mintlify docs site (33 pages)") and were never reconciled against the
spec. One of them sat in a table row asserting that HUP is "unified
across brain, SDKs, and daemon manifests. CI-gated." while using a name
no other artifact used, which is the shape of problem this file exists
to stop: a claim of consistency that nothing checks.

It was reported by somebody reading the public repo, not caught here.
A reader who opens two pages and sees two names for the same protocol
has learned something true about how carefully the rest is maintained.

CHANGELOG.md is exempt on the same reasoning the sibling linter uses for
its own forbidden literal: past entries are a record of what was
written at the time and are not rewritten.
"""

from __future__ import annotations

from pathlib import Path

CANONICAL = "Hardware Unification Protocol"

FORBIDDEN = (
    "Hardware Unified Protocol",
    "Hardware Use Protocol",
)

REPO_ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "dist", "build", "coverage", "htmlcov",
    "_site", ".next",
}

# Historical records, and this file, which must name what it blocks.
EXEMPT_FILES = {
    "CHANGELOG.md",
    "scripts/check_hup_naming.py",
    "feral-core/tests/test_hup_naming.py",
}

TEXT_SUFFIXES = {
    ".md", ".mdx", ".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".yml",
    ".yaml", ".toml", ".txt", ".html", ".swift", ".kt", ".rs",
}


def violations() -> list[str]:
    hits: list[str] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in EXEMPT_FILES:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(lines, start=1):
            for wrong in FORBIDDEN:
                if wrong in line:
                    hits.append(f"{rel}:{number}: {wrong!r} (use {CANONICAL!r})")
    return hits


def main() -> int:
    hits = violations()
    if not hits:
        print(f"OK: HUP is spelled {CANONICAL!r} everywhere it is expanded.")
        return 0
    print(f"HUP must be expanded as {CANONICAL!r}, per feral-nodes/HUP_SPEC.md.")
    print("A reader who sees two names for one protocol learns something")
    print("true about how carefully the rest is maintained.\n")
    for hit in hits:
        print(f"  {hit}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
