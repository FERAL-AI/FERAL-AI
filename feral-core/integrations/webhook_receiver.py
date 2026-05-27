"""
FERAL Webhook Receiver & Event Bus
=====================================
Handles incoming webhooks from external apps (Home Assistant,
Notion, Stripe, GitHub, etc.) and routes them through an internal
event bus that can trigger skills, update memory, or notify users.
"""

from __future__ import annotations
import hashlib
import hmac
import json
import logging
import time
from typing import Callable, Awaitable, Optional, Any
from dataclasses import dataclass, field

logger = logging.getLogger("feral.webhooks")


@dataclass
class WebhookEvent:
    """Normalized event from an external app."""
    app_id: str
    event_type: str
    payload: dict
    timestamp: float = field(default_factory=time.time)
    raw_headers: dict = field(default_factory=dict)
    verified: bool = False


@dataclass
class WebhookConfig:
    """Configuration for a registered webhook endpoint."""
    app_id: str
    secret: str = ""
    signature_header: str = ""
    signature_prefix: str = ""
    hash_algorithm: str = "sha256"
    enabled: bool = True


EventHandler = Callable[[WebhookEvent], Awaitable[None]]


def _normalize_headers(headers: dict) -> dict:
    """Return a lowercase-keyed copy of ``headers`` so signature and
    event-type lookups are case-insensitive regardless of where the
    headers came from (FastAPI's ``Request.headers`` is case-
    insensitive; raw dicts from tests are not)."""
    if headers is None:
        return {}
    return {str(k).lower(): v for k, v in headers.items()}


class EventBus:
    """
    Internal event bus that routes WebhookEvents to registered handlers.
    Handlers can be skill executors, memory updaters, or user notifiers.
    """

    def __init__(self):
        self._handlers: dict[str, list[EventHandler]] = {}
        self._global_handlers: list[EventHandler] = []
        self._event_log: list[dict] = []
        self._max_log = 200

    def on(self, app_id: str, handler: EventHandler):
        """Register a handler for events from a specific app."""
        if app_id not in self._handlers:
            self._handlers[app_id] = []
        self._handlers[app_id].append(handler)

    def on_all(self, handler: EventHandler):
        """Register a handler for all events."""
        self._global_handlers.append(handler)

    async def emit(self, event: WebhookEvent):
        """Route an event to all matching handlers."""
        self._log_event(event)

        handlers = self._handlers.get(event.app_id, []) + self._global_handlers
        for handler in handlers:
            try:
                await handler(event)
            except Exception as e:
                logger.error(f"Event handler error [{event.app_id}/{event.event_type}]: {e}")

    def _log_event(self, event: WebhookEvent):
        self._event_log.append({
            "app_id": event.app_id,
            "event_type": event.event_type,
            "timestamp": event.timestamp,
            "verified": event.verified,
        })
        if len(self._event_log) > self._max_log:
            self._event_log = self._event_log[-self._max_log:]

    def recent_events(self, limit: int = 20) -> list[dict]:
        return self._event_log[-limit:]

    def stats(self) -> dict:
        return {
            "registered_apps": list(self._handlers.keys()),
            "global_handlers": len(self._global_handlers),
            "total_events": len(self._event_log),
        }


def _default_integration_configs() -> dict[str, WebhookConfig]:
    """Stub configs the receiver seeds when the persistent store is
    empty. Operator secrets are NOT baked in here — they're written
    via ``set_secret_persistent`` / the PUT config route."""
    return {
        "home_assistant": WebhookConfig(
            app_id="home_assistant",
            signature_header="",
            enabled=True,
        ),
        "notion": WebhookConfig(
            app_id="notion",
            signature_header="",
            enabled=True,
        ),
        "github": WebhookConfig(
            app_id="github",
            signature_header="X-Hub-Signature-256",
            signature_prefix="sha256=",
            hash_algorithm="sha256",
            enabled=True,
        ),
        "stripe": WebhookConfig(
            app_id="stripe",
            signature_header="Stripe-Signature",
            enabled=True,
        ),
    }


