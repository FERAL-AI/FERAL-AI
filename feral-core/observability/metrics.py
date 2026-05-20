"""FERAL observability metrics surface.

Two layers live here:

1. **Legacy in-process layer** (``increment``/``observe``/``measure``/
   ``in_memory_snapshot``). Optional OpenTelemetry meter; falls back to
   a process-local counter dict. Predates W13 — kept verbatim so the
   dozens of call sites scattered across the codebase keep working.

2. **W13 Prometheus REGISTRY + ``emit()``** (added in PR #36, roadmap
   §3.1 #4). A single :class:`prometheus_client.CollectorRegistry`
   singleton holds every metric the default Grafana dashboard
   (``ops/grafana/feral-overview.json``) and Prometheus alert rules
   (``ops/prometheus/alerts.yml``) reference. ``emit()`` is the
   uniform write API; it no-ops when ``FERAL_METRICS_ENDPOINT`` is
   explicitly turned off, so individual call sites can land without
   gating logic of their own.

The W13 charter intentionally only wires ONE call site
(``api/server.py``'s rate-limit middleware, for
``feral_http_requests_total``). The rest — sync, MCP, LLM provider,
supervisor, sandbox, vault — are tracked as W13.1 follow-ups so each
owning workstream adds its own ``emit()`` calls inside its own PR.
The metric definitions below document which workstream owns the
eventual call site.
"""
from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Mapping

logger = logging.getLogger("feral.observability")

# ───────────────────────────────────────────────────────────────────
# Legacy in-process metrics (unchanged from pre-W13)
# ───────────────────────────────────────────────────────────────────

_OTEL_ENABLED = False
_meter = None
_counters = {}
_histograms = {}
_inmem = {"counters": defaultdict(int), "histograms": defaultdict(list)}


def init_metrics(service_name: str = "feral"):
    """Initialize OpenTelemetry metrics if opentelemetry-sdk is installed."""
    global _OTEL_ENABLED, _meter
    try:
        from opentelemetry import metrics
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader, ConsoleMetricExporter
        from opentelemetry.sdk.resources import Resource

        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
        readers = []
        if endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
                readers.append(PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=endpoint)))
            except ImportError:
                pass
        if os.environ.get("FERAL_METRICS_CONSOLE") == "1":
            readers.append(PeriodicExportingMetricReader(ConsoleMetricExporter()))

        if readers:
            provider = MeterProvider(resource=Resource.create({"service.name": service_name}), metric_readers=readers)
            metrics.set_meter_provider(provider)
            _meter = metrics.get_meter(service_name)
            _OTEL_ENABLED = True
            logger.info("OpenTelemetry metrics initialized (readers=%d)", len(readers))
    except ImportError:
        logger.debug("opentelemetry-sdk not installed; using in-memory metrics")


def increment(name: str, by: int = 1, attributes: dict = None):
    """Increment a counter. Falls back to in-memory if OTel not available."""
    if _OTEL_ENABLED and _meter:
        c = _counters.get(name)
        if not c:
            c = _meter.create_counter(name)
            _counters[name] = c
        c.add(by, attributes or {})
    else:
        _inmem["counters"][name] += by


def observe(name: str, value: float, attributes: dict = None):
    """Record a value to a histogram."""
    if _OTEL_ENABLED and _meter:
        h = _histograms.get(name)
        if not h:
            h = _meter.create_histogram(name)
            _histograms[name] = h
        h.record(value, attributes or {})
    else:
        _inmem["histograms"][name].append(value)


@contextmanager
def measure(name: str, attributes: dict = None):
    """Context manager: record elapsed ms into histogram {name}_ms."""
    t0 = time.time()
    try:
        yield
    finally:
        observe(f"{name}_ms", (time.time() - t0) * 1000, attributes)


def in_memory_snapshot():
    """For tests and debugging — snapshot the in-mem metrics."""
    return {
        "counters": dict(_inmem["counters"]),
        "histograms": {k: {"count": len(v), "mean": sum(v) / max(len(v), 1)} for k, v in _inmem["histograms"].items()},
    }


def _reset_inmem():
    """Reset in-memory stores — for test isolation only."""
    _inmem["counters"].clear()
    _inmem["histograms"].clear()


# ───────────────────────────────────────────────────────────────────
# W13 — Prometheus registry + emit() helper
# ───────────────────────────────────────────────────────────────────
#
# Owned by W13 (roadmap §3.1 #4). REGISTRY is a dedicated
# CollectorRegistry instance so we never collide with the
# prometheus_client default registry the multiprocess collector or
# third-party libs may scribble into. Every metric the default
# Grafana dashboard + alert rules reference must be defined below;
# tests/test_metrics_registry.py enforces that contract.

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

