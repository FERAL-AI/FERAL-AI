"""
FERAL Image Generation Skill
=============================
Provider-abstracted image generation with DALL·E 3 and failover between providers.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict

import httpx

from skills.base import BaseSkill
from skills.impl import register_skill

logger = logging.getLogger("feral.skills.image_gen")

_VALID_DALLE3_SIZES = frozenset({"1024x1024", "1792x1024", "1024x1792"})


@dataclass
class ImageResult:
    url: str
    b64: str
    revised_prompt: str
    provider: str
    size: str


class ImageGenProvider(ABC):
    """Abstract image generation backend."""

    name: str = "abstract"

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        size: str = "1024x1024",
        style: str = "vivid",
        n: int = 1,
    ) -> ImageResult:
        ...


class DallE3Provider(ImageGenProvider):
    """OpenAI DALL·E 3 via Images API (b64_json)."""

    name = "dall-e-3"

    def __init__(self, api_key: str):
        self._api_key = api_key

    async def generate(
        self,
        prompt: str,
        size: str = "1024x1024",
        style: str = "vivid",
        n: int = 1,
    ) -> ImageResult:
        _ = n  # dall-e-3 API supports n=1 only; reserved for interface parity
        sz = size if size in _VALID_DALLE3_SIZES else "1024x1024"
        st = style if style in ("vivid", "natural") else "vivid"
        payload: Dict[str, Any] = {
            "model": "dall-e-3",
            "prompt": prompt,
            "n": 1,
            "size": sz,
            "response_format": "b64_json",
            "style": st,
        }
        async with httpx.AsyncClient(
            timeout=120.0,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        ) as client:
            resp = await client.post(
                "https://api.openai.com/v1/images/generations",
                json=payload,
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"OpenAI images error {resp.status_code}: {resp.text[:500]}")
            data = resp.json()
            items = data.get("data") or []
            if not items:
                raise RuntimeError("OpenAI returned no image data")
            first = items[0]
            b64 = first.get("b64_json") or ""
            revised = first.get("revised_prompt") or prompt
            return ImageResult(
                url="",
                b64=b64,
                revised_prompt=revised,
                provider=self.name,
                size=sz,
            )


class ImageGenEngine:
    """
    Selects an available provider from env (and optional explicit keys) and
    fails over in registration order.
    """

    def __init__(self, openai_key: str | None = None):
        self.providers: list[ImageGenProvider] = []
        key = openai_key or os.getenv("OPENAI_API_KEY")
        if key:
            self.providers.append(DallE3Provider(key))

    async def generate(self, prompt: str, size: str = "1024x1024", style: str = "vivid") -> ImageResult:
        if not prompt.strip():
            raise ValueError("prompt is required")
        last_err: Exception | None = None
        for p in self.providers:
            try:
                return await p.generate(prompt, size=size, style=style, n=1)
            except Exception as e:
                last_err = e
                logger.warning("Image provider %s failed: %s", getattr(p, "name", p), e)
        if last_err:
            raise last_err
        raise RuntimeError("No image generation providers configured (set OPENAI_API_KEY)")


@register_skill
class ImageGenSkill(BaseSkill):
    """LLM-callable skill: text-to-image via configured providers."""

    endpoints = [
        {
            "id": "generate",
            "description": "Generate an image from text prompt",
            "params": [
                {"name": "prompt", "type": "string", "required": True, "description": "Image description"},
                {"name": "size", "type": "string", "required": False, "description": "e.g. 1024x1024"},
                {"name": "style", "type": "string", "required": False, "description": "vivid or natural (DALL·E 3)"},
                {"name": "n", "type": "integer", "required": False, "description": "Number of images (provider limits apply)"},
            ],
        }
    ]

    def __init__(self):
        super().__init__(skill_id="image_gen")

    async def execute(self, endpoint_id: str, args: Dict[str, Any], vault: Dict[str, str]) -> Dict[str, Any]:
        # Positive dispatch rather than a negative guard. The manifest and
        # backend contract test parses execute() for `endpoint_id ==` chains
        # to prove every declared endpoint is really implemented, and it
        # cannot read `!=`. This skill was invisible to that check for as
        # long as it had no manifest; now that it has one, it is held to the
        # same contract as every other skill.
        if endpoint_id == "generate":
            return await self._generate(args, vault)

        return {
            "success": False,
            "status_code": 404,
            "data": None,
            "error": f"Unknown endpoint_id: {endpoint_id}",
        }

    async def _generate(self, args: Dict[str, Any], vault: Dict[str, str]) -> Dict[str, Any]:
        api_key = self.get_api_key(vault, fallback_env="OPENAI_API_KEY")
        engine = ImageGenEngine(openai_key=api_key)

        prompt = (args.get("prompt") or args.get("text") or "").strip()
        if not prompt:
            return {
                "success": False,
                "status_code": 400,
                "data": None,
                "error": "Missing 'prompt' parameter.",
            }

        size = str(args.get("size") or "1024x1024")
        style = str(args.get("style") or "vivid")
        n = int(args.get("n") or 1)

        try:
            # DALL·E 3 only supports n=1; extra providers could use n later.
            result = await engine.generate(prompt, size=size, style=style)
        except ValueError as e:
            return {"success": False, "status_code": 400, "data": None, "error": str(e)}
        except Exception as e:
            return {"success": False, "status_code": 502, "data": None, "error": str(e)}

        _ = n  # reserved for multi-provider / future batching
        return {
            "success": True,
            "status_code": 200,
            "data": {
                "b64": result.b64,
                # Same bytes under the name the tool-result image pipeline
                # recognises (agents/multimodal_blocks.TOOL_RESULT_IMAGE_FIELDS).
                # Under "b64" alone the generated image was not extracted as an
                # image block, so it was stringified into the text result and
                # clamped: the model never saw the picture it had just made.
                # "b64" is kept because callers and stored results use it.
                "image_b64": result.b64,
                # Always None: every provider requests b64_json, so the
                # Images API returns no URL. Declared so the shape is stable.
                "url": result.url or None,
                "revised_prompt": result.revised_prompt,
                "provider": result.provider,
                "size": result.size,
            },
            "error": None,
        }
