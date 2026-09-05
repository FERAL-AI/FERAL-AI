"""
FERAL GenUI Generator — Structured UI Generation
====================================================
LLM generates SDUI JSON via structured output.
Service provider SDK for external integrations.

Components: VStack, HStack, Text, Card, Button, Image, MapView, Chart,
AudioPlayer, VideoPlayer, Form, Table, CodeBlock, Markdown, ProgressBar, Skeleton.
"""

from __future__ import annotations
import json
import logging
import time
from pathlib import Path
from typing import Optional
from uuid import uuid4

from config.loader import feral_data_home

logger = logging.getLogger("feral.genui")

SDUI_COMPONENT_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": [
            "VStack", "HStack", "Text", "Card", "Button", "Image", "MapView",
            "Chart", "AudioPlayer", "VideoPlayer", "Form", "Table", "CodeBlock",
            "Markdown", "ProgressBar", "Skeleton", "Badge", "Divider", "Icon",
            "MetricCard", "Grid", "ScrollView", "WebView", "Spacer",
        ]},
        "children": {"type": "array", "items": {"$ref": "#"}},
        "value": {}, "label": {"type": "string"},
        "style": {"type": "string"}, "color": {"type": "string"},
        "action_id": {"type": "string"}, "url": {"type": "string"},
        "spacing": {"type": "integer"}, "padding": {"type": "integer"},
    },
    "required": ["type"],
}

GENUI_SYSTEM_PROMPT = """You generate SDUI (Server-Driven UI) JSON components.
Available types: VStack, HStack, Text, Card, Button, Image, MapView, Chart,
AudioPlayer, VideoPlayer, Form, Table, CodeBlock, Markdown, ProgressBar,
MetricCard, Grid, Badge, Divider, Icon, WebView, Skeleton.

Rules:
- Output ONLY valid JSON (no markdown, no explanation)
- Root must be a single component (usually VStack)
- Use semantic colors: #6c5ce7 (accent), #00b894 (success), #e17055 (error)
- Text styles: headline, subtitle, body, caption
- Cards have corner_radius, children array
- Charts need: data (array), chart_type (line/bar), label
- Forms need: fields [{name, type, label, placeholder}], submit_label, action_id
- Maps need: lat, lon, zoom, markers [{lat, lon, label}]
- Tables need: headers [], rows [[]]
"""


