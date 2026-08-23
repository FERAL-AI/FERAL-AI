"""Private OpenAI-compatible STT is explicit and fail-closed."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from perception.audio_pipeline import AudioPipeline


def _configure(monkeypatch, *, endpoint: str = "http://stt.internal/v1/audio/transcriptions"):
    monkeypatch.setenv("FERAL_STT_PROVIDER", "openai-compatible")
    monkeypatch.setenv("FERAL_STT_MODEL", "whisper-v3:turbo")
    monkeypatch.setenv("FERAL_STT_ENDPOINT", endpoint)
    monkeypatch.setenv("FERAL_STT_TIMEOUT_SECONDS", "900")
    monkeypatch.delenv("FERAL_STT_API_KEY", raising=False)


@pytest.mark.asyncio
async def test_private_endpoint_needs_no_key_and_never_inherits_openai_key(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leave-this-process")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers.get("authorization") is None
        assert request.url == httpx.URL(
            "http://stt.internal/v1/audio/transcriptions"
        )
        assert b'whisper-v3:turbo' in request.content
        assert request.content.startswith(b"--")
        return httpx.Response(200, json={"text": "private transcript"})

    pipeline = AudioPipeline()
    await pipeline._compatible_stt_client.aclose()
    pipeline._compatible_stt_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=pipeline._stt_timeout
    )
    try:
        result = await pipeline._transcribe(
            b"\x00\x01" * 800, encoding="pcm16", sample_rate=24000
        )
    finally:
        await pipeline.close()

    assert result == "private transcript"
    assert len(seen) == 1
    assert pipeline._stt_timeout == 900


@pytest.mark.asyncio
async def test_private_endpoint_uses_only_its_dedicated_optional_key(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("FERAL_STT_API_KEY", "private-token")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-openai-token")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer private-token"
        assert "unrelated-openai-token" not in str(request.headers)
        return httpx.Response(200, text="plain-text transcript")

    pipeline = AudioPipeline()
    headers = dict(pipeline._compatible_stt_client.headers)
    await pipeline._compatible_stt_client.aclose()
    pipeline._compatible_stt_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers=headers
    )
    try:
        result = await pipeline._transcribe(
            b"\x00\x01" * 800, encoding="pcm16", sample_rate=16000
        )
    finally:
        await pipeline.close()

    assert result == "plain-text transcript"


@pytest.mark.asyncio
async def test_private_endpoint_failure_never_falls_through_to_openai(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "valid-but-forbidden-fallback")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private endpoint offline", request=request)

    pipeline = AudioPipeline()
    pipeline._transcribe_cloud = AsyncMock(return_value="cloud transcript")
    await pipeline._compatible_stt_client.aclose()
    pipeline._compatible_stt_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    try:
        result = await pipeline._transcribe(
            b"\x00\x01" * 800, encoding="pcm16", sample_rate=16000
        )
    finally:
        await pipeline.close()

    assert result is None
    pipeline._transcribe_cloud.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_private_endpoint_is_unavailable_not_cloud(monkeypatch):
    _configure(monkeypatch, endpoint="")
    monkeypatch.setenv("OPENAI_API_KEY", "valid-but-forbidden-fallback")
    pipeline = AudioPipeline()
    pipeline._transcribe_cloud = AsyncMock(return_value="cloud transcript")
    try:
        assert pipeline.selected_stt_ready is False
        result = await pipeline._transcribe(
            b"\x00\x01" * 800, encoding="pcm16", sample_rate=16000
        )
    finally:
        await pipeline.close()

    assert result is None
    pipeline._transcribe_cloud.assert_not_awaited()


@pytest.mark.asyncio
async def test_endpoint_rejects_embedded_credentials(monkeypatch):
    _configure(
        monkeypatch,
        endpoint="http://user:password@stt.internal/v1/audio/transcriptions",
    )
    pipeline = AudioPipeline()
    try:
        assert pipeline._stt_endpoint == ""
        assert pipeline.selected_stt_ready is False
    finally:
        await pipeline.close()
