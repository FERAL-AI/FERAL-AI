"""
FERAL Brain — Unleashed AI Core
==========================================
The local-first agentic brain. Runs on the user's machine.
Clients (phone, web, daemon, glasses, robots) connect via WebSocket.
MCP clients (Claude, Cursor) connect via JSON-RPC.
Channels (Telegram, Discord, Slack) bridge messaging platforms.
"""

import asyncio
import collections
import logging
import os
import re
import secrets
import time
from collections.abc import Awaitable  # noqa: F401 — used by quoted return annotations in WS9 task spawners
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, HTMLResponse, FileResponse, RedirectResponse
from starlette.routing import compile_path

from version import VERSION as __version__
from models.protocol import (
    HUP_VERSION,
    FeralMessage,
    TextCommandPayload,
    UIEventPayload,
    NodeRegisterPayload,
    TextResponsePayload,
    DeviceRegisterPayload,
    AudioChunkPayload,
    parse_message,
)
from config.runtime import brain_bind_host, brain_port, brain_public_base_url
from gateway.protocol import GatewaySession

from api.state import state, _log_activity, VISION_MAX_FRAME_KB
from api.routes.config import _build_greeting
from api.routes.dashboard import _get_dashboard_data

from security import session_auth as _session_auth_module
from security.session_auth import (
    session_auth_required,
    verify_session,
    is_localhost,
    local_bypass_enabled,
    warn_if_unsafe_bypass,
)
from security.device_pairing import DevicePairingStore  # used in type hint

from api.routes.dashboard import router as dashboard_router
from api.routes.config import router as config_router
from api.routes.skills import router as skills_router
from api.routes.tools import router as tools_router
from api.routes.memory import router as memory_router
from api.routes.routines import router as routines_router
from api.routes.taskflows import router as taskflows_router
from api.routes.llm import router as llm_router
from api.routes.audio import router as audio_router
from api.routes.genui import router as genui_router
from api.routes.mcp import router as mcp_router
from api.routes.channels import router as channels_router
from api.routes.conversations import router as conversations_router
from api.routes.access import router as access_router
from api.routes.devices import router as devices_router
from api.routes.timeline import router as timeline_router
from api.routes.brain_rest import router as brain_rest_router
from api.routes.baseline import router as baseline_router
from api.routes.handoff import router as handoff_router
from api.routes.tool_genesis import router as tool_genesis_router
from api.routes.agent_mitosis import router as agent_mitosis_router
from api.routes.intents import router as intents_router
from api.routes.webhooks import router as webhooks_router
from api.routes.outgoing_webhooks import router as outgoing_webhooks_router
from api.routes.ambient import router as ambient_router
from api.routes.auth import router as auth_router
from api.routes.personas import router as personas_router
from api.routes.jobs import router as jobs_router
from api.routes.consciousness import router as consciousness_router
from api.routes.about_me import router as about_me_router
from api.routes.ideas import router as ideas_router
from api.routes.apps import router as apps_router
from api.routes.uploads import router as uploads_router  # PR 10
from api.routes.supervisor import router as supervisor_router
from api.routes.twin import router as twin_router
from api.routes.sessions import router as sessions_router  # 
from api.routes.capabilities import router as capabilities_router  # Phase 5
from api.routes.system_permissions import router as system_permissions_router  # Phase 11
from api.routes.discovery import router as discovery_router  # Phase 13
from api.routes.approvals import router as approvals_router
from api.routes.checkpoints import router as checkpoints_router
# --- Subagent A (realtime GA) additions ---
from api.routes.realtime_client_secret import router as realtime_client_secret_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s")
logger = logging.getLogger("feral.brain")


# ─────────────────────────────────────────────
# Application
# ─────────────────────────────────────────────

app = FastAPI(
    title="FERAL Brain",
    description="FERAL — Open AI agent with computer use, GenUI, voice, and hardware control",
    version=__version__,
)

from observability.metrics import init_metrics
init_metrics("feral")

CORS_ORIGINS = os.getenv("FERAL_CORS_ORIGINS", "http://localhost:5173,http://localhost:9090").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────
# Rate Limiting Middleware
# ─────────────────────────────────────────────

_rate_limit_store: collections.OrderedDict[str, collections.deque] = collections.OrderedDict()
# Default: 1200 req/min per remote IP. Local-first clients poll aggressively
# (dashboard / ambient / jobs / skills). We keep the limit but trust loopback.
RATE_LIMIT_RPM = int(os.getenv("FERAL_RATE_LIMIT_RPM", "1200"))
_RATE_LIMIT_MAX_KEYS = 10_000
_rate_limit_last_cleanup = 0.0

# Loopback clients (the Brain + same-host browser / CLI / iOS sim) are never
# rate-limited — that would throttle the app talking to itself.
_LOOPBACK_IPS = frozenset({"127.0.0.1", "::1", "localhost", "unknown"})

# Low-cost polling endpoints exempted from the per-IP bucket so a UI tab cannot
# DoS itself. These are idempotent reads that the Brain should always answer.
_RATE_LIMIT_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/health",
    "/metrics",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/api/dashboard",
    "/api/ambient/",
    "/api/ideas/",
    "/api/jobs",
    "/api/skills",
    "/api/channels",
    "/api/llm/status",
    "/api/identity",
    "/api/soul",
    "/api/memory/",
    # Pairing endpoints + installer must stay unthrottled — fresh phones
    # hit them before anything else and we don't want to lock them out.
    "/api/devices/pair",
    "/install-phone-bridge.sh",
    # Supervisor oversight surface polls aggressively on the /oversight
    # v2 page; it's a read-only audit view.
    "/api/supervisor/events",
    "/api/supervisor/stats",
    # Twin policy + approval queue polling.
    "/api/twin/",
)


def _route_template_for(request) -> str:
    """Return the FastAPI route template for *request* (e.g. ``/api/jobs/{id}``).

    Falls back to the literal path when the matcher hasn't run yet
    (rare — only happens for routes resolved by the catch-all SPA
    handler). Using the template instead of the raw path keeps
    ``feral_http_requests_total`` cardinality bounded.
    """
    route = request.scope.get("route")
    path_template = getattr(route, "path", None)
    return path_template or request.url.path


def _status_class(code: int) -> str:
    return f"{code // 100}xx" if 100 <= code < 600 else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        global _rate_limit_last_cleanup
        client_ip = request.client.host if request.client else "unknown"
        path = request.url.path

        # Skip loopback + well-known read-only polling endpoints entirely.
        if client_ip in _LOOPBACK_IPS:
            response = await call_next(request)
            _emit_http_metrics(request, response, time.time())
            return response
        if any(path == p or path.startswith(p) for p in _RATE_LIMIT_EXEMPT_PREFIXES):
            response = await call_next(request)
            _emit_http_metrics(request, response, time.time())
            return response

        now = time.time()

        if now - _rate_limit_last_cleanup > 60:
            _rate_limit_last_cleanup = now
            cutoff = now - 60
            stale = [k for k, v in _rate_limit_store.items() if not v or v[-1] < cutoff]
            for k in stale:
                del _rate_limit_store[k]

        if client_ip in _rate_limit_store:
            _rate_limit_store.move_to_end(client_ip)
        window = _rate_limit_store.setdefault(client_ip, collections.deque())
        while window and window[0] < now - 60:
            window.popleft()
        if len(window) >= RATE_LIMIT_RPM:
            response = JSONResponse({"error": "Rate limit exceeded"}, status_code=429)
            _emit_http_metrics(request, response, now)
            return response
        window.append(now)

        while len(_rate_limit_store) > _RATE_LIMIT_MAX_KEYS:
            _rate_limit_store.popitem(last=False)

        response = await call_next(request)
        _emit_http_metrics(request, response, now)
        return response


def _emit_http_metrics(request, response, started_at: float) -> None:
    """ proof-of-concept: emit feral_http_requests_total + duration.

    This is the ONLY emit() call site this PR ships — every other
    module's emit() wiring is deferred to  so each owning
    workstream lands its own changes inside its own owned-paths set.
    """
    from observability.metrics import emit  # local import — keeps the
    # cold-import cost off the boot path when metrics are killed.

    status = getattr(response, "status_code", 0)
    labels = {
        "method": request.method,
        "route": _route_template_for(request),
        "status": _status_class(status),
    }
    emit("feral_http_requests_total", labels=labels)
    emit(
        "feral_http_request_duration_seconds",
        value=max(0.0, time.time() - started_at),
        labels={"method": labels["method"], "route": labels["route"]},
    )


app.add_middleware(RateLimitMiddleware)


# ─────────────────────────────────────────────
# Optional REST API Key Middleware (Part C)
# ─────────────────────────────────────────────

from api.keys import load_or_generate_api_key as _generate_key_impl
from api.keys import load_api_key as _load_api_key
from api.keys import get_api_key_path as _get_api_key_path


def _load_or_generate_api_key() -> str:
    """Load FERAL_API_KEY from env or ~/.feral/api_key; generate on first boot."""
    key_path = _get_api_key_path()
    existed = (key_path.exists() and key_path.read_text().strip()) or os.environ.get("FERAL_API_KEY", "").strip()
    key = _generate_key_impl()
    if not existed:
        print("=" * 70)
        print("FERAL: Generated new API key on first boot.")
        print(f"Location: {key_path}")
        print(f"Key: {key}")
        print("Use this key to authenticate clients (iOS, Android, browser ext).")
        print("Set FERAL_API_KEY env var to override.")
        print("=" * 70)
    return key


FERAL_API_KEY = _load_or_generate_api_key()


_OPEN_PATHS = frozenset({
    "/health", "/docs", "/redoc", "/openapi.json", "/metrics",
    "/api/auth/local-key", "/api/boot-report",
    # Phone-bridge installer script must be fetchable without an API key
    # because it's delivered over `curl … | bash` from a laptop / phone
    # that doesn't have the key yet.
    "/install-phone-bridge.sh",
    # Note: ``/api/devices/pair/url`` and ``/api/devices/pair/qr``
    # used to be open-listed here so a brand-new phone could fetch
    # them. That was wrong — those endpoints **mint** pairing
    # tokens; leaving them open meant any LAN attacker could spam
    # token issuance and pollute the paired_devices table (or, in
    # Mode C, exfiltrate one-time tokens by guessing the URL). They
    # are now authenticated: the dashboard (which has the API key)
    # is the only client that issues tokens; the phone receives the
    # already-issued URL inside the QR / Bluetooth handoff and
    # only ever talks to the **claim** half of the flow
    # (``/pair/check`` → ``/pair/verify_pin`` → ``/pair/complete``)
    # which stays open below.
    "/api/devices/pair/complete",
    # Code-pair flow (SDK ↔ dashboard typed pair code).
    # Daemon announces an 8-char base32 code, dashboard claims it.
    # Codes have ~38 bits of entropy, 600s TTL, and /code/claim is
    # rate-limited to 5 wrong attempts per IP per 15 minutes — see
    # ``feral-core/api/middleware/rate_limit.py``.
    "/api/devices/pair/announce",
    "/api/devices/pair/status",
    "/api/devices/pair/code/claim",
    # PIN second-factor (pair-pin-confirm PR). The phone calls /check
    # before rendering the form to learn whether a PIN is required;
    # /verify_pin is how it submits the PIN before /complete is allowed
    # to issue a phone_bearer. Both are open-listed because the phone
    # has the URL token but no API key yet.
    "/api/devices/pair/check",
    "/api/devices/pair/verify_pin",
})

_OPEN_PATH_PREFIXES = (
    "/docs",
    "/redoc",
    "/api/oauth/callback",
    "/webhooks/",
)

# Narrow GET-only allowlist for the device-pairing landing page and the
# static bundle it needs to boot. A phone on the LAN that scanned the
# pairing QR will not have the Brain's API key yet; locking these paths
# behind Bearer-auth would make `/pair?t=…` unusable off-loopback. The
# pairing token is validated separately on the WebSocket handshake
# (`verify_device`), so serving the SPA shell + hashed asset bundles
# here does not widen the authenticated API surface.
_OPEN_GET_PATHS = frozenset({
    "/pair",
    "/v2/pair",
    # PWA + browser metadata. A phone scanning a Mode-A LAN pair URL
    # is not on loopback and does not yet have an API key; the bundle
    # fetches these eagerly during boot. Without them in the GET
    # allowlist the pair flow worked but PWA install was silently
    # broken (manifest 401 → no "Add to Home Screen" prompt; favicon
    # 401 → red console errors that look scary). They are static and
    # carry no secrets.
    "/manifest.webmanifest",
    "/favicon.ico",
    "/sw.js",
})

_OPEN_GET_PATH_PREFIXES = (
    "/assets/",
    "/v2/assets/",
    "/icons/",
)


class _PathAllowlist:
    """API-key middleware allowlist that supports literal paths, prefix
    matches, and FastAPI-style parameterized patterns
    (``/api/approvals/{request_id}/approve``).

    Parameterized patterns compile via Starlette's ``compile_path`` —
    the same function FastAPI's router uses — so a match here is
    behaviourally identical to a match at routing time. This kills the
    pre-audit-r12 class of bug where the allowlist literal
    (``/api/approvals/approve``) drifted from the real route path
    (``/api/approvals/{request_id}/approve``) and the operator's iOS
    Approvals tab silently 401'd.

    Used by:

    1. ``APIKeyMiddleware.dispatch`` — decides whether a phone-bearer
       request is acceptable for the requested ``(method, path)``.
    2. ``_assert_allowlist_routes_exist`` — startup invariant that
       refuses to boot the brain if any entry on this allowlist does
       not match a route actually registered on the FastAPI app.
    """

    __slots__ = ("name", "_literals", "_prefixes", "_patterns")

    def __init__(self, name: str) -> None:
        self.name = name
        self._literals: set[str] = set()
        self._prefixes: list[str] = []
        # (original pattern, compiled anchored regex from compile_path)
        self._patterns: list[tuple[str, re.Pattern[str]]] = []

    def add_literal(self, path: str) -> None:
        self._literals.add(path)

    def add_prefix(self, prefix: str) -> None:
        if prefix not in self._prefixes:
            self._prefixes.append(prefix)

    def add_pattern(self, pattern: str) -> None:
        """Register a FastAPI-style parameterized path like
        ``/api/approvals/{request_id}/approve``. The compiled regex is
        anchored end-to-end, so ``/api/approvals/x/approve/extra``
        does NOT match (parity with FastAPI's own dispatch)."""
        regex, *_ = compile_path(pattern)
        self._patterns.append((pattern, regex))

    def matches(self, path: str) -> bool:
        if path in self._literals:
            return True
        for prefix in self._prefixes:
            if path.startswith(prefix):
                return True
        for _, regex in self._patterns:
            if regex.match(path):
                return True
        return False

    def literals(self) -> frozenset[str]:
        return frozenset(self._literals)

    def prefixes(self) -> tuple[str, ...]:
        return tuple(self._prefixes)

    def patterns(self) -> tuple[str, ...]:
        return tuple(p for p, _ in self._patterns)


# v2026.5.26 — paths the iOS companion / web client reads with the
# operator's phone-bearer token (minted during pair flow). The
# APIKeyMiddleware below treats a valid `phone_bearer` like the
# dashboard API key for these endpoints ONLY — destructive operations
# still require the explicit FERAL_API_KEY.
#
# Background: prior to v2026.5.26, APIKeyMiddleware accepted only
# `Bearer <FERAL_API_KEY>` on HTTP. The phone-bearer scheme worked on
# the WebSocket handshake (verify_phone_bearer at server.py:1234) but
# every HTTP call from the iOS app 401'd, breaking the Phase 7b-2
# Context tab + the entire Phase 13 / Phase 10 phone surface.
#
# v2026.5.32 (audit-r12 D1) — refactored from raw frozenset literals
# to `_PathAllowlist` so parameterised routes like
# `/api/approvals/{request_id}/approve` actually match. Five entries
# had drifted from their canonical routes; the new
# `_assert_allowlist_routes_exist` invariant (called at module bottom)
# now refuses to boot the brain on any further drift.
#
# Scope is intentionally narrow — every endpoint registered here is
# read-mostly or returns operator-owned data the phone already has
# implicit access to via the WS bridge. Anything that mutates
# server-wide state (skill installs, vault writes, autonomy changes,
# config updates, OAuth grants, etc.) stays gated to the dashboard
# API key.
_PHONE_BEARER_GET = _PathAllowlist("_PHONE_BEARER_GET")
for _p in (
    "/api/context/live",                  # Phase 7b-2 iOS Context tab
    "/api/sessions/primary",              # Phase 3 — primary session id
    "/api/sessions/primary/transcript",   # Phase 9 — chat resume (GET, since_ms is a query arg)
    "/api/capabilities",                  # Phase 5 — capability registry
    "/api/capabilities/has",              # Phase 5 — routability probe (GET with query)
    "/api/system/permissions",            # Phase 11 — macOS TCC status
    "/api/discovery/brain",               # Phase 13 — onboarding wizard
    "/api/devices",                       # Phase 10 — connected devices
    "/api/devices/connected",             # Phase 10 — live HUP set
    "/api/ambient/next_event",            # ambient calendar context
    "/api/ambient/briefing",              # ambient morning summary (was the stale "/digest")
    "/api/conversations",                 # chat history list
    "/api/conversations/active/thread",
    "/api/memory/context",                # memory read
    "/api/timeline",                      # operator timeline (single route, was the stale "/api/timeline/" prefix)
    "/api/autonomy",                      # iOS may surface current tier
):
    _PHONE_BEARER_GET.add_literal(_p)
# Prefix-matched read-mostly families. Anything below a prefix here is
# accepted for phone bearers. Specific paths under these prefixes that
# need to stay dashboard-only should be FILTERED later in middleware,
# but today the prefixes here are read-only by construction.
# v2026.5.32 (audit-r12 D1): dropped "/api/skills/" prefix's bare-literal
# companion ("/api/skills" was dead — no GET /api/skills route exists;
# only sub-paths like /api/skills/pending are real, and the prefix below
# already covers them) and the "/api/timeline/" prefix (no routes under
# it; the single canonical route is GET /api/timeline, now a literal above).
for _p in (
    "/api/conversations/",   # GET /api/conversations/{id}
    "/api/skills/",          # GET /api/skills/pending, /api/skills/{id} (latter aspirational)
):
    _PHONE_BEARER_GET.add_prefix(_p)
del _p

# POST endpoints the iOS app legitimately needs. Tight allowlist;
# approvals + UI events are the operator-facing surface that already
# has WebSocket equivalents (so the security envelope is unchanged).
_PHONE_BEARER_POST = _PathAllowlist("_PHONE_BEARER_POST")
_PHONE_BEARER_POST.add_literal("/api/system/permissions/open")  # Phase 13 — open Settings pane
_PHONE_BEARER_POST.add_literal("/api/system/permissions/request")  # Lane 11 R-PROD-004b — trigger native prompt
# v2026.6.7 — operator report 2026-06-07: the iOS HealthKit adapter
# was POSTing to ``/api/health/ingest`` (declared by the iOS SDK's
# ``BrainHTTP.IngestKind.healthKit``) and getting HTTP 401 because
# the route wasn't on the phone-bearer POST allowlist. The route
# itself is implemented in ``api/routes/dashboard.py`` and bridges
# Apple HealthKit samples into the memory store; allowlist it here
# so the phone bearer is accepted.
_PHONE_BEARER_POST.add_literal("/api/health/ingest")
# Operator approval surface. Path-parameterised on `request_id`; the
# matcher uses Starlette's `compile_path`, the same function FastAPI's
# router uses to dispatch the request — so a match here is by
# construction the same set of paths FastAPI accepts.
_PHONE_BEARER_POST.add_pattern("/api/approvals/{request_id}/approve")
_PHONE_BEARER_POST.add_pattern("/api/approvals/{request_id}/reject")


def _is_webhook_receive(path: str) -> bool:
    """External webhook endpoints (POST /api/webhooks/{app_id}) must be public.
    
    External services cannot know our API key; they authenticate via HMAC signature.
    The LIST endpoint (GET /api/webhooks) remains authenticated.
    """
    if not path.startswith("/api/webhooks/"):
        return False
    tail = path[len("/api/webhooks/"):].strip("/")
    return bool(tail) and "/" not in tail


class APIKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        if path in _OPEN_PATHS:
            return await call_next(request)
        if any(path.startswith(p) for p in _OPEN_PATH_PREFIXES):
            return await call_next(request)
        if _is_webhook_receive(path) and request.method == "POST":
            return await call_next(request)

        if request.method == "GET":
            if path in _OPEN_GET_PATHS:
                return await call_next(request)
            if any(path.startswith(p) for p in _OPEN_GET_PATH_PREFIXES):
                return await call_next(request)

        scope_type = request.scope.get("type", "")
        if scope_type == "websocket":
            return await call_next(request)

        # audit-r12 A1 (v2026.5.38) — secure-by-default.
        # Loopback (127.0.0.1 / ::1 / localhost) ALWAYS bypasses HTTP auth so
        # the local dashboard works out of the box. Off-loopback (LAN /
        # Tailscale / 0.0.0.0) requires the API key or phone bearer; the
        # dev escape hatch is ``FERAL_LOCAL_BYPASS=1``, which emits a loud
        # boot warning via ``warn_if_unsafe_bypass``.
        client_host = request.client.host if request.client else None
        if _session_auth_module.is_localhost(client_host):
            return await call_next(request)
        if _session_auth_module.local_bypass_enabled():
            return await call_next(request)

        auth = request.headers.get("authorization", "")
        if auth == f"Bearer {FERAL_API_KEY}":
            return await call_next(request)

        # v2026.5.26 — phone-bearer HTTP auth. Pre-fix the middleware
        # accepted only the dashboard FERAL_API_KEY, so the iOS app's
        # phone_bearer (minted during pair flow + accepted on the WS
        # handshake) 401'd on every HTTP call.
        #
        # Acceptance is path-allowlisted to read-mostly endpoints the
        # iOS app actually consumes (see `_PHONE_BEARER_GET` and
        # `_PHONE_BEARER_POST` above). Destructive paths still require
        # the dashboard API key. The allowlists are
        # `_PathAllowlist` instances, so parameterised routes like
        # `/api/approvals/{request_id}/approve` match correctly — the
        # pre-r12 literal-only allowlist drifted whenever a route was
        # renamed or parameterised.
        if auth.startswith("Bearer "):
            phone_ok = False
            if request.method in ("GET", "HEAD") and _PHONE_BEARER_GET.matches(path):
                phone_ok = True
            elif request.method == "POST" and _PHONE_BEARER_POST.matches(path):
                phone_ok = True

            if phone_ok:
                bearer = auth[len("Bearer "):].strip()
                try:
                    from api.state import state as _state
                    store = getattr(_state, "device_pairing_store", None)
                    verifier = getattr(store, "verify_phone_bearer", None) if store else None
                    device_id = verifier(bearer) if callable(verifier) else None
                except Exception:
                    device_id = None
                if device_id:
                    # Stash the verified device id on the request so
                    # downstream handlers can use it for per-device
                    # filtering / auditing without re-verifying.
                    try:
                        request.state.phone_device_id = device_id
                    except Exception:
                        pass
                    return await call_next(request)

        return JSONResponse({"error": "Unauthorized — provide Authorization: Bearer <key>"}, status_code=401)


app.add_middleware(APIKeyMiddleware)


# ─────────────────────────────────────────────
# Include Route Modules
# ─────────────────────────────────────────────

app.include_router(dashboard_router)
app.include_router(config_router)
app.include_router(skills_router)
app.include_router(tools_router)
app.include_router(memory_router)
app.include_router(routines_router)
app.include_router(taskflows_router)
app.include_router(llm_router)
app.include_router(audio_router)
app.include_router(genui_router)
app.include_router(mcp_router)
app.include_router(channels_router)
app.include_router(conversations_router)
app.include_router(devices_router)
app.include_router(access_router)
app.include_router(checkpoints_router)

# Optional demo routes — mounted only when feral-demo-data is installed
# AND FERAL_DEV_DEMO=1. Discovery is via the `feral.plugins` entry
# point group; if the plugin isn't installed, /api/demo/* simply
# doesn't exist (no 404 stub, no fake data path).
def _maybe_mount_demo_routes() -> None:
    if os.environ.get("FERAL_DEV_DEMO", "").lower() not in ("1", "true", "yes"):
        return
    try:
        from importlib.metadata import entry_points
    except ImportError:  # py<3.10 fallback
        from importlib_metadata import entry_points  # type: ignore
    try:
        eps = entry_points(group="feral.plugins")
    except TypeError:
        eps = entry_points().get("feral.plugins", [])  # type: ignore
    for ep in eps:
        if ep.name != "demo":
            continue
        try:
            plugin = ep.load()()
            router_factory = plugin.get("status_routes")
            if callable(router_factory):
                demo_router = router_factory()
                if demo_router is not None:
                    app.include_router(demo_router)
                    logger.info("Mounted /api/demo/* routes from feral-demo-data plugin")
        except Exception as exc:  # noqa: BLE001 — demo is best-effort
            logger.warning("Failed to mount feral-demo-data routes: %s", exc)
        break


_maybe_mount_demo_routes()

app.include_router(timeline_router)
app.include_router(brain_rest_router)
app.include_router(baseline_router)
app.include_router(handoff_router)
app.include_router(tool_genesis_router)
app.include_router(agent_mitosis_router)
app.include_router(intents_router)
app.include_router(webhooks_router)
app.include_router(outgoing_webhooks_router)
app.include_router(ambient_router)
app.include_router(auth_router)
app.include_router(personas_router)
app.include_router(jobs_router)
app.include_router(consciousness_router)
app.include_router(about_me_router)
app.include_router(ideas_router)
app.include_router(apps_router)
app.include_router(uploads_router)  # PR 10
app.include_router(supervisor_router)
app.include_router(twin_router)
app.include_router(sessions_router)  # 
app.include_router(capabilities_router)  # Phase 5 — capability registry
app.include_router(system_permissions_router)  # Phase 11 — macOS TCC state
app.include_router(discovery_router)  # Phase 13 — brain identity discovery
app.include_router(approvals_router)
# --- Subagent A (realtime GA) additions ---
app.include_router(realtime_client_secret_router)


# ─────────────────────────────────────────────
# Prometheus-compatible /metrics endpoint
# ─────────────────────────────────────────────

from observability.metrics import (
    in_memory_snapshot as _metrics_snapshot,
    render_prometheus as _render_prometheus,
)


@app.get("/install-phone-bridge.sh")
async def install_phone_bridge_script():
    """Serve the phone-bridge installer over HTTP so the one-liner works:

        curl -fsSL http://brain.local:9090/install-phone-bridge.sh | bash -s -- \
            --token ... --brain-url ws://brain.local:9090/v1/node
    """
    from pathlib import Path as _Path
    from starlette.responses import PlainTextResponse

    here = _Path(__file__).resolve().parent.parent.parent
    candidates = [
        here / "scripts" / "install-phone-bridge.sh",
        _Path(__file__).resolve().parent.parent / "scripts" / "install-phone-bridge.sh",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return PlainTextResponse(candidate.read_text(), media_type="text/x-shellscript")
    return PlainTextResponse(
        "# install-phone-bridge.sh not bundled in this build\n",
        status_code=404,
        media_type="text/plain",
    )


# /metrics ownership notes
# ─────────────────────────
#  (roadmap §3.1 #4) flipped this endpoint from default-OFF to
# default-ON-on-loopback. Two switches gate it:
#
#   FERAL_METRICS_ENDPOINT  — kill switch. Set to "0"/"false" to silence
#                              both the endpoint and every emit() write.
#                              Defaults to "1" (on).
#   FERAL_METRICS_PUBLIC    — exposure switch. Off-loopback callers get
#                              404 unless this is set to "1"/"true".
#                              Defaults to "0".
#
# Off-loopback default is 404 (NOT 401/403) so the response is
# indistinguishable from "endpoint not mounted" — preserving the
#  public-internet behaviour for unconfigured installs.
#
# The body concatenates the  prometheus_client REGISTRY (Grafana /
# alert-rule surface) with the legacy in-memory snapshot lines so
#  ``increment()``/``observe()`` call sites stay scrapeable
# during the cross-module emit() rollout ( follow-up).

_METRICS_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


def _metrics_endpoint_killed() -> bool:
    val = os.getenv("FERAL_METRICS_ENDPOINT", "1").strip().lower()
    return val in ("0", "false", "off", "no")


def _metrics_public_enabled() -> bool:
    val = os.getenv("FERAL_METRICS_PUBLIC", "0").strip().lower()
    return val in ("1", "true", "yes", "on")


@app.get("/metrics")
async def metrics_endpoint(request: Request):
    if _metrics_endpoint_killed():
        return JSONResponse({"error": "Metrics endpoint disabled. Set FERAL_METRICS_ENDPOINT=1"}, status_code=404)
    client_host = request.client.host if request.client else None
    if client_host not in _METRICS_LOOPBACK_HOSTS and not _metrics_public_enabled():
        return JSONResponse({"error": "Not Found"}, status_code=404)

    from starlette.responses import PlainTextResponse
    body, content_type = _render_prometheus()

    # Append legacy in-memory snapshot lines so  increment()/observe()
    # callers remain scrapeable until  migrates them to emit().
    snap = _metrics_snapshot()
    legacy_lines: list[str] = []
    for name, v in snap["counters"].items():
        legacy_lines.append(f"# TYPE {name} counter")
        legacy_lines.append(f"{name} {v}")
    for name, h in snap["histograms"].items():
        legacy_lines.append(f"# TYPE {name} histogram")
        legacy_lines.append(f"{name}_count {h['count']}")
        legacy_lines.append(f"{name}_mean {h['mean']}")
    if legacy_lines:
        body = body + "\n".join(legacy_lines) + "\n"

    return PlainTextResponse(body, media_type=content_type)


# ─────────────────────────────────────────────
# Lifecycle
# ─────────────────────────────────────────────

async def _log_routine_device_action(
    session_id: str,
    skill_id: str,
    endpoint: str,
    skill_args: dict,
    result: dict,
    run_id: int,
) -> None:
    """Persist a cron-fired device action to the episodic timeline.

    The chat/voice paths log via ``Orchestrator._emit_tool_result``;
    the cron skill branch bypasses the orchestrator entirely, so recall
    queries like "what did my cutebot do yesterday?" missed routine-
    driven motion until this hook landed.
    """
    orch = getattr(state, "orchestrator", None)
    if orch is None:
        return
    try:
        tool_call = {
            "name": f"{skill_id}__{endpoint}",
            "args": skill_args or {},
            "id": f"routine-{run_id}",
        }
        fields = orch._device_action_episode_fields(tool_call, result)
        if fields is None:
            return
        summary, detail = fields
        memory = getattr(orch, "memory", None)
        if memory is None or not hasattr(memory, "episode_save"):
            return
        await memory.episode_save(
            session_id=session_id or f"routine-{run_id}",
            event_type="device_action",
            summary=summary,
            detail=detail,
            importance=0.6,
        )
    except Exception:
        logger.debug(
            "routine device-action episode log failed (non-fatal)",
            exc_info=True,
        )


def execute_routine_job(job):
    """Dispatch a fired CronService routine.

    Module-level (rather than a startup closure) so it is unit-testable by
    monkeypatching ``api.server.state``. Resolution order for a routine's
    payload:

      1. ``workflow_id`` — instantiate a workflow pack as a live TaskFlow.
      2. ``flow_id`` + inline ``steps`` — create an ad-hoc TaskFlow.
      3. ``skill`` + ``endpoint`` — direct skill invoke, behind a safety
         pre-flight on surface="cron" (DENY → skip + record).
      4. ``prompt`` / ``action_text`` — run through the orchestrator.
      5. otherwise — log a no-op.
    """
    import asyncio as _aio
    logger.info("Routine fired: id=%s type=%s desc=%s", job.id, job.job_type, job.description)
    run_id = state.cron_service.record_run_start(job.id)
    try:
        payload = job.payload or {}
        skill_id = payload.get("skill")
        endpoint = payload.get("endpoint")
        # NL automations stash the action under "action_text"; treat it as a
        # prompt so those routines stop silently no-op'ing.
        prompt = payload.get("prompt") or payload.get("action_text")
        workflow_id = payload.get("workflow_id")
        flow_id = payload.get("flow_id")
        flow_steps = payload.get("steps")

        # Workflow branch: a routine can launch a workflow pack each time it
        # fires, reusing the exact instantiate semantics the REST route uses.
        if workflow_id:
            from api.routes.personas import instantiate_pack
            try:
                flow = instantiate_pack(
                    workflow_id,
                    session_id=job.session_id or f"routine-{job.id}",
                    context={
                        "instantiated_from": "routine",
                        "routine_id": job.id,
                        "workflow_id": workflow_id,
                    },
                )
                state.cron_service.record_run_finish(
                    run_id, "success", {"flow_id": flow.get("id"), "workflow_id": workflow_id}, None,
                )
            except KeyError:
                state.cron_service.record_run_finish(
                    run_id, "error", {}, f"Unknown workflow pack '{workflow_id}'",
                )
            except Exception as exc:
                state.cron_service.record_run_finish(run_id, "error", {}, str(exc))
            return

        # Ad-hoc multi-step branch: routine carries an inline flow def.
        if flow_id and isinstance(flow_steps, list) and flow_steps and state.taskflows:
            flow = state.taskflows.create_flow(
                session_id=job.session_id or f"routine-{job.id}",
                title=payload.get("title") or job.description or f"routine-{job.id}",
                steps=flow_steps,
                context={"instantiated_from": "routine", "routine_id": job.id},
            )
            state.cron_service.record_run_finish(
                run_id, "success", {"flow_id": flow.get("id")}, None,
            )
            return

        if skill_id and endpoint and state.skill_registry:
            # audit / smart-loops S2 — the cron skill path bypasses the
            # orchestrator's safety resolver entirely. Run an explicit
            # pre-flight on surface="cron" so a DENY verdict skips (and
            # records) the run instead of firing blind.
            skill_args = payload.get("args", {}) or {}
            # A routine the user DELIBERATELY created with auto-confirm is an
            # explicit, pre-authorised action — the cron pre-flight must not
            # silently DENY it (there is no human at fire time to approve).
            # We still skip a DENY for routines that were NOT user-confirmed,
            # so an auto-generated/unsafe routine can't fire blind.
            auto_confirm = bool(payload.get("auto_confirm"))
            try:
                from security.safety_resolver import resolve_policy, LEVEL_DENY
                decision = resolve_policy(
                    f"{skill_id}__{endpoint}",
                    skill_args,
                    surface="cron",
                    registry=state.skill_registry,
                )
            except Exception:
                decision = None
            # Physical-safety denials (e.g. robot wheel speed > limit) are
            # NEVER overridable by auto_confirm — they protect hardware.
            hard_physical_deny = bool(
                decision is not None
                and (decision.sources or {}).get("cutebot_speed_limit")
            )
            deny_overridden = auto_confirm and not hard_physical_deny
            if decision is not None and decision.level == LEVEL_DENY and not deny_overridden:
                state.cron_service.record_run_finish(
                    run_id, "skipped", {"policy": decision.to_dict()},
                    f"denied by safety policy: {decision.deny_reason}",
                )
                return

            skill = state.skill_registry.get_skill(skill_id)
            if skill:
                loop = _aio.new_event_loop()
                session_id = job.session_id or f"routine-{job.id}"

                async def _dispatch_skill():
                    result = await skill.execute(endpoint, skill_args, {})
                    if isinstance(result, dict) and result.get("success"):
                        await _log_routine_device_action(
                            session_id,
                            skill_id,
                            endpoint,
                            skill_args,
                            result,
                            run_id,
                        )
                    return result

                try:
                    result = loop.run_until_complete(_dispatch_skill())
                finally:
                    loop.close()
                state.cron_service.record_run_finish(
                    run_id, "success" if result.get("success") else "error",
                    result, result.get("error"),
                )
                return

        if prompt and state.orchestrator:
            # audit-r14 / S6 — pre-flight against the cron cost cap before
            # invoking the orchestrator. A scheduled routine is the same cost
            # class as a user chat turn, so a paused cap must skip the turn
            # and let the operator see why via the UI banner. CronService runs
            # on a daemon thread so the guard's broadcast is a no-op (no
            # running asyncio loop) — the structured log line is still emitted.
            guard = getattr(state, "cron_cost_guard", None)
            if guard is not None and not guard.allow(
                model="gpt-4o-mini",
                estimated_max_tokens=512,
            ):
                state.cron_service.record_run_finish(
                    run_id,
                    "skipped",
                    {},
                    "cost cap reached; routine deferred",
                )
                return
            session_id = job.session_id or f"routine-{job.id}"
            # Pass an explicit context so the Supervisor audit log can
            # distinguish cron-driven turns from user / web. Without this,
            # source defaulted to "web".
            cron_context = {
                "source": "cron",
                "actor": "system",
                "routine_id": job.id,
                "routine_type": job.job_type,
                # Surfaced so the orchestrator can auto-approve a device action
                # the user explicitly scheduled with auto-confirm (no human at
                # fire time). Honoured by confirmation-gated paths that consult
                # the turn context.
                "auto_confirm": bool(payload.get("auto_confirm")),
            }
            loop = _aio.new_event_loop()
            try:
                loop.run_until_complete(
                    state.orchestrator.handle_command(session_id, prompt, context=cron_context)
                )
            finally:
                loop.close()
            state.cron_service.record_run_finish(run_id, "success", {"prompt": prompt}, None)
            return

        state.cron_service.record_run_finish(
            run_id, "success",
            {"message": "No skill or prompt configured; routine logged."},
            None,
        )
    except Exception as exc:
        logger.exception("Routine execution error for job %s", job.id)
        state.cron_service.record_run_finish(run_id, "error", {}, str(exc))


@app.on_event("startup")
async def startup():
    # audit-r12 A1 — surface FERAL_LOCAL_BYPASS=1 on non-loopback bind
    # as a loud boot warning. The middleware enforces the actual policy
    # (loopback still bypasses by default); the warning makes the trust
    # degradation visible the moment the brain comes up.
    try:
        _bind = brain_bind_host()
    except Exception:
        _bind = ""
    warn_if_unsafe_bypass(_bind)

    await state.init()
    if state.memory:
        state.memory.start_background_tasks()
    if state.cron_service:
        state.cron_service.start(execute_routine_job)

    async def _state_heartbeat():
        """Push dashboard/system state to all WS clients every 10s."""
        while True:
            await asyncio.sleep(10)
            if not state.sessions:
                continue
            try:
                dashboard = await _get_dashboard_data()
                await state.broadcast_event("dashboard_update", dashboard)
            except Exception:
                pass
    state.register_background_task(
        asyncio.create_task(_state_heartbeat(), name="feral-state-heartbeat")
    )

    async def _provider_catalog_refresher():
        """Refresh the ProviderCatalog every 6h while the Brain is up.

        Owned by  (Roadmap §3.5 P0 / Appendix A.1): the daily
        provider-research.yml cron keeps the bundled `model_catalog.json`
        current for fresh clones, but a brain that's been running for
        days would otherwise serve a 24h+ stale model list to the v2
        Settings picker. ProviderCatalog.refresh_async() skips providers
        without a configured key so this is a no-op for adapters the
        user hasn't set up.
        """
        # Initial nudge so Settings sees fresh data shortly after boot
        # without waiting six hours.
        await asyncio.sleep(60)
        while True:
            try:
                if state.provider_catalog is not None:
                    await state.provider_catalog.refresh_async()
            except Exception as exc:
                logger.debug("provider catalog refresh failed: %s", exc)
            await asyncio.sleep(6 * 3600)
    state.register_background_task(
        asyncio.create_task(_provider_catalog_refresher(), name="feral-provider-catalog-refresher")
    )


@app.on_event("shutdown")
async def shutdown_event():
    """Graceful shutdown: stop producers, close LLM, then teardown I/O.

    A7 — Ordering matters. Before this pass, ``llm.close()`` ran first
    while ambient loops (proactive, screen loop, scheduled tasks,
    scene analysis, channel handlers) were still firing HTTP requests
    through the shared client, producing ``Cannot send a request, as
    the client has been closed`` tracebacks. We now:

      1. Stop every background producer (registry + engines + integrations
         + channel manager + embed queue).
      2. THEN close the LLM + MCP so no in-flight request can leak.
      3. Stop taskflows.
      4. Tear down sync/mDNS via the async-safe paths so zeroconf
         doesn't stall the loop (``EventLoopBlocked``).
      5. Snapshot ConsciousnessStore last, while SQLite pools are alive.
    """
    logger.info("FERAL Brain shutting down gracefully...")

    # (a) Cancel every registered background task (heartbeat, catalog
    # refresher, ideas brief, screen loop bootstrap, demo, proactive
    # evaluation loop, etc.). This flips producer state before we
    # touch the shared HTTP client.
    try:
        cancelled = await state.shutdown_background_tasks(timeout=5.0)
        if cancelled:
            logger.info("Shutdown: cancelled %d background task(s)", cancelled)
    except Exception as exc:
        logger.warning("Shutdown: background-task cancellation failed: %s", exc)

    # (a.1) Ask the engines that own their own task handles to stop so
    # they can drain any in-flight tick cleanly. These are idempotent
    # with the registry cancellation above — if the task is already
    # cancelled, stop() becomes a no-op.
    for owner_name in ("proactive", "screen_loop"):
        owner = getattr(state, owner_name, None)
        if owner is None:
            continue
        stop = getattr(owner, "stop", None)
        if not callable(stop):
            continue
        try:
            result = stop()
            if hasattr(result, "__await__"):
                await result
        except Exception as exc:
            logger.debug("Shutdown: %s.stop() raised: %s", owner_name, exc)

    # audit-r14 finding 18 #1 — CronService runs on a daemon thread that
    # the BrainState registry never owned, so the pre-fix shutdown left
    # the cron loop polling the SQLite job DB until process death. Call
    # stop() here so the join (35s timeout) drains the thread before
    # the LLM client closes; without this, a routine that fires during
    # shutdown raced ``llm.close()`` and produced
    # "Cannot send a request, as the client has been closed"
    # tracebacks in the operator log.
    if getattr(state, "cron_service", None) is not None:
        try:
            state.cron_service.stop()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Shutdown: cron_service.stop() raised: %s", exc)

    # (a.2) Messaging + channel integrations that spawn their own
    # polling loops.
    for bridge_name in ("channel_manager", "mqtt_bridge", "email_watcher"):
        bridge = getattr(state, bridge_name, None)
        if bridge is None:
            continue
        stop = getattr(bridge, "stop_all", None) or getattr(bridge, "stop", None)
        if not callable(stop):
            continue
        try:
            result = stop()
            if hasattr(result, "__await__"):
                await result
        except Exception as exc:
            logger.warning("Shutdown: %s stop failed: %s", bridge_name, exc)

    # (a.3) Close the MemoryStore so the embed queue's background
    # coroutine stops before the event loop starts tearing down.
    try:
        if state.memory is not None:
            close = getattr(state.memory, "close", None)
            if callable(close):
                close()
    except Exception as exc:
        logger.debug("Shutdown: memory.close() raised: %s", exc)

    # (b) LLM client — safe now that every producer is stopped.
    if state.orchestrator and state.orchestrator.llm:
        try:
            await state.orchestrator.llm.close()
        except Exception as exc:
            logger.debug("Shutdown: llm.close() raised: %s", exc)

    # (c) MCP connections.
    if state.mcp_client:
        try:
            await state.mcp_client.disconnect_all()
        except Exception as exc:
            logger.debug("Shutdown: mcp disconnect_all raised: %s", exc)

    # (d) Taskflows. These may call back into skills/LLM; we keep them
    # after LLM close because TaskFlowRuntime.stop() is expected to
    # cancel outstanding runs rather than start new ones.
    if state.taskflows:
        try:
            await state.taskflows.stop()
        except Exception as exc:
            logger.debug("Shutdown: taskflows.stop raised: %s", exc)

    # (e) Sync engine mDNS teardown (async-safe — see memory/sync.py).
    if state.sync_engine:
        try:
            await state.sync_engine.stop_discovery()
        except Exception as exc:
            logger.warning("Shutdown: sync_engine.stop_discovery failed: %s", exc)

    # (e.1) v2026.5.34 PR 2 D11/D12: stop the new memory-v2 services.
    # Done before the memory store's connection pool dies so in-flight
    # sweeps / per-peer sync attempts can release their connections
    # cleanly. SyncScheduler stops first because its tasks own
    # MemoryStore.refresh() awaits that need the pool to be alive.
    scheduler = getattr(state, "sync_scheduler", None)
    if scheduler is not None:
        try:
            await scheduler.stop()
        except Exception as exc:
            logger.warning("Shutdown: sync_scheduler.stop failed: %s", exc)
    decay = getattr(state, "memory_decay", None)
    if decay is not None:
        try:
            await decay.stop()
        except Exception as exc:
            logger.warning("Shutdown: memory_decay.stop failed: %s", exc)

    # (f) Persist consciousness before the SQLite connection pools die.
    try:
        store = getattr(state, "consciousness", None)
        if store is not None:
            from memory.consciousness import default_snapshot_path
            import json as _json
            blob = store.snapshot()
            path = default_snapshot_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_json.dumps(blob, indent=2))
            logger.info(
                "Consciousness snapshot written: %d entities -> %s",
                blob.get("count", 0), path,
            )
    except Exception as exc:
        logger.warning("Consciousness snapshot-on-shutdown failed: %s", exc)
    try:
        from services.mdns import stop_advertisement
        stop_advertisement()
    except Exception:
        pass
    logger.info("Shutdown complete.")


