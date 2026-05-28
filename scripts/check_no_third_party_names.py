#!/usr/bin/env python3
"""W24c — CI linter: forbid third-party project names in shipped artifacts.

Implements the enforcement half of the workspace rule
``.cursor/rules/no-third-party-project-names-in-deliverables.mdc``.

Scans the repository from ``ASOS/`` and, for every non-exempt file,
reports any occurrence of the forbidden literal ``openclaw``
(case-insensitive). Exits 1 with a per-hit report (``path:line:match``)
when anything is found; exits 0 on a clean repo.

Exempt locations (from the workspace rule):
* ``docs/OPENCLAW_LESSONS.md``
* ``docs/OPENCLAW_LESSONS_PROMPT.md``
* ``docs/AGENT_PROMPTS.md``
* ``docs/AGENT_PROMPTS_FOLLOWUPS.md``
* ``docs/critique.md``
* ``CHANGELOG.md`` (all existing entries are dated on or before the
  2026-04-25 2026.5.0 cutoff and stay intact for historical honesty;
  future post-cutoff entries MUST follow the rule — a separate release
  CI gate is tracked under ``docs/AGENT_PROMPTS_FOLLOWUPS.md``).
* Generated / machine caches: ``node_modules``, ``.git``, ``__pycache__``,
  ``.venv``, ``.pytest_cache``, ``.mypy_cache``, ``.ruff_cache``, ``dist``,
  ``build``, ``coverage``, ``htmlcov``, ``_site``.
* The rule file itself
  (``.cursor/rules/no-third-party-project-names-in-deliverables.mdc``).
* This linter + the workflow + the pytest sibling (they all carry the
  forbidden literal as the term being blocked).

Filename references to the exempt comparative-analysis docs
(``OPENCLAW_LESSONS.md`` / ``OPENCLAW_LESSONS_PROMPT.md``) are
allowlisted. The workspace rule's own "Replacement vocabulary" row 2
approves this form. The allowlist is evaluated per-match, so a sentence
that uses ``openclaw`` as a bare word elsewhere in the same line still
fails.

Usage::

    python3 scripts/check_no_third_party_names.py
    python3 scripts/check_no_third_party_names.py --list-forbidden-terms
    python3 scripts/check_no_third_party_names.py --root /path/to/repo
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Iterable, Iterator


FORBIDDEN_TERMS: tuple[str, ...] = (
    "openclaw",
)


EXEMPT_FILES: frozenset[str] = frozenset(
    {
        # CHANGELOG historical entries on/before 2026-04-25 keep their
        # original prose for honest history. The whole file is exempt
        # because the linter has no per-heading awareness; future-dated
        # entries follow the rule via code review.
        "CHANGELOG.md",
        # The rule itself + enforcement scaffolding — they carry the
        # term as data, not prose.
        ".cursor/rules/no-third-party-project-names-in-deliverables.mdc",
        "scripts/check_no_third_party_names.py",
        ".github/workflows/no-third-party-names-lint.yml",
        "feral-core/tests/test_no_third_party_names_literal.py",
        # Agent worktree scratchpads — never reach origin/main in
        # well-formed PRs, but belt-and-braces for the linter.
        "_PROPOSAL.md",
        # v2026.5.43: enforcement scaffolding from the docs-leakage
        # carve-out. Both files carry internal-doc identifiers as data
        # (a gitignore glob and an allowlist literal) so the same
        # carve-out this script already grants itself applies here.
        ".gitignore",
        "scripts/check_docs_no_internal_leakage.py",
    }
)


# Directories that are private-by-definition. Everything inside is
# exempt from the shipped-artifacts rule because the whole tree is
# gitignored and never published.
EXEMPT_DIR_PREFIXES: tuple[str, ...] = (
    # Conductor analysis docs, handoff notes, scratch — .internal/ is
    # in .gitignore and never ships to GitHub.
    ".internal/",
    # v2026.5.43: rolling audit dossier (findings, scoreboards, lane
    # reports, snapshots, handoffs). The whole tree is in .gitignore
    # and never reaches the public repo; locally-tracked walkers
    # would otherwise trip the linter on internal artifact names.
    "AUDIT-r14/",
)


EXEMPT_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
        "coverage",
        "htmlcov",
        "_site",
        ".next",
        ".turbo",
        ".parcel-cache",
    }
)


BINARY_SUFFIXES: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".ico",
        ".pdf",
        ".mp3",
        ".mp4",
        ".wav",
        ".ogg",
        ".webm",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".whl",
        ".dylib",
        ".so",
        ".class",
        ".jar",
        ".pyc",
        ".pyo",
        ".bin",
    }
)


ALLOWED_FILENAME_REFERENCES: tuple[str, ...] = (
    "OPENCLAW_LESSONS.md",
    "OPENCLAW_LESSONS_PROMPT.md",
)


_TERM_PATTERN = re.compile(
    "|".join(re.escape(t) for t in FORBIDDEN_TERMS),
    flags=re.IGNORECASE,
)


def _relpath(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path)


def _is_exempt_dir(dirname: str) -> bool:
    return dirname in EXEMPT_DIR_NAMES


def _is_exempt_file(rel: str) -> bool:
    if rel in EXEMPT_FILES:
        return True
    for prefix in EXEMPT_DIR_PREFIXES:
        if rel.startswith(prefix):
            return True
    suffix = Path(rel).suffix.lower()
    if suffix in BINARY_SUFFIXES:
        return True
    return False


def _iter_candidate_files(root: Path) -> Iterator[Path]:
    """Walk ``root`` yielding every file that is NOT inside an exempt
    directory and whose suffix is not in the binary-blocklist."""
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except (PermissionError, FileNotFoundError):
            continue
        for entry in entries:
            name = entry.name
            if entry.is_dir():
                if _is_exempt_dir(name):
                    continue
                stack.append(entry)
                continue
            if entry.is_file():
                yield entry


def _line_is_filename_reference_only(line: str, match_start: int, match_end: int) -> bool:
    """Return True if the matched span is entirely inside one of the
    allowlisted filename references (e.g. ``OPENCLAW_LESSONS.md``).

    We expand the match span to the word/path boundary on each side
    (characters that look like they belong to a path/filename token)
    and check whether the expanded token is exactly one of the allowed
    filename strings.
    """
    # Characters that are valid inside a filename / path token.
    def is_tok(ch: str) -> bool:
        return ch.isalnum() or ch in "_.-"

    start = match_start
    while start > 0 and is_tok(line[start - 1]):
        start -= 1
    end = match_end
    while end < len(line) and is_tok(line[end]):
        end += 1
    token = line[start:end]
    # Compare case-insensitively (the filenames ship as uppercase).
    return token.upper() in {s.upper() for s in ALLOWED_FILENAME_REFERENCES}


def scan_file(path: Path, rel: str) -> list[tuple[str, int, str, str]]:
    """Scan one file and return ``(rel, lineno, matched_term, line)`` per hit."""
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    hits: list[tuple[str, int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for m in _TERM_PATTERN.finditer(line):
            if _line_is_filename_reference_only(line, m.start(), m.end()):
                continue
            hits.append((rel, lineno, m.group(0), line.rstrip()))
    return hits


def scan_repo(root: Path) -> list[tuple[str, int, str, str]]:
    """Scan the whole repo and return the full hit list."""
    root = root.resolve()
    all_hits: list[tuple[str, int, str, str]] = []
    for candidate in _iter_candidate_files(root):
        rel = _relpath(candidate, root)
        if _is_exempt_file(rel):
            continue
        all_hits.extend(scan_file(candidate, rel))
    return all_hits


def format_hits(hits: Iterable[tuple[str, int, str, str]]) -> str:
    lines = []
    for rel, lineno, term, content in hits:
        lines.append(f"{rel}:{lineno}: [{term}] {content}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Check shipped artifacts for forbidden third-party project "
            "names. Exits 1 if any non-exempt file mentions a forbidden "
            "term; exits 0 on a clean repo."
        ),
    )
    parser.add_argument(
        "--root",
        default=None,
        help="Repository root to scan (default: auto-detect from script location).",
    )
    parser.add_argument(
        "--list-forbidden-terms",
        action="store_true",
        help="Print the current forbidden-term list and exit 0.",
    )
    args = parser.parse_args(argv)

    if args.list_forbidden_terms:
        for term in FORBIDDEN_TERMS:
            print(term)
        return 0

    if args.root:
        root = Path(args.root).resolve()
    else:
        root = Path(__file__).resolve().parent.parent

    hits = scan_repo(root)
    if not hits:
        print(f"ok: no forbidden third-party names found under {root}")
        return 0

    print(
        f"FAIL: {len(hits)} forbidden third-party name mention(s) found "
        f"under {root}:",
        file=sys.stderr,
    )
    print(format_hits(hits), file=sys.stderr)
    print(
        "\nFix: rewrite the prose per the 'Replacement vocabulary' in "
        ".cursor/rules/no-third-party-project-names-in-deliverables.mdc",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
