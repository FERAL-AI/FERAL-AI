"""Codex app-server provider and runtime wiring tests."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from agents.llm_provider import SUPPORTED_RUNTIME_PROVIDERS, LLMProvider
from cli.setup.helpers import STATUS_READY
from cli.setup.steps.llm import _build_options
from providers.base import ChatMessage, ChatResponse
from providers.catalog import ProviderCatalog, ProviderStatus
from providers.codex_provider import (
    CodexAppServerClient,
    CodexAppServerError,
    CodexProvider,
)


class _QueueReader:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def readline(self) -> bytes:
        return await self.queue.get()

    def feed(self, message: dict | None) -> None:
        payload = b"" if message is None else json.dumps(message).encode() + b"\n"
        self.queue.put_nowait(payload)


class _FakeStdin:
    def __init__(self, handler) -> None:
        self.handler = handler

    def write(self, payload: bytes) -> None:
        self.handler(json.loads(payload.decode()))

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


class _FakeCodexProcess:
    def __init__(self) -> None:
        self.stdout = _QueueReader()
        self.stderr = _QueueReader()
        self.stdin = _FakeStdin(self._handle)
        self.returncode = None
        self.client_responses: list[dict] = []
        self.methods: list[str] = []

    def _handle(self, message: dict) -> None:
        method = message.get("method")
        if not method:
            self.client_responses.append(message)
            return
        self.methods.append(method)
        request_id = message["id"]
        if method == "initialize":
            result = {"userAgent": "codex-test"}
        elif method == "account/read":
            result = {
                "requiresOpenaiAuth": True,
                "account": {
                    "type": "chatgpt",
                    "email": "operator@example.com",
                    "planType": "plus",
                },
            }
        elif method == "model/list":
            result = {
                "data": [
                    {
                        "id": "gpt-secondary",
                        "model": "gpt-secondary",
                        "hidden": False,
                        "isDefault": False,
                    },
                    {
                        "id": "gpt-default",
                        "model": "gpt-default",
                        "hidden": False,
                        "isDefault": True,
                    },
                ],
                "nextCursor": None,
            }
        elif method == "thread/start":
            result = {"thread": {"id": "thread-1"}}
        elif method == "turn/start":
            result = {"turn": {"id": "turn-1"}}
        else:
            raise AssertionError(f"unexpected method: {method}")
        self.stdout.feed({"jsonrpc": "2.0", "id": request_id, "result": result})
        if method == "turn/start":
            self.stdout.feed(
                {
                    "jsonrpc": "2.0",
                    "id": 900,
                    "method": "item/commandExecution/requestApproval",
                    "params": {},
                }
            )
            self.stdout.feed(
                {
                    "method": "item/agentMessage/delta",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "itemId": "item-1",
                        "delta": "codex ",
                    },
                }
            )
            self.stdout.feed(
                {
                    "method": "item/agentMessage/delta",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "itemId": "item-1",
                        "delta": "works",
                    },
                }
            )
            self.stdout.feed(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "item": {
                            "id": "item-1",
                            "type": "agentMessage",
                            "text": "codex works",
                        },
                    },
                }
            )
            self.stdout.feed(
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "tokenUsage": {
                            "last": {
                                "inputTokens": 10,
                                "cachedInputTokens": 0,
                                "outputTokens": 2,
                                "reasoningOutputTokens": 1,
                                "totalTokens": 12,
                            },
                            "total": {},
                        },
                    },
                }
            )
            self.stdout.feed(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turn": {
                            "id": "turn-1",
                            "status": "completed",
                            "items": [
                                {
                                    "id": "item-1",
                                    "type": "agentMessage",
                                    "text": "codex works",
                                }
                            ],
                        },
                    },
                }
            )

    def terminate(self) -> None:
        self.returncode = 0
        self.stdout.feed(None)
        self.stderr.feed(None)

    def kill(self) -> None:
        self.terminate()

    async def wait(self) -> int:
        return int(self.returncode or 0)


def _client_factory(processes: list[_FakeCodexProcess]):
    async def process_factory(*_args, **_kwargs):
        process = _FakeCodexProcess()
        processes.append(process)
        return process

    def factory(**kwargs):
        return CodexAppServerClient(
            executable=kwargs["executable"],
            timeout_seconds=kwargs["timeout_seconds"],
            process_factory=process_factory,
        )

    return factory


def test_codex_provider_discovers_default_and_completes_turn(tmp_path):
    processes: list[_FakeCodexProcess] = []
    provider = CodexProvider(
        executable="codex-test",
        cwd=tmp_path,
        client_factory=_client_factory(processes),
    )

    async def run():
        models = await provider.refresh_models()
        response = await provider.chat(
            [
                ChatMessage(role="system", content="Be concise."),
                ChatMessage(role="user", content="Test the provider."),
            ],
            model=models[0],
        )
        await asyncio.sleep(0)
        return models, response

    models, response = asyncio.run(run())
    assert models == ["gpt-default", "gpt-secondary"]
    assert response.text == "codex works"
    assert response.usage == {
        "input_tokens": 10,
        "output_tokens": 2,
        "reasoning_tokens": 1,
        "total_tokens": 12,
    }
    assert any(
        message.get("id") == 900
        and message.get("result", {}).get("decision") == "decline"
        for process in processes
        for message in process.client_responses
    )


def test_catalog_exposes_codex_without_api_key(tmp_path):
    catalog = ProviderCatalog(cache_path=tmp_path / "models.json")
    descriptor = catalog.get_descriptor("codex")
    assert descriptor is not None
    assert descriptor.requires_api_key is False
    assert descriptor.display_name == "Codex (ChatGPT sign-in)"
    assert isinstance(catalog.get_adapter("codex"), CodexProvider)


def test_codex_provider_treats_empty_timeout_env_as_default(monkeypatch):
    monkeypatch.setenv("FERAL_CODEX_TIMEOUT_SECONDS", "")
    provider = CodexProvider()
    assert provider.timeout_seconds == 300.0


def test_codex_provider_rejects_api_key_auth_mode():
    client = CodexAppServerClient()

    async def request(_method, _params):
        return {
            "requiresOpenaiAuth": True,
            "account": {"type": "apiKey"},
        }

    client.request = request
    with pytest.raises(CodexAppServerError, match="not using a ChatGPT sign-in"):
        asyncio.run(client.account())


def test_setup_marks_signed_in_codex_ready_without_api_key(tmp_path):
    catalog = ProviderCatalog(cache_path=tmp_path / "models.json")
    status = ProviderStatus(
        provider_id="codex",
        display_name="Codex (ChatGPT sign-in)",
        supports_local=False,
        requires_api_key=False,
        configured=True,
        reachable=True,
    )
    options = _build_options(catalog, {"codex": status})
    codex = next(option for option in options if option.id == "codex")
    assert codex.status == STATUS_READY


class _RuntimeCodexAdapter:
    def cli_available(self) -> bool:
        return True

    async def chat(self, messages, *, model, **_kwargs):
        assert messages[-1].content == "hello"
        return ChatResponse(
            text="runtime works",
            model=model,
            usage={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
        )

    async def stream_events(self, messages, *, model, tools=None):
        del messages, model, tools
        yield {"type": "text_delta", "content": "stream "}
        yield {"type": "text_delta", "content": "works"}
        yield {"type": "done", "usage": {}, "text": "stream works"}


def test_llm_runtime_routes_codex_without_openai_key():
    adapter = _RuntimeCodexAdapter()
    env = {
        "FERAL_LLM_PROVIDER": "codex",
        "FERAL_LLM_MODEL": "gpt-default",
        "OPENAI_API_KEY": "",
    }

    async def run():
        with patch.dict("os.environ", env, clear=False), patch.object(
            LLMProvider, "_get_codex_adapter", return_value=adapter
        ):
            provider = LLMProvider()
            response = await provider.chat([{"role": "user", "content": "hello"}])
            events = []
            async for event in provider.chat_stream(
                [{"role": "user", "content": "hello"}]
            ):
                events.append(event)
            await provider.close()
            return provider, response, events

    provider, response, events = asyncio.run(run())
    assert "codex" in SUPPORTED_RUNTIME_PROVIDERS
    assert provider.provider == "codex"
    assert provider.available is True
    assert response["choices"][0]["message"]["content"] == "runtime works"
    assert [e for e in events if e["type"] == "text_delta"] == [
        {"type": "text_delta", "content": "stream "},
        {"type": "text_delta", "content": "works"},
    ]
    assert events[-1] == {"type": "done"}
