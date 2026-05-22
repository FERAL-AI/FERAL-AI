"""
Tests for the FERAL observability metrics module.
"""
import time

import pytest
from starlette.testclient import TestClient

from observability.metrics import (
    increment,
    observe,
    measure,
    in_memory_snapshot,
    init_metrics,
    _reset_inmem,
)


@pytest.fixture(autouse=True)
def _clean_inmem():
    """Reset in-memory metrics between tests."""
    _reset_inmem()
    yield
    _reset_inmem()


class TestInMemoryCounters:
    def test_increment_default(self):
        increment("feral.test.counter")
        snap = in_memory_snapshot()
        assert snap["counters"]["feral.test.counter"] == 1

    def test_increment_by(self):
        increment("feral.test.counter", by=5)
        increment("feral.test.counter", by=3)
        snap = in_memory_snapshot()
        assert snap["counters"]["feral.test.counter"] == 8

    def test_multiple_counters(self):
        increment("a")
        increment("b")
        increment("a")
        snap = in_memory_snapshot()
        assert snap["counters"]["a"] == 2
        assert snap["counters"]["b"] == 1


class TestInMemoryHistograms:
    def test_observe_single(self):
        observe("feral.test.latency", 42.5)
        snap = in_memory_snapshot()
        h = snap["histograms"]["feral.test.latency"]
        assert h["count"] == 1
        assert h["mean"] == 42.5

    def test_observe_multiple(self):
        observe("lat", 10.0)
        observe("lat", 20.0)
        observe("lat", 30.0)
        snap = in_memory_snapshot()
        h = snap["histograms"]["lat"]
        assert h["count"] == 3
        assert h["mean"] == pytest.approx(20.0)


class TestMeasureContextManager:
    def test_measure_records_time(self):
        with measure("feral.test.op"):
            time.sleep(0.05)
        snap = in_memory_snapshot()
        h = snap["histograms"]["feral.test.op_ms"]
        assert h["count"] == 1
        assert h["mean"] >= 40, "Expected at least 40ms of measured time"

    def test_measure_records_on_exception(self):
        try:
            with measure("feral.test.fail"):
                raise ValueError("boom")
        except ValueError:
            pass
        snap = in_memory_snapshot()
        assert "feral.test.fail_ms" in snap["histograms"]


class TestInitMetrics:
    def test_init_without_otel_does_not_crash(self):
        init_metrics("test-service")

    def test_init_with_no_endpoint(self):
        import os
        os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
        init_metrics("test-service")


class TestMetricsEndpoint:
    def test_metrics_endpoint_returns_counters(self, monkeypatch):
        # NOTE: Use ``monkeypatch.setenv`` so pytest auto-restores each
        # var at test teardown. The earlier raw ``os.environ[...] = "1"``
        # form leaked ``FERAL_LOCAL_BYPASS=1`` into every subsequent test
        # in the same process — which silently re-enabled the API-key
        # middleware bypass and caused the off-loopback pairing security
        # test (``test_pair_url_requires_auth_off_loopback``) to return
        # 200 instead of 401 under the CI fast lane ordering.
        monkeypatch.setenv("FERAL_METRICS_ENDPOINT", "1")
        monkeypatch.setenv("FERAL_API_KEY", "test-key-metrics")
        monkeypatch.setenv("FERAL_LOCAL_BYPASS", "1")

        increment("feral.test.requests_total", by=42)
        observe("feral.test.latency_ms", 123.4)

        from api.server import app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/metrics")
        assert resp.status_code == 200
        body = resp.text
        assert "feral.test.requests_total" in body
        assert "42" in body
