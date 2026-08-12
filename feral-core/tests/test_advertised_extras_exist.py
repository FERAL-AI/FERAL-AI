"""F-11 — install commands printed to users must name extras that exist.

``pip install 'feral-ai[macos-extras]'`` is printed by ``feral doctor``. There
is no ``macos-extras`` extra, and pip does not treat an unknown extra as an
error: it prints a warning to stderr and installs the base package. So the user
runs the command, sees no failure, and nothing they were told to install gets
installed::

    $ python scripts/check_extras_installed.py feral-ai macos-extras
    ✗ extras check failed for feral-ai:
        extra 'macos-extras' is not declared by the installed distribution

The audit cites one site. A sweep of the tree finds **three sites naming two
nonexistent extras**:

    feral-core/cli/main.py:2675          feral-ai[macos-extras]
    feral-core/cli/app_commands.py:274   feral-ai[cli]
    feral-core/cli/app_commands.py:419   feral-ai[cli]

``[cli]`` is wrong twice over. It does not exist, and the dependency it claims
to provide is ``httpx``, which is a base runtime dependency
(``pyproject.toml:42``). A user whose ``import httpx`` fails has a broken
install, not a missing extra, and no `pip install` of any extra will fix it.

This test is the class guard, not a check of those three lines: any future
install hint naming an extra that is not declared fails here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


tomllib = pytest.importorskip("tomllib")

FERAL_CORE = Path(__file__).resolve().parents[1]
REPO_ROOT = FERAL_CORE.parent
PYPROJECT = FERAL_CORE / "pyproject.toml"

EXTRA_PATTERN = re.compile(r"feral-ai\[([A-Za-z0-9_,\- ]+)\]")


def _is_comment(line: str) -> bool:
    """True for a whole-line comment in Python, shell or TOML.

    These files explain the defect in prose next to the fix ("was
    feral-ai[cli], which does not exist"), and a scan that reads its own
    rationale as a violation can never go green.
    """
    return line.lstrip().startswith("#")

# Roots whose text is read by a user as an instruction to run.
SEARCH_ROOTS = [
    (FERAL_CORE, (".py",)),
    (REPO_ROOT / "scripts", (".sh",)),
    (REPO_ROOT / "docs" / "site", (".md", ".mdx")),
]

EXCLUDED_PARTS = ("build", "dist", "node_modules", ".venv", "tests")


def _declared_extras() -> set[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return set(data["project"].get("optional-dependencies", {}))


def _walk(root: Path, suffixes: tuple[str, ...]):
    """Yield matching files, pruning excluded directories as we descend.

    `rglob` would still walk `feral-core/build/`, which is a complete duplicate
    of the source tree (trap 1 in CLAUDE.md) and made this test take 25s.
    """
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name not in EXCLUDED_PARTS and not entry.name.startswith("."):
                    stack.append(entry)
            elif entry.suffix in suffixes:
                yield entry


def _advertised() -> dict[str, list[str]]:
    """Every `feral-ai[...]` occurrence, mapped extra name -> where."""
    found: dict[str, list[str]] = {}
    for root, suffixes in SEARCH_ROOTS:
        if not root.exists():
            continue
        for path in _walk(root, suffixes):
            text = path.read_text(encoding="utf-8", errors="ignore")
            for line_number, line in enumerate(text.splitlines(), start=1):
                if _is_comment(line):
                    continue
                for match in EXTRA_PATTERN.finditer(line):
                    for name in match.group(1).split(","):
                        name = name.strip()
                        if not name:
                            continue
                        found.setdefault(name, []).append(
                            f"{path.relative_to(REPO_ROOT)}:{line_number}"
                        )
    return found


def test_the_sweep_finds_something():
    """A regex that matches nothing would make the test below vacuous."""
    advertised = _advertised()
    assert len(advertised) >= 5, (
        f"only found {sorted(advertised)}; the scan roots or the pattern are "
        f"wrong, and a green result here would mean nothing"
    )


def test_every_advertised_extra_is_declared():
    declared = _declared_extras()
    advertised = _advertised()
    undeclared = {
        name: sites for name, sites in advertised.items() if name not in declared
    }
    assert not undeclared, (
        "these install commands are printed to users and install nothing, "
        "because pip warns on an unknown extra instead of failing:\n  "
        + "\n  ".join(
            f"[{name}] at {', '.join(sites)}" for name, sites in sorted(undeclared.items())
        )
    )


def test_no_extra_is_advertised_for_a_base_dependency():
    """An extra cannot fix a missing base dependency.

    `cli/app_commands.py` told users to install an extra when `import httpx`
    failed. httpx is in `dependencies`, so that import failing means the
    install is broken; no extra installs it and the advice sent people in a
    circle.
    """
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    base_names = {
        re.split(r"[<>=!~;\[ ]", requirement, maxsplit=1)[0].strip().lower()
        for requirement in data["project"]["dependencies"]
    }
    assert "httpx" in base_names, (
        "httpx is no longer a base dependency; re-derive what "
        "cli/app_commands.py should tell users"
    )

    offenders = []
    for path in (FERAL_CORE / "cli").rglob("*.py"):
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1
        ):
            if _is_comment(line) or not EXTRA_PATTERN.search(line):
                continue
            for name in base_names:
                # Only flag when the same line blames a base distribution.
                if re.search(rf"\b{re.escape(name)}\b", line):
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}:{line_number}: {line.strip()}"
                    )
    assert not offenders, (
        "these lines offer an extra as the remedy for a base dependency:\n  "
        + "\n  ".join(offenders)
    )
