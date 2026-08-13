"""Codex app-server provider backed by the operator's ChatGPT sign-in.

This adapter deliberately uses Codex's supported app-server protocol
instead of extracting OAuth tokens or pretending the login is an OpenAI
API key. FERAL owns conversation state; each call therefore uses an
ephemeral Codex thread and returns the final assistant message in the
normal :class:`ChatResponse` shape.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, ClassVar, Self

from .base import BaseProvider, ChatMessage, ChatResponse

logger = logging.getLogger("feral.providers.codex")

_SANDBOX_MODES = {"read-only", "workspace-write", "danger-full-access"}

# The safe mode to land on whenever the request cannot be honoured. Codex
# runs with ``approvalPolicy: "never"``, so the sandbox is the only thing
# standing between a model-chosen command and the machine.
_SANDBOX_FALLBACK = "read-only"

# ``danger-full-access`` lets Codex run commands that never pass
# ``security/dangerous_tools.py``. Reaching it needs a second, explicit
# opt-in rather than one env var, so a copied .env or a tutorial cannot
# hand out unrestricted execution by itself.
_DANGER_OPT_IN = "FERAL_CODEX_ALLOW_DANGEROUS_SANDBOX"

# Dropped from the Codex subprocess environment. Codex authenticates
# itself, so it needs none of FERAL's credentials, and the subprocess is
# the one place in this provider where they would leave the process.
_SECRET_ENV_MARKERS = ("API_KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL", "PASSPHRASE")


def _resolve_sandbox(requested: str | None) -> str:
    """Return the sandbox mode to run under. Never raises.

    This used to raise ``ValueError`` on an unrecognised value. That runs
    inside ``CodexProvider.__init__``, which ``ProviderCatalog`` calls from
    its own ``__init__`` via ``_bind_builtin_adapters``, and that catch
    only handles ``ImportError``. So a typo in ``FERAL_CODEX_SANDBOX`` (an
    env var documented in .env.example) did not disable one provider, it
    aborted catalog construction for all sixteen and the brain did not
    boot. A misspelled setting must never be able to do that.

    Falling back is safe in the direction that matters: every unusable
    value lands on ``read-only``, so the failure mode is Codex being less
    capable than asked, never more.
    """
    value = (requested or os.getenv("FERAL_CODEX_SANDBOX") or _SANDBOX_FALLBACK).strip()

    if value not in _SANDBOX_MODES:
        logger.error(
            "Unsupported FERAL_CODEX_SANDBOX=%r; falling back to %r. Choose one of %s.",
            value, _SANDBOX_FALLBACK, sorted(_SANDBOX_MODES),
        )
        return _SANDBOX_FALLBACK

    if value == "danger-full-access" and os.getenv(_DANGER_OPT_IN, "").strip().lower() not in {"1", "true", "yes"}:
        logger.error(
            "FERAL_CODEX_SANDBOX=danger-full-access ignored; falling back to %r. "
            "Codex runs with approvalPolicy=never, so this grants command execution "
            "that bypasses FERAL's dangerous-tool gate entirely. Set %s=1 as well if "
            "that is genuinely what you want.",
            _SANDBOX_FALLBACK, _DANGER_OPT_IN,
        )
        return _SANDBOX_FALLBACK

    return value


def _child_env() -> dict[str, str]:
    """The environment for the Codex subprocess, minus FERAL's secrets.

    ``os.environ.copy()`` handed a child process every provider key the
    brain had loaded. Codex manages its own auth, so none of them are
    needed for it to work.
    """
    return {
        key: value
        for key, value in os.environ.items()
        if key.startswith("CODEX_")
        or not any(marker in key.upper() for marker in _SECRET_ENV_MARKERS)
    }


class CodexAppServerError(RuntimeError):
    """Raised when the Codex app-server protocol cannot complete a request."""


class CodexAppServerClient:
    """Small async JSONL client for ``codex app-server --stdio``."""

    def __init__(
        self,
        *,
        executable: str = "codex",
        timeout_seconds: float = 300.0,
        process_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self._process_factory = process_factory or asyncio.create_subprocess_exec
        self._process: Any = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._notifications: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._write_lock = asyncio.Lock()
        self._next_id = 1
        self._stderr_tail: deque[str] = deque(maxlen=30)

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._process is not None:
            return
        try:
            self._process = await self._process_factory(
                self.executable,
                "app-server",
                "--stdio",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_child_env(),
            )
        except FileNotFoundError as exc:
            raise CodexAppServerError(
                f"Codex CLI not found at {self.executable!r}. "
                "Install it and run `codex login`."
            ) from exc

        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())
        try:
            await self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "feral",
                        "title": "FERAL",
                        "version": "1",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return

        stdin = getattr(process, "stdin", None)
        if stdin is not None:
            with contextlib.suppress(Exception):
                stdin.close()
            wait_closed = getattr(stdin, "wait_closed", None)
            if wait_closed is not None:
                with contextlib.suppress(Exception):
                    await wait_closed()

        if getattr(process, "returncode", None) is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(Exception):
                    await process.wait()

        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._reader_task = None
        self._stderr_task = None
        self._fail_pending("Codex app-server closed")

    async def request(
        self, method: str, params: dict[str, Any] | None = None
    ) -> Any:
        if self._process is None:
            raise CodexAppServerError("Codex app-server is not running")
        request_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params or {},
                }
            )
        except Exception:
            self._pending.pop(request_id, None)
            future.cancel()
            raise
        try:
            return await asyncio.wait_for(future, timeout=self.timeout_seconds)
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise CodexAppServerError(
                f"Codex app-server timed out during {method}"
            ) from exc

    async def account(self) -> dict[str, Any]:
        result = await self.request("account/read", {"refreshToken": False})
        account = result.get("account") if isinstance(result, dict) else None
        if not account:
            raise CodexAppServerError(
                "Codex is not signed in. Run `codex login`, then retry FERAL setup."
            )
        if account.get("type") != "chatgpt":
            raise CodexAppServerError(
                "Codex is not using a ChatGPT sign-in. Run `codex logout`, then "
                "`codex login` and choose ChatGPT authentication."
            )
        return account

    async def list_models(self) -> list[dict[str, Any]]:
        models: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"includeHidden": False, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            result = await self.request("model/list", params)
            if not isinstance(result, dict):
                raise CodexAppServerError("Codex returned an invalid model list")
            models.extend(m for m in result.get("data", []) if isinstance(m, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                return models

    async def stream_turn(
        self,
        *,
        model: str,
        cwd: Path,
        sandbox: str,
        developer_instructions: str,
        input_text: str,
        effort: str = "",
    ) -> AsyncIterator[dict[str, Any]]:
        thread_params: dict[str, Any] = {
            "model": model or None,
            "cwd": str(cwd),
            "ephemeral": True,
            "approvalPolicy": "never",
            "sandbox": sandbox,
            "developerInstructions": developer_instructions or None,
            "dynamicTools": [],
            "environments": [],
        }
        thread_result = await self.request("thread/start", thread_params)
        thread = thread_result.get("thread", {}) if isinstance(thread_result, dict) else {}
        thread_id = thread.get("id")
        if not thread_id:
            raise CodexAppServerError("Codex did not return a thread id")

        turn_params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": input_text}],
        }
        if effort:
            turn_params["effort"] = effort
        turn_result = await self.request("turn/start", turn_params)
        turn = turn_result.get("turn", {}) if isinstance(turn_result, dict) else {}
        turn_id = turn.get("id")
        if not turn_id:
            raise CodexAppServerError("Codex did not return a turn id")

        deadline = time.monotonic() + self.timeout_seconds
        final_text = ""
        streamed = False
        usage: dict[str, int] = {}
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexAppServerError("Codex turn timed out")
            try:
                message = await asyncio.wait_for(
                    self._notifications.get(), timeout=remaining
                )
            except TimeoutError as exc:
                raise CodexAppServerError("Codex turn timed out") from exc

            method = message.get("method")
            params = message.get("params") or {}
            if params.get("threadId") != thread_id:
                continue
            if params.get("turnId") not in (None, turn_id):
                continue

            if method == "item/agentMessage/delta":
                delta = params.get("delta") or ""
                if delta:
                    streamed = True
                    yield {"type": "text_delta", "content": delta}
                continue

            if method == "item/completed":
                item = params.get("item") or {}
                if item.get("type") == "agentMessage":
                    final_text = item.get("text") or final_text
                continue

            if method == "thread/tokenUsage/updated":
                token_usage = params.get("tokenUsage") or {}
                usage = self._normalize_usage(token_usage.get("last") or {})
                continue

            if method != "turn/completed":
                continue

            completed_turn = params.get("turn") or {}
            for item in completed_turn.get("items", []) or []:
                if isinstance(item, dict) and item.get("type") == "agentMessage":
                    final_text = item.get("text") or final_text
            status = completed_turn.get("status") or "completed"
            if status == "failed":
                error = completed_turn.get("error") or {}
                detail = error.get("message") if isinstance(error, dict) else str(error)
                raise CodexAppServerError(detail or "Codex turn failed")
            if not streamed and final_text:
                yield {"type": "text_delta", "content": final_text}
            yield {
                "type": "done",
                "text": final_text,
                "usage": usage,
                "finish_reason": "stop" if status == "completed" else status,
            }
            return

    async def _read_stdout(self) -> None:
        process = self._process
        stdout = getattr(process, "stdout", None)
        if stdout is None:
            self._fail_pending("Codex app-server stdout is unavailable")
            return
        try:
            while True:
                line = await stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    logger.debug("Ignoring non-JSON Codex app-server output")
                    continue
                if not isinstance(message, dict):
                    continue
                if "id" in message and "method" not in message:
                    self._resolve_response(message)
                elif "id" in message and "method" in message:
                    await self._handle_server_request(message)
                elif "method" in message:
                    await self._notifications.put(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail every pending protocol call
            self._fail_pending(f"Codex app-server reader failed: {exc}")
            return
        self._fail_pending(self._exit_detail())

    async def _read_stderr(self) -> None:
        process = self._process
        stderr = getattr(process, "stderr", None)
        if stderr is None:
            return
        while True:
            line = await stderr.readline()
            if not line:
                return
            self._stderr_tail.append(line.decode("utf-8", errors="replace").strip())

    def _resolve_response(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        future = self._pending.pop(request_id, None)
        if future is None or future.done():
            return
        error = message.get("error")
        if error:
            detail = error.get("message") if isinstance(error, dict) else str(error)
            future.set_exception(CodexAppServerError(detail or "Codex request failed"))
        else:
            future.set_result(message.get("result"))

    async def _handle_server_request(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        method = message.get("method") or ""
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "execCommandApproval",
            "applyPatchApproval",
        }:
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"decision": "decline"},
                }
            )
            return
        if method == "item/tool/requestUserInput":
            await self._write(
                {"jsonrpc": "2.0", "id": request_id, "result": {"answers": {}}}
            )
            return
        if method == "mcpServer/elicitation/request":
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"action": "decline"},
                }
            )
            return
        if method == "currentTime/read":
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"currentTimeAt": int(time.time())},
                }
            )
            return
        if method == "item/tool/call":
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "success": False,
                        "contentItems": [
                            {
                                "type": "inputText",
                                "text": "FERAL did not expose this dynamic tool.",
                            }
                        ],
                    },
                }
            )
            return
        await self._write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"Unsupported method: {method}"},
            }
        )

    async def _write(self, message: dict[str, Any]) -> None:
        process = self._process
        stdin = getattr(process, "stdin", None)
        if process is None or stdin is None:
            raise CodexAppServerError("Codex app-server stdin is unavailable")
        payload = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        async with self._write_lock:
            stdin.write(payload)
            await stdin.drain()

    def _exit_detail(self) -> str:
        tail = " | ".join(line for line in self._stderr_tail if line)
        if tail:
            return f"Codex app-server exited: {tail}"
        return "Codex app-server exited unexpectedly"

    def _fail_pending(self, detail: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(CodexAppServerError(detail))
        self._pending.clear()

    @staticmethod
    def _normalize_usage(raw: dict[str, Any]) -> dict[str, int]:
        input_tokens = int(raw.get("inputTokens") or 0)
        output_tokens = int(raw.get("outputTokens") or 0)
        reasoning_tokens = int(raw.get("reasoningOutputTokens") or 0)
        total_tokens = int(raw.get("totalTokens") or input_tokens + output_tokens)
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "reasoning_tokens": reasoning_tokens,
            "total_tokens": total_tokens,
        }


class CodexProvider(BaseProvider):
    """FERAL provider that delegates turns to Codex app-server."""

    provider_id = "codex"
    display_name = "Codex (ChatGPT sign-in)"
    _capabilities: ClassVar[set[str]] = {"streaming", "thinking"}

    def __init__(
        self,
        *,
        executable: str | None = None,
        cwd: str | Path | None = None,
        sandbox: str | None = None,
        timeout_seconds: float | None = None,
        client_factory: Callable[..., CodexAppServerClient] | None = None,
    ) -> None:
        self.executable = executable or os.getenv("FERAL_CODEX_PATH", "codex")
        self.cwd = Path(
            cwd
            or os.getenv("FERAL_CODEX_CWD", "")
            or Path.home() / ".feral" / "codex-workspace"
        ).expanduser()
        self.sandbox = _resolve_sandbox(sandbox)
        self.timeout_seconds = timeout_seconds or float(
            os.getenv("FERAL_CODEX_TIMEOUT_SECONDS") or "300"
        )
        self.reasoning_effort = os.getenv("FERAL_CODEX_REASONING_EFFORT", "")
        self._client_factory = client_factory or CodexAppServerClient
        self._models: list[str] = []
        self._warned_tools = False

    def cli_available(self) -> bool:
        path = Path(self.executable).expanduser()
        if path.is_absolute() or "/" in self.executable:
            return path.is_file() and os.access(path, os.X_OK)
        return shutil.which(self.executable) is not None

    async def refresh_models(self) -> list[str]:
        async with self._client() as client:
            await client.account()
            records = await client.list_models()
        visible = [record for record in records if not record.get("hidden")]
        visible.sort(key=lambda record: not bool(record.get("isDefault")))
        models: list[str] = []
        for record in visible:
            model = str(record.get("model") or record.get("id") or "").strip()
            if model and model not in models:
                models.append(model)
        if not models:
            raise CodexAppServerError("Codex returned no available models")
        self._models = models
        return list(models)

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        max_tokens: int | None = None,
        temperature: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        del max_tokens, temperature, kwargs
        text = ""
        final_text = ""
        usage: dict[str, int] = {}
        finish_reason = "stop"
        async for event in self.stream_events(messages, model=model, tools=tools):
            if event.get("type") == "text_delta":
                text += str(event.get("content") or "")
            elif event.get("type") == "done":
                final_text = str(event.get("text") or "")
                usage = event.get("usage") or {}
                finish_reason = str(event.get("finish_reason") or "stop")
        return ChatResponse(
            text=final_text or text,
            model=model,
            usage=usage,
            finish_reason=finish_reason,
        )

    async def stream_events(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        if tools and not self._warned_tools:
            logger.warning(
                "Codex provider received FERAL tools, but dynamic-tool bridging "
                "is not available; this turn will run without those tools"
            )
            self._warned_tools = True
        self.cwd.mkdir(parents=True, exist_ok=True)
        developer_instructions, input_text = self._format_messages(messages)
        async with self._client() as client:
            await client.account()
            async for event in client.stream_turn(
                model=model,
                cwd=self.cwd,
                sandbox=self.sandbox,
                developer_instructions=developer_instructions,
                input_text=input_text,
                effort=self.reasoning_effort,
            ):
                yield event

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        tools = kwargs.get("tools")

        async def _stream() -> AsyncIterator[str]:
            async for event in self.stream_events(messages, model=model, tools=tools):
                if event.get("type") == "text_delta":
                    yield str(event.get("content") or "")

        return _stream()

    def _client(self) -> CodexAppServerClient:
        return self._client_factory(
            executable=self.executable,
            timeout_seconds=self.timeout_seconds,
        )

    @staticmethod
    def _format_messages(messages: list[ChatMessage]) -> tuple[str, str]:
        systems = [message.content for message in messages if message.role == "system"]
        developer = (
            "You are the language-model backend for FERAL. Follow the supplied "
            "conversation and return the assistant's next response. Do not inspect "
            "files or run commands unless the user explicitly asks for that work."
        )
        if systems:
            developer += "\n\nFERAL runtime instructions:\n" + "\n\n".join(systems)

        conversation = [message for message in messages if message.role != "system"]
        if len(conversation) == 1 and conversation[0].role == "user":
            return developer, conversation[0].content

        rendered: list[str] = [
            "Continue this conversation as the assistant. Respond to the final user message."
        ]
        for message in conversation:
            label = message.role.upper()
            rendered.append(f"[{label}]\n{message.content}")
            if message.tool_calls:
                rendered.append(
                    "[ASSISTANT TOOL CALLS]\n"
                    + json.dumps(message.tool_calls, ensure_ascii=True)
                )
        return developer, "\n\n".join(rendered)