class ProviderSurfaceCache:
    """Persistent cache for provider-generated GenUI layouts."""

    def __init__(self, base_dir: Optional[str | Path] = None):
        self._base_dir = Path(base_dir) if base_dir else feral_data_home() / "genui_surfaces"
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def _surface_path(self, provider_id: str, surface_id: str) -> Path:
        provider_dir = self._base_dir / provider_id
        provider_dir.mkdir(parents=True, exist_ok=True)
        return provider_dir / f"{surface_id}.json"

    def load(self, provider_id: str, surface_id: str) -> Optional[dict]:
        path = self._surface_path(provider_id, surface_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except Exception as e:
            logger.warning(f"Failed to read cached GenUI surface {provider_id}/{surface_id}: {e}")
            return None

    def save(self, provider_id: str, surface_id: str, payload: dict, metadata: dict | None = None) -> dict:
        record = {
            "provider_id": provider_id,
            "surface_id": surface_id,
            "cached_at": int(time.time()),
            "metadata": metadata or {},
            "payload": payload,
        }
        path = self._surface_path(provider_id, surface_id)
        path.write_text(json.dumps(record, indent=2))
        return record

    def list(self, provider_id: str) -> list[dict]:
        provider_dir = self._base_dir / provider_id
        if not provider_dir.exists():
            return []

        cached: list[dict] = []
        for path in sorted(provider_dir.glob("*.json")):
            try:
                record = json.loads(path.read_text())
            except Exception as e:
                logger.warning(f"Failed to parse cached GenUI surface {path}: {e}")
                continue
            cached.append({
                "surface_id": record.get("surface_id", path.stem),
                "cached_at": record.get("cached_at"),
                "metadata": record.get("metadata", {}),
            })
        return cached


class GenUIEngine:
    """Production GenUI engine with LLM structured output."""

    def __init__(self, llm=None, cache_dir: Optional[str | Path] = None):
        self._llm = llm
        self._providers: dict[str, ServiceProvider] = {}
        self._surface_cache = ProviderSurfaceCache(cache_dir)

    def set_llm(self, llm):
        self._llm = llm

    async def generate_from_prompt(self, prompt: str, context: dict = None) -> dict:
        """Ask the LLM to generate SDUI JSON for a given prompt."""
        if not self._llm or not self._llm.available:
            return self._fallback_text(prompt)

        messages = [
            {"role": "system", "content": GENUI_SYSTEM_PROMPT},
            {"role": "user", "content": f"Generate UI for: {prompt}\nContext: {json.dumps(context or {})[:1000]}"},
        ]

        try:
            response = await self._llm.chat(messages, tools=None)
            text, _ = self._llm.extract_response(response)
            cleaned = (text or "").strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            sdui = json.loads(cleaned)
            if "type" in sdui:
                return sdui
        except Exception as e:
            logger.warning(f"GenUI generation failed: {e}")

        return self._fallback_text(prompt)

    async def generate_for_data(self, data: dict, skill_brand: dict = None,
                                ui_hint: str = None, endpoint_id: str = "") -> dict:
        """Generate UI for API response data (deterministic + LLM fallback)."""
        if isinstance(data, dict) and "success" in data and "data" in data and isinstance(data["data"], dict):
            data = data["data"]

        if ui_hint == "map" and ("lat" in data or "latitude" in data):
            return self._build_map(data, skill_brand)

        if ui_hint == "chart" or "chart_data" in data:
            return self._build_chart(data, skill_brand)

        if isinstance(data, dict):
            if "results" in data and isinstance(data["results"], list):
                return self._build_list(data["results"], skill_brand)
            if "image_url" in data or "image_b64" in data:
                return self._build_image(data, skill_brand)

        return self._build_card(data, skill_brand)

    def register_provider(self, provider: "ServiceProvider"):
        """Register a service provider's UI components."""
        self._providers[provider.provider_id] = provider
        logger.info(f"GenUI provider registered: {provider.provider_id} ({len(provider.components)} components)")

    def get_provider_component(self, provider_id: str, component_id: str, data: dict) -> Optional[dict]:
        """Get a rendered component from a service provider."""
        provider = self._providers.get(provider_id)
        if not provider:
            return None
        return provider.render(component_id, data)

    def get_cached_surface(self, provider_id: str, surface_id: str) -> Optional[dict]:
        """Return a previously compiled provider surface, if any."""
        return self._surface_cache.load(provider_id, surface_id)

    def list_provider_surfaces(self, provider_id: str) -> list[dict]:
        """List provider surfaces with cache status."""
        provider = self._providers.get(provider_id)
        if not provider:
            return []

        cached_by_id = {
            item["surface_id"]: item for item in self._surface_cache.list(provider_id)
        }
        surfaces = []
        for surface in provider.list_surfaces():
            cache_info = cached_by_id.get(surface["surface_id"], {})
            surfaces.append({
                **surface,
                "cached": surface["surface_id"] in cached_by_id,
                "cached_at": cache_info.get("cached_at"),
                "cache_metadata": cache_info.get("metadata", {}),
            })
        return surfaces

    async def compile_provider_surface(self, provider_id: str, surface_id: str, force: bool = False) -> dict:
        """
        Compile a provider surface once and persist the layout.

        This is the core static-caching path: providers describe a surface in JSON,
        FERAL compiles it into SDUI once, then later opens reuse the cached layout.
        """
        provider = self._providers.get(provider_id)
        if not provider:
            return {"ok": False, "error": "Provider not registered"}

        surface = provider.get_surface(surface_id)
        if not surface:
            return {"ok": False, "error": "Surface not found"}

        cache_mode = str(provider.cache_policy.get("mode", "static")).lower()
        if cache_mode != "disabled" and not force:
            cached = self.get_cached_surface(provider_id, surface_id)
            if cached:
                return {
                    "ok": True,
                    "provider_id": provider_id,
                    "surface_id": surface_id,
                    "cache_hit": True,
                    "payload": cached.get("payload"),
                    "metadata": cached.get("metadata", {}),
                }

        payload = surface.get("template")
        if not isinstance(payload, dict):
            prompt = surface.get("prompt") or (
                f"Generate a fixed-layout surface named '{surface.get('title', surface_id)}' "
                f"for provider {provider.name}. Keep layout stable and brand compliant."
            )
            payload = await self.generate_from_prompt(
                prompt,
                context=provider.build_generation_context(surface_id),
            )

        if not isinstance(payload, dict) or "type" not in payload:
            return {"ok": False, "error": "Failed to compile a valid surface"}

        metadata = {
            "title": surface.get("title", surface_id),
            "cache_mode": cache_mode,
            "layout_mode": provider.ui_rules.get("layout_mode", "fixed"),
            "brand_mode": provider.ui_rules.get("brand_mode", "strict"),
        }
        if cache_mode != "disabled":
            self._surface_cache.save(provider_id, surface_id, payload, metadata)

        return {
            "ok": True,
            "provider_id": provider_id,
            "surface_id": surface_id,
            "cache_hit": False,
            "payload": payload,
            "metadata": metadata,
        }

    async def render_provider_surface(
        self,
        provider_id: str,
        surface_id: str,
        data: Optional[dict] = None,
        force_compile: bool = False,
    ) -> dict:
        """
        Render a provider surface using the cached layout.

        The cached layout gives the user a stable shell while runtime data can still
        hydrate placeholders inside the structure.
        """
        compiled = await self.compile_provider_surface(provider_id, surface_id, force=force_compile)
        if not compiled.get("ok"):
            return compiled

        provider = self._providers.get(provider_id)
        payload = compiled.get("payload") or {}
        rendered = provider.render_surface(surface_id, payload, data or {}) if provider else payload
        return {
            **compiled,
            "payload": rendered,
            "layout": payload,
        }

    @staticmethod
    def _fallback_text(text: str) -> dict:
        return {
            "type": "VStack", "spacing": 12, "padding": 16,
            "children": [{"type": "Text", "value": str(text)[:500], "style": "body"}],
        }

    @staticmethod
    def _build_map(data: dict, brand: dict = None) -> dict:
        lat = data.get("lat") or data.get("latitude") or 0
        lon = data.get("lon") or data.get("longitude") or 0
        return {
            "type": "VStack", "spacing": 12, "padding": 16,
            "children": [
                {"type": "MapView", "lat": lat, "lon": lon, "zoom": 14,
                 "markers": data.get("markers", []), "height": 250},
                {"type": "Text", "value": data.get("label", f"{lat:.4f}, {lon:.4f}"), "style": "caption"},
            ],
        }

    @staticmethod
    def _build_chart(data: dict, brand: dict = None) -> dict:
        chart_data = data.get("chart_data") or data.get("data") or data.get("values", [])
        return {
            "type": "VStack", "spacing": 12, "padding": 16,
            "children": [
                {"type": "Text", "value": data.get("title", "Chart"), "style": "headline",
                 "color": (brand or {}).get("primary_color", "#6c5ce7")},
                {"type": "Chart", "data": chart_data,
                 "chart_type": data.get("chart_type", "line"),
                 "label": data.get("label", ""), "height": 200},
            ],
        }

    @staticmethod
    def _build_list(items: list, brand: dict = None) -> dict:
        cards = []
        for item in items[:10]:
            if isinstance(item, dict):
                title = item.get("title") or item.get("name") or str(item)[:80]
                desc = item.get("description") or item.get("snippet") or ""
                cards.append({
                    "type": "Card", "corner_radius": 12,
                    "children": [
                        {"type": "Text", "value": title, "style": "subtitle"},
                        *([{"type": "Text", "value": desc[:200], "style": "caption"}] if desc else []),
                    ],
                })
            else:
                cards.append({"type": "Text", "value": str(item)[:200], "style": "body"})
        return {
            "type": "VStack", "spacing": 12, "padding": 16,
            "children": [
                {"type": "Text", "value": f"{len(items)} results", "style": "headline",
                 "color": (brand or {}).get("primary_color", "#6c5ce7")},
                *cards,
            ],
        }

    @staticmethod
    def _build_image(data: dict, brand: dict = None) -> dict:
        url = data.get("image_url") or ""
        if data.get("image_b64"):
            url = f"data:image/jpeg;base64,{data['image_b64']}"
        return {
            "type": "VStack", "spacing": 12, "padding": 16,
            "children": [
                {"type": "Image", "url": url, "corner_radius": 12},
                *([{"type": "Text", "value": data.get("caption", ""), "style": "caption"}]
                  if data.get("caption") else []),
            ],
        }

    @staticmethod
    def _build_card(data: dict, brand: dict = None) -> dict:
        SKIP_KEYS = {
            "success", "status_code", "error", "ok", "status",
            "created_at", "updated_at", "timestamp", "_source",
        }

        if isinstance(data, dict) and "success" in data and "data" in data and isinstance(data.get("data"), dict):
            data = data["data"]

        children = []

        if brand and brand.get("name"):
            children.append({
                "type": "HStack", "spacing": 8,
                "children": [
                    {"type": "Icon", "name": brand.get("icon", "Sparkles"), "size": 16,
                     "color": brand.get("color", "#06b6d4")},
                    {"type": "Text", "value": brand["name"], "style": "subtitle"},
                ],
            })

        for k, v in data.items():
            if k.startswith("_") or k in SKIP_KEYS:
                continue
            if v is None or v == "" or v == [] or v == {}:
                continue
            if isinstance(v, bool):
                continue
            label = k.replace("_", " ").title()
            if isinstance(v, dict):
                for sub_k, sub_v in v.items():
                    if sub_v is None or sub_v == "":
                        continue
                    children.append({
                        "type": "HStack", "spacing": 8,
                        "children": [
                            {"type": "Text", "value": sub_k.replace("_", " ").title(), "style": "caption"},
                            {"type": "Text", "value": str(sub_v)[:200], "style": "body"},
                        ],
                    })
            elif isinstance(v, list):
                children.append({"type": "Text", "value": label, "style": "caption"})
                for item in v[:8]:
                    children.append({"type": "Text", "value": f"  {item}" if not isinstance(item, dict) else f"  {json.dumps(item, default=str)[:150]}", "style": "body"})
            else:
                children.append({
                    "type": "HStack", "spacing": 8,
                    "children": [
                        {"type": "Text", "value": label, "style": "caption"},
                        {"type": "Text", "value": str(v)[:200], "style": "body"},
                    ],
                })

        if not children:
            children.append({"type": "Text", "value": "Done", "style": "body"})

        return {
            "type": "Card", "corner_radius": 12,
            "children": children[:15],
        }


class ServiceProvider:
    """
    External service provider that registers UI components with FERAL.
    This is the "UI as a service" SDK.
    """

    def __init__(
        self,
        provider_id: str,
        name: str,
        description: str = "",
        base_url: str = "",
        brand: Optional[dict] = None,
        ui_rules: Optional[dict] = None,
        endpoints: Optional[list[dict]] = None,
        cache_policy: Optional[dict] = None,
    ):
        self.provider_id = provider_id
        self.name = name
        self.description = description
        self.base_url = base_url
        self.brand = brand or {}
        self.ui_rules = {
            "layout_mode": "fixed",
            "brand_mode": "strict",
            **(ui_rules or {}),
        }
        self.endpoints = endpoints or []
        self.cache_policy = {
            "mode": "static",
            "persist": True,
            **(cache_policy or {}),
        }
        self.components: dict[str, dict] = {}
        self.surfaces: dict[str, dict] = {}
        self._renderers: dict[str, callable] = {}

    def register_component(self, component_id: str, schema: dict, renderer: callable = None):
        """Register a UI component spec with optional custom renderer."""
        self.components[component_id] = schema
        if renderer:
            self._renderers[component_id] = renderer

    def render(self, component_id: str, data: dict) -> Optional[dict]:
        """Render a component with data."""
        renderer = self._renderers.get(component_id)
        if renderer:
            return renderer(data)

        schema = self.components.get(component_id)
        if not schema:
            return None

        template = schema.get("template", {})
        return self._fill_template(template, data)

    def register_surface(self, surface_id: str, spec: dict):
        """Register a named app surface/screen for compile-once GenUI."""
        self.surfaces[surface_id] = spec

    def get_surface(self, surface_id: str) -> Optional[dict]:
        return self.surfaces.get(surface_id)

    def list_surfaces(self) -> list[dict]:
        return [
            {
                "surface_id": surface_id,
                "title": spec.get("title", surface_id),
                "has_template": isinstance(spec.get("template"), dict),
                "has_prompt": bool(spec.get("prompt")),
                "entry": bool(spec.get("entry", False)),
            }
            for surface_id, spec in self.surfaces.items()
        ]

    def build_generation_context(self, surface_id: str) -> dict:
        """Build provider context for one-time surface compilation."""
        surface = self.surfaces.get(surface_id, {})
        surface_contract = {k: v for k, v in surface.items() if k != "template"}
        return {
            "provider": {
                "provider_id": self.provider_id,
                "name": self.name,
                "description": self.description,
                "base_url": self.base_url,
                "brand": self.brand,
                "ui_rules": self.ui_rules,
                "cache_policy": self.cache_policy,
                "endpoints": self.endpoints,
            },
            "surface": surface_contract,
            "instructions": [
                "Respect provider brand rules and theme tokens.",
                "Generate a stable layout suitable for caching.",
                "Do not shift primary navigation or action placement between renders.",
            ],
        }

    def render_surface(self, surface_id: str, layout: Optional[dict], data: dict) -> Optional[dict]:
        """Hydrate a cached or templated surface with runtime data."""
        base_layout = layout or (self.surfaces.get(surface_id) or {}).get("template")
        if not isinstance(base_layout, dict):
            return None
        return self._fill_template(base_layout, data)

    @staticmethod
    def _fill_template(template: dict, data: dict) -> dict:
        """Fill a component template with data values."""
        result = {}
        for k, v in template.items():
            if isinstance(v, str) and v.startswith("$"):
                data_key = v[1:]
                result[k] = data.get(data_key, v)
            elif isinstance(v, dict):
                result[k] = ServiceProvider._fill_template(v, data)
            elif isinstance(v, list):
                result[k] = [
                    ServiceProvider._fill_template(item, data) if isinstance(item, dict) else item
                    for item in v
                ]
            else:
                result[k] = v
        return result


class ServiceProviderRegistry:
    """REST API interface for service provider management."""

    def __init__(self):
        self._providers: dict[str, ServiceProvider] = {}

    def register(self, config: dict) -> ServiceProvider:
        pid = config.get("provider_id", str(uuid4())[:8])
        provider = ServiceProvider(
            provider_id=pid,
            name=config.get("name", "Unknown"),
            description=config.get("description", ""),
            base_url=config.get("base_url", ""),
            brand=config.get("brand"),
            ui_rules=config.get("ui_rules"),
            endpoints=config.get("endpoints"),
            cache_policy=config.get("cache_policy"),
        )
        for comp in config.get("components", []):
            provider.register_component(
                comp.get("id", ""),
                comp.get("schema", {}),
            )
        for surface in config.get("surfaces", []):
            surface_id = surface.get("id") or surface.get("surface_id") or str(uuid4())[:8]
            provider.register_surface(surface_id, surface)
        self._providers[pid] = provider
        return provider

    def list_providers(self) -> list[dict]:
        return [
            {
                "provider_id": p.provider_id,
                "name": p.name,
                "description": p.description,
                "components": list(p.components.keys()),
                "surface_ids": list(p.surfaces.keys()),
                "brand": p.brand,
                "ui_rules": p.ui_rules,
                "cache_policy": p.cache_policy,
                "endpoint_count": len(p.endpoints),
            }
            for p in self._providers.values()
        ]

    def get(self, provider_id: str) -> Optional[ServiceProvider]:
        return self._providers.get(provider_id)