REGISTRY: CollectorRegistry = CollectorRegistry(auto_describe=True)

# HTTP — emitted by feral-core/api/server.py's RateLimitMiddleware.
# Owned by W13.
HTTP_REQUESTS_TOTAL = Counter(
    "feral_http_requests_total",
    "Total HTTP requests served by the FERAL Brain, labelled by method, route template, and status class.",
    labelnames=("method", "route", "status"),
    registry=REGISTRY,
)
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "feral_http_request_duration_seconds",
    "HTTP request latency in seconds, labelled by method and route template.",
    labelnames=("method", "route"),
    registry=REGISTRY,
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# LLM — to be emitted by feral-core/agents/llm_provider.py.
# W13.1 follow-up owner: W19 (LLM provider hardening).
LLM_429_TOTAL = Counter(
    "feral_llm_429_total",
    "Provider rate-limit (HTTP 429) responses returned by upstream LLM APIs, labelled by provider id.",
    labelnames=("provider",),
    registry=REGISTRY,
)
LLM_FAILOVER_CHAIN_EXHAUSTED_TOTAL = Counter(
    "feral_llm_failover_chain_exhausted_total",
    "Number of LLM requests that fell through every configured provider in the failover chain.",
    registry=REGISTRY,
)

# Federated sync — to be emitted by feral-core/memory/sync.py.
# W13.1 follow-up owner: W11 (federated sync chaos).
SYNC_ACTIVE_PEERS = Gauge(
    "feral_sync_active_peers",
    "Current number of federated-sync peers the Brain has an open transport to.",
    registry=REGISTRY,
)
SYNC_FAILURES_TOTAL = Counter(
    "feral_sync_failures_total",
    "Federated-sync transport or apply-changes failures, labelled by failure reason.",
    labelnames=("reason",),
    registry=REGISTRY,
)
SYNC_WAS_ACTIVE_RECENT = Gauge(
    "feral_sync_was_active_recent",
    "1 if the sync engine has had at least one active peer in the recent window (default 24h), 0 otherwise. Used by the SyncPeerDown alert to suppress noise on single-node installs.",
    registry=REGISTRY,
)

# Supervisor — to be emitted by feral-core/agents/supervisor.py.
# W13.1 follow-up owner: W17 (supervisor / approval queue).
SUPERVISOR_APPROVAL_QUEUE = Gauge(
    "feral_supervisor_approval_queue",
    "Current depth of the supervisor's pending-approval queue.",
    registry=REGISTRY,
)

# Tool denials / sandbox — to be emitted by feral-core/security/sandbox_policy.py
# and the sandbox runner. W13.1 follow-up owner: W4 (sandbox / security policy).
TOOL_DENIALS_TOTAL = Counter(
    "feral_tool_denials_total",
    "Tool calls denied by policy before reaching the executor, labelled by tool id.",
    labelnames=("tool",),
    registry=REGISTRY,
)
SANDBOX_KILLS_TOTAL = Counter(
    "feral_sandbox_kills_total",
    "Sandbox processes killed by the runner (timeout, OOM, policy violation), labelled by reason.",
    labelnames=("reason",),
    registry=REGISTRY,
)

# Vault — to be emitted by feral-core/security/vault.py.
# W13.1 follow-up owner: W9 (vault encryption-at-rest).
VAULT_DECRYPT_ERRORS_TOTAL = Counter(
    "feral_vault_decrypt_errors_total",
    "Vault entries that failed AEAD decrypt — typically wrong key, tampered ciphertext, or schema drift.",
    registry=REGISTRY,
)

# WebSocket — to be emitted by feral-core/api/server.py's main client
# WS endpoint and the daemon endpoint. Single gauge, value =
# len(state.sessions). W13.1 follow-up owner: W17 / W13 sweep.
WS_ACTIVE_SESSIONS = Gauge(
    "feral_ws_active_sessions",
    "Current number of authenticated WebSocket sessions held open by the Brain.",
    registry=REGISTRY,
)

