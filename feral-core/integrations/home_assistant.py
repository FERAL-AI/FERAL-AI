"""
FERAL Home Assistant Integration — Smart Home Control
========================================================
Real Home Assistant REST API integration with entity discovery,
service calls, and WebSocket event subscription.
Uses long-lived access tokens (no OAuth needed).
"""

from __future__ import annotations
import asyncio
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger("feral.integrations.ha")

# AUDIT-FIXES F-06. ``_close_later`` is a staticmethod with no instance to
# hang state off, so the strong references live at module level. The loop
# keeps tasks only weakly: a collected ``aclose()`` leaves the replaced
# httpx client's connection pool open, which is the socket leak this helper
# exists to prevent. Discard-on-done bounds the set to in-flight closes.
_close_tasks: set = set()
ws_logger = logging.getLogger("feral.integrations.ha.ws")

# Where a Home Assistant lives when nobody has said otherwise.
DEFAULT_BASE_URL = "http://homeassistant.local:8123"
ADDON_DEFAULT_BASE_URL = "http://supervisor/core"

# Vault slot holding the operator's Home Assistant URL. Not a secret, but
# the vault is the store this integration already reaches (through the
# OAuth manager) and it is the one place a value written by an HTTP route
# survives a restart without a settings-schema change.
URL_VAULT_NAMESPACE = "integration_config"
URL_VAULT_KEY = "home_assistant_url"


def normalize_base_url(raw: str) -> str:
    """Return a dialable base URL, or raise ``ValueError``.

    Accepts what a person actually pastes: ``homeassistant.local:8123``,
    ``http://10.0.0.4:8123/``, ``https://ha.example.com``. A missing
    scheme becomes ``http://`` because that is what a LAN Home Assistant
    speaks; a trailing slash is dropped so joining ``/api/states`` never
    produces a double slash.
    """
    url = (raw or "").strip()
    if not url:
        raise ValueError("Home Assistant URL is empty")
    if "://" not in url:
        url = f"http://{url}"
    scheme, _, rest = url.partition("://")
    if scheme.lower() not in ("http", "https"):
        raise ValueError(
            f"Home Assistant URL must be http:// or https://, got {scheme!r}",
        )
    host = rest.split("/")[0]
    if not host:
        raise ValueError("Home Assistant URL has no host")
    return url.rstrip("/")


def _vault_of(oauth_manager) -> object | None:
    """The BlindVault an OAuth manager is holding, when it has one."""
    return getattr(oauth_manager, "_vault", None)


def _url_from_vault(vault) -> str:
    if vault is None:
        return ""
    getter = getattr(vault, "get", None)
    if getter is None:
        return ""
    try:
        return (getter(URL_VAULT_NAMESPACE, URL_VAULT_KEY,
                       requester="home_assistant") or "").strip()
    except Exception as exc:
        logger.debug("HA URL vault read failed: %s", exc)
        return ""


def resolve_base_url(vault=None) -> str:
    """Resolve the Home Assistant base URL, most explicit source first.

    1. Add-on mode (``SUPERVISOR_TOKEN`` present): ``FERAL_HA_URL`` or
       the Supervisor proxy. The add-on knows where Core is.
    2. ``FERAL_HA_URL`` / ``HA_URL``, an operator env override outranks
       stored state, same as everywhere else in FERAL.
    3. The URL saved from Settings (vault ``integration_config``).
    4. :data:`DEFAULT_BASE_URL`.

    Before this existed only 1, 2 (``HA_URL`` alone, outside add-on
    mode) and 4 were reachable, and no HTTP route accepted a URL. Anyone
    whose Home Assistant is not at ``homeassistant.local:8123`` had to
    set an env var and restart the brain, while the provider's own setup
    text told them to paste the token "alongside your HA URL" into a
    field that did not exist.
    """
    if os.getenv("SUPERVISOR_TOKEN"):
        return os.getenv("FERAL_HA_URL", ADDON_DEFAULT_BASE_URL).rstrip("/")
    for env_key in ("FERAL_HA_URL", "HA_URL"):
        env_value = (os.getenv(env_key) or "").strip()
        if env_value:
            try:
                return normalize_base_url(env_value)
            except ValueError as exc:
                logger.warning("Ignoring invalid %s (%s)", env_key, exc)
    stored = _url_from_vault(vault)
    if stored:
        try:
            resolved = normalize_base_url(stored)
        except ValueError as exc:
            logger.warning("Ignoring invalid stored Home Assistant URL (%s)", exc)
        else:
            # ``security.probe._probe_home_assistant`` resolves its own
            # base URL from the environment and nothing else, so without
            # this export the integration would talk to the operator's
            # Home Assistant while the probe that decides the connected
            # badge talked to homeassistant.local. Exporting keeps the
            # two honest. The env var still wins on the next resolve,
            # and it is the same value, so this stays idempotent.
            os.environ["HA_URL"] = resolved
            return resolved
    return DEFAULT_BASE_URL


