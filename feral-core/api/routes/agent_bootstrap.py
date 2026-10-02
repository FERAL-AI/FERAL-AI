"""Explicit continuation endpoints. Existing global auth applies normally."""
from fastapi import APIRouter, HTTPException
from security.agent_bootstrap_continuation import BootstrapRefusal


def create_agent_bootstrap_router(controller_provider):
    router = APIRouter()
    def controller():
        value = controller_provider()
        if value is None:
            raise HTTPException(503, detail={"code": "continuation_unsupported", "message": "Reviewed agent continuation is unavailable."})
        return value
    def token(body):
        value = body.get("review_token")
        if set(body) != {"review_token"} or not isinstance(value, str) or len(value) != 43 or not all(c.isascii() and (c.isalnum() or c in "_-") for c in value):
            raise HTTPException(400, detail={"code": "invalid_review", "message": "A server-issued continuation review is required."})
        return value
    @router.get("/api/security/agent-bootstrap/status")
    async def status():
        value = controller_provider()
        if value is None:
            # Passive discovery does not attempt startup or inspect OS state.
            # These availability flags grant no agent capability. The required
            # fence remains conservative until a real controller can verify it.
            return {
                "supported": False, "phase": "pending", "in_flight": False,
                "restart_required": False, "credentials_available": False,
                "memory_available": False, "orchestrator_available": False,
                "bootstrap_required": True, "agent_ready": False,
                "code": "continuation_unsupported",
                "message": "Reviewed agent continuation is unavailable; agent readiness was not inspected.",
            }
        return value.status()
    @router.post("/api/security/agent-bootstrap/review")
    async def review(body: dict):
        if body: raise HTTPException(400, detail={"code": "invalid_review", "message": "Review accepts an empty object only."})
        try: return controller().review()
        except BootstrapRefusal as exc: raise HTTPException(409, detail={"code": exc.code, "message": str(exc)}) from None
    @router.post("/api/security/agent-bootstrap")
    async def confirm(body: dict):
        try: return await controller().confirm(token(body))
        except BootstrapRefusal as exc: raise HTTPException(409, detail={"code": exc.code, "message": str(exc)}) from None
    @router.post("/api/security/agent-bootstrap/cancel")
    async def cancel(body: dict):
        controller().cancel(token(body)); return {"ok": True}
    return router