# ─────────────────────────────────────────────
# Lane 08 WS7 — shared chat-turn dispatch (WebUI + HUP parity)
# ─────────────────────────────────────────────


async def _prepare_chat_turn_context(
    *,
    session_id: str,
    text: str,
    raw_context: dict | None,
    attachments: list[dict] | None = None,
    source_node: str | None = None,
) -> tuple[str, dict, str]:
    """Build the (refined_text, ctx, user_msg_text) triple that the
    orchestrator should be invoked with.

    Both the WebUI session WS (``/v1/session``) and the HUP node WS
    (``/v1/node`` ``text_command``) call this so they produce
    IDENTICAL orchestrator invocations for the same input — closes
    Lane 08 WS7 phone-chat parity drift.

    Side effects:
      * ``state.memory.working_push`` records the user turn with the
        same shape on both paths (``{"role": "user", "text": ...}``).
      * Runs ``PromptRefiner`` so ``ctx["refinement"]`` is always
        present (Phase 2 audit-r10 contract). On failure the original
        text passes through verbatim.

    Returns:
        ``(refined_text, ctx, user_msg_text)``.
    """
    user_msg_text = text
    ctx: dict = dict(raw_context or {})
    if attachments:
        # Inline a summary into the working-memory transcript so the
        # LLM history visibly carries the attachment list; symmetric
        # with the legacy WebUI path.
        attach_summary = ", ".join(
            f"{a.get('filename') or a.get('upload_id')} "
            f"({a.get('content_type') or 'unknown'}, "
            f"{int(a.get('size_bytes') or 0)} bytes, "
            f"upload_id={a.get('upload_id')})"
            for a in attachments
        )
        user_msg_text = f"{text}\n\n[attached files: {attach_summary}]"
        ctx["attachments"] = attachments
    if source_node:
        ctx["source_node"] = source_node

    try:
        state.memory.working_push(
            session_id, {"role": "user", "text": user_msg_text},
        )
    except Exception:
        logger.debug("working_push (chat prelude) failed", exc_info=True)

    refined_text = text
    try:
        from agents.prompt_refiner import refine as _refine_prompt
        history: list[dict] = []
        try:
            history = state.memory.working_get(session_id) or []
        except Exception:
            history = []
        envelope = await _refine_prompt(
            text,
            llm=getattr(state.orchestrator, "llm", None),
            device_target_hint=ctx.get("device_target"),
            history=history,
        )
        if envelope.refined_text:
            refined_text = envelope.refined_text
        if envelope.device_target and "device_target" not in ctx:
            ctx["device_target"] = envelope.device_target
        ctx["refinement"] = envelope.model_dump()
    except Exception as exc:
        logger.debug("PromptRefiner skipped: %s", exc)

    return refined_text, ctx, user_msg_text


def _build_chat_turn_runner(
    *,
    ws: WebSocket,
    session_id: str,
    refined_text: str,
    ctx: dict,
) -> "Awaitable[None]":
    """Construct the coroutine that drives ``handle_command_stream``
    plus the optional skill-gen detection. Identical between WebUI
    and HUP so the parity test diffs the same execution path.
    """

    async def _run() -> None:
        try:
            await state.orchestrator.handle_command_stream(
                session_id=session_id,
                text=refined_text,
                context=ctx,
            )
            if state.skill_gen:
                history = state.memory.working_get(session_id) or []
                need = await state.skill_gen.detect_unmet_need(history)
                if need:
                    manifest = await state.skill_gen.generate_skill(
                        capability=need.get("capability", ""),
                        service=need.get("service", ""),
                    )
                    if manifest:
                        await ws.send_json(FeralMessage(
                            session_id=session_id,
                            hop="brain",
                            type="skill_proposal",
                            payload={
                                "manifest": manifest,
                                "reason": need.get("capability", ""),
                            },
                        ).model_dump())
        except asyncio.CancelledError:
            raise
        except Exception as turn_err:
            logger.error(
                "background chat turn failed for %s: %s",
                session_id[:8] if len(session_id) >= 8 else session_id,
                turn_err,
                exc_info=True,
            )
            try:
                await ws.send_json(FeralMessage(
                    session_id=session_id,
                    hop="brain",
                    type="text_response",
                    payload=TextResponsePayload(
                        text=f"Sorry, something went wrong: {turn_err}",
                    ).model_dump(),
                ).model_dump())
            except Exception:
                pass

    return _run()


# ─────────────────────────────────────────────
# Main Client WebSocket
# ─────────────────────────────────────────────

@app.websocket("/v1/session")
async def client_session(ws: WebSocket, token: str = Query(default=None)):
    await ws.accept()

    client_host = ws.client.host if ws.client else None
    _ws_authed = False

    # audit-r12 A1 (v2026.5.38) — loopback always bypasses; off-loopback
    # requires a token, with ``FERAL_LOCAL_BYPASS=1`` as the dev opt-in.
    if is_localhost(client_host):
        _ws_authed = True
    elif local_bypass_enabled():
        _ws_authed = True
    elif token and (verify_session(token) or token == FERAL_API_KEY):
        _ws_authed = True

    if not _ws_authed:
        try:
            first_msg = await asyncio.wait_for(ws.receive_json(), timeout=5)
            if first_msg.get("type") == "auth":
                t = first_msg.get("token", "")
                if verify_session(t) or t == FERAL_API_KEY:
                    _ws_authed = True
        except Exception:
            pass

    if not _ws_authed:
        await ws.close(code=4001, reason="Unauthorized")
        return

    # Audit-r9 fix — operator: "the chat and memory should be the same
    # for my phone chat and the webui for feral brain". The web socket
    # used to mint a fresh `uuid4()` per connection, which split
    # `Orchestrator.conversation_history[session_id]` per WebSocket
    # AND per surface (phone path uses `chat_request` with its own id
    # at line 1486). Default to the per-install `primary_session_id`
    # so a single-user brain shares one conversation thread + working
    # memory across web tabs AND iOS chat. Multi-thread / "new chat"
    # is now an explicit client opt-in (pass `?session_id=...` on the
    # WebSocket query string).
    requested_sid = ws.query_params.get("session_id", "").strip() if hasattr(ws, "query_params") else ""
    session_id = requested_sid or getattr(state, "primary_session_id", "") or str(uuid4())
    state.sessions[session_id] = ws
    # Phase 3 (audit-r10) — refcount this attachment so concurrent
    # tabs sharing the primary session each register; per-session
    # cleanup only fires when the last surface detaches AND the
    # session isn't the persistent primary.
    try:
        state.attach_session(session_id)
    except Exception:  # best-effort — never block accept on bookkeeping
        pass
    logger.info(f"Client connected: {session_id}")

    gw_session = GatewaySession(session_id, ws, state.gateway_registry)

    # Lane 08 WS9 — track in-flight orchestrator tasks per WS so the
    # message loop doesn't block on long-running turns (AUDIT-r13
    # finding 6.2). When the WS disconnects we cancel everything so
    # background turns don't keep writing into a dead session.
    chat_tasks: set[asyncio.Task] = set()

    def _spawn_chat_task(coro: "Awaitable[None]") -> asyncio.Task:
        task = asyncio.create_task(coro)
        chat_tasks.add(task)

        def _on_done(t: asyncio.Task) -> None:
            chat_tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                logger.warning(
                    "background chat turn failed for session %s: %s",
                    session_id[:8] if len(session_id) >= 8 else session_id,
                    exc,
                )

        task.add_done_callback(_on_done)
        return task

    for node_id in state.daemons:
        state.bind_session_to_daemon(session_id, node_id)
        state.perception.update_connected_nodes(session_id, list(state.daemons.keys()))

    # Greeting policy (RC fix for chat thread switching): only greet on
    # the DEFAULT connection (no explicit ``?session_id=``). The WebUI
    # reconnects this socket with an explicit session id every time the
    # user switches to a non-primary thread; emitting a greeting on each
    # of those reconnects injected a stray "How can I help?" bubble into
    # the thread. Explicit-session connects skip the greeting entirely.
    greeting = _build_greeting() if not requested_sid else ""

    if greeting:
        await ws.send_json(FeralMessage(
            session_id=session_id,
            hop="brain",
            type="text_response",
            payload=TextResponsePayload(
                text=greeting
            ).model_dump(),
        ).model_dump())
        state.memory.working_push(session_id, {"role": "assistant", "content": greeting})

    try:
        while True:
            try:
                raw = await ws.receive_json()
            except (ValueError, TypeError) as e:
                logger.warning("Malformed message from session %s: %s", session_id[:8], e)
                await state.send_to_session(session_id, FeralMessage(
                    type="error", payload={"text": "Invalid message format. Please send valid JSON."}
                ))
                continue
            raw["session_id"] = session_id

            msg_type = raw.get("type", "")
            if msg_type in ("req", "res", "event"):
                await gw_session.handle_message(raw)
                continue

            try:
                msg, payload = parse_message(raw)

                if msg.type == "text_command" and isinstance(payload, TextCommandPayload):
                    # Lane 08 WS7 + WS9 — single shared helper for
                    # WebUI + HUP so the response shape never drifts.
                    attachments: list[dict] = []
                    if payload.attachments:
                        attachments = [
                            a.model_dump() if hasattr(a, "model_dump") else dict(a)
                            for a in payload.attachments
                        ]

                    refined_text_web, ctx, _ = await _prepare_chat_turn_context(
                        session_id=session_id,
                        text=payload.text,
                        raw_context=payload.context,
                        attachments=attachments,
                    )
                    _spawn_chat_task(
                        _build_chat_turn_runner(
                            ws=ws,
                            session_id=session_id,
                            refined_text=refined_text_web,
                            ctx=ctx,
                        )
                    )

                elif msg.type == "voice_config":
                    vcfg = raw.get("payload", {})
                    mode = vcfg.get("mode", "realtime")
                    provider = vcfg.get("provider", "openai")
                    if state.voice_router:
                        state.voice_router.set_session_voice_mode(session_id, mode)
                        if mode == "disabled":
                            await state.voice_router.stop_session_voice(session_id)

                    if provider == "gemini" and mode == "realtime" and state.gemini_proxy:
                        system_prompt = ""
                        if state.identity_workspace:
                            try:
                                frame = state.perception.get_frame(session_id) if getattr(state, "perception", None) else None
                            except Exception:
                                frame = None
                            system_prompt = state.identity_workspace.build_system_prompt(
                                frame=frame,
                                skill_registry=getattr(state, "skills", None),
                            )

                        async def _gemini_audio_cb(sid, b64, is_done):
                            try:
                                await ws.send_json(FeralMessage(
                                    session_id=sid,
                                    hop="brain",
                                    type="audio_response",
                                    payload={
                                        "data_b64": b64,
                                        "encoding": "pcm16",
                                        "sample_rate": 24000,
                                        "is_final": is_done,
                                    },
                                ).model_dump())
                            except Exception:
                                pass

                        async def _gemini_transcript_cb(sid, text, is_partial):
                            try:
                                await ws.send_json(FeralMessage(
                                    session_id=sid,
                                    hop="brain",
                                    type="transcript",
                                    payload={"text": text, "role": "assistant", "is_partial": is_partial},
                                ).model_dump())
                            except Exception:
                                pass

                        await state.gemini_proxy.start_session(
                            session_id=session_id,
                            node_id="web",
                            system_prompt=system_prompt,
                            on_audio_delta=_gemini_audio_cb,
                            on_transcript=_gemini_transcript_cb,
                        )

                    await ws.send_json(FeralMessage(
                        session_id=session_id,
                        hop="brain",
                        type="voice_config_ack",
                        payload={"mode": mode, "provider": provider, "status": "ok"},
                    ).model_dump())
                    logger.info(f"Web client voice mode: {mode} (provider: {provider})")

                elif msg.type == "audio_chunk" and isinstance(payload, AudioChunkPayload):
                    if state.gemini_proxy and state.gemini_proxy.has_session(session_id):
                        await state.gemini_proxy.relay_audio(session_id, payload.data_b64)
                    elif state.voice_router:
                        await state.voice_router.handle_audio_from_client(
                            session_id=session_id,
                            audio_b64=payload.data_b64,
                            chunk_index=payload.chunk_index,
                            is_final=payload.is_final,
                            encoding=payload.encoding or "pcm16",
                            sample_rate=payload.sample_rate or 24000,
                        )

                elif msg.type == "ui_event" and isinstance(payload, UIEventPayload):
                    await state.orchestrator.handle_ui_event(
                        session_id=session_id,
                        action_id=payload.action_id,
                        event=payload.event,
                        value=payload.value,
                        app_id=payload.app_id,
                        screen_id=payload.screen_id,
                    )

                elif msg.type == "device_register" and isinstance(payload, DeviceRegisterPayload):
                    state.devices[payload.device_id] = payload.model_dump()
                    logger.info(f"Device registered: {payload.device_id} ({payload.device_type})")

                elif msg.type == "vision_query":
                    payload_dict = raw.get("payload", {})
                    query_text = payload_dict.get("query", "What do you see?")
                    target_node = payload_dict.get("node_id", "")
                    if not target_node:
                        nodes = state.vision_buffer.node_ids_with_frames()
                        target_node = nodes[0] if nodes else "default"
                    state.change_detector.force_trigger(target_node, "user_request")
                    latest = state.vision_buffer.latest(target_node)
                    if latest and state.scene and state.scene.available:
                        asyncio.ensure_future(
                            _analyze_scene_background(target_node, latest, mode="query", query=query_text)
                        )

                elif msg.type == "vision_frame":
                    frame_payload = raw.get("payload", {})
                    frame_b64_len = len(frame_payload.get("data_b64", ""))
                    if frame_b64_len > VISION_MAX_FRAME_KB * 1024:
                        logger.warning(f"Rejecting oversized frame from webclient {session_id[:8]}: {frame_b64_len}B")
                    else:
                        virtual_node = f"webclient_{session_id[:8]}"
                        state.vision_buffer.push(virtual_node, frame_payload)
                        state.perception.update_vision(session_id, state.vision_buffer, virtual_node)
                        state.bind_session_to_daemon(session_id, virtual_node)

                        data_b64 = frame_payload.get("data_b64", "")
                        change_event = state.change_detector.should_analyze(
                            virtual_node,
                            data_b64,
                            frame_payload.get("encoding", "jpeg"),
                        )
                        if change_event and state.scene and state.scene.available:
                            mode = "tracking" if change_event.trigger_reason == "scene_change" else "general"
                            asyncio.ensure_future(
                                _analyze_scene_background(virtual_node, frame_payload, mode=mode)
                            )

                elif msg.type == "biometric":
                    bio = raw.get("payload", {})
                    if state.orchestrator:
                        state.orchestrator.update_biometric(session_id, bio)
                        await state.orchestrator._emit_brain_event(session_id, "device_telemetry", {"source": "client"})
                    state.perception.update_sensors(session_id, bio)
                    if state.somatic_engine:
                        state.somatic_engine.update_from_perception_frame(session_id, bio)
                    _record_biometrics_to_baseline(bio)

            except Exception as msg_err:
                logger.error(f"Error processing message from {session_id[:8]}: {msg_err}", exc_info=True)
                try:
                    await ws.send_json(FeralMessage(
                        session_id=session_id, hop="brain", type="text_response",
                        payload=TextResponsePayload(text=f"Sorry, something went wrong: {msg_err}").model_dump(),
                    ).model_dump())
                except Exception:
                    pass

    except WebSocketDisconnect:
        logger.info(f"Client disconnected: {session_id}")
        # Lane 08 WS9 — let in-flight orchestrator turns drain
        # briefly before we cancel them. Disconnect is usually a
        # tab close: the user expects the turn that they sent
        # right before closing to still write to memory + finish.
        # We give it 2 seconds, then force-cancel so the brain
        # doesn't leak work onto a dead session.
        if chat_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*list(chat_tasks), return_exceptions=True),
                    timeout=2.0,
                )
            except asyncio.TimeoutError:
                for _task in list(chat_tasks):
                    if not _task.done():
                        _task.cancel()
        chat_tasks.clear()
        # Phase 3 (audit-r10) — decrement refcount; only run cleanup
        # when the last surface for this session_id has detached AND
        # the session is not the persistent `primary_session_id`. This
        # is the residual fix for operator complaint #15 ("app can't
        # fetch stuff I did on the local brain chat"): web tab close
        # used to wipe the shared primary thread in RAM even with the
        # iOS surface still attached. Now the primary thread is
        # protected at the lifecycle layer AND persisted to disk via
        # the snapshot store.
        try:
            remaining_attachments = state.detach_session(session_id)
        except Exception:
            remaining_attachments = 0
        # Voice teardown BEFORE the session de-registration below.
        # Pre-fix no disconnect handler touched the voice router at
        # all: a tab close / network blip / backgrounded app left the
        # OpenAI Realtime WebSocket open and billing, and left a stale
        # `_node_to_session` entry so the NEXT voice_session_start
        # handed the user a dead handle.
        #
        # Identity-checked on purpose: when the same session_id has
        # already reconnected on a NEW socket, `state.sessions` points
        # at that newer ws and the voice session belongs to it. Tearing
        # down here would kill the live call the reconnect just
        # established. Only the socket still registered for this
        # session may stop its voice.
        if state.voice_router and state.sessions.get(session_id) is ws:
            try:
                await state.voice_router.stop_session_voice(session_id)
            except Exception as voice_exc:
                logger.warning(f"Voice teardown on disconnect failed: {voice_exc}")
        should_clear = state.should_clear_on_disconnect(session_id)
        if remaining_attachments == 0 and should_clear:
            if state.orchestrator:
                try:
                    await state.orchestrator.on_session_disconnect(session_id)
                except Exception as e:
                    logger.warning(f"Session summarization failed: {e}")
            if state.identity_workspace:
                try:
                    _llm = state.orchestrator.llm if state.orchestrator else None
                    await state.identity_workspace.maintenance_cycle(
                        memory_store=state.memory,
                        llm=_llm,
                        session_id=session_id,
                    )
                except Exception as e:
                    logger.debug(f"Identity maintenance skipped: {e}")
        # Identity-checked de-registration. ``state.sessions`` holds ONE
        # WebSocket per session_id, and every web tab that connects
        # without ``?session_id=`` resolves to ``primary_session_id`` —
        # so a second surface (another browser tab, the iOS app)
        # overwrites the slot at line 1422 on connect. Popping
        # unconditionally here meant the *older* handler's disconnect
        # de-registered the *newer*, still-live socket. After that
        # ``BrainState.send_to_session`` misses the key and silently
        # returns, so every stream_delta / text_response for the turn
        # goes nowhere while the client socket stays open and healthy:
        # the composer spins on "thinking" forever with no error, no
        # toast, and no reconnect. Operator report 2026-07 ("say hi,
        # switch to another tab, it stops replying").
        #
        # Only tear down the shared per-session state when the socket in
        # the slot is still ours; if a newer surface owns it, that
        # surface owns its audio and perception buffers too.
        if state.sessions.get(session_id) is ws:
            state.sessions.pop(session_id, None)
            state.audio.clear_session(session_id)
            # Perception buffers are surface-local (one fusion frame per
            # active socket); clear so a stale frame from a closed tab
            # doesn't leak into the next session.
            state.perception.clear(session_id)
        # Working memory + orchestrator history persist while ANY
        # surface remains AND for the primary session always. The
        # snapshot store handles cold-boot durability.
        if remaining_attachments == 0 and should_clear:
            state.memory.working_clear(session_id)
        elif remaining_attachments == 0 and session_id == state.primary_session_id:
            # Force a snapshot on the way out so a brain restart
            # immediately after losing all surfaces still has the
            # latest turn on disk.
            try:
                state.snapshot_primary_thread(force=True)
            except Exception as snap_exc:
                logger.debug(f"Primary snapshot on disconnect failed: {snap_exc}")
    except Exception as exc:
        logger.error(f"Unexpected error in session {session_id[:8]}: {exc}", exc_info=True)
        # Same identity check as the WebSocketDisconnect path above, for the
        # same reason. This sibling handler was missed when that one was
        # fixed, so the original bug survived here in a narrower window: an
        # exception raised inside the OLDER socket's own cleanup lands in
        # this block and de-registers the NEWER, still-live surface, after
        # which send_to_session silently drops every reply to it.
        #
        # It was additionally worse than the disconnect path: working memory
        # was cleared unconditionally, where that path gates the same call
        # behind ``remaining_attachments == 0 and should_clear``. On the
        # shared primary session that wiped state out from under every other
        # attached surface. Mirror the guard rather than re-deriving it.
        if state.sessions.get(session_id) is ws:
            state.sessions.pop(session_id, None)
            state.audio.clear_session(session_id)
            state.perception.clear(session_id)
            try:
                remaining = state.detach_session(session_id)
            except Exception:
                logger.debug("detach_session failed on the error path", exc_info=True)
                remaining = 0
            if remaining == 0 and state.should_clear_on_disconnect(session_id):
                state.memory.working_clear(session_id)