# PR 12 — Automation truthfulness metrics. Emitted by ToolRunner /
# BrowserController / GUIComputerUseSkill / CodingRunner so the
# operator can answer "is the agent silently failing?" from the
# Grafana dashboard without scraping logs.
AUTOMATION_BLOCKED_TOTAL = Counter(
    "feral_automation_blocked_total",
    "Tool calls blocked before execution by the safety resolver, labelled by tool and reason.",
    labelnames=("tool", "reason"),
    registry=REGISTRY,
)
AUTOMATION_FAILURE_TOTAL = Counter(
    "feral_automation_failure_total",
    "Automation actions that started but failed (browser, GUI, coding loop, voice tool), labelled by subsystem and reason.",
    labelnames=("subsystem", "reason"),
    registry=REGISTRY,
)
AUTOMATION_PERMISSION_DENIED_TOTAL = Counter(
    "feral_automation_permission_denied_total",
    "Local OS-level permission denials (macOS TCC, sandbox grant, OAuth scope missing).",
    labelnames=("permission",),
    registry=REGISTRY,
)
AUTOMATION_REPAIR_LOOP_TOTAL = Counter(
    "feral_automation_repair_loop_total",
    "CodingRun / GoalChecker repair iterations triggered, labelled by outcome (repaired, gave_up, max_iters).",
    labelnames=("outcome",),
    registry=REGISTRY,
)

# v2026.5.34 (PR 2) D11 — memory decay sweeper.
# Owned by memory/decay.py:MemoryDecayService.run_once().
MEMORY_DECAY_SWEEPS_TOTAL = Counter(
    "feral_memory_decay_sweeps_total",
    "Number of background decay sweeps that have completed.",
    registry=REGISTRY,
)
MEMORY_DECAY_SWEEP_DURATION_SECONDS = Histogram(
    "feral_memory_decay_sweep_duration_seconds",
    "Wall-clock duration of each decay sweep, in seconds.",
    registry=REGISTRY,
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)
MEMORY_EPISODES_ACTIVE = Gauge(
    "feral_memory_episodes_active",
    "Episodes currently not forgotten (forgotten_at IS NULL).",
    registry=REGISTRY,
)
MEMORY_EPISODES_FORGOTTEN = Gauge(
    "feral_memory_episodes_forgotten",
    "Episodes whose decay_factor crossed forget_threshold and are awaiting hard-delete.",
    registry=REGISTRY,
)
MEMORY_EPISODES_HARD_DELETED_TOTAL = Counter(
    "feral_memory_episodes_hard_deleted_total",
    "Episodes hard-deleted after retention_days past forgotten_at.",
    registry=REGISTRY,
)

# v2026.5.34 (PR 2) D12 — federated sync scheduler / engine.
# Owned by memory/sync.py:SyncEngine + memory/sync_scheduler.py.
# feral_sync_active_peers + feral_sync_failures_total + feral_sync_was_active_recent
# were defined above for W13.1; the labels below extend the surface.
SYNC_ATTEMPTS_TOTAL = Counter(
    "feral_sync_attempts_total",
    "Sync attempts initiated by the scheduler, labelled by peer node id and final status.",
    labelnames=("peer", "status"),
    registry=REGISTRY,
)
SYNC_OPS_SENT_TOTAL = Counter(
    "feral_sync_ops_sent_total",
    "Sync operations transmitted to a peer, labelled by peer node id.",
    labelnames=("peer",),
    registry=REGISTRY,
)
SYNC_OPS_RECEIVED_TOTAL = Counter(
    "feral_sync_ops_received_total",
    "Sync operations received from a peer (post-WAL append), labelled by peer node id.",
    labelnames=("peer",),
    registry=REGISTRY,
)
SYNC_LAG_SECONDS = Gauge(
    "feral_sync_lag_seconds",
    "Seconds since the last successful sync with a peer, labelled by peer node id.",
    labelnames=("peer",),
    registry=REGISTRY,
)
SYNC_WAL_SIZE_BYTES = Gauge(
    "feral_sync_wal_size_bytes",
    "Current on-disk size of the sync WAL (sync_wal.db).",
    registry=REGISTRY,
)
SYNC_HEARTBEAT_MISSES_TOTAL = Counter(
    "feral_sync_heartbeat_misses_total",
    "Heartbeat pings that did not get a reply within heartbeat_interval_seconds, labelled by peer.",
    labelnames=("peer",),
    registry=REGISTRY,
)


