"""Reviewed initialization router bound to the deferred server coordinator.

Root must provide a stable controller bound to the existing coordinator;
no route creates another coordinator or changes the release acceptance gate.
The passive GET reports missing optional setup with HTTP 200 and no prepare
authority; underlying storage remains uninspected. Mutations still refuse it.
"""
from fastapi import APIRouter, HTTPException
from security.vault_initialization_api import VaultInitializationAPIRefusal


def create_vault_initialization_router(controller_provider):
    router = APIRouter()

    def controller():
        value = controller_provider()
        if value is None:
            raise HTTPException(status_code=503, detail={"code": "initializer_not_configured", "message": "Secure fresh-vault initialization is not configured. Local operation does not require it."})
        return value

    def token(body):
        value = body.get("review_token")
        if set(body) != {"review_token"} or not isinstance(value, str) or len(value) != 36:
            raise HTTPException(status_code=400, detail={"code": "invalid_review", "message": "A server-issued initialization review token is required."})
        return value

    def refuse(exc):
        return HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)})

    @router.get("/api/security/vault/initialize/status")
    async def status():
        value = controller_provider()
        if value is None:
            # These conservative wire fields confer no prepare authority. The
            # missing optional controller has not inspected the underlying vault;
            # storage_inspected and the message explicitly preserve that unknown.
            return {
                "supported": False,
                "code": "initializer_not_configured",
                "can_initialize": False,
                "configured": False,
                "storage_inspected": False,
                "message": "Secure fresh-vault initialization is not configured. Credential readiness and existing vault artifacts have not been inspected. Local operation does not require initialization.",
                "in_flight": False,
                "credential_storage_available": False,
                "existing_artifacts": False,
                "requires_signed_acceptance": True,
                "local_use_requires_vault": False,
            }
        return value.status()

    @router.post("/api/security/vault/initialize/review")
    async def review(body: dict):
        if body:
            raise HTTPException(status_code=400, detail={"code": "invalid_review", "message": "Initialization review accepts an empty object only."})
        try:
            return controller().review()
        except VaultInitializationAPIRefusal as exc:
            raise refuse(exc) from None

    @router.post("/api/security/vault/initialize")
    async def initialize(body: dict):
        value = token(body)
        try:
            return await controller().confirm(value)
        except VaultInitializationAPIRefusal as exc:
            raise refuse(exc) from None

    @router.post("/api/security/vault/initialize/cancel")
    async def cancel(body: dict):
        controller().cancel(token(body))
        return {"ok": True}

    return router
