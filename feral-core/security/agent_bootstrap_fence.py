"""ASGI ingress fence for an unavailable native encrypted-memory agent."""
import json


class AgentBootstrapFence:
    PASSIVE = frozenset({"/health", "/api/auth/local-key", "/api/boot-report", "/api/setup/status"})
    CONTROL = ("/api/security/vault/", "/api/security/agent-bootstrap")

    def __init__(self, app, state):
        self.app, self.state = app, state

    def blocked(self):
        return bool(getattr(self.state, "_native_vault_deferred", False) and
                    getattr(self.state, "_native_bootstrap_required", False))

    async def __call__(self, scope, receive, send):
        kind, path = scope["type"], scope.get("path", "")
        control = path == "/api/security/agent-bootstrap" or path.startswith(self.CONTROL)
        passive = scope.get("method") == "GET" and path in self.PASSIVE
        if kind == "http" and self.blocked() and not (control or passive):
            body = json.dumps({"detail": {"code": "agent_bootstrap_required", "message": "Unlock encrypted memory and review agent continuation before using agent features."}}).encode()
            await send({"type": "http.response.start", "status": 503, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        if kind == "websocket":
            if self.blocked():
                await send({"type": "websocket.close", "code": 1013})
                return
            async def checked_receive():
                message = await receive()
                if self.blocked() and message["type"] != "websocket.disconnect":
                    await send({"type": "websocket.close", "code": 1013})
                    return {"type": "websocket.disconnect", "code": 1013}
                return message
            await self.app(scope, checked_receive, send)
            return
        await self.app(scope, receive, send)
