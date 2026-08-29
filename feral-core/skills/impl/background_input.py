"""Background click delivery, for elements the AX tree cannot activate.

Where this fits
===============
``macos_ax`` targets semantically. The model reads a text tree, names
``[ax7] AXButton "Back"``, and the runtime performs an AX action. No
screenshot, no vision model, no cursor movement.

That covers every element publishing an activating action. It does not
cover the rest, and for those the only remaining option was
``_coordinate_click``: a real mouse event that teleports the operator's
cursor, in a module whose stated premise is that it does not touch the
cursor.

cua-driver fills exactly that hole, and fills it without changing how we
target. It is a **coordinate** driver rather than a semantic one: of its
25 tools none reads an accessibility tree, ``get_desktop_state`` is a
screenshot, and ``click`` takes an absolute point. Its contribution is
**delivery** - it injects into the target process, so no cursor moves,
no window is raised, and no Space switches.

So the two compose rather than compete. We keep semantic targeting,
which works with a text-only model and which a screenshot-based driver
structurally cannot offer, and borrow its hands for the elements our
actions cannot reach. Crucially the composition still needs no vision
model, because the AX tree already carries each element's bounds.

The coordinate trap
===================
``AXPosition`` and ``AXSize`` are **logical points**. cua-driver's
``click`` takes coordinates in ``get_desktop_state`` space, which is
**native resolution**, and ``get_desktop_state`` reports the
``scale_factor`` relating them.

On a 2x display those spaces differ by exactly the factor that put every
VLM click at 57% of its target elsewhere in this codebase. That bug
survived because two individually-correct functions were composed and
nothing tested the composition, so :func:`logical_to_native` is tested
directly and in both directions.

Availability
============
This is optional. When cua-driver is not configured, not connected, or
there is no brain at all, callers are told so and keep their existing
behaviour. A configured-but-dead server must never look like a working
one: reporting it available would turn a cursor-free path into a silent
failure, which is worse than falling back to the mouse.
"""

from __future__ import annotations

import logging
import sys
from typing import Optional, Tuple

logger = logging.getLogger("feral.skill.background_input")

#: MCP server name this expects. Matches the id used when registering
#: cua-driver with ``MCPClientManager``.
BACKGROUND_DRIVER_SERVER = "cua-driver"

#: Prefixed tool name, in the ``mcp_<server>_<tool>`` form
#: ``MCPClientManager.call_tool`` parses.
BACKGROUND_CLICK_TOOL = f"mcp_{BACKGROUND_DRIVER_SERVER}_click"

#: Wall-clock ceiling for one injected click, seconds. Generous next to
#: an injected event (single-digit milliseconds) and short next to a
#: person waiting, so a wedged driver cannot hold the click path open.
BACKGROUND_CLICK_TIMEOUT_S = 5.0


def logical_to_native(x: float, y: float, scale: Optional[float]) -> Tuple[int, int]:
    """Convert AX logical points to native pixels.

    ``scale`` is the display's backing factor as reported by
    ``get_desktop_state``. A missing, zero or negative value means the
    probe failed; treating it as 1.0 degrades to the untranslated point,
    which is correct on a non-Retina display and merely wrong by a known
    factor on a Retina one. Multiplying by garbage would put the click
    anywhere at all.
    """
    if not scale or scale <= 0:
        scale = 1.0
    return int(round(x * scale)), int(round(y * scale))


def _driver_connection():
    """The live cua-driver connection, or ``None``.

    Reached through ``sys.modules`` rather than an import so ``skills``
    gains no dependency on ``api``, matching how ``SkillExecutor._gate``
    finds the tool runner.
    """
    state_mod = sys.modules.get("api.state")
    state_obj = getattr(state_mod, "state", None)
    manager = getattr(state_obj, "mcp_client", None)
    if manager is None:
        return None, None
    servers = getattr(manager, "_servers", None) or {}
    conn = servers.get(BACKGROUND_DRIVER_SERVER)
    if conn is None:
        return manager, None
    return manager, conn


def background_click_available() -> bool:
    """Whether a background click can actually be delivered right now."""
    _manager, conn = _driver_connection()
    if conn is None:
        return False
    return bool(getattr(conn, "is_connected", False))


async def background_click(
    *,
    logical_x: float,
    logical_y: float,
    scale: Optional[float],
    button: str = "left",
    count: int = 1,
) -> Tuple[bool, str]:
    """Deliver a click at an AX-derived point without moving the cursor.

    Returns ``(delivered, detail)``. Never raises: a caller reaching
    for this is already in its fallback path, and an exception there
    would replace a degraded outcome with no outcome.
    """
    manager, conn = _driver_connection()
    if conn is None or not getattr(conn, "is_connected", False):
        return False, (
            f"{BACKGROUND_DRIVER_SERVER} is not connected, so no background "
            f"click backend is available"
        )

    native_x, native_y = logical_to_native(logical_x, logical_y, scale)
    try:
        result = await manager.call_tool(
            BACKGROUND_CLICK_TOOL,
            {"x": native_x, "y": native_y, "button": button, "count": count},
        )
    except Exception as exc:
        logger.warning("background click raised: %s", exc)
        return False, f"background click raised: {exc}"

    if isinstance(result, dict) and result.get("error"):
        return False, str(result["error"])
    return True, f"delivered at native ({native_x}, {native_y})"


def background_click_sync(loop, **kwargs) -> Tuple[bool, str]:
    """Call :func:`background_click` from a worker thread.

    ``macos_ax.execute`` runs its handlers through ``asyncio.to_thread``,
    so the click path is not on the event loop and cannot simply await.
    ``run_coroutine_threadsafe`` schedules onto the loop that owns the
    MCP transport, which is the same mechanism the cron path uses to
    reach the brain's loop from its own thread.

    ``loop`` may be ``None`` when there is no brain, in which case there
    is no MCP client either and the answer is already "unavailable".
    """
    if loop is None:
        return False, "no event loop available for the background driver"
    try:
        import asyncio

        future = asyncio.run_coroutine_threadsafe(
            background_click(**kwargs), loop,
        )
        # Bounded: a wedged driver must not hold the click path open. The
        # budget is generous relative to an injected event (single-digit
        # milliseconds) and short relative to a user waiting.
        return future.result(timeout=BACKGROUND_CLICK_TIMEOUT_S)
    except Exception as exc:
        logger.warning("background click bridge failed: %s", exc)
        return False, f"background click bridge failed: {exc}"