class HomeAssistantIntegration:
    """
    Native Home Assistant integration.
    Connects via REST API and optional WebSocket for events.
    """

    def __init__(self, oauth_manager=None, base_url: Optional[str] = None):
        self._oauth = oauth_manager
        self._is_addon = bool(os.getenv("SUPERVISOR_TOKEN"))
        if base_url:
            self._base_url = normalize_base_url(base_url)
        else:
            self._base_url = resolve_base_url(vault=_vault_of(oauth_manager))
        if self._is_addon:
            self._token = os.getenv("SUPERVISOR_TOKEN", "")
            logger.info("Running as HA add-on — using Supervisor API at %s", self._base_url)
        else:
            self._token = os.getenv("HA_TOKEN", "")
        self._http: Optional[httpx.AsyncClient] = None
        self._entities_cache: dict[str, dict] = {}
        self._ws = None
        self._event_handlers: list = []

    @property
    def base_url(self) -> str:
        """The Home Assistant this integration is pointed at."""
        return self._base_url

    def set_base_url(self, url: str, *, persist: bool = True) -> str:
        """Point the integration at a different Home Assistant, live.

        Drops the cached HTTP client so the next call dials the new host,
        and exports ``HA_URL`` so the registered probe agrees with the
        integration. Returns the normalized URL; raises ``ValueError``
        when the input is not a usable http(s) URL.
        """
        resolved = normalize_base_url(url)
        if self._is_addon:
            logger.warning(
                "Ignoring Home Assistant URL %s, running as an add-on, where "
                "Core is reached through the Supervisor proxy.", resolved,
            )
            return self._base_url

        previous, self._base_url = self._base_url, resolved
        stale, self._http = self._http, None
        if stale is not None:
            self._close_later(stale)
        os.environ["HA_URL"] = resolved

        if persist:
            vault = _vault_of(self._oauth)
            putter = getattr(vault, "put", None) if vault is not None else None
            if putter is None:
                logger.warning(
                    "Home Assistant URL set to %s for this process only, no "
                    "vault attached, so it will not survive a restart.",
                    resolved,
                )
            else:
                try:
                    putter(URL_VAULT_NAMESPACE, URL_VAULT_KEY, resolved,
                           stored_by="home_assistant")
                except Exception as exc:
                    logger.warning("Failed to persist Home Assistant URL: %s", exc)
        if previous != resolved:
            logger.info("Home Assistant URL: %s -> %s", previous, resolved)
        return resolved

    @staticmethod
    def _close_later(client: httpx.AsyncClient) -> None:
        """Close a replaced HTTP client without blocking a sync caller."""
        try:
            task = asyncio.get_running_loop().create_task(
                client.aclose(), name="ha-client-close",
            )
            _close_tasks.add(task)
            task.add_done_callback(_close_tasks.discard)
        except RuntimeError:
            # No loop here; the client never opened a connection pool it
            # could leak from a sync context.
            pass

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
            # THESIS_SCENARIOS S5 — actuator round-trip for the
            # "Roomba scenario". The vision side (smart-glasses
            # frame stream) is Lane 11's job; Lane 10 ships the
            # actuator side so the orchestrator can dispatch a
            # ``home_assistant__vacuum_start`` tool call once vision
            # decides "this room looks dirty, send the Roomba" — see
            # AUDIT-r14/phase2/THESIS_SCENARIOS.md S5 step 4.
            "vacuum_start": self.vacuum_start,
            "vacuum_stop": self.vacuum_stop,
            "vacuum_return_to_base": self.vacuum_return_to_base,
            # ``light.turn_on`` already worked through ``set_light``;
            # this alias matches the natural skill-manifest naming so
            # an LLM tool plan that asks for ``light_turn_on`` (with
            # an underscore) doesn't fall off the dispatch table.
            "light_turn_on": self.light_turn_on,
            "light_turn_off": self.light_turn_off,
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

    # ─────────────────────────────────────────────────────────────────
    # THESIS_SCENARIOS S5 — Roomba actuator round-trip.
    # ``vacuum.start`` is the canonical Home Assistant service for any
    # vacuum entity (iRobot/Roomba via the official integration,
    # Roborock, Xiaomi etc.). ``vacuum_start`` returns a structured
    # ``{success, data: {started: True, entity}}`` payload so the
    # orchestrator can render "Started the Roomba in the living room"
    # without inspecting raw HA service responses.
    # ─────────────────────────────────────────────────────────────────

    async def vacuum_start(self, entity_id: str = "", **kwargs) -> dict:
        """Start a vacuum cleaning cycle. ``entity_id`` should be the
        full HA entity id (e.g. ``vacuum.living_room``)."""
        if not entity_id:
            return {
                "success": False,
                "error": "entity_id is required",
                "reason": "missing_entity_id",
            }
        result = await self.call_service(
            domain="vacuum", service="start", entity_id=entity_id,
        )
        if result.get("success"):
            return {
                "success": True,
                "data": {
                    "started": True,
                    "entity_id": entity_id,
                    "service": "vacuum.start",
                },
            }
        return result

    async def vacuum_stop(self, entity_id: str = "", **kwargs) -> dict:
        if not entity_id:
            return {"success": False, "error": "entity_id is required",
                    "reason": "missing_entity_id"}
        result = await self.call_service(
            domain="vacuum", service="stop", entity_id=entity_id,
        )
        if result.get("success"):
            return {
                "success": True,
                "data": {
                    "stopped": True,
                    "entity_id": entity_id,
                    "service": "vacuum.stop",
                },
            }
        return result

    async def vacuum_return_to_base(self, entity_id: str = "", **kwargs) -> dict:
        if not entity_id:
            return {"success": False, "error": "entity_id is required",
                    "reason": "missing_entity_id"}
        result = await self.call_service(
            domain="vacuum", service="return_to_base", entity_id=entity_id,
        )
        if result.get("success"):
            return {
                "success": True,
                "data": {
                    "returning": True,
                    "entity_id": entity_id,
                    "service": "vacuum.return_to_base",
                },
            }
        return result

    async def light_turn_on(
        self,
        entity_id: str = "",
        brightness: Optional[int] = None,
        color_temp: Optional[int] = None,
        rgb_color: Optional[list] = None,
        **kwargs,
    ) -> dict:
        """Manifest-friendly alias for ``set_light``. Returns the same
        structured shape as ``vacuum_start`` so a single tool plan
        ("turn the lights on, send the Roomba") gets parallel-shaped
        results from each step."""
        if not entity_id:
            return {"success": False, "error": "entity_id is required",
                    "reason": "missing_entity_id"}
        result = await self.set_light(
            entity_id=entity_id,
            brightness=brightness,
            color_temp=color_temp,
            rgb_color=rgb_color,
        )
        if result.get("success"):
            return {
                "success": True,
                "data": {
                    "on": True,
                    "entity_id": entity_id,
                    "service": "light.turn_on",
                },
            }
        return result

    async def light_turn_off(self, entity_id: str = "", **kwargs) -> dict:
        if not entity_id:
            return {"success": False, "error": "entity_id is required",
                    "reason": "missing_entity_id"}
        result = await self.call_service(
            domain="light", service="turn_off", entity_id=entity_id,
        )
        if result.get("success"):
            return {
                "success": True,
                "data": {
                    "on": False,
                    "entity_id": entity_id,
                    "service": "light.turn_off",
                },
            }
        return result

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