# ─────────────────────────────────────────────
# Daemon WebSocket (HUP nodes)
# ─────────────────────────────────────────────

NODE_API_KEY = os.environ.get("NODE_API_KEY", "")


async def _send_protocol_error(ws: WebSocket, code: int, message: str, *, name: str = "bad_schema") -> None:
    """Emit an HUP §8 error frame to the daemon."""
    try:
        await ws.send_json({
            "hup_version": HUP_VERSION,
            "type": "error",
            "ts": __import__("time").time(),
            "payload": {
                "code": code,
                "name": name,
                "message": message,
                "recoverable": False,
                "ref_action_id": None,
            },
        })
    except Exception:
        pass


def _extract_protocol_bearer(protocols_header: str) -> str:
    """Return ``feral-token-...`` bearer from Sec-WebSocket-Protocol."""
    for candidate in (protocols_header or "").split(","):
        value = candidate.strip()
        if value.startswith("feral-token-"):
            return value.replace("feral-token-", "", 1).strip()
    return ""


def _verify_credential(store: DevicePairingStore, credential: str):
    """Try pair token first, then phone bearer."""
    if not store or not credential:
        return None, None
    pair_device_id = store.verify_device(credential)
    if pair_device_id:
        return pair_device_id, "pair_token"
    verify_phone_bearer = getattr(store, "verify_phone_bearer", None)
    if callable(verify_phone_bearer):
        phone_device_id = verify_phone_bearer(credential)
        if phone_device_id:
            return phone_device_id, "phone_bearer"
    return None, None


