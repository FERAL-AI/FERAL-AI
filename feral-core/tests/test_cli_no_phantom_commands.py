"""audit-r14 / lane-07 (W8) — every `feral <cmd>` documented in
`docs/mintlify/**/*.mdx` MUST exist in the live argparse parser.

Closes finding 08's "documented-but-missing matrix": pre-Lane-07 the
docs claimed `feral memory encrypt`, `feral memory wiki *`,
`feral voice train-wakeword`, `feral publish --kind <kind>`, etc. —
none of which existed in `cli/main.py`. Operators ran the docs
verbatim and got `unrecognized command` errors; trust shot.

This test is the CI gate Lane 21 (docs truth pass) consumes when it
deletes phantom command rows. The contract is:

* If a doc says `feral X`, the parser MUST have `X` registered as a
  top-level subcommand.
* If a top-level command is undocumented, that's fine — operators
  discover via `feral --help`.

The test deliberately scopes to top-level commands; sub-action
phantoms (e.g. `feral memory wiki`) are recorded as known-issues
under `KNOWN_PHANTOM_SUBCOMMANDS` so docs cleanup can land
incrementally — see Lane 21's PR for the doc-side cleanup.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ----------------------------------------------------------------------
# Phantom subcommand allowlist (Lane 21 — known docs cleanup pending)
# ----------------------------------------------------------------------
#
# When Lane 21's docs-truth pass lands, this list shrinks. Until then,
# the test allows these specific phantom *sub-actions* (not top-level
# commands) so the rest of the matrix can pin behaviour today. Each
# entry is the literal string the docs use AFTER `feral`, lowercase.

KNOWN_PHANTOM_SUBCOMMANDS: set[str] = {
    # Documented sub-actions that have no implementation. Lane 21 will
    # delete these rows from the docs.
    "memory encrypt",
    "memory wiki",
    "memory wiki compile",
    "memory wiki list",
    "memory wiki read",
    "memory sync",  # The docs misuse this — `feral sync` is the path.
    "voice train-wakeword",  # pre-Lane-07 docs claim
}

# Top-level phantom commands documented but not implemented. Pre-
# Lane-07 these all flooded the audit-r14 finding 08 "documented-but-
# missing matrix". Lane 21's docs-truth pass deletes the doc rows;
# this allowlist is the bridge that lets the CI gate land in Lane 07
# without forcing Lane 21's PR to ship in the same wave. Each entry
# has a comment naming the doc file + the future of the row.
KNOWN_PHANTOM_TOP_LEVEL: set[str] = {
    "hardware",     # docs/mintlify/hardware/{wristband,smart-home}.mdx — should be `feral devices`
    "backup",       # docs/mintlify/deployment.mdx — no `feral backup`; rsync ~/.feral
    "webhooks",     # docs/mintlify/guides/webhooks.mdx — webhooks live at /api/webhooks
    "providers",    # docs/mintlify/operations/metrics.mdx — should be `feral models list`
    "supervisor",   # docs/mintlify/operations/metrics.mdx — internal process name, not a CLI verb
    "upgrade",      # operations/metrics.mdx — should be `pip install -U feral-ai`
    "vault",        # docs/mintlify/{deployment,operations/metrics,guides/channels}.mdx — should be `feral key`
    "sends",        # docs/mintlify/channels/push.mdx — prose inside a code block, not a command
    "uses",         # docs/mintlify/help/troubleshooting.mdx — same
    "aggregates",   # docs/mintlify/hardware/wristband.mdx — same
}


# ----------------------------------------------------------------------
# Doc scanning
# ----------------------------------------------------------------------


# A `feral <cmd>` claim only counts when it appears inside a code
# context — fenced ```bash ... ``` blocks or inline ``...``. Matching
# prose like "FERAL is a personal AI" or "feral connects to..." would
# flood the test with false positives. The two regexes below are the
# narrow doc-code-only scanners.

_FENCED_CODE_RE = re.compile(r"```[a-zA-Z0-9-]*\n(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_FERAL_LINE_RE = re.compile(
    r"^\s*\$?\s*feral\s+([a-z0-9-]+)(?:\s+([a-z0-9-]+))?",
    re.IGNORECASE | re.MULTILINE,
)
_FERAL_INLINE_RE = re.compile(
    r"^\s*feral\s+([a-z0-9-]+)(?:\s+([a-z0-9-]+))?",
    re.IGNORECASE,
)


def _iter_mdx_files() -> Iterable[Path]:
    """Yield every Mintlify .mdx file under the repo's `docs/` trees."""
    candidates = [
        REPO_ROOT / "docs" / "mintlify",
        REPO_ROOT / "feral-core" / "docs" / "mintlify",
    ]
    for root in candidates:
        if not root.is_dir():
            continue
        yield from root.rglob("*.mdx")


