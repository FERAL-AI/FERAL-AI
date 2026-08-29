"""Background click delivery for elements the AX tree cannot activate.

`macos_ax` targets semantically: the model reads a text tree and names
`[ax7] AXButton "Back"`, with no screenshot and no vision model. When the
element publishes an activating action that is the whole story, and
nothing touches the cursor.

The gap is elements that publish none. Today the only remaining option is
`_coordinate_click`, which posts a real mouse event and teleports the
operator's cursor, in a module whose premise is that it does not.

cua-driver fills exactly that hole. It is a coordinate driver, not a
semantic one: its `click` takes an absolute point and delivers it by
injecting into the target process, with no cursor movement and no focus
theft. So the two compose rather than compete, and the composition needs
no vision model, because the AX tree already carries each element's
bounds.

The trap is the handoff. `AXPosition` and `AXSize` come back in LOGICAL
POINTS. cua-driver's `click` takes coordinates in `get_desktop_state`
space, which is NATIVE RESOLUTION. On a 2x display those differ by
exactly the factor that put every VLM click at 57% of target earlier in
this codebase. That bug existed because two individually-correct
functions were composed and nothing tested the composition, so the
conversion here is tested directly.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from skills.impl.background_input import (  # noqa: E402
    BACKGROUND_CLICK_TOOL,
    background_click_available,
    logical_to_native,
)


# ── the coordinate conversion ──────────────────────────────────────

@pytest.mark.parametrize("logical,scale,expected", [
    ((100, 200), 1.0, (100, 200)),      # non-Retina: identity
    ((100, 200), 2.0, (200, 400)),      # Retina: AX points -> native pixels
    ((840, 525), 2.0, (1680, 1050)),
    ((0, 0), 2.0, (0, 0)),
])
def test_logical_points_convert_to_native_pixels(logical, scale, expected):
    """The AX tree speaks points; cua-driver's click speaks pixels."""
    assert logical_to_native(logical[0], logical[1], scale) == expected


def test_a_missing_or_absurd_scale_does_not_silently_move_the_click():
    """Better to click where we were told than somewhere invented.

    A scale of 0 or a negative one means the probe failed. Treating that
    as 1.0 degrades to the untranslated point, which is correct on a
    non-Retina display and merely wrong-by-a-known-factor on a Retina
    one. Multiplying by garbage would put the click anywhere.
    """
    for bad in (0.0, -2.0, None):
        assert logical_to_native(100, 200, bad) == (100, 200)


def test_the_conversion_is_the_one_that_bit_us_before():
    """Pin the direction. Inverting it is the whole failure mode.

    AX reports a button at logical (1662, 500) on a 2x display. Its
    native-pixel position is (3324, 1000). Dividing instead of
    multiplying would aim at (831, 250), which is not merely off, it is
    a different control.
    """
    assert logical_to_native(1662, 500, 2.0) == (3324, 1000)
    assert logical_to_native(1662, 500, 2.0) != (831, 250)


# ── availability ───────────────────────────────────────────────────

def _state_with(monkeypatch, servers: dict):
    mgr = MagicMock()
    mgr._servers = servers
    state_mod = MagicMock()
    state_obj = MagicMock()
    state_obj.mcp_client = mgr
    state_mod.state = state_obj
    monkeypatch.setitem(sys.modules, "api.state", state_mod)
    return mgr


def test_unavailable_when_no_brain_is_attached(monkeypatch):
    monkeypatch.delitem(sys.modules, "api.state", raising=False)
    assert background_click_available() is False


def test_unavailable_when_the_driver_is_not_configured(monkeypatch):
    _state_with(monkeypatch, {"github": MagicMock(is_connected=True)})
    assert background_click_available() is False


def test_unavailable_when_configured_but_disconnected(monkeypatch):
    """A configured-but-dead server must not look like a working one.

    Reporting it available would turn a cursor-free path into a silent
    failure, which is worse than falling back to the mouse.
    """
    conn = MagicMock()
    conn.is_connected = False
    _state_with(monkeypatch, {"cua-driver": conn})
    assert background_click_available() is False


def test_available_when_connected(monkeypatch):
    conn = MagicMock()
    conn.is_connected = True
    _state_with(monkeypatch, {"cua-driver": conn})
    assert background_click_available() is True


# ── the call ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_it_calls_the_driver_with_native_coordinates(monkeypatch):
    from skills.impl.background_input import background_click

    conn = MagicMock()
    conn.is_connected = True
    mgr = _state_with(monkeypatch, {"cua-driver": conn})
    mgr.call_tool = AsyncMock(return_value={"ok": True})

    ok, detail = await background_click(logical_x=840, logical_y=525, scale=2.0)

    assert ok is True, detail
    mgr.call_tool.assert_awaited_once()
    name, args = mgr.call_tool.await_args.args
    assert name == BACKGROUND_CLICK_TOOL
    assert (args["x"], args["y"]) == (1680, 1050), (
        f"logical points were passed through untranslated: {args}"
    )


@pytest.mark.asyncio
async def test_a_driver_error_is_reported_not_swallowed(monkeypatch):
    from skills.impl.background_input import background_click

    conn = MagicMock()
    conn.is_connected = True
    mgr = _state_with(monkeypatch, {"cua-driver": conn})
    mgr.call_tool = AsyncMock(return_value={"error": "no such display"})

    ok, detail = await background_click(logical_x=10, logical_y=20, scale=1.0)
    assert ok is False
    assert "no such display" in detail


@pytest.mark.asyncio
async def test_it_reports_unavailable_rather_than_raising(monkeypatch):
    from skills.impl.background_input import background_click

    monkeypatch.delitem(sys.modules, "api.state", raising=False)
    ok, detail = await background_click(logical_x=10, logical_y=20, scale=1.0)
    assert ok is False
    assert detail
