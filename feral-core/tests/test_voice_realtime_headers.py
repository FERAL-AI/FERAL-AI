"""Regression test pinning the websockets header-kwarg contract.

The brain calls the **legacy** ``websockets.connect`` entrypoint, which
in websockets 13.x is exposed as ``websockets.legacy.client.Connect``.
That entrypoint accepts ``extra_headers``. The newer
``websockets.asyncio.client.connect`` renamed the same kwarg to
``additional_headers``.

If a future refactor swaps in ``additional_headers``, the legacy
``create_connection`` rejects the unknown kwarg with
``TypeError: create_connection() got an unexpected keyword argument
'additional_headers'`` and every voice provider silently fails to
connect (only surfacing as ``Voice session failed to connect`` warnings
in the brain log). This test catches the regression at unit-test time
instead of at real-hardware smoke time.

Pinned by the v2026.5.17 voice fix; see ``voice/realtime_proxy.py``
``OpenAIRealtimeProxy.connect`` and the parallel call sites in
``voice/gemini_realtime.py`` and ``voice/stt_providers/deepgram.py``.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest


def test_websockets_has_a_usable_connect_entrypoint() -> None:
    """Brain code uses ``websockets.asyncio.client.connect`` first
    (always has ``additional_headers``); falls back to legacy
    ``websockets.connect`` (``extra_headers``) if the asyncio client
    isn't available. At least one of the two MUST exist on the
    installed websockets version, otherwise voice can't open at all.
    Pinned by ``voice/realtime_proxy.py`` `_connect_with_retry`.
    """
    has_asyncio_client = False
    try:
        from websockets.asyncio.client import connect as _asyncio_connect  # noqa: F401
        has_asyncio_client = True
    except ImportError:
        pass

    has_legacy = False
    try:
        import websockets
        sig = inspect.signature(websockets.connect)
        if "extra_headers" in sig.parameters:
            has_legacy = True
    except Exception:
        pass

    assert has_asyncio_client or has_legacy, (
        "Neither websockets.asyncio.client.connect (additional_headers) "
        "nor websockets.connect (extra_headers) is available — the brain's "
        "voice path can't open any realtime session. Pin a usable "
        "websockets version in feral-core/pyproject.toml."
    )


def test_voice_modules_use_cross_version_connect_pattern() -> None:
    """Every voice module that opens a websocket MUST go through the
    cross-version helper (`_connect_with_retry`) OR contain its own
    `from websockets.asyncio.client import connect` fallback. Bare
    `websockets.connect(url, ...)` calls without a try/except for the
    asyncio import are forbidden — they break on websockets 14.x where
    the legacy entrypoint was removed.
    """
    from pathlib import Path

    voice_root = Path(__file__).resolve().parent.parent / "voice"
    bare_calls: list[str] = []
    for py in voice_root.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        # If the module contains the asyncio-client try/except, it's safe.
        if "from websockets.asyncio.client import connect" in text:
            continue
        # Otherwise scan for direct `websockets.connect(` invocations.
        for lineno, line in enumerate(text.splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            if "websockets.connect(" in line:
                bare_calls.append(f"{py}:{lineno}: {line.rstrip()}")
    assert not bare_calls, (
        "Voice modules call `websockets.connect(...)` directly without a "
        "websockets.asyncio.client fallback. Routes break on websockets "
        "14.x. Use the `_connect_with_retry` helper in realtime_proxy / "
        "gemini_realtime, or copy the try/except shape into your module. "
        "Hits:\n  " + "\n  ".join(bare_calls)
    )


def test_realtime_proxy_connect_attempts_actually_dispatch() -> None:
    """Smoke-test that the proxy hits the patched connect entrypoint.

    We don't talk to OpenAI — we monkey-patch the connect function the
    proxy actually uses (asyncio client first, legacy fallback) to
    capture kwargs and raise. The point is to confirm the call site
    survives kwarg validation across `websockets` 13.x AND 14.x+.
    """
    captured: dict[str, object] = {}

    async def fake_connect(uri, **kwargs):  # type: ignore[no-untyped-def]
        captured["uri"] = uri
        captured["kwargs"] = kwargs
        raise RuntimeError("dial halted (test)")

    # Patch BOTH possible entry points so the test works regardless of
    # which one is installed.
    #
    # Every original is captured BEFORE anything is patched, and that
    # order is the whole point. ``websockets`` resolves ``connect``
    # lazily through a module __getattr__ that forwards to
    # ``websockets.asyncio.client.connect`` and does not cache the result
    # in its __dict__. Capturing interleaved with patching, which is what
    # this did, meant the second capture read ``websockets.connect``
    # while the asyncio client was already faked, so it recorded the FAKE
    # as the original and the restore wrote the fake back permanently.
    #
    # The damage was not local. ``memory/sync.py`` dials peers through
    # ``websockets.connect``, so every sync test that happened to run
    # after this one in a randomised order failed with "dial halted
    # (test)", the sentinel raised below. That was 11 of the 28 failures
    # in a full-suite run, all of them passing in isolation, which is the
    # signature that sends people looking for a product bug that is not
    # there.
    targets = []
    try:
        from websockets.asyncio import client as _asyncio_client_mod
        targets.append(("websockets.asyncio.client", _asyncio_client_mod))
    except ImportError:
        pass
    try:
        import websockets as _ws_mod
        targets.append(("websockets", _ws_mod))
    except ImportError:
        pass

    patched = [(name, mod, mod.connect) for name, mod in targets]
    for _, mod, _original in patched:
        mod.connect = fake_connect  # type: ignore[assignment]

    try:
        from voice.realtime_proxy import RealtimeSession

        session = RealtimeSession(
            session_id="test-session",
            node_id="test-node",
            api_key="sk-test",
        )

        async def _run() -> None:
            await session.connect()

        asyncio.run(_run())
    finally:
        for _, mod, original in patched:
            mod.connect = original  # type: ignore[assignment]

    assert captured.get("uri", "").startswith("wss://"), captured
    # Either kwarg name is acceptable — the helper translates as
    # needed. What MUST be present is the Authorization header.
    kwargs = captured["kwargs"]
    headers = kwargs.get("additional_headers") or kwargs.get("extra_headers") or {}
    assert "Authorization" in headers, (
        f"connect() invoked without an Authorization header. "
        f"kwargs={list(kwargs)}"
    )