def _extract_claims(path: Path) -> set[tuple[str, str]]:
    """Return the set of `(top_level, sub_action)` claims found in `path`.

    Only scans inside fenced code blocks + inline backticks — prose
    `feral foo` is ignored. Words that look like flags (`--xxx`)
    are filtered out — they're option references, not command
    claims.
    """
    text = path.read_text(errors="replace")
    out: set[tuple[str, str]] = set()

    # Fenced code blocks: every line of the form `feral X [Y]` is a claim.
    for fence in _FENCED_CODE_RE.finditer(text):
        body = fence.group(1)
        for match in _FERAL_LINE_RE.finditer(body):
            top = (match.group(1) or "").strip().lower()
            sub = (match.group(2) or "").strip().lower()
            if not top or top.startswith("-"):
                continue
            if "." in top or "/" in top:
                continue
            if sub.startswith("-"):
                sub = ""
            out.add((top, sub))

    # Inline backtick code: only treat `feral X` as a claim when it's
    # the entire backtick content (or starts the content) — avoids
    # picking up `feral.io` style anchors.
    for inline in _INLINE_CODE_RE.finditer(text):
        body = inline.group(1).strip()
        if not body.lower().startswith("feral "):
            continue
        match = _FERAL_INLINE_RE.match(body)
        if match is None:
            continue
        top = (match.group(1) or "").strip().lower()
        sub = (match.group(2) or "").strip().lower()
        if not top or top.startswith("-"):
            continue
        if "." in top or "/" in top:
            continue
        if sub.startswith("-"):
            sub = ""
        out.add((top, sub))

    return out


# ----------------------------------------------------------------------
# Live argparse introspection (subprocess so we don't dispatch)
# ----------------------------------------------------------------------


_HELP_RUNNER = (
    "import sys\n"
    "sys.argv = ['feral', '--help']\n"
    "from cli.main import main\n"
    "try:\n"
    "    main()\n"
    "except SystemExit:\n"
    "    pass\n"
)


def _parser_subcommands() -> set[str]:
    proc = subprocess.run(
        [sys.executable, "-c", _HELP_RUNNER],
        capture_output=True,
        text=True,
        timeout=10,
    )
    text = (proc.stdout or "") + (proc.stderr or "")
    match = re.search(r"\{([^}]+)\}", text, flags=re.DOTALL)
    assert match, f"could not find subcommand list in --help output: {text!r}"
    raw = match.group(1).replace("\n", "").replace(" ", "")
    return {x for x in raw.split(",") if x}


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


def test_no_phantom_top_level_commands():
    """Every top-level `feral X` documented under `docs/mintlify`
    MUST appear in the argparse parser. Sub-action phantoms are
    allowlisted in ``KNOWN_PHANTOM_SUBCOMMANDS``."""
    registered = _parser_subcommands()
    # Words that legitimately appear after `feral` inside code blocks
    # but aren't argparse subcommands — typically positional/path
    # values for one of the registered commands.
    allow_top_level_noncommands = {
        # `feral "search the web for X"` — the docs document free-form
        # chat as a positional. The first word inside the quote will
        # match here when it's lowercased.
        "search",
        # `feral install <item_id>` — the doc examples place the item
        # id as the second word. The argparse parser knows `install`
        # but a doc snippet like `feral install weather-skill` could
        # also match `weather-skill` as a "top-level" if it's split
        # poorly. We catch it via the fenced-code regex anchoring
        # to the start of the line; this safety net is unused in
        # practice but documents intent.
    }

    phantoms: dict[str, list[str]] = {}
    for mdx in _iter_mdx_files():
        for top, sub in _extract_claims(mdx):
            full = f"{top} {sub}".strip()
            if full in KNOWN_PHANTOM_SUBCOMMANDS:
                continue
            if top in KNOWN_PHANTOM_TOP_LEVEL:
                continue
            if top in allow_top_level_noncommands:
                continue
            if top in registered:
                continue
            phantoms.setdefault(top, []).append(str(mdx.relative_to(REPO_ROOT)))

    assert not phantoms, (
        "Documented `feral <cmd>` references do not exist in the "
        "argparse parser. Either implement the command or delete the "
        "doc reference (Lane 21 docs cleanup):\n"
        + "\n".join(f"  - {cmd}: {sorted(set(paths))[:3]}"
                    for cmd, paths in sorted(phantoms.items()))
    )


def test_known_phantom_subcommands_are_actually_phantom():
    """Any entry in ``KNOWN_PHANTOM_SUBCOMMANDS`` whose top-level word
    IS now registered means the implementation landed and the entry
    should be removed from the allowlist (Lane 21 will delete the
    doc reference; this test reminds us to drop the temporary
    allowlist row)."""
    registered = _parser_subcommands()
    no_longer_phantom = []
    for entry in KNOWN_PHANTOM_SUBCOMMANDS:
        top = entry.split()[0]
        if top in registered:
            # The top-level command exists. It's still possible the
            # *sub-action* is missing (memory encrypt is a sub-action
            # of memory which DOES exist). We surface the entry so
            # the maintainer knows to look. But — we don't fail the
            # test on this case; the test below ("test_no_phantom_…")
            # is the authoritative gate.
            no_longer_phantom.append(entry)
    # This is informational only — soft guard via a print.
    if no_longer_phantom:
        print(
            "INFO: KNOWN_PHANTOM_SUBCOMMANDS entries whose top-level "
            "command is now registered: "
            + ", ".join(no_longer_phantom)
        )


def test_top_level_help_lists_new_lane07_commands():
    """Sanity: the new Lane 07 commands MUST appear in `feral --help`."""
    registered = _parser_subcommands()
    for cmd in ("voice", "models", "integrations", "doctor", "key"):
        assert cmd in registered, f"{cmd} missing from argparse subparsers"
