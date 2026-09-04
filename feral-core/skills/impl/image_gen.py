"""
FERAL Image Generation Skill
=============================
Provider-abstracted image generation over OpenAI's Images API, with failover
between providers.

Why the request body looks the way it does
------------------------------------------
On 2026-09-04 the operator asked the brain to make a wallpaper and got back
``OpenAI images error 400: Unknown parameter: 'response_format'``. This file
was sending ``model: dall-e-3`` with ``response_format: b64_json``, which was
the documented shape for that model. OpenAI now rejects the parameter on
``/v1/images/generations`` outright. The GPT image models (``gpt-image-1``,
``gpt-image-1.5``, ``gpt-image-2``) never took it: they always return base64
and reject ``response_format`` and ``style`` as unknown parameters. So the
body is now the one those models accept, and the default model is a GPT image
model rather than DALL·E 3, which is a legacy name on the same endpoint.

The result shape the rest of the brain reads (``b64``, ``image_b64``,
``revised_prompt``, ``provider``, ``size``) is unchanged.
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

# Sizes the GPT image models accept. ``auto`` lets the model choose. The old
# DALL·E 3 landscape/portrait sizes (1792x1024, 1024x1792) are mapped onto the
# nearest GPT-image size below rather than refused, so a caller written against
# the old manifest still gets a landscape or portrait picture back.
_VALID_SIZES = frozenset({"1024x1024", "1536x1024", "1024x1536", "auto"})
_LEGACY_SIZE_MAP = {"1792x1024": "1536x1024", "1024x1792": "1024x1536"}

# Default model. Read from ``FERAL_IMAGE_MODEL`` so an operator can pin a
# different one (for example ``gpt-image-2``) without a code change; the
# default is the mid-tier GPT image model rather than DALL·E 3, whose request
# shape OpenAI no longer accepts (see module docstring).
_DEFAULT_MODEL = "gpt-image-1.5"


def _image_model() -> str:
    return (os.getenv("FERAL_IMAGE_MODEL") or "").strip() or _DEFAULT_MODEL


def _normalise_size(size: str) -> str:
    size = (size or "").strip()
    if size in _VALID_SIZES:
        return size
    return _LEGACY_SIZE_MAP.get(size, "1024x1024")


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


class OpenAIImagesProvider(ImageGenProvider):
    """OpenAI Images API with a GPT image model.

    The body deliberately carries only ``model``, ``prompt``, ``n`` and
    ``size``. ``response_format`` is rejected by the endpoint (the failure
    this class exists to fix) and ``style`` is a DALL·E 3 only parameter that
    the GPT image models reject as unknown. The models always return
    ``b64_json``, which is what the tool-result image pipeline wants.
    """

    def __init__(self, api_key: str, model: str | None = None):
        self._api_key = api_key
        self.name = model or _image_model()

    async def generate(
        self,
        prompt: str,
        size: str = "1024x1024",
        style: str = "vivid",
        n: int = 1,
    ) -> ImageResult:
        # ``style`` is accepted for interface parity and ignored: sending it
        # is a 400. ``n`` is clamped to one because the skill returns a single
        # image and a larger batch would only be billed and dropped.
        _ = style
        _ = n
        sz = _normalise_size(size)
        payload: Dict[str, Any] = {
            "model": self.name,
            "prompt": prompt,
            "n": 1,
            "size": sz,
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
            self.providers.append(OpenAIImagesProvider(key))

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
                {"name": "size", "type": "string", "required": False, "description": "1024x1024, 1536x1024, 1024x1536 or auto"},
                {"name": "style", "type": "string", "required": False, "description": "Accepted and ignored; GPT image models take no style parameter"},
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
