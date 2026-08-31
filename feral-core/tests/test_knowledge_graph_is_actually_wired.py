"""Three subsystems read a knowledge-graph attribute that never existed.

``MemoryStore`` exposes the graph as ``kg`` -- a property over ``_kg``,
built during ``__init__`` (``memory/store.py:649``, ``:711-713``). Three
call sites asked for ``knowledge_graph`` instead, which exists on
neither the store nor ``BrainState``. Every one of them took the
``getattr`` default and got ``None``:

    api/server.py:5170   ambient conversations never extracted people
                         or relations into the graph
    api/state.py:599     every hardware device skill was constructed
                         with knowledge_graph=None
    api/state.py:1745    hardware_mesh.set_knowledge_graph() never ran,
                         so device announces never ingested

All three guard with ``if kg is not None``, so nothing raised, nothing
logged, and the features were simply absent. The comment above the third
site even says "The KG is built earlier in MemoryStore.__init__ but
exposed lazily on the store" -- the author knew where it lived and still
wrote the wrong name.

This is the trap CLAUDE.md documents from the other direction: a
``getattr`` default is a silent fallback, so a misspelled attribute is
indistinguishable from a legitimately absent one. The tests below assert
the wiring by behaviour rather than by reading the source, plus one
AST guard so a future rename cannot quietly reintroduce it.
"""

from __future__ import annotations

import ast
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory.store import MemoryStore  # noqa: E402


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as d:
        yield MemoryStore(db_path=f"{d}/m.db")


# ----------------------------------------------------------------------
# The store's own contract
# ----------------------------------------------------------------------

def test_the_graph_is_reachable_under_the_name_the_callers_use(store):
    assert store.kg is not None, "MemoryStore.kg is not populated"


def test_the_name_the_callers_used_to_ask_for_does_not_exist(store):
    """If this ever starts passing, the bug's premise changed.

    Someone adding a ``knowledge_graph`` alias would make the old call
    sites work again, which is fine -- but then this file is describing
    history rather than a live constraint and should be revisited.
    """
    assert not hasattr(store, "knowledge_graph")
    assert getattr(store, "knowledge_graph", None) is None


def test_brain_state_has_no_knowledge_graph_either():
    """The server site fell back to ``state`` as a second chance.

    It was not one: neither name resolves, so the ``or`` chain could
    only ever produce None.
    """
    from api.state import BrainState

    assert not hasattr(BrainState, "knowledge_graph")


# ----------------------------------------------------------------------
# The guard: no call site may read the dead name again
# ----------------------------------------------------------------------

_PRODUCTION_DIRS = ("api", "agents", "hardware", "memory", "skills", "perception")


def _getattr_names_read(path: Path) -> list[tuple[int, str]]:
    """Every ``getattr(x, "literal", ...)`` name read in a file."""
    try:
        tree = ast.parse(path.read_text())
    except (SyntaxError, UnicodeDecodeError):
        return []
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            found.append((node.lineno, node.args[1].value))
    return found


def _production_files() -> list[Path]:
    files: list[Path] = []
    for d in _PRODUCTION_DIRS:
        for p in (ROOT / d).rglob("*.py"):
            # ``build/`` is a stale duplicate of the source tree; see
            # CLAUDE.md trap 1.
            if "build/" in str(p.relative_to(ROOT)):
                continue
            files.append(p)
    return files


def test_no_production_code_reads_the_dead_attribute_name():
    """The rename, pinned.

    ``getattr(memory, "knowledge_graph", None)`` cannot fail loudly --
    that is the whole defect -- so the only place it can be caught is
    here.
    """
    offenders = [
        f"{p.relative_to(ROOT)}:{lineno}"
        for p in _production_files()
        for lineno, name in _getattr_names_read(p)
        if name == "knowledge_graph"
    ]
    assert not offenders, (
        "these read a 'knowledge_graph' attribute that exists on neither "
        "MemoryStore nor BrainState, so they silently receive None. The "
        "graph is exposed as 'kg':\n  " + "\n  ".join(offenders)
    )


def test_the_ambient_path_still_reads_the_graph():
    """A rename that deleted the read instead of fixing it would also
    make the guard above pass."""
    src = (ROOT / "api" / "server.py").read_text()
    assert 'getattr(memory, "kg", None)' in src


@pytest.mark.parametrize("count", [2])
def test_both_device_wiring_sites_read_the_graph(count):
    src = (ROOT / "api" / "state.py").read_text()
    assert src.count('getattr(self.memory, "kg", None)') == count
