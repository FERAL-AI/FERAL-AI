#!/usr/bin/env python3
"""Assert that a named extra was actually installed, not just requested.

``pip install 'feral-ai[typo]'`` does not fail. pip warns on stderr and
installs the base package, so a job that requests an extra and then checks
nothing has proved nothing about the extra. That is F-11 in ``AUDIT-FIXES.md``
(``feral-ai[macos-extras]`` was printed to users for releases and installs
nothing), and it is why ``.github/workflows/install-smoke.yml`` runs this after
installing ``feral-ai[all]``.

For every extra named on the command line this checks two things:

1. The extra is declared by the installed distribution. An extra pip never
   heard of is the silent-no-op case.
2. Every requirement the extra pulls in, whose environment markers apply to
   this interpreter, is installed at a version the specifier accepts.

Usage::

    python scripts/check_extras_installed.py feral-ai all
    python scripts/check_extras_installed.py feral-ai all llm vec

Exit code is 0 when every named extra is fully present, 1 otherwise.
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import sys

from packaging.requirements import Requirement
from packaging.version import InvalidVersion, Version


def _requirements_for_extra(dist: md.Distribution, extra: str) -> list[Requirement]:
    """Requirements `dist` pulls in for `extra`, with markers applied.

    A Requires-Dist line belongs to an extra when its marker evaluates true
    with that extra in the environment and false without it. Testing both ways
    is what separates "only for this extra" from "always installed anyway", so
    a base dependency is not credited to the extra.
    """
    out: list[Requirement] = []
    for raw in dist.metadata.get_all("Requires-Dist") or []:
        req = Requirement(raw)
        if req.marker is None:
            continue
        if not req.marker.evaluate({"extra": extra}):
            continue
        if req.marker.evaluate({"extra": ""}):
            continue
        out.append(req)
    return out


def _check_extra(dist: md.Distribution, extra: str) -> list[str]:
    problems: list[str] = []

    declared = dist.metadata.get_all("Provides-Extra") or []
    if extra not in declared:
        return [
            f"extra {extra!r} is not declared by the installed distribution "
            f"(declared: {', '.join(sorted(declared)) or 'none'}). "
            f"pip installs nothing for an extra it does not recognise."
        ]

    requirements = _requirements_for_extra(dist, extra)
    if not requirements:
        return [
            f"extra {extra!r} is declared but pulls in no requirements that "
            f"apply to this interpreter, so installing it proves nothing"
        ]

    for req in requirements:
        try:
            installed = md.version(req.name)
        except md.PackageNotFoundError:
            problems.append(f"{extra}: {req.name} is not installed")
            continue
        if not req.specifier:
            continue
        try:
            parsed = Version(installed)
        except InvalidVersion:
            # A non-PEP-440 version cannot be compared. Say so rather than
            # letting an unparseable version read as a pass.
            problems.append(
                f"{extra}: {req.name}=={installed} is not a PEP 440 version"
            )
            continue
        if parsed not in req.specifier:
            problems.append(
                f"{extra}: {req.name}=={installed} does not satisfy "
                f"{req.specifier}"
            )

    if not problems:
        print(f"  · [{extra}] OK — {len(requirements)} requirement(s) installed")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("package", help="installed distribution name, e.g. feral-ai")
    parser.add_argument("extras", nargs="+", help="extra names to verify")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        dist = md.distribution(args.package)
    except md.PackageNotFoundError:
        print(
            f"✗ {args.package} is not installed in this interpreter",
            file=sys.stderr,
        )
        return 1

    problems: list[str] = []
    for extra in args.extras:
        problems.extend(_check_extra(dist, extra))

    if problems:
        print(
            f"✗ extras check failed for {args.package}:", file=sys.stderr
        )
        for problem in problems:
            print(f"    {problem}", file=sys.stderr)
        return 1

    print(f"✓ {args.package} extras present: {', '.join(args.extras)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