@app.websocket("/v1/node")
async def daemon_session(ws: WebSocket, api_key: str = Query(default=None)):
    credential_source = ""
    credential = ""

    auth_header = ws.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        credential = auth_header[7:].strip()
        credential_source = "authorization"
    if not credential:
        credential = (ws.headers.get("x-api-key", "") or "").strip()
        if credential:
            credential_source = "x-api-key"
    if not credential:
        credential = _extract_protocol_bearer(
            ws.headers.get("sec-websocket-protocol", "")
        )
        if credential:
            credential_source = "sec-websocket-protocol"
    if not credential:
        credential = (api_key or "").strip()
        if credential:
            credential_source = "query"

    store = state.device_pairing_store
    paired_device_id, bearer_kind = _verify_credential(store, credential)

    await ws.accept()

    if credential_source == "query" and credential:
        logger.warning(
            "feral.security.deprecated_query_auth: source=query path=/v1/node "
            "sunset=2026.7.0"
        )

    # An unset NODE_API_KEY used to mean "allow everyone": the gate was a
    # single `credential != NODE_API_KEY`, so a node that presented no
    # credential at all compared `"" != ""` — False — and was admitted with
    # full capabilities. An auditor registered a node called `attacker-node`
    # this way and got back a complete `node_ack`; from there a node can
    # inject `text_command` (LLM spend), `telemetry`/`device_event` (poisons
    # baselines and health answers), and `device_announce` (writes the
    # knowledge graph). Unconfigured must mean CLOSED, never open, so the
    # empty-key case is refused explicitly BEFORE any comparison can make an
    # absent credential look valid. Pairing tokens and phone bearers
    # (`_verify_credential`) are checked first and are unaffected, so
    # legitimately paired devices keep connecting with no key configured.
    if paired_device_id is None:
        if not NODE_API_KEY:
            logger.error(
                "feral.security.node_api_key_unset: refused an unpaired "
                "/v1/node connection because NODE_API_KEY is not configured. "
                "An empty key NEVER grants access. Fix: set NODE_API_KEY in "
                "the brain's environment (or security.node_api_key in "
                "config.yaml) and give every daemon the same value, or pair "
                "the device so it presents a pairing token instead."
            )
            await ws.close(
                code=4003,
                reason="Edge Node API Key not configured on brain",
            )
            return
        # Constant-time compare: the credential is attacker-supplied and a
        # naive `!=` leaks key length/prefix through response timing.
        if not secrets.compare_digest(credential, NODE_API_KEY):
            logger.warning("Unauthorized daemon connection attempt rejected")
            await ws.close(code=4003, reason="Unauthorized Edge Node API Key")
            return
    node_id = None
    logger.info(
        "Daemon connecting (device_id=%s bearer_kind=%s auth_source=%s)...",
        paired_device_id or "legacy-key",
        bearer_kind or ("legacy_node_api_key" if credential == NODE_API_KEY else "unknown"),
        credential_source or "none",
    )

    def _record_phone_envelope(
        decision: str,
        message_type: str,
        *,
        detail: dict | None = None,
        payload_for_hash=None,
    ) -> None:
        supervisor = getattr(state, "supervisor", None)
        if supervisor is None:
            return
        info = {"message_type": message_type}
        if isinstance(detail, dict):
            info.update(detail)
        try:
            supervisor.record(
                source="phone",
                kind="phone_envelope",
                session_id=str(node_id or paired_device_id or ""),
                actor="phone",
                payload=payload_for_hash if payload_for_hash is not None else {"type": message_type},
                decision=decision,
                detail=info,
            )
        except Exception as exc:
            logger.debug("phone_envelope supervisor record failed: %s", exc)

    try:
        while True:
            try:
                raw = await ws.receive_json()
            except (ValueError, KeyError):
                await _send_protocol_error(ws, 1002, "Malformed JSON frame")
                continue
            except WebSocketDisconnect:
                # Graceful disconnect from the daemon side. Re-raise so the
                # outer `except WebSocketDisconnect` block runs the daemon
                # cleanup (state.daemons.pop, skill_executor.unregister_daemon,
                # hardware_mesh.on_node_disconnected, perception updates).
                # Returning here would leak `state.daemons[node_id]` and
                # break test_accepts_legacy_node_api_key_and_registers.
                logger.info(
                    "daemon_session: peer disconnected (device_id=%s node_id=%s)",
                    paired_device_id, node_id,
                )
                raise
            except RuntimeError as exc:
                # Starlette raises RuntimeError("WebSocket is not connected ...")
                # when the underlying socket has dropped between accept()
                # and the next receive — typically because the iOS client
                # got a TLS / ATS denial or the peer closed without the
                # 1000 close-frame. Treat as a graceful disconnect AND run
                # the same teardown by raising WebSocketDisconnect so the
                # outer handler does the cleanup.
                logger.info(
                    "daemon_session: peer transport gone (device_id=%s node_id=%s) — %s",
                    paired_device_id, node_id, exc,
                )
                raise WebSocketDisconnect(code=1006) from exc
            try:
                msg, payload = parse_message(raw)
            except Exception as exc:  # noqa: BLE001 — pydantic ValidationError + others
                # A typed-payload mismatch (e.g. an unknown node_type Literal,
                # missing required field) used to bubble out of parse_message
                # → out of daemon_session → silent WS close, leaving the
                # phone with "connecting…" forever. Now we surface a real
                # HUP §8 error frame and keep the loop alive so the daemon
                # sees what's wrong.
                logger.warning(
                    "daemon_session: malformed payload from device_id=%s: %s",
                    paired_device_id, exc,
                )
                await _send_protocol_error(
                    ws, 1003,
                    f"payload validation failed: {exc.__class__.__name__}: {exc}",
                    name="bad_payload",
                )
                continue

            if msg.type in ("node_register", "register") and isinstance(payload, NodeRegisterPayload):
                node_id = payload.node_id
                state.daemons[node_id] = ws
                # Stash the HUP-declared node_type on the WebSocket so
                # /api/devices/connected can report the real type instead
                # of the legacy "phone"-for-everyone default. `manufacturer`
                # and `model` are HUP v1 fields that the narrower
                # models.protocol.NodeRegisterPayload doesn't yet mirror —
                # getattr falls back to "" when absent, so we pick them up
                # from v1.1+ daemons without tripping on v1.0 payloads.
                setattr(ws, "_feral_node_type", (getattr(payload, "node_type", None) or "unknown").lower())
                setattr(ws, "_feral_capabilities", list(getattr(payload, "capabilities", []) or []))
                setattr(ws, "_feral_platform", getattr(payload, "platform", "") or "")
                setattr(ws, "_feral_manufacturer", getattr(payload, "manufacturer", "") or "")
                setattr(ws, "_feral_model", getattr(payload, "model", "") or "")
                if state.skill_executor:
                    state.skill_executor.register_daemon_type(node_id, payload.node_type)
                # Phase 5 (audit-r10 overhaul) — record the structured
                # skill manifests this node publishes so
                # `GET /api/capabilities` and the orchestrator's
                # capability-aware routing know which `phone.*` /
                # `glasses.*` action names are live right now.
                # `payload.skills` is the Phase 4 wire field; legacy
                # nodes (v2026.5.x and earlier) don't set it and the
                # registry happily records an empty list.
                state.capability_registry.register_node(
                    node_id,
                    node_type=payload.node_type,
                    platform=payload.platform,
                    skills=getattr(payload, "skills", []) or [],
                )
                logger.info(f"Node registered: {node_id} ({payload.node_type}/{payload.platform}) — caps: {payload.capabilities}, skills: {len(getattr(payload, 'skills', []) or [])}")
                _log_activity("device_connected", f"{node_id} ({payload.node_type})")

                for sid in state.sessions:
                    state.bind_session_to_daemon(sid, node_id)
                    state.perception.update_connected_nodes(sid, list(state.daemons.keys()))

                if state.hardware_mesh:
                    await state.hardware_mesh.on_node_connected(node_id, {
                        "node_type": payload.node_type,
                        "platform": payload.platform,
                        "capabilities": payload.capabilities,
                    })

                session_token = str(__import__("uuid").uuid4())
                await ws.send_json({
                    "hup_version": HUP_VERSION,
                    "type": "node_ack",
                    "ts": __import__("time").time(),
                    "payload": {
                        "node_id": node_id,
                        "session_token": session_token,
                        "hup_version": HUP_VERSION,
                        "heartbeat_ms": 10000,
                        "server_time": __import__("time").time(),
                        "capabilities": list(payload.capabilities),
                        "granted_capabilities": list(payload.capabilities),
                        "denied_capabilities": [],
                    },
                })

            elif msg.type == "execute_result":
                logger.info(f"Daemon result from {node_id}")
                result_payload = raw.get("payload", {})
                request_id = result_payload.get("request_id", "")
                if state.hardware_mesh and request_id:
                    state.hardware_mesh.resolve_invoke(request_id, result_payload)
                if state.orchestrator:
                    await state.orchestrator.handle_daemon_result(
                        node_id=node_id,
                        result=result_payload,
                        session_id=msg.session_id,
                    )

            elif msg.type == "vision_frame":
                frame_payload = raw.get("payload", {})
                if "data_b64" not in frame_payload and "image_b64" in frame_payload:
                    frame_payload["data_b64"] = frame_payload["image_b64"]
                frame_b64_len = len(frame_payload.get("data_b64", ""))
                if frame_b64_len > VISION_MAX_FRAME_KB * 1024:
                    logger.warning(f"Rejecting oversized frame from {node_id}: {frame_b64_len}B")
                else:
                    effective_node = node_id or frame_payload.get("node_id", "unknown")
                    state.vision_buffer.push(effective_node, frame_payload)

                    for sid in state.get_sessions_for_daemon(effective_node):
                        state.perception.update_vision(sid, state.vision_buffer, effective_node)

                    data_b64 = frame_payload.get("data_b64", "")
                    change_event = state.change_detector.should_analyze(
                        effective_node, data_b64, frame_payload.get("encoding", "jpeg"),
                    )
                    if change_event and state.scene and state.scene.available:
                        mode = "tracking" if change_event.trigger_reason == "scene_change" else "general"
                        asyncio.ensure_future(
                            _analyze_scene_background(effective_node, frame_payload, mode=mode)
                        )

                    if state.orchestrator:
                        state.orchestrator.resolve_pending_frame(msg.msg_id, frame_payload)

            elif msg.type == "vision_query":
                payload_dict = raw.get("payload", {})
                query_text = payload_dict.get("query", "What do you see?")
                target_node = payload_dict.get("node_id", "") or node_id or "default"
                state.change_detector.force_trigger(target_node, "user_request")
                latest = state.vision_buffer.latest(target_node)
                if latest and state.scene and state.scene.available:
                    asyncio.ensure_future(
                        _analyze_scene_background(target_node, latest, mode="query", query=query_text)
                    )

            elif msg.type == "gesture":
                gesture_payload = raw.get("payload", {})
                gesture = gesture_payload.get("gesture", "")
                if gesture and node_id:
                    logger.info(f"Gesture from {node_id}: {gesture}")
                    for sid in state.get_sessions_for_daemon(node_id):
                        state.perception.update_gesture(sid, gesture)
                        if state.orchestrator:
                            await state.orchestrator.handle_command(
                                session_id=sid,
                                text=f"[GESTURE] User performed: {gesture}",
                                context={"source": "gesture", "gesture": gesture, "node": node_id},
                    )

            elif msg.type == "telemetry":
                telemetry_payload = raw.get("payload", {})
                sensors = telemetry_payload.get("sensors", {})

                vitals = sensors.get("vitals", {})
                hr = vitals.get("ppg_heart_rate") or sensors.get("ppg_heart_rate")
                if hr:
                    logger.info(f"Telemetry from {node_id}: {hr} BPM")

                if state.orchestrator:
                    state.orchestrator.update_biometric(node_id, sensors)

                if node_id:
                    for sid in state.get_sessions_for_daemon(node_id):
                        state.perception.update_sensors(sid, sensors)
                        if state.somatic_engine:
                            state.somatic_engine.update_from_perception_frame(sid, sensors)
                        if state.orchestrator:
                            await state.orchestrator._emit_brain_event(sid, "device_telemetry", {"source": node_id, "hr": hr or 0})
                _record_biometrics_to_baseline(sensors)

            elif msg.type == "sensor_telemetry":
                payload_dict = raw.get("payload", {})
                # Defense in depth: pre-v2026.5.43 iOS builds ship the
                # field as ``sensor_type`` (string overload of
                # ``FeralBrainClient.sendSensorData``). The Pydantic
                # alias on ``SensorTelemetryPayload`` coerces that for
                # ``parse_message`` callers; this raw-dict ingest path
                # honours the same legacy key explicitly so HealthKit
                # frames from un-updated phones still flow through
                # update_biometric / sensors_map under the canonical
                # name. Remove once the iOS App Store update has
                # propagated.
                sensor_name = payload_dict.get("sensor") or payload_dict.get("sensor_type", "")
                sensor_data = payload_dict.get("data", {})
                source = payload_dict.get("source", "unknown")
                logger.info(f"Sensor [{sensor_name}] from {node_id} ({source}): {sensor_data}")

                sensors_map = {sensor_name: sensor_data}
                if state.orchestrator:
                    state.orchestrator.update_biometric(node_id, sensors_map)
                if node_id:
                    for sid in state.get_sessions_for_daemon(node_id):
                        state.perception.update_sensors(sid, sensors_map)
                        if state.somatic_engine:
                            state.somatic_engine.update_from_perception_frame(sid, sensors_map)
                        if state.orchestrator:
                            await state.orchestrator._emit_brain_event(sid, "device_telemetry", {"source": node_id, "sensor": sensor_name})

            elif msg.type == "sensor_batch":
                payload_dict = raw.get("payload", {})
                readings = payload_dict.get("readings", {})
                logger.info(f"Sensor batch from {node_id}: {list(readings.keys())}")
                if state.orchestrator:
                    state.orchestrator.update_biometric(node_id, readings)
                if node_id:
                    for sid in state.get_sessions_for_daemon(node_id):
                        state.perception.update_sensors(sid, readings)
                        if state.somatic_engine:
                            state.somatic_engine.update_from_perception_frame(sid, readings)
                        if state.orchestrator:
                            await state.orchestrator._emit_brain_event(sid, "device_telemetry", {"source": node_id, "sensors": list(readings.keys())})
                _record_biometrics_to_baseline(readings)

            elif msg.type == "node_heartbeat":
                if node_id and state.hardware_mesh:
                    state.hardware_mesh.node_health.record_heartbeat(node_id)
                    pending = state.hardware_mesh.ledger.get_pending(node_id)
                    if pending:
                        unacked_ids = [
                            r.envelope.command_id for r in pending
                            if r.state.value == "submitted"
                        ]
                        if unacked_ids:
                            await ws.send_json({
                                "type": "pending_commands",
                                "payload": {"command_ids": unacked_ids},
                            })

            elif msg.type == "hup_action_response":
                result_payload = raw.get("payload", {})
                action_id = result_payload.get("action_id", "") or result_payload.get("request_id", "")
                if state.hardware_mesh and action_id:
                    state.hardware_mesh.resolve_invoke(action_id, result_payload)
                if state.orchestrator:
                    await state.orchestrator.handle_daemon_result(
                        node_id=node_id,
                        result=result_payload,
                        session_id=msg.session_id,
                    )

            elif msg.type == "node_bye":
                logger.info("node_bye from %s: %s", node_id, raw.get("payload", {}).get("reason", ""))
                if node_id:
                    state.daemons.pop(node_id, None)
                    if state.skill_executor:
                        state.skill_executor.unregister_daemon(node_id)
                    if state.hardware_mesh:
                        state.hardware_mesh.on_node_disconnected(node_id)
                    state.capability_registry.unregister_node(node_id)
                await ws.close(code=1000)
                return

            elif msg.type == "glasses_status":
                payload_dict = raw.get("payload", {})
                connected = payload_dict.get("glasses_connected", False)
                battery = payload_dict.get("battery_level", -1)
                model = payload_dict.get("glasses_model", "FERAL")
                logger.info(f"Glasses ({model}) {'connected' if connected else 'disconnected'} via {node_id}, battery={battery}%")
                # Persist into the sub-device truth store so dashboards
                # render a real binding instead of a hardcoded dot.
                await _handle_subdevice_status(ws, node_id, "glasses_status", payload_dict)

            elif msg.type == "voice_config":
                payload_dict = raw.get("payload", {})
                if state.voice_router and node_id:
                    state.voice_router.register_voice_config(node_id, payload_dict)
                    for sid in state.get_sessions_for_daemon(node_id):
                        state.voice_router.bind_node_to_session(node_id, sid)
                    supports_rt = payload_dict.get("supports_realtime", False)
                    logger.info(f"Voice config from {node_id}: realtime={supports_rt}")

            elif msg.type == "chat_request":
                payload_dict = raw.get("payload", {})
                text = payload_dict.get("text", "")
                channel = payload_dict.get("channel", "chat")
                reply_mode = payload_dict.get("reply_mode", "final")
                reply_to = payload_dict.get("reply_to")
                # Audit-r9 fix — share conversation thread with web by
                # default. Resolution order:
                #   1. Explicit `session_id` from the phone payload
                #      (e.g. iOS "new chat" button picks its own id).
                #   2. `state.primary_session_id` — the per-install
                #      shared id used by `/v1/session` too, so phone +
                #      web see the same `conversation_history` +
                #      working memory.
                #   3. Legacy `phone-{node_id}` — only as a final
                #      fallback if the brain hasn't successfully
                #      minted a primary id (filesystem error). Without
                #      this, the operator's "iOS chat has no idea
                #      about web events" bug was caused by the phone
                #      thread being completely partitioned from web's
                #      thread.
                target_sid = (
                    payload_dict.get("session_id", "").strip()
                    or getattr(state, "primary_session_id", "")
                    or f"phone-{node_id or paired_device_id or 'session'}"
                )

                if not text or not state.orchestrator:
                    _record_phone_envelope(
                        "denied",
                        "chat_request",
                        detail={"reason": "missing_text_or_orchestrator"},
                        payload_for_hash=payload_dict,
                    )
                    await ws.send_json({
                        "hup_version": HUP_VERSION,
                        "type": "chat_response",
                        "ts": time.time(),
                        "payload": {
                            "session_id": target_sid,
                            "text": "",
                            "reply_mode": reply_mode,
                            "channel": channel,
                            "reply_to": reply_to,
                        },
                    })
                    continue

                if target_sid not in state.sessions:
                    state.sessions[target_sid] = ws
                if node_id:
                    state.bind_session_to_daemon(target_sid, node_id)
                    if channel == "vision_ask" and getattr(state, "perception", None):
                        # First vision turn can race: phone sends frame first, then
                        # chat_request. The frame may land before this session is bound
                        # to the daemon, so refresh perception here after binding.
                        state.perception.update_vision(target_sid, state.vision_buffer, node_id)

                if state.memory:
                    state.memory.working_push(target_sid, {"role": "user", "text": text})

                # Phase 1 (audit-r10 overhaul plan) — `device_target`
                # tells the brain WHERE the requested action should run.
                # When the iOS client sends "brain" (e.g. "open my Mac
                # browser"), `resolve_surface_from_context` swaps the
                # legacy `phone_surface → http_api` hard-deny for the
                # `brain_host` surface, unblocking the operator's
                # "do X on my Mac" complaint. When "phone" or "glasses"
                # the brain dispatches to `phone_actuator` so the LLM
                # is steered toward `phone.*` skills (Phase 4).
                device_target_raw = payload_dict.get("device_target")
                device_target = (
                    device_target_raw.strip().lower()
                    if isinstance(device_target_raw, str)
                    else None
                ) or None

                # Phase 2 (audit-r10 overhaul plan) — PromptRefiner runs
                # BEFORE the orchestrator so the LLM gets a cleaned,
                # disambiguated rewrite + an inferred device_target
                # when the iOS client didn't set one. Feature-flagged
                # via FERAL_PROMPT_REFINER (default off) so this PR
                # lands the wiring without changing behavior; flip the
                # flag once shadow metrics show it improves routing.
                refined_text = text
                refined_envelope = None
                try:
                    from agents.prompt_refiner import refine as _refine_prompt
                    history = []
                    if state.memory:
                        try:
                            history = state.memory.working_get(target_sid) or []
                        except Exception:
                            history = []
                    refined_envelope = await _refine_prompt(
                        text,
                        llm=getattr(state.orchestrator, "llm", None),
                        device_target_hint=device_target,
                        history=history,
                    )
                    if refined_envelope.refined_text:
                        refined_text = refined_envelope.refined_text
                    if refined_envelope.device_target and not device_target:
                        device_target = refined_envelope.device_target
                except Exception as _refine_exc:
                    logger.debug("PromptRefiner skipped: %s", _refine_exc)

                context = {
                    "source": "phone_surface",
                    "mode": "phone_surface",
                    "channel": channel,
                    "reply_mode": reply_mode,
                    "source_node": node_id or "",
                    "paired_device_id": paired_device_id or "",
                }
                if device_target:
                    context["device_target"] = device_target
                if refined_envelope is not None:
                    context["refinement"] = refined_envelope.model_dump()
                if reply_to:
                    context["reply_to"] = reply_to

                response_text = ""
                # Audit-r11 fix — Bug 1: iOS double assistant bubble.
                # The orchestrator's broadcast ``text_response`` AND
                # the synchronous ``chat_response`` below both reach
                # the phone WS when the phone is the only client on
                # this session. Set a per-session suppression flag for
                # the duration of this turn; ``response_delivery.send_text``
                # consults it and skips the broadcast frame. The
                # ``try/finally`` guarantees we always clear the flag
                # so a desktop client joining the session later still
                # gets ``text_response`` on its OWN turns.
                state.orchestrator._text_response_suppressed[target_sid] = True
                try:
                    if reply_mode == "stream":
                        result = await state.orchestrator.handle_command_stream(
                            session_id=target_sid,
                            text=refined_text,
                            context=context,
                        )
                    else:
                        result = await state.orchestrator.handle_command(
                            session_id=target_sid,
                            text=refined_text,
                            context=context,
                        )
                    if isinstance(result, str):
                        response_text = result
                    elif isinstance(result, dict):
                        response_text = str(result.get("text") or result.get("message") or "")
                    if not response_text and state.memory:
                        history = state.memory.working_get(target_sid) or []
                        for item in reversed(history):
                            if item.get("role") == "assistant" and item.get("text"):
                                response_text = str(item["text"])
                                break
                    _record_phone_envelope(
                        "allowed",
                        "chat_request",
                        detail={
                            "session_id": target_sid,
                            "channel": channel,
                            "reply_mode": reply_mode,
                            "text_len": len(text),
                        },
                        payload_for_hash=payload_dict,
                    )
                    orch_error: str | None = None
                except Exception as exc:
                    orch_error = str(exc)[:500] or exc.__class__.__name__
                    _record_phone_envelope(
                        "error",
                        "chat_request",
                        detail={"reason": "orchestrator_error", "error": orch_error[:200]},
                        payload_for_hash=payload_dict,
                    )
                    response_text = ""
                finally:
                    state.orchestrator._text_response_suppressed.pop(target_sid, None)

                # Phase-1 validation pass (Item 2): the brain emits
                # an explicit HUP `error` frame on the failure branch
                # AND populates `payload.error` on the chat_response
                # so a chat-only client (one that doesn't track the
                # parallel `error` frame) still surfaces the real
                # failure string. Pinned by
                # tests/test_phone_envelopes.py round-trip + the
                # daemon_session regression test.
                if orch_error:
                    await _send_protocol_error(
                        ws,
                        4001,
                        f"Orchestrator failed for chat_request: {orch_error}",
                        name="orchestrator_error",
                    )
                await ws.send_json({
                    "hup_version": HUP_VERSION,
                    "type": "chat_response",
                    "ts": time.time(),
                    "payload": {
                        "session_id": target_sid,
                        "text": response_text,
                        "reply_mode": reply_mode,
                        "channel": channel,
                        "reply_to": reply_to,
                        "error": orch_error,
                    },
                })

            elif msg.type == "chat_response":
                _record_phone_envelope(
                    "denied",
                    "chat_response",
                    detail={"reason": "brain_emitted_only"},
                    payload_for_hash=raw.get("payload", {}),
                )
                await _send_protocol_error(
                    ws,
                    1003,
                    "chat_response is brain->phone only",
                    name="capability_denied",
                )

            elif msg.type == "voice_session_start":
                payload_dict = raw.get("payload", {})
                stream_id = payload_dict.get("stream_id", "")
                if not node_id or not state.voice_router:
                    _record_phone_envelope(
                        "denied",
                        "voice_session_start",
                        detail={"reason": "missing_node_or_voice_router"},
                        payload_for_hash=payload_dict,
                    )
                    continue
                session_id = stream_id or f"voice-{node_id}"
                if session_id not in state.sessions:
                    state.sessions[session_id] = ws
                state.bind_session_to_daemon(session_id, node_id)
                state.voice_router.bind_node_to_session(node_id, session_id)

                # PR #61 (voice-v2) wire-up: dispatch to the user-selected
                # voice mode (openai_realtime / gemini_live / chained) via
                # VoiceRouter.open_session. Phone emits the selected mode
                # in the `voice_mode` payload field; falls back to the
                # operator's configured default when absent.
                selected_mode = (
                    payload_dict.get("voice_mode")
                    or payload_dict.get("provider_mode")
                )
                if not selected_mode:
                    cfg = getattr(state, "config", None)
                    merged_cfg = getattr(cfg, "_merged", {}) if cfg else {}
                    if not isinstance(merged_cfg, dict):
                        merged_cfg = {}
                    voice_cfg = merged_cfg.get("voice") or {}
                    selected_mode = voice_cfg.get("mode", "openai_realtime")
                if selected_mode not in (
                    "openai_realtime", "gemini_live", "chained",
                ):
                    logger.warning(
                        "voice_session_start: unknown voice_mode=%r, "
                        "defaulting to openai_realtime",
                        selected_mode,
                    )
                    selected_mode = "openai_realtime"

                voice_provider = "openai"
                if selected_mode == "gemini_live":
                    voice_provider = "gemini"
                mode_for_router = selected_mode if selected_mode in {"openai_realtime", "gemini_live", "chained"} else "openai_realtime"
                state.voice_router.register_voice_config(
                    node_id,
                    {
                        "node_id": node_id,
                        "mode": mode_for_router,
                        "voice_provider": voice_provider,
                        "supports_realtime": selected_mode in {"openai_realtime", "gemini_live"},
                        "sample_rate": payload_dict.get("sample_rate", 24000),
                        "channels": payload_dict.get("channels", 1),
                        "language_hint": payload_dict.get("language_hint", "en-US"),
                        "interrupt_policy": payload_dict.get("interrupt_policy", "barge_in"),
                        "camera_linked": bool(payload_dict.get("camera_linked", False)),
                        "phone_mode": payload_dict.get("mode", "push_to_talk"),
                        "skip_wake": True,
                    },
                )

                try:
                    await state.voice_router.open_session(
                        session_id=session_id,
                        mode=selected_mode,
                        provider_opts={
                            "node_id": node_id,
                            "sample_rate": payload_dict.get("sample_rate", 24000),
                            "language_hint": payload_dict.get("language_hint", "en-US"),
                            **(payload_dict.get("provider_opts") or {}),
                        },
                    )
                except Exception as exc:
                    logger.exception(
                        "voice_router.open_session failed for mode=%s: %s",
                        selected_mode, exc,
                    )
                    _record_phone_envelope(
                        "error",
                        "voice_session_start",
                        detail={
                            "mode": selected_mode,
                            "error": str(exc)[:200],
                        },
                        payload_for_hash=payload_dict,
                    )
                    continue

                _record_phone_envelope(
                    "allowed",
                    "voice_session_start",
                    detail={
                        "stream_id": stream_id,
                        "session_id": session_id,
                        "voice_mode": selected_mode,
                    },
                    payload_for_hash=payload_dict,
                )

            elif msg.type == "voice_interrupt":
                payload_dict = raw.get("payload", {})
                stream_id = payload_dict.get("stream_id", "")
                if not node_id or not state.voice_router:
                    _record_phone_envelope(
                        "denied",
                        "voice_interrupt",
                        detail={"reason": "missing_node_or_voice_router"},
                        payload_for_hash=payload_dict,
                    )
                    continue

                # A barge-in cancels the ASSISTANT'S CURRENT RESPONSE.
                # It must never be able to end the session: pre-fix,
                # when `get_session` missed (which a zombie session
                # guarantees), this fell through to `stop_session` on
                # the realtime AND gemini proxies — so speaking over
                # the assistant killed the call, and with no
                # `voice_status` emitted the client kept rendering
                # "listening" at a socket that no longer existed.
                # Cancel-only now; a miss is reported as a no-op.
                cancelled = False
                try:
                    realtime = getattr(state.voice_router, "_realtime", None)
                    if realtime:
                        rs = realtime.get_session(node_id)
                        if rs and hasattr(rs, "cancel_response"):
                            await rs.cancel_response()
                            cancelled = True
                    gemini = getattr(state.voice_router, "_gemini", None)
                    if gemini and not cancelled:
                        gs = gemini.get_session(node_id)
                        if gs and hasattr(gs, "cancel_response"):
                            await gs.cancel_response()
                            cancelled = True
                    # Chained mode had no barge-in at all: the two branches
                    # above only reach the realtime and Gemini proxies, so
                    # speaking over the assistant on a chained session did
                    # nothing. Note the key differs, realtime and Gemini are
                    # keyed by node_id while chained sessions are keyed by
                    # session_id. Safe on unknown or idle sessions and on a
                    # router with no pipeline wired, so no mode check.
                    if not cancelled:
                        cancelled = await state.voice_router.cancel_chained_response(
                            session_id
                        )
                    if not cancelled:
                        logger.info(
                            "voice_interrupt for node=%s found no live session "
                            "to cancel — ignoring (barge-in never tears a "
                            "session down)", node_id,
                        )
                except Exception as exc:
                    _record_phone_envelope(
                        "error",
                        "voice_interrupt",
                        detail={"reason": "interrupt_failed", "error": str(exc)[:200], "stream_id": stream_id},
                        payload_for_hash=payload_dict,
                    )
                    continue

                _record_phone_envelope(
                    "allowed" if cancelled else "denied",
                    "voice_interrupt",
                    detail={"stream_id": stream_id, "cancelled": cancelled},
                    payload_for_hash=payload_dict,
                )

            elif msg.type == "genui_event":
                payload_dict = raw.get("payload", {})
                if not state.orchestrator:
                    _record_phone_envelope(
                        "denied",
                        "genui_event",
                        detail={"reason": "missing_orchestrator"},
                        payload_for_hash=payload_dict,
                    )
                    continue
                try:
                    from agents.ui_handlers import _handle_app_action

                    app_id = payload_dict.get("app_id", "")
                    surface_id = payload_dict.get("surface_id", "")
                    event_type = payload_dict.get("event_type", "tap")
                    action_id = payload_dict.get("action_id", "")
                    value = payload_dict.get("value")
                    target_sid = next(iter(state.get_sessions_for_daemon(node_id)), "") if node_id else ""
                    if not target_sid:
                        target_sid = f"phone-{node_id or paired_device_id or 'session'}"
                        state.sessions[target_sid] = ws
                        if node_id:
                            state.bind_session_to_daemon(target_sid, node_id)
                    screen_id = payload_dict.get("screen_id")
                    if not screen_id:
                        registry = getattr(state, "app_registry", None)
                        if registry is not None and hasattr(registry, "build_screen_id"):
                            screen_id = registry.build_screen_id(
                                app_id=app_id,
                                surface_id=surface_id or "home",
                                scope=target_sid,
                            )
                        else:
                            screen_id = f"{app_id}:{surface_id}:{target_sid}"
                    await _handle_app_action(
                        state.orchestrator,
                        session_id=target_sid,
                        app_id=app_id,
                        action_id=action_id,
                        event=event_type,
                        value=value,
                        screen_id=screen_id,
                    )
                    _record_phone_envelope(
                        "allowed",
                        "genui_event",
                        detail={
                            "session_id": target_sid,
                            "app_id": app_id,
                            "surface_id": surface_id,
                            "action_id": action_id,
                        },
                        payload_for_hash=payload_dict,
                    )
                except Exception as exc:
                    _record_phone_envelope(
                        "error",
                        "genui_event",
                        detail={"reason": "dispatch_failed", "error": str(exc)[:200]},
                        payload_for_hash=payload_dict,
                    )

            elif msg.type == "location_update":
                # Phone-as-peer: location streamed over the same HUP
                # WebSocket as audio/video/etc. Replaces the legacy
                # POST /api/location/update HTTP path that returned
                # 401 for phones (they have phone_bearer in IDB, not
                # the dashboard API key the HTTP endpoint required).
                # HUP v1.3.1.
                payload_dict = raw.get("payload", {})
                if not state.location_engine:
                    _record_phone_envelope(
                        "denied",
                        "location_update",
                        detail={"reason": "missing_location_engine"},
                        payload_for_hash=payload_dict,
                    )
                    continue
                try:
                    lat = float(payload_dict.get("lat") or 0)
                    lon = float(payload_dict.get("lon") or 0)
                    src = (
                        payload_dict.get("source")
                        or payload_dict.get("node_id")
                        or "browser_node"
                    )
                    if lat == 0 and lon == 0:
                        # Browser geolocation can briefly emit (0,0)
                        # before the GPS fix lands; ignore so it
                        # doesn't poison geofence checks at Null Island.
                        _record_phone_envelope(
                            "skipped",
                            "location_update",
                            detail={"reason": "null_island"},
                            payload_for_hash=payload_dict,
                        )
                        continue
                    triggered = await state.location_engine.update_location(
                        lat, lon, source=str(src)[:64],
                    )
                    _record_phone_envelope(
                        "accepted",
                        "location_update",
                        detail={
                            "lat": lat, "lon": lon,
                            "source": src,
                            "geofence_events": len(triggered),
                        },
                        payload_for_hash=payload_dict,
                    )
                except Exception as exc:
                    _record_phone_envelope(
                        "error",
                        "location_update",
                        detail={"reason": "update_failed", "error": str(exc)[:200]},
                        payload_for_hash=payload_dict,
                    )

            elif msg.type == "peripheral_bridge_register":
                payload_dict = raw.get("payload", {})
                if not state.device_registry:
                    _record_phone_envelope(
                        "denied",
                        "peripheral_bridge_register",
                        detail={"reason": "missing_device_registry"},
                        payload_for_hash=payload_dict,
                    )
                    continue
                try:
                    from hardware.protocol import DeviceManifest

                    registered_ids: list[str] = []
                    bridge_id = payload_dict.get("bridge_id", "")
                    platform = payload_dict.get("platform", "")
                    expires_at = payload_dict.get("expires_at", "")
                    devices = payload_dict.get("devices", []) or []
                    for entry in devices:
                        manifest_dict = dict(entry.get("manifest") or {})
                        device_id = entry.get("device_id", "")
                        if not manifest_dict.get("device_id"):
                            manifest_dict["device_id"] = device_id
                        if not manifest_dict.get("device_type"):
                            manifest_dict["device_type"] = entry.get("kind", "sensor_hub")
                        if not manifest_dict.get("name"):
                            manifest_dict["name"] = device_id or "phone-bridge-device"
                        if not manifest_dict.get("connection_type"):
                            manifest_dict["connection_type"] = entry.get("protocol", "websocket")
                        if not isinstance(manifest_dict.get("capabilities"), list):
                            manifest_dict["capabilities"] = []
                        elif manifest_dict["capabilities"] and not isinstance(manifest_dict["capabilities"][0], dict):
                            manifest_dict["capabilities"] = []
                        if not isinstance(manifest_dict.get("sensors"), list):
                            manifest_dict["sensors"] = list(entry.get("capabilities", []) or [])
                        if not isinstance(manifest_dict.get("actuators"), list):
                            manifest_dict["actuators"] = []
                        manifest = DeviceManifest(**manifest_dict)
                        # Give the peripheral a transport so its actions can
                        # actually execute (relayed through the bridge node),
                        # instead of registering a manifest with no adapter.
                        bridge_adapter = None
                        try:
                            from hardware.adapters.bridge import BridgedPeripheralAdapter

                            bridge_adapter = BridgedPeripheralAdapter(
                                manifest.device_id,
                                node_id=node_id,
                                mesh=getattr(state, "hardware_mesh", None),
                                manifest=manifest,
                            )
                        except Exception:
                            bridge_adapter = None
                        state.device_registry.register_device(manifest, bridge_adapter)
                        # Universal HUP ingress: self-describing peripherals
                        # become LLM tools + safety + honesty loop generically.
                        try:
                            register = getattr(
                                state, "register_generic_hardware_skill_for", None
                            )
                            if callable(register) and bridge_adapter is not None:
                                register(manifest, bridge_adapter, device_id=manifest.device_id)
                        except Exception:
                            pass
                        if manifest.device_id:
                            registered_ids.append(manifest.device_id)
                    state.devices[bridge_id] = {
                        "node_id": node_id,
                        "bridge_id": bridge_id,
                        "platform": platform,
                        "expires_at": expires_at,
                        "devices": registered_ids,
                    }
                    _record_phone_envelope(
                        "allowed",
                        "peripheral_bridge_register",
                        detail={
                            "bridge_id": bridge_id,
                            "platform": platform,
                            "device_count": len(registered_ids),
                        },
                        payload_for_hash=payload_dict,
                    )
                except Exception as exc:
                    _record_phone_envelope(
                        "error",
                        "peripheral_bridge_register",
                        detail={"reason": "registry_write_failed", "error": str(exc)[:200]},
                        payload_for_hash=payload_dict,
                    )

            elif msg.type == "backchannel_request":
                payload_dict = raw.get("payload", {})
                import json as _json
                import sqlite3 as _sqlite3
                from config.loader import feral_home as _feral_home

                request_id = payload_dict.get("request_id") or str(uuid4())
                req_ts = float(raw.get("ts") or time.time())
                device_id = payload_dict.get("device_id") or node_id or str(paired_device_id or "")
                kind = payload_dict.get("kind", "general")
                status = payload_dict.get("status", "pending")
                payload_json = _json.dumps(payload_dict, sort_keys=True, default=str)
                db_path = _feral_home() / "backchannel_requests.db"
                try:
                    with _sqlite3.connect(str(db_path)) as conn:
                        conn.execute(
                            """
                            CREATE TABLE IF NOT EXISTS backchannel_requests (
                                id TEXT PRIMARY KEY,
                                ts REAL NOT NULL,
                                device_id TEXT NOT NULL,
                                kind TEXT NOT NULL,
                                payload_json TEXT NOT NULL,
                                status TEXT NOT NULL
                            )
                            """
                        )
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO backchannel_requests
                            (id, ts, device_id, kind, payload_json, status)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (request_id, req_ts, device_id, kind, payload_json, status),
                        )
                        conn.commit()
                    _record_phone_envelope(
                        "allowed",
                        "backchannel_request",
                        detail={"id": request_id, "device_id": device_id, "kind": kind, "status": status},
                        payload_for_hash=payload_dict,
                    )
                except Exception as exc:
                    _record_phone_envelope(
                        "error",
                        "backchannel_request",
                        detail={"reason": "sqlite_persist_failed", "error": str(exc)[:200]},
                        payload_for_hash=payload_dict,
                    )

            elif msg.type == "audio_chunk" and node_id:
                payload_dict = raw.get("payload", {})
                audio_b64 = payload_dict.get("data_b64", "")
                chunk_idx = payload_dict.get("chunk_index", 0)
                # Live-test diagnostic: log the FIRST chunk + every 50th
                # chunk so the brain log shows whether phone PCM16 is
                # actually reaching us. Without this, the "no audio"
                # failure mode is invisible from the brain side.
                if chunk_idx == 0 or (chunk_idx % 50 == 0):
                    logger.info(
                        "audio_chunk from node=%s chunk=%d bytes_b64=%d "
                        "final=%s", node_id, chunk_idx, len(audio_b64 or ""),
                        payload_dict.get("is_final", False),
                    )
                if state.voice_router and audio_b64:
                    sessions = state.get_sessions_for_daemon(node_id)
                    target_sid = next(iter(sessions), None)
                    if not target_sid:
                        if chunk_idx == 0:
                            logger.warning(
                                "audio_chunk from node=%s dropped — "
                                "no voice session bound to this daemon. "
                                "Did voice_session_start arrive before audio?",
                                node_id,
                            )
                    else:
                        await state.voice_router.handle_audio_from_node(
                            node_id=node_id,
                            session_id=target_sid,
                            audio_b64=audio_b64,
                            chunk_index=chunk_idx,
                            is_final=payload_dict.get("is_final", False),
                            encoding=payload_dict.get("encoding", "pcm16"),
                            sample_rate=payload_dict.get("sample_rate", 24000),
                        )
                elif not state.voice_router:
                    if chunk_idx == 0:
                        logger.warning(
                            "audio_chunk from node=%s dropped — "
                            "voice_router not initialised", node_id,
                        )
                elif not audio_b64:
                    if chunk_idx == 0:
                        logger.warning(
                            "audio_chunk from node=%s dropped — empty data_b64",
                            node_id,
                        )

            elif msg.type == "skill_approval":
                payload_dict = raw.get("payload", {})
                skill_id = payload_dict.get("skill_id", "")
                approved = payload_dict.get("approved", False)
                if state.skill_gen and skill_id:
                    if approved:
                        await state.skill_gen.approve_skill(skill_id)
                        logger.info(f"Skill approved via phone: {skill_id}")
                    else:
                        state.skill_gen.reject_skill(skill_id)
                        logger.info(f"Skill rejected via phone: {skill_id}")

            elif msg.type == "text_command":
                payload_dict = raw.get("payload", {})
                text = payload_dict.get("text", "")
                context = payload_dict.get("context", {})
                if text and state.orchestrator and node_id:
                    sessions = state.get_sessions_for_daemon(node_id)
                    target_sid = next(iter(sessions), None)
                    if not target_sid:
                        target_sid = f"daemon-{node_id}"
                        state.sessions[target_sid] = ws
                        state.bind_session_to_daemon(target_sid, node_id)

                    # Lane 08 WS7 — route the phone HUP text_command
                    # through the same prelude as the WebUI session
                    # WS so the orchestrator sees an identical
                    # invocation shape (same working_push, same
                    # PromptRefiner output, same ctx["refinement"]
                    # contract for Lane 12). Without this the phone
                    # path bypassed PromptRefiner and the assistant
                    # saw raw text without device_target resolution.
                    refined_text, refined_ctx, _ = await _prepare_chat_turn_context(
                        session_id=target_sid,
                        text=text,
                        raw_context=context,
                        source_node=node_id,
                    )

                    # Lane 08 WS9 — non-blocking; identical task
                    # lifecycle to WebUI. The /v1/node WS keeps
                    # receiving daemon frames while the turn runs.
                    state.register_background_task(
                        asyncio.create_task(
                            _build_chat_turn_runner(
                                ws=ws,
                                session_id=target_sid,
                                refined_text=refined_text,
                                ctx=refined_ctx,
                            )
                        )
                    )
                    logger.info(f"Text command from daemon {node_id}: {text[:80]}")

            elif msg.type == "frame":
                frame_payload = raw.get("payload", {})
                data_b64 = frame_payload.get("data_b64") or frame_payload.get("image_b64", "")
                if data_b64:
                    frame_payload["data_b64"] = data_b64
                    frame_b64_len = len(data_b64)
                    if frame_b64_len > VISION_MAX_FRAME_KB * 1024:
                        logger.warning(f"Rejecting oversized frame from {node_id}: {frame_b64_len}B")
                    else:
                        effective_node = node_id or frame_payload.get("node_id", "unknown")
                        state.vision_buffer.push(effective_node, frame_payload)
                        for sid in state.get_sessions_for_daemon(effective_node):
                            state.perception.update_vision(sid, state.vision_buffer, effective_node)

            elif msg.type == "video_frame":
                # HUP v1.1 §5.4.2 — route video frames into the vision buffer,
                # same sink as the legacy vision_frame branch above.
                _handle_video_frame(node_id, raw.get("payload", {}), msg.msg_id)

            elif msg.type == "audio_frame":
                # HUP v1.1 §5.4.1 — route audio frames into the audio pipeline
                # when available; otherwise log and move on.
                _handle_audio_frame(node_id, raw.get("payload", {}))

            elif msg.type == "glasses_frame":
                # HUP v1.3.0 §5.4.3 — smart-glasses (or glasses-equivalent
                # phone-camera fallback) vision frame. Routes into the
                # dedicated per-device circular buffer at
                # ``state.glasses_buffer`` which the orchestrator's
                # vision-context-attach (Lane 08) reads.
                _handle_glasses_frame(node_id, raw.get("payload", {}), msg.msg_id)

            elif msg.type == "device_announce":
                # HUP v1.3.0 §5.4.4 — peripheral discovery from a scanning
                # node. Routes through hardware_mesh into the knowledge
                # graph so device queries answer via the same memory tool
                # as everything else.
                await _handle_device_announce(node_id, raw.get("payload", {}))

            elif msg.type == "device_event":
                # HUP v1.1 `device_event` envelope. Unwrap to the concrete
                # event_type and dispatch. Biometric / sensor / gesture
                # types land in the same sinks as the legacy `telemetry`
                # and `gesture` branches above. Unknown event_types are
                # ignored per the forward-compat rule in HUP_SPEC.md §1.
                de_payload = raw.get("payload", {}) or {}
                ev_type = de_payload.get("event_type", "")
                if ev_type == "audio_frame":
                    _handle_audio_frame(node_id, de_payload)
                elif ev_type == "video_frame":
                    _handle_video_frame(node_id, de_payload, msg.msg_id)
                elif ev_type in {
                    "heart_rate", "spo2", "skin_temperature", "steps",
                    "temperature", "accelerometer", "gesture",
                }:
                    _handle_biometric_device_event(node_id, ev_type, de_payload)
                elif ev_type in {"robot_telemetry", "robot_event"}:
                    # CuteBot bridge node feedback (HUP §6.2). Thin path:
                    # normalize payload → perception.update_sensors(robot=…).
                    _robot_payload = _unwrap_hup_frame(de_payload)
                    _robot_sensors = {
                        "mode": str(_robot_payload.get("mode") or ""),
                        "state": str(_robot_payload.get("state") or ""),
                        "sonar_cm": float(_robot_payload.get("sonar_cm") or 0.0),
                        "online": True,
                        "battery": bool(_robot_payload.get("battery", False)),
                    }
                    for sid in state.get_sessions_for_daemon(node_id):
                        state.perception.update_sensors(
                            sid, {"robot": _robot_sensors},
                        )
                elif ev_type.endswith("_status"):
                    # Sub-device status frames (e.g. ``glasses_status``,
                    # future ``apple_health_status`` /
                    # ``whoop_status``). Routed to the truth store so
                    # the dashboard, the native iOS UI, and any future
                    # MCP consumer share one binding for "Active".
                    await _handle_subdevice_status(ws, node_id, ev_type, de_payload)
                else:
                    logger.debug(
                        "Ignoring unknown device_event event_type=%r from %s",
                        ev_type, node_id,
                    )

            else:
                logger.debug("Unknown HUP msg type=%r from %s", msg.type, node_id)
                await _send_protocol_error(ws, 1002, f"Unknown message type: {msg.type}")

    except WebSocketDisconnect:
        if node_id:
            logger.info(f"Daemon disconnected: {node_id}")
            state.daemons.pop(node_id, None)
            # Same leak as the web handler: audio/perception/mesh state
            # was cleared here but the voice router never was, so a
            # phone that dropped LTE or went to background kept a live
            # (billing) OpenAI Realtime socket and a stale
            # node->session entry that poisoned its next
            # voice_session_start. `stop_node_voice` is the
            # node-shaped teardown — `stop_session_voice` only ever
            # finds web sessions, whose node id is the synthetic
            # `webclient_<sid>`.
            if state.voice_router:
                try:
                    await state.voice_router.stop_node_voice(node_id)
                except Exception as voice_exc:
                    logger.warning(
                        f"Voice teardown for node {node_id} failed: {voice_exc}"
                    )
            if state.skill_executor:
                state.skill_executor.unregister_daemon(node_id)
            if state.hardware_mesh:
                state.hardware_mesh.on_node_disconnected(node_id)
            state.capability_registry.unregister_node(node_id)
            for sid in state.get_sessions_for_daemon(node_id):
                state.perception.update_connected_nodes(sid, list(state.daemons.keys()))


