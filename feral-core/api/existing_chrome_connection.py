"""Explicit local connection ownership; no browser launch or action dispatch."""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from memory.runtime_session_checkpoint import validate_session_id
from skills.call_context import bind_context, context_enabled, current_context
from skills.impl.existing_chrome import ExistingChromeConnector, ExistingChromeError


class ChromeConnectionManager:
    def __init__(self, brain: Callable[[], Any], factory=ExistingChromeConnector):
        self._brain = brain
        self.factory = factory
        self._lock = asyncio.Lock()
        self._connector: Any = None
        self._previous: Any = None
        self._installed: Any = None
        self._supplier = self._resource

    def _resource(self):
        browser = getattr(self._brain(), "browser", None)
        binding = getattr(browser, "approval_binding", None)
        return binding() if callable(binding) else None

    @staticmethod
    def _owner(session_id: str):
        validate_session_id(session_id)
        if not context_enabled():
            raise ExistingChromeError("existing_chrome_context_disabled", 403)
        if current_context().session_id != session_id:
            raise ExistingChromeError("existing_chrome_owner_required", 403)

    def _captured(self, session_id: str, connection_id: str):
        self._owner(session_id)
        connector = self._connector
        if connector is None:
            raise ExistingChromeError("existing_chrome_not_connected")
        status = connector.status()
        if status.get("owner_session_id") != session_id:
            raise ExistingChromeError("existing_chrome_owner_required", 403)
        if status.get("connection_id") != connection_id:
            raise ExistingChromeError("existing_chrome_connection_changed")
        return connector

    async def status(self, session_id: str):
        self._owner(session_id)
        async with self._lock:
            self._owner(session_id)
            if self._connector is None:
                return {"success": True, "connected": False, "mode": "existing_chrome",
                        "connection_id": None, "owner_session_id": None,
                        "selected_target_id": None, "targets": []}
            connector = self._connector
            brain = self._brain()
            browser = brain.browser
            orch = getattr(brain, "orchestrator", None)
            runner = getattr(orch, "tool_runner", None)
            value = connector.status()
            if value.get("owner_session_id") != session_id:
                raise ExistingChromeError("existing_chrome_owner_required", 403)
            targets = await connector.targets() if value.get("connected") else []
            if (self._brain() is not brain or brain.browser is not browser
                    or connector is not self._connector or connector.status() != value
                    or getattr(brain, "orchestrator", None) is not orch
                    or getattr(orch, "tool_runner", None) is not runner):
                raise ExistingChromeError("existing_chrome_connection_changed")
            self._owner(session_id)
            return {**value, "success": True, "targets": targets}

    async def connect(self, session_id: str, consent: bool):
        self._owner(session_id)
        if consent is not True:
            raise ExistingChromeError("existing_chrome_consent_required", 400)
        async with self._lock:
            self._owner(session_id)
            brain = self._brain()
            if self._connector is not None or bool(getattr(brain.browser, "connected", False)):
                raise ExistingChromeError("existing_chrome_busy")
            orch = getattr(brain, "orchestrator", None)
            runner = getattr(orch, "tool_runner", None)
            if runner is None or not callable(getattr(runner, "browser_resource_scope", None)):
                raise ExistingChromeError("existing_chrome_authority_unavailable", 503)
            previous = brain.browser
            connector = self.factory(session_id)
            try:
                await connector.connect(consent)
                targets = await connector.targets()
                if self._brain() is not brain or brain.browser is not previous or bool(getattr(previous, "connected", False)):
                    raise ExistingChromeError("existing_chrome_connection_changed")
                self._owner(session_id)
                if brain.orchestrator is not orch or orch.tool_runner is not runner:
                    raise ExistingChromeError("existing_chrome_connection_changed")
                self._previous = previous
                self._connector = connector
                return {**connector.status(), "success": True, "targets": targets}
            except BaseException:
                await connector.disconnect()
                raise

    @staticmethod
    def _inject_web(browser):
        from skills.impl import get_implementation
        web = get_implementation("web_actions")
        if web is not None and callable(getattr(web, "set_browser", None)):
            web.set_browser(browser)

    async def select(self, session_id: str, connection_id: str, target_id: str):
        async with self._lock:
            connector = self._captured(session_id, connection_id)
            brain = self._brain()
            orch = getattr(brain, "orchestrator", None)
            runner = getattr(orch, "tool_runner", None)
            if runner is None or not callable(getattr(runner, "browser_resource_scope", None)):
                raise ExistingChromeError("existing_chrome_authority_unavailable", 503)
            expected = self._installed if self._installed is not None else self._previous
            if brain.browser is not expected or (self._installed is None and bool(getattr(expected, "connected", False))):
                raise ExistingChromeError("existing_chrome_connection_changed")
            try:
                await connector.select(target_id)
                controller = connector.controller
                if controller is None or not controller.connected:
                    raise ExistingChromeError("existing_chrome_target_unavailable")
                selected_status = connector.status()
                targets = await connector.targets()
                self._owner(session_id)
                if (self._brain() is not brain or brain.browser is not expected
                        or connector.controller is not controller or connector.status() != selected_status):
                    raise ExistingChromeError("existing_chrome_connection_changed")
                if brain.orchestrator is not orch or orch.tool_runner is not runner:
                    raise ExistingChromeError("existing_chrome_connection_changed")
                orch._browser_resource_supplier = self._supplier
                # Publish synchronously, retaining exact rollback references even
                # when the web-actions setter raises after changing its backing.
                brain.browser = controller
                self._installed = controller
                self._inject_web(controller)
                return {**selected_status, "success": True, "targets": targets}
            except BaseException:
                cleanup = asyncio.create_task(self._close(connector))
                await asyncio.shield(cleanup)
                raise

    async def disconnect(self, session_id: str, connection_id: str):
        async with self._lock:
            connector = self._captured(session_id, connection_id)
            return await self._close(connector)

    async def _close(self, connector):
        brain = self._brain()
        installed = self._installed
        orch = getattr(brain, "orchestrator", None)
        runner = getattr(orch, "tool_runner", None)
        # Restore only our exact installed object, never a replacement runtime.
        try:
            result = await connector.disconnect()
        finally:
            if (self._brain() is brain and brain.browser is installed and installed is not None
                    and getattr(brain, "orchestrator", None) is orch
                    and getattr(orch, "tool_runner", None) is runner):
                brain.browser = self._previous
                self._installed = None
                self._inject_web(self._previous)
        if result.get("success") is not True:
            raise ExistingChromeError("existing_chrome_operation_unconfirmed", 502)
        if self._connector is connector:
            self._connector = None
            self._installed = None
            self._previous = None
        return {"success": True, "connected": False, "mode": "existing_chrome",
                "connection_id": None, "owner_session_id": None,
                "selected_target_id": None, "targets": [],
                "browser_closed": False, "disconnected": True}

    async def shutdown(self):
        async with self._lock:
            if self._connector is not None:
                await self._close(self._connector)


def local_owner_scope(session_id: str):
    """Bind only the explicit operator identity validated by the HTTP contract."""
    validate_session_id(session_id)
    return bind_context(session_id=session_id, surface="http_api", tool_name="browser_connection")
