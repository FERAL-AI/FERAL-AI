"""
FERAL Browser Control — CDP + Playwright
===========================================
Real browser automation via Chrome DevTools Protocol.

- Raw CDP for screenshots, navigation, and JS evaluation
- ARIA accessibility snapshots for agent-readable page structure
- Playwright bridge for reliable click/type/fill interactions
- Screenshot pipeline: resize + compress for VLM analysis
- Session video recording via CDP screencast + ffmpeg assembly
"""

from __future__ import annotations
import asyncio
import base64
import io
import json
import logging
import os
import re
import shutil
import statistics
import time
from pathlib import Path
from typing import Optional, Callable
from uuid import uuid4

logger = logging.getLogger("feral.skill.browser")

CDP_PORT = int(os.getenv("FERAL_CDP_PORT", "9222"))
CDP_HOST = os.getenv("FERAL_CDP_HOST", "localhost")
MAX_SCREENSHOT_WIDTH = 1920
JPEG_QUALITY = 75

# ── Session video recording ──────────────────────────────────────────
#
# Recording defaults. JPEG rather than PNG because a screencast of a
# 1280x800 viewport at 30fps is ~100x larger as PNG and the frames are
# only ever re-encoded into a lossy video anyway.
RECORDING_FRAME_QUALITY = 70
RECORDING_MAX_WIDTH = 1280
RECORDING_MAX_HEIGHT = 800
# ~3 minutes at 30fps. The cap exists because Page.screencastFrame has no
# natural end: a forgotten recording would otherwise fill the user's disk.
RECORDING_MAX_FRAMES = 5400
# Chrome's screencast is variable-rate: it emits a frame when the page
# repaints, not on a clock. A frame held for longer than this is almost
# always the page sitting idle, and replaying that idle time verbatim
# makes the video mostly dead air, so it is clamped.
RECORDING_MAX_FRAME_SECONDS = 5.0
RECORDING_MIN_FRAME_SECONDS = 1.0 / 60.0
RECORDING_FALLBACK_FPS = 12.0
RECORDING_REDACTION_STYLE_ID = "feral-recording-redaction"

# Text scrubbed out of anything a recording persists to disk.
#
# The three classes below (email addresses, UUID-shaped identifiers, and
# explicit tenant/user/object id parameters) are the ones that actually
# leak from browser recordings, which is the same set that
# browser-use/browser-harness (MIT) scrubs before exporting a video, in
# src/browser_harness/video.py and recorder.py. The rules here were
# written independently against that observation; no code was copied.
_SENSITIVE_TEXT = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    r"|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
    r"|(?:tenant|user|object|account|org)[_-]?id=[^&#\s]+",
    re.IGNORECASE,
)
# Credential-bearing query/fragment params. An OAuth redirect that lands
# mid-recording would otherwise write a live token into manifest.json.
_URL_SECRET_PARAMS = re.compile(
    r"([?&#](?:code|access_token|id_token|refresh_token|token|api_?key"
    r"|client_secret|client_info|session_state|signature|sig|auth"
    r"|authorization|password|secret)=)[^&#]*",
    re.IGNORECASE,
)
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

# ── Native JavaScript dialogs ────────────────────────────────────────
#
# alert()/confirm()/prompt()/beforeunload block the renderer until some
# CDP client answers them. An unanswered dialog is indistinguishable from
# a hung page: every subsequent Runtime.evaluate simply never returns.
#
# The default is DISMISS, not accept. Accepting is the dangerous side of
# a confirm() ("Delete account?", "Discard 40 unsaved edits?"), and a
# model that never saw the dialog cannot have consented to it. Dismiss is
# the conservative answer for every dialog type: alert() ignores the
# distinction, confirm() reads it as Cancel, prompt() as Cancel, and
# beforeunload as "stay on the page", which cancels a navigation rather
# than throwing away the user's work.
#
# Whatever the policy, every dialog is RECORDED and surfaced on the next
# endpoint result (``dialog_events``) so it is never handled behind the
# model's back.
DIALOG_POLICIES = ("dismiss", "accept", "manual")
DEFAULT_DIALOG_POLICY = "dismiss"
# In `manual` mode the page stays blocked until handle_dialog is called.
# A watchdog dismisses it after this long so a forgotten dialog degrades
# into a recorded, explained event instead of a permanently frozen tab.
DIALOG_MANUAL_TIMEOUT_S = 60.0
DIALOG_LOG_MAX = 50

# ── Accessibility snapshot ───────────────────────────────────────────
#
# Chrome's own menu bar and shell contribute hundreds of AX nodes before
# any page content, so an unfiltered head-of-list cap silently drops the
# elements the agent actually needs. Snapshot therefore filters to
# interactive roles by default and paginates over the full match list,
# reporting TRUNCATED with the next offset (the same contract the
# macos_ax skill uses).
SNAPSHOT_DEFAULT_MAX_NODES = 200
SNAPSHOT_MAX_MAX_NODES = 2000
# Skipped under BOTH filters: these carry no name an agent can act on and
# no structure it can orient by. Everything else survives filter=all, so
# "all" means all.
SNAPSHOT_SKIP_ROLES = frozenset({
    "none", "presentation", "generic", "InlineTextBox", "LineBreak",
})
# Roles that either accept input or carry the structure an agent needs to
# orient itself. Everything else is prose, which get_page_text reads far
# more cheaply than the AX tree does.
SNAPSHOT_INTERACTIVE_ROLES = frozenset({
    "button", "link", "textbox", "searchbox", "combobox", "listbox",
    "option", "checkbox", "radio", "radiogroup", "switch", "slider",
    "spinbutton", "menuitem", "menuitemcheckbox", "menuitemradio",
    "menu", "menubar", "tab", "tablist", "treeitem", "textarea",
    "ComboBox", "DisclosureTriangle", "PopUpButton", "ToggleButton",
    "form", "search", "dialog", "alertdialog", "heading", "img",
    "table", "row", "cell", "columnheader", "rowheader", "list",
    "listitem", "progressbar", "alert", "status", "tooltip",
    "colorwell", "date", "datetime", "InputTime",
})


def feral_browser_root() -> Path:
    """Root for everything this skill writes: ``$FERAL_HOME/browser``.

    ``config.loader.feral_data_home`` is the canonical resolver and it is
    the ONLY correct way to reach it. Parts of this file used to build
    ``Path.home() / ".feral"`` by hand, which ignores ``FERAL_HOME``
    outright: an isolated end-to-end run with ``FERAL_HOME`` pointed at a
    scratch directory still wrote live session cookies into the operator's
    real ``~/.feral/browser/cookies/``. That is a test-isolation hazard and
    a split brain for anyone who relocated their install, and it was
    inconsistent inside this one file, since ``_recordings_root`` already
    did it correctly.
    """
    from config.loader import feral_data_home
    return feral_data_home() / "browser"


def redact_recording_text(value: object) -> str:
    """Scrub secrets and identities out of text bound for a recording manifest.

    Applied unconditionally, not opt-in: a manifest is the part of a
    recording people paste into a ticket, so it must never be the thing
    that carries a token or a tenant id out of the machine. Pixel-level
    masking is separate and opt-in via ``redact_selectors``.
    """
    text = str(value or "")
    text = _URL_SECRET_PARAMS.sub(r"\1[REDACTED]", text)
    return _SENSITIVE_TEXT.sub("[REDACTED]", text)


def safe_recording_name(name: str) -> str:
    """Reduce a caller-supplied recording name to a single path segment.

    A recording id becomes a directory name, so ``../`` or an absolute
    path in it would write frames outside the FERAL data home. That is
    exactly the exposure this feature must not create.
    """
    cleaned = _UNSAFE_NAME.sub("-", str(name or "")).strip("-.")
    return cleaned[:64]


class CDPConnection:
    """Low-level Chrome DevTools Protocol connection via WebSocket."""

    def __init__(self, host: str = CDP_HOST, port: int = CDP_PORT):
        self._host = host
        self._port = port
        self._ws = None
        self._msg_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._recv_task: Optional[asyncio.Task] = None
        self._connected = False
        self._page_ws_url: Optional[str] = None
        self._event_listeners: list[Callable[[dict], None]] = []
        # AUDIT-FIXES F-06. Strong references to the async event-listener
        # dispatches fired from ``_receive_loop``. This is the highest-churn
        # site in the sweep (one task per CDP event per async listener), so
        # it is exactly the shape where retaining tasks unconditionally
        # would leak. The done-callback discard is what makes it safe: the
        # set only ever holds listeners that have not finished, and the loop
        # keeps tasks weakly, so without it a listener could be collected
        # mid-flight and the event silently dropped.
        self._bg_tasks: set[asyncio.Task] = set()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def is_page_target(self) -> bool:
        """True when the socket is attached to a tab rather than the browser.

        ``/json/version`` hands back the *browser* endpoint, and a
        browser-level socket does not implement the ``Page`` domain at
        all: ``Page.enable`` comes back as "wasn't found". Anything that
        needs ``Page.*`` (screencast, printToPDF, captureScreenshot) has
        to check this rather than trusting ``connected``.
        """
        return "/devtools/page/" in (self._page_ws_url or "")

    @property
    def target_id(self) -> str:
        """CDP target id of the tab this socket is attached to, if any.

        Empty for a browser-level socket. This is what lets the
        controller know which tab every ``Page.*``/``Runtime.*`` command
        it sends is actually going to hit, which is the whole point of
        :meth:`BrowserController.attach_to_tab`.
        """
        url = self._page_ws_url or ""
        marker = "/devtools/page/"
        if marker not in url:
            return ""
        return url.rsplit(marker, 1)[-1].strip()

    async def connect(self, prefer_page: bool = False, target_id: str = "") -> bool:
        """Connect to Chrome CDP endpoint.

        ``prefer_page`` resolves a tab target first instead of the
        browser endpoint. It is opt-in so existing callers keep the
        browser-level socket they already depend on.

        ``target_id`` pins the socket to one specific tab. Without it a
        reconnect lands on ``/json``'s first page target, which is not
        necessarily the tab the caller asked for, which is precisely the
        silent wrong-target failure ``switch_tab`` used to have.
        """
        try:
            import httpx
            if target_id:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        f"http://{self._host}:{self._port}/json",
                        timeout=5.0,
                    )
                    for t in resp.json():
                        if str(t.get("id")) == str(target_id):
                            self._page_ws_url = t.get("webSocketDebuggerUrl")
                            break
                if not self._page_ws_url:
                    logger.error("No CDP target with id %r", target_id)
                    return False
            elif prefer_page:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        f"http://{self._host}:{self._port}/json",
                        timeout=5.0,
                    )
                    pages = [t for t in resp.json() if t.get("type") == "page"]
                    if pages:
                        self._page_ws_url = pages[0].get("webSocketDebuggerUrl")

            if not self._page_ws_url:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        f"http://{self._host}:{self._port}/json/version",
                        timeout=5.0,
                    )
                    info = resp.json()
                    self._page_ws_url = info.get("webSocketDebuggerUrl")

            if not self._page_ws_url:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        f"http://{self._host}:{self._port}/json",
                        timeout=5.0,
                    )
                    targets = resp.json()
                    pages = [t for t in targets if t.get("type") == "page"]
                    if pages:
                        self._page_ws_url = pages[0].get("webSocketDebuggerUrl")

            if not self._page_ws_url:
                logger.error("No CDP WebSocket URL found")
                return False

            import websockets
            for _attempt in range(3):
                try:
                    self._ws = await websockets.connect(
                        self._page_ws_url,
                        max_size=50 * 1024 * 1024,
                        ping_interval=20,
                    )
                    break
                except Exception:
                    if _attempt == 2:
                        raise
                    await asyncio.sleep(2 ** _attempt)
            self._connected = True
            self._recv_task = asyncio.create_task(self._receive_loop())
            logger.info(f"CDP connected: {self._page_ws_url}")
            return True

        except Exception as e:
            logger.error(f"CDP connection failed: {e}")
            return False

    async def disconnect(self):
        self._connected = False
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def send_command(self, method: str, params: dict = None, timeout: float = 30.0) -> dict:
        """Send a CDP command and wait for the response."""
        if not self._connected or not self._ws:
            raise ConnectionError("Not connected to CDP")

        self._msg_id += 1
        msg_id = self._msg_id
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = future

        msg = {"id": msg_id, "method": method}
        if params:
            msg["params"] = params

        t0 = time.monotonic()
        await self._ws.send(json.dumps(msg))
        result = await asyncio.wait_for(future, timeout=timeout)
        logger.debug("CDP %s elapsed_ms=%.1f", method, (time.monotonic() - t0) * 1000)
        return result

    def add_event_listener(self, listener: Callable[[dict], None]):
        """Subscribe to raw CDP event messages (messages without request IDs)."""
        self._event_listeners.append(listener)

    async def _receive_loop(self):
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                    msg_id = msg.get("id")
                    if msg_id and msg_id in self._pending:
                        future = self._pending.pop(msg_id)
                        if "error" in msg:
                            future.set_exception(Exception(msg["error"].get("message", str(msg["error"]))))
                        else:
                            future.set_result(msg.get("result", {}))
                    elif msg.get("method"):
                        for listener in self._event_listeners:
                            try:
                                maybe_coro = listener(msg)
                                if asyncio.iscoroutine(maybe_coro):
                                    _t = asyncio.create_task(
                                        maybe_coro, name="cdp-event-listener",
                                    )
                                    self._bg_tasks.add(_t)
                                    _t.add_done_callback(self._bg_tasks.discard)
                            except Exception:
                                continue
                except json.JSONDecodeError:
                    continue
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"CDP receive error: {e}")
            self._connected = False


