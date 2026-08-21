"""The user manual must only document commands that exist.

Omarchy splits documentation three ways by audience: task procedure for
agents, system shape for contributors, and a manual for end users that
never mentions internals. FERAL had the middle tier (68 pages under
docs/mintlify: architecture, deployment, contributing, the HUP spec) and
nothing at all for the first-time user.

docs/manual/ is that tier. Prose rots faster than code, and a manual that
tells a new user to run a command that no longer exists is worse than no
manual, because they cannot tell whether they typed it wrong or the
software changed. So every `feral ...` invocation in those pages is
checked against the CLI's real parser.

The parser is captured the same way test_cli_is_self_describing.py does
it, for the same reason: it is built inline in _main().
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pytest

MANUAL = Path(__file__).resolve().parent.parent.parent / "docs" / "manual"


def _capture_parser():
    captured: dict = {}
    originals = {}
    for meth in ("parse_args", "parse_known_args"):
        originals[meth] = getattr(argparse.ArgumentParser, meth)

        def make(real=originals[meth]):
            def spy(self, *a, **k):
                if "p" not in captured and self._subparsers is not None:
                    captured["p"] = self
                    raise SystemExit(0)
                return real(self, *a, **k)
            return spy

        setattr(argparse.ArgumentParser, meth, make())
    argv = sys.argv
    try:
        sys.argv = ["feral", "doctor"]
        import cli.main as m
        try:
            m._main()
        except SystemExit:
            pass
    finally:
        sys.argv = argv
        for meth, real in originals.items():
            setattr(argparse.ArgumentParser, meth, real)
    return captured.get("p")


def _command_tree() -> dict:
    """{top: {nested, ...}} for every subcommand the CLI really has."""
    parser = _capture_parser()
    assert parser is not None, "could not capture the CLI parser"
    tree: dict = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                nested = set()
                for a2 in sub._actions:
                    if isinstance(a2, argparse._SubParsersAction):
                        nested |= set(a2.choices)
                tree[name] = nested
    return tree


def _documented_invocations() -> list[tuple[Path, str, list[str]]]:
    """Every `feral ...` line in a fenced block in the manual."""
    out = []
    for page in sorted(MANUAL.glob("*.md")):
        for block in re.findall(r"```(?:.*?)\n(.*?)```", page.read_text(), re.S):
            for line in block.splitlines():
                line = line.split("#", 1)[0].strip()
                if not line.startswith("feral "):
                    continue
                parts = [p for p in line.split() if not p.startswith("-")]
                out.append((page, line, parts[1:]))
    return out


pytestmark = pytest.mark.skipif(
    not MANUAL.is_dir(), reason="docs/manual is not present"
)


class TestTheManualDocumentsRealCommands:
    def test_the_manual_exists_and_has_pages(self):
        pages = list(MANUAL.glob("*.md"))
        assert len(pages) >= 5, f"only {len(pages)} manual pages found"
        assert (MANUAL / "README.md").is_file(), "the manual has no index"

    def test_it_actually_contains_commands_to_check(self):
        """A guard over an empty set passes vacuously."""
        assert len(_documented_invocations()) >= 10

    def test_every_documented_command_exists(self):
        tree = _command_tree()
        bad = []
        for page, line, parts in _documented_invocations():
            if not parts:
                continue
            top = parts[0]
            if top not in tree:
                bad.append(f"{page.name}: `{line}` -> no such command `{top}`")
                continue
            # Check the nested action too, when the command has any and the
            # manual named one.
            if len(parts) > 1 and tree[top]:
                nested = parts[1]
                # A path argument is not an action; only flag a bare word
                # that looks like a subcommand.
                if re.fullmatch(r"[a-z][a-z0-9-]*", nested) and nested not in tree[top]:
                    bad.append(
                        f"{page.name}: `{line}` -> `{top}` has no action "
                        f"`{nested}` (has: {', '.join(sorted(tree[top]))})"
                    )
        assert not bad, "the manual documents commands that do not exist:\n" + "\n".join(bad)

    def test_the_manual_stays_out_of_the_internals(self):
        """The audience split is the point; keep it enforceable.

        Omarchy's manual never explains how the system is built. If these
        words show up here, the content probably belongs in
        docs/mintlify/architecture.mdx instead.
        """
        forbidden = ["CRDT", "aiosqlite", "asyncio", "FastAPI", "pydantic", "argparse"]
        offenders = []
        for page in sorted(MANUAL.glob("*.md")):
            text = page.read_text()
            for word in forbidden:
                if re.search(rf"\b{re.escape(word)}\b", text):
                    offenders.append(f"{page.name} mentions {word}")
        assert not offenders, (
            "the user manual drifted into internals: " + "; ".join(offenders)
        )
