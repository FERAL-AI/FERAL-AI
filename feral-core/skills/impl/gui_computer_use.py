"""
FERAL GUI Computer Use — Anthropic-Style Desktop Control
==========================================================
Industry-standard computer-use skill providing individual GUI primitives:
screenshot, mouse clicks, typing, key combos, scrolling, and window management.

All coordinates from VLMs are in screenshot-space and are converted to
pyautogui's click space before use. The divisor comes from the screenshot
pipeline (capture width, the resize in ``encode_for_vlm``, then the
logical click space), NOT from the display's backing scale factor: the
model's coordinates went through the resize, so the resize is the ratio
that matters. See ``vlm_to_screen_divisor``.

Hardened: action rate limiter (configurable), proper logger namespace.
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import math
import os
import platform
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from skills.base import BaseSkill
from skills.impl import register_skill
from security.desktop_input import (
    DesktopInputBusy, DesktopInputCleanupFailed, DesktopInputRevoked,
    check_input, cleanup_failed, input_failure, input_pause,
    physical_input_scope, run_worker,
)

logger = logging.getLogger("feral.skill.gui")

_SCREENSHOT_MAX_WIDTH = 1920


# ── DPI / Retina helpers ─────────────────────────────────────────

def detect_dpi_scale() -> float:
    """Detect the display DPI scale factor.

    macOS: queries NSScreen via a subprocess call to AppKit.
    Linux: reads GDK_SCALE env var.
    Falls back to 1.0 everywhere else.
    """
    system = platform.system()
    if system == "Darwin":
        try:
            result = subprocess.run(
                [
                    "python3", "-c",
                    "import AppKit; print(AppKit.NSScreen.mainScreen().backingScaleFactor())",
                ],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return float(result.stdout.strip())
        except Exception:
            pass
        return 2.0  # safe default for modern Macs
    elif system == "Linux":
        scale = os.environ.get("GDK_SCALE")
        if scale:
            try:
                return float(scale)
            except ValueError:
                pass
        return 1.0
    return 1.0


def vlm_to_screen_divisor(
    logical_width: float,
    backing_scale: float,
    max_width: int = _SCREENSHOT_MAX_WIDTH,
) -> float:
    """Ratio between the image the model sees and the space we click in.

    Three coordinate spaces are involved and the click path used the
    wrong ratio between two of them:

      1. native capture: ``screencapture`` writes physical pixels, so a
         1680-point Retina screen produces a 3360-wide PNG.
      2. what the model sees: ``encode_for_vlm`` resizes anything wider
         than ``max_width`` and throws the ratio away.
      3. where pyautogui clicks: logical points, 1680 wide.

    Model coordinates live in space 2. The old code divided by the
    display's backing scale factor (2.0), which is the ratio between
    spaces 1 and 3 and describes a resize that already happened
    differently. The correct divisor is ``1920 / 1680 = 1.143``, so
    every click landed at about 57% of the intended position, with the
    error growing toward the screen edge: a target at x=1900 was clicked
    at 950 rather than 1662.

    Deriving it from the pipeline rather than from the display also
    fixes a case the old arithmetic could not express at all. On a 5K
    Retina panel the capture is 5120 wide, the model sees 1920, and
    pyautogui clicks in 2560, so the divisor is **below one** and
    coordinates must scale UP. Dividing by a backing factor can only
    ever scale down.
    """
    if logical_width <= 0:
        return 1.0
    if backing_scale <= 0:
        backing_scale = 1.0
    native_width = logical_width * backing_scale
    seen_by_model = min(native_width, float(max_width))
    divisor = seen_by_model / logical_width
    return divisor if divisor > 0 else 1.0


def scale_coordinates(x: int, y: int, scale: float) -> Tuple[int, int]:
    """Convert VLM screenshot-space coords to physical screen coords.

    ``scale`` is the value from :func:`vlm_to_screen_divisor`, not the
    display's backing scale factor. Passing the latter is the defect
    this signature's caller was fixed for; see that function.
    """
    if scale <= 0:
        scale = 1.0
    return int(x / scale), int(y / scale)


# ── Rate limiter ─────────────────────────────────────────────────

class ActionRateLimiter:
    """Sliding-window rate limiter for GUI actions."""

    def __init__(self, max_per_second: float = 10.0):
        self._max_per_second = max_per_second
        self._timestamps: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> bool:
        """Return True if the action is allowed, False if rate-limited."""
        async with self._lock:
            now = time.monotonic()
            cutoff = now - 1.0
            self._timestamps = [t for t in self._timestamps if t > cutoff]
            if len(self._timestamps) >= self._max_per_second:
                return False
            self._timestamps.append(now)
            return True


# ── Screenshot capture (cross-platform) ─────────────────────────

async def capture_screenshot_bytes() -> Optional[bytes]:
    """Capture the screen and return raw JPEG bytes (resized for VLM)."""
    system = platform.system()
    raw_data: Optional[bytes] = None

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        path = tmp.name

    try:
        if system == "Darwin":
            proc = await asyncio.create_subprocess_exec(
                "screencapture", "-x", "-t", "png", path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.wait()
            if Path(path).exists() and Path(path).stat().st_size > 0:
                raw_data = Path(path).read_bytes()

        elif system == "Linux":
            for tool in ["gnome-screenshot", "scrot", "import"]:
                if not shutil.which(tool):
                    continue
                if tool == "gnome-screenshot":
                    proc = await asyncio.create_subprocess_exec(
                        tool, "-f", path,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                elif tool == "scrot":
                    proc = await asyncio.create_subprocess_exec(
                        tool, path,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                elif tool == "import":
                    proc = await asyncio.create_subprocess_exec(
                        tool, "-window", "root", path,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                else:
                    continue
                await proc.wait()
                if Path(path).exists() and Path(path).stat().st_size > 0:
                    raw_data = Path(path).read_bytes()
                    break
        else:
            logger.warning("gui_computer_use: unsupported platform %s", system)
            return None

    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    if not raw_data:
        return None

    return encode_for_vlm(raw_data)


def encode_for_vlm(raw_data: bytes) -> bytes:
    """Downscale a screenshot and re-encode it as JPEG for a VLM.

    Split out of :func:`capture_screenshot_bytes` so it is testable
    without shelling out to ``screencapture``. Returns ``raw_data``
    unchanged when Pillow is unavailable.
    """
    try:
        from PIL import Image
    except ImportError:
        return raw_data

    img = Image.open(io.BytesIO(raw_data))
    if img.width > _SCREENSHOT_MAX_WIDTH:
        ratio = _SCREENSHOT_MAX_WIDTH / img.width
        img = img.resize(
            (_SCREENSHOT_MAX_WIDTH, int(img.height * ratio)),
            Image.LANCZOS,
        )
    # JPEG has no alpha channel, and macOS `screencapture -t png` writes
    # RGBA. Without this flatten, `img.save(..., "JPEG")` raised
    # `OSError: cannot write mode RGBA as JPEG` for every single capture
    #, verified on macOS 15, where the endpoint returned a 500 on 100%
    # of calls. `screen_capture` and `perception/screen_loop` already
    # carried the same guard; this surface was the one that missed it.
    # Composite onto black rather than dropping the channel so partially
    # transparent windows do not come out with garbage colour.
    if img.mode not in ("RGB", "L"):
        if img.mode in ("RGBA", "LA") or "transparency" in img.info:
            rgba = img.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (0, 0, 0))
            flat.paste(rgba, mask=rgba.split()[-1])
            img = flat
        else:
            img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=75)
    return buf.getvalue()


# ── AppleScript helpers ──────────────────────────────────────────

def _as_str(value: str) -> str:
    """Quote a Python string as an AppleScript string literal.

    AppleScript escapes only backslash and double quote inside a
    literal. Without this, any window title containing a quote (or a
    backslash) produced a syntax error, and a hostile title could
    close the literal and inject script.
    """
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class OSAResult:
    """Thin result wrapper around one ``osascript`` invocation.

    Deliberately not the ``skills.desktop_control.AppleScriptResult``
    dataclass: this module must stay importable on non-macOS hosts,
    where that runner raises on call. It reuses that module's denial
    classifier so both surfaces agree on what a TCC denial looks like.
    """

    __slots__ = ("ok", "stdout", "stderr", "returncode", "tcc_permission")

    def __init__(self, ok: bool, stdout: str, stderr: str, returncode: int):
        self.ok = ok
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.tcc_permission: Optional[str] = None
        if not ok:
            try:
                from skills.desktop_control.applescript import _is_accessibility_denial
                if _is_accessibility_denial(stderr):
                    self.tcc_permission = "accessibility"
            except Exception:  # pragma: no cover, import guard only
                if "not allowed assistive access" in stderr or "-25211" in stderr:
                    self.tcc_permission = "accessibility"


def run_osascript(script: str, *, timeout_s: float = 8.0) -> OSAResult:
    """Run AppleScript and report the truth about how it went.

    The bug this exists to prevent: the previous window-list code ran
    ``subprocess.run(["osascript", ...])`` and read only ``stdout``,
    never ``returncode`` or ``stderr``. A denied or erroring script
    produced an empty stdout, which the caller reported as "zero
    windows, success".
    """
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return OSAResult(False, "", f"osascript timed out after {timeout_s}s", 124)
    except FileNotFoundError:
        return OSAResult(False, "", "osascript binary not found on PATH", 127)
    except Exception as exc:  # pragma: no cover, defensive
        return OSAResult(False, "", f"osascript failed to launch: {exc}", 1)
    return OSAResult(
        proc.returncode == 0, proc.stdout or "", proc.stderr or "", proc.returncode,
    )


# ── Window enumeration ───────────────────────────────────────────

# Per-process iteration. The aggregate form this replaces
#   {name, position, size} of every window of every process whose visible is true
#, fails on macOS with "System Events got an error: osascript is not
# allowed assistive access. (-25211)" even when assistive access IS
# granted (verified live: `get UI elements enabled` returns true and
# `tell process "X" to get name of every window` succeeds in the same
# second). Iterating processes one at a time avoids the quirk entirely.
#
# It also emits one tab-delimited record per line. The aggregate form
# returned a single comma-joined blob for the whole machine, so
# `splitlines()` produced one unparseable row and the caller shipped it
# to the model as {"raw": <blob>} with no title and no bounds, while
# the manifest promised "titles and bounds".
_WINDOW_LIST_APPLESCRIPT = """
set AppleScript's text item delimiters to ""
set out to ""
tell application "System Events"
	set procs to every process whose background only is false
	repeat with p in procs
		set pname to name of p
		try
			repeat with w in (every window of p)
				try
					set wname to name of w
				on error
					set wname to ""
				end try
				try
					set pos to position of w
					set sz to size of w
					set out to out & pname & tab & wname & tab & ¬
						((item 1 of pos) as text) & tab & ((item 2 of pos) as text) & tab & ¬
						((item 1 of sz) as text) & tab & ((item 2 of sz) as text) & tab & ¬
					((unix id of p) as text) & linefeed
				end try
			end repeat
		end try
	end repeat