# Map of metric name → metric object so emit() can dispatch by string.
# Tests/test_metrics_registry.py walks this map to enforce parity with
# the dashboard + alert rules.
_METRICS: dict[str, Counter | Gauge | Histogram] = {
    "feral_http_requests_total": HTTP_REQUESTS_TOTAL,
    "feral_http_request_duration_seconds": HTTP_REQUEST_DURATION_SECONDS,
    "feral_llm_429_total": LLM_429_TOTAL,
    "feral_llm_failover_chain_exhausted_total": LLM_FAILOVER_CHAIN_EXHAUSTED_TOTAL,
    "feral_sync_active_peers": SYNC_ACTIVE_PEERS,
    "feral_sync_failures_total": SYNC_FAILURES_TOTAL,
    "feral_sync_was_active_recent": SYNC_WAS_ACTIVE_RECENT,
    "feral_supervisor_approval_queue": SUPERVISOR_APPROVAL_QUEUE,
    "feral_tool_denials_total": TOOL_DENIALS_TOTAL,
    "feral_sandbox_kills_total": SANDBOX_KILLS_TOTAL,
    "feral_vault_decrypt_errors_total": VAULT_DECRYPT_ERRORS_TOTAL,
    "feral_ws_active_sessions": WS_ACTIVE_SESSIONS,
    "feral_automation_blocked_total": AUTOMATION_BLOCKED_TOTAL,
    "feral_automation_failure_total": AUTOMATION_FAILURE_TOTAL,
    "feral_automation_permission_denied_total": AUTOMATION_PERMISSION_DENIED_TOTAL,
    "feral_automation_repair_loop_total": AUTOMATION_REPAIR_LOOP_TOTAL,
    "feral_memory_decay_sweeps_total": MEMORY_DECAY_SWEEPS_TOTAL,
    "feral_memory_decay_sweep_duration_seconds": MEMORY_DECAY_SWEEP_DURATION_SECONDS,
    "feral_memory_episodes_active": MEMORY_EPISODES_ACTIVE,
    "feral_memory_episodes_forgotten": MEMORY_EPISODES_FORGOTTEN,
    "feral_memory_episodes_hard_deleted_total": MEMORY_EPISODES_HARD_DELETED_TOTAL,
    "feral_sync_attempts_total": SYNC_ATTEMPTS_TOTAL,
    "feral_sync_ops_sent_total": SYNC_OPS_SENT_TOTAL,
    "feral_sync_ops_received_total": SYNC_OPS_RECEIVED_TOTAL,
    "feral_sync_lag_seconds": SYNC_LAG_SECONDS,
    "feral_sync_wal_size_bytes": SYNC_WAL_SIZE_BYTES,
    "feral_sync_heartbeat_misses_total": SYNC_HEARTBEAT_MISSES_TOTAL,
}


def registered_metric_names() -> set[str]:
    """Return the set of metric names defined in :data:`REGISTRY`.

    Used by ``tests/test_metrics_registry.py`` to assert dashboard/alert
    parity. Includes every metric name registered above (without any
    ``_total`` / ``_bucket`` suffix the prometheus_client client tacks
    on at scrape time — those are accessor names, not separate metrics).
    """
    return set(_METRICS.keys())


def _metrics_endpoint_enabled() -> bool:
    """Whether the /metrics endpoint and emit() writes are active.

    Default-on as of W13: the kill switch must be EXPLICITLY set to a
    falsy value to silence the metrics surface. This matches the
    behaviour the dashboard + alert rules assume (a fresh install
    serves /metrics on loopback by default).
    """
    val = os.getenv("FERAL_METRICS_ENDPOINT", "1").strip().lower()
    return val not in ("0", "false", "off", "no")


def emit(metric: str, value: float = 1.0, labels: Mapping[str, str] | None = None) -> None:
    """Write to a registered metric. No-op when the metrics surface is off.

    Dispatches on the underlying prometheus_client type:
      * Counter   → ``inc(value)`` (default 1.0)
      * Gauge     → ``set(value)``
      * Histogram → ``observe(value)``

    Unknown metric names are silently ignored — emit() is the surface
    the rest of the codebase calls into and we never want a missing
    metric definition to crash a hot path. ``tests/test_metrics_registry.py``
    is responsible for catching dashboard / alert references that don't
    have a matching ``_METRICS`` entry.
    """
    if not _metrics_endpoint_enabled():
        return
    m = _METRICS.get(metric)
    if m is None:
        return
    target = m.labels(**dict(labels)) if labels else m
    if isinstance(m, Counter):
        target.inc(value)
    elif isinstance(m, Gauge):
        target.set(value)
    elif isinstance(m, Histogram):
        target.observe(value)


def render_prometheus() -> tuple[str, str]:
    """Render the registry into Prometheus exposition text.

    Returns ``(body, content_type)`` ready for the ASGI response. Kept
    out of ``api/server.py`` so the endpoint handler stays a one-liner
    and tests can call it without spinning up FastAPI.
    """
    return generate_latest(REGISTRY).decode("utf-8"), CONTENT_TYPE_LATEST