# ─────────────────────────────────────────────
# Federated Sync WebSocket
# ─────────────────────────────────────────────

@app.websocket("/sync")
async def sync_peer_endpoint(ws: WebSocket):
    """Peer-to-peer sync endpoint for federated memory."""
    await ws.accept()
    logger.info("Sync peer connected")

    try:
        while True:
            raw = await ws.receive_json()
            msg_type = raw.get("type")

            if msg_type == "sync_request":
                peer_id = raw.get("node_id", "unknown")
                remote_vc = raw.get("vector_clock", {})

                # audit-r12 A2 (v2026.5.38) — handshake REJECTS when
                # the local passphrase is unset (was: accepted, which
                # made /sync a zero-auth endpoint on fresh installs).
                # ``ensure_sync_passphrase`` runs at boot so a fresh
                # install always has a value (auto-generated + printed
                # to the operator banner). The remote-side mismatch
                # check below stays identical.
                from memory.sync import SYNC_PASSPHRASE as _local_pass
                expected_pass = os.getenv("FERAL_SYNC_PASSPHRASE", "") or _local_pass
                remote_pass = raw.get("passphrase", "")
                if not expected_pass:
                    await ws.send_json({
                        "type": "sync_error",
                        "message": (
                            "Local sync passphrase unset — set "
                            "FERAL_SYNC_PASSPHRASE on this brain and retry."
                        ),
                    })
                    break
                if remote_pass != expected_pass:
                    await ws.send_json({"type": "sync_error", "message": "Invalid passphrase"})
                    break

                # v2026.5.34 (PR 2 D12): refuse the handshake when a
                # peer advertises our own node_id. The HLC protocol's
                # tiebreaker assumes globally-unique ids; a duplicate
                # means an operator cloned ~/.feral/sync_node_id
                # between two brains and both copies must rotate
                # before sync can land safely.
                if state.sync_engine and peer_id == state.sync_engine.node_id:
                    await ws.send_json({
                        "type": "sync_error",
                        "message": (
                            "duplicate_node_id: peer advertised the same "
                            "node_id as the local brain. Rotate ~/.feral/sync_node_id "
                            "on one side and restart."
                        ),
                    })
                    logger.warning("Sync handshake rejected: duplicate node_id %s", peer_id)
                    break

                await ws.send_json({
                    "type": "sync_response",
                    "node_id": state.sync_engine.node_id if state.sync_engine else "",
                    "vector_clock": state.sync_engine.get_vector_clock() if state.sync_engine else {},
                })

                incoming = await ws.receive_json()
                applied = 0
                if incoming.get("type") == "sync_data" and state.sync_engine:
                    # v2026.5.34 (PR 2 D12): refresh-gate the apply.
                    # If the on-disk store has been corrupted /
                    # restored / rotated since boot, the in-memory
                    # cache is stale and apply_remote_changes would
                    # mutate a wrong shape. Refresh fails loud to the
                    # peer so they can retry once we've recovered.
                    try:
                        refresh = await state.memory.refresh()
                        if not refresh.get("ok", True):
                            await ws.send_json({
                                "type": "sync_error",
                                "message": (
                                    f"memory_refresh_failed: {refresh.get('error', 'unknown')}"
                                ),
                            })
                            logger.warning(
                                "Sync apply aborted: memory.refresh() reported %s", refresh,
                            )
                            break
                    except Exception as exc:
                        await ws.send_json({
                            "type": "sync_error",
                            "message": f"memory_refresh_exception: {exc}",
                        })
                        logger.warning("Sync apply aborted: memory.refresh() raised %s", exc)
                        break
                    applied = await state.sync_engine.apply_remote_changes(
                        incoming.get("changes", [])
                    )

                my_changes = []
                if state.sync_engine and hasattr(state.sync_engine, '_wal'):
                    my_changes = state.sync_engine._wal.get_changes_since(
                        remote_vc.get(state.sync_engine.node_id, "0:0:"),
                        exclude_node=peer_id,
                    )
                await ws.send_json({
                    "type": "sync_data",
                    "changes": [op.to_dict() for op in my_changes] if my_changes else [],
                })
                _log_activity("sync", f"Synced with {peer_id}: received {applied} ops")
                break

    except WebSocketDisconnect:
        logger.info("Sync peer disconnected")
    except Exception as e:
        logger.warning(f"Sync peer error: {e}")


# ─────────────────────────────────────────────
# Baseline Biometric Recording
# ─────────────────────────────────────────────

_BIOMETRIC_KEY_MAP = {
    "heart_rate": ("hr_resting", "health"),
    "ppg_heart_rate": ("hr_resting", "health"),
    "spo2": ("spo2_pct", "health"),
    "spo2_pct": ("spo2_pct", "health"),
    "skin_temp_c": ("skin_temp", "health"),
    "skin_temperature_c": ("skin_temp", "health"),
    "hrv_ms": ("hrv_ms", "health"),
    "sleep_hours": ("sleep_hours", "health"),
    "sleep_score": ("sleep_score", "health"),
    "steps": ("steps_daily", "activity"),
    "calories": ("calories_daily", "activity"),
}

# Vitals that participate in per-source baseline namespacing
# (Fix #5). With BOTH the W300 glasses and the Veepoo wristband
# streaming, the prior bare ``hr_resting`` row averaged samples
# from two physically different sensors (chest-strap-equivalent
# vs wrist PPG) into the same series — the resulting baseline was
# biased toward whichever device pushed more samples and either
# device's "anomaly" check was running against a polluted mean.
# Now: a known live-wearable source trains a per-source row
# (``hr_resting:jw_health_glasses``) AND we keep writing the bare
# ``hr_resting`` so legacy queries and the existing test fixtures
# keep working back-compat. Lagging / unknown sources only train
# the bare row (per-source rows are reserved for the canonical
# wearable taxonomy listed below).
_BASELINE_PER_SOURCE_VITALS: frozenset[str] = frozenset({
    "hr_resting",
    "spo2_pct",
})


AUDIO_FRAME_MAX_BYTES = 64 * 1024  # HUP_SPEC.md §5.4.1 cap
VIDEO_FRAME_MAX_BYTES = 512 * 1024  # HUP_SPEC.md §5.4.2 cap (matches existing VISION_MAX_FRAME_KB)


def _unwrap_hup_frame(raw_payload: dict) -> dict:
    """Accept both ``device_event`` shapes.

    The HUP v1.1 Python SDK wraps media fields inside
    ``DeviceEventPayload.data`` (so the wire carries
    ``payload.data.data_b64``), while legacy direct-send daemons emit
    the fields flat at the top of the payload (``payload.data_b64``).
    Normalise to a single flat dict here so the downstream vision /
    audio sinks keep working regardless of which client shipped the
    frame. Top-level fields always win, so partially-migrated daemons
    that send both shapes are tolerated.
    """
    if not isinstance(raw_payload, dict):
        return {}
    nested = raw_payload.get("data") if isinstance(raw_payload.get("data"), dict) else {}
    if not nested:
        return raw_payload
    merged: dict = {}
    merged.update(nested)
    for k, v in raw_payload.items():
        if k == "data":
            continue
        merged[k] = v
    return merged


def _handle_video_frame(node_id, frame_payload: dict, msg_id=None) -> None:
    """Dispatch a HUP v1.1 ``video_frame`` payload into the vision buffer.

    Shares the existing vision-buffer sink with the legacy ``vision_frame``
    branch so downstream perception code stays unchanged. Over-cap frames
    are dropped with a warning per HUP_SPEC.md error code 4020.

    Accepts both the flat and nested ``device_event`` payload shapes
    via :func:`_unwrap_hup_frame` — the HUP v1.1 Python SDK serialises
    its frames nested under ``payload.data`` while the legacy direct
    ``vision_frame`` path carries them flat.
    """
    frame_payload = _unwrap_hup_frame(frame_payload)
    data_b64 = frame_payload.get("data_b64", "") or ""
    if len(data_b64) > VIDEO_FRAME_MAX_BYTES:
        logger.warning(
            "Rejecting oversized video_frame from %s: %dB > %dB (HUP error 4020)",
            node_id, len(data_b64), VIDEO_FRAME_MAX_BYTES,
        )
        return

    effective_node = node_id or frame_payload.get("node_id", "unknown")
    state.vision_buffer.push(effective_node, frame_payload)

    for sid in state.get_sessions_for_daemon(effective_node):
        state.perception.update_vision(sid, state.vision_buffer, effective_node)

    change_event = state.change_detector.should_analyze(
        effective_node, data_b64, frame_payload.get("codec", "jpeg"),
    )
    if change_event and state.scene and state.scene.available:
        mode = "tracking" if change_event.trigger_reason == "scene_change" else "general"
        asyncio.ensure_future(
            _analyze_scene_background(effective_node, frame_payload, mode=mode)
        )

    if msg_id and state.orchestrator:
        state.orchestrator.resolve_pending_frame(msg_id, frame_payload)


