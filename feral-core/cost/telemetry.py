"""Cost telemetry — Prometheus counters with structured-log fallback."""

from __future__ import annotations

import logging
from typing import Mapping

logger = logging.getLogger("feral.cost.telemetry")

_PROMETHEUS = False
_COST_REGISTRY = None
_DOLLARS_TOTAL = None
_CALLS_TOTAL = None
_CAP_HIT_TOTAL = None


def _init_prometheus() -> bool:
    global _PROMETHEUS, _COST_REGISTRY, _DOLLARS_TOTAL, _CALLS_TOTAL, _CAP_HIT_TOTAL
    if _PROMETHEUS:
        return True
    try:
        from prometheus_client import CollectorRegistry, Counter

        _COST_REGISTRY = CollectorRegistry(auto_describe=True)
        _DOLLARS_TOTAL = Counter(
            "feral_cost_dollars_total",
            "LLM spend in USD accumulated by the cost budget service.",
            labelnames=("call_site", "model", "token_type"),
            registry=_COST_REGISTRY,
        )
        _CALLS_TOTAL = Counter(
            "feral_cost_calls_total",
            "LLM calls recorded by the cost budget service.",
            labelnames=("call_site", "model"),
            registry=_COST_REGISTRY,
        )
        _CAP_HIT_TOTAL = Counter(
            "feral_cost_cap_hit_total",
            "Budget cap rejections by call site and window.",
            labelnames=("call_site", "window"),
            registry=_COST_REGISTRY,
        )
        _PROMETHEUS = True
        return True
    except ImportError:
        logger.debug("prometheus_client unavailable; cost telemetry uses structured logs")
        return False


def record_call(call_site: str, model: str) -> None:
    if _init_prometheus() and _CALLS_TOTAL is not None:
        _CALLS_TOTAL.labels(call_site=call_site, model=model).inc()
    else:
        logger.info(
            "cost_call call_site=%s model=%s",
            call_site,
            model,
        )


def record_dollars(
    call_site: str,
    model: str,
    *,
    input_dollars: float,
    output_dollars: float,
) -> None:
    if _init_prometheus() and _DOLLARS_TOTAL is not None:
        if input_dollars:
            _DOLLARS_TOTAL.labels(
                call_site=call_site, model=model, token_type="input"
            ).inc(input_dollars)
        if output_dollars:
            _DOLLARS_TOTAL.labels(
                call_site=call_site, model=model, token_type="output"
            ).inc(output_dollars)
    else:
        logger.info(
            "cost_dollars call_site=%s model=%s input_usd=%.8f output_usd=%.8f",
            call_site,
            model,
            input_dollars,
            output_dollars,
        )


def record_cap_hit(call_site: str, window: str) -> None:
    if _init_prometheus() and _CAP_HIT_TOTAL is not None:
        _CAP_HIT_TOTAL.labels(call_site=call_site, window=window).inc()
    else:
        logger.warning(
            "cost_cap_hit call_site=%s window=%s",
            call_site,
            window,
        )


def prometheus_registry():
    """Return the dedicated cost metrics registry (may be None)."""
    _init_prometheus()
    return _COST_REGISTRY


def counter_values(labels: Mapping[str, str] | None = None) -> dict[str, float]:
    """Test helper — scrape counter values from the cost registry."""
    if not _init_prometheus() or _COST_REGISTRY is None:
        return {}
    from prometheus_client import generate_latest
    from prometheus_client.parser import text_string_to_metric_families

    out: dict[str, float] = {}
    for family in text_string_to_metric_families(generate_latest(_COST_REGISTRY).decode()):
        for sample in family.samples:
            if labels and any(sample.labels.get(k) != v for k, v in labels.items()):
                continue
            out[sample.name] = out.get(sample.name, 0.0) + sample.value
    return out
