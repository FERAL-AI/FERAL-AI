"""Authored local calculation, not generated source or an expression evaluator."""
from skills.base import BaseSkill
from skills.call_context import current_context


class DeveloperMathSkill(BaseSkill):
    skill_id = "developer_math"

    def __init__(self):
        super().__init__(self.skill_id)
        self.invocations = 0  # Disposable harness diagnostic, not user data.

    async def execute(self, endpoint_id, args, vault):
        if endpoint_id != "calculate":
            return {"success": False, "status_code": 404, "data": None, "error": "Unknown endpoint"}
        values = [args.get("a"), args.get("b")]
        if any(type(value) is not int or abs(value) > 1_000_000 for value in values):
            return {"success": False, "status_code": 400, "data": None, "error": "Expected two integers between -1000000 and 1000000"}
        self.invocations += 1
        return {
            "success": True,
            "status_code": 200,
            "data": {"sum": values[0] + values[1], "session_id": current_context().session_id},
            "error": None,
        }