def _handle_audio_frame(node_id, frame_payload: dict) -> None:
    """Dispatch a HUP v1.1 ``audio_frame`` payload into the audio pipeline.

    Accepts both SDK-nested and flat payload shapes via
    :func:`_unwrap_hup_frame`. Falls back to a debug log when
    ``state.audio`` does not expose an ``ingest_frame`` hook — the
    Brain boot tolerates the pipeline being absent, so we must too.
    """
    frame_payload = _unwrap_hup_frame(frame_payload)
    data_b64 = frame_payload.get("data_b64", "") or ""
    if len(data_b64) > AUDIO_FRAME_MAX_BYTES:
        logger.warning(
            "Rejecting oversized audio_frame from %s: %dB > %dB (HUP error 4020)",
            node_id, len(data_b64), AUDIO_FRAME_MAX_BYTES,
        )
        return

    effective_node = node_id or frame_payload.get("node_id", "unknown")
    audio = getattr(state, "audio", None)
    ingest = getattr(audio, "ingest_frame", None)
    if callable(ingest):
        try:
            ingest(effective_node, frame_payload)
        except Exception as exc:
            logger.warning("audio.ingest_frame raised for %s: %s", effective_node, exc)
    else:
        logger.debug(
            "Received audio_frame from %s but state.audio has no ingest_frame hook; dropping.",
            effective_node,
        )


def _handle_glasses_frame(node_id, frame_payload: dict, msg_id=None) -> None:
    """Dispatch a HUP v1.3.0 ``glasses_frame`` payload into the glasses
    buffer.

    Shares the 512 KiB-per-frame decoded cap with ``_handle_video_frame``
    (HUP §2). Over-cap frames are dropped with HUP error code 4020. The
    buffer (``state.glasses_buffer``) is a per-``device_id`` ring; the
    orchestrator's vision-context-attach reads it freshness-gated.

    Tolerant of both flat payloads (canonical ``glasses_frame`` envelope)
    and nested ``device_event``-style payloads via
    :func:`_unwrap_hup_frame` — symmetric with the ``video_frame`` /
    ``audio_frame`` handlers.
    """
    frame_payload = _unwrap_hup_frame(frame_payload)
    data_b64 = frame_payload.get("data_b64", "") or ""
    if len(data_b64) > VIDEO_FRAME_MAX_BYTES:
        logger.warning(
            "Rejecting oversized glasses_frame from %s: %dB > %dB (HUP error 4020)",
            node_id, len(data_b64), VIDEO_FRAME_MAX_BYTES,
        )
        return

    effective_node = node_id or frame_payload.get("node_id", "unknown")
    buf = getattr(state, "glasses_buffer", None)
    if buf is None:
        logger.debug(
            "Received glasses_frame from %s but state.glasses_buffer is not "
            "wired; dropping. (boot wiring at api/state.py)",
            effective_node,
        )
        return
    try:
        buf.ingest(frame_payload, node_id=effective_node)
    except Exception as exc:
        logger.warning(
            "glasses_buffer.ingest raised for %s: %s", effective_node, exc
        )
        return

    if msg_id and state.orchestrator:
        # Allow orchestrators that explicitly requested a frame
        # (e.g. ``vision_ask`` mode) to resolve their pending request.
        try:
            state.orchestrator.resolve_pending_frame(msg_id, frame_payload)
        except Exception:  # noqa: BLE001 — best-effort signal
            pass


async def _handle_device_announce(node_id, frame_payload: dict) -> None:
    """Route a HUP v1.3.0 ``device_announce`` payload through the
    hardware mesh.

    The mesh upserts a knowledge-graph entity (``category=device``) so
    chat memory queries like "what BLE devices are around my phone?"
    can answer via the standard memory tool surface. Repeated
    announcements for the same ``device_id`` update ``last_seen`` /
    ``rssi_dbm`` in place rather than duplicating the entity.

    Defensive against missing mesh / memory wiring at boot — drops
    cleanly with a debug log so a half-booted brain doesn't 500 on
    an early peripheral scan.
    """
    payload = _unwrap_hup_frame(frame_payload)
    mesh = getattr(state, "hardware_mesh", None)
    ingest = getattr(mesh, "ingest_device_announce", None) if mesh else None
    if not callable(ingest):
        logger.debug(
            "Received device_announce from %s but hardware_mesh has no "
            "ingest_device_announce hook; dropping.", node_id,
        )
        return
    # Brain falls back to the WS-level node id when the scanner_node_id
    # field is missing — daemons SHOULD set it but the spec defaults to
    # the WS-level id.
    if not payload.get("scanner_node_id") and node_id:
        payload["scanner_node_id"] = node_id
    try:
        await ingest(payload)
    except Exception as exc:
        logger.warning(
            "hardware_mesh.ingest_device_announce raised for scanner=%s "
            "device=%s: %s",
            node_id, payload.get("device_id", "?"), exc,
        )


async def _handle_subdevice_status(
    ws,
    node_id,
    event_type: str,
    frame_payload: dict,
) -> None:
    """Ingest a sub-device status update into the truth store.

    A sub-device is anything an HUP node owns that is not the node
    itself — Theora glasses paired through the iPhone companion, an
    Apple Health pipeline behind the same phone, a cloud-synced Whoop
    account, etc. Every status frame the brain receives lands here so
    a single SQLite-backed store is the source of truth for the web
    dashboard, the iOS UI, and any future MCP consumer.

    Accepts two wire shapes, both flattened by :func:`_unwrap_hup_frame`:

    * **iOS / native node** (``device_event`` envelope, ``event_type:
      "glasses_status"``): ``data`` carries ``status`` (e.g. ``"ready"``,
      ``"failed"``, ``"connecting"``), ``source`` (capability id, e.g.
      ``"jw_health_glasses"``), and any extras (``device_name``,
      ``reason``, ``rssi``, etc.) which become ``attrs``.
    * **Top-level ``glasses_status``** (legacy / Pydantic
      ``GlassesStatusPayload``): ``glasses_connected: bool``,
      ``battery_level: int``, ``glasses_model: str``. Mapped to
      ``status="ready"|"disconnected"`` and the rest of the fields
      become ``attrs``.

    Drop / reject behaviour (Phase 1.5 strict ingest):

    * Missing ``status`` AND missing ``glasses_connected`` → log
      and drop. We do not invent a status from thin air.
    * Missing ``capability`` → log and drop.
    * Unknown ``provenance`` (anything not in
      ``{"ble", "cloud", "host", "synthetic"}``) → reject the frame
      with HUP error code ``1003`` and log the source node + bad
      value. Coercing to ``"ble"`` would silently produce a row
      with the wrong heartbeat window, so we fail loud.
    """
    if state.node_subdevices is None:
        return
    if not node_id:
        return
    payload = _unwrap_hup_frame(frame_payload)

    # Source-of-truth: prefer an explicit status string from the iOS
    # path; fall back to the legacy boolean shape if that is what
    # arrived.
    status_raw = payload.get("status")
    glasses_connected = payload.get("glasses_connected")
    if isinstance(status_raw, str) and status_raw.strip():
        status = status_raw.strip()
    elif isinstance(glasses_connected, bool):
        status = "ready" if glasses_connected else "disconnected"
    else:
        logger.debug(
            "Subdevice status frame from %s/%s missing status field; dropping payload=%r",
            node_id, event_type, payload,
        )
        return

    capability = (
        payload.get("source")
        or payload.get("capability")
        # No suffix-stripping: the source-of-truth for capability id is
        # the iOS adapter's own ``capability`` string. Falling back to
        # the event_type unchanged gives us a stable bucket for legacy
        # frames that did not declare ``source``.
        or event_type
    )
    if not isinstance(capability, str) or not capability.strip():
        logger.debug(
            "Subdevice status from %s missing capability id; dropping payload=%r",
            node_id, payload,
        )
        return
    capability = capability.strip()

    # Strict provenance: the sub-device store enforces a closed set
    # so heartbeat-window math stays correct. Reject unknown values
    # with HUP 1003 so the client knows we didn't ingest the frame.
    allowed_provenances = {"ble", "cloud", "host", "synthetic"}
    provenance_raw = payload.get("provenance")
    if provenance_raw is None or provenance_raw == "":
        provenance_raw = "ble"
    if provenance_raw not in allowed_provenances:
        logger.warning(
            "Subdevice status from %s/%s carried unknown provenance=%r; "
            "rejecting (allowed=%s)",
            node_id, capability, provenance_raw, sorted(allowed_provenances),
        )
        if ws is not None:
            await _send_protocol_error(
                ws,
                1003,
                (
                    f"Unknown provenance {provenance_raw!r} on "
                    f"{event_type} for capability {capability!r}; "
                    f"allowed: {sorted(allowed_provenances)}"
                ),
                name="bad_provenance",
            )
        return

    # ``attrs`` carries everything the caller sent that wasn't part of
    # the canonical envelope. Top-level ``glasses_status`` adds
    # ``battery_level`` / ``glasses_model`` automatically.
    reserved = {
        "status", "source", "capability", "provenance",
        "event_type", "node_id", "ts",
    }
    attrs: dict = {}
    for key, value in payload.items():
        if key in reserved:
            continue
        attrs[key] = value

    try:
        state.node_subdevices.upsert(
            node_id=node_id,
            capability=capability,
            status=status,
            attrs=attrs,
            provenance=provenance_raw,
        )
    except Exception as exc:
        logger.warning(
            "node_subdevices.upsert failed for %s/%s: %s",
            node_id, capability, exc,
        )


# Map a connected node's advertised capability ids
# (from the HUP `node_register.capabilities` list) to the canonical
# wearable source string the brain pipeline keys off of. Used by
# `_infer_wearable_source_from_node` when a daemon emits a
# `heart_rate` / `spo2` `device_event` without setting an explicit
# `heart_rate_source` / `spo2_source` — without this inference,
# the source ends up "" and the freshness/priority logic demotes
# the (genuinely live) BLE PPG read to second-class.
_CAPABILITY_TO_WEARABLE_SOURCE = {
    "veepoo_wristband": "veepoo_wristband",
    "jw_health_glasses": "jw_health_glasses",
    "theora_w300": "jw_health_glasses",  # legacy alias → canonical
    "w610_glasses": "w610_glasses",
}


def _infer_wearable_source_from_node(node_id: str) -> str:
    """Return the canonical wearable source string for ``node_id``,
    or "" if no inference is possible.

    The brain stashes each connected daemon's HUP-declared
    ``capabilities`` list on the WebSocket as ``_feral_capabilities``
    at ``node_register`` time. Daemons like the iOS FeralSensorBridge
    and the local wristband_daemon emit `heart_rate` device_events
    without setting `heart_rate_source` (the iOS adapter only sets
    it for HealthKit-mirrored reads). When that happens the brain
    must NOT leave the source as "" — it lets a HealthKit emit demote
    a fresh BLE read in `perception.fusion._is_live_wearable("")` →
    False. So we look up the node's advertised caps and pick the
    matching canonical source string.

    First match wins, in the order `_CAPABILITY_TO_WEARABLE_SOURCE`
    iterates — Veepoo before glasses is fine because each node only
    advertises one PPG capability. Phase 12+ multi-wearable nodes
    would need a richer hint.
    """
    if not node_id:
        return ""
    daemons = getattr(state, "daemons", None) or {}
    ws = daemons.get(node_id)
    if ws is None:
        return ""
    caps = getattr(ws, "_feral_capabilities", None) or []
    for cap in caps:
        cap_norm = (cap or "").strip().lower()
        if cap_norm in _CAPABILITY_TO_WEARABLE_SOURCE:
            return _CAPABILITY_TO_WEARABLE_SOURCE[cap_norm]
    return ""


def _resolve_sample_ts(
    payload: dict,
    *,
    source: str,
    ts_keys: tuple[str, ...],
) -> float:
    """Return the canonical sample timestamp for a vitals reading.

    The bug being fixed (operator report 2026-06-08): HealthKit /
    cloud bridges that omit a timestamp were getting arrival-stamped
    with `time.time()`, then the freshness gate happily treated the
    stale reading as live and the proactive engine fired
    `hr_elevated` → `scene.calming` automation on a workout-from-
    hours-ago heart rate.

    Resolution order:

    1. Walk ``ts_keys`` in priority order — the canonical
       ``heart_rate_sample_ts`` / ``spo2_sample_ts`` first, then
       generic ``sample_ts``, then the canonical HUP envelope ``ts``,
       then the legacy iOS HealthKit ``timestamp``. Using the
       provided value (if any) preserves the real sample time even
       for lagging sources.

    2. If no timestamp was provided AND the source is a known
       lagging/cloud source (``apple_healthkit`` etc.) → return
       ``0.0``. The downstream freshness gates treat 0.0 as
       "never seen", which keeps a stale HealthKit reading from
       tripping a real-time alert.

    3. Otherwise (no timestamp AND the source is a live BLE
       wearable, or unknown / empty) → arrival-stamp with
       ``time.time()``. A genuinely live BLE wearable push (the
       wristband_daemon and iOS FeralSensorBridge both legitimately
       omit ts) is reaching the brain right now, so arrival time
       IS a valid sample time. Unknown / empty source defaults to
       arrival-stamp too — backward-compatible with legacy daemons
       that pre-date the source field, since `_is_lagging_source` is
       the only blocking predicate that matters.
    """
    for key in ts_keys:
        raw = payload.get(key)
        if isinstance(raw, (int, float)) and raw > 0:
            return float(raw)
    from perception.fusion import _is_lagging_source as _lag
    if _lag(source):
        return 0.0
    return time.time()


def _handle_biometric_device_event(node_id, event_type: str, frame_payload: dict) -> None:
    """Dispatch ``device_event`` payloads with biometric / sensor event types.

    Accepts both SDK-nested and flat shapes. Lands in the same sinks
    as the legacy ``telemetry`` branch: ``state.perception.update_sensors``
    per session and ``_record_biometrics_to_baseline`` for rolling stats.
    Handles ``heart_rate``, ``spo2``, ``skin_temperature``, ``steps``,
    ``temperature``, ``accelerometer``, ``button_press``.
    """
    frame_payload = _unwrap_hup_frame(frame_payload)
    effective_node = node_id or frame_payload.get("node_id", "unknown")
    # Reshape into the same ``sensors`` dict the legacy ``telemetry``
    # branch already expects: numeric keys land at the top level.
    sensors: dict = {}
    # HR carried either as {"bpm": int} (per HUP_SPEC examples) or as a
    # flat number under the event_type key — accept both.
    if event_type == "heart_rate":
        bpm = frame_payload.get("bpm")
        if bpm is None and isinstance(frame_payload.get("value"), (int, float)):
            bpm = frame_payload.get("value")
        if bpm is not None:
            sensors["ppg_heart_rate"] = bpm
            # Source resolution (Fix #3): prefer the explicit source
            # the daemon set; if absent, infer it from the emitting
            # node's advertised HUP capabilities (so a wristband_daemon
            # / iOS FeralSensorBridge that omitted the field still
            # carries `veepoo_wristband` / `jw_health_glasses` and
            # clears the live-wearable predicate downstream).
            _src = frame_payload.get("heart_rate_source") or frame_payload.get("source")
            if not _src:
                _src = _infer_wearable_source_from_node(effective_node)
            if _src:
                sensors["heart_rate_source"] = _src
            # Sample timestamp (Fix #1): use the device-supplied ts when
            # present, fall back to 0.0 for lagging/cloud sources that
            # forgot to stamp (so the freshness gate blocks them), and
            # arrival-stamp only for live wearables / unknown senders.
            sensors["heart_rate_sample_ts"] = _resolve_sample_ts(
                frame_payload,
                source=str(_src or ""),
                ts_keys=(
                    "heart_rate_sample_ts",
                    "sample_ts",
                    "ts",
                    "timestamp",
                ),
            )
    elif event_type == "spo2":
        val = frame_payload.get("current") or frame_payload.get("spo2") or frame_payload.get("value")
        if val is not None:
            sensors["spo2_pct"] = val
            _src = frame_payload.get("spo2_source") or frame_payload.get("source")
            if not _src:
                _src = _infer_wearable_source_from_node(effective_node)
            if _src:
                sensors["spo2_source"] = _src
            sensors["spo2_sample_ts"] = _resolve_sample_ts(
                frame_payload,
                source=str(_src or ""),
                ts_keys=(
                    "spo2_sample_ts",
                    "sample_ts",
                    "ts",
                    "timestamp",
                ),
            )
    elif event_type == "skin_temperature":
        val = frame_payload.get("celsius") or frame_payload.get("value")
        if val is not None:
            sensors["skin_temperature_c"] = val
            # Skin temperature carries the same envelope ts in HUP v1.1;
            # propagate it so downstream consumers can age-check the
            # reading the same way HR/SpO2 are aged. No baseline gate
            # for skin_temp_c so source inference is informational only.
            _src = (
                frame_payload.get("skin_temperature_source")
                or frame_payload.get("source")
                or _infer_wearable_source_from_node(effective_node)
            )
            sensors["skin_temperature_sample_ts"] = _resolve_sample_ts(
                frame_payload,
                source=str(_src or ""),
                ts_keys=(
                    "skin_temperature_sample_ts",
                    "sample_ts",
                    "ts",
                    "timestamp",
                ),
            )
    elif event_type == "steps":
        val = frame_payload.get("count") or frame_payload.get("value")
        if val is not None:
            sensors["steps"] = val
    elif event_type == "temperature":
        val = frame_payload.get("celsius") or frame_payload.get("value")
        if val is not None:
            sensors["temperature"] = val
    elif event_type == "accelerometer":
        accel = [
            frame_payload.get("x", 0.0),
            frame_payload.get("y", 0.0),
            frame_payload.get("z", 0.0),
        ]
        sensors["accel_xyz"] = accel
    elif event_type == "gesture":
        # Route straight to the gesture pipeline. No baseline recording.
        gesture = frame_payload.get("gesture") or frame_payload.get("name") or ""
        if gesture and effective_node:
            for sid in state.get_sessions_for_daemon(effective_node):
                state.perception.update_gesture(sid, gesture)
        return

    if not sensors:
        logger.debug(
            "Dropping device_event %r from %s — could not extract a value from %r",
            event_type, effective_node, frame_payload,
        )
        return

    if effective_node:
        for sid in state.get_sessions_for_daemon(effective_node):
            state.perception.update_sensors(sid, sensors)
            if state.somatic_engine:
                state.somatic_engine.update_from_perception_frame(sid, sensors)

    _record_biometrics_to_baseline(sensors)
    # Purely additive (operator report 2026-06-13): append the raw
    # reading to the durable biometric time-series so the health
    # summary can answer "over the last week how were my vitals?" from
    # the glasses stream alone. Runs AFTER the baseline record so it
    # can never perturb the freshness / per-source / wearable-priority
    # logic the baseline path depends on.
    _record_biometrics_to_history(sensors, effective_node)


# Vitals that must clear the live-wearable + freshness gate before they
# train a baseline, mapped to the (source_key, sample_ts_key) carried
# alongside them in the sensor payload. Activity totals (steps/calories)
# are intentionally absent — they're cumulative daily counters, not
# point-in-time vitals, so a stale push doesn't distort their baseline.
_GATED_BASELINE_VITALS = {
    "heart_rate": ("heart_rate_source", "heart_rate_sample_ts"),
    "ppg_heart_rate": ("heart_rate_source", "heart_rate_sample_ts"),
    "spo2": ("spo2_source", "spo2_sample_ts"),
    "spo2_pct": ("spo2_source", "spo2_sample_ts"),
}

# Same 120 s window perception.fusion / the dashboard "current" slot use.
# Hardcoded (not imported) so each gate stays independently auditable;
# see proactive_engine._FRESH_WINDOW_S for the canonical rationale.
_BASELINE_FRESH_WINDOW_S = 120.0