end tell
return out
"""


class WindowListing:
    """Outcome of a window enumeration, errors included."""

    __slots__ = ("ok", "windows", "source", "error", "tcc_permission", "attempts")

    def __init__(self) -> None:
        self.ok: bool = False
        self.windows: List[dict] = []
        self.source: Optional[str] = None
        self.error: Optional[str] = None
        self.tcc_permission: Optional[str] = None
        # One entry per backend tried, so a failure is diagnosable
        # without re-running anything.
        self.attempts: List[dict] = []


def _focus_message_timeout(ax, element) -> bool:
    try:
        return ax.AXUIElementSetMessagingTimeout(element, 0.25) == 0
    except Exception:
        return False


def _foreground_state() -> dict:
    """Fresh system-wide AX observation; unavailable is never a cached guess."""
    state: dict[str, Any] = {"pid": None, "window": None, "title": None, "bounds": None}
    try:
        from skills.impl.macos_ax import _ax, _attr, _bounds
        ax = _ax()
        if not ax.AXIsProcessTrusted():
            return state
        system = ax.AXUIElementCreateSystemWide()
        error, app = _attr(system, "AXFocusedApplication")
        if error or app is None:
            return state
        error, pid = ax.AXUIElementGetPid(app, None)
        if error or type(pid) is not int or pid <= 0:
            return state
        state["pid"] = pid
        if not _focus_message_timeout(ax, app):
            return state
        error, window = _attr(app, "AXFocusedWindow")
        if error or window is None:
            return state
        if not _focus_message_timeout(ax, window):
            return state
        state["window"], state["bounds"] = window, _bounds(window)
        error, title = _attr(window, "AXTitle")
        if not error and isinstance(title, str):
            state["title"] = title
        return state
    except Exception:
        return state


def _frontmost_pid() -> Optional[int]:
    return _foreground_state()["pid"]


def _same_bounds(left: dict | None, right: dict | None) -> bool:
    """Compare the same integer point precision returned by window_list."""
    if left is None or right is None:
        return False
    keys = ("x", "y", "width", "height")
    try:
        for bounds in (left, right):
            if any(type(bounds.get(key)) not in (int, float)
                   or not math.isfinite(bounds[key]) for key in keys):
                return False
            if bounds["width"] <= 0 or bounds["height"] <= 0:
                return False
        return all(int(left[key]) == int(right[key]) for key in keys)
    except (TypeError, ValueError, OverflowError):
        return False


def _row_for_ax_window(state: dict, windows: list[dict]) -> dict | None:
    candidates = [w for w in windows if w.get("pid") == state.get("pid") and state.get("pid")]
    titled = [w for w in candidates if state.get("title") is not None and w.get("title") == state["title"]]
    if len(titled) == 1:
        return titled[0]
    bounded = [w for w in candidates if _same_bounds(w, state.get("bounds"))]
    return bounded[0] if len(bounded) == 1 else None


def _annotate_window_focus(windows: list[dict]) -> None:
    state = _foreground_state()
    focused = _row_for_ax_window(state, windows) if state["window"] is not None else None
    for row in windows:
        known = type(row.get("pid")) is int and state["pid"] is not None
        row["app_frontmost_known"] = known
        row["app_frontmost"] = bool(known and row["pid"] == state["pid"])
        row["focused_known"] = bool(known and (not row["app_frontmost"] or focused is not None))
        row["focused"] = row is focused


def _windows_via_quartz() -> Tuple[Optional[List[dict]], Optional[str]]:
    """Enumerate windows through CoreGraphics.

    Needs no Accessibility grant at all: bounds, owner name and pid come
    back regardless. Window *titles* (``kCGWindowName``) are gated on
    Screen Recording, so with that denied this returns rows with empty
    titles and the AppleScript backend is preferred instead.
    """
    try:
        import Quartz  # type: ignore[import-not-found]
    except ImportError as exc:
        return None, f"PyObjC Quartz not importable: {exc}"
    try:
        raw = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        )
    except Exception as exc:
        return None, f"CGWindowListCopyWindowInfo failed: {exc}"
    if raw is None:
        return None, "CGWindowListCopyWindowInfo returned NULL"

    out: List[dict] = []
    for entry in raw:
        # Layer 0 is the normal application-window layer. Higher layers
        # are menu bar items, the Dock, tooltips, status popovers.
        if int(entry.get("kCGWindowLayer") or 0) != 0:
            continue
        if float(entry.get("kCGWindowAlpha") or 0.0) <= 0.0:
            continue
        bounds = entry.get("kCGWindowBounds") or {}
        pid = entry.get("kCGWindowOwnerPID")
        out.append({
            "app": str(entry.get("kCGWindowOwnerName") or ""),
            "title": str(entry.get("kCGWindowName") or ""),
            "x": int(bounds.get("X", 0)),
            "y": int(bounds.get("Y", 0)),
            "width": int(bounds.get("Width", 0)),
            "height": int(bounds.get("Height", 0)),
            "pid": int(pid) if pid is not None else None,
            "window_id": int(entry.get("kCGWindowNumber") or 0) or None,
            "focused": False,
        })
    _annotate_window_focus(out)
    return out, None


def _windows_via_applescript() -> Tuple[Optional[List[dict]], Optional[str], Optional[str]]:
    """Enumerate windows through System Events, one process at a time.

    Returns ``(windows, error, tcc_permission)``.
    """
    res = run_osascript(_WINDOW_LIST_APPLESCRIPT, timeout_s=15.0)
    if not res.ok:
        return None, (res.stderr.strip() or f"osascript exit {res.returncode}"), res.tcc_permission

    windows: List[dict] = []
    for line in res.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) not in (6, 7):
            logger.debug("window_list: unparseable AppleScript row: %r", line)
            continue
        app, title, x, y, w, h = parts[:6]
        try:
            rect = (int(x), int(y), int(w), int(h))
            pid = int(parts[6]) if len(parts) == 7 else None
        except ValueError:
            logger.debug("window_list: non-numeric bounds in row: %r", line)
            continue
        windows.append({
            "app": app,
            "title": title,
            "x": rect[0], "y": rect[1], "width": rect[2], "height": rect[3],
            "pid": pid if pid is not None and pid > 0 else None,
            "window_id": None,
            "focused": False,
        })
    _annotate_window_focus(windows)
    return windows, None, None


def collect_windows() -> WindowListing:
    """List on-screen windows, trying every backend and reporting why.

    Order: CoreGraphics first (no Accessibility grant needed), then
    System Events. A backend that returns rows but no titles at all
    (Screen Recording denied, in the CoreGraphics case) loses to a
    backend that returns titles.
    """
    listing = WindowListing()
    if platform.system() != "Darwin":
        listing.error = (
            f"window_list is implemented for macOS only; host is {platform.system()}"
        )
        listing.attempts.append({"backend": "platform", "error": listing.error})
        return listing

    # Only backends that actually *succeeded* become candidates. A
    # backend returning zero windows without an error is a legitimate
    # answer (nothing is open), and must not be reported as a failure
    # that would be the mirror image of the bug being fixed here.
    candidates: List[Tuple[str, List[dict]]] = []

    quartz_windows, quartz_err = _windows_via_quartz()
    listing.attempts.append({
        "backend": "coregraphics",
        "ok": quartz_err is None,
        "count": len(quartz_windows or []),
        "error": quartz_err,
    })
    if quartz_err is None:
        candidates.append(("coregraphics", quartz_windows or []))

    # CoreGraphics reports bounds without any grant but leaves
    # kCGWindowName empty unless Screen Recording is granted, so fall
    # through to System Events whenever it produced no titles.
    if not any(w["title"] for w in (quartz_windows or [])):
        as_windows, as_err, as_tcc = _windows_via_applescript()
        listing.attempts.append({
            "backend": "applescript",
            "ok": as_err is None,
            "count": len(as_windows or []),
            "error": as_err,
            "tcc_permission": as_tcc,
        })
        if as_err is None:
            candidates.append(("applescript", as_windows or []))
        elif as_tcc and not candidates:
            listing.tcc_permission = as_tcc

    if not candidates:
        errors = [
            f"{a['backend']}: {a['error']}"
            for a in listing.attempts if a.get("error")
        ]
        listing.error = "; ".join(errors) or "every window backend failed"
        return listing

    titled = [c for c in candidates if any(w["title"] for w in c[1])]
    non_empty = [c for c in candidates if c[1]]
    chosen = (titled or non_empty or candidates)[0]

    listing.ok = True
    listing.source, listing.windows = chosen
    return listing


# ── Skill implementation ─────────────────────────────────────────

@register_skill
class GUIComputerUseSkill(BaseSkill):
    """Individual GUI-control primitives for VLM-driven computer use."""

    def __init__(self) -> None:
        super().__init__(skill_id="gui_computer_use")
        self._scale: Optional[float] = None
        self._dpi_scale: Optional[float] = None
        max_actions = float(os.getenv("FERAL_GUI_MAX_ACTIONS_PER_S", "10"))
        self._rate_limiter = ActionRateLimiter(max_per_second=max_actions)

    @property
    def dpi_scale(self) -> float:
        """The display's backing scale factor, for reporting only.

        Not the click divisor. Using it as one is what put every Retina
        click at ~57% of the intended position; see
        :func:`vlm_to_screen_divisor`.
        """
        if self._dpi_scale is None:
            self._dpi_scale = detect_dpi_scale()
            logger.info("DPI scale factor detected: %.1f", self._dpi_scale)
        return self._dpi_scale

    @property
    def scale(self) -> float:
        """Divisor taking model coordinates to pyautogui coordinates.

        Derived from the screenshot pipeline (capture, resize, click
        space) rather than from the display, because the resize step is
        what the model's coordinates actually went through.
        """
        if self._scale is None:
            logical_w = self._logical_screen_width()
            self._scale = vlm_to_screen_divisor(logical_w, self.dpi_scale)
            logger.info(
                "click divisor %.3f (logical width %.0f, dpi scale %.1f)",
                self._scale, logical_w, self.dpi_scale,
            )
        return self._scale

    @staticmethod
    def _logical_screen_width() -> float:
        """Width of the space pyautogui clicks in.

        pyautogui is the authority here because it is what receives the
        coordinates. Falling back to a probe-free default of 0 would
        make the divisor 1.0, i.e. no correction, which is the safe
        direction: it degrades to the pre-resize behaviour rather than
        moving clicks somewhere new.
        """
        try:
            import pyautogui
            return float(pyautogui.size()[0])
        except Exception:
            logger.warning(
                "could not read the screen size; clicks will not be "
                "corrected for the screenshot resize"
            )
            return 0.0

    async def execute(
        self, endpoint_id: str, args: Dict[str, Any], vault: Dict[str, str],
    ) -> Dict[str, Any]:
        if endpoint_id != "screenshot":
            allowed = await self._rate_limiter.acquire()
            if not allowed:
                return {
                    "success": False, "status_code": 429,
                    "data": None,
                    "reason": "rate_limit_exceeded",
                    "error": f"Rate limit exceeded (max {self._rate_limiter._max_per_second}/s)",
                }

        dispatch = {
            "screenshot": self._screenshot,
            "mouse_click": self._mouse_click,
            "mouse_double_click": self._mouse_double_click,
            "mouse_right_click": self._mouse_right_click,
            "mouse_move": self._mouse_move,
            "type_text": self._type_text,
            "key_press": self._key_press,
            "scroll": self._scroll,
            "cursor_position": self._cursor_position,
            "window_list": self._window_list,
            "window_focus": self._window_focus,
        }
        handler = dispatch.get(endpoint_id)
        if not handler:
            return {
                "success": False, "status_code": 404,
                "data": None, "error": f"Unknown endpoint: {endpoint_id}",
            }
        try:
            return await handler(args)
        except (DesktopInputBusy, DesktopInputCleanupFailed, DesktopInputRevoked) as exc:
            return input_failure(exc)
        except Exception as exc:
            if endpoint_id in {"mouse_click", "mouse_double_click", "mouse_right_click", "mouse_move",
                               "type_text", "key_press", "scroll", "window_focus"}:
                logger.error("gui_computer_use.%s failed (%s)", endpoint_id, type(exc).__name__)
                return self._err(500, "Desktop input failed; earlier effects may need checking")
            logger.exception("gui_computer_use.%s failed", endpoint_id)
            return {
                "success": False, "status_code": 500,
                "data": None, "error": str(exc),
            }

    # ── screenshot ────────────────────────────────────────────────

    async def _screenshot(self, args: dict) -> dict:
        data = await asyncio.to_thread(self._sync_screenshot)
        if data is None:
            return {
                "success": False, "status_code": 500,
                "data": None, "error": "Screenshot capture failed",
            }
        return {
            "success": True, "status_code": 200,
            "data": {
                "image_base64": data,
                "format": "jpeg",
                "dpi_scale": self.dpi_scale,
                "click_divisor": self.scale,
            },
            "error": None,
        }

    def _sync_screenshot(self) -> Optional[str]:
        loop = asyncio.new_event_loop()
        try:
            raw = loop.run_until_complete(capture_screenshot_bytes())
        finally:
            loop.close()
        if raw is None:
            return None
        return base64.b64encode(raw).decode()

    # ── mouse_click ───────────────────────────────────────────────

    async def _mouse_click(self, args: dict) -> dict:
        x, y = self._scaled_xy(args)
        await run_worker(self._pyautogui_click, x, y, 1, "left", physical=True)
        return self._ok(f"Clicked ({x}, {y})")

    async def _mouse_double_click(self, args: dict) -> dict:
        x, y = self._scaled_xy(args)
        await run_worker(self._pyautogui_click, x, y, 2, "left", physical=True)
        return self._ok(f"Double-clicked ({x}, {y})")

    async def _mouse_right_click(self, args: dict) -> dict:
        x, y = self._scaled_xy(args)
        await run_worker(self._pyautogui_click, x, y, 1, "right", physical=True)
        return self._ok(f"Right-clicked ({x}, {y})")

    # ── mouse_move ────────────────────────────────────────────────

    async def _mouse_move(self, args: dict) -> dict:
        x, y = self._scaled_xy(args)
        await run_worker(self._pyautogui_move, x, y, physical=True)
        return self._ok(f"Moved to ({x}, {y})")

    # ── type_text ─────────────────────────────────────────────────

    async def _type_text(self, args: dict) -> dict:
        text = args.get("text", "")
        if not text:
            return self._err(400, "text is required")
        await run_worker(self._do_type, text, physical=True)
        preview = text[:80] + ("..." if len(text) > 80 else "")
        return self._ok(f"Typed: {preview}")

    # ── key_press ─────────────────────────────────────────────────

    async def _key_press(self, args: dict) -> dict:
        combo = args.get("keys", "") or args.get("key", "")
        if not combo:
            return self._err(400, "keys is required (e.g. 'cmd+c')")
        await run_worker(self._do_hotkey, combo, physical=True)
        return self._ok(f"Key combo: {combo}")

    # ── scroll ────────────────────────────────────────────────────

    async def _scroll(self, args: dict) -> dict:
        x = int(args.get("x", 0))
        y = int(args.get("y", 0))
        direction = args.get("direction", "down")
        amount = int(args.get("amount", 3))
        sx, sy = scale_coordinates(x, y, self.scale) if (x or y) else (None, None)
        await run_worker(self._do_scroll, sx, sy, direction, amount, physical=True)
        return self._ok(f"Scrolled {direction} by {amount}")

    # ── cursor_position ───────────────────────────────────────────

    async def _cursor_position(self, args: dict) -> dict:
        pos = await asyncio.to_thread(self._get_cursor_pos)
        return {
            "success": True, "status_code": 200,
            "data": {"x": pos[0], "y": pos[1], "dpi_scale": self.dpi_scale},
            "error": None,
        }

    # ── window_list ───────────────────────────────────────────────

    async def _window_list(self, args: dict) -> dict:
        """List on-screen windows with real titles and bounds.

        Pre-fix this returned ``{"success": True, "status_code": 200,
        "windows": []}`` whenever the underlying AppleScript failed,
        because ``_get_windows`` swallowed every error and the caller
        never inspected the return code. On a Mac where the AppleScript
        was denied, the model was told with a 200 that the machine had
        no windows open. It now fails loudly and says why.
        """
        result = await asyncio.to_thread(collect_windows)
        if not result.ok:
            code = 403 if result.tcc_permission else 500
            error = (
                f"tcc_denied:{result.tcc_permission}"
                if result.tcc_permission
                else (result.error or "window enumeration failed")
            )
            return {
                "success": False, "status_code": code,
                "data": {"attempts": result.attempts},
                "error": error,
            }
        return {
            "success": True, "status_code": 200,
            "data": {
                "windows": result.windows,
                "count": len(result.windows),
                "source": result.source,
                "attempts": result.attempts,
            },
            "error": None,
        }

    # ── window_focus ──────────────────────────────────────────────

    async def _window_focus(self, args: dict) -> dict:
        title = args.get("title", "")
        if not isinstance(title, str) or not title or len(title) > 2048:
            return self._err(400, "title is required")
        ax_title = args.get("window_ax_title")
        if "window_ax_title" in args and (not isinstance(ax_title, str) or not ax_title or len(ax_title) > 2048):
            return self._err(400, "window_ax_title must be an exact nonempty observed AX title")
        if ax_title is not None and platform.system() != "Darwin":
            return self._err(400, "window_ax_title is supported on macOS only")
        if ax_title is None:
            outcome = await run_worker(self._focus_window, title, physical=True)
        else:
            outcome = await run_worker(self._focus_window, title, ax_title, physical=True)
        if outcome.get("ok"):
            return {
                "success": True, "status_code": 200,
                "data": {
                    "message": f"Focused: {outcome.get('matched') or title}",
                    "app": outcome.get("app"),
                    "matched": outcome.get("matched"),
                    "strategy": outcome.get("strategy"),
                    "pid": outcome.get("pid"),
                    "window_id": outcome.get("window_id"),
                    "ax_title": outcome.get("ax_title"),
                    "identity_match": outcome.get("identity_match"),
                    "foreground_verified": outcome.get("foreground_verified", False),
                    "window_focus_verified": outcome.get("window_focus_verified", False),
                },
                "error": None,
            }
        if outcome.get("error_code"):
            return {"success": False, "status_code": outcome.get("status_code", 409),
                "data": {"foreground_verified": False, "window_focus_verified": False},
                "error": outcome["error"], "error_code": outcome["error_code"]}
        if outcome.get("tcc_permission"):
            return {
                "success": False, "status_code": 403,
                "data": {"detail": outcome.get("error")},
                "error": f"tcc_denied:{outcome['tcc_permission']}",
            }
        if outcome.get("status_code") == 404:
            return {
                "success": False, "status_code": 404,
                "data": {"candidates": outcome.get("candidates", [])},
                "error": outcome.get("error") or f"No window or app matched: {title}",
            }
        return self._err(
            outcome.get("status_code", 500),
            outcome.get("error") or f"window_focus failed for: {title}",
        )

    # ── internal helpers ──────────────────────────────────────────

    def _scaled_xy(self, args: dict) -> Tuple[int, int]:
        raw_x = int(args.get("x", 0))
        raw_y = int(args.get("y", 0))
        return scale_coordinates(raw_x, raw_y, self.scale)

    @staticmethod
    def _ok(msg: str) -> dict:
        return {"success": True, "status_code": 200, "data": {"message": msg}, "error": None}

    @staticmethod
    def _err(code: int, msg: str) -> dict:
        return {"success": False, "status_code": code, "data": None, "error": msg}

    # ── pyautogui wrappers (run in thread) ────────────────────────

    @staticmethod
    def _pyautogui_click(x: int, y: int, clicks: int, button: str) -> None:
        import pyautogui
        with physical_input_scope():
            for _ in range(clicks):
                check_input()
                # Mark attempted before the OS call: a failing call may have
                # posted the down event before raising.
                try:
                    pyautogui.mouseDown(x, y, button=button)
                    check_input()
                finally:
                    try:
                        pyautogui.mouseUp(button=button)
                    except Exception:
                        cleanup_failed()

    @staticmethod
    def _pyautogui_move(x: int, y: int) -> None:
        import pyautogui
        with physical_input_scope():
            check_input()
            pyautogui.moveTo(x, y)

    @staticmethod
    def _do_type(text: str) -> None:
        """Type text. Uses pyperclip + Cmd/Ctrl+V for non-ASCII."""
        import pyautogui
        with physical_input_scope():
            check_input()
            if not text.isascii():
                try:
                    import pyperclip
                except ImportError:
                    pass
                else:
                    check_input()
                    pyperclip.copy(text)
                    check_input()
                    modifier = "command" if platform.system() == "Darwin" else "ctrl"
                    GUIComputerUseSkill._do_hotkey(modifier + "+v")
                    return
            # One character per bounded call. Revocation never retries the
            # remaining text, and cleanup releases only our attempted key.
            for character in text:
                check_input()
                try:
                    pyautogui.keyDown(character)
                    check_input()
                finally:
                    try:
                        pyautogui.keyUp(character)
                    except Exception:
                        cleanup_failed()
                input_pause(0.02)

    @staticmethod
    def _do_hotkey(combo: str) -> None:
        import pyautogui
        parts = [k.strip() for k in combo.split("+")]
        mapped = []
        for p in parts:
            low = p.lower()
            if low in ("cmd", "command", "meta", "super"):
                mapped.append("command" if platform.system() == "Darwin" else "ctrl")
            elif low in ("ctrl", "control"):
                mapped.append("ctrl")
            elif low in ("alt", "option"):
                mapped.append("alt")
            elif low in ("shift",):
                mapped.append("shift")
            else:
                mapped.append(low)
        with physical_input_scope():
            held = []
            try:
                for key in mapped:
                    check_input()
                    held.append(key)
                    pyautogui.keyDown(key)
                    check_input()
            finally:
                failed = False
                for key in reversed(held):
                    try:
                        pyautogui.keyUp(key)
                    except Exception:
                        failed = True
                if failed:
                    cleanup_failed()

    @staticmethod
    def _do_scroll(
        x: Optional[int], y: Optional[int], direction: str, amount: int,
    ) -> None:
        import pyautogui
        with physical_input_scope():
            check_input()
            clicks = amount if direction == "up" else -amount
            if x is not None and y is not None:
                pyautogui.scroll(clicks, x=x, y=y)
            else:
                pyautogui.scroll(clicks)

    @staticmethod
    def _get_cursor_pos() -> Tuple[int, int]:
        import pyautogui
        pos = pyautogui.position()
        return (pos.x, pos.y)

    @staticmethod
    def _get_windows() -> List[dict]:
        """Back-compat shim: the window rows only, no error detail.

        Kept because callers outside this module import it. New code
        should call :func:`collect_windows`, which reports *why* an
        enumeration returned nothing instead of pretending the machine
        has no windows.
        """
        return collect_windows().windows

    @staticmethod
    def _focus_window(title: str, window_ax_title: str | None = None) -> dict:
        """Bring a window (or its owning app) to the front.

        Returns a dict rather than a bare bool so the caller can tell
        "no such window" from "the OS refused us".

        The pre-fix macOS path interpolated ``title`` straight into an
        AppleScript string literal (a title containing a double quote
        produced a syntax error, and the aggregate
        ``first process whose ... name of every window contains``
        form is the same shape that trips System Events' -25211), then
        collapsed every outcome to ``returncode == 0``.
        """
        system = platform.system()
        if system == "Darwin":
            return _focus_window_macos(title, window_ax_title)
        if system == "Linux":
            for tool, argv in (
                ("wmctrl", ["wmctrl", "-a", title]),
                ("xdotool", ["xdotool", "search", "--name", title, "windowactivate"]),
            ):
                if not shutil.which(tool):
                    continue
                try:
                    result = subprocess.run(
                        argv, capture_output=True, text=True, timeout=5,
                    )
                except Exception as exc:
                    return {"ok": False, "status_code": 500, "error": f"{tool}: {exc}"}
                if result.returncode == 0:
                    return {"ok": True, "matched": title, "strategy": tool}
            return {
                "ok": False, "status_code": 404,
                "error": (
                    f"No window matched {title!r} "
                    "(wmctrl/xdotool unavailable or no match)"
                ),
            }
        return {
            "ok": False, "status_code": 501,
            "error": f"window_focus is not implemented for platform {system}",
        }


def _focus_failure(code: str, message: str, status: int = 409) -> dict:
    return {"ok": False, "status_code": status, "error_code": code, "error": message}


def _selected_ax_window(target: dict, rows: list[dict], app,
                        window_ax_title: str | None = None) -> tuple[Any, str | None, str] | None:
    """Bind one CG row to an actual AX handle; never infer a title suffix."""
    from skills.impl.macos_ax import _ax, _attr, _bounds
    error, windows = _attr(app, "AXWindows")
    if error or windows is None:
        return None
    try:
        if len(windows) > 64:
            return None
    except TypeError:
        return None
    deadline = time.monotonic() + 2
    observed = []
    for window in windows:
        check_input()
        if time.monotonic() > deadline:
            return None
        if not _focus_message_timeout(_ax(), window):
            return None
        error, pid = _ax().AXUIElementGetPid(window, None)
        if error or type(pid) is not int or pid != target["pid"]:
            return None
        error, title = _attr(window, "AXTitle")
        title = title if not error and isinstance(title, str) else None
        observed.append((window, title, _bounds(window)))
    if time.monotonic() > deadline:
        return None
    exact = [item for item in observed if item[1] is not None and item[1] == target.get("title")]
    siblings = [row for row in rows if row.get("pid") == target["pid"]]
    if window_ax_title is not None:
        explicit = [item for item in observed if item[1] == window_ax_title]
        cg_title = target.get("title")
        if not isinstance(cg_title, str) or not cg_title or len(explicit) != 1:
            return None
        # Both titles are observed strings, never a constructed app/profile
        # suffix. Require a one-to-one prefix relationship on BOTH inventories
        # as well as geometry, so hidden/similarly named windows cannot win.
        ax_prefixes = [item for item in observed if isinstance(item[1], str) and item[1].startswith(cg_title)]
        cg_prefixes = [row for row in siblings if isinstance(row.get("title"), str) and row["title"]
                       and window_ax_title.startswith(row["title"])]
        if (len(ax_prefixes) == len(cg_prefixes) == 1 and ax_prefixes[0] is explicit[0]
                and cg_prefixes[0] is target and _same_bounds(explicit[0][2], target)):
            return explicit[0][0], explicit[0][1], "explicit_observed_titles_and_bounds"
        return None
    if len(exact) == 1 and sum(row.get("title") == target.get("title") for row in siblings) == 1:
        return exact[0][0], exact[0][1], "observed_title"
    bounded = [item for item in observed if _same_bounds(item[2], target)]
    if len(bounded) == 1 and sum(_same_bounds(row, target) for row in siblings) == 1:
        return bounded[0][0], bounded[0][1], "bounds"
    return None


def _focus_window_macos(title: str, window_ax_title: str | None = None) -> dict:
    """Request exact app/window focus and independently verify its outcome."""
    from skills.impl.macos_ax import _ax, _running_apps
    from CoreFoundation import CFEqual
    listing = collect_windows()
    rows = listing.windows if listing.ok else []
    needle = title.casefold()
    exact = [w for w in rows if (w.get("title") or "").casefold() == needle]
    partial = [w for w in rows if needle in (w.get("title") or "").casefold()]
    by_app = [w for w in rows if needle in (w.get("app") or "").casefold()]
    matches = exact or partial or by_app
    if len(matches) > 1:
        return _focus_failure("window_focus_ambiguous", "Multiple windows matched; select an exact unique window title from window_list.")
    target = matches[0] if matches else None
    if target is not None:
        pid, app_name = target.get("pid"), target.get("app")
        if type(pid) is not int or pid <= 0 or not isinstance(app_name, str) or not app_name:
            return _focus_failure("window_focus_unverified", "Selected window has no exact process identity; refresh window_list.")
    else:
        if window_ax_title is not None:
            return _focus_failure("window_focus_unverified", "An explicit AX title requires a unique selected window_list row.")
        # App-only focus is allowed for a uniquely observed running app, but
        # never launches an unknown app or claims a specific window outcome.
        apps = _running_apps()
        exact_apps = [a for a in apps if a.name.casefold() == needle or a.bundle_id.casefold() == needle]
        apps = exact_apps or [a for a in apps if needle in a.name.casefold()]
        if len(apps) > 1:
            return _focus_failure("window_focus_ambiguous", "Multiple running applications matched; use an exact unique app name.")
        if not apps:
            return {"ok": False, "status_code": 404,
                "error": "No open window or running app matched the requested title.",
                "candidates": sorted({w.get("app", "") for w in rows if w.get("app")})}
        pid, app_name = apps[0].pid, apps[0].name
    if type(pid) is not int or pid <= 0:
        return _focus_failure("window_focus_unverified", "Selected application has no usable process identity.")
    ax = _ax()
    if not ax.AXIsProcessTrusted():
        return {"ok": False, "status_code": 403, "tcc_permission": "accessibility",
                "error": "Accessibility is required to verify and focus the selected app/window."}
    app = ax.AXUIElementCreateApplication(pid)
    if not _focus_message_timeout(ax, app):
        return _focus_failure("window_focus_unverified", "Selected application could not establish a bounded focus read.")
    error, app_pid = ax.AXUIElementGetPid(app, None)
    if error or type(app_pid) is not int or app_pid != pid:
        return _focus_failure("window_focus_unverified", "Selected application identity could not be read.")
    selected = _selected_ax_window(target, rows, app, window_ax_title) if target is not None else None
    if target is not None and selected is None:
        return _focus_failure("window_focus_unverified", "Selected CG window cannot be uniquely bound to an observed AX window; no focus was requested. For differing titles/overlapping windows, copy exact window_ax_title from macos_ax__list_windows.")
    check_input()
    activation = ax.AXUIElementSetAttributeValue(app, "AXFrontmost", True)
    if activation != 0:
        if activation == -25211:
            return {"ok": False, "status_code": 403, "tcc_permission": "accessibility", "error": "Accessibility refused selected-app activation."}
        return _focus_failure("window_focus_refused", "Selected-app activation was refused; foreground focus was not verified.", 502)
    if selected is not None:
        check_input()
        raised = ax.AXUIElementPerformAction(selected[0], "AXRaise")
        if raised != 0:
            if raised == -25211:
                return {"ok": False, "status_code": 403, "tcc_permission": "accessibility", "error": "Accessibility refused selected-window raising; app activation may already have occurred."}
            return _focus_failure("window_focus_refused", "Selected-window raising was refused; app activation may already have occurred.", 502)
    for attempt in range(6):
        check_input()
        current = collect_windows() if target is not None else None
        state = _foreground_state()
        verified = state["pid"] == pid
        if selected is not None:
            assert target is not None
            try:
                same_handle = bool(state["window"] is not None and CFEqual(state["window"], selected[0]))
            except Exception:
                same_handle = False
            verified = verified and same_handle
            if current is None or not current.ok:
                verified = False
            else:
                live = [row for row in current.windows if all(row.get(key) == target.get(key)
                        for key in ("pid", "app", "title", "window_id"))]
                if verified and len(live) == 1 and window_ax_title is not None:
                    rebound = _selected_ax_window(live[0], current.windows, app, window_ax_title)
                    try:
                        verified = bool(rebound is not None and CFEqual(rebound[0], selected[0]))
                    except Exception:
                        verified = False
                else:
                    verified = bool(verified and len(live) == 1 and _row_for_ax_window(state, current.windows) is live[0])
        if verified:
            return {"ok": True, "app": app_name, "pid": pid,
                "matched": target.get("title") or app_name if target is not None else app_name,
                "strategy": "window_match" if target is not None else "app_name",
                "window_id": target.get("window_id") if target is not None else None,
                "ax_title": selected[1] if selected is not None else None,
                "identity_match": selected[2] if selected is not None else "running_app_pid",
                "foreground_verified": True, "window_focus_verified": selected is not None}
        if attempt < 5:
            input_pause(0.05)
    return _focus_failure("window_focus_unverified", "Focus was requested but the selected app/window did not become verifiably foreground; do not send global keys or text.")
