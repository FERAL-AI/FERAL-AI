"""The image skill must send a request body OpenAI's Images API accepts.

Observed on the operator's brain on 2026-09-04: asking for a wallpaper
produced ``OpenAI images error 400: {"error": {"message": "Unknown
parameter: 'response_format'.", "type": "invalid_request_error", "param":
"response_format", "code": "unknown_parameter"}}``. The skill was sending
``model: dall-e-3`` plus ``response_format: b64_json`` and ``style``.

These tests pin the body at the HTTP boundary with ``httpx.MockTransport``,
so the assertion is about the bytes that leave the process and not about
which helper built them. They also pin the result shape the rest of the
brain reads (``b64`` / ``image_b64`` / ``revised_prompt``), because the
whole point of the skill is that the model gets to see the picture.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from skills.impl import image_gen  # noqa: E402

_PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()


def _install_transport(monkeypatch, handler):
    """Route the skill's own ``httpx.AsyncClient`` through *handler*.

    The provider builds its client inside ``generate`` with a timeout and
    headers, so there is no injection point. Subclassing keeps those
    arguments and only adds the mock transport.
    """
    transport = httpx.MockTransport(handler)

    class _Client(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("transport", transport)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(image_gen.httpx, "AsyncClient", _Client)


def _ok_handler(captured: list):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"data": [{"b64_json": _PNG, "revised_prompt": "a raccoon, rendered"}]},
        )
    return handler


@pytest.mark.asyncio
async def test_body_carries_no_parameter_openai_rejects(monkeypatch):
    """The reported bug: ``response_format`` and ``style`` are 400s."""
    monkeypatch.delenv("FERAL_IMAGE_MODEL", raising=False)
    captured: list = []
    _install_transport(monkeypatch, _ok_handler(captured))

    provider = image_gen.OpenAIImagesProvider("sk-test")
    await provider.generate("a raccoon", size="1024x1024", style="vivid", n=3)

    assert len(captured) == 1
    body = captured[0]
    assert "response_format" not in body
    assert "style" not in body
    assert set(body) == {"model", "prompt", "n", "size"}
    assert body["n"] == 1, "the skill returns one image; extra ones would only be billed"
    assert body["model"] == image_gen._DEFAULT_MODEL
    assert body["model"].startswith("gpt-image-"), (
        "dall-e-3 is the model whose request shape the endpoint stopped accepting"
    )


@pytest.mark.asyncio
async def test_operator_can_pin_the_model(monkeypatch):
    monkeypatch.setenv("FERAL_IMAGE_MODEL", "gpt-image-2")
    captured: list = []
    _install_transport(monkeypatch, _ok_handler(captured))

    await image_gen.OpenAIImagesProvider("sk-test").generate("x")

    assert captured[0]["model"] == "gpt-image-2"


@pytest.mark.parametrize(
    "requested, sent",
    [
        ("1024x1024", "1024x1024"),
        ("1536x1024", "1536x1024"),
        ("1024x1536", "1024x1536"),
        ("auto", "auto"),
        # The DALL-E 3 sizes the old manifest advertised keep their
        # orientation instead of being refused or silently squared.
        ("1792x1024", "1536x1024"),
        ("1024x1792", "1024x1536"),
        ("banana", "1024x1024"),
        ("", "1024x1024"),
    ],
)
def test_sizes_are_normalised_to_what_the_model_accepts(requested, sent):
    assert image_gen._normalise_size(requested) == sent


@pytest.mark.asyncio
async def test_result_shape_the_brain_reads_is_unchanged(monkeypatch):
    """``image_b64`` is the field the tool-result image pipeline extracts."""
    monkeypatch.delenv("FERAL_IMAGE_MODEL", raising=False)
    _install_transport(monkeypatch, _ok_handler([]))

    skill = image_gen.ImageGenSkill()
    out = await skill.execute("generate", {"prompt": "a raccoon"}, {"image_gen": "sk-test"})

    assert out["success"] is True
    data = out["data"]
    assert data["b64"] == _PNG
    assert data["image_b64"] == _PNG
    assert data["url"] is None
    assert data["revised_prompt"] == "a raccoon, rendered"
    assert data["provider"] == image_gen._DEFAULT_MODEL


@pytest.mark.asyncio
async def test_a_400_from_openai_is_reported_not_swallowed(monkeypatch):
    """A rejected body must reach the model as a plain error, with the
    provider's own text, so the failure the operator saw is at least
    legible rather than a generic 'failed'."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "Unknown parameter: 'x'.", "param": "x"}})
    _install_transport(monkeypatch, handler)

    out = await image_gen.ImageGenSkill().execute("generate", {"prompt": "p"}, {"image_gen": "sk-test"})

    assert out["success"] is False
    assert out["status_code"] == 502
    assert "Unknown parameter: 'x'" in out["error"]