class BrowserController:
    """
    High-level browser control combining CDP and Playwright.
    Registered as an orchestrator skill for the agent to use.
    """

    def __init__(self):
        self._cdp = CDPConnection()
        self._playwright = None
        self._browser = None
        self._page = None
        self._aria_refs: dict[str, dict] = {}
        self._console_logs: list[dict] = []
        self._console_listener_attached = False
        self._network_log: list[dict] = []
        self._network_monitoring = False
        self._max_network_log = 500
        # PR 7 gap-fill: tracing / HAR / download bookkeeping
        self._tracing_active = False
        self._tracing_name: str = ""
        self._har_active = False
        self._har_context = None
        self._har_prev_page = None
        self._har_path: Optional[Path] = None
        # Session video recording (CDP screencast) bookkeeping
        self._recording: Optional[dict] = None
        self._recording_listener: Optional[Callable[[dict], object]] = None
        # Native JavaScript dialog bookkeeping. ``_pending_dialog`` is the
        # one currently blocking the renderer (manual policy only);
        # ``_dialog_log`` is the audit trail every endpoint result drains
        # so a dialog is never handled behind the model's back.
        self._dialog_policy: str = DEFAULT_DIALOG_POLICY
        self._dialog_accept_text: str = ""
        self._dialog_manual_timeout_s: float = DIALOG_MANUAL_TIMEOUT_S
        self._pending_dialog: Optional[dict] = None
        self._dialog_log: list[dict] = []
        self._dialog_seq = 0
        self._dialog_listener: Optional[Callable[[dict], object]] = None
        self._pw_dialog_handler: Optional[Callable] = None
        # Background tasks (dialog watchdogs). Held strongly with a
        # done-callback discard: the event loop keeps tasks only weakly,
        # so a bare create_task can be collected mid-flight.
        self._bg_tasks: set = set()
        # CDP target id of the tab this controller is currently driving.
        self._attached_target_id: str = ""

    @property
    def connected(self) -> bool:
        return self._cdp.connected

    async def initialize(self) -> bool:
        """Connect to Chrome CDP and optionally Playwright. Auto-launches Chrome if needed.

        ``prefer_page=True`` is load-bearing, not a preference. ``connect()``
        defaults to the ``/json/version`` endpoint, which is the BROWSER
        target, and the browser target implements neither ``Page`` nor
        ``Runtime`` nor ``Accessibility`` nor ``Network``. Every CDP-domain
        endpoint on this controller was therefore dead on a default
        connection, and measurably so:

          * ``snapshot`` (Accessibility.getFullAXTree), the ARIA tree the
            agent needs to address elements, returned success=False on
            every call. It has no Playwright fallback, so it never worked.
          * ``get_console_logs`` always returned zero entries, because
            ``Runtime.enable``/``Log.enable`` failed inside a try/except
            that logged at debug and moved on.
          * ``network_monitor_start`` raised "'Network.enable' wasn't
            found" out of the skill.
          * ``get_page_pdf`` and the CDP-only ``screenshot`` path failed
            the same way.

        Playwright masked this for click/type/navigate/screenshot, which is
        why it survived: the endpoints with a Playwright path worked and
        the ones without silently did not. ``_screencast_cdp`` already
        opened its own page-level socket to work around it for recording;
        this fixes it once, for every endpoint.

        Browser-domain commands still work over a page socket, so
        ``Target.getTargets``, ``Browser.setDownloadBehavior`` and
        ``Network.getAllCookies`` are unaffected. ``connect`` falls back to
        the browser endpoint when Chrome has no page target yet, so a
        freshly launched browser with no tab still connects.
        """
        cdp_ok = await self._cdp.connect(prefer_page=True)
        if not cdp_ok:
            launched = await self._auto_launch_chrome()
            if launched:
                await asyncio.sleep(2.0)
                cdp_ok = await self._cdp.connect(prefer_page=True)
            if not cdp_ok:
                logger.warning("CDP not available — browser control disabled.")
                return False
        if not self._cdp.is_page_target:
            logger.warning(
                "CDP attached to the browser target, not a tab: Chrome exposes no "
                "page target yet. snapshot, get_console_logs, network_log and "
                "get_page_pdf will fail until a tab exists."
            )

        if not self._console_listener_attached:
            self._attach_cdp_listeners(self._cdp)
            self._console_listener_attached = True
        self._attached_target_id = getattr(self._cdp, "target_id", "") or ""
        try:
            await self._cdp.send_command("Runtime.enable")
            await self._cdp.send_command("Log.enable")
            # Page.enable is what delivers Page.javascriptDialogOpening.
            # Without it a blocking alert() is invisible to this process
            # and every later Runtime.evaluate simply never returns.
            await self._cdp.send_command("Page.enable")
        except Exception as e:
            logger.debug(f"CDP event channels setup skipped: {e}")

        try:
            from playwright.async_api import async_playwright
            pw = await async_playwright().start()
            # Store the driver so close() can stop() it cleanly. The
            # previous code only kept a local `pw`, leaking the
            # `Playwright` async driver subprocess for the entire FERAL
            # process lifetime.
            self._playwright = pw
            self._browser = await pw.chromium.connect_over_cdp(
                f"http://{CDP_HOST}:{CDP_PORT}",
            )
            contexts = self._browser.contexts
            if contexts:
                pages = contexts[0].pages
                self._page = pages[0] if pages else await contexts[0].new_page()
            else:
                ctx = await self._browser.new_context()
                self._page = await ctx.new_page()
            # Playwright and CDP each pick a "first" page independently,
            # and there is no guarantee they pick the SAME tab. When they
            # disagree, selector actions (Playwright) and evaluate /
            # snapshot / screenshot (CDP) drive two different tabs while
            # every result reads like success. Align them on the tab the
            # CDP socket actually holds.
            if self._attached_target_id:
                aligned = await self._playwright_page_for_target(
                    self._attached_target_id
                )
                if aligned is not None:
                    self._page = aligned
                else:
                    logger.debug(
                        "No Playwright page matches CDP target %s; leaving the "
                        "Playwright page as-is.", self._attached_target_id,
                    )
            # Playwright auto-dismisses every dialog on a page that has no
            # `dialog` listener, and it does so before our CDP handler can
            # look at it. Registering a listener is what hands dialog
            # policy back to this controller.
            self._install_pw_dialog_handler()
            logger.info("Playwright connected via CDP")
        except Exception as e:
            # Roll back any partial driver start so we don't leak it on
            # the CDP-only path either.
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None
            logger.info(f"Playwright not available (CDP-only mode): {e}")

        try:
            await self.restore_cookies()
        except Exception:
            pass

        return True

    async def _auto_launch_chrome(self) -> bool:
        """Try to launch Chrome/Chromium with remote debugging enabled."""
        import platform
        system = platform.system()

        candidates = []
        if system == "Darwin":
            candidates = [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
                "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            ]
        elif system == "Linux":
            for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
                p = shutil.which(name)
                if p:
                    candidates.append(p)
        else:
            for name in ("chrome.exe", "chromium.exe"):
                p = shutil.which(name)
                if p:
                    candidates.append(p)

        chrome_bin = None
        for c in candidates:
            if os.path.isfile(c):
                chrome_bin = c
                break

        if not chrome_bin:
            logger.warning("No Chrome/Chromium binary found for auto-launch")
            return False

        from config.loader import feral_home
        profile_dir = str(feral_home() / "chrome-profile")

        args = [
            chrome_bin,
            f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ]

        try:
            import subprocess
            self._chrome_proc = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info(f"Auto-launched Chrome (pid={self._chrome_proc.pid}) on port {CDP_PORT}")
            return True
        except Exception as e:
            logger.error(f"Chrome auto-launch failed: {e}")
            return False

    async def list_tabs(self) -> list[dict]:
        """List all open browser tabs via CDP HTTP API."""
        import aiohttp
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://{CDP_HOST}:{CDP_PORT}/json/list") as resp:
                    tabs = await resp.json()
                    return [
                        {"id": t.get("id"), "title": t.get("title", ""), "url": t.get("url", "")}
                        for t in tabs if t.get("type") == "page"
                    ]
        except Exception as e:
            logger.warning(f"Failed to list tabs: {e}")
            return []

    async def switch_tab(self, tab_id: str) -> dict:
        """Activate a tab AND move this controller onto it.

        The bug this fixes: the old implementation only asked Chrome to
        bring the tab to the front. ``self._page`` (Playwright) and
        ``self._cdp`` (the page-level websocket) both stayed bound to the
        ORIGINAL tab, so every endpoint called afterwards, including
        ``snapshot``, ``get_page_text``, ``click`` and ``screenshot``,
        silently acted on the tab the user was no longer looking at and
        reported success. Wrong-target-but-successful is the worst
        failure mode a driver can have, because nothing above it can tell.

        Refusals (each returns ``success: false`` with the reason):
          * no such tab id, or the tab is not a page target;
          * a screencast recording is in flight on the shared CDP socket
            (re-attaching would sever it mid-recording; stop_recording
            first);
          * a HAR session is active (``self._page`` belongs to the HAR
            context; har_stop first).
        """
        tab_id = str(tab_id or "").strip()
        if not tab_id:
            return {"success": False, "error": "tab_id is required (get one from list_tabs)."}
        import aiohttp
        activated = False
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"http://{CDP_HOST}:{CDP_PORT}/json/activate/{tab_id}"
                ) as resp:
                    activated = resp.status == 200
                    if not activated:
                        body = (await resp.text())[:200]
                        return {
                            "success": False,
                            "tab_id": tab_id,
                            "error": (
                                f"Chrome refused to activate tab {tab_id!r} "
                                f"(HTTP {resp.status}): {body}"
                            ),
                        }
        except Exception as e:
            logger.warning(f"Failed to switch tab: {e}")
            return {"success": False, "tab_id": tab_id, "error": str(e)}

        attached = await self.attach_to_tab(tab_id)
        attached["activated"] = activated
        return attached

    async def attach_to_tab(self, tab_id: str) -> dict:
        """Re-point the CDP socket and the Playwright page at one tab.

        Both halves must move together. Moving only one leaves selector
        actions on one tab and evaluate/snapshot/screenshot on another,
        which is a harder bug to see than either half failing outright.
        """
        tab_id = str(tab_id or "").strip()
        if not tab_id:
            return {"success": False, "error": "tab_id is required."}
        if self._recording is not None and not self._recording.get("owns_cdp"):
            return {
                "success": False,
                "tab_id": tab_id,
                "error": (
                    f"Recording {self._recording['recording_id']} is using this "
                    "CDP socket; switching tabs would sever the screencast. "
                    "Call stop_recording first."
                ),
            }
        if getattr(self, "_har_active", False):
            return {
                "success": False,
                "tab_id": tab_id,
                "error": (
                    "A HAR session owns the active page. Call har_stop before "
                    "switching tabs, or the HAR would capture the wrong tab."
                ),
            }

        if self._attached_target_id == tab_id and self._cdp.connected:
            page_ok = await self._attach_playwright_page(tab_id)
            return {
                "success": True, "tab_id": tab_id, "already_attached": True,
                "playwright_attached": page_ok,
                **(await self._attached_identity()),
            }

        host = getattr(self._cdp, "_host", CDP_HOST)
        port = getattr(self._cdp, "_port", CDP_PORT)
        fresh = CDPConnection(host=host, port=port)
        if not await fresh.connect(target_id=tab_id):
            return {
                "success": False,
                "tab_id": tab_id,
                "error": (
                    f"No CDP page target {tab_id!r} on {host}:{port}. Call "
                    "list_tabs for current ids; a tab id goes stale when the "
                    "tab is closed."
                ),
            }

        old = self._cdp
        self._cdp = fresh
        self._console_listener_attached = False
        self._attach_cdp_listeners(fresh)
        self._console_listener_attached = True
        self._attached_target_id = fresh.target_id or tab_id
        # ARIA refs are backend node ids in the OLD tab's DOM. Reusing
        # them after a switch would resolve to nothing, or worse, to a
        # coincidentally-valid node in the new document.
        self._aria_refs.clear()
        try:
            await old.disconnect()
        except Exception as e:
            logger.debug("Old CDP socket did not close cleanly: %s", e)

        for domain in ("Runtime.enable", "Log.enable", "Page.enable"):
            try:
                await fresh.send_command(domain)
            except Exception as e:
                logger.debug("%s failed on the new tab socket: %s", domain, e)
        if self._network_monitoring:
            try:
                await fresh.send_command("Network.enable")
            except Exception as e:
                logger.debug("Network.enable failed on the new tab socket: %s", e)

        page_ok = await self._attach_playwright_page(tab_id)
        return {
            "success": True,
            "tab_id": tab_id,
            "already_attached": False,
            "playwright_attached": page_ok,
            "aria_refs_invalidated": True,
            **(await self._attached_identity()),
        }

    async def _attached_identity(self) -> dict:
        info = await self.get_page_info()
        return {"url": info.get("url", ""), "title": info.get("title", "")}

    async def _attach_playwright_page(self, tab_id: str) -> bool:
        """Point ``self._page`` at the Playwright page for ``tab_id``."""
        if not self._browser:
            return False
        page = await self._playwright_page_for_target(tab_id)
        if page is None:
            logger.warning(
                "CDP moved to tab %s but no Playwright page matches it; "
                "selector-based endpoints would still drive the old tab, so "
                "the Playwright page has been dropped and those endpoints "
                "will use the CDP fallback.", tab_id,
            )
            self._page = None
            return False
        self._page = page
        self._install_pw_dialog_handler()
        return True

    async def _playwright_page_for_target(self, tab_id: str):
        """Find the Playwright ``Page`` whose CDP target id is ``tab_id``.

        Identity comes from ``Target.getTargetInfo`` over a per-page CDP
        session, not from a URL comparison: two tabs on the same URL are
        completely ordinary, and matching them by URL would pick the
        wrong one without any signal that it had.
        """
        if not self._browser:
            return None
        pages = []
        try:
            for ctx in self._browser.contexts:
                pages.extend(ctx.pages)
        except Exception as e:
            logger.debug("Could not enumerate Playwright pages: %s", e)
            return None
        for page in pages:
            try:
                if page.is_closed():
                    continue
                session = await page.context.new_cdp_session(page)
                try:
                    info = await session.send("Target.getTargetInfo")
                finally:
                    try:
                        await session.detach()
                    except Exception:
                        pass
                if str((info.get("targetInfo") or {}).get("targetId")) == tab_id:
                    return page
            except Exception as e:
                logger.debug("Target id probe failed for a Playwright page: %s", e)
                continue
        return None

    async def new_tab(self, url: str = "about:blank", activate: bool = True) -> dict:
        """Open a new browser tab and (by default) move the controller onto it.

        PUT, not GET. Chrome stopped honouring ``GET /json/new`` in M111 and
        now answers it with the plain-text body "Using unsafe HTTP verb GET
        to invoke /json/new. This action supports only PUT verb." The old
        GET therefore failed on every current Chrome, and it failed as a
        JSON decode error swallowed into ``return None``, so the endpoint
        reported "no tab id" rather than "your browser rejected the verb",
        and nothing above could tell the difference between that and a
        browser that was not running.

        ``activate`` defaults to True because "open a new tab" almost
        always means "and work in it". With ``activate=False`` the tab is
        opened but every other endpoint keeps acting on the current tab,
        which is what this endpoint used to do unconditionally.
        """
        import aiohttp
        tab_id = ""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.put(
                    f"http://{CDP_HOST}:{CDP_PORT}/json/new?{url}"
                ) as resp:
                    body = await resp.text()
                    if resp.status >= 400:
                        logger.warning(
                            "Failed to open new tab: CDP answered %s: %s",
                            resp.status, body[:200],
                        )
                        return {
                            "success": False, "tab_id": "", "url": url,
                            "error": f"Chrome answered {resp.status}: {body[:200]}",
                        }
                    tab_id = str(json.loads(body).get("id") or "")
        except Exception as e:
            logger.warning(f"Failed to open new tab: {e}")
            return {"success": False, "tab_id": "", "url": url, "error": str(e)}

        if not tab_id:
            return {
                "success": False, "tab_id": "", "url": url,
                "error": "Chrome opened a tab but reported no target id.",
            }
        result = {"success": True, "tab_id": tab_id, "url": url, "attached": False}
        # Only re-attach if there is a live connection to move. A fresh
        # controller that has never connected has nothing to switch.
        if activate and self._cdp.connected:
            attached = await self.attach_to_tab(tab_id)
            result["attached"] = bool(attached.get("success"))
            if not attached.get("success"):
                result["attach_error"] = attached.get("error", "")
            else:
                result["ready_state"] = await self._wait_for_document_ready()
                result["title"] = (await self._attached_identity()).get("title", "")
        return result

    async def _wait_for_document_ready(self, timeout_s: float = 10.0) -> str:
        """Poll ``document.readyState`` until the new tab is usable.

        ``PUT /json/new`` answers the moment the target exists, long
        before the document parses. Acting on that gap is not a
        theoretical race: a drag issued straight after new_tab returned
        SUCCESS and moved nothing, because the page's own dragstart
        listener had not been attached yet. A success that did nothing is
        the failure mode this whole lane is about, so new_tab waits.
        """
        deadline = time.monotonic() + max(0.5, float(timeout_s))
        state = ""
        while time.monotonic() < deadline:
            try:
                out = await self._cdp.send_command(
                    "Runtime.evaluate",
                    {"expression": "document.readyState", "returnByValue": True},
                    timeout=5.0,
                )
                state = str((out.get("result") or {}).get("value") or "")
            except Exception as e:
                logger.debug("readyState poll failed: %s", e)
                state = ""
            if state == "complete":
                return state
            await asyncio.sleep(0.1)
        logger.info(
            "Tab did not reach readyState=complete within %.0fs (last: %r).",
            timeout_s, state,
        )
        return state or "unknown"

    async def close_tab(self, tab_id: str) -> dict:
        """Close a browser tab by its CDP target id.

        Closing the tab the controller is attached to leaves it driving a
        dead socket, so this re-attaches to another open tab and says
        which one in ``reattached_to``. If no tab is left, every
        subsequent endpoint fails loudly rather than acting on nothing.
        """
        tab_id = str(tab_id or "").strip()
        if not tab_id:
            return {"success": False, "error": "tab_id is required (get one from list_tabs)."}
        was_attached = self._attached_target_id == tab_id
        import aiohttp
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"http://{CDP_HOST}:{CDP_PORT}/json/close/{tab_id}"
                ) as resp:
                    if resp.status != 200:
                        body = (await resp.text())[:200]
                        return {
                            "success": False, "tab_id": tab_id,
                            "error": f"Chrome refused to close tab {tab_id!r} "
                                     f"(HTTP {resp.status}): {body}",
                        }
        except Exception as e:
            logger.warning(f"Failed to close tab: {e}")
            return {"success": False, "tab_id": tab_id, "error": str(e)}

        result = {"success": True, "tab_id": tab_id, "closed": True,
                  "was_active_tab": was_attached, "reattached_to": ""}
        if not was_attached:
            return result
        # Chrome needs a beat to retire the target before /json stops
        # listing it; re-attaching too early can land back on the corpse.
        await asyncio.sleep(0.2)
        for tab in await self.list_tabs():
            if tab.get("id") and tab["id"] != tab_id:
                attached = await self.attach_to_tab(tab["id"])
                if attached.get("success"):
                    result["reattached_to"] = tab["id"]
                    result["url"] = attached.get("url", "")
                return result
        self._attached_target_id = ""
        result["error"] = (
            "Closed the tab this controller was driving and no other tab is "
            "open. Call new_tab before any further browser action."
        )
        return result

    # ── Native JavaScript dialogs ────────────────────────────────────
    #
    # alert(), confirm(), prompt() and beforeunload freeze the renderer
    # until a CDP client answers. Before this, nothing in this controller
    # answered them on the CDP path, so a page that popped a confirm went
    # from "loading" to permanently unresponsive with no error anywhere:
    # every later Runtime.evaluate just timed out. A blocked page is
    # indistinguishable from a hung one.
    #
    # Policy, and why the default is dismiss:
    #   dismiss (DEFAULT) - Cancel/stay. Safe for every dialog type.
    #   accept            - OK/leave. Opt-in only, because "Delete
    #                       account?" is a confirm() and auto-accepting
    #                       it destroys data on the user's behalf with no
    #                       one having read the question.
    #   manual            - leave it open for handle_dialog. The page
    #                       stays blocked meanwhile, so a watchdog
    #                       dismisses it after dialog_manual_timeout_s and
    #                       records that it did.
    # Every dialog is logged either way, and the log is drained onto the
    # next endpoint result as `dialog_events`.

    def _attach_cdp_listeners(self, cdp) -> None:
        """Wire the console and dialog listeners onto a CDP socket."""
        cdp.add_event_listener(self._on_cdp_event)
        self._dialog_listener = self._on_cdp_dialog_event
        cdp.add_event_listener(self._dialog_listener)

    def _install_pw_dialog_handler(self) -> None:
        """Take dialog control back from Playwright's auto-dismiss.

        Playwright dismisses every dialog on a page with no ``dialog``
        listener, and it does so before our CDP handler sees it, so
        without this the policy would be permanently stuck on "dismiss"
        and handle_dialog could never accept anything. Playwright's own
        default is not even uniform: with no listener it DISMISSES an
        alert/confirm/prompt but ACCEPTS a beforeunload, so "leave the
        default alone" would silently mean "walk away from the user's
        unsaved work".

        The handler must also be the thing that ANSWERS, not just
        observes. Answering over this controller's raw CDP socket instead
        was tried and is wrong: Playwright's Dialog additionally runs
        ``frameAbortedNavigation`` when a beforeunload is dismissed, and
        that is what settles the ``page.goto`` waiting on it. Measured
        against Chrome 151, answering behind Playwright's back left the
        goto pending forever and wedged the page's protocol channel, with
        the tab permanently frozen.
        """
        if not self._page:
            return
        try:
            if self._pw_dialog_handler is not None:
                try:
                    self._page.remove_listener("dialog", self._pw_dialog_handler)
                except Exception:
                    pass
            handler = self._on_pw_dialog
            self._page.on("dialog", handler)
            self._pw_dialog_handler = handler
        except Exception as e:
            logger.debug("Could not install the Playwright dialog handler: %s", e)

    def _is_duplicate_dialog(self, entry: dict) -> bool:
        """True when this is the SAME dialog arriving down a second path.

        Chrome delivers ``Page.javascriptDialogOpening`` to every attached
        CDP client, and Playwright is one, so one dialog can surface twice
        inside this process. Answering it twice fails the second time with
        "No dialog is showing".

        The test is "a matching dialog is STILL OPEN", not "a matching one
        was seen recently". A time window was tried first and was wrong in
        a way that mattered: a page that raises the same beforeunload on
        two navigation attempts a few hundred milliseconds apart had its
        second dialog silently classified as an echo of the first, so
        nothing ever answered it and the tab hung with the modal up. The
        pending check cannot make that mistake, because Chrome shows at
        most one dialog per page: if one with this identity is open right
        now, this delivery is that same dialog.
        """
        pending = self._pending_dialog
        return bool(
            pending
            and pending.get("pending")
            and pending.get("type") == entry.get("type")
            and pending.get("message") == entry.get("message")
        )

    def _record_dialog(self, entry: dict) -> dict:
        self._dialog_seq += 1
        entry["dialog_id"] = f"dlg{self._dialog_seq}"
        entry["seen_at"] = round(time.time(), 3)
        entry.setdefault("handled", False)
        entry.setdefault("action", "")
        self._dialog_log.append(entry)
        if len(self._dialog_log) > DIALOG_LOG_MAX:
            self._dialog_log = self._dialog_log[-DIALOG_LOG_MAX:]
        logger.info(
            "JavaScript dialog (%s) on the page: %r -> policy=%s",
            entry.get("type"), str(entry.get("message", ""))[:120],
            self._dialog_policy,
        )
        return entry

    def _spawn(self, coro, name: str) -> None:
        """Run a coroutine as a strongly-referenced background task."""
        task = asyncio.ensure_future(coro)
        try:
            task.set_name(name)
        except AttributeError:
            pass
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _on_cdp_dialog_event(self, msg: dict) -> None:
        """Answer (or park) a dialog seen on the raw CDP socket."""
        method = msg.get("method", "")
        if method == "Page.javascriptDialogClosed":
            # Fires whoever answered, including another attached client or
            # Playwright's driver. Marking the entry as well as clearing
            # the handle keeps the log from reporting a dialog as still
            # blocking the page after it has plainly gone.
            entry = self._pending_dialog
            self._pending_dialog = None
            if entry is not None and entry.get("pending"):
                entry["pending"] = False
                if not entry.get("handled"):
                    entry["handled"] = True
                    entry["handled_by"] = "closed_by_another_cdp_client"
                    entry["action"] = (
                        "accept" if (msg.get("params") or {}).get("result")
                        else "dismiss"
                    )
            return
        if method != "Page.javascriptDialogOpening":
            return
        # Playwright owns a dialog on a tab it has a page for. It must be
        # the one to answer it: for a beforeunload raised by page.goto,
        # only Playwright's own Dialog also runs frameAbortedNavigation,
        # and without that its goto stays pending forever and the page's
        # protocol channel wedges. Measured against Chrome 151: answering
        # over this socket instead left the tab permanently frozen.
        if self._page is not None and self._pw_dialog_handler is not None:
            return
        params = msg.get("params") or {}
        candidate = {
            "type": params.get("type", "alert"),
            "message": str(params.get("message", "")),
            "default_value": str(params.get("defaultPrompt", "")),
            "url": str(params.get("url", "")),
            "via": "cdp",
        }
        if self._is_duplicate_dialog(candidate):
            return
        entry = self._record_dialog(candidate)
        entry["pending"] = True
        self._pending_dialog = entry
        if self._dialog_policy == "manual":
            self._spawn(self._dialog_watchdog(entry["dialog_id"]), "dialog-watchdog")
            return
        await self._answer_pending(
            self._dialog_policy, self._dialog_accept_text, reason="policy"
        )

    async def _on_pw_dialog(self, dialog) -> None:
        """Answer (or park) a dialog delivered by the Playwright driver."""
        candidate = {
            "type": getattr(dialog, "type", "alert"),
            "message": str(getattr(dialog, "message", "")),
            "default_value": str(getattr(dialog, "default_value", "") or ""),
            "url": self._page.url if self._page else "",
            "via": "playwright",
        }
        if self._is_duplicate_dialog(candidate):
            return
        entry = self._record_dialog(candidate)
        entry["pending"] = True
        entry["_dialog"] = dialog
        self._pending_dialog = entry
        if self._dialog_policy == "manual":
            self._spawn(self._dialog_watchdog(entry["dialog_id"]), "dialog-watchdog")
            return
        await self._answer_pending(
            self._dialog_policy, self._dialog_accept_text, reason="policy"
        )

    async def _dialog_watchdog(self, dialog_id: str) -> None:
        """Dismiss a manually-parked dialog that nobody ever answered.

        Without this, ``dialog_policy=manual`` plus a forgotten
        ``handle_dialog`` is a permanently frozen tab, which is the exact
        symptom this whole feature exists to remove.
        """
        try:
            await asyncio.sleep(max(1.0, float(self._dialog_manual_timeout_s)))
        except asyncio.CancelledError:
            return
        pending = self._pending_dialog
        if not pending or pending.get("dialog_id") != dialog_id:
            return
        logger.warning(
            "Dialog %s was left open for %.0fs under dialog_policy=manual; "
            "dismissing it so the page stops being blocked.",
            dialog_id, self._dialog_manual_timeout_s,
        )
        await self._answer_pending("dismiss", "", reason="manual_timeout")

    async def _answer_pending(self, action: str, prompt_text: str, reason: str) -> dict:
        """Actually answer the open dialog and mark the log entry."""
        entry = self._pending_dialog
        if entry is None:
            return {"success": False, "error": "No dialog is currently open."}
        accept = action == "accept"
        dialog = entry.pop("_dialog", None)
        # promptText belongs to a prompt() and nothing else. Sending it on
        # an alert, a confirm or a beforeunload is not harmless: passing an
        # explicit empty promptText while accepting a beforeunload left
        # Chrome 151 never answering, so page.goto sat out its full 30s
        # timeout and the tab was left blocked. Omitting it entirely on
        # every non-prompt dialog is what makes accept work.
        is_prompt = entry.get("type") == "prompt"
        text = prompt_text or entry.get("default_value", "")
        params = {"accept": accept}
        if accept and is_prompt:
            params["promptText"] = text
        try:
            if dialog is not None:
                # Bounded, with a raw-socket fallback. An answer that never
                # returns would leave the tab frozen with no way for the
                # caller to find out, which is the failure mode this whole
                # feature exists to remove.
                if not accept:
                    answer = dialog.dismiss()
                elif is_prompt:
                    answer = dialog.accept(text)
                else:
                    answer = dialog.accept()
                try:
                    await asyncio.wait_for(answer, timeout=10.0)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Playwright did not answer dialog %s within 10s; "
                        "unblocking the page over the raw CDP socket.",
                        entry.get("dialog_id"),
                    )
                    await self._cdp.send_command(
                        "Page.handleJavaScriptDialog", params, timeout=10.0,
                    )
            else:
                await self._cdp.send_command(
                    "Page.handleJavaScriptDialog", params, timeout=10.0,
                )
        except Exception as e:
            entry["pending"] = False
            self._pending_dialog = None
            # "No dialog is showing" means someone else (Playwright's own
            # driver, or another attached CDP client) got there first. The
            # page is unblocked, which is the outcome that matters, so
            # this is a note rather than a failure.
            if "No dialog is showing" in str(e):
                entry["handled"] = True
                entry["handled_by"] = "already_answered_by_another_cdp_client"
                logger.debug("Dialog %s was already answered elsewhere.",
                             entry.get("dialog_id"))
                return {"success": True, "dialog": self._public_dialog(entry),
                        "note": "The dialog had already been answered by another "
                                "attached CDP client; the page is not blocked."}
            entry["error"] = str(e)
            logger.warning("Could not answer dialog %s: %s", entry.get("dialog_id"), e)
            return {"success": False, "error": f"Could not answer the dialog: {e}",
                    "dialog": self._public_dialog(entry)}
        entry["pending"] = False
        entry["handled"] = True
        entry["action"] = "accept" if accept else "dismiss"
        entry["handled_by"] = reason
        if accept and entry.get("type") == "prompt":
            entry["prompt_text"] = prompt_text or entry.get("default_value", "")
        self._pending_dialog = None
        return {"success": True, "dialog": self._public_dialog(entry)}

    @staticmethod
    def _public_dialog(entry: dict) -> dict:
        return {k: v for k, v in entry.items() if not k.startswith("_")}

    async def set_dialog_policy(
        self, policy: str = DEFAULT_DIALOG_POLICY, prompt_text: str = "",
        manual_timeout_s: float = DIALOG_MANUAL_TIMEOUT_S,
    ) -> dict:
        """Choose how alert/confirm/prompt/beforeunload are answered."""
        policy = str(policy or "").strip().lower()
        if policy not in DIALOG_POLICIES:
            return {
                "success": False,
                "error": f"policy must be one of {', '.join(DIALOG_POLICIES)}; got {policy!r}.",
            }
        try:
            timeout = max(1.0, min(float(manual_timeout_s), 600.0))
        except (TypeError, ValueError):
            timeout = DIALOG_MANUAL_TIMEOUT_S
        self._dialog_policy = policy
        self._dialog_accept_text = str(prompt_text or "")
        self._dialog_manual_timeout_s = timeout
        note = {
            "dismiss": "Dialogs are cancelled. confirm() reads false, prompt() null, "
                       "beforeunload keeps the page.",
            "accept": "DANGEROUS: every confirm() is answered OK without anyone "
                      "reading the question, including destructive ones.",
            "manual": (
                f"The page stays BLOCKED until handle_dialog is called. A watchdog "
                f"dismisses an unanswered dialog after {timeout:.0f}s."
            ),
        }[policy]
        return {"success": True, "policy": policy, "prompt_text": self._dialog_accept_text,
                "manual_timeout_s": timeout, "note": note}

    async def handle_dialog(self, action: str = "dismiss", prompt_text: str = "") -> dict:
        """Answer the dialog currently blocking the page.

        Only useful under ``dialog_policy=manual``; under the automatic
        policies the dialog has already been answered by the time any
        endpoint could run, and this reports that there is nothing open.
        """
        action = str(action or "").strip().lower()
        if action not in ("accept", "dismiss"):
            return {"success": False, "error": "action must be 'accept' or 'dismiss'."}
        if self._pending_dialog is None:
            return await self._answer_untracked(action, str(prompt_text or ""))
        return await self._answer_pending(action, str(prompt_text or ""), reason="manual")

    async def _answer_untracked(self, action: str, prompt_text: str) -> dict:
        """Blind-answer a dialog this controller never saw open.

        This is the escape hatch, and it is not hypothetical. A dialog
        that was ALREADY on screen when the controller attached to the
        tab produced its ``Page.javascriptDialogOpening`` before anyone
        was listening, so there is nothing tracked and no CDP command to
        ask "is one showing". Refusing here would leave the agent staring
        at a tab that answers nothing, with no way out, which is the
        exact trap this feature exists to remove. Chrome answers "No
        dialog is showing" when there is genuinely nothing to clear, and
        that is reported truthfully rather than as an error.
        """
        params = {"accept": action == "accept"}
        if action == "accept" and prompt_text:
            params["promptText"] = prompt_text
        try:
            await self._cdp.send_command(
                "Page.handleJavaScriptDialog", params, timeout=10.0,
            )
        except Exception as e:
            if "No dialog is showing" in str(e):
                return {
                    "success": False,
                    "no_dialog_open": True,
                    "policy": self._dialog_policy,
                    "error": (
                        "Chrome reports no dialog is open, so there was nothing to "
                        f"{action}. Dialogs are answered automatically under "
                        f"dialog_policy={self._dialog_policy!r}; set it to 'manual' "
                        "if you need to answer one by hand, and call get_dialogs to "
                        "see what has already been answered."
                    ),
                }
            return {"success": False, "error": f"Could not clear the dialog: {e}"}
        entry = self._record_dialog({
            "type": "unknown",
            "message": "(dialog was already open before this controller attached)",
            "default_value": "",
            "url": "",
            "via": "blind",
        })
        entry["handled"] = True
        entry["action"] = action
        entry["handled_by"] = "blind_clear"
        return {
            "success": True,
            "untracked": True,
            "dialog": self._public_dialog(entry),
            "note": (
                "Cleared a dialog that was already open before this controller "
                "started watching, so its text was never captured. Take a "
                "screenshot before answering next time if the wording matters."
            ),
        }

    async def get_dialogs(self, limit: int = 20, clear: bool = False) -> dict:
        """Return the dialogs this session has seen, newest last."""
        try:
            bounded = max(1, min(int(limit), DIALOG_LOG_MAX))
        except (TypeError, ValueError):
            bounded = 20
        entries = [self._public_dialog(e) for e in self._dialog_log[-bounded:]]
        if clear:
            self._dialog_log.clear()
        result = {
            "success": True,
            "count": len(entries),
            "dialogs": entries,
            "policy": self._dialog_policy,
            "pending": self._public_dialog(self._pending_dialog)
            if self._pending_dialog else None,
        }
        blocked = await self._renderer_is_blocked()
        result["page_blocked"] = blocked
        if blocked:
            result["blocked_hint"] = (
                "The renderer is not answering, which is what a page with an "
                "unanswered modal dialog looks like from here. It may be a dialog "
                "that was already open before this controller attached, so nothing "
                "was recorded about it. Call handle_dialog with action=dismiss to "
                "clear it blind, then take a screenshot to see where you are."
            )
        return result

    async def _renderer_is_blocked(self) -> bool:
        """True when the renderer stops answering, i.e. a modal is up.

        There is no CDP command that asks "is a dialog showing", so the
        only honest signal is that the renderer has stopped evaluating.
        A blocked page and a hung page look identical from outside, which
        is exactly why the agent needs to be TOLD rather than left to
        infer it from a timeout it cannot distinguish from a slow page.
        """
        if not self._cdp.connected:
            return False
        try:
            await self._cdp.send_command(
                "Runtime.evaluate",
                {"expression": "1", "returnByValue": True},
                timeout=3.0,
            )
            return False
        except asyncio.TimeoutError:
            return True
        except Exception:
            return False

    def _drain_dialog_events(self) -> list[dict]:
        """Unreported dialogs, for attaching to the next endpoint result."""
        fresh = [e for e in self._dialog_log if not e.get("_reported")]
        for entry in fresh:
            entry["_reported"] = True
        return [self._public_dialog(e) for e in fresh]

    def _on_cdp_event(self, event: dict):
        """Capture console/log events so the agent can inspect browser errors."""
        method = event.get("method", "")
        params = event.get("params", {}) or {}

        if method == "Runtime.consoleAPICalled":
            args = []
            for arg in params.get("args", []):
                val = arg.get("value")
                if val is None:
                    val = arg.get("description") or arg.get("type")
                if val is not None:
                    args.append(str(val))
            self._append_console_log({
                "source": "runtime",
                "level": params.get("type", "log"),
                "text": " ".join(args).strip(),
                "timestamp": params.get("timestamp"),
            })
        elif method == "Log.entryAdded":
            entry = params.get("entry", {}) or {}
            self._append_console_log({
                "source": "log",
                "level": entry.get("level", "info"),
                "text": entry.get("text", ""),
                "timestamp": entry.get("timestamp"),
                "url": entry.get("url"),
            })
        elif method == "Network.requestWillBeSent" and self._network_monitoring:
            self._network_log.append({
                "url": params.get("request", {}).get("url", ""),
                "method": params.get("request", {}).get("method", ""),
                "type": params.get("type", ""),
                "timestamp": params.get("timestamp", 0),
            })
            if len(self._network_log) > self._max_network_log:
                self._network_log = self._network_log[-self._max_network_log:]

    def _append_console_log(self, entry: dict):
        if not entry.get("text"):
            return
        self._console_logs.append(entry)
        if len(self._console_logs) > 500:
            self._console_logs = self._console_logs[-500:]

    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> dict:
        """Navigate and verify the browser actually loaded ``url``.

        Previously the CDP-only branch returned ``success: True`` immediately
        after ``Page.navigate`` without observing any page event — so if Chrome
        was frozen, the tab was unchanged, or the navigation was blocked, logs
        still said "navigated successfully" while the user saw nothing move.

        The new contract:
          * Playwright path uses ``page.goto`` (unchanged; it already waits).
          * CDP path subscribes to ``Page.loadEventFired`` BEFORE issuing the
            navigate, polls the current URL as a fallback signal, and returns
            ``success: False`` on timeout with an actionable error.
          * If the browser is not connected at all, we fail loudly instead of
            faking success.
        """
        try:
            if wait_until not in ("load", "domcontentloaded", "networkidle", "commit"):
                wait_until = "domcontentloaded"

            if self._page:
                resp = await self._page.goto(url, wait_until=wait_until, timeout=30000)
                status = resp.status if resp else 0
                title = await self._get_title()
                final_url = ""
                try:
                    final_url = self._page.url or url
                except Exception:
                    final_url = url
                return {
                    "success": True,
                    "url": final_url or url,
                    "requested_url": url,
                    "status": status,
                    "title": title,
                }

            if not self._cdp.connected:
                return {
                    "success": False,
                    "error": (
                        "Browser not running. Start Chrome with "
                        "--remote-debugging-port=9222 or install Playwright."
                    ),
                    "requested_url": url,
                }

            load_future: asyncio.Future = asyncio.get_event_loop().create_future()

            def _on_event(msg: dict):
                method = msg.get("method", "")
                if method == "Page.loadEventFired" and not load_future.done():
                    load_future.set_result(True)

            self._cdp.add_event_listener(_on_event)
            try:
                await self._cdp.send_command("Page.enable")
                await self._cdp.send_command("Page.navigate", {"url": url})
                try:
                    await asyncio.wait_for(load_future, timeout=15.0)
                except asyncio.TimeoutError:
                    return {
                        "success": False,
                        "error": f"Navigation to {url} did not fire Page.loadEventFired within 15s.",
                        "requested_url": url,
                    }
            finally:
                try:
                    self._cdp._event_listeners.remove(_on_event)
                except (ValueError, AttributeError):
                    pass

            final_url = url
            try:
                info = await self._cdp.send_command("Runtime.evaluate", {
                    "expression": "window.location.href",
                    "returnByValue": True,
                })
                final_url = info.get("result", {}).get("value", url) or url
            except Exception:
                pass

            title = await self._get_title()
            return {
                "success": True,
                "url": final_url,
                "requested_url": url,
                "status": 200,
                "title": title,
            }
        except Exception as e:
            return {"success": False, "error": str(e), "requested_url": url}

    async def _get_title(self) -> str:
        try:
            if self._page:
                return await self._page.title()
            r = await self._cdp.send_command("Runtime.evaluate", {
                "expression": "document.title", "returnByValue": True,
            })
            return r.get("result", {}).get("value", "")
        except Exception:
            return ""

    async def screenshot(self, full_page: bool = False) -> dict:
        """Capture a screenshot, resize and compress for VLM."""
        try:
            if self._page:
                raw = await self._page.screenshot(full_page=full_page, type="jpeg", quality=JPEG_QUALITY)
            else:
                result = await self._cdp.send_command("Page.captureScreenshot", {
                    "format": "jpeg", "quality": JPEG_QUALITY,
                })
                raw = base64.b64decode(result["data"])

            img_b64 = self._compress_image(raw)
            return {"success": True, "image_b64": img_b64, "format": "jpeg"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def snapshot(
        self,
        filter: str = "interactive",
        max_nodes: int = SNAPSHOT_DEFAULT_MAX_NODES,
        offset: int = 0,
        include_coordinates: bool = True,
    ) -> dict:
        """Get the ARIA accessibility tree, filtered and paginated.

        What this fixes: the tree used to be cut at a hard ``nodes[:500]``
        head slice with no filter, no offset and no signal that anything
        had been dropped. Chrome contributes hundreds of chrome-shell and
        text nodes before the page's own content, so on a real app the
        buttons the agent needed fell off the end of the list and were
        simply unreachable, with the result still reporting success.

        Now it filters to interactive roles by default, paginates over the
        FULL match list, and says ``TRUNCATED`` with the next offset in
        the tree text itself so the model reads it without inspecting
        fields.
        """
        mode = str(filter or "interactive").strip().lower()
        if mode not in ("interactive", "all"):
            return {
                "success": False,
                "error": f"filter must be 'interactive' or 'all'; got {filter!r}.",
            }
        try:
            limit = max(1, min(int(max_nodes), SNAPSHOT_MAX_MAX_NODES))
        except (TypeError, ValueError):
            limit = SNAPSHOT_DEFAULT_MAX_NODES
        try:
            start = max(0, int(offset))
        except (TypeError, ValueError):
            start = 0
        try:
            await self._cdp.send_command("DOM.enable")
            await self._cdp.send_command("Accessibility.enable")
            result = await self._cdp.send_command("Accessibility.getFullAXTree")
            nodes = result.get("nodes", [])
            self._aria_refs.clear()
            matched = [n for n in nodes if self._ax_node_matches(n, mode)]
            page = matched[start:start + limit]
            text = self._build_aria_text(page, ref_offset=start)
            # Resolve backend DOM node IDs to CSS selectors (and, when
            # asked, viewport coordinates) for the page we just emitted.
            await self._resolve_aria_selectors(
                include_coordinates=bool(include_coordinates)
            )
            text = self._annotate_aria_text(text)
            truncated = start + len(page) < len(matched)
            header = (
                f"{len(matched)} matching elements (filter={mode}), "
                f"showing {start}-{start + len(page)}."
            )
            if truncated:
                header += (
                    f"\nTRUNCATED: call snapshot again with offset="
                    f"{start + len(page)} for the next page, raise max_nodes, "
                    f"or narrow with find."
                )
            if mode == "interactive":
                header += (
                    "\n(filter=interactive: prose and layout nodes are omitted. "
                    "Use get_page_text to read content, or filter=all to see "
                    "every node.)"
                )
            ambiguous = sorted(
                ref for ref, info in self._aria_refs.items()
                if info.get("selector_unique") is False
            )
            if ambiguous:
                header += (
                    f"\nAMBIGUOUS SELECTORS ({len(ambiguous)}): "
                    f"{', '.join(ambiguous[:12])}"
                    f"{'...' if len(ambiguous) > 12 else ''} match more than one "
                    "element. Clicking them by selector would hit the first "
                    "match, which may not be the one listed; click_at with the "
                    "ref's reported x/y instead."
                )
            return {
                "success": True,
                "aria_tree": f"{header}\n{text}",
                "ref_count": len(self._aria_refs),
                "total_matched": len(matched),
                "total_nodes": len(nodes),
                "offset": start,
                "shown": len(page),
                "next_offset": start + len(page) if truncated else None,
                "truncated": truncated,
                "filter": mode,
                "ambiguous_refs": ambiguous,
                "refs": self._public_refs() if include_coordinates else {},
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _annotate_aria_text(self, text: str) -> str:
        """Append each ref's viewport centre to its line in the tree.

        This is the bridge that was missing: the snapshot named elements
        the agent could only reach through a selector, so when the
        selector failed (overlay, ambiguity, shadow DOM) there was no
        coordinate to fall back to and no way to get one without a second
        round trip. ``!ambiguous`` marks a ref whose selector matches more
        than one element, where the coordinate is the only safe route.
        """
        out = []
        for line in text.split("\n"):
            match = re.match(r"^(\s*)\[(ax\d+)\]", line)
            info = self._aria_refs.get(match.group(2)) if match else None
            if info:
                centre = info.get("center")
                if centre:
                    line += f" @{centre['x']:.0f},{centre['y']:.0f}"
                    if info.get("visible") is False:
                        line += " !offscreen"
                if info.get("selector_unique") is False:
                    line += " !ambiguous"
            out.append(line)
        return "\n".join(out)

    def _public_refs(self) -> dict:
        """Ref -> {selector, x, y, ...} so a failed click has a fallback.

        Without coordinates in the snapshot there is no bridge from a ref
        to click_at, so a selector-based click that fails (overlay,
        ambiguous selector, shadow DOM) is a dead end.
        """
        out = {}
        for ref, info in self._aria_refs.items():
            entry = {
                "role": info.get("role", ""),
                "name": info.get("name", ""),
                "selector": info.get("selector", ""),
                "selector_unique": info.get("selector_unique"),
            }
            centre = info.get("center")
            if centre:
                entry["x"] = centre["x"]
                entry["y"] = centre["y"]
                entry["visible"] = info.get("visible")
            out[ref] = entry
        return out

    @staticmethod
    def _ax_node_matches(node: dict, mode: str) -> bool:
        role = (node.get("role") or {}).get("value", "")
        if not role or role in SNAPSHOT_SKIP_ROLES:
            return False
        if node.get("ignored"):
            return False
        if mode == "all":
            return True
        if role in SNAPSHOT_INTERACTIVE_ROLES:
            return True
        # A node the page has explicitly made focusable or clickable is
        # interactive whatever its role says.
        for prop in node.get("properties") or []:
            if prop.get("name") == "focusable" and (prop.get("value") or {}).get("value"):
                return True
        return False

    # Runs inside the page against one element (via Runtime.callFunctionOn
    # on a resolved backend node) and returns a UNIQUE selector plus the
    # element's viewport geometry in a single round trip.
    #
    # Uniqueness is the point. The old resolver emitted `f"{tag}.{cls}"`
    # with no check at all, so `div.row` on a table of 40 rows became the
    # selector for every one of them and `querySelector` silently took the
    # first. A wrong-element click looks exactly like a successful one,
    # which made it the most dangerous defect in this file. Here the
    # candidate is verified in the page, and an nth-of-type path is built
    # from the element itself when the short form is not unique.
    _REF_RESOLVE_JS = """
    function () {
      var el = this;
      if (!el || el.nodeType !== 1) {
        el = el && el.parentElement ? el.parentElement : null;
      }
      if (!el) return null;
      function esc(v) { return String(v).replace(/["\\\\]/g, '\\\\$&'); }
      function unique(sel) {
        if (!sel) return false;
        try { return document.querySelectorAll(sel).length === 1; }
        catch (e) { return false; }
      }
      function pathFor(node) {
        var parts = [];
        while (node && node.nodeType === 1 && parts.length < 12) {
          if (node.id && unique('#' + esc(node.id))) {
            parts.unshift('#' + esc(node.id));
            break;
          }
          var tag = node.tagName.toLowerCase();
          var parent = node.parentElement;
          if (parent) {
            var same = Array.prototype.filter.call(
              parent.children, function (c) { return c.tagName === node.tagName; });
            if (same.length > 1) {
              tag += ':nth-of-type(' + (same.indexOf(node) + 1) + ')';
            }
          }
          parts.unshift(tag);
          node = parent;
        }
        return parts.join(' > ');
      }
      var tag = el.tagName.toLowerCase();
      var candidates = [];
      if (el.id) candidates.push('#' + esc(el.id));
      var testid = el.getAttribute('data-testid');
      if (testid) candidates.push('[data-testid="' + esc(testid) + '"]');
      var nm = el.getAttribute('name');
      if (nm) candidates.push(tag + '[name="' + esc(nm) + '"]');
      var aria = el.getAttribute('aria-label');
      if (aria) candidates.push(tag + '[aria-label="' + esc(aria) + '"]');
      var cls = (el.getAttribute('class') || '').split(/\\s+/).filter(Boolean)[0];
      if (cls) candidates.push(tag + '.' + esc(cls));
      candidates.push(tag);
      var selector = '';
      for (var i = 0; i < candidates.length; i++) {
        if (unique(candidates[i])) { selector = candidates[i]; break; }
      }
      var isUnique = true;
      if (!selector) {
        selector = pathFor(el);
        isUnique = unique(selector);
        if (!isUnique) { selector = candidates[0] || tag; }
      }
      var rect = el.getBoundingClientRect();
      var style = window.getComputedStyle(el);
      return {
        selector: selector,
        unique: isUnique,
        match_count: (function () {
          try { return document.querySelectorAll(selector).length; }
          catch (e) { return 0; }
        })(),
        tag: tag,
        x: rect.x + rect.width / 2,
        y: rect.y + rect.height / 2,
        width: rect.width,
        height: rect.height,
        visible: rect.width > 0 && rect.height > 0 &&
                 style.visibility !== 'hidden' && style.display !== 'none' &&
                 rect.bottom > 0 && rect.right > 0 &&
                 rect.top < (window.innerHeight || 0) &&
                 rect.left < (window.innerWidth || 0)
      };
    }
    """

    async def _resolve_one_ref(self, ref_id: str, info: dict) -> None:
        """Resolve a single ARIA ref in the page, geometry included."""
        backend_id = info.get("backend_id")
        try:
            resolved = await self._cdp.send_command(
                "DOM.resolveNode", {"backendNodeId": backend_id}, timeout=10.0,
            )
            object_id = (resolved.get("object") or {}).get("objectId")
            if not object_id:
                return
            try:
                out = await self._cdp.send_command("Runtime.callFunctionOn", {
                    "objectId": object_id,
                    "functionDeclaration": self._REF_RESOLVE_JS,
                    "returnByValue": True,
                }, timeout=10.0)
            finally:
                try:
                    await self._cdp.send_command(
                        "Runtime.releaseObject", {"objectId": object_id}, timeout=5.0,
                    )
                except Exception:
                    pass
        except Exception as e:
            logger.debug("In-page resolve failed for %s: %s", ref_id, e)
            return
        value = (out.get("result") or {}).get("value")
        if not isinstance(value, dict) or not value.get("selector"):
            return
        info["selector"] = value["selector"]
        info["selector_unique"] = bool(value.get("unique"))
        info["match_count"] = int(value.get("match_count") or 0)
        info["visible"] = bool(value.get("visible"))
        info["center"] = {"x": float(value.get("x", 0.0)), "y": float(value.get("y", 0.0))}
        info["size"] = {
            "width": float(value.get("width", 0.0)),
            "height": float(value.get("height", 0.0)),
        }

    async def _resolve_aria_selectors(self, include_coordinates: bool = True):
        """Resolve ARIA refs to CSS selectors.

        Two passes, in this order:

        1. An in-page pass (``DOM.resolveNode`` + ``Runtime.callFunctionOn``)
           that produces a VERIFIED-UNIQUE selector and the element's
           viewport centre in one round trip per ref, all refs gathered
           concurrently over the one socket. This is what makes a ref
           usable with ``click_at`` when the selector route fails.
        2. ``DOM.describeNode`` for anything pass 1 could not resolve
           (detached node, cross-process frame, no Runtime domain). That
           fallback is the original strategy and is dual-mode aware:
           a plain CSS form when the DOM offers one, otherwise a
           Playwright-only ``tag:has-text(...)`` plus a ``text_match``
           record so the CDP path can do a JS text scan instead of
           choking on the unknown pseudo.
        """
        if include_coordinates:
            pending = [
                (ref_id, info) for ref_id, info in self._aria_refs.items()
                if info.get("backend_id") and not info.get("selector")
            ]
            if pending:
                await asyncio.gather(
                    *(self._resolve_one_ref(ref_id, info) for ref_id, info in pending),
                    return_exceptions=True,
                )
        for ref_id, info in list(self._aria_refs.items()):
            backend_id = info.get("backend_id")
            if not backend_id or info.get("selector"):
                continue
            try:
                desc = await self._cdp.send_command("DOM.describeNode", {
                    "backendNodeId": backend_id, "depth": 0,
                })
                node = desc.get("node", {})
                tag = node.get("localName", "")
                attrs = node.get("attributes", [])
                attr_dict = dict(zip(attrs[::2], attrs[1::2])) if attrs else {}
                if attr_dict.get("id"):
                    info["selector"] = f"#{attr_dict['id']}"
                elif attr_dict.get("data-testid"):
                    info["selector"] = f'[data-testid="{attr_dict["data-testid"]}"]'
                elif tag and attr_dict.get("class"):
                    first_class = attr_dict["class"].split()[0]
                    info["selector"] = f"{tag}.{first_class}"
                elif tag and info.get("name"):
                    safe_name = info["name"].replace('"', '\\"')[:50]
                    info["selector"] = f'{tag}:has-text("{safe_name}")'
                    info["text_match"] = {"tag": tag, "text": info["name"][:120]}
                # Always carry the backend id so a CDP-only caller can
                # resolve the element directly without parsing the
                # selector string.
                info["backend_id"] = backend_id
                # This path could not verify uniqueness (no Runtime
                # domain, or the node had already gone). None means
                # "unknown", which is not the same as "unique" and must
                # not be reported as it.
                info.setdefault("selector_unique", None)
            except Exception:
                pass

    async def click(self, ref_or_selector: str) -> dict:
        """Click an element by ARIA ref or CSS selector."""
        try:
            if self._page:
                if ref_or_selector.startswith("ax"):
                    node_info = self._aria_refs.get(ref_or_selector)
                    if node_info and node_info.get("selector"):
                        await self._page.click(node_info["selector"], timeout=5000)
                    else:
                        await self._page.click(f"text={ref_or_selector}", timeout=5000)
                else:
                    await self._page.click(ref_or_selector, timeout=5000)
            else:
                await self._cdp_click(ref_or_selector)
            return {"success": True, "clicked": ref_or_selector}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def type_text(self, ref_or_selector: str, text: str) -> dict:
        """Type text into an element."""
        try:
            if self._page:
                if ref_or_selector.startswith("ax"):
                    node_info = self._aria_refs.get(ref_or_selector)
                    if node_info and node_info.get("selector"):
                        await self._page.fill(node_info["selector"], text, timeout=5000)
                    else:
                        await self._page.type(f"text={ref_or_selector}", text)
                else:
                    await self._page.fill(ref_or_selector, text, timeout=5000)
            else:
                await self._cdp.send_command("Input.dispatchKeyEvent", {
                    "type": "keyDown", "text": text,
                })
            return {"success": True, "typed": text[:50]}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def fill(self, ref_or_selector: str, value: str) -> dict:
        """Fill a form field (clears first, then types)."""
        return await self.type_text(ref_or_selector, value)

    async def fill_form(self, fields: dict) -> dict:
        """Fill multiple fields in one action: {selector_or_ref: value}."""
        if not isinstance(fields, dict) or not fields:
            return {"success": False, "error": "fields must be a non-empty object mapping selector/ref to value"}

        filled: list[str] = []
        failed: dict[str, str] = {}
        for target, value in fields.items():
            result = await self.fill(str(target), "" if value is None else str(value))
            if result.get("success"):
                filled.append(str(target))
            else:
                failed[str(target)] = str(result.get("error", "fill failed"))

        return {
            "success": len(failed) == 0,
            "filled": filled,
            "failed": failed,
            "total": len(fields),
        }

    async def hover(self, ref_or_selector: str) -> dict:
        """Hover over an element by ARIA ref or selector."""
        try:
            selector = self._resolve_selector(ref_or_selector)
            if self._page:
                await self._page.hover(selector, timeout=5000)
            else:
                await self._cdp_hover(selector)
            return {"success": True, "hovered": ref_or_selector}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def evaluate(self, js_code: str) -> dict:
        """Execute JavaScript in the page context."""
        try:
            if self._page:
                result = await self._page.evaluate(js_code)
            else:
                resp = await self._cdp.send_command("Runtime.evaluate", {
                    "expression": js_code, "returnByValue": True,
                })
                result = resp.get("result", {}).get("value")
            return {"success": True, "result": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def scroll(self, direction: str = "down", amount: int = 500) -> dict:
        """Scroll the page."""
        try:
            dx, dy = 0, amount if direction == "down" else -amount
            if direction == "right":
                dx, dy = amount, 0
            elif direction == "left":
                dx, dy = -amount, 0
            if self._page:
                await self._page.evaluate(f"window.scrollBy({dx}, {dy})")
            else:
                await self._cdp.send_command("Input.dispatchMouseEvent", {
                    "type": "mouseWheel", "x": 400, "y": 400, "deltaX": dx, "deltaY": dy,
                })
            return {"success": True, "direction": direction, "amount": amount}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def select(self, ref_or_selector: str, value: str) -> dict:
        """Select an option from a dropdown."""
        try:
            selector = self._resolve_selector(ref_or_selector)
            if self._page:
                await self._page.select_option(selector, value, timeout=5000)
            else:
                # Escape user input to prevent injection
                safe_sel = json.dumps(selector)
                safe_val = json.dumps(value)
                await self.evaluate(
                    f"(function(){{ var el = document.querySelector({safe_sel}); if(el) el.value = {safe_val}; }})()"
                )
            return {"success": True, "selected": value}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _resolve_selector(self, ref_or_selector: str) -> str:
        """Resolve an ARIA ref (ax0, ax1...) to a CSS selector."""
        if ref_or_selector.startswith("ax"):
            info = self._aria_refs.get(ref_or_selector, {})
            return info.get("selector") or ref_or_selector
        return ref_or_selector

    async def wait(self, ms: int = 1000) -> dict:
        """Wait for a specified duration."""
        await asyncio.sleep(ms / 1000.0)
        return {"success": True, "waited_ms": ms}

    async def get_console_logs(self, limit: int = 50, clear: bool = False) -> dict:
        """Return captured browser console logs."""
        try:
            bounded = max(1, min(int(limit), 500))
        except Exception:
            bounded = 50
        logs = self._console_logs[-bounded:]
        if clear:
            self._console_logs.clear()
        return {"success": True, "count": len(logs), "logs": logs}

    async def get_page_pdf(self, print_background: bool = True, landscape: bool = False) -> dict:
        """Export current page to PDF via CDP."""
        try:
            await self._cdp.send_command("Page.enable")
            result = await self._cdp.send_command("Page.printToPDF", {
                "printBackground": bool(print_background),
                "landscape": bool(landscape),
            })
            pdf_b64 = result.get("data", "")
            if not pdf_b64:
                return {"success": False, "error": "No PDF data returned by browser"}
            size_bytes = len(base64.b64decode(pdf_b64))
            return {"success": True, "pdf_b64": pdf_b64, "size_bytes": size_bytes}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def get_page_info(self) -> dict:
        """Get current page URL and title."""
        try:
            if self._page:
                return {
                    "url": self._page.url,
                    "title": await self._page.title(),
                }
            result = await self._cdp.send_command("Runtime.evaluate", {
                "expression": "JSON.stringify({url: location.href, title: document.title})",
                "returnByValue": True,
            })
            return json.loads(result.get("result", {}).get("value", "{}"))
        except Exception:
            return {"url": "", "title": ""}

    async def close(self):
        # PR 7 gap-fill: if a tracing session is still active when the
        # controller is torn down, flush it to a final trace file rather
        # than dropping the operator's recording on the floor.
        if getattr(self, "_tracing_active", False):
            try:
                await self.stop_tracing()
            except Exception:
                pass
        # Same reasoning for an in-flight screencast: the frames are
        # already on disk, so flush the manifest rather than leaving a
        # recording that can never be assembled.
        if getattr(self, "_recording", None) is not None:
            try:
                await self.stop_recording()
            except Exception:
                pass
        # A dialog watchdog outliving the controller would try to answer a
        # dialog over a socket that is about to close.
        for task in list(self._bg_tasks):
            task.cancel()
        self._bg_tasks.clear()
        if self._page is not None and self._pw_dialog_handler is not None:
            try:
                self._page.remove_listener("dialog", self._pw_dialog_handler)
            except Exception:
                pass
        self._pw_dialog_handler = None
        await self._cdp.disconnect()
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        # Stop the Playwright driver subprocess. Skipping this leaves a
        # dangling node process per BrowserController instance, which is
        # what the leak tests caught.
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        self._page = None

    # ── PR 7 gap-fill: Tracing / HAR / Downloads ────────────────────

    @property
    def _artifacts_root(self) -> Path:
        root = feral_browser_root() / "artifacts"
        root.mkdir(parents=True, exist_ok=True)
        return root

    async def start_tracing(
        self,
        *,
        screenshots: bool = True,
        snapshots: bool = True,
        sources: bool = False,
        name: str = "",
    ) -> dict:
        """Start a Playwright tracing session for the active context.

        Returns ``{success, path, message}``. When Playwright is not
        available (CDP-only mode), the call truthfully reports that
        tracing requires the Playwright driver — no fake-success.
        """
        if not self._page:
            return {"success": False, "error": "Playwright not connected — tracing requires the Playwright driver. Install with `pip install playwright` and run `playwright install chromium`."}
        ctx = self._page.context
        try:
            tracing = ctx.tracing
        except Exception:
            return {"success": False, "error": "Playwright tracing API not available on this browser context."}
        try:
            await tracing.start(screenshots=screenshots, snapshots=snapshots, sources=sources)
        except Exception as e:
            return {"success": False, "error": f"tracing.start failed: {e}"}
        self._tracing_active = True
        self._tracing_name = name or f"trace_{int(time.time())}"
        return {
            "success": True,
            "message": "Tracing started. Call stop_tracing to flush the zip.",
            "name": self._tracing_name,
        }

    async def stop_tracing(self, *, name: str = "") -> dict:
        """Stop tracing and write the zip under $FERAL_HOME/browser/artifacts/."""
        if not getattr(self, "_tracing_active", False) or not self._page:
            return {"success": False, "error": "No active tracing session."}
        ctx = self._page.context
        try:
            tracing = ctx.tracing
        except Exception:
            return {"success": False, "error": "Playwright tracing API not available."}
        out_name = name or getattr(self, "_tracing_name", f"trace_{int(time.time())}")
        out_path = self._artifacts_root / f"{out_name}.zip"
        try:
            await tracing.stop(path=str(out_path))
        except Exception as e:
            return {"success": False, "error": f"tracing.stop failed: {e}"}
        self._tracing_active = False
        return {
            "success": True,
            "path": str(out_path),
            "size_bytes": out_path.stat().st_size if out_path.exists() else 0,
            "viewer": "Open with: `playwright show-trace " + str(out_path) + "`",
        }

    async def start_har(self, *, name: str = "") -> dict:
        """Start HAR network recording on a fresh context.

        HAR must be configured at context creation time, so this method
        spins up a NEW Playwright context on the same browser and uses
        it for subsequent calls until ``stop_har`` flushes the file.
        The previous context is preserved and restored on stop so the
        agent's running page state isn't lost.
        """
        if not self._browser:
            return {"success": False, "error": "Playwright browser not connected — HAR requires the Playwright driver."}
        out_name = name or f"har_{int(time.time())}"
        out_path = self._artifacts_root / f"{out_name}.har"
        try:
            har_context = await self._browser.new_context(
                record_har_path=str(out_path),
                record_har_content="embed",
            )
            har_page = await har_context.new_page()
        except Exception as e:
            return {"success": False, "error": f"new_context with HAR failed: {e}"}
        self._har_active = True
        self._har_prev_page = self._page
        self._har_context = har_context
        self._har_path = out_path
        self._page = har_page
        return {"success": True, "path": str(out_path), "name": out_name}

    async def stop_har(self) -> dict:
        if not getattr(self, "_har_active", False):
            return {"success": False, "error": "No active HAR session."}
        ctx = getattr(self, "_har_context", None)
        out_path = getattr(self, "_har_path", None)
        prev_page = getattr(self, "_har_prev_page", None)
        try:
            if ctx is not None:
                await ctx.close()
        except Exception as e:
            return {"success": False, "error": f"har context close failed: {e}"}
        self._har_active = False
        self._har_context = None
        self._page = prev_page
        return {
            "success": True,
            "path": str(out_path) if out_path else "",
            "size_bytes": out_path.stat().st_size if out_path and out_path.exists() else 0,
        }

    async def wait_for_download(self, *, save_as: str = "", timeout_ms: int = 30000) -> dict:
        """Wait for a download to start, save it under artifacts/, and
        return the local path + suggested filename.

        Pre-condition: an action that triggers a download (click on a
        download link, etc.) is performed *immediately after* this call
        — Playwright registers the listener before the click via
        ``expect_download`` semantics emulated here through ``page.wait_for_event``.
        """
        if not self._page:
            return {"success": False, "error": "Playwright page not connected — downloads require the Playwright driver."}
        try:
            download = await self._page.wait_for_event("download", timeout=timeout_ms)
        except Exception as e:
            return {"success": False, "error": f"no download event within {timeout_ms}ms: {e}"}
        suggested = download.suggested_filename or f"download_{int(time.time())}"
        out_name = save_as or suggested
        out_path = self._artifacts_root / out_name
        try:
            await download.save_as(str(out_path))
        except Exception as e:
            return {"success": False, "error": f"download save_as failed: {e}"}
        return {
            "success": True,
            "path": str(out_path),
            "suggested_filename": suggested,
            "size_bytes": out_path.stat().st_size if out_path.exists() else 0,
            "url": download.url,
        }

    # ── Session video recording (CDP screencast) ─────────────────────
    #
    # The browser skill could already emit screenshots, a PDF, a
    # Playwright trace and a HAR, but none of those is a video, so there
    # was no way to film someone using the brain.
    #
    # ``Page.startScreencast`` is the right primitive: Chrome pushes an
    # encoded frame every time the page repaints, over the CDP socket we
    # already hold. No screen-capture process, no window-manager
    # permissions, no second browser.
    #
    # It is variable-rate by construction, so frames are persisted with
    # their capture timestamps and assembled through ffmpeg's concat
    # demuxer with per-frame durations. Assuming a constant framerate
    # would speed up idle stretches and slow down bursts, i.e. it would
    # not show what the user actually saw.

    @property
    def _recordings_root(self) -> Path:
        """Root for session recordings, under the FERAL data home.

        Recordings are the most sensitive artefact this skill produces:
        every pixel the operator saw, including anything already logged
        in. They therefore never leave the user's own data directory,
        and the directory is 0700 so other accounts on a shared machine
        cannot read them.
        """
        root = feral_browser_root() / "recordings"
        root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(root, 0o700)
        except OSError:
            # Non-POSIX filesystems reject the mode; the path is still
            # inside the user's home, which is the load-bearing part.
            pass
        return root

    async def start_recording(
        self,
        *,
        name: str = "",
        quality: int = RECORDING_FRAME_QUALITY,
        max_width: int = RECORDING_MAX_WIDTH,
        max_height: int = RECORDING_MAX_HEIGHT,
        every_nth_frame: int = 1,
        max_frames: int = RECORDING_MAX_FRAMES,
        redact_selectors: Optional[list] = None,
    ) -> dict:
        """Start recording the current tab to JPEG frames on disk.

        ``redact_selectors`` is opt-in pixel masking: each CSS selector is
        blurred out in the live page for the duration of the recording, so
        the sensitive region never reaches a frame at all. Post-hoc masking
        was rejected because it leaves the unmasked pixels on disk in the
        meantime.
        """
        if self._recording is not None:
            return {
                "success": False,
                "error": f"Already recording {self._recording['recording_id']}. Call stop_recording first.",
            }
        cdp, owned = await self._screencast_cdp()
        if cdp is None:
            return {
                "success": False,
                "error": (
                    "CDP not connected to a page target. Video recording needs Chrome "
                    f"started with --remote-debugging-port={CDP_PORT} and at least one "
                    "open tab."
                ),
            }

        recording_id = safe_recording_name(name) or (
            f"rec-{time.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}"
        )
        directory = self._recordings_root / recording_id
        if directory.exists():
            if owned:
                await cdp.disconnect()
            return {"success": False, "error": f"Recording {recording_id} already exists at {directory}."}
        frames_dir = directory / "frames"
        frames_dir.mkdir(parents=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass

        page = await self._page_identity(cdp)
        selectors = [str(s) for s in (redact_selectors or []) if str(s).strip()]
        masked = await self._apply_recording_mask(cdp, selectors) if selectors else False

        state = {
            "recording_id": recording_id,
            "directory": directory,
            "frames_dir": frames_dir,
            "cdp": cdp,
            "owns_cdp": owned,
            "started_at": time.time(),
            "next_index": 1,
            "frames": [],
            "max_frames": max(1, int(max_frames)),
            "truncated": False,
            "write_errors": 0,
            "ack_errors": 0,
            "redact_selectors": selectors,
            "mask_applied": masked,
            "start_url": redact_recording_text(page.get("url", "")),
            "start_title": redact_recording_text(page.get("title", "")),
            "quality": int(quality),
        }
        self._recording = state

        listener = self._on_screencast_frame
        cdp.add_event_listener(listener)
        self._recording_listener = listener

        try:
            await cdp.send_command("Page.enable")
            await cdp.send_command("Page.startScreencast", {
                "format": "jpeg",
                "quality": max(1, min(int(quality), 100)),
                "maxWidth": max(1, int(max_width)),
                "maxHeight": max(1, int(max_height)),
                "everyNthFrame": max(1, int(every_nth_frame)),
            })
        except Exception as e:
            self._recording = None
            self._detach_recording_listener(cdp)
            if masked:
                await self._clear_recording_mask(cdp)
            if owned:
                await cdp.disconnect()
            return {"success": False, "error": f"Page.startScreencast failed: {e}"}

        return {
            "success": True,
            "recording_id": recording_id,
            "directory": str(directory),
            "max_frames": state["max_frames"],
            "redact_selectors": selectors,
            "mask_applied": masked,
            "start_url": state["start_url"],
            "message": (
                "Recording started. Frames land under the FERAL data home; "
                "call stop_recording to assemble the video."
            ),
        }

    async def stop_recording(
        self,
        *,
        assemble: bool = True,
        output_format: str = "mp4",
    ) -> dict:
        """Stop the screencast, write the manifest, and assemble the video."""
        state = self._recording
        if state is None:
            return {"success": False, "error": "No active recording."}

        # Clear the handle first so late frames are dropped instead of
        # racing the manifest write.
        self._recording = None
        cdp = state["cdp"]
        try:
            await cdp.send_command("Page.stopScreencast")
        except Exception as e:
            logger.warning("Page.stopScreencast failed for %s: %s", state["recording_id"], e)
        self._detach_recording_listener(cdp)
        # Frame writes run as tasks off the CDP receive loop, so give the
        # ones already in flight a moment to land before counting them.
        await asyncio.sleep(0.25)
        if state.get("mask_applied"):
            await self._clear_recording_mask(cdp)
        if state.get("owns_cdp"):
            await cdp.disconnect()

        state["stopped_at"] = time.time()
        manifest = self._build_manifest(state)
        manifest_path = state["directory"] / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        result = {
            "success": True,
            "recording_id": state["recording_id"],
            "directory": str(state["directory"]),
            "manifest_path": str(manifest_path),
            "frame_count": len(manifest["frames"]),
            "duration_seconds": manifest["duration_seconds"],
            "truncated": state["truncated"],
            "write_errors": state["write_errors"],
            "ack_errors": state["ack_errors"],
            "video_path": "",
            "degraded": "",
        }
        if state["truncated"]:
            logger.warning(
                "Recording %s hit the %d-frame cap; the tail of the session was not captured.",
                state["recording_id"], state["max_frames"],
            )
            result["degraded"] = "frame_cap_reached"

        if not assemble:
            return result

        assembled = await self.assemble_recording(
            recording_id=state["recording_id"], output_format=output_format
        )
        result["video_path"] = assembled.get("video_path", "")
        result["assembled"] = assembled
        if assembled.get("degraded"):
            result["degraded"] = assembled["degraded"]
        if not assembled.get("success"):
            result["error"] = assembled.get("error", "")
        return result

    async def assemble_recording(
        self,
        recording_id: str,
        *,
        output_format: str = "mp4",
        overwrite: bool = False,
    ) -> dict:
        """Assemble a stored recording's frames into a video file.

        Split out from ``stop_recording`` so a recording captured on a
        machine without ffmpeg is not lost: the frames and manifest stay
        on disk and this can be re-run after ffmpeg is installed.
        """
        recording_id = safe_recording_name(recording_id)
        if not recording_id:
            return {"success": False, "error": "recording_id is required.", "degraded": ""}
        directory = self._recordings_root / recording_id
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            return {
                "success": False,
                "error": f"No recording manifest at {manifest_path}.",
                "degraded": "",
            }
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            return {"success": False, "error": f"Unreadable manifest: {e}", "degraded": ""}

        frames = manifest.get("frames") or []
        if not frames:
            return {
                "success": False,
                "error": f"Recording {recording_id} captured 0 frames; nothing to assemble.",
                "degraded": "",
            }

        fmt = str(output_format or "mp4").lower()
        if fmt not in ("mp4", "webm"):
            return {"success": False, "error": "output_format must be mp4 or webm.", "degraded": ""}
        output = directory / f"{recording_id}.{fmt}"
        if output.exists() and not overwrite:
            return {
                "success": True,
                "video_path": str(output),
                "size_bytes": output.stat().st_size,
                "frame_count": len(frames),
                "duration_seconds": manifest.get("duration_seconds", 0.0),
                "degraded": "",
                "message": "Video already assembled; pass overwrite to rebuild.",
            }

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            # Never pretend a video exists. The frames are real work and
            # stay on disk, but the caller has to know why there is no
            # file to play and what to install.
            logger.warning(
                "ffmpeg not found on PATH. Recording %s is preserved as %d JPEG frames "
                "in %s but no video could be assembled. Install ffmpeg "
                "(macOS: `brew install ffmpeg`) and re-run assemble_recording.",
                recording_id, len(frames), directory / "frames",
            )
            return {
                "success": False,
                "degraded": "ffmpeg_missing",
                "missing_dependency": "ffmpeg",
                "video_path": "",
                "frames_dir": str(directory / "frames"),
                "frame_count": len(frames),
                "duration_seconds": manifest.get("duration_seconds", 0.0),
                "error": (
                    "ffmpeg is not installed, so the frames could not be assembled into a "
                    "video. The recording is intact at "
                    f"{directory}; install ffmpeg (macOS: `brew install ffmpeg`, "
                    "Debian/Ubuntu: `apt install ffmpeg`) and call assemble_recording again."
                ),
            }

        concat_path = directory / "frames.txt"
        try:
            concat_path.write_text(self._build_concat_script(directory, frames), encoding="utf-8")
        except OSError as e:
            return {"success": False, "error": f"Could not write concat script: {e}", "degraded": ""}

        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_path),
            # libx264 refuses odd dimensions under yuv420p, and Chrome
            # happily hands back an odd-height viewport.
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-fps_mode", "vfr", "-pix_fmt", "yuv420p",
        ]
        if fmt == "mp4":
            command += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                        "-movflags", "+faststart"]
        else:
            command += ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "34"]
        command.append(str(output))

        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _out, err = await asyncio.wait_for(proc.communicate(), timeout=600)
        except asyncio.TimeoutError:
            logger.warning("ffmpeg timed out assembling recording %s", recording_id)
            return {
                "success": False,
                "degraded": "ffmpeg_timeout",
                "video_path": "",
                "error": "ffmpeg did not finish within 600s.",
            }
        except OSError as e:
            logger.warning("ffmpeg could not be executed for recording %s: %s", recording_id, e)
            return {
                "success": False,
                "degraded": "ffmpeg_failed",
                "missing_dependency": "ffmpeg",
                "video_path": "",
                "error": f"ffmpeg could not be executed: {e}",
            }

        if proc.returncode != 0 or not output.exists():
            detail = (err or b"").decode("utf-8", "replace").strip()[-800:]
            logger.warning(
                "ffmpeg exited %s assembling recording %s: %s",
                proc.returncode, recording_id, detail,
            )
            return {
                "success": False,
                "degraded": "ffmpeg_failed",
                "video_path": "",
                "frames_dir": str(directory / "frames"),
                "frame_count": len(frames),
                "error": f"ffmpeg exited {proc.returncode}: {detail}",
            }

        return {
            "success": True,
            "degraded": "",
            "recording_id": recording_id,
            "video_path": str(output),
            "size_bytes": output.stat().st_size,
            "frame_count": len(frames),
            "duration_seconds": manifest.get("duration_seconds", 0.0),
            "format": fmt,
        }

    async def list_recordings(self, limit: int = 20) -> dict:
        """List stored session recordings, newest first."""
        root = self._recordings_root
        found = []
        for child in root.iterdir():
            manifest_path = child / "manifest.json"
            if not child.is_dir() or not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            videos = sorted(
                str(p) for p in child.iterdir()
                if p.suffix.lower() in (".mp4", ".webm")
            )
            found.append({
                "recording_id": manifest.get("recording_id", child.name),
                "directory": str(child),
                "started_at": manifest.get("started_at"),
                "duration_seconds": manifest.get("duration_seconds", 0.0),
                "frame_count": len(manifest.get("frames") or []),
                "start_url": manifest.get("start_url", ""),
                "videos": videos,
            })
        found.sort(key=lambda item: item.get("started_at") or 0, reverse=True)
        bounded = max(1, min(int(limit or 20), 200))
        return {"success": True, "recordings": found[:bounded], "total": len(found), "root": str(root)}

    async def _on_screencast_frame(self, msg: dict) -> None:
        """Persist one screencast frame and acknowledge it.

        The ack is the whole trick. Chrome buffers only a couple of
        unacknowledged screencast frames and then stops emitting
        entirely, so a recorder that skips ``Page.screencastFrameAck``
        captures the first few frames and nothing else. The ack is
        therefore sent before any work that can fail or block.
        """
        if msg.get("method") != "Page.screencastFrame":
            return
        state = self._recording
        if state is None:
            return
        params = msg.get("params") or {}
        session_id = params.get("sessionId")

        over_cap = state["next_index"] > state["max_frames"]
        if over_cap:
            state["truncated"] = True
            index = 0
        else:
            index = state["next_index"]
            state["next_index"] += 1

        if session_id is not None:
            try:
                await state["cdp"].send_command(
                    "Page.screencastFrameAck", {"sessionId": session_id}, timeout=15.0
                )
            except Exception as e:
                state["ack_errors"] += 1
                logger.warning(
                    "screencastFrameAck failed for %s (frames will stop arriving): %s",
                    state["recording_id"], e,
                )
        if over_cap:
            return

        filename = f"{index:06d}.jpg"
        try:
            data = base64.b64decode(params.get("data") or "")
        except Exception:
            state["write_errors"] += 1
            return
        if not data:
            state["write_errors"] += 1
            return
        try:
            await asyncio.to_thread((state["frames_dir"] / filename).write_bytes, data)
        except OSError as e:
            state["write_errors"] += 1
            logger.warning("Could not write frame %s of %s: %s", filename, state["recording_id"], e)
            return

        meta = params.get("metadata") or {}
        timestamp = meta.get("timestamp")
        state["frames"].append({
            "index": index,
            "file": filename,
            # Chrome reports the capture time in epoch seconds. Falling
            # back to arrival time keeps ordering sane on the CDP stubs
            # and Chrome builds that omit it.
            "timestamp": float(timestamp) if isinstance(timestamp, (int, float)) else time.time(),
            "device_width": meta.get("deviceWidth"),
            "device_height": meta.get("deviceHeight"),
            "scroll_offset_y": meta.get("scrollOffsetY"),
            "size_bytes": len(data),
        })

    async def _screencast_cdp(self) -> tuple:
        """Return (connection, we_opened_it) for a page-attached CDP socket.

        The controller's own socket usually comes from ``/json/version``,
        which is the browser-level target, and the browser target does
        not implement the ``Page`` domain at all: ``Page.startScreencast`` on it
        fails with "wasn't found". Rather than change what every other
        endpoint connects to, recording opens its own short-lived socket
        against a tab and closes it on stop. CDP allows several clients
        per target, so this does not disturb the existing session.
        """
        existing = self._cdp
        if getattr(existing, "connected", False) and getattr(existing, "is_page_target", False):
            return existing, False
        host = getattr(existing, "_host", CDP_HOST)
        port = getattr(existing, "_port", CDP_PORT)
        page_cdp = CDPConnection(host=host, port=port)
        try:
            if await page_cdp.connect(prefer_page=True) and page_cdp.is_page_target:
                return page_cdp, True
        except Exception as e:
            logger.warning("Could not open a page-level CDP socket for recording: %s", e)
        try:
            await page_cdp.disconnect()
        except Exception:
            pass
        return None, False

    def _detach_recording_listener(self, cdp) -> None:
        listener = self._recording_listener
        self._recording_listener = None
        if listener is None:
            return
        try:
            cdp._event_listeners.remove(listener)
        except (ValueError, AttributeError):
            pass

    @staticmethod
    async def _page_identity(cdp) -> dict:
        """URL and title of the tab being recorded, read over ``cdp``."""
        try:
            result = await cdp.send_command("Runtime.evaluate", {
                "expression": "JSON.stringify({url: location.href, title: document.title})",
                "returnByValue": True,
            })
            value = result.get("result", {}).get("value")
            return json.loads(value) if isinstance(value, str) else {}
        except Exception:
            return {}

    @staticmethod
    async def _apply_recording_mask(cdp, selectors: list) -> bool:
        """Blur the given selectors in the live page for the recording.

        Masking happens in the page, not in post-production, because a
        post-hoc mask still leaves the unmasked pixels sitting in
        ``frames/`` in the meantime.
        """
        css = ", ".join(selectors) + " { filter: blur(14px) !important; }"
        script = (
            "(function(){"
            f"var id = {json.dumps(RECORDING_REDACTION_STYLE_ID)};"
            "var old = document.getElementById(id); if (old) old.remove();"
            "var el = document.createElement('style'); el.id = id;"
            f"el.textContent = {json.dumps(css)};"
            "document.documentElement.appendChild(el); return true;"
            "})()"
        )
        try:
            await cdp.send_command("Runtime.evaluate", {
                "expression": script, "returnByValue": True,
            })
            return True
        except Exception as e:
            logger.warning("Recording redaction mask could not be applied: %s", e)
            return False

    @staticmethod
    async def _clear_recording_mask(cdp) -> None:
        script = (
            "(function(){"
            f"var el = document.getElementById({json.dumps(RECORDING_REDACTION_STYLE_ID)});"
            "if (el) el.remove(); return true;"
            "})()"
        )
        try:
            await cdp.send_command("Runtime.evaluate", {
                "expression": script, "returnByValue": True,
            })
        except Exception as e:
            logger.warning("Recording redaction mask could not be removed: %s", e)

    def _build_manifest(self, state: dict) -> dict:
        """Build the on-disk manifest, ordered and with per-frame offsets."""
        frames = sorted(state["frames"], key=lambda f: f["index"])
        base = frames[0]["timestamp"] if frames else state["started_at"]
        for frame in frames:
            frame["offset_seconds"] = round(max(0.0, frame["timestamp"] - base), 6)
        durations = self._frame_durations(frames)
        for frame, duration in zip(frames, durations):
            frame["duration_seconds"] = duration
        return {
            "schema_version": 1,
            "recording_id": state["recording_id"],
            "started_at": round(state["started_at"], 6),
            "stopped_at": round(state.get("stopped_at", time.time()), 6),
            "duration_seconds": round(sum(durations), 3),
            "frame_count": len(frames),
            "truncated": state["truncated"],
            "write_errors": state["write_errors"],
            "ack_errors": state["ack_errors"],
            "quality": state["quality"],
            "start_url": state["start_url"],
            "start_title": state["start_title"],
            "redact_selectors": state["redact_selectors"],
            "mask_applied": state["mask_applied"],
            "frames": frames,
        }

    @staticmethod
    def _frame_durations(frames: list) -> list:
        """Per-frame hold times derived from real capture timestamps.

        Screencast frames arrive when the page repaints, so a fixed
        framerate would compress the pauses and stretch the bursts. The
        clamps keep a single stalled frame from becoming minutes of a
        frozen picture, and keep a duplicate timestamp from producing a
        zero-length entry ffmpeg would drop.
        """
        if not frames:
            return []
        durations = []
        for current, following in zip(frames, frames[1:]):
            gap = float(following["timestamp"]) - float(current["timestamp"])
            durations.append(round(
                min(max(gap, RECORDING_MIN_FRAME_SECONDS), RECORDING_MAX_FRAME_SECONDS), 6
            ))
        if durations:
            tail = statistics.median(durations)
        else:
            tail = 1.0 / RECORDING_FALLBACK_FPS
        durations.append(round(
            min(max(tail, RECORDING_MIN_FRAME_SECONDS), RECORDING_MAX_FRAME_SECONDS), 6
        ))
        return durations

    @staticmethod
    def _build_concat_script(directory: Path, frames: list) -> str:
        """Render an ffmpeg concat demuxer script with per-frame durations.

        Absolute paths because the concat demuxer resolves relative
        entries against the script's own directory, which silently
        breaks the moment a recording is moved.
        """
        lines = ["ffconcat version 1.0"]
        for frame in frames:
            path = str(directory / "frames" / frame["file"]).replace("'", r"'\''")
            lines.append(f"file '{path}'")
            duration = frame.get("duration_seconds") or (1.0 / RECORDING_FALLBACK_FPS)
            lines.append(f"duration {float(duration):.6f}")
        # The concat demuxer ignores the duration of the final entry
        # unless the file is repeated, which otherwise truncates the last
        # frame to nothing.
        if frames:
            last = str(directory / "frames" / frames[-1]["file"]).replace("'", r"'\''")
            lines.append(f"file '{last}'")
        return "\n".join(lines) + "\n"

    # ── Cookie / Session Persistence ─────────────────────────────────

    async def save_cookies(self, profile: str = "default") -> dict:
        """Save all cookies to disk for session persistence."""
        cookies_dir = feral_browser_root() / "cookies"
        cookies_dir.mkdir(parents=True, exist_ok=True)
        cookies_path = cookies_dir / f"{profile}.json"

        if self._cdp.connected:
            result = await self._cdp.send_command("Network.getAllCookies")
            cookies = result.get("cookies", [])
            cookies_path.write_text(json.dumps(cookies, indent=2))
            return {"success": True, "count": len(cookies), "path": str(cookies_path)}
        elif self._page:
            ctx = self._page.context
            cookies = await ctx.cookies()
            cookies_path.write_text(json.dumps(cookies, indent=2))
            return {"success": True, "count": len(cookies), "path": str(cookies_path)}
        return {"success": False, "error": "No browser connection"}

    async def restore_cookies(self, profile: str = "default") -> dict:
        """Restore cookies from disk."""
        cookies_path = feral_browser_root() / "cookies" / f"{profile}.json"
        if not cookies_path.exists():
            return {"success": False, "error": "No saved cookies"}

        cookies = json.loads(cookies_path.read_text())
        if self._cdp.connected:
            await self._cdp.send_command("Network.setCookies", {"cookies": cookies})
        elif self._page:
            await self._page.context.add_cookies(cookies)
        else:
            return {"success": False, "error": "No browser connection"}
        return {"success": True, "count": len(cookies)}

    # ── Network Request Interception ─────────────────────────────────

    async def enable_network_monitor(self) -> dict:
        """Enable network request monitoring via CDP."""
        if not self._cdp.connected:
            return {"success": False, "error": "CDP not connected"}

        self._network_log = []
        self._network_monitoring = True
        await self._cdp.send_command("Network.enable")
        return {"success": True}

    async def get_network_log(self, filter_type: str = "") -> dict:
        """Get captured network requests, optionally filtered by type."""
        log = self._network_log
        if filter_type:
            log = [e for e in log if filter_type.lower() in e.get("type", "").lower()]
        return {"success": True, "requests": log[-100:], "total": len(log)}

    # ── Iframe Support ───────────────────────────────────────────────

    async def list_iframes(self) -> dict:
        """List all iframes on the current page."""
        if self._page:
            frames = self._page.frames
            return {"success": True, "frames": [
                {"name": f.name, "url": f.url, "index": i}
                for i, f in enumerate(frames)
            ]}
        elif self._cdp.connected:
            result = await self._cdp.send_command("Target.getTargets")
            iframes = [t for t in result.get("targetInfos", []) if t.get("type") == "iframe"]
            return {"success": True, "frames": [
                {"title": f.get("title", ""), "url": f.get("url", ""), "targetId": f.get("targetId")}
                for f in iframes
            ]}
        return {"success": False, "error": "No browser connection"}

    async def execute_in_iframe(self, frame_index: int, script: str) -> dict:
        """Execute JavaScript in a specific iframe."""
        if self._page:
            frames = self._page.frames
            if 0 <= frame_index < len(frames):
                result = await frames[frame_index].evaluate(script)
                return {"success": True, "result": str(result)[:5000]}
        return {"success": False, "error": "Frame not found or CDP-only"}

    # ── File Download Management ─────────────────────────────────────

    async def set_download_path(self, path: str = "") -> dict:
        """Configure file download behavior."""
        download_dir = path or str(feral_browser_root() / "downloads")
        Path(download_dir).mkdir(parents=True, exist_ok=True)

        if self._cdp.connected:
            await self._cdp.send_command("Browser.setDownloadBehavior", {
                "behavior": "allow",
                "downloadPath": download_dir,
            })
        return {"success": True, "download_path": download_dir}

    def _build_aria_text(
        self, nodes: list[dict], max_depth: int = 10, ref_offset: int = 0,
    ) -> str:
        """Convert AX tree nodes to readable text with assigned refs.

        No cap here any more. ``snapshot`` decides how many nodes to hand
        over and reports the truncation; a second silent cap inside this
        function is exactly how the useful elements used to disappear.

        ``ref_offset`` numbers the refs by their ABSOLUTE position in the
        match list, so page two of a paginated snapshot starts at ax200
        rather than restarting at ax0. Two different elements answering to
        ax0 within one page load is a trap, not a convenience.
        """
        lines = []
        ref_counter = int(ref_offset or 0)

        for node in nodes:
            role = node.get("role", {}).get("value", "")
            name = node.get("name", {}).get("value", "")
            if not role or role in ("none", "generic", "InlineTextBox"):
                continue

            ref_id = f"ax{ref_counter}"
            ref_counter += 1

            backend_id = node.get("backendDOMNodeId")
            self._aria_refs[ref_id] = {
                "node_id": node.get("nodeId", ""),
                "backend_id": backend_id,
                "role": role,
                "name": name,
                "selector": "",
            }

            indent = "  " * min(node.get("depth", 0), max_depth)
            desc = f"[{ref_id}] {role}"
            if name:
                desc += f': "{name}"'

            props = node.get("properties", [])
            for p in props:
                pname = p.get("name", "")
                pval = p.get("value", {}).get("value", "")
                if pname in ("disabled", "checked", "selected", "expanded") and pval:
                    desc += f" ({pname}={pval})"

            lines.append(f"{indent}{desc}")

        return "\n".join(lines) if lines else "(empty page)"

    def _compress_image(self, raw_bytes: bytes) -> str:
        """Resize and compress image for VLM analysis."""
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(raw_bytes))
            if img.width > MAX_SCREENSHOT_WIDTH:
                ratio = MAX_SCREENSHOT_WIDTH / img.width
                new_h = int(img.height * ratio)
                img = img.resize((MAX_SCREENSHOT_WIDTH, new_h), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
            return base64.b64encode(buf.getvalue()).decode()
        except ImportError:
            return base64.b64encode(raw_bytes).decode()

    async def _cdp_click(self, selector: str):
        """Click via CDP (fallback when Playwright isn't available)."""
        x, y = await self._cdp_get_element_center(selector)
        for event_type in ("mousePressed", "mouseReleased"):
            await self._cdp.send_command("Input.dispatchMouseEvent", {
                "type": event_type, "x": x, "y": y, "button": "left", "clickCount": 1,
            })

    async def _cdp_hover(self, selector: str):
        """Move mouse over element center via CDP."""
        x, y = await self._cdp_get_element_center(selector)
        await self._cdp.send_command("Input.dispatchMouseEvent", {
            "type": "mouseMoved",
            "x": x,
            "y": y,
        })

    async def _cdp_get_element_center(self, selector: str) -> tuple[float, float]:
        # `:has-text("X")` is a Playwright pseudo, not real CSS. When
        # we are running CDP-only, splitting into tag + text and using
        # a small JS text scan finds the element instead of crashing
        # `document.querySelector`.
        tag, text = self._parse_has_text(selector)
        if tag is not None:
            expression = (
                "(function() {"
                f"const tag = {json.dumps(tag)};"
                f"const text = {json.dumps(text)};"
                "const els = Array.from(document.querySelectorAll(tag));"
                "const el = els.find(e => (e.innerText || e.textContent || '').includes(text));"
                "if (!el) return null;"
                "const rect = el.getBoundingClientRect();"
                "return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };"
                "})()"
            )
        else:
            safe_selector = json.dumps(selector)
            expression = (
                "(function() {"
                f"const el = document.querySelector({safe_selector});"
                "if (!el) return null;"
                "const rect = el.getBoundingClientRect();"
                "return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };"
                "})()"
            )
        result = await self._cdp.send_command("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
        })
        coords = result.get("result", {}).get("value")
        if not coords:
            raise Exception(f"Element not found: {selector}")
        return float(coords["x"]), float(coords["y"])

    @staticmethod
    def _parse_has_text(selector: str) -> tuple[Optional[str], str]:
        """If selector looks like ``tag:has-text("X")``, return (tag, X);
        otherwise return (None, "") so the caller falls through to
        plain ``querySelector``.
        """
        if not selector or ":has-text(" not in selector:
            return None, ""
        try:
            head, _, rest = selector.partition(":has-text(")
            text, _, _ = rest.rpartition(")")
            text = text.strip()
            if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
                text = text[1:-1]
            tag = head.strip() or "*"
            return tag, text
        except Exception:
            return None, ""

    # ── Wait / Retry primitives ──────────────────────────────────────
    #
    # The brain's planner needs to express "before clicking, make sure
    # this thing exists / is visible". Flat sleeps cause flaky
    # automation. These primitives poll until a real condition is
    # met (or the budget runs out) and report exactly which condition
    # was satisfied (or which timeout was exceeded), so the agent can
    # repair instead of guessing.

    async def wait_for_selector(
        self,
        ref_or_selector: str,
        timeout_ms: int = 5000,
        poll_ms: int = 100,
        state: str = "visible",
    ) -> dict:
        """Wait until an element matching the selector/ARIA ref exists
        (and optionally is visible). Falls back to a CDP polling loop
        when Playwright is unavailable. Always returns a structured
        dict — never silently waits the full budget on missing DOM.
        """
        target = self._resolve_selector(ref_or_selector)
        deadline = max(50, int(timeout_ms))
        poll = max(20, int(poll_ms))
        if self._page:
            try:
                await self._page.wait_for_selector(
                    target,
                    timeout=deadline,
                    state=state if state in ("attached", "detached", "visible", "hidden") else "visible",
                )
                return {"success": True, "selector": target, "via": "playwright"}
            except Exception as e:
                return {"success": False, "selector": target, "error": str(e), "via": "playwright"}

        loop = asyncio.get_event_loop()
        start = loop.time()
        last_error = ""
        while (loop.time() - start) * 1000.0 < deadline:
            try:
                tag, text = self._parse_has_text(target)
                if tag is not None:
                    expression = (
                        "(function() {"
                        f"const tag = {json.dumps(tag)};"
                        f"const text = {json.dumps(text)};"
                        "const els = Array.from(document.querySelectorAll(tag));"
                        "const el = els.find(e => (e.innerText || e.textContent || '').includes(text));"
                        "if (!el) return false;"
                        "const r = el.getBoundingClientRect();"
                        "return r.width > 0 && r.height > 0;"
                        "})()"
                    )
                else:
                    safe_selector = json.dumps(target)
                    expression = (
                        "(function() {"
                        f"const el = document.querySelector({safe_selector});"
                        "if (!el) return false;"
                        "const r = el.getBoundingClientRect();"
                        "return r.width > 0 && r.height > 0;"
                        "})()"
                    )
                result = await self._cdp.send_command("Runtime.evaluate", {
                    "expression": expression,
                    "returnByValue": True,
                })
                if bool(result.get("result", {}).get("value")):
                    return {"success": True, "selector": target, "via": "cdp"}
            except Exception as e:
                last_error = str(e)
            await asyncio.sleep(poll / 1000.0)
        return {
            "success": False,
            "selector": target,
            "via": "cdp",
            "error": last_error or f"Selector {target!r} not visible within {deadline}ms",
        }

    # ── Page reading primitives ──────────────────────────────────────
    #
    # `snapshot()` returns the ARIA tree, which is what an agent needs to
    # ACT on a page. It is not what an agent needs to READ one: the AX
    # tree drops paragraph prose, and it is capped at 500 nodes. Article
    # text, prices, error banners and search results all live in the
    # rendered text, so reading it needed its own primitive rather than
    # the model hand-rolling `evaluate("document.body.innerText")`, an
    # endpoint that is CRITICAL in the danger map and hard-denied on the
    # http_api, mcp and cron surfaces.

    async def get_page_text(self, max_chars: int = 20000, selector: str = "") -> dict:
        """Return the page's rendered (visible) text.

        ``selector`` narrows the read to one subtree. ``innerText`` rather
        than ``textContent`` on purpose: textContent includes the contents
        of <script>/<style> and of display:none nodes, which is noise the
        model then has to pay for and reason around.
        """
        try:
            bound = max(200, min(int(max_chars), 500_000))
        except (TypeError, ValueError):
            bound = 20000
        target = self._resolve_selector(selector) if selector else ""
        expression = (
            "(function(){"
            f"var sel = {json.dumps(target)};"
            "var root = sel ? document.querySelector(sel) : document.body;"
            "if (!root) return null;"
            "return root.innerText || root.textContent || '';"
            "})()"
        )
        result = await self.evaluate(expression)
        if not result.get("success"):
            return result
        text = result.get("result")
        if text is None:
            return {
                "success": False,
                "error": (
                    f"No element matched selector {target!r}."
                    if target else "Page has no <body> to read yet."
                ),
            }
        text = str(text)
        truncated = len(text) > bound
        return {
            "success": True,
            "text": text[:bound],
            "chars": min(len(text), bound),
            "truncated": truncated,
            "selector": target,
        }

    # JS that locates candidate elements by a natural-language-ish
    # description. Kept as a module-level constant so the escaping is
    # reviewed in one place; the caller's text is injected as a JSON
    # literal, never concatenated into the source.
    _FIND_JS = """
    (function(query, limit) {
      function cssEscape(v) {
        return String(v).replace(/["\\\\]/g, '\\\\$&');
      }
      function selectorFor(el) {
        if (el.id) return '#' + el.id;
        var testid = el.getAttribute('data-testid');
        if (testid) return '[data-testid="' + cssEscape(testid) + '"]';
        var name = el.getAttribute('name');
        if (name) return el.tagName.toLowerCase() + '[name="' + cssEscape(name) + '"]';
        var aria = el.getAttribute('aria-label');
        if (aria) return el.tagName.toLowerCase() + '[aria-label="' + cssEscape(aria) + '"]';
        var parts = [];
        var node = el;
        while (node && node.nodeType === 1 && parts.length < 6) {
          var tag = node.tagName.toLowerCase();
          if (node.id) { parts.unshift('#' + node.id); break; }
          var parent = node.parentElement;
          if (parent) {
            var same = Array.prototype.filter.call(
              parent.children, function (c) { return c.tagName === node.tagName; });
            if (same.length > 1) {
              tag += ':nth-of-type(' + (same.indexOf(node) + 1) + ')';
            }
          }
          parts.unshift(tag);
          node = node.parentElement;
        }
        return parts.join(' > ');
      }
      var SEL = 'a,button,input,select,textarea,summary,label,[role],[onclick],' +
                '[contenteditable="true"],h1,h2,h3';
      var q = String(query || '').toLowerCase().trim();
      var terms = q.split(/\\s+/).filter(Boolean);
      var out = [];
      var seen = 0;
      var els = Array.prototype.slice.call(document.querySelectorAll(SEL));
      for (var i = 0; i < els.length && seen < 4000; i++) {
        var el = els[i];
        seen++;
        var rect = el.getBoundingClientRect();
        var style = window.getComputedStyle(el);
        var visible = rect.width > 0 && rect.height > 0 &&
                      style.visibility !== 'hidden' && style.display !== 'none';
        var hay = [
          el.innerText || el.textContent || '',
          el.getAttribute('aria-label') || '',
          el.getAttribute('placeholder') || '',
          el.getAttribute('title') || '',
          el.getAttribute('name') || '',
          el.getAttribute('value') || '',
          el.id || ''
        ].join(' ').replace(/\\s+/g, ' ').trim();
        var low = hay.toLowerCase();
        var score = 0;
        if (!terms.length) {
          score = visible ? 1 : 0;
        } else {
          for (var t = 0; t < terms.length; t++) {
            if (low.indexOf(terms[t]) !== -1) score += 1;
          }
          if (low.indexOf(q) !== -1) score += terms.length;
        }
        if (score <= 0) continue;
        if (!visible) score -= 0.5;
        out.push({
          score: score,
          tag: el.tagName.toLowerCase(),
          role: el.getAttribute('role') || '',
          type: el.getAttribute('type') || '',
          text: hay.slice(0, 200),
          selector: selectorFor(el),
          visible: visible,
          x: Math.round(rect.x + rect.width / 2),
          y: Math.round(rect.y + rect.height / 2)
        });
      }
      out.sort(function (a, b) { return b.score - a.score; });
      return out.slice(0, limit);
    })
    """

    async def find(self, description: str, limit: int = 10) -> dict:
        """Locate candidate elements matching a plain-language description.

        Returns ranked candidates with a ready-to-use ``selector`` and the
        element's viewport centre, so the caller can follow up with
        ``click``/``fill`` (selector) or ``click_at`` (coordinates).

        This is a text/attribute matcher, not a vision model: it scores on
        the element's own text, aria-label, placeholder, title, name, value
        and id. "the blue button in the corner" will not match; "sign in"
        will. An empty ``candidates`` list means no element carried those
        words, NOT that the element does not exist.
        """
        query = str(description or "").strip()
        if not query:
            return {"success": False, "error": "description is required."}
        try:
            bound = max(1, min(int(limit), 50))
        except (TypeError, ValueError):
            bound = 10
        expression = f"{self._FIND_JS}({json.dumps(query)}, {bound})"
        result = await self.evaluate(expression)
        if not result.get("success"):
            return result
        candidates = result.get("result") or []
        return {
            "success": True,
            "query": query,
            "count": len(candidates),
            "candidates": candidates,
        }

    # ── Keyboard / pointer primitives ────────────────────────────────
    #
    # `click(selector)` cannot press Enter, cannot Tab between fields and
    # cannot reach a canvas or a custom widget that has no selector. Those
    # are the three cases every real form hits, so both a key primitive
    # and a coordinate primitive are needed alongside the selector ones.

    # CDP has no name->keycode table, so the common navigation/editing
    # keys carry theirs here. Anything not listed is sent as literal text.
    _CDP_KEYS: dict = {
        "Enter": (13, "\r"), "Return": (13, "\r"), "Tab": (9, "\t"),
        "Escape": (27, ""), "Esc": (27, ""), "Backspace": (8, ""),
        "Delete": (46, ""), "ArrowUp": (38, ""), "ArrowDown": (40, ""),
        "ArrowLeft": (37, ""), "ArrowRight": (39, ""), "Home": (36, ""),
        "End": (35, ""), "PageUp": (33, ""), "PageDown": (34, ""),
        "Space": (32, " "),
    }

    async def press_key(self, key: str, ref_or_selector: str = "") -> dict:
        """Press a single key, optionally focusing an element first.

        ``key`` uses Playwright/DOM names: ``Enter``, ``Tab``, ``Escape``,
        ``ArrowDown``, ``Backspace``, ``Control+a``, ``a``. Chords are only
        supported on the Playwright path; the CDP-only fallback presses the
        final key of a chord and reports ``modifiers_dropped``.
        """
        key = str(key or "").strip()
        if not key:
            return {"success": False, "error": "key is required."}
        try:
            if self._page:
                if ref_or_selector:
                    await self._page.press(
                        self._resolve_selector(ref_or_selector), key, timeout=5000,
                    )
                else:
                    await self._page.keyboard.press(key)
                return {"success": True, "key": key, "via": "playwright"}

            dropped = "+" in key
            bare = key.rsplit("+", 1)[-1]
            if ref_or_selector:
                focus = await self._focus_cdp(self._resolve_selector(ref_or_selector))
                if not focus.get("success"):
                    return focus
            code, text = self._CDP_KEYS.get(bare, (0, bare if len(bare) == 1 else ""))
            for phase in ("keyDown", "keyUp"):
                params = {"type": phase, "key": bare, "windowsVirtualKeyCode": code,
                          "nativeVirtualKeyCode": code}
                if phase == "keyDown" and text:
                    params["text"] = text
                await self._cdp.send_command("Input.dispatchKeyEvent", params)
            out = {"success": True, "key": key, "via": "cdp"}
            if dropped:
                out["modifiers_dropped"] = True
            return out
        except Exception as e:
            return {"success": False, "error": str(e), "key": key}

    async def _focus_cdp(self, selector: str) -> dict:
        result = await self.evaluate(
            "(function(){var el=document.querySelector(%s);"
            "if(!el) return false; el.focus(); return true;})()"
            % json.dumps(selector)
        )
        if not result.get("success"):
            return result
        if not result.get("result"):
            return {"success": False, "error": f"Element not found: {selector}"}
        return {"success": True}

    async def click_at(
        self, x: float, y: float, button: str = "left", click_count: int = 1,
    ) -> dict:
        """Click at viewport coordinates, for elements no selector reaches.

        Coordinates are CSS pixels relative to the viewport's top-left, the
        same frame ``find`` and ``screenshot`` report. They go stale the
        moment the page scrolls or relayouts, so re-read them rather than
        reusing coordinates across a navigation.
        """
        try:
            bx, by = float(x), float(y)
        except (TypeError, ValueError):
            return {"success": False, "error": "x and y must be numbers."}
        btn = str(button or "left").lower()
        if btn not in ("left", "right", "middle"):
            return {"success": False, "error": "button must be left, right or middle."}
        try:
            count = max(1, min(int(click_count), 3))
        except (TypeError, ValueError):
            count = 1
        try:
            if self._page:
                await self._page.mouse.click(bx, by, button=btn, click_count=count)
            else:
                for phase in ("mousePressed", "mouseReleased"):
                    await self._cdp.send_command("Input.dispatchMouseEvent", {
                        "type": phase, "x": bx, "y": by,
                        "button": btn, "clickCount": count,
                    })
            return {"success": True, "x": bx, "y": by, "button": btn, "click_count": count}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── Drag and drop ────────────────────────────────────────────────
    #
    # Reordering a list, moving a Kanban card, dragging a slider handle,
    # selecting a range of spreadsheet cells and dropping a file on a
    # drop zone are all one gesture that click/hover cannot express, and
    # none of them are reachable any other way.
    #
    # There are two incompatible drag mechanisms on the web and the
    # endpoint has to be honest about which one it is driving:
    #
    #   mouse  - mousedown, several mousemoves, mouseup. This is what
    #            sliders, canvases, range selections and the pointer-event
    #            sortables (SortableJS in fallback mode, dnd-kit,
    #            react-beautiful-dnd) listen for. INTERMEDIATE moves are
    #            mandatory: a single jump from A to B leaves most of these
    #            libraries thinking the drag never started, so the press
    #            and release read as a plain click on the source.
    #   html5  - the native draggable/dragstart/dragover/drop protocol.
    #            Chrome does NOT synthesise these from raw
    #            Input.dispatchMouseEvent, so the mouse path silently does
    #            nothing on a native drop zone. Playwright's drag_and_drop
    #            drives Chromium's drag interception and does work.
    #
    # `auto` therefore prefers html5 (Playwright) when both endpoints are
    # selectors and Playwright is connected, and uses the mouse sequence
    # otherwise, including for every coordinate drag.

    async def drag(
        self,
        from_ref_or_selector: str = "",
        to_ref_or_selector: str = "",
        from_x=None,
        from_y=None,
        to_x=None,
        to_y=None,
        steps: int = 12,
        hold_ms: int = 120,
        settle_ms: int = 120,
        mode: str = "auto",
    ) -> dict:
        """Drag from one point to another, by refs/selectors or coordinates.

        Give EITHER ``from_ref_or_selector`` + ``to_ref_or_selector``
        (ARIA refs from snapshot or CSS selectors) OR the four
        coordinates, which are CSS pixels from the viewport's top-left,
        the same frame ``find``, ``screenshot`` and ``click_at`` use. The
        two forms can be mixed: a ref source with a coordinate target is
        valid and is how you drop onto a canvas.

        This performs a real gesture, so it commits whatever the drop
        does (reordering, moving between lists, setting a slider value).
        """
        mode = str(mode or "auto").strip().lower()
        if mode not in ("auto", "mouse", "html5"):
            return {"success": False, "error": "mode must be auto, mouse or html5."}
        try:
            steps = max(2, min(int(steps), 100))
        except (TypeError, ValueError):
            steps = 12
        try:
            hold = max(0, min(int(hold_ms), 5000))
        except (TypeError, ValueError):
            hold = 120
        try:
            settle = max(0, min(int(settle_ms), 5000))
        except (TypeError, ValueError):
            settle = 120

        src_sel = str(from_ref_or_selector or "").strip()
        dst_sel = str(to_ref_or_selector or "").strip()
        has_src_xy = from_x is not None and from_y is not None
        has_dst_xy = to_x is not None and to_y is not None
        if not src_sel and not has_src_xy:
            return {
                "success": False,
                "error": "Give from_ref_or_selector, or both from_x and from_y.",
            }
        if not dst_sel and not has_dst_xy:
            return {
                "success": False,
                "error": "Give to_ref_or_selector, or both to_x and to_y.",
            }

        if mode == "html5" and not (src_sel and dst_sel):
            return {
                "success": False,
                "error": (
                    "mode=html5 needs a selector or ARIA ref for BOTH ends: "
                    "the native drag protocol targets elements, not points."
                ),
            }
        if mode == "html5" and not self._page:
            return {
                "success": False,
                "error": (
                    "mode=html5 requires the Playwright driver (Chrome does not "
                    "synthesise dragstart/drop from raw CDP mouse events). "
                    "Install with `pip install playwright`, or use mode=mouse."
                ),
            }

        use_html5 = mode == "html5" or (
            mode == "auto" and self._page is not None and src_sel and dst_sel
        )
        if use_html5:
            try:
                await self._page.drag_and_drop(
                    self._resolve_selector(src_sel),
                    self._resolve_selector(dst_sel),
                    timeout=10000,
                )
                return {
                    "success": True, "via": "playwright_html5",
                    "from": src_sel, "to": dst_sel,
                    "note": (
                        "Drop dispatched. Verify with snapshot or get_page_text: "
                        "a page can accept the gesture and reject the drop."
                    ),
                }
            except Exception as e:
                if mode == "html5":
                    return {"success": False, "via": "playwright_html5",
                            "error": str(e), "from": src_sel, "to": dst_sel}
                logger.info(
                    "Playwright drag_and_drop failed (%s); falling back to a "
                    "synthesised mouse drag.", e,
                )

        try:
            sx, sy = (
                (float(from_x), float(from_y)) if has_src_xy
                else await self._point_for(src_sel)
            )
            dx, dy = (
                (float(to_x), float(to_y)) if has_dst_xy
                else await self._point_for(dst_sel)
            )
        except ValueError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": f"Could not locate a drag endpoint: {e}"}

        try:
            await self._mouse_drag(sx, sy, dx, dy, steps, hold, settle)
        except Exception as e:
            return {"success": False, "via": "mouse", "error": str(e)}
        return {
            "success": True,
            "via": "mouse_playwright" if self._page else "mouse_cdp",
            "from": {"x": sx, "y": sy, "selector": src_sel},
            "to": {"x": dx, "y": dy, "selector": dst_sel},
            "steps": steps,
            "note": (
                "Synthesised mouse drag. Native HTML5 drop zones (draggable=true "
                "elements) do NOT respond to this; retry with mode=html5 and "
                "selectors for both ends if nothing moved."
            ),
        }

    async def _point_for(self, ref_or_selector: str) -> tuple[float, float]:
        """Viewport-centre coordinates for a ref or selector.

        Prefers the ARIA snapshot's cached box (already measured), then
        Playwright's bounding_box, then the CDP measurement. Raises
        ValueError with an actionable message when the element cannot be
        located, because a drag to (0, 0) looks like a working drag that
        did the wrong thing.
        """
        cached = self._aria_refs.get(ref_or_selector) if ref_or_selector.startswith("ax") else None
        if cached and cached.get("center"):
            return float(cached["center"]["x"]), float(cached["center"]["y"])
        selector = self._resolve_selector(ref_or_selector)
        if not selector:
            raise ValueError(f"Cannot resolve {ref_or_selector!r} to an element.")
        if self._page:
            try:
                box = await self._page.locator(selector).first.bounding_box(timeout=5000)
            except Exception as e:
                box = None
                logger.debug("Playwright bounding_box failed for %r: %s", selector, e)
            if box:
                return (
                    float(box["x"]) + float(box["width"]) / 2.0,
                    float(box["y"]) + float(box["height"]) / 2.0,
                )
        try:
            return await self._cdp_get_element_center(selector)
        except Exception as e:
            raise ValueError(
                f"No element matched {selector!r}, so there is nothing to drag "
                f"to or from ({e}). Call snapshot or find for a live selector."
            ) from e

    async def _mouse_drag(
        self, sx: float, sy: float, dx: float, dy: float,
        steps: int, hold_ms: int, settle_ms: int,
    ) -> None:
        """press, hold, move in increments, dwell, release."""
        if self._page:
            mouse = self._page.mouse
            await mouse.move(sx, sy)
            await mouse.down()
            if hold_ms:
                await asyncio.sleep(hold_ms / 1000.0)
            for i in range(1, steps + 1):
                await mouse.move(
                    sx + (dx - sx) * i / steps, sy + (dy - sy) * i / steps,
                )
                await asyncio.sleep(0.008)
            if settle_ms:
                await asyncio.sleep(settle_ms / 1000.0)
            await mouse.move(dx, dy)
            await mouse.up()
            return

        async def _mouse(event_type: str, x: float, y: float, buttons: int) -> None:
            await self._cdp.send_command("Input.dispatchMouseEvent", {
                "type": event_type, "x": x, "y": y,
                "button": "left" if buttons else "none",
                "buttons": buttons, "clickCount": 1,
            })

        # `buttons: 1` on the moves is load-bearing. Without the bitmask
        # Chrome delivers the moves with no button held, so a drag
        # listener that checks event.buttons never starts the drag.
        await _mouse("mouseMoved", sx, sy, 0)
        await _mouse("mousePressed", sx, sy, 1)
        if hold_ms:
            await asyncio.sleep(hold_ms / 1000.0)
        for i in range(1, steps + 1):
            await _mouse(
                "mouseMoved", sx + (dx - sx) * i / steps, sy + (dy - sy) * i / steps, 1,
            )
            await asyncio.sleep(0.008)
        if settle_ms:
            await asyncio.sleep(settle_ms / 1000.0)
        await _mouse("mouseMoved", dx, dy, 1)
        await _mouse("mouseReleased", dx, dy, 0)

    async def type_keys(self, text: str, delay_ms: int = 0) -> dict:
        """Type text into whatever currently has focus.

        Unlike ``type_text``/``fill`` this takes no selector and does not
        clear the field: it is the primitive for canvases, rich-text
        editors, and anything already focused by ``click_at``/``press_key``.
        """
        value = "" if text is None else str(text)
        if not value:
            return {"success": False, "error": "text is required."}
        try:
            delay = max(0, min(int(delay_ms), 1000))
        except (TypeError, ValueError):
            delay = 0
        try:
            if self._page:
                await self._page.keyboard.type(value, delay=delay)
            else:
                await self._cdp.send_command("Input.insertText", {"text": value})
            return {"success": True, "typed_chars": len(value)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── History navigation ───────────────────────────────────────────

    async def go_back(self) -> dict:
        """Go back one entry in session history."""
        return await self._history_step(-1, "back")

    async def go_forward(self) -> dict:
        """Go forward one entry in session history."""
        return await self._history_step(1, "forward")

    async def _history_step(self, delta: int, label: str) -> dict:
        try:
            if self._page:
                # Two Playwright behaviours make the obvious implementation
                # report failure on a Back that plainly worked, and both
                # were observed against a real Chrome:
                #
                # 1. `wait_until` defaults to "load". A page restored from
                #    the back/forward cache does not re-fire `load`, so
                #    go_back raised "Timeout 15000ms exceeded ... navigated
                #    to https://example.com/", the log line naming the
                #    successful navigation is inside the timeout error.
                #    "commit" is what Back actually means: the history entry
                #    became the current document.
                # 2. A null Response also does not mean "no history entry";
                #    a bfcache restore issues no network response at all.
                #
                # The URL is the evidence in both cases, so it decides.
                before = self._page.url
                try:
                    resp = await (
                        self._page.go_back(timeout=15000, wait_until="commit")
                        if delta < 0 else
                        self._page.go_forward(timeout=15000, wait_until="commit")
                    )
                except Exception as nav_exc:
                    if self._page.url == before:
                        return {"success": False, "error": str(nav_exc)}
                    resp = None
                after = self._page.url
                if resp is None and after == before:
                    return {
                        "success": False,
                        "error": f"No {label} entry in this tab's history.",
                    }
                return {"success": True, "direction": label, "url": after,
                        "title": await self._get_title()}
            history = await self._cdp.send_command("Page.getNavigationHistory")
            entries = history.get("entries", [])
            index = int(history.get("currentIndex", 0)) + delta
            if index < 0 or index >= len(entries):
                return {"success": False, "error": f"No {label} entry in this tab's history."}
            await self._cdp.send_command(
                "Page.navigateToHistoryEntry", {"entryId": entries[index]["id"]},
            )
            info = await self.get_page_info()
            return {"success": True, "direction": label, "url": info.get("url", ""),
                    "title": info.get("title", "")}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def reload_page(self, ignore_cache: bool = False) -> dict:
        """Reload the current page."""
        try:
            if self._page:
                await self._page.reload(timeout=30000)
            else:
                await self._cdp.send_command(
                    "Page.reload", {"ignoreCache": bool(ignore_cache)},
                )
            info = await self.get_page_info()
            return {"success": True, "url": info.get("url", ""), "title": info.get("title", "")}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── File upload ──────────────────────────────────────────────────

    async def upload_file(self, ref_or_selector: str, file_paths) -> dict:
        """Attach local files to an ``<input type=file>`` on the page.

        The paths are read from the machine FERAL runs on, so this uploads
        the operator's own files to whatever site is loaded. Every path is
        checked to exist first: a silent no-op on a typo'd path would leave
        a form looking filled when nothing was attached.
        """
        if isinstance(file_paths, str):
            paths = [file_paths]
        elif isinstance(file_paths, (list, tuple)):
            paths = [str(p) for p in file_paths]
        else:
            return {"success": False, "error": "file_paths must be a path or list of paths."}
        if not paths:
            return {"success": False, "error": "file_paths must not be empty."}

        resolved: list[str] = []
        missing: list[str] = []
        for raw in paths:
            path = Path(str(raw)).expanduser()
            if path.is_file():
                resolved.append(str(path.resolve()))
            else:
                missing.append(str(raw))
        if missing:
            return {
                "success": False,
                "error": f"File(s) not found on this machine: {', '.join(missing)}",
                "missing": missing,
            }

        selector = self._resolve_selector(ref_or_selector)
        if not selector:
            return {"success": False, "error": "ref_or_selector is required."}
        try:
            if self._page:
                await self._page.set_input_files(selector, resolved, timeout=10000)
                return {"success": True, "selector": selector, "files": resolved,
                        "via": "playwright"}
            doc = await self._cdp.send_command("DOM.getDocument", {"depth": 0})
            node = await self._cdp.send_command("DOM.querySelector", {
                "nodeId": doc["root"]["nodeId"], "selector": selector,
            })
            node_id = node.get("nodeId")
            if not node_id:
                return {"success": False, "error": f"No file input matched {selector!r}."}
            await self._cdp.send_command("DOM.setFileInputFiles", {
                "files": resolved, "nodeId": node_id,
            })
            return {"success": True, "selector": selector, "files": resolved, "via": "cdp"}
        except Exception as e:
            return {"success": False, "error": str(e), "selector": selector}

    # ── Viewport ─────────────────────────────────────────────────────

    async def set_viewport(
        self, width: int = 1280, height: int = 800, device_scale_factor: float = 1.0,
        mobile: bool = False,
    ) -> dict:
        """Resize the rendered viewport (CDP device-metrics override).

        This changes what the page LAYS OUT to and what a screenshot
        captures. It overrides metrics rather than resizing the OS window,
        so the visible Chrome window keeps its size.
        """
        try:
            w, h = int(width), int(height)
        except (TypeError, ValueError):
            return {"success": False, "error": "width and height must be integers."}
        if not (100 <= w <= 10000 and 100 <= h <= 10000):
            return {"success": False, "error": "width/height must be between 100 and 10000."}
        try:
            scale = float(device_scale_factor or 1.0)
        except (TypeError, ValueError):
            scale = 1.0
        try:
            await self._cdp.send_command("Emulation.setDeviceMetricsOverride", {
                "width": w, "height": h,
                "deviceScaleFactor": scale, "mobile": bool(mobile),
            })
            return {"success": True, "width": w, "height": h,
                    "device_scale_factor": scale, "mobile": bool(mobile)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── Endpoint dispatch ────────────────────────────────────────────

    async def execute(self, endpoint_id: str, args: Optional[dict] = None) -> dict:
        """Route one manifest endpoint id to the controller method behind it.

        This is the single dispatcher for the agent-visible browser
        surface. ``skills/manifests/browser_use.json`` declares exactly the
        ids in ``_DISPATCH``; ``api.state.BrainState._dispatch_browser_action``
        calls straight through to here. Keeping the table in this module
        rather than in ``api/state.py`` is what lets a test prove that every
        declared endpoint routes, without booting the brain.

        An unknown id returns an error naming the id: never a silent
        no-op, and never a fabricated success.
        """
        args = dict(args or {})
        handler = self._DISPATCH.get(str(endpoint_id))
        if handler is None:
            return {
                "success": False,
                "error": (
                    f"Unknown browser endpoint {endpoint_id!r}. Known endpoints: "
                    + ", ".join(sorted(self._DISPATCH))
                ),
            }
        try:
            result = await handler(self, args)
            return await self._with_dialog_events(result)
        except TypeError as e:
            return {"success": False, "error": f"Bad arguments for {endpoint_id}: {e}"}
        except Exception as e:
            # Most controller methods already return a structured failure.
            # The ones that talk straight to CDP (enable_network_monitor,
            # save_cookies) do not, and a raw CDP exception such as
            # "'Network.enable' wasn't found" propagating out of the skill
            # aborts the whole tool call instead of giving the model a
            # result it can read and route around.
            logger.warning("browser endpoint %s raised: %s", endpoint_id, e)
            return await self._with_dialog_events(
                {"success": False, "error": f"{endpoint_id} failed: {e}"}
            )

    async def _settle_dialog_answers(self) -> None:
        """Let an in-flight automatic answer land before we report on it.

        The dialog handler runs as its own task, so an endpoint can return
        while the answer is still on the wire. Reporting then said
        ``handled: false, pending: true`` for a dialog that was dismissed
        a few milliseconds later, which is a worse lie than saying nothing:
        it tells the model the page is still blocked when it is not.
        Skipped under ``manual``, where pending is the intended state and
        waiting would tax every call.
        """
        if self._dialog_policy == "manual":
            return
        for _ in range(25):
            if not any(
                e.get("pending") for e in self._dialog_log if not e.get("_reported")
            ):
                return
            await asyncio.sleep(0.02)

    async def _with_dialog_events(self, result):
        """Attach any dialog seen since the last endpoint call.

        A dialog handled by policy is still a thing that HAPPENED to the
        page: a confirm() that was cancelled means the action behind it
        did not run. Reporting it on the next result is what stops the
        model reasoning about a page state that a silent auto-answer
        already changed. Non-dict results (list_tabs) are left alone.
        """
        if not isinstance(result, dict):
            return result
        await self._settle_dialog_answers()
        events = self._drain_dialog_events()
        if events:
            result["dialog_events"] = events
            result["dialog_policy"] = self._dialog_policy
            # A dismissed beforeunload is the specific case where the
            # dialog is the REASON the action failed, and the failure
            # itself says nothing about it.
            if result.get("success") is False and any(
                e.get("type") == "beforeunload" and e.get("action") == "dismiss"
                for e in events
            ):
                result["dialog_hint"] = (
                    "The page raised a beforeunload ('leave site?') prompt and the "
                    "current dialog policy dismissed it, which CANCELS the "
                    "navigation and keeps the page. That is why this failed. The "
                    "page believes it has unsaved work; leaving anyway means "
                    "discarding it, so confirm with the user before calling "
                    "dialog_policy with policy=accept and retrying."
                )
        if self._pending_dialog is not None:
            result["dialog_pending"] = self._public_dialog(self._pending_dialog)
        return result

# ── Agent-visible endpoint surface ───────────────────────────────────
#
# The manifest (skills/manifests/browser_use.json) and this table are two
# halves of one contract: an id in the manifest that is not a key here is
# a runtime 404 the model only discovers by calling it, and a key here
# that the manifest does not declare is code nothing can reach.
# tests/test_browser_use_endpoints.py asserts both directions.
#
# Endpoint ids are deliberately NOT method names. `save_session` reads
# better to a model than `save_cookies`, and pinning the agent surface to
# an internal method name means renaming the method breaks the agent.
BROWSER_ENDPOINT_ALIASES: dict[str, str] = {
    "save_session": "save_cookies",
    "restore_session": "restore_cookies",
    "network_monitor_start": "enable_network_monitor",
    "network_log": "get_network_log",
    "trace_start": "start_tracing",
    "trace_stop": "stop_tracing",
    "har_start": "start_har",
    "har_stop": "stop_har",
    "download_next": "wait_for_download",
    "reload": "reload_page",
}


def _s(args: dict, key: str, default: str = "") -> str:
    value = args.get(key, default)
    return default if value is None else str(value)


def _i(args: dict, key: str, default: int) -> int:
    try:
        return int(args.get(key, default))
    except (TypeError, ValueError):
        return default


def _b(args: dict, key: str, default: bool = False) -> bool:
    value = args.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


BrowserController._DISPATCH = {
    # ── navigation ──
    "navigate": lambda c, a: c.navigate(
        _s(a, "url"), wait_until=_s(a, "wait_until", "domcontentloaded")),
    "go_back": lambda c, a: c.go_back(),
    "go_forward": lambda c, a: c.go_forward(),
    "reload": lambda c, a: c.reload_page(ignore_cache=_b(a, "ignore_cache")),
    # ── reading ──
    "get_page_info": lambda c, a: c.get_page_info(),
    "get_page_text": lambda c, a: c.get_page_text(
        max_chars=_i(a, "max_chars", 20000), selector=_s(a, "selector")),
    "snapshot": lambda c, a: c.snapshot(
        filter=_s(a, "filter", "interactive"),
        max_nodes=_i(a, "max_nodes", SNAPSHOT_DEFAULT_MAX_NODES),
        offset=_i(a, "offset", 0),
        include_coordinates=_b(a, "include_coordinates", True)),
    "screenshot": lambda c, a: c.screenshot(_b(a, "full_page")),
    "find": lambda c, a: c.find(_s(a, "description"), limit=_i(a, "limit", 10)),
    "get_page_pdf": lambda c, a: c.get_page_pdf(
        _b(a, "print_background", True), _b(a, "landscape")),
    # ── interaction ──
    "click": lambda c, a: c.click(_s(a, "ref_or_selector")),
    "click_at": lambda c, a: c.click_at(
        a.get("x"), a.get("y"), button=_s(a, "button", "left"),
        click_count=_i(a, "click_count", 1)),
    "hover": lambda c, a: c.hover(_s(a, "ref_or_selector")),
    "drag": lambda c, a: c.drag(
        from_ref_or_selector=_s(a, "from_ref_or_selector"),
        to_ref_or_selector=_s(a, "to_ref_or_selector"),
        from_x=a.get("from_x"), from_y=a.get("from_y"),
        to_x=a.get("to_x"), to_y=a.get("to_y"),
        steps=_i(a, "steps", 12), hold_ms=_i(a, "hold_ms", 120),
        settle_ms=_i(a, "settle_ms", 120), mode=_s(a, "mode", "auto")),
    "type_text": lambda c, a: c.type_text(_s(a, "ref_or_selector"), _s(a, "text")),
    "type_keys": lambda c, a: c.type_keys(_s(a, "text"), delay_ms=_i(a, "delay_ms", 0)),
    "press_key": lambda c, a: c.press_key(
        _s(a, "key"), ref_or_selector=_s(a, "ref_or_selector")),
    "fill_form": lambda c, a: c.fill_form(a.get("fields") or {}),
    "select_option": lambda c, a: c.select(_s(a, "ref_or_selector"), _s(a, "value")),
    "scroll": lambda c, a: c.scroll(_s(a, "direction", "down"), _i(a, "amount", 500)),
    "upload_file": lambda c, a: c.upload_file(
        _s(a, "ref_or_selector"), a.get("file_paths")),
    "set_viewport": lambda c, a: c.set_viewport(
        width=_i(a, "width", 1280), height=_i(a, "height", 800),
        device_scale_factor=a.get("device_scale_factor", 1.0),
        mobile=_b(a, "mobile")),
    "evaluate": lambda c, a: c.evaluate(_s(a, "js_code")),
    # ── waiting ──
    "wait": lambda c, a: c.wait(_i(a, "ms", 1000)),
    "wait_for_selector": lambda c, a: c.wait_for_selector(
        _s(a, "ref_or_selector"), timeout_ms=_i(a, "timeout_ms", 5000),
        poll_ms=_i(a, "poll_ms", 100), state=_s(a, "state", "visible")),
    # ── tabs ──
    "list_tabs": lambda c, a: c.list_tabs(),
    "new_tab": lambda c, a: c.new_tab(
        _s(a, "url", "about:blank"), activate=_b(a, "activate", True)),
    "switch_tab": lambda c, a: c.switch_tab(_s(a, "tab_id")),
    "close_tab": lambda c, a: c.close_tab(_s(a, "tab_id")),
    # ── native dialogs ──
    "dialog_policy": lambda c, a: c.set_dialog_policy(
        policy=_s(a, "policy", DEFAULT_DIALOG_POLICY),
        prompt_text=_s(a, "prompt_text"),
        manual_timeout_s=a.get("manual_timeout_s", DIALOG_MANUAL_TIMEOUT_S)),
    "handle_dialog": lambda c, a: c.handle_dialog(
        action=_s(a, "action", "dismiss"), prompt_text=_s(a, "prompt_text")),
    "get_dialogs": lambda c, a: c.get_dialogs(
        limit=_i(a, "limit", 20), clear=_b(a, "clear")),
    # ── frames ──
    "list_iframes": lambda c, a: c.list_iframes(),
    "execute_in_iframe": lambda c, a: c.execute_in_iframe(
        _i(a, "frame_index", 0), _s(a, "script")),
    # ── diagnostics ──
    "get_console_logs": lambda c, a: c.get_console_logs(
        _i(a, "limit", 50), _b(a, "clear")),
    "network_monitor_start": lambda c, a: c.enable_network_monitor(),
    "network_log": lambda c, a: c.get_network_log(_s(a, "filter_type")),
    # ── session ──
    "save_session": lambda c, a: c.save_cookies(_s(a, "profile", "default")),
    "restore_session": lambda c, a: c.restore_cookies(_s(a, "profile", "default")),
    # ── downloads / artifacts ──
    "set_download_path": lambda c, a: c.set_download_path(_s(a, "path")),
    "download_next": lambda c, a: c.wait_for_download(
        save_as=_s(a, "save_as"), timeout_ms=_i(a, "timeout_ms", 30000)),
    "trace_start": lambda c, a: c.start_tracing(
        screenshots=_b(a, "screenshots", True), snapshots=_b(a, "snapshots", True),
        sources=_b(a, "sources"), name=_s(a, "name")),
    "trace_stop": lambda c, a: c.stop_tracing(name=_s(a, "name")),
    "har_start": lambda c, a: c.start_har(name=_s(a, "name")),
    "har_stop": lambda c, a: c.stop_har(),
    # ── session video ──
    "start_recording": lambda c, a: c.start_recording(
        name=_s(a, "name"), quality=_i(a, "quality", RECORDING_FRAME_QUALITY),
        max_width=_i(a, "max_width", RECORDING_MAX_WIDTH),
        max_height=_i(a, "max_height", RECORDING_MAX_HEIGHT),
        every_nth_frame=_i(a, "every_nth_frame", 1),
        max_frames=_i(a, "max_frames", RECORDING_MAX_FRAMES),
        redact_selectors=a.get("redact_selectors")),
    "stop_recording": lambda c, a: c.stop_recording(
        assemble=_b(a, "assemble", True),
        output_format=_s(a, "output_format", "mp4")),
    "assemble_recording": lambda c, a: c.assemble_recording(
        _s(a, "recording_id"), output_format=_s(a, "output_format", "mp4"),
        overwrite=_b(a, "overwrite")),
    "list_recordings": lambda c, a: c.list_recordings(_i(a, "limit", 20)),
}


BROWSER_MANIFEST_PATH = (
    Path(__file__).resolve().parent.parent / "manifests" / "browser_use.json"
)


def get_browser_skill_manifest() -> dict:
    """Return the agent-visible browser skill manifest.

    Source of truth is ``skills/manifests/browser_use.json`` so the
    registry, the safety resolver and the result-budget trust check all
    read the same declaration. The in-code dict below is the fallback for
    a stripped install where the manifests directory is absent; it is a
    strict subset and carries no safety metadata, which is why the JSON
    is preferred whenever it is readable.
    """
    try:
        data = json.loads(BROWSER_MANIFEST_PATH.read_text(encoding="utf-8"))
        if data.get("endpoints"):
            return data
        logger.warning(
            "%s declares no endpoints; falling back to the in-code manifest.",
            BROWSER_MANIFEST_PATH,
        )
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "Could not read %s (%s); falling back to the in-code browser "
            "manifest, which carries no safety_tier metadata.",
            BROWSER_MANIFEST_PATH, exc,
        )
    return _fallback_browser_manifest()


def _fallback_browser_manifest() -> dict:
    """Minimal in-code manifest used only when the JSON file is missing."""
    return {
        "skill_id": "browser",
        "name": "Browser Control",
        "description": "Control web browsers — navigate, click, type, screenshot, read page content",
        "safety_level": "WARN",
        "endpoints": [
            {"id": "navigate", "description": "Navigate to a URL", "params": [
                {"name": "url", "type": "string", "required": True, "description": "URL to navigate to"},
                {"name": "wait_until", "type": "string", "required": False, "description": "Wait strategy: load, domcontentloaded, networkidle, commit"},
            ]},
            {"id": "screenshot", "description": "Capture a screenshot of the current page", "params": [
                {"name": "full_page", "type": "boolean", "required": False, "description": "Capture full scrollable page"},
            ]},
            {"id": "snapshot", "description": "Get ARIA accessibility tree of the current page", "params": []},
            {"id": "click", "description": "Click an element by ref or CSS selector", "params": [
                {"name": "ref_or_selector", "type": "string", "required": True},
            ]},
            {"id": "type_text", "description": "Type text into an element", "params": [
                {"name": "ref_or_selector", "type": "string", "required": True},
                {"name": "text", "type": "string", "required": True},
            ]},
            {"id": "fill_form", "description": "Fill multiple form fields in one step", "params": [
                {"name": "fields", "type": "object", "required": True, "description": "Mapping: selector/ref -> value"},
            ]},
            {"id": "hover", "description": "Hover over an element to reveal menus/tooltips", "params": [
                {"name": "ref_or_selector", "type": "string", "required": True},
            ]},
            {"id": "evaluate", "description": "Execute JavaScript in the page", "params": [
                {"name": "js_code", "type": "string", "required": True},
            ]},
            {"id": "scroll", "description": "Scroll the page", "params": [
                {"name": "direction", "type": "string", "required": False, "description": "up/down/left/right"},
                {"name": "amount", "type": "integer", "required": False},
            ]},
            {"id": "wait_for_selector", "description": "Wait until a selector or ARIA ref is visible (or until the timeout expires). Returns success/failure with a real reason — never silently waits.", "params": [
                {"name": "ref_or_selector", "type": "string", "required": True, "description": "ARIA ref (axN) or CSS selector"},
                {"name": "timeout_ms", "type": "integer", "required": False, "description": "Max wait in ms (default 5000)"},
                {"name": "poll_ms", "type": "integer", "required": False, "description": "Poll interval in ms (default 100, CDP-only path)"},
                {"name": "state", "type": "string", "required": False, "description": "visible|attached|detached|hidden (default visible, Playwright path)"},
            ]},
            {"id": "get_console_logs", "description": "Read captured browser console logs", "params": [
                {"name": "limit", "type": "integer", "required": False, "description": "Max log entries to return"},
                {"name": "clear", "type": "boolean", "required": False, "description": "Clear logs after reading"},
            ]},
            {"id": "get_page_pdf", "description": "Export current page to PDF and return base64 bytes", "params": [
                {"name": "print_background", "type": "boolean", "required": False},
                {"name": "landscape", "type": "boolean", "required": False},
            ]},
            {"id": "get_page_info", "description": "Get current page URL and title", "params": []},
            {"id": "list_tabs", "description": "List all open browser tabs", "params": []},
            {"id": "switch_tab", "description": "Activate a browser tab by its id", "params": [
                {"name": "tab_id", "type": "string", "required": True, "description": "Tab id from list_tabs"},
            ]},
            {"id": "new_tab", "description": "Open a new browser tab", "params": [
                {"name": "url", "type": "string", "required": False, "description": "URL to open (default: blank)"},
            ]},
            {"id": "close_tab", "description": "Close a browser tab", "params": [
                {"name": "tab_id", "type": "string", "required": True, "description": "Tab id to close"},
            ]},
            {"id": "save_session", "description": "Save browser cookies/session to disk", "params": [
                {"name": "profile", "type": "string", "required": False, "description": "Session profile name (default: 'default')"},
            ]},
            {"id": "restore_session", "description": "Restore browser cookies/session from disk", "params": [
                {"name": "profile", "type": "string", "required": False, "description": "Session profile name (default: 'default')"},
            ]},
            {"id": "network_monitor_start", "description": "Start capturing network requests via CDP", "params": []},
            {"id": "network_log", "description": "Get captured network requests", "params": [
                {"name": "filter_type", "type": "string", "required": False, "description": "Filter by resource type (e.g. XHR, Fetch, Script)"},
            ]},
            {"id": "list_iframes", "description": "List all iframes on the current page", "params": []},
            {"id": "execute_in_iframe", "description": "Execute JavaScript in a specific iframe", "params": [
                {"name": "frame_index", "type": "integer", "required": True, "description": "Index of the iframe from list_iframes"},
                {"name": "script", "type": "string", "required": True, "description": "JavaScript to execute"},
            ]},
            {"id": "set_download_path", "description": "Configure browser file download directory", "params": [
                {"name": "path", "type": "string", "required": False, "description": "Download directory path (default: $FERAL_HOME/browser/downloads)"},
            ]},
            {"id": "start_recording", "description": "Start recording the current tab to video via CDP screencast. Frames are stored under the FERAL data home; call stop_recording to assemble.", "params": [
                {"name": "name", "type": "string", "required": False, "description": "Recording id (default: timestamped)"},
                {"name": "quality", "type": "integer", "required": False, "description": "JPEG frame quality 1-100 (default 70)"},
                {"name": "max_width", "type": "integer", "required": False, "description": "Max frame width in px (default 1280)"},
                {"name": "max_height", "type": "integer", "required": False, "description": "Max frame height in px (default 800)"},
                {"name": "every_nth_frame", "type": "integer", "required": False, "description": "Capture every Nth repaint (default 1)"},
                {"name": "max_frames", "type": "integer", "required": False, "description": "Frame cap before truncation (default 5400)"},
                {"name": "redact_selectors", "type": "array", "required": False, "description": "CSS selectors to blur in the page for the whole recording, so sensitive regions never reach a frame"},
            ]},
            {"id": "stop_recording", "description": "Stop the active recording, write its manifest, and assemble the video. Reports a `degraded` reason instead of a video path when ffmpeg is missing.", "params": [
                {"name": "assemble", "type": "boolean", "required": False, "description": "Assemble the video now (default true)"},
                {"name": "output_format", "type": "string", "required": False, "description": "mp4 or webm (default mp4)"},
            ]},
            {"id": "assemble_recording", "description": "Assemble a stored recording's frames into a video at real capture speed. Re-runnable after installing ffmpeg.", "params": [
                {"name": "recording_id", "type": "string", "required": True, "description": "Recording id from start_recording/list_recordings"},
                {"name": "output_format", "type": "string", "required": False, "description": "mp4 or webm (default mp4)"},
                {"name": "overwrite", "type": "boolean", "required": False, "description": "Rebuild an existing video file"},
            ]},
            {"id": "list_recordings", "description": "List stored session recordings, newest first", "params": [
                {"name": "limit", "type": "integer", "required": False, "description": "Max recordings to return (default 20)"},
            ]},
        ],
    }
