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

    async def connect(self, prefer_page: bool = False) -> bool:
        """Connect to Chrome CDP endpoint.

        ``prefer_page`` resolves a tab target first instead of the
        browser endpoint. It is opt-in so existing callers keep the
        browser-level socket they already depend on.
        """
        try:
            import httpx
            if prefer_page:
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
                                    asyncio.create_task(maybe_coro)
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

    @property
    def connected(self) -> bool:
        return self._cdp.connected

    async def initialize(self) -> bool:
        """Connect to Chrome CDP and optionally Playwright. Auto-launches Chrome if needed."""
        cdp_ok = await self._cdp.connect()
        if not cdp_ok:
            launched = await self._auto_launch_chrome()
            if launched:
                await asyncio.sleep(2.0)
                cdp_ok = await self._cdp.connect()
            if not cdp_ok:
                logger.warning("CDP not available — browser control disabled.")
                return False

        if not self._console_listener_attached:
            self._cdp.add_event_listener(self._on_cdp_event)
            self._console_listener_attached = True
        try:
            await self._cdp.send_command("Runtime.enable")
            await self._cdp.send_command("Log.enable")
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

    async def switch_tab(self, tab_id: str) -> bool:
        """Activate a tab by its CDP target id."""
        import aiohttp
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://{CDP_HOST}:{CDP_PORT}/json/activate/{tab_id}") as resp:
                    return resp.status == 200
        except Exception as e:
            logger.warning(f"Failed to switch tab: {e}")
            return False

    async def new_tab(self, url: str = "about:blank") -> Optional[str]:
        """Open a new browser tab."""
        import aiohttp
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://{CDP_HOST}:{CDP_PORT}/json/new?{url}") as resp:
                    data = await resp.json()
                    return data.get("id")
        except Exception as e:
            logger.warning(f"Failed to open new tab: {e}")
            return None

    async def close_tab(self, tab_id: str) -> bool:
        """Close a browser tab by its CDP target id."""
        import aiohttp
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://{CDP_HOST}:{CDP_PORT}/json/close/{tab_id}") as resp:
                    return resp.status == 200
        except Exception as e:
            logger.warning(f"Failed to close tab: {e}")
            return False

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

    async def snapshot(self) -> dict:
        """Get ARIA accessibility tree as structured text with resolved selectors."""
        try:
            await self._cdp.send_command("DOM.enable")
            await self._cdp.send_command("Accessibility.enable")
            result = await self._cdp.send_command("Accessibility.getFullAXTree")
            nodes = result.get("nodes", [])
            self._aria_refs.clear()
            text = self._build_aria_text(nodes)
            # Resolve backend DOM node IDs to CSS selectors
            await self._resolve_aria_selectors()
            return {"success": True, "aria_tree": text, "ref_count": len(self._aria_refs)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _resolve_aria_selectors(self):
        """Resolve ARIA refs to CSS selectors via DOM.describeNode.

        Selector strategy is dual-mode aware:
        * Always emit a *plain* CSS form (id / data-testid / tag.class)
          when the DOM offers one — these work in both Playwright and
          CDP `document.querySelector` paths.
        * Only when nothing else is available do we fall back to
          ``tag:has-text(...)``. That is a Playwright pseudo-selector
          (not real CSS), so we also stash the ARIA name in
          ``text_match`` so the CDP-only path can find the element via
          a JS text scan instead of choking on the unknown pseudo.
        """
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
        root = Path.home() / ".feral" / "browser" / "artifacts"
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
        """Stop tracing and write the artifact to ~/.feral/browser/artifacts/<name>.zip."""
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
        from config.loader import feral_data_home
        root = feral_data_home() / "browser" / "recordings"
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
        cookies_dir = Path.home() / ".feral" / "browser" / "cookies"
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
        cookies_path = Path.home() / ".feral" / "browser" / "cookies" / f"{profile}.json"
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
        download_dir = path or str(Path.home() / ".feral" / "browser" / "downloads")
        Path(download_dir).mkdir(parents=True, exist_ok=True)

        if self._cdp.connected:
            await self._cdp.send_command("Browser.setDownloadBehavior", {
                "behavior": "allow",
                "downloadPath": download_dir,
            })
        return {"success": True, "download_path": download_dir}

    def _build_aria_text(self, nodes: list[dict], max_depth: int = 10) -> str:
        """Convert AX tree nodes to readable text with assigned refs."""
        lines = []
        ref_counter = 0

        for node in nodes[:500]:
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


def get_browser_skill_manifest() -> dict:
    """Return the skill manifest for browser control."""
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
                {"name": "path", "type": "string", "required": False, "description": "Download directory path (default: ~/.feral/browser/downloads)"},
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