class WebhookReceiver:
    """
    Validates and processes incoming webhook HTTP requests.
    Verifies HMAC signatures when configured, normalizes events,
    and publishes them to the EventBus.

    W19 (finding-19 cross-cut): per-app configs now live in the
    process-local cache ``self._configs`` **backed by** an optional
    :class:`integrations.webhook_store.WebhookStore`. When a store is
    wired and :meth:`hydrate_from_store` is awaited at boot:

    * Existing rows are loaded into the cache (operator secrets survive
      brain restarts).
    * Missing default integrations (home_assistant/notion/github/stripe)
      are seeded into the store **without overwriting** any existing
      row — i.e. an operator-edited GitHub secret keeps its value.

    The sync ``register_webhook`` / ``set_secret`` API is preserved
    for the existing in-process callers (test fixtures, code paths
    that never need durable storage). New persistent surfaces should
    use the async :meth:`upsert_config` / :meth:`set_secret_persistent`
    methods which write through to the store **and** update the cache.
    The hot path (``handle_request``) is unchanged: it reads from the
    cache only, so per-request latency stays sync.
    """

    def __init__(self, event_bus: EventBus, store: Optional[Any] = None):
        self._bus = event_bus
        self._store = store
        self._configs: dict[str, WebhookConfig] = {}
        # Seed the in-memory cache with defaults so callers that don't
        # ``await hydrate_from_store`` (e.g. existing unit tests with
        # no store) still see the four known integrations.
        self._configs.update(_default_integration_configs())

    @property
    def store(self):
        return self._store

    async def hydrate_from_store(self) -> None:
        """Boot helper: load persisted integration configs into the
        cache and seed defaults into the store on first run.

        Safe to call multiple times; later calls re-read the DB
        snapshot. If no store is wired this is a no-op (defaults
        already in the cache from ``__init__``).
        """
        if self._store is None:
            return
        existing = await self._store.list_integrations()
        existing_by_app = {c.app_id: c for c in existing}
        for app_id, default_cfg in _default_integration_configs().items():
            if app_id not in existing_by_app:
                await self._store.upsert_integration(default_cfg)
                existing_by_app[app_id] = default_cfg
        # Cache = union of defaults (so unknown-to-store apps the
        # operator added in-process still resolve) and store rows
        # (operator-edited secrets win).
        self._configs = dict(_default_integration_configs())
        self._configs.update(existing_by_app)

    def register_webhook(self, config: WebhookConfig):
        """In-memory cache update. For durable persistence use
        :meth:`upsert_config`."""
        self._configs[config.app_id] = config

    def set_secret(self, app_id: str, secret: str):
        """In-memory cache mutation only. For durable persistence use
        :meth:`set_secret_persistent`."""
        if app_id in self._configs:
            self._configs[app_id].secret = secret
        else:
            self._configs[app_id] = WebhookConfig(app_id=app_id, secret=secret)

    async def upsert_config(self, config: WebhookConfig) -> WebhookConfig:
        """Persist a config to the store (if wired) and update the
        cache. Returns the cached config."""
        self._configs[config.app_id] = config
        if self._store is not None:
            await self._store.upsert_integration(config)
        return config

    async def set_secret_persistent(self, app_id: str, secret: str) -> WebhookConfig:
        """Set the HMAC secret for ``app_id`` and persist via the
        store. Preserves any pre-existing signature_header / prefix /
        algorithm on the cached entry."""
        existing = self._configs.get(app_id)
        if existing is None:
            existing = WebhookConfig(app_id=app_id, secret=secret)
        else:
            existing.secret = secret
        self._configs[app_id] = existing
        if self._store is not None:
            await self._store.upsert_integration(existing)
        return existing

    async def handle_request(
        self,
        app_id: str,
        body: bytes,
        headers: dict,
        content_type: str = "application/json",
    ) -> dict:
        """
        Process an incoming webhook request.

        **Fail-closed** when a secret is configured: a missing or
        invalid signature returns ``{accepted: False, reason: ...}``
        instead of accepting the request with ``verified: False``.
        Pre-Lane-10 the receiver always emitted the event into the bus
        regardless of signature validity, which let unauthenticated
        callers trigger downstream handlers as long as they knew an
        ``app_id``. Finding 19: "bad sig still **accepted** with
        ``verified=False``."

        Returns one of:
          * ``{accepted: True, event_type, verified: bool}`` — happy
            path. ``verified`` reflects whether a valid signature was
            present (always ``True`` when a secret is configured;
            ``True`` for unsigned providers without a secret).
          * ``{accepted: False, reason, error}`` — rejected. ``reason``
            is one of ``unknown_app``, ``missing_signature``,
            ``invalid_signature``.
        """
        config = self._configs.get(app_id)
        if not config or not config.enabled:
            return {
                "accepted": False,
                "reason": "unknown_app",
                "error": f"Unknown or disabled webhook: {app_id}",
            }

        norm_headers = _normalize_headers(headers)
        verify_decision = self._verify_signature(config, body, norm_headers)
        if verify_decision == "missing_signature":
            return {
                "accepted": False,
                "reason": "missing_signature",
                "error": (
                    f"webhook {app_id}: secret configured but no "
                    f"{config.signature_header!r} header on request"
                ),
            }
        if verify_decision == "invalid_signature":
            return {
                "accepted": False,
                "reason": "invalid_signature",
                "error": (
                    f"webhook {app_id}: HMAC signature mismatch"
                ),
            }
        # ``unsigned`` providers (e.g. Home Assistant, internal Notion
        # webhooks without a secret) report ``verified=True`` because
        # there's nothing TO verify — the operator opted not to enforce
        # a signature for that app. The flag is reserved for "we have a
        # signature config and we ran it"; with no config there's no
        # security claim to make either way, so a downstream handler
        # that wants stricter behaviour can require a secret on the
        # registered ``WebhookConfig``.
        verified = verify_decision in ("verified", "unsigned")

        try:
            if content_type.startswith("application/json"):
                payload = json.loads(body)
            else:
                payload = {"raw": body.decode("utf-8", errors="replace")[:5000]}
        except json.JSONDecodeError:
            payload = {"raw": body.decode("utf-8", errors="replace")[:5000]}

        event_type = self._extract_event_type(app_id, payload, norm_headers)

        event = WebhookEvent(
            app_id=app_id,
            event_type=event_type,
            payload=payload,
            raw_headers={k: v for k, v in headers.items()
                         if k.lower().startswith("x-")},
            verified=verified,
        )

        await self._bus.emit(event)

        logger.info(f"Webhook [{app_id}] event={event_type} verified={verified}")
        return {"accepted": True, "event_type": event_type, "verified": verified}

    def _verify_signature(
        self, config: WebhookConfig, body: bytes, headers: dict,
    ) -> str:
        """Return one of:

        * ``"unsigned"`` — no secret/header configured; nothing to
          verify (verified=True downstream).
        * ``"verified"`` — signature header present and matches.
        * ``"missing_signature"`` — secret configured but no header on
          the request. Caller MUST reject.
        * ``"invalid_signature"`` — header present but HMAC mismatch.
          Caller MUST reject.
        """
        if not config.secret or not config.signature_header:
            return "unsigned"

        sig_header = headers.get(config.signature_header.lower(), "")
        if not sig_header:
            return "missing_signature"

        expected_sig = sig_header
        if config.signature_prefix and expected_sig.startswith(config.signature_prefix):
            expected_sig = expected_sig[len(config.signature_prefix):]

        if config.hash_algorithm == "sha256":
            computed = hmac.new(
                config.secret.encode(), body, hashlib.sha256,
            ).hexdigest()
        elif config.hash_algorithm == "sha1":
            computed = hmac.new(
                config.secret.encode(), body, hashlib.sha1,
            ).hexdigest()
        else:
            return "invalid_signature"

        return "verified" if hmac.compare_digest(computed, expected_sig) else "invalid_signature"

    def _extract_event_type(self, app_id: str, payload: dict, headers: dict) -> str:
        # ``headers`` is the normalized lowercase view returned by
        # ``_normalize_headers``. We look up each header in lowercase
        # so the function works regardless of how the caller cased
        # them coming in.
        if app_id == "github":
            return headers.get("x-github-event", "unknown")
        elif app_id == "stripe":
            return payload.get("type", "unknown")
        elif app_id == "home_assistant":
            return payload.get("event_type", payload.get("type", "state_changed"))
        elif app_id == "notion":
            return payload.get("type", "page_updated")
        return payload.get("event", payload.get("type", "unknown"))

    def list_webhooks(self) -> list[dict]:
        return [
            {
                "app_id": c.app_id,
                "enabled": c.enabled,
                "has_secret": bool(c.secret),
                "signature_header": c.signature_header,
            }
            for c in self._configs.values()
        ]
