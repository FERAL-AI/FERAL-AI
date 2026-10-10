"""Aggregates brain REST endpoints (security, hardware, policy, integrations, webhooks, marketplace, browser, identity, nodes, sync)."""

from fastapi import APIRouter

from api.routes.hardware_reviewed import router as hardware_reviewed_router
from api.routes.identity_nodes_sync import router as identity_nodes_sync_router
from api.routes.integrations_webhooks import router as integrations_webhooks_router
from api.routes.marketplace_browser import router as marketplace_browser_router
from api.routes.security_and_hardware import router as security_and_hardware_router

router = APIRouter()
router.include_router(security_and_hardware_router)
router.include_router(integrations_webhooks_router)
router.include_router(marketplace_browser_router)
router.include_router(identity_nodes_sync_router)

router.include_router(hardware_reviewed_router)