def _record_biometrics_to_baseline(data: dict) -> None:
    """Extract known biometric keys from a sensor payload and record them.

    Freshness + source gate (operator report 2026-06-07): the baseline
    previously recorded EVERY numeric biometric — including stale
    ``apple_healthkit`` "resting"/"last-measured" reads (e.g. HR=115 from
    a workout hours earlier) — straight into ``hr_resting``. That polluted
    the learned mean to a non-physiological ~100 bpm (real wearable
    samples were 51-56), so every fresh wristband read tripped a 2σ
    anomaly and the proactive layer fanned out duplicate "resting HR 51 /
    baseline 100" cards.

    Vitals now only train the baseline when BOTH:
      * the sample is fresh (``<= _BASELINE_FRESH_WINDOW_S``), and
      * the source is NOT a known lagging/cloud source.

    We exclude (not allow-list) because lagging sources like HealthKit
    resample ``sample_ts`` to "now" even when the underlying read is hours
    old — freshness alone can't catch them, which is the whole reason
    ``perception.fusion._LAGGING_SOURCES`` exists. Real wearables (Theora
    W300 / Veepoo / BLE PPG), including unlabeled local BLE daemons, still
    train the baseline; only cloud/HealthKit reads are kept out.
    """
    if not state.baseline_engine or not data:
        return
    try:
        from perception.fusion import _is_lagging_source, _is_live_wearable

        now = time.time()
        flat: dict[str, float] = {}
        for key, val in data.items():
            if isinstance(val, dict):
                for k2, v2 in val.items():
                    if isinstance(v2, (int, float)) and v2 > 0:
                        flat[k2] = float(v2)
            elif isinstance(val, (int, float)) and val > 0:
                flat[key] = float(val)

        for raw_key, value in flat.items():
            mapping = _BIOMETRIC_KEY_MAP.get(raw_key)
            if not mapping:
                continue
            src = ""
            gate = _GATED_BASELINE_VITALS.get(raw_key)
            if gate:
                src_key, ts_key = gate
                src = str(data.get(src_key, "") or "")
                ts_raw = data.get(ts_key, 0.0)
                ts = float(ts_raw) if isinstance(ts_raw, (int, float)) else 0.0
                fresh = ts > 0 and (now - ts) <= _BASELINE_FRESH_WINDOW_S
                if _is_lagging_source(src) or not fresh:
                    logger.debug(
                        "Skipping baseline record for %s=%.1f — source=%r fresh=%s "
                        "(lagging/cloud or stale vitals do not train the baseline)",
                        raw_key, value, src, fresh,
                    )
                    continue
            metric_id, category = mapping
            # Always write the bare metric row so legacy queries
            # (`check_anomaly("hr_resting", v)`) keep working
            # untouched — back-compat with old baselines on disk
            # and with the Whoop / Oura aggregator paths that don't
            # carry a wearable source.
            state.baseline_engine.record(metric_id, value, category=category)
            # Fix #5: per-source namespaced row. Only known live
            # wearable sources get their own series so unknown /
            # untagged daemons don't fragment the baseline space.
            if (
                metric_id in _BASELINE_PER_SOURCE_VITALS
                and _is_live_wearable(src)
            ):
                src_norm = src.strip().lower()
                state.baseline_engine.record(
                    f"{metric_id}:{src_norm}",
                    value,
                    category=category,
                )
    except Exception as exc:
        logger.debug("Baseline biometric recording error: %s", exc)


# Raw sensor key → (canonical_metric, source_key, sample_ts_key) for the
# durable biometric time-series. ``source_key`` / ``sample_ts_key`` are
# the sibling fields the biometric handler already stamps alongside the
# value; ``None`` means the event type doesn't carry them (steps /
# body temperature), in which case the source is inferred from the
# emitting node's advertised wearable capability.
_HISTORY_METRIC_MAP = {
    "ppg_heart_rate": ("hr", "heart_rate_source", "heart_rate_sample_ts"),
    "heart_rate": ("hr", "heart_rate_source", "heart_rate_sample_ts"),
    "spo2_pct": ("spo2", "spo2_source", "spo2_sample_ts"),
    "spo2": ("spo2", "spo2_source", "spo2_sample_ts"),
    "skin_temperature_c": (
        "skin_temp", "skin_temperature_source", "skin_temperature_sample_ts",
    ),
    "skin_temp_c": (
        "skin_temp", "skin_temperature_source", "skin_temperature_sample_ts",
    ),
    "temperature": ("body_temp", None, None),
    "steps": ("steps", None, None),
}


def _record_biometrics_to_history(data: dict, effective_node: str = "") -> None:
    """Append live-wearable biometric samples to the durable time-series.

    Operator report 2026-06-13: glasses HR/SpO2 fed the live snapshot
    and the rolling baseline but were NOT persisted as a queryable
    historical series, so "how were my vitals last week?" returned "no
    data" for every trend. This records each genuine wearable reading
    (W300 glasses, Veepoo wristband, any BLE PPG) into
    ``BaselineEngine.biometric_samples`` keyed by (ts, source, metric,
    value), which the health aggregator queries to build real
    week-over-week stats from the glasses ALONE.

    Cloud / lagging mirrors (HealthKit) are kept OUT of the wearable
    time-series: they already have their own trend via the Whoop/Oura
    aggregator branches and re-stamp stale reads, so persisting them
    here would pollute the glasses-derived trend. This mirrors the
    exclusion philosophy of ``_record_biometrics_to_baseline`` without
    touching it.
    """
    eng = state.baseline_engine
    if not eng or not data or not hasattr(eng, "record_sample"):
        return
    try:
        from perception.fusion import _is_lagging_source

        now = time.time()
        for raw_key, (metric, src_key, ts_key) in _HISTORY_METRIC_MAP.items():
            if raw_key not in data:
                continue
            value = data.get(raw_key)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            if value <= 0:
                continue
            src = str(data.get(src_key, "") or "") if src_key else ""
            if not src:
                src = _infer_wearable_source_from_node(effective_node) or ""
            if _is_lagging_source(src):
                continue
            ts_raw = data.get(ts_key, 0.0) if ts_key else 0.0
            ts = (
                float(ts_raw)
                if isinstance(ts_raw, (int, float)) and ts_raw
                else now
            )
            eng.record_sample(metric, float(value), source=src, ts=ts)
    except Exception as exc:
        logger.debug("Biometric history recording error: %s", exc)


# ─────────────────────────────────────────────
# Background Scene Analysis
# ─────────────────────────────────────────────

async def _analyze_scene_background(
    node_id: str, frame_payload: dict, mode: str = "general", query: str = "",
):
    """Run VLM scene analysis on a vision frame and update perception."""
    try:
        data_b64 = frame_payload.get("data_b64", "")
        encoding = frame_payload.get("encoding", "jpeg")
        if not data_b64:
            return

        result = await state.scene.analyze_frame(
            data_b64=data_b64, encoding=encoding, node_id=node_id,
            force=True, mode=mode, query=query,
        )
        if result:
            for sid in state.get_sessions_for_daemon(node_id):
                frame = state.perception.get_frame(sid)
                frame.scene_description = result.get("scene_description", result.get("answer", ""))
                frame.detected_objects = result.get("detected_objects", [])
                frame.text_in_scene = result.get("text_in_scene", [])

                if mode == "query" and query:
                    answer = result.get("answer", result.get("scene_description", ""))
                    if answer and state.orchestrator:
                        from models.protocol import FeralMessage, TextResponsePayload
                        await state.send_to_session(sid, FeralMessage(
                            session_id=sid, hop="brain", type="text_response",
                            payload=TextResponsePayload(text=f"[Vision] {answer}").model_dump(),
                        ))
    except Exception as e:
        logger.warning(f"Background scene analysis failed: {e}")


# ─────────────────────────────────────────────
# Bundled Web UI
# ─────────────────────────────────────────────
#
# v2 (feral-client-v2) is the default UI. When ``webui_v2/index.html`` is on
# disk the Brain serves it at / directly, and v1 (``webui/``) is never
# reached. If webui_v2/ isn't built (fresh clone), fall back to v1 so users
# still see something. v1 source is kept in the tree for history only.
#
# The directory is named ``webui_v2`` (underscore) so setuptools treats it
# as a real Python package — without that, ``pip install feral-ai`` ships a
# wheel missing the v2 bundle and the fallback kicks in on end-user machines.
# See feral-core/pyproject.toml [tool.setuptools.package-data] for the mirror.
#
# The ``/v2/`` alias is retained so existing bookmarks keep working even
# when v2 is already the default at /.

_webui_v2_dir = Path(__file__).parent.parent / "webui_v2"
_webui_legacy_dir = Path(__file__).parent.parent / "webui"
_webui_v2_ready = _webui_v2_dir.is_dir() and (_webui_v2_dir / "index.html").exists()
_webui_legacy_ready = _webui_legacy_dir.is_dir() and (_webui_legacy_dir / "index.html").exists()

_webui_dir = _webui_v2_dir if _webui_v2_ready else _webui_legacy_dir
_webui_ready = _webui_v2_ready or _webui_legacy_ready
_webui_variant = "v2" if _webui_v2_ready else ("v1-legacy" if _webui_legacy_ready else "missing")
_webui_route_mode = "spa" if _webui_ready else "fallback"
logger.info("Web UI routing mode=%s variant=%s path=%s", _webui_route_mode, _webui_variant, _webui_dir)

if _webui_ready and (_webui_dir / "assets").is_dir():
    from starlette.staticfiles import StaticFiles
    app.mount("/assets", StaticFiles(directory=str(_webui_dir / "assets")), name="webui-assets")
    logger.info(f"Web UI ({_webui_variant}) bundled from {_webui_dir} — open {brain_public_base_url()}")
else:
    logger.warning(
        f"Web UI not found at {_webui_dir}. Dashboard will show setup instructions. "
        "Run 'make bundle-webui' to build the dashboard."
    )

# Keep the /v2/ alias so ``http://host/v2/`` still resolves when v2 is
# already the default at /. Harmless: both paths end up serving the same
# bundle because feral-client-v2 uses relative asset URLs.
if _webui_v2_ready:
    from starlette.staticfiles import StaticFiles

    # v2026.5.29 — Starlette's StaticFiles mount answers 404 on missing
    # files inside the mount and does NOT fall through to the root
    # catch-all's PWA basename special-case. So deep SPA URLs that
    # resolve `./manifest.webmanifest` to `/v2/chat/manifest.webmanifest`
    # would 404 even after the v2026.5.28 catch-all fix. Register
    # explicit routes for the PWA bundle names BEFORE the mount so they
    # match first regardless of subpath.
    def _v2_manifest_response():
        canonical = _webui_v2_dir / "manifest.webmanifest"
        if canonical.is_file():
            return FileResponse(canonical, media_type="application/manifest+json")
        # Fall back to the shared /webui bundle copy when missing.
        shared = _webui_dir / "manifest.webmanifest"
        if shared.is_file():
            return FileResponse(shared, media_type="application/manifest+json")
        raise HTTPException(status_code=404, detail="manifest.webmanifest not bundled")

    def _v2_sw_response():
        canonical = _webui_v2_dir / "sw.js"
        if canonical.is_file():
            return FileResponse(canonical, media_type="application/javascript")
        raise HTTPException(status_code=404, detail="sw.js not bundled")

    @app.get("/v2/manifest.webmanifest")
    async def _v2_manifest_root():
        return _v2_manifest_response()

    @app.get("/v2/{subpath:path}/manifest.webmanifest")
    async def _v2_manifest_deep(subpath: str):  # noqa: ARG001
        return _v2_manifest_response()

    @app.get("/v2/sw.js")
    async def _v2_sw_root():
        return _v2_sw_response()

    @app.get("/v2/{subpath:path}/sw.js")
    async def _v2_sw_deep(subpath: str):  # noqa: ARG001
        return _v2_sw_response()

    app.mount("/v2", StaticFiles(directory=str(_webui_v2_dir), html=True), name="webui-v2")
    logger.info(f"Web UI v2 alias also available at {brain_public_base_url()}/v2/")

_FALLBACK_HTML = """<!DOCTYPE html>
<html><head><title>FERAL Brain</title>
<style>body{font-family:system-ui;background:#0a0a0a;color:#e0e0e0;display:flex;align-items:center;
justify-content:center;min-height:100vh;margin:0;padding:2rem}
.card{background:#141414;border:1px solid #222;border-radius:16px;padding:2.5rem;max-width:520px;text-align:center}
h1{color:#06b6d4;margin-bottom:.5rem}code{background:#1a1a1a;padding:.2em .5em;border-radius:4px;font-size:.85em}
a{color:#06b6d4}p{line-height:1.6}</style></head>
<body><div class="card">
<h1>FERAL Brain is Running</h1>
<p>The API is active, but the web dashboard is not bundled in this install.</p>
<p style="margin-top:1.5rem"><strong>Quick fix — reinstall with the dashboard:</strong></p>
<ol style="text-align:left;line-height:2">
<li>Clone: <code>git clone https://github.com/FERAL-AI/FERAL-AI.git</code></li>
<li>Build UI: <code>cd FERAL-AI && make bundle-webui</code></li>
<li>Install: <code>pip install -e feral-core[llm]</code></li>
<li>Restart: <code>feral serve</code></li>
</ol>
<p style="margin-top:1rem;opacity:.6">Or use the CLI directly: <code>feral start</code></p>
<p style="margin-top:1.5rem"><a href="/docs">API Docs</a> &middot;
<a href="/api/config">Config</a> &middot;
<a href="/skills">Skills</a> &middot;
<a href="/health">Health</a></p>
</div></body></html>"""


@app.get("/setup/legacy")
async def setup_legacy_redirect():
    """Hard-redirect the deleted /setup/legacy route to /setup.

    The legacy wizard (SetupWizard.jsx) was removed in 2026.5.8.
    A server-side 301 (rather than the App.jsx <Navigate>) is required
    because the bundled UI uses relative asset paths (Vite ``base: './'``
    so the /v2/ alias works), which means depth-2 SPA routes can't
    boot React on a direct URL load — assets resolve to /setup/assets/*
    which doesn't exist. The redirect bypasses that entirely.
    """
    return RedirectResponse(url="/setup", status_code=301)


# v2026.5.28 — PWA bundle files that MUST be served from the bundle
# root regardless of the requested subpath. The built `webui_v2/index.html`
# uses a relative href (`./manifest.webmanifest`) because Vite is
# configured with `base: './'` to keep the `/v2/` alias compatible
# with the canonical mount at `/`. When the SPA is on a deep route
# like `/chat/`, the browser resolves `./manifest.webmanifest` to
# `/chat/manifest.webmanifest`. Without this special-case the catch-all
# fell through to `index.html`, the browser tried to parse HTML as
# JSON, and the console showed `Manifest: Line: 1, column: 1, Syntax
# error.` The same trap exists for `sw.js` (service-worker registration).
_PWA_BUNDLE_BASENAMES = {"manifest.webmanifest", "sw.js"}
_PWA_BUNDLE_CONTENT_TYPES = {
    "manifest.webmanifest": "application/manifest+json",
    "sw.js": "application/javascript",
}


@app.get("/{full_path:path}")
async def serve_webui_or_fallback(full_path: str = ""):
    # Honest 404 for unknown API and protocol paths. Until this guard
    # was added the catch-all returned 200 SPA HTML for any unknown
    # ``/api/...`` GET, which silently broke SDKs that polled missing
    # endpoints (parsers crashed on HTML; flows hung indefinitely).
    if (
        full_path.startswith("api/")
        or full_path.startswith("v1/")
        or full_path.startswith("v2/api/")
    ):
        raise HTTPException(
            status_code=404,
            detail={"code": "no_such_route", "path": "/" + full_path},
        )
    if _webui_ready:
        # Serve PWA bundle files (manifest.webmanifest / sw.js) from
        # the bundle root regardless of the requested subpath, with
        # the right content-type so the browser parses them as JSON /
        # JS instead of throwing the "Line 1 col 1 syntax error" the
        # operator saw on deep SPA routes.
        basename = full_path.rsplit("/", 1)[-1] if full_path else ""
        if basename in _PWA_BUNDLE_BASENAMES:
            canonical = _webui_dir / basename
            if canonical.is_file():
                return FileResponse(
                    canonical,
                    media_type=_PWA_BUNDLE_CONTENT_TYPES.get(basename),
                )

        file_path = (_webui_dir / full_path).resolve()
        if not file_path.is_relative_to(_webui_dir.resolve()):
            return HTMLResponse("Forbidden", status_code=403)
        if full_path and file_path.is_file():
            return FileResponse(file_path)
        return FileResponse(_webui_dir / "index.html")
    return HTMLResponse(_FALLBACK_HTML)


def _assert_allowlist_routes_exist(target_app) -> None:
    """audit-r12 D1 startup invariant: every entry in every API-key
    allowlist MUST resolve to at least one route registered on
    ``target_app``.

    Pre-r12, drift between allowlist literals and the real route table
    (``/api/ambient/digest`` after the ambient route was renamed to
    ``/api/ambient/briefing``; ``/api/approvals/approve`` after the
    approvals route was parameterised to
    ``/api/approvals/{request_id}/approve``; a couple of POST entries
    that pointed at GET-only endpoints) silently 401'd iOS clients.
    No unit test caught it because nobody was iterating the route
    table against the allowlist.

    This invariant runs once, at module import, AFTER every
    ``app.include_router(...)`` and every top-level ``@app.<method>``
    decorator has registered its route. If anything has drifted, it
    raises ``RuntimeError`` with the operator-facing fix list — the
    brain refuses to boot rather than ship a half-broken auth
    surface.

    Scope: literals in ``_OPEN_PATHS`` and every entry of
    ``_PHONE_BEARER_GET`` / ``_PHONE_BEARER_POST``.

    Excluded by design:

    * ``_OPEN_GET_PATHS`` — these paths (``/pair``, ``/sw.js``,
      ``/favicon.ico``, ``/manifest.webmanifest``, ``/v2/pair``) are
      served via the SPA catch-all at the bottom of this module
      (``@app.get("/{full_path:path}")``); they have no explicit
      route registration by design, and would be false positives here.
    * ``_OPEN_PATH_PREFIXES`` / ``_OPEN_GET_PATH_PREFIXES`` — these
      cover static-asset mounts whose Starlette ``route.path`` shape
      (mount root, no trailing slash) doesn't lend itself to the
      same exact-match test. The phone-bearer surface is where drift
      causes silent failures; static-asset drift surfaces immediately
      in browser DevTools.
    """
    registered_paths: set[str] = set()
    for route in target_app.routes:
        path = getattr(route, "path", None)
        if path:
            registered_paths.add(path)

    errors: list[str] = []

    def _check_literal(allowlist_name: str, path: str) -> None:
        if path not in registered_paths:
            errors.append(
                f"{allowlist_name}: literal {path!r} is not registered "
                "on the app (typo or route was renamed)"
            )

    def _check_prefix(allowlist_name: str, prefix: str) -> None:
        if not any(p.startswith(prefix) for p in registered_paths):
            errors.append(
                f"{allowlist_name}: prefix {prefix!r} matches no "
                "registered route (typo or routes moved out)"
            )

    def _check_pattern(allowlist_name: str, pattern: str) -> None:
        # FastAPI keeps ``{param}`` in ``route.path``, so exact-string
        # match against any registered path proves the pattern is wired.
        if pattern not in registered_paths:
            errors.append(
                f"{allowlist_name}: pattern {pattern!r} is not registered "
                "on the app (typo or route was renamed)"
            )

    for _path in _OPEN_PATHS:
        _check_literal("_OPEN_PATHS", _path)
    for _path in _PHONE_BEARER_GET.literals():
        _check_literal("_PHONE_BEARER_GET", _path)
    for _prefix in _PHONE_BEARER_GET.prefixes():
        _check_prefix("_PHONE_BEARER_GET", _prefix)
    for _pattern in _PHONE_BEARER_GET.patterns():
        _check_pattern("_PHONE_BEARER_GET", _pattern)
    for _path in _PHONE_BEARER_POST.literals():
        _check_literal("_PHONE_BEARER_POST", _path)
    for _prefix in _PHONE_BEARER_POST.prefixes():
        _check_prefix("_PHONE_BEARER_POST", _prefix)
    for _pattern in _PHONE_BEARER_POST.patterns():
        _check_pattern("_PHONE_BEARER_POST", _pattern)

    if errors:
        raise RuntimeError(
            "API auth allowlist drift detected at boot. These entries "
            "do not match any registered route, which means iOS / web "
            "clients calling these paths receive 401 even with valid "
            "credentials. Fix each entry below, or update the "
            "allowlist in `api/server.py` to match the canonical route "
            "path:\n  - " + "\n  - ".join(errors)
        )


_assert_allowlist_routes_exist(app)


if __name__ == "__main__":
    import uvicorn
    print(f"""
    ╔══════════════════════════════════════╗
    ║        FERAL v{__version__:<22s}║
    ║   Open AI Agent · Computer Use      ║
    ║   Voice · GenUI · Hardware          ║
    ╚══════════════════════════════════════╝
    """)
    uvicorn.run(app, host=brain_bind_host(), port=brain_port(), log_level="info")
