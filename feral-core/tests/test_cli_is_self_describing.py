"""The CLI must describe itself, and that must be enforced, not remembered.

Omarchy's dispatcher scans a metadata header on each of its 430+ binaries
and builds routes, groups, help and examples from it, so a command that
ships without help is not a discipline failure, it is impossible.

FERAL builds its argparse tree by hand in `cli/main._main()`. Every
subcommand happens to carry help text today, all 34 top level and every
nested one, which is a credit to whoever wrote them. Nothing enforces
it: a new `sub.add_parser("thing")` with no `help=` is accepted by
argparse, prints as a blank line under `feral --help`, and no test
anywhere notices. That is the gap this closes.

The parser is built inline inside `_main()`, roughly 475 lines, so
extracting a `build_parser()` would be a large refactor of the entry
point for very little benefit. Instead this captures the real parser the
real CLI builds, by letting `_main()` run and intercepting the parse
call, then walks every depth of the tree. It therefore asserts against
what a user actually gets from `feral --help`, not against the source.
"""

from __future__ import annotations

import argparse
import sys

import pytest


def _capture_parser() -> argparse.ArgumentParser:
    """Return the parser `cli.main._main()` really builds.

    `_main()` short-circuits `--version` and `--help` before building the
    heavy tree, so the argv here has to be a normal subcommand. Both
    parse entry points are intercepted because the CLI uses
    `parse_known_args`, and the guard on `_subparsers` makes sure we grab
    the root parser rather than some nested one built along the way.
    """
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

    parser = captured.get("p")
    if parser is None:
        pytest.fail(
            "could not capture the CLI parser. If _main() stopped using "
            "argparse, this guard needs rewriting rather than deleting: "
            "the contract it protects is that every subcommand is "
            "self-describing."
        )
    return parser


def _walk(parser, path=()):
    """Every (path, help) pair at every depth of the subcommand tree."""
    rows = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            helps = {c.dest: c.help for c in action._choices_actions}
            for name, sub in action.choices.items():
                rows.append((path + (name,), helps.get(name)))
                rows += _walk(sub, path + (name,))
    return rows


@pytest.fixture(scope="module")
def commands():
    return _walk(_capture_parser())


class TestEverySubcommandDescribesItself:
    def test_the_tree_is_actually_there(self, commands):
        """A guard that captured an empty parser would pass vacuously."""
        assert len(commands) >= 34, (
            f"only found {len(commands)} subcommands; the capture is "
            "probably grabbing the wrong parser"
        )

    def test_every_subcommand_at_every_depth_has_help(self, commands):
        missing = [" ".join(p) for p, h in commands if not (h or "").strip()]
        assert not missing, (
            "these subcommands render as a blank line under `feral --help`, "
            "so a user cannot tell what they do: " + ", ".join(missing)
        )

    def test_help_is_a_sentence_not_a_placeholder(self, commands):
        """`help="TODO"` satisfies argparse and helps nobody."""
        placeholders = {"todo", "tbd", "fixme", "xxx", "n/a", "-", "..."}
        bad = [
            f"{' '.join(p)} -> {h!r}"
            for p, h in commands
            if (h or "").strip().lower() in placeholders
            or len((h or "").strip()) < 8
        ]
        assert not bad, "help text that says nothing: " + "; ".join(bad)

    def test_nested_commands_are_covered_too(self, commands):
        """The tree is two deep in places; the shallow check would miss it."""
        nested = [p for p, _ in commands if len(p) > 1]
        assert nested, "expected nested subcommands such as `key add`"
        # And every one of them is included in the help assertion above.
        assert all(len(p) <= 3 for p, _ in commands), (
            "a third level appeared; confirm the help walk still reaches it"
        )
