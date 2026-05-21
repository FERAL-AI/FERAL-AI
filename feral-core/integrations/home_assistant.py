"""
FERAL Home Assistant Integration — Smart Home Control
========================================================
Real Home Assistant REST API integration with entity discovery,
service calls, and WebSocket event subscription.
Uses long-lived access tokens (no OAuth needed).
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger("feral.integrations.ha")
ws_logger = logging.getLogger("feral.integrations.ha.ws")


class HomeAssistantIntegration:
    """
    Native Home Assistant integration.
    Connects via REST API and optional WebSocket for events.
    """

    def __init__(self, oauth_manager=None):
        self._oauth = oauth_manager
        self._is_addon = bool(os.getenv("SUPERVISOR_TOKEN"))
        if self._is_addon:
            self._base_url = os.getenv("FERAL_HA_URL", "http://supervisor/core")
            self._token = os.getenv("SUPERVISOR_TOKEN", "")
            logger.info("Running as HA add-on — using Supervisor API at %s", self._base_url)
        else:
            self._base_url = os.getenv("HA_URL", "http://homeassistant.local:8123")
            self._token = os.getenv("HA_TOKEN", "")
        self._http: Optional[httpx.AsyncClient] = None
        self._entities_cache: dict[str, dict] = {}
        self._ws = None
        self._event_handlers: list = []

    async def _ensure_client(self):
        if self._http is None:
            token = self._token
            if not token and self._oauth:
                token = await self._oauth.get_token("home_assistant") or ""
            self._http = httpx.AsyncClient(
                base_url=self._base_url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                timeout=10.0,
            )

    @property
    def connected(self) -> bool:
        from integrations._probe_status import is_connected_cached

        token_present = bool(self._token) or (
            self._oauth is not None and self._oauth.is_connected("home_assistant")
        )
        return is_connected_cached("home_assistant", fallback=token_present)

    async def probe_connected(self) -> bool:
        """Force a live ``GET {base_url}/api/states`` probe with the
        configured long-lived token."""
        from integrations._probe_status import refresh, mark_probe_result

        result = await refresh(
            "home_assistant",
            vault=getattr(self._oauth, "_vault", None),
        )
        if result is not None:
            return result
        # No registered probe — do a one-off direct check so the cache
        # gets populated. Without this the integration would never
        # transition out of token-presence fallback.
        await self._ensure_client()
        try:
            resp = await self._http.get("/api/states")
            ok = resp.status_code == 200
            mark_probe_result(
                "home_assistant",
                ok=ok,
                reason="ok" if ok else f"http_{resp.status_code}",
                detail=("" if ok else (resp.text or "")[:200]),
            )
            return ok
        except Exception as exc:
            mark_probe_result(
                "home_assistant",
                ok=False,
                reason="network_error",
                detail=str(exc),
            )
            return False

    async def execute(self, endpoint_id: str, args: dict, vault: dict = None) -> dict:
        """Skill executor interface."""
        dispatch = {
            "get_states": self.get_states,
            "get_entities": self.get_entities,
            "call_service": self.call_service,
            "toggle_entity": self.toggle_entity,
            "set_light": self.set_light,
            "get_automations": self.get_automations,
            "trigger_automation": self.trigger_automation,
            "get_entity_state": self.get_entity_state,
        }
        fn = dispatch.get(endpoint_id)
        if not fn:
            return {"success": False, "error": f"Unknown endpoint: {endpoint_id}"}
        return await fn(**args)

    async def get_states(self, **kwargs) -> dict:
        await self._ensure_client()
        try:
            resp = await self._http.get("/api/states")
            resp.raise_for_status()
            states = resp.json()
            summary = []
            for s in states[:50]:
                summary.append({
                    "entity_id": s.get("entity_id", ""),
                    "state": s.get("state", ""),
                    "friendly_name": s.get("attributes", {}).get("friendly_name", ""),
                })
            return {"success": True, "data": {"entities": summary, "total": len(states)}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def get_entities(self, domain: str = "", **kwargs) -> dict:
        await self._ensure_client()
        try:
            resp = await self._http.get("/api/states")
            resp.raise_for_status()
            states = resp.json()

            entities = []
            for s in states:
                eid = s.get("entity_id", "")
                if domain and not eid.startswith(f"{domain}."):
                    continue
                attrs = s.get("attributes", {})
                entities.append({
                    "entity_id": eid,
                    "state": s.get("state", ""),
                    "friendly_name": attrs.get("friendly_name", ""),
                    "device_class": attrs.get("device_class", ""),
                })
                self._entities_cache[eid] = s

            return {"success": True, "data": {"entities": entities[:30], "total": len(entities)}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def get_entity_state(self, entity_id: str = "", **kwargs) -> dict:
        await self._ensure_client()
        try:
            resp = await self._http.get(f"/api/states/{entity_id}")
            resp.raise_for_status()
            s = resp.json()
            attrs = s.get("attributes", {})
            return {
                "success": True,
                "data": {
                    "entity_id": entity_id,
                    "state": s.get("state", ""),
                    "friendly_name": attrs.get("friendly_name", ""),
                    "attributes": {
                        k: v for k, v in attrs.items()
                        if k in ("brightness", "color_temp", "temperature", "humidity",
                                 "battery", "device_class", "unit_of_measurement")
                    },
                    "last_changed": s.get("last_changed", ""),
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def call_service(self, domain: str = "", service: str = "", entity_id: str = "", data: dict = None, **kwargs) -> dict:
        await self._ensure_client()
        try:
            body = {"entity_id": entity_id}
            if data:
                body.update(data)
            resp = await self._http.post(f"/api/services/{domain}/{service}", json=body)
            resp.raise_for_status()
            return {"success": True, "data": {"called": f"{domain}.{service}", "entity": entity_id}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def toggle_entity(self, entity_id: str = "", **kwargs) -> dict:
        domain = entity_id.split(".")[0] if "." in entity_id else "homeassistant"
        return await self.call_service(domain=domain, service="toggle", entity_id=entity_id)

    async def set_light(self, entity_id: str = "", brightness: int = None, color_temp: int = None, rgb_color: list = None, **kwargs) -> dict:
        data = {}
        if brightness is not None:
            data["brightness"] = max(0, min(255, brightness))
        if color_temp is not None:
            data["color_temp"] = color_temp
        if rgb_color:
            data["rgb_color"] = rgb_color
        return await self.call_service(domain="light", service="turn_on", entity_id=entity_id, data=data)

    async def get_automations(self, **kwargs) -> dict:
        return await self.get_entities(domain="automation")

    async def trigger_automation(self, entity_id: str = "", **kwargs) -> dict:
        return await self.call_service(domain="automation", service="trigger", entity_id=entity_id)

    def on_event(self, handler):
        """Register a callback for HA events (from WebSocket subscription)."""
        self._event_handlers.append(handler)

    async def discover_capabilities(self) -> dict:
        """Fetch all entities and build a capabilities map for the LLM."""
        await self._ensure_client()
        try:
            resp = await self._http.get("/api/states")
            resp.raise_for_status()
            states = resp.json()

            domains = {}
            for s in states:
                eid = s.get("entity_id", "")
                domain = eid.split(".")[0] if "." in eid else "unknown"
                if domain not in domains:
                    domains[domain] = []
                name = s.get("attributes", {}).get("friendly_name", eid)
                domains[domain].append(name)

            return {
                "total_entities": len(states),
                "domains": {d: len(items) for d, items in domains.items()},
                "sample_entities": {
                    d: items[:5] for d, items in domains.items()
                },
            }
        except Exception as e:
            return {"error": str(e)}

    async def close(self):
        if self._http:
            await self._http.aclose()
