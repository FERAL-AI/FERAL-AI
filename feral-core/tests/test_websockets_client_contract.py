"""The installed websockets must support how memory/sync.py dials a peer.

``_sync_once`` awaits the connect and then uses the result as a context
manager::

    ws = await asyncio.wait_for(websockets.connect(uri, ...), ...)
    ...
    async with ws:

That is only valid from websockets 14.0. Through 13.x, ``connect``
resolves to the legacy implementation and the await yields a
``WebSocketClientProtocol``, which has no ``__aenter__``, so every
brain-to-brain sync attempt fails with::

    'WebSocketClientProtocol' object does not support the asynchronous
    context manager protocol

feral-core declared ``websockets>=13.0,<16.0``, so a resolver was free to
pick a version the code cannot use. It happened: the maintainer's own
machine had 13.1, 24 sync tests failed there and passed in CI, and the
running brain had working chat with silently broken replication. Chat
never touches this path, which is why nothing else noticed.

These tests pin the contract rather than the version number, so they
keep meaning something if the library reorganises again. The declared
floor is checked too, because the contract holding on a developer's
machine says nothing about what a user's resolver will choose.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_awaiting_connect_yields_something_usable_as_a_context_manager():
    """The exact property memory/sync.py depends on."""
    from websockets.asyncio.client import ClientConnection

    assert hasattr(ClientConnection, "__aenter__"), (
        "the object `await websockets.connect(...)` returns must support "
        "`async with`, because _sync_once awaits first and enters second"
    )
    assert hasattr(ClientConnection, "__aexit__")


def test_connect_resolves_to_the_asyncio_implementation_not_the_legacy_one():
    """13.x routes `websockets.connect` at the legacy client.

    Asked of a FRESH interpreter, on purpose. ``websockets.connect`` is
    a module attribute, and tests legitimately patch it: for instance
    test_voice_realtime_headers swaps in a fake to prove the proxy
    dispatches through it, restoring it in a finally. Under
    pytest-randomly this test can run while such a swap is the current
    value, and reading it in-process then measures the test suite rather
    than the installed library.

    That is not hypothetical. In-process, this assertion failed in CI
    twice with two different wrong answers: once with "builtins" (the
    patched value was an async function, so calling it produced a
    coroutine) and once with "tests.test_voice_realtime_headers" (the
    fake itself). A subprocess cannot see any of that.

    Checked by module path rather than by version string: the version is
    a proxy, and this is the thing that actually differs. Measured
    identical on 13.1 (websockets.legacy.client) and on 14.0 and 15.0.1
    (websockets.asyncio.client).
    """
    import subprocess

    probe = (
        "import websockets;"
        "print(getattr(websockets.connect, '__module__', ''))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, f"could not import websockets: {out.stderr[:300]}"
    module = out.stdout.strip()

    assert "legacy" not in module, (
        f"websockets.connect resolves to {module}, the legacy implementation, "
        "whose awaited result is a WebSocketClientProtocol with no __aenter__"
    )
    assert module.startswith("websockets.asyncio"), (
        f"expected the asyncio client, got {module!r}"
    )


def test_the_legacy_protocol_really_lacks_the_method():
    """Guards the reasoning above, not the product.

    If a future websockets gives WebSocketClientProtocol an __aenter__,
    the floor could be relaxed, and this test failing is the signal to
    revisit rather than a defect. Skips when the legacy module is gone,
    which is itself fine.
    """
    try:
        from websockets.legacy.client import WebSocketClientProtocol
    except Exception:
        pytest.skip("legacy implementation removed from this websockets")
    assert not hasattr(WebSocketClientProtocol, "__aenter__"), (
        "the legacy protocol grew __aenter__; the >=14.0 floor can be revisited"
    )


def test_the_declared_floor_cannot_resolve_to_a_broken_version():
    """A green suite here proves the DEV box is fine, not the user's.

    The bug was a dependency range, so the range is what has to be
    asserted; a user whose resolver picked 13.x got a brain that chatted
    normally and could not replicate.
    """
    text = (ROOT / "pyproject.toml").read_text()
    match = re.search(r'"websockets>=(\d+)\.(\d+)', text)
    assert match, "feral-core no longer declares a websockets floor"
    major, minor = int(match.group(1)), int(match.group(2))
    assert (major, minor) >= (14, 0), (
        f"declared floor websockets>={major}.{minor} allows a resolver to "
        "install 13.x, where memory/sync.py cannot open a peer connection"
    )


def test_sync_still_uses_the_pattern_this_floor_exists_for():
    """If _sync_once stops awaiting-then-entering, revisit the floor.

    Otherwise this constraint outlives its reason and nobody remembers
    why the floor is where it is.
    """
    src = (ROOT / "memory" / "sync.py").read_text()
    assert "await asyncio.wait_for(" in src
    assert "websockets.connect(" in src
    assert "async with ws:" in src, (
        "memory/sync.py no longer enters the awaited connection; the "
        ">=14.0 floor may no longer be required"
    )
