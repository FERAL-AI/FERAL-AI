"""A test double must not answer to arguments the real class refuses.

This is the generalisation of AUDIT-FIXES F-01. Scheduled federated sync
called ``engine.sync_with_peer(peer_id, passphrase=...)`` for roughly 40
releases. The real method never had a ``passphrase`` parameter, so every
scheduled sync raised TypeError in production. The suite stayed green the
whole time because ``_StubEngine`` in ``tests/test_sync_scheduler.py``
declared the parameter the real engine lacked.

Nothing compared the double to the thing it doubles. That is trap 3 in
CLAUDE.md stated precisely: a green suite is not evidence a call site
works, because the suite may be exercising a signature that exists
nowhere in production.

There are 109 double classes across 73 test files here, so checking them
by hand is not a strategy. This walks them mechanically.

WHAT IS CHECKED, AND WHAT DELIBERATELY IS NOT

Only methods, never ``__init__``. A double legitimately takes whatever
constructor arguments its test finds convenient, because the test
constructs it. Production never does. Flagging those would produce noise
that trains people to skip this test, which is how the last mechanism
failed.

Only parameters the double accepts and the real class does not. The
reverse is fine: a double may implement a subset, since a test only
exercises the parts it needs.

Doubles are matched to real classes by stripping the Stub/Fake/Mock/
Dummy/Recording/Noop prefix and looking for a class of the remaining name
outside ``tests/``. That is a heuristic, so an unmatched double is
skipped rather than failed. It found four real drifts on first run:

    FakeVisionBuffer.push(payload)      vs VisionBuffer.push(node_id, frame)
    _FakeChannelManager.get_channel(name) vs ChannelManager.get_channel(channel_type)
    _FakeOrchestrator.handle_command(prompt) vs Orchestrator.handle_command(text)
    _FakeSkillExecutor.execute(name)    vs SkillExecutor.execute(tool_name)

None was live: every production caller passes positionally or already
uses the correct keyword, so all four were latent rather than broken. F-01
was latent too, right up until the day somebody wrote the keyword.
"""

from __future__ import annotations

import ast
import collections
import pathlib
import re

FERAL_CORE = pathlib.Path(__file__).resolve().parent.parent

_DOUBLE_PREFIX = re.compile(r"^_?(Stub|Fake|Mock|Dummy|Recording|Noop)")


def _iter_class_defs(root: pathlib.Path):
    for path in root.rglob("*.py"):
        text = str(path)
        # build/lib is a complete duplicate of the source tree. See trap 1
        # in CLAUDE.md: including it does not merely double-count, it makes
        # every lookup ambiguous.
        if "/build/" in text or "/dist/" in text or "/node_modules/" in text:
            continue
        try:
            tree = ast.parse(path.read_text(errors="ignore"))
        except (SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                yield path, node


def _methods(node: ast.ClassDef) -> dict:
    return {
        m.name: m
        for m in node.body
        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _accepted_names(fn) -> tuple[set[str], bool]:
    args = fn.args
    positional = [a.arg for a in args.posonlyargs + args.args if a.arg != "self"]
    keyword_only = [a.arg for a in args.kwonlyargs]
    return set(positional) | set(keyword_only), bool(args.kwarg)


def _collect():
    real: dict[str, list] = collections.defaultdict(list)
    doubles: list = []
    for path, node in _iter_class_defs(FERAL_CORE):
        in_tests = "/tests/" in str(path) or str(path.parent).endswith("/tests")
        if in_tests:
            if _DOUBLE_PREFIX.match(node.name):
                doubles.append((path, node))
        else:
            real[node.name].append((path, node))
    return real, doubles


def test_no_double_accepts_arguments_its_real_class_refuses():
    real, doubles = _collect()
    assert doubles, "found no test doubles; the scan is broken, not the code"

    problems: list[str] = []
    compared = 0

    for dpath, dnode in doubles:
        base = _DOUBLE_PREFIX.sub("", dnode.name)
        candidates = [rc for rc in real.get(base, [])]
        if not candidates:
            continue
        rpath, rnode = candidates[0]
        real_methods = _methods(rnode)

        for mname, dfn in _methods(dnode).items():
            # Constructors are exempt on purpose; see the module docstring.
            if mname == "__init__" or mname not in real_methods:
                continue
            compared += 1
            dnames, _ = _accepted_names(dfn)
            rnames, real_takes_kwargs = _accepted_names(real_methods[mname])
            if real_takes_kwargs:
                continue
            extra = dnames - rnames
            if extra:
                problems.append(
                    f"{dpath.relative_to(FERAL_CORE)}::{dnode.name}.{mname} "
                    f"accepts {sorted(extra)}, which {base}.{mname} "
                    f"({rpath.relative_to(FERAL_CORE)}) does not. A test that "
                    f"passes those by keyword proves nothing about production."
                )

    assert compared > 0, "matched no double methods to real ones; heuristic broke"
    assert not problems, "test double signatures have drifted:\n  " + "\n  ".join(problems)


def test_the_f01_stub_still_matches_its_engine():
    """The original instance, pinned by name so it cannot silently return.

    Kept separate from the sweep above because this specific pair is the
    one that cost 40 releases, and it should fail with its own name in the
    output rather than as one line in a list.
    """
    import inspect

    from memory.sync import SyncEngine

    from tests.test_sync_scheduler import _StubEngine

    real = set(inspect.signature(SyncEngine.sync_with_peer).parameters)
    stub = set(inspect.signature(_StubEngine.sync_with_peer).parameters)
    assert not (stub - real), f"_StubEngine drifted again: {sorted(stub - real)}"
