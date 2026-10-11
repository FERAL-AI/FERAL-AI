"""
FERAL SDK — Build plugins, tools, device adapters, and GenUI components.

Quick start:

    from feral_sdk import FeralPlugin, feral_tool, FeralClient

    class MyPlugin(FeralPlugin):
        name = "my-plugin"

        @feral_tool(description="Say hello")
        async def greet(self, name: str) -> dict:
            return {"message": f"Hello, {name}!"}
"""

__version__ = "0.1.0"

from feral_sdk.plugin import FeralPlugin
from feral_sdk.tool import feral_tool
from feral_sdk.client import FeralClient, ChatTurnReceipt, TurnProcessingOutcome, ChatTurnError, ChatTurnTimeout
from feral_sdk.device import HUPDevice
from feral_sdk.manifest import SkillManifest, Endpoint, Parameter
from feral_sdk.genui import GenUIComponent, GenUICard, GenUIMetric

__all__ = [
    "FeralPlugin",
    "feral_tool",
    "FeralClient",
    "ChatTurnReceipt",
    "TurnProcessingOutcome",
    "ChatTurnError",
    "ChatTurnTimeout",
    "HUPDevice",
    "SkillManifest",
    "Endpoint",
    "Parameter",
    "GenUIComponent",
    "GenUICard",
    "GenUIMetric",
]
