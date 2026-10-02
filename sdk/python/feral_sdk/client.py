"""
FeralClient — Connect to a running FERAL Brain via HTTP + WebSocket.

Usage::

    async with FeralClient("http://localhost:9090") as client:
        response = await client.chat("What's the weather?")
        print(response)

        dashboard = await client.get_dashboard()
        print(dashboard["skills_count"])
"""

from __future__ import annotations
import asyncio
import json
import logging
from typing import Any, AsyncIterator
from contextlib import asynccontextmanager

import httpx

logger = logging.getLogger("feral.sdk.client")


class FeralClient:
    """HTTP + WebSocket client for the FERAL Brain API."""

    def __init__(
        self,
        base_url: str = "http://localhost:9090",
        *,
        bearer_token: str | None = None,
        timeout: float | httpx.Timeout = 30,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.ws_url = self.base_url.replace("http://", "ws://").replace("https://", "wss://") + "/v1/session"
        headers = {"Accept": "application/json"}
        if bearer_token is not None:
            if not bearer_token.strip() or any(c in bearer_token for c in "\r\n"):
                raise ValueError("bearer_token must be a nonempty, single-line credential")
            headers["Authorization"] = f"Bearer {bearer_token}"
        self._http = httpx.AsyncClient(
            base_url=self.base_url, timeout=timeout, headers=headers, transport=transport,
            follow_redirects=False,
        )
        self._ws = None
        self._session_id: str | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def close(self):
        await self._http.aclose()
        if self._ws:
            await self._ws.close()

    async def health(self) -> dict:
        """Check brain health."""
        return await self._request_object("GET", "/health")

    async def _request_json(self, method: str, path: str, **kwargs) -> Any:
        response = await self._http.request(method, path, **kwargs)
        response.raise_for_status()
        try:
            return response.json()
        except ValueError:
            # Do not include a response body or credential in diagnostics.
            raise ValueError("Brain returned invalid JSON") from None

    async def _request_object(self, method: str, path: str, **kwargs) -> dict:
        data = await self._request_json(method, path, **kwargs)
        if not isinstance(data, dict):
            raise ValueError("Brain returned a JSON value where an object was required")
        return data

    @staticmethod
    def _rows(data: Any) -> list[dict]:
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise ValueError("Brain returned an invalid record list")
        return data

    async def get_dashboard(self) -> dict:
        """Get aggregated dashboard data."""
        return await self._request_object("GET", "/api/dashboard")

    async def get_system_info(self) -> dict:
        """Get system info (version, memory stats, etc.)."""
        return await self._request_object("GET", "/api/system/info")

    async def chat(self, message: str, session_id: str | None = None) -> str:
        """Send a text message and wait for the full response."""
        import websockets

        ws_url = self.ws_url
        async with websockets.connect(ws_url) as ws:
            greeting = json.loads(await ws.recv())
            sid = greeting.get("session_id", session_id or "sdk")

            await ws.send(json.dumps({
                "type": "text_command",
                "session_id": sid,
                "payload": {"text": message},
            }))

            response_parts = []
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=60)
                    msg = json.loads(raw)
                    if msg.get("type") == "text_response":
                        return msg["payload"]["text"]
                    elif msg.get("type") == "stream_delta":
                        if msg["payload"].get("is_final"):
                            return "".join(response_parts)
                        response_parts.append(msg["payload"].get("delta", ""))
                except asyncio.TimeoutError:
                    return "".join(response_parts) or "[timeout]"

    async def list_skills(self) -> list[dict]:
        """List all registered skills."""
        data = await self._request_json("GET", "/skills")
        return self._rows(data.get("skills") if isinstance(data, dict) else data)

    async def search_memory(self, query: str, limit: int = 10) -> list[dict]:
        """Search the agent's memory."""
        data = await self._request_object("GET", "/api/memory/search", params={"q": query, "limit": limit})
        return self._rows(data.get("results"))

    async def create_note(
        self, content: str, tags: list[str] | None = None, *,
        session_id: str | None = None, confirm: bool = False,
    ) -> dict:
        """Save a note through policy gates; return the tool result envelope."""
        return await self.invoke_skill(
            "notes_memory", "save_note", {"content": content, "tags": tags or []},
            session_id=session_id, confirm=confirm,
        )

    async def list_conversations(self, limit: int = 20) -> list[dict]:
        """List conversation threads."""
        data = await self._request_object("GET", "/api/conversations", params={"limit": limit})
        return self._rows(data.get("conversations"))

    async def invoke_skill(
        self, skill_id: str, endpoint: str, args: dict | None = None, *,
        session_id: str | None = None, confirm: bool = False,
    ) -> dict:
        """Invoke through REST policy gates, without confirming or retrying implicitly.

        HTTP failures raise httpx.HTTPStatusError. An HTTP-200 tool rejection is
        returned unchanged; inspect success/status_code/error before reporting an
        action as executed. confirm=True is an explicit caller assertion of review.
        """
        if type(confirm) is not bool:
            raise ValueError("confirm must be a Boolean")
        if args is not None and not isinstance(args, dict):
            raise ValueError("args must be an object")
        body = {"skill_id": skill_id, "endpoint": endpoint, "args": args or {}, "confirm": confirm}
        if session_id is not None:
            body["session_id"] = session_id
        return await self._request_object("POST", "/api/tools/execute", json=body)
