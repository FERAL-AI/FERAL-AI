"""
FERAL GUI Computer Use — Anthropic-Style Desktop Control
==========================================================
Industry-standard computer-use skill providing individual GUI primitives:
screenshot, mouse clicks, typing, key combos, scrolling, and window management.

All coordinates from VLMs are in screenshot-space and automatically divided
by the DPI scale factor before being passed to pyautogui, so Retina/HiDPI
displays work correctly out of the box.

Hardened: action rate limiter (configurable), proper logger namespace.
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
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


def scale_coordinates(x: int, y: int, scale: float) -> Tuple[int, int]:
    """Convert VLM screenshot-space coords to physical screen coords."""
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
						((item 1 of sz) as text) & tab & ((item 2 of sz) as text) & linefeed
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


def _frontmost_pid() -> Optional[int]:
    try:
        from AppKit import NSWorkspace  # type: ignore[import-not-found]
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return int(app.processIdentifier()) if app is not None else None
    except Exception:
        return None


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

    front_pid = _frontmost_pid()
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
            "focused": bool(front_pid is not None and pid is not None
                            and int(pid) == front_pid),
        })
    return out, None


def _windows_via_applescript() -> Tuple[Optional[List[dict]], Optional[str], Optional[str]]:
    """Enumerate windows through System Events, one process at a time.

    Returns ``(windows, error, tcc_permission)``.
    """
    res = run_osascript(_WINDOW_LIST_APPLESCRIPT, timeout_s=15.0)
    if not res.ok:
        return None, (res.stderr.strip() or f"osascript exit {res.returncode}"), res.tcc_permission

    front_pid = _frontmost_pid()
    front_app = ""
    if front_pid is not None:
        try:
            from AppKit import NSWorkspace  # type: ignore[import-not-found]
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            front_app = str(app.localizedName() or "") if app is not None else ""
        except Exception:
            front_app = ""

    windows: List[dict] = []
    for line in res.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 6:
            logger.debug("window_list: unparseable AppleScript row: %r", line)
            continue
        app, title, x, y, w, h = parts
        try:
            rect = (int(x), int(y), int(w), int(h))
        except ValueError:
            logger.debug("window_list: non-numeric bounds in row: %r", line)
            continue
        windows.append({
            "app": app,
            "title": title,
            "x": rect[0], "y": rect[1], "width": rect[2], "height": rect[3],
            "pid": None,
            "window_id": None,
            "focused": bool(front_app and app == front_app),
        })
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
        max_actions = float(os.getenv("FERAL_GUI_MAX_ACTIONS_PER_S", "10"))
        self._rate_limiter = ActionRateLimiter(max_per_second=max_actions)

    @property
    def scale(self) -> float:
        if self._scale is None:
            self._scale = detect_dpi_scale()
            logger.info("DPI scale factor detected: %.1f", self._scale)
        return self._scale

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
        except Exception as exc:
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
                "dpi_scale": self.scale,
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
        await asyncio.to_thread(self._pyautogui_click, x, y, 1, "left")
        return self._ok(f"Clicked ({x}, {y})")

    async def _mouse_double_click(self, args: dict) -> dict:
        x, y = self._scaled_xy(args)
        await asyncio.to_thread(self._pyautogui_click, x, y, 2, "left")
        return self._ok(f"Double-clicked ({x}, {y})")

    async def _mouse_right_click(self, args: dict) -> dict:
        x, y = self._scaled_xy(args)
        await asyncio.to_thread(self._pyautogui_click, x, y, 1, "right")
        return self._ok(f"Right-clicked ({x}, {y})")

    # ── mouse_move ────────────────────────────────────────────────

    async def _mouse_move(self, args: dict) -> dict:
        x, y = self._scaled_xy(args)
        await asyncio.to_thread(self._pyautogui_move, x, y)
        return self._ok(f"Moved to ({x}, {y})")

    # ── type_text ─────────────────────────────────────────────────

    async def _type_text(self, args: dict) -> dict:
        text = args.get("text", "")
        if not text:
            return self._err(400, "text is required")
        await asyncio.to_thread(self._do_type, text)
        preview = text[:80] + ("..." if len(text) > 80 else "")
        return self._ok(f"Typed: {preview}")

    # ── key_press ─────────────────────────────────────────────────

    async def _key_press(self, args: dict) -> dict:
        combo = args.get("keys", "") or args.get("key", "")
        if not combo:
            return self._err(400, "keys is required (e.g. 'cmd+c')")
        await asyncio.to_thread(self._do_hotkey, combo)
        return self._ok(f"Key combo: {combo}")

    # ── scroll ────────────────────────────────────────────────────

    async def _scroll(self, args: dict) -> dict:
        x = int(args.get("x", 0))
        y = int(args.get("y", 0))
        direction = args.get("direction", "down")
        amount = int(args.get("amount", 3))
        sx, sy = scale_coordinates(x, y, self.scale) if (x or y) else (None, None)
        await asyncio.to_thread(self._do_scroll, sx, sy, direction, amount)
        return self._ok(f"Scrolled {direction} by {amount}")

    # ── cursor_position ───────────────────────────────────────────

    async def _cursor_position(self, args: dict) -> dict:
        pos = await asyncio.to_thread(self._get_cursor_pos)
        return {
            "success": True, "status_code": 200,
            "data": {"x": pos[0], "y": pos[1], "dpi_scale": self.scale},
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
        if not title:
            return self._err(400, "title is required")
        outcome = await asyncio.to_thread(self._focus_window, title)
        if outcome.get("ok"):
            return {
                "success": True, "status_code": 200,
                "data": {
                    "message": f"Focused: {outcome.get('matched') or title}",
                    "app": outcome.get("app"),
                    "matched": outcome.get("matched"),
                    "strategy": outcome.get("strategy"),
                },
                "error": None,
            }
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
        pyautogui.click(x, y, clicks=clicks, button=button)

    @staticmethod
    def _pyautogui_move(x: int, y: int) -> None:
        import pyautogui
        pyautogui.moveTo(x, y)

    @staticmethod
    def _do_type(text: str) -> None:
        """Type text. Uses pyperclip + Cmd/Ctrl+V for non-ASCII."""
        import pyautogui
        if text.isascii():
            pyautogui.write(text, interval=0.02)
        else:
            try:
                import pyperclip
                pyperclip.copy(text)
                modifier = "command" if platform.system() == "Darwin" else "ctrl"
                pyautogui.hotkey(modifier, "v")
            except ImportError:
                pyautogui.write(text, interval=0.02)

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
        pyautogui.hotkey(*mapped)

    @staticmethod
    def _do_scroll(
        x: Optional[int], y: Optional[int], direction: str, amount: int,
    ) -> None:
        import pyautogui
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
    def _focus_window(title: str) -> dict:
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
            return _focus_window_macos(title)
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


def _focus_window_macos(title: str) -> dict:
    """macOS focus: match a window title first, fall back to app name."""
    listing = collect_windows()
    needle = title.casefold()

    exact = [w for w in listing.windows if (w.get("title") or "").casefold() == needle]
    partial = [w for w in listing.windows if needle in (w.get("title") or "").casefold()]
    by_app = [w for w in listing.windows if needle in (w.get("app") or "").casefold()]
    target = (exact or partial or by_app or [None])[0]

    app_name = (target or {}).get("app") or title
    res = run_osascript(f'tell application {_as_str(app_name)} to activate', timeout_s=8)
    if res.ok:
        # Raising a specific window inside the app needs Accessibility.
        # Best effort: never let its failure turn a successful activate
        # into a reported failure.
        if target and target.get("title"):
            run_osascript(
                'tell application "System Events" to tell process '
                f'{_as_str(app_name)} to perform action "AXRaise" of '
                f'(first window whose name is {_as_str(target["title"])})',
                timeout_s=8,
            )
        return {
            "ok": True,
            "app": app_name,
            "matched": (target or {}).get("title") or app_name,
            "strategy": "window_match" if target else "app_name",
        }
    if res.tcc_permission:
        return {
            "ok": False, "status_code": 403,
            "tcc_permission": res.tcc_permission, "error": res.stderr.strip(),
        }
    if target is None:
        return {
            "ok": False, "status_code": 404,
            "error": (
                f"No open window or running app matched {title!r}; "
                f"activate failed: {res.stderr.strip()}"
            ),
            "candidates": sorted(
                {w.get("app", "") for w in listing.windows if w.get("app")}
            ),
        }
    return {"ok": False, "status_code": 500, "error": res.stderr.strip()}
