#!/usr/bin/env python3
"""Fail CI if shipped documentation references internal audit artifacts.

Scope: anything under ``docs/mintlify/`` and ``docs/site/``, plus the
top-level operator-facing surface (``README.md``, ``CONTRIBUTING.md``).

What's forbidden in those trees:

- Internal audit identifiers: ``AUDIT-r14``, ``SCOREBOARD``, ``SCORECARD``,
  ``THESIS_SCENARIOS``, ``V1_0_RELEASE``, internal "finding-NN" pointers.
- Conductor / internal review docs: ``AGENT_PROMPTS``, ``OPENCLAW_LESSONS``,
  ``WAVE5_HARDENING_PROMPT``.
- Conductor workstream IDs in prose (regex ``\\bW\\d{1,2}(\\.\\d+)?\\b``)
  with a small allowlist of legitimate product names that happen to match
  (e.g. the W300 smart-glasses product reference inside hardware/*.mdx).

Run locally:
    python3 scripts/check_docs_no_internal_leakage.py

CI invocation lives in .github/workflows/ci.yml. Exit code is non-zero on
the first match; the offending file/line is printed so the contributor
can fix it before merging.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories / files to scan
SCAN_ROOTS = [
    REPO_ROOT / "docs" / "mintlify",
    REPO_ROOT / "docs" / "site",
]

# Every markdown file at the repository root, not a hand-maintained list.
#
# This used to name README.md and CONTRIBUTING.md only, which meant the
# checker could not see the two files that actually leaked. WORK-ORDER.md
# sat at the root of a public repository listing four P0 security holes
# with file:line citations under a line reading "Nothing here is started",
# long after all four were fixed. It passed every gate because nothing
# looked at it.
#
# A glob rather than a list, because the failure mode was somebody adding
# a root-level document and nobody remembering to register it. Files that
# are genuinely internal belong outside the repository or in .gitignore,
# not in an allowlist here.
#
# Exemptions are named here with a reason, and the DEFAULT is to scan.
# That inversion is the whole point: previously the default was to
# ignore and inclusions were listed, so a new root-level document was
# unscanned unless somebody remembered to add it. Now a new document is
# scanned unless somebody deliberately exempts it and says why.
_EXEMPT_ROOT_DOCS = {
    # A dated historical record. It cites internal workstream IDs in
    # release notes going back years, and rewriting shipped history to
    # satisfy a lint would destroy the forensic value the file exists
    # for. New entries should still avoid internal numbering.
    "CHANGELOG.md",
    # Instructions addressed to coding agents working in this tree. Its
    # job is to point at the internal audits and name the traps, so the
    # tokens this checker forbids in shipped docs are exactly what it is
    # supposed to contain.
    "CLAUDE.md",
    # A record of an audit whose work is done: 34 of 38 entries are
    # FIXED with the evidence and the commit, and CLAUDE.md sends
    # agents here on purpose. Its "P0" headings describe what the
    # priority WAS, not a live queue, which is the opposite of the
    # WORK-ORDER.md failure. The single still-open entry (F-04, the
    # embedding path blocking the event loop) is a performance defect,
    # not a security one.
    #
    # If that ratio ever inverts, this exemption should go rather than
    # the rule.
    "AUDIT-FIXES.md",
}

SCAN_FILES = sorted(
    p for p in REPO_ROOT.glob("*.md") if p.name not in _EXEMPT_ROOT_DOCS
)

# Forbidden substrings (case-insensitive). Adding a new internal artifact?
# Add its identifier here so it can never leak into shipped docs again.
FORBIDDEN_SUBSTRINGS = [
    "AUDIT-r14",
    "AUDIT-r13",
    "SCOREBOARD",
    "SCORECARD",
    "AGENT_PROMPTS_FOLLOWUPS",
    "AGENT_PROMPTS",
    "OPENCLAW_LESSONS",
    "WAVE5_HARDENING_PROMPT",
    "THESIS_SCENARIOS",
    "V1_0_RELEASE_HANDOFF",
    "wave3-followup-",
]

# Forbidden file references (relative paths that should never appear)
FORBIDDEN_PATHS = [
    "docs/SCORECARD.md",
    "docs/SCOREBOARD.md",
    "docs/findings/",
    "docs/critique.md",
]

# Regex for conductor workstream IDs in prose:  ..  (with optional .N).
# We tolerate these specific tokens that happen to match the pattern but are
# real product / spec references rather than internal workstream IDs.
WORKSTREAM_REGEX = re.compile(r"\bW\d{1,2}(?:\.\d+)?\b")
WORKSTREAM_ALLOWLIST = {
    "W300",   # smart-glasses product reference (hardware docs)
    "W3C",    # standards body
}
# Additional per-file exceptions (path glob → allowed tokens). Use sparingly.
WORKSTREAM_FILE_EXCEPTIONS: dict[str, set[str]] = {
    # No file-specific exceptions today.
}

INTERNAL_FINDING_REGEX = re.compile(r"\bfinding-\d{1,3}\b", re.IGNORECASE)

# A markdown heading that triages by severity: "## P0. Security",
# "### P0.1 A third-party skill can declare itself safe".
#
# This is the shape of an internal work order, and it is the rule that
# would actually have caught WORK-ORDER.md. Extending the file glob was
# necessary but not sufficient: that document contained none of the
# forbidden tokens above, so a token scan passed it while it sat in a
# public repository listing four P0 security holes with file:line
# citations, under a line reading "Nothing here is started", months
# after all four were fixed.
#
# The danger is not the word "P0". It is publishing a triage list of
# security findings, because such a list is read as current, is a map
# for anybody hostile, and goes stale silently the moment the work is
# done. Triage belongs somewhere private; what ships is the fix and the
# changelog entry.
SEVERITY_TRIAGE_HEADING_REGEX = re.compile(
    r"^#{1,6}\s+P[0-4](?:\.\d+)?[\s.:]", re.MULTILINE
)


def _iter_target_files() -> list[Path]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in {".md", ".mdx", ".json"}:
                files.append(path)
    for f in SCAN_FILES:
        if f.exists():
            files.append(f)
    return files


def _check_file(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    violations: list[str] = []

    # Substring checks (case-insensitive)
    lowered = text.lower()
    for token in FORBIDDEN_SUBSTRINGS:
        if token.lower() in lowered:
            # Surface the first line for actionable feedback
            for lineno, line in enumerate(text.splitlines(), start=1):
                if token.lower() in line.lower():
                    violations.append(
                        f"{path}:{lineno}: forbidden token '{token}' — internal audit reference"
                    )
                    break

    # Forbidden file path references
    for ref in FORBIDDEN_PATHS:
        if ref in text:
            for lineno, line in enumerate(text.splitlines(), start=1):
                if ref in line:
                    violations.append(
                        f"{path}:{lineno}: forbidden path reference '{ref}'"
                    )
                    break

    # Workstream ID regex
    file_exceptions = set()
    for pattern, allowed in WORKSTREAM_FILE_EXCEPTIONS.items():
        if path.match(pattern):
            file_exceptions |= allowed
    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in WORKSTREAM_REGEX.findall(line):
            if match in WORKSTREAM_ALLOWLIST or match in file_exceptions:
                continue
            violations.append(
                f"{path}:{lineno}: conductor workstream ID '{match}' in prose — "
                "rewrite to drop internal numbering"
            )
            break  # one per line is enough

    # Internal finding numbering
    for lineno, line in enumerate(text.splitlines(), start=1):
        if INTERNAL_FINDING_REGEX.search(line):
            violations.append(
                f"{path}:{lineno}: internal 'finding-NN' reference — rewrite for operators"
            )
            break

    # Severity-triage headings: the shape of an internal work order.
    for lineno, line in enumerate(text.splitlines(), start=1):
        if SEVERITY_TRIAGE_HEADING_REGEX.match(line):
            violations.append(
                f"{path}:{lineno}: severity-triage heading {line.strip()!r} — a "
                "published triage list reads as current, maps the gaps for an "
                "attacker, and goes stale the moment the work lands. Keep triage "
                "private; ship the fix and a changelog entry."
            )
            break

    return violations


def main() -> int:
    files = _iter_target_files()
    if not files:
        print("no doc files found to scan", file=sys.stderr)
        return 0

    all_violations: list[str] = []
    for path in files:
        all_violations.extend(_check_file(path))

    if all_violations:
        print(
            "Internal-audit artifacts leaked into shipped docs. "
            "Fix the lines below before merging.",
            file=sys.stderr,
        )
        for line in all_violations:
            print(f"  {line}", file=sys.stderr)
        print(
            f"\n{len(all_violations)} violation(s). "
            "See scripts/check_docs_no_internal_leakage.py for the rule list.",
            file=sys.stderr,
        )
        return 1

    print(f"OK: {len(files)} doc file(s) scanned, no internal-audit leakage detected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
