"""Explicit Chrome 144+ existing-session CDP connection; no browser launch.

Chrome's permission-based connection uses the same two-line DevToolsActivePort
discovery as the official Chrome DevTools MCP/Puppeteer connector. This adapter
does not install or run that MCP server. Selected pages use flattened sessions
on our owned browser socket; closing it never closes the user's browser.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import inspect
import json
import math
import os
from pathlib import Path
import re
import stat
import unicodedata
from typing import Any
from uuid import uuid4

from memory.runtime_session_checkpoint import (
    CheckpointValidationError,
    validate_session_id,
)
from skills.call_context import context_enabled, current_context
from skills.impl.browser_use import BrowserController, CDPConnection


class ExistingChromeError(Exception):
    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status
        super().__init__(code)


def _require_owner(owner: str) -> None:
    if not context_enabled():
        raise ExistingChromeError("existing_chrome_context_disabled", 403)
    if not owner or current_context().session_id != owner:
        raise ExistingChromeError("existing_chrome_owner_required", 403)


def _endpoint(profile: Path) -> str:
    """Read only the explicitly selected user-data root, never scan profiles."""
    try:
        if profile.is_symlink() or not profile.is_dir():
            raise ExistingChromeError("existing_chrome_unavailable", 503)
        descriptor = os.open(
            profile / "DevToolsActivePort", os.O_RDONLY | os.O_NOFOLLOW
        )
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_size > 1024
            ):
                raise ExistingChromeError("existing_chrome_endpoint_invalid", 422)
            raw = os.read(descriptor, 1025)
        finally:
            os.close(descriptor)
        lines = raw.decode("ascii").splitlines()
        if (
            len(raw) > 1024
            or len(lines) != 2
            or re.fullmatch(r"[1-9][0-9]{0,4}", lines[0]) is None
            or not 1024 <= int(lines[0]) <= 65535
            or re.fullmatch(r"/devtools/browser/[A-Za-z0-9_-]{1,128}", lines[1]) is None
        ):
            raise ExistingChromeError("existing_chrome_endpoint_invalid", 422)
        return f"ws://127.0.0.1:{lines[0]}{lines[1]}"
    except ExistingChromeError:
        raise
    except (OSError, UnicodeError, ValueError):
        raise ExistingChromeError("existing_chrome_unavailable", 503) from None


# Permits carry an object and the actual asyncio task. A child task inheriting a
# ContextVar cannot inherit the operator-view/internal-discovery exemption.
_permit: ContextVar[tuple | None] = ContextVar(
    "feral_existing_chrome_permit", default=None
)


@contextmanager
def _internal(subject, kind):
    token = _permit.set((subject, kind, asyncio.current_task()))
    try:
        yield
    finally:
        _permit.reset(token)


def _permitted(subject, kind) -> bool:
    value = _permit.get()
    return value is not None and value == (subject, kind, asyncio.current_task())


class _BrowserSocket(CDPConnection):
    """One bounded reader, correlated replies, session-specific event fanout."""

    def __init__(self):
        super().__init__(host="127.0.0.1", port=0)
        self._ws: Any = None
        self._retired = False
        self._sessions: dict[str, _SelectedCDP] = {}
        self._expected: dict[int, str | None] = {}
        self._write_lock = asyncio.Lock()

    async def open(self, endpoint: str):
        import websockets

        try:
            ws = await asyncio.wait_for(
                websockets.connect(
                    endpoint,
                    open_timeout=20,
                    close_timeout=2,
                    max_size=16 * 1024 * 1024,
                    ping_interval=20,
                    proxy=None,
                ),
                21,
            )
            if self._retired:
                await ws.close()
                raise ExistingChromeError("existing_chrome_connection_changed")
            self._ws, self._connected = ws, True
            self._recv_task = asyncio.create_task(
                self._receive_loop(), name="existing-chrome-cdp-reader"
            )
        except ExistingChromeError:
            raise
        except TimeoutError:
            raise ExistingChromeError("existing_chrome_timeout", 504) from None
        except Exception:
            raise ExistingChromeError("existing_chrome_unavailable", 503) from None

    async def connect(self, *args, **kwargs):
        raise ExistingChromeError("existing_chrome_endpoint_unsupported", 422)

    async def send_command(
        self, method: str, params: dict | None = None, timeout: float = 8
    ) -> dict:
        if not _permitted(self, "discovery"):
            raise ExistingChromeError("existing_chrome_owner_required", 403)
        if method not in {
            "Browser.getVersion",
            "Target.getTargets",
            "Target.getTargetInfo",
            "Target.attachToTarget",
            "Target.detachFromTarget",
        }:
            raise ExistingChromeError("existing_chrome_endpoint_unsupported", 422)
        return await self.request(method, params, None, timeout)

    async def request(self, method, params, session_id, timeout, admit=None):
        if not self.connected or self._retired or len(self._pending) >= 128:
            raise ExistingChromeError("existing_chrome_not_connected")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0 < timeout <= 30
        ):
            raise ExistingChromeError("existing_chrome_protocol_error", 502)
        self._msg_id += 1
        identifier = self._msg_id
        future = asyncio.get_running_loop().create_future()
        self._pending[identifier] = future
        self._expected[identifier] = session_id
        packet = {"id": identifier, "method": method, "params": params or {}}
        if session_id is not None:
            packet["sessionId"] = session_id
        try:
            async with asyncio.timeout(timeout):
                async with self._write_lock:
                    if not self.connected or self._retired:
                        raise ExistingChromeError("existing_chrome_connection_changed")
                    if admit is not None:
                        admit()
                    await self._ws.send(json.dumps(packet))
                return await future
        except asyncio.CancelledError:
            raise
        except ExistingChromeError:
            raise
        except TimeoutError:
            raise ExistingChromeError("existing_chrome_timeout", 504) from None
        except Exception:
            raise ExistingChromeError("existing_chrome_protocol_error", 502) from None
        finally:
            self._pending.pop(identifier, None)
            self._expected.pop(identifier, None)
            if not future.done():
                future.cancel()

    async def _receive_loop(self):
        try:
            async for raw in self._ws:
                try:
                    packet = json.loads(raw)
                    if not isinstance(packet, dict):
                        continue
                    identifier = packet.get("id")
                    if type(identifier) is int and identifier in self._pending:
                        if packet.get("sessionId") != self._expected[identifier]:
                            continue
                        future = self._pending[identifier]
                        if future.done():
                            continue
                        if "error" in packet or not isinstance(
                            packet.get("result"), dict
                        ):
                            future.set_exception(
                                ExistingChromeError(
                                    "existing_chrome_protocol_error", 502
                                )
                            )
                        else:
                            future.set_result(packet["result"])
                    elif isinstance(packet.get("method"), str):
                        params = packet.get("params") or {}
                        if not isinstance(params, dict):
                            continue
                        if (
                            packet["method"] == "Target.detachedFromTarget"
                            and packet.get("sessionId") is None
                        ):
                            selected = self._sessions.pop(params.get("sessionId"), None)
                            if selected:
                                selected._retired = True
                        if (
                            packet["method"] == "Target.targetDestroyed"
                            and packet.get("sessionId") is None
                        ):
                            for session, selected in list(self._sessions.items()):
                                if selected.target_id == params.get("targetId"):
                                    selected._retired = True
                                    del self._sessions[session]
                        selected = self._sessions.get(packet.get("sessionId"))
                        if selected and selected.connected:
                            for listener in selected._event_listeners:
                                try:
                                    result = listener(packet)
                                    if inspect.isawaitable(result):
                                        task = asyncio.create_task(result)
                                        selected._bg_tasks.add(task)
                                        task.add_done_callback(
                                            selected._bg_tasks.discard
                                        )
                                except Exception:
                                    pass
                except (ValueError, TypeError):
                    continue
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        finally:
            self._connected = False
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(
                        ExistingChromeError("existing_chrome_not_connected")
                    )

    async def disconnect(self):
        self._retired, self._connected = True, False
        tasks = []
        for selected in self._sessions.values():
            selected._retired = True
            tasks.extend(selected._bg_tasks)
            selected._bg_tasks.clear()
        self._sessions.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=1)
        if self._recv_task and self._recv_task is not asyncio.current_task():
            self._recv_task.cancel()
            await asyncio.gather(self._recv_task, return_exceptions=True)
        if self._ws is not None:
            try:
                await asyncio.wait_for(self._ws.close(), 3)
            except Exception:
                pass
        for future in self._pending.values():
            if not future.done():
                future.set_exception(
                    ExistingChromeError("existing_chrome_not_connected")
                )


class _SelectedCDP(CDPConnection):
    _owner_methods = frozenset(
        {
            "Runtime.evaluate",
            "Runtime.callFunctionOn",
            "DOM.getDocument",
            "DOM.describeNode",
            "DOM.resolveNode",
            "DOM.requestNode",
            "DOM.querySelector",
            "DOM.querySelectorAll",
            "DOM.getBoxModel",
            "DOM.focus",
            "DOM.enable",
            "Accessibility.enable",
            "Accessibility.getFullAXTree",
            "Page.navigate",
            "Page.enable",
            "Page.captureScreenshot",
            "Input.dispatchMouseEvent",
            "Input.dispatchKeyEvent",
            "Input.insertText",
        }
    )

    def __init__(
        self, socket: _BrowserSocket, session: str, target: str, binding: dict
    ):
        super().__init__(host="127.0.0.1", port=0)
        self._socket, self._session, self._target = socket, session, target
        self._binding, self._retired = dict(binding), False
        socket._sessions[session] = self

    @property
    def connected(self):
        return (
            not self._retired
            and self._socket.connected
            and self._socket._sessions.get(self._session) is self
        )

    @property
    def target_id(self):
        return self._target

    @property
    def is_page_target(self):
        return self.connected

    def require_owner(self):
        _require_owner(self._binding["owner_session_id"])
        if not self.connected:
            raise ExistingChromeError("existing_chrome_connection_changed")

    def require_action(self):
        self.require_owner()
        from agents.tool_runner import current_browser_resource_binding

        try:
            captured = current_browser_resource_binding()
        except Exception:
            raise ExistingChromeError("existing_chrome_connection_changed") from None
        if captured != self._binding:
            raise ExistingChromeError("existing_chrome_owner_required", 403)

    def _admit(self, method, params):
        if not self.connected:
            raise ExistingChromeError("existing_chrome_connection_changed")
        if _permitted(self, "setup"):
            if method not in {"Page.enable", "DOM.enable", "Runtime.enable"}:
                raise ExistingChromeError("existing_chrome_endpoint_unsupported", 422)
        elif _permitted(self, "view"):
            if method not in {
                "DOM.getDocument",
                "Page.captureScreenshot",
                "Runtime.evaluate",
            }:
                raise ExistingChromeError("existing_chrome_endpoint_unsupported", 422)
            if method == "Runtime.evaluate" and not str(
                (params or {}).get("expression", "")
            ).startswith("/* FERAL_VIEW_MASK_"):
                raise ExistingChromeError("existing_chrome_endpoint_unsupported", 422)
        else:
            self.require_action()
            if method not in self._owner_methods:
                raise ExistingChromeError("existing_chrome_endpoint_unsupported", 422)

    async def send_command(
        self, method: str, params: dict | None = None, timeout: float = 8
    ):
        self._admit(method, params)
        result = await self._socket.request(
            method,
            params,
            self._session,
            min(timeout, 8),
            admit=lambda: self._admit(method, params),
        )
        self._admit(method, params)
        return result

    async def connect(self, *args, **kwargs):
        raise ExistingChromeError("existing_chrome_endpoint_unsupported", 422)

    async def disconnect(self):
        self._retired = True
        if self._socket._sessions.get(self._session) is self:
            del self._socket._sessions[self._session]
            if self._socket.connected:
                try:
                    with _internal(self._socket, "discovery"):
                        await self._socket.send_command(
                            "Target.detachFromTarget", {"sessionId": self._session}
                        )
                except Exception:
                    pass
        for task in list(self._bg_tasks):
            task.cancel()
        self._bg_tasks.clear()


class ExistingChromeController(BrowserController):
    """Existing implementations with no global HTTP/Playwright/browser ownership."""

    _supported = frozenset(
        {
            "navigate",
            "click",
            "type_text",
            "fill",
            "fill_form",
            "hover",
            "scroll",
            "press_key",
            "click_at",
            "snapshot",
            "get_page_info",
            "wait",
        }
    )

    def __init__(self, cdp: _SelectedCDP):
        super().__init__()
        self._cdp, self._attached_target_id = cdp, cdp.target_id
        self._dialog_policy = "manual"

    def approval_binding(self) -> dict:
        # Trusted metadata lookup is also used while resolving an HTTP review,
        # outside the action's task context. This grants no browser operation.
        if not self._cdp.connected:
            raise ExistingChromeError("existing_chrome_connection_changed")
        return dict(self._cdp._binding)

    async def initialize(self) -> bool:
        return False

    async def capture_view_frame(self, target_id: str) -> dict:
        if not self.connected or target_id != self._attached_target_id:
            return {"success": False, "error_code": "view_target_changed"}
        with _internal(self._cdp, "view"):
            return await super().capture_view_frame(target_id)

    async def screenshot(self, full_page: bool = False) -> dict:
        try:
            self._cdp.require_action()
            if full_page is not False:
                raise ExistingChromeError("existing_chrome_endpoint_unsupported", 422)
            return await self.capture_view_frame(self._attached_target_id)
        except ExistingChromeError as error:
            return {"success": False, "error_code": error.code}

    async def execute(self, endpoint_id: str, args: dict | None = None) -> dict:
        if endpoint_id not in self._supported | {"screenshot"}:
            return {
                "success": False,
                "error_code": "existing_chrome_endpoint_unsupported",
            }
        try:
            self._cdp.require_action()
            return await super().execute(endpoint_id, args)
        except ExistingChromeError as error:
            return {"success": False, "error_code": error.code}

    async def close(self):
        await self._cdp.disconnect()


def _guard_method(name, implementation):
    @wraps(implementation)
    async def guarded(self, *args, **kwargs):
        try:
            self._cdp.require_action()
            if name not in self._supported:
                raise ExistingChromeError("existing_chrome_endpoint_unsupported", 422)
            result = await implementation(self, *args, **kwargs)
            self._cdp.require_action()
            return result
        except ExistingChromeError as error:
            return {"success": False, "error_code": error.code}

    return guarded


# Guard every inherited public async method, including reads that may return a
# cached result without touching CDP. New upstream endpoints fail closed until
# explicitly included. Private implementation helpers retain their CDP fence.
for _name, _method in BrowserController.__dict__.items():
    if (
        not _name.startswith("_")
        and inspect.iscoroutinefunction(_method)
        and _name not in ExistingChromeController.__dict__
    ):
        setattr(ExistingChromeController, _name, _guard_method(_name, _method))


class ExistingChromeConnector:
    def __init__(self, owner_session_id: str, profile_dir: Path | None = None):
        try:
            validate_session_id(owner_session_id)
        except CheckpointValidationError:
            raise ExistingChromeError("existing_chrome_owner_required", 403) from None
        self.owner_session_id = owner_session_id
        self._profile = (
            Path(profile_dir)
            if profile_dir is not None
            else Path.home() / "Library/Application Support/Google/Chrome"
        )
        self._production_profile = profile_dir is None
        self._socket: _BrowserSocket | None = None
        self._controller: ExistingChromeController | None = None
        self._generation = str(uuid4())
        self._selecting = False

    @property
    def controller(self):
        return (
            self._controller
            if self._controller is not None and self._controller.connected
            else None
        )

    def status(self) -> dict:
        selected = self.controller
        return {
            "mode": "existing_chrome",
            "connected": self._socket is not None and self._socket.connected,
            "connection_id": self._generation,
            "owner_session_id": self.owner_session_id,
            "selected_target_id": selected._attached_target_id if selected else None,
        }

    def _current(self, socket, generation):
        _require_owner(self.owner_session_id)
        if (
            self._socket is not socket
            or self._generation != generation
            or not socket.connected
        ):
            raise ExistingChromeError("existing_chrome_connection_changed")

    async def connect(self, consent: bool) -> dict:
        if consent is not True:
            raise ExistingChromeError("existing_chrome_consent_required", 400)
        _require_owner(self.owner_session_id)
        if self._socket is not None:
            raise ExistingChromeError("existing_chrome_busy")
        if self._production_profile and any(
            path.is_symlink()
            for path in [self._profile, *list(self._profile.parents)[:3]]
        ):
            raise ExistingChromeError("existing_chrome_endpoint_invalid", 422)
        socket, generation = _BrowserSocket(), str(uuid4())
        self._socket, self._generation = socket, generation
        try:
            endpoint = await asyncio.to_thread(_endpoint, self._profile)
            if self._socket is not socket or self._generation != generation:
                raise ExistingChromeError("existing_chrome_connection_changed")
            await socket.open(endpoint)
            self._current(socket, generation)
            with _internal(socket, "discovery"):
                version = await socket.send_command("Browser.getVersion")
            self._current(socket, generation)
            product = version.get("product", "")
            match = (
                re.fullmatch(r"(?:Chrome|HeadlessChrome)/(\d+)\.[0-9.]+", product)
                if isinstance(product, str)
                else None
            )
            if match is None or int(match[1]) < 144:
                raise ExistingChromeError("existing_chrome_unsupported_version", 422)
            return {"success": True, **self.status()}
        except BaseException:
            if self._socket is socket:
                self._socket = None
            await socket.disconnect()
            raise

    async def targets(self) -> list[dict]:
        _require_owner(self.owner_session_id)
        socket, generation = self._socket, self._generation
        if socket is None or not socket.connected:
            raise ExistingChromeError("existing_chrome_not_connected")
        with _internal(socket, "discovery"):
            response = await socket.send_command("Target.getTargets")
        self._current(socket, generation)
        infos = response.get("targetInfos")
        if not isinstance(infos, list) or len(infos) > 512:
            raise ExistingChromeError("existing_chrome_protocol_error", 502)
        rows: list[dict] = []
        for info in infos:
            if (
                isinstance(info, dict)
                and info.get("type") == "page"
                and isinstance(info.get("url"), str)
                and (
                    info["url"].startswith(("http://", "https://"))
                    or info["url"] == "about:blank"
                )
            ):
                target = info.get("targetId")
                if (
                    not isinstance(target, str)
                    or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", target) is None
                    or any(row["id"] == target for row in rows)
                ):
                    raise ExistingChromeError("existing_chrome_protocol_error", 502)
                label = f"Chrome tab {len(rows) + 1}"
                title = info.get("title")
                if isinstance(title, str):
                    title = "".join(
                        char
                        for char in title[:1024]
                        if not unicodedata.category(char).startswith("C")
                    ).strip()[:160]
                    if title:
                        label = (
                            (f"{label}: {title}")
                            .encode("utf-8")[:180]
                            .decode("utf-8", errors="ignore")
                        )
                rows.append({"id": target, "label": label})
        return rows

    async def select(self, target_id: str) -> dict:
        _require_owner(self.owner_session_id)
        if self._selecting:
            raise ExistingChromeError("existing_chrome_busy")
        if (
            not isinstance(target_id, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", target_id) is None
        ):
            raise ExistingChromeError("existing_chrome_target_unavailable")
        socket, before = self._socket, self._generation
        if socket is None or not socket.connected:
            raise ExistingChromeError("existing_chrome_not_connected")
        self._selecting = True
        selected = None
        try:
            targets = await self.targets()
            self._current(socket, before)
            if target_id not in {row["id"] for row in targets}:
                raise ExistingChromeError("existing_chrome_target_unavailable")
            previous, self._controller = self._controller, None
            self._generation = str(uuid4())
            generation = self._generation
            if previous is not None:
                await previous.close()
            self._current(socket, generation)
            with _internal(socket, "discovery"):
                result = await socket.send_command(
                    "Target.attachToTarget", {"targetId": target_id, "flatten": True}
                )
            self._current(socket, generation)
            session = result.get("sessionId")
            if (
                not isinstance(session, str)
                or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session) is None
            ):
                raise ExistingChromeError("existing_chrome_protocol_error", 502)
            binding = {
                "connection_id": generation,
                "target_id": target_id,
                "owner_session_id": self.owner_session_id,
            }
            selected = _SelectedCDP(socket, session, target_id, binding)
            with _internal(socket, "discovery"):
                info = await socket.send_command(
                    "Target.getTargetInfo", {"targetId": target_id}
                )
            self._current(socket, generation)
            if (
                not isinstance(info.get("targetInfo"), dict)
                or info["targetInfo"].get("targetId") != target_id
                or info["targetInfo"].get("type") != "page"
            ):
                raise ExistingChromeError("existing_chrome_target_unavailable")
            with _internal(selected, "setup"):
                for method in ("Page.enable", "DOM.enable", "Runtime.enable"):
                    await selected.send_command(method)
                    self._current(socket, generation)
            self._controller = ExistingChromeController(selected)
            return {"success": True, **self.status()}
        except BaseException:
            # A lost attach reply may already have created a browser session.
            # Closing our exact socket retires that uncertain session too.
            if self._socket is socket:
                self._socket, self._controller = None, None
                self._generation = str(uuid4())
            await socket.disconnect()
            raise
        finally:
            self._selecting = False

    async def disconnect(self) -> dict:
        socket, self._socket = self._socket, None
        selected, self._controller = self._controller, None
        self._generation = str(uuid4())
        if selected is not None:
            selected._cdp._retired = True
        if socket is not None:
            await socket.disconnect()
        return {"success": True, **self.status(), "browser_closed": False}
