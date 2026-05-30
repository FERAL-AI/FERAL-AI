"""enforce that the prometheus_client REGISTRY is in lockstep with
the default Grafana dashboard and Prometheus alert rules.

If a panel or alert references a metric we never registered, the
exposition will be empty and operators will silently miss signal.
If we register a metric no panel or alert ever consumes, it's dead
code and we burn cardinality budget for nothing. This test fails the
build on either drift.

Owned paths: feral-core/observability/metrics.py,
ops/grafana/feral-overview.json, ops/prometheus/alerts.yml.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from observability.metrics import registered_metric_names

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_PATH = REPO_ROOT / "ops" / "grafana" / "feral-overview.json"
ALERTS_PATH = REPO_ROOT / "ops" / "prometheus" / "alerts.yml"

# Identifier shape for a Prometheus metric name. We anchor on the
# ``feral_`` prefix so dashboard expressions like ``rate(...)`` or
# ``histogram_quantile(...)`` don't pollute the set with the function
# names themselves. Every  metric MUST start with ``feral_``.
_METRIC_RE = re.compile(r"\bferal_[a-zA-Z_][a-zA-Z0-9_]*\b")

# Histograms expose three derived series at scrape time
# (``_bucket``, ``_count``, ``_sum``). The dashboard always queries
# the bucket form via histogram_quantile, so we strip the suffix
# back to the base metric name when comparing against the registry.
_HIST_SUFFIXES = ("_bucket", "_count", "_sum")


def _strip_hist_suffix(name: str) -> str:
    for suffix in _HIST_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _extract_metric_names(blob: str) -> set[str]:
    return {_strip_hist_suffix(m) for m in _METRIC_RE.findall(blob)}


@pytest.fixture(scope="module")
def dashboard_metric_names() -> set[str]:
    assert DASHBOARD_PATH.is_file(), f"Missing dashboard JSON at {DASHBOARD_PATH}"
    raw = DASHBOARD_PATH.read_text()
    parsed = json.loads(raw)
    panels = parsed.get("panels", [])
    assert panels, "Dashboard has no panels — that's almost certainly a regression."
    found: set[str] = set()
    for panel in panels:
        for target in panel.get("targets", []):
            expr = target.get("expr", "")
            found |= _extract_metric_names(expr)
    return found


@pytest.fixture(scope="module")
def alert_metric_names() -> set[str]:
    assert ALERTS_PATH.is_file(), f"Missing alerts YAML at {ALERTS_PATH}"
    parsed = yaml.safe_load(ALERTS_PATH.read_text())
    assert parsed and "groups" in parsed, "Alerts YAML missing top-level 'groups' key."
    found: set[str] = set()
    for group in parsed["groups"]:
        for rule in group.get("rules", []):
            expr = rule.get("expr", "")
            found |= _extract_metric_names(expr)
    return found


def test_dashboard_has_panels():
    parsed = json.loads(DASHBOARD_PATH.read_text())
    panels = parsed.get("panels", [])
    assert len(panels) >= 8, (
        f"Default overview dashboard collapsed to {len(panels)} panels; W13 charter "
        "requires the eight signal categories listed in roadmap §3.1 #4."
    )


def test_alerts_yaml_has_required_rules():
    parsed = yaml.safe_load(ALERTS_PATH.read_text())
    rule_names = {
        rule["alert"]
        for group in parsed["groups"]
        for rule in group.get("rules", [])
        if "alert" in rule
    }
    expected = {
        "HighErrorRate",
        "LLMAllProvidersDown",
        "SyncPeerDown",
        "SupervisorBacklog",
        "VaultDecryptFailed",
    }
    missing = expected - rule_names
    assert not missing, f"alerts.yml missing required W13 rules: {sorted(missing)}"


def test_every_dashboard_metric_is_registered(dashboard_metric_names):
    registered = registered_metric_names()
    missing = dashboard_metric_names - registered
    assert not missing, (
        f"Dashboard references metrics that are not registered in the W13 REGISTRY: "
        f"{sorted(missing)}. Add them in feral-core/observability/metrics.py or "
        f"remove the panel."
    )


def test_every_alert_metric_is_registered(alert_metric_names):
    registered = registered_metric_names()
    missing = alert_metric_names - registered
    assert not missing, (
        f"Alert rules reference metrics that are not registered in the W13 REGISTRY: "
        f"{sorted(missing)}. Add them in feral-core/observability/metrics.py or "
        f"remove the rule."
    )


def test_no_orphan_metrics(dashboard_metric_names, alert_metric_names):
    """Every registered metric must be consumed by at least one panel or alert.

    Keeps the registry from quietly accumulating dead metrics. If a
    new metric is genuinely needed but not yet referenced, add a
    placeholder panel/alert in the same PR.
    """
    registered = registered_metric_names()
    referenced = dashboard_metric_names | alert_metric_names
    orphans = registered - referenced
    assert not orphans, (
        f"Registered metrics are never referenced by the dashboard or alerts: "
        f"{sorted(orphans)}. Either consume them or drop the registration."
    )
