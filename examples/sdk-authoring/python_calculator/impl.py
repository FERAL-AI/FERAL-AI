"""A known read-only SDK plugin with the explicit runtime adapter export."""
from __future__ import annotations
from feral_sdk import FeralPlugin, feral_tool


class SDKCalculator(FeralPlugin):
    name = "sdk_calculator"
    description = "Add two bounded integers without account, file or network access."
    author = "FERAL SDK example"
    version = "1.0.0"

    def __init__(self):
        super().__init__()
        self.invocations = 0

    @feral_tool(description="Add two integers, each between -1000000 and 1000000.")
    async def calculate(self, a: int, b: int) -> dict:
        if type(a) is not int or type(b) is not int or abs(a) > 1_000_000 or abs(b) > 1_000_000:
            raise ValueError("Arguments must be bounded integers")
        from skills.call_context import current_context
        self.invocations += 1
        return {"sum": a + b, "session_id": current_context().session_id}


# The existing loader discovers BaseSkill classes, not standalone FeralPlugin.
SDKCalculatorSkill = SDKCalculator.runtime_skill()
