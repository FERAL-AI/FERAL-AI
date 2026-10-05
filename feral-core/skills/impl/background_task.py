"""Background task transport through the existing local control plane."""
from skills.base import BaseSkill
from skills.impl import register_skill


@register_skill
class BackgroundTaskSkill(BaseSkill):
    def __init__(self):
        super().__init__("background_task")

    async def execute(self, endpoint_id, args, vault):
        # Lazy import keeps skill autoload independent of application startup.
        from api.routes.taskflows import execute_background_task_skill
        result = await execute_background_task_skill(endpoint_id, args or {})
        ok = result.get("ok", "error" not in result)
        return {"success": ok, "status_code": 200 if ok else 409,
                "data": result, "error": result.get("error"),
                **({"error_code": result["error_code"]} if "error_code" in result else {})}
