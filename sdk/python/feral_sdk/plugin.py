"""
FeralPlugin — Base class for all FERAL plugins.

A plugin bundles tools, device adapters, and UI components into a single
installable package that the Brain discovers and loads at startup.
"""

from __future__ import annotations
import logging
import inspect
import re
from copy import deepcopy
from typing import Any

logger = logging.getLogger("feral.sdk.plugin")


class FeralPlugin:
    """Base class for FERAL plugins.

    Subclass this to create a plugin. Decorate methods with @feral_tool
    to expose them as agent tools.

    Example::

        class WeatherPlugin(FeralPlugin):
            name = "weather"
            description = "Real-time weather data"

            @feral_tool(description="Get current weather for a city")
            async def current(self, city: str) -> dict:
                ...
    """

    name: str = ""
    description: str = ""
    version: str = "0.1.0"
    author: str = ""

    def __init__(self):
        if not self.name:
            self.name = type(self).__name__.lower().replace("plugin", "")
        _validate_identifier(self.name, "plugin name")
        self._tools: dict[str, Any] = {}
        self._discover_tools()
        if len(self._tools) > 64:
            raise ValueError("At most 64 plugin tools are supported")

    def _discover_tools(self):
        """Find all methods decorated with @feral_tool."""
        for attr_name in dir(self):
            method = getattr(self, attr_name, None)
            if callable(method) and hasattr(method, "_feral_tool_meta"):
                meta = method._feral_tool_meta
                tool_id = meta.get("name") or attr_name
                _validate_identifier(tool_id, "tool name")
                if tool_id in self._tools:
                    raise ValueError(f"Duplicate tool name: {tool_id}")
                if not inspect.iscoroutinefunction(method):
                    raise ValueError(f"Tool {tool_id} must be async")
                self._tools[tool_id] = {
                    "handler": method,
                    "meta": meta,
                }

    @property
    def tools(self) -> dict[str, dict]:
        return {key: {"handler": value["handler"], "meta": deepcopy(value["meta"])}
                for key, value in self._tools.items()}

    def to_manifest(self) -> dict:
        """Generate a FERAL skill manifest from this plugin's tools.

        PYTHON endpoints have no HTTP URL. Export ``runtime_skill()`` as a
        module-level class in impl.py for the existing runtime loader.
        Conservative generated endpoints require review; this is not a grant.
        """
        endpoints = []
        for tool_id, info in self._tools.items():
            meta = info["meta"]
            params = []
            for pname, pinfo in (meta.get("parameters") or {}).items():
                param = {
                    "name": pname,
                    "type": pinfo.get("type", "string"),
                    "description": pinfo.get("description", ""),
                    "required": pinfo.get("required", True),
                }
                if "default" in pinfo:
                    param["default"] = deepcopy(pinfo["default"])
                params.append(param)
            endpoints.append({
                "id": tool_id,
                "method": "PYTHON",
                "url": "",
                "safety_tier": "confirm",
                "requires_user_approval": True,
                "description": meta.get("description", ""),
                "params": params,
            })
        return {
            "skill_id": self.name,
            "version": self.version,
            "author": self.author,
            "description": self.description,
            "brand": {"name": self.name.replace("_", " ").title(), "icon": "puzzle"},
            "endpoints": endpoints,
            "trigger_phrases": [],
            "categories": ["plugin"],
        }

    @classmethod
    def runtime_skill(cls):
        """Return a zero-argument BaseSkill adapter class for export in impl.py.

        SDK import and manifest generation stay independent of feral-core.
        Call this factory only in the runtime environment, where both packages
        are installed. The adapter does not authorize execution or start services.
        The current loader does not call on_load/on_unload hooks.
        """
        try:
            from skills.base import BaseSkill
        except ImportError as exc:
            raise RuntimeError(
                "runtime_skill() requires the FERAL runtime (skills.base) in this "
                "interpreter; install feral-sdk in that same runtime environment"
            ) from exc

        class PluginSkill(BaseSkill):
            def __init__(self):
                self.plugin = cls()
                super().__init__(self.plugin.name)

            async def execute(self, endpoint_id: str, args: dict, vault: dict) -> dict:
                return await self.plugin.execute(endpoint_id, args, vault)

        PluginSkill.__name__ = cls.__name__ + "Skill"
        return PluginSkill

    async def execute(self, endpoint_id: str, args: dict, vault: dict) -> dict:
        """Execute a tool endpoint. Called by the Brain's SkillExecutor."""
        tool = self._tools.get(endpoint_id)
        if not tool:
            return {"success": False, "status_code": 404, "data": None, "error": f"Unknown endpoint: {endpoint_id}"}
        try:
            result = await tool["handler"](**args)
            return {"success": True, "status_code": 200, "data": result, "error": None}
        except Exception as e:
            return {"success": False, "status_code": 500, "data": None, "error": str(e)}

    async def on_load(self):
        """Called when the plugin is loaded by the Brain. Override for setup."""
        pass

    async def on_unload(self):
        """Called when the plugin is unloaded. Override for cleanup."""
        pass


def _validate_identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", value) or "__" in value:
        raise ValueError(f"Invalid {label}: use at most 64 identifier characters without '__'")
