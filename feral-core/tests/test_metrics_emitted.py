"""smoke test that the proof-of-concept emit() call inside the
HTTP middleware produces a scrapeable feral_http_requests_total
sample on /metrics.

Cross-module emit() wiring (sync, MCP, LLM, supervisor, sandbox,
vault) is deferred to , so this test only covers the single
emit() site this PR ships. See ops/grafana/feral-overview.json for
the panels these samples drive.
"""
from __future__ import annotations

import re

import httpx
import pytest
from httpx import ASGITransport
from starlette.testclient import TestClient


async def _get_off_loopback(app, path: str) -> httpx.Response:
    """GET *path* against *app* with a non-loopback client address.

    ``httpx.ASGITransport`` only handles async requests, so this
    coroutine is the right shape for pytest-asyncio. starlette's
    TestClient kwargs differ across versions; reaching one layer down
    to the transport gives us a stable ``client=(host, port)``
    parameter that propagates to ``request.scope["client"]`` and
    therefore to ``request.client.host`` inside the endpoint handler.
    """
    transport = ASGITransport(app=app, client=("203.0.113.7", 50000), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://feral.test") as client:
        return await client.get(path)


@pytest.fixture
def metrics_client(monkeypatch):
    """TestClient with the metrics surface explicitly enabled.

    FERAL_METRICS_ENDPOINT defaults to "1" , but we set it
    explicitly so the test isn't accidentally green just because the
    default flips again later. FERAL_LOCAL_BYPASS lets the API-key
    middleware see the request as loopback so we don't need to mint a
    bearer token. FERAL_API_KEY is set so the boot-time "generated key
    on first run" banner doesn't pollute stdout during pytest -v.
    """
    monkeypatch.setenv("FERAL_METRICS_ENDPOINT", "1")
    monkeypatch.setenv("FERAL_LOCAL_BYPASS", "1")
    monkeypatch.setenv("FERAL_API_KEY", "test-key-w13-emit")

    from api.server import app

    return TestClient(app, raise_server_exceptions=False)


def _scrape(client: TestClient) -> str:
    resp = client.get("/metrics")
    assert resp.status_code == 200, f"/metrics returned {resp.status_code}: {resp.text[:200]}"
    return resp.text


def test_metrics_endpoint_serves_prometheus_exposition(metrics_client):
    body = _scrape(metrics_client)
    # generate_latest emits HELP/TYPE banners for every registered metric
    # even when no samples have been recorded yet.
    assert "# HELP feral_http_requests_total" in body
    assert "# TYPE feral_http_requests_total counter" in body


def test_http_requests_total_increments_on_real_traffic(metrics_client):
    for _ in range(3):
        metrics_client.get("/health")

    body = _scrape(metrics_client)
    # The middleware emits with method/route/status labels. The route
    # template for /health is literally "/health" because no path
    # params are involved.
    pattern = re.compile(
        r'feral_http_requests_total\{[^}]*method="GET"[^}]*route="/health"[^}]*status="2xx"[^}]*\}\s+(\d+(?:\.\d+)?)'
    )
    matches = [float(v) for v in pattern.findall(body)]
    assert matches, (
        "feral_http_requests_total{method=\"GET\",route=\"/health\",status=\"2xx\"} "
        "did not appear in /metrics output. emit() proof-of-concept call site "
        "in feral-core/api/server.py is not firing."
    )
    assert max(matches) >= 3, (
        f"Expected at least 3 GET /health observations, saw values {matches}."
    )


async def test_off_loopback_returns_404_without_public_switch(monkeypatch):
    """Without FERAL_METRICS_PUBLIC, off-loopback callers must get 404.

    We force the route by patching request.client.host to a non-loopback
    IP. A real off-loopback caller would hit the same code path.
    """
    monkeypatch.setenv("FERAL_METRICS_ENDPOINT", "1")
    monkeypatch.setenv("FERAL_LOCAL_BYPASS", "1")
    monkeypatch.setenv("FERAL_API_KEY", "test-key-w13-public")
    monkeypatch.delenv("FERAL_METRICS_PUBLIC", raising=False)

    from api.server import app

    resp = await _get_off_loopback(app, "/metrics")
    assert resp.status_code == 404, (
        f"Expected 404 for off-loopback /metrics without FERAL_METRICS_PUBLIC, "
        f"got {resp.status_code}: {resp.text[:200]}"
    )


async def test_off_loopback_allowed_when_public_switch_set(monkeypatch):
    monkeypatch.setenv("FERAL_METRICS_ENDPOINT", "1")
    monkeypatch.setenv("FERAL_LOCAL_BYPASS", "1")
    monkeypatch.setenv("FERAL_API_KEY", "test-key-w13-public-on")
    monkeypatch.setenv("FERAL_METRICS_PUBLIC", "1")

    from api.server import app

    resp = await _get_off_loopback(app, "/metrics")
    assert resp.status_code == 200, (
        f"Expected 200 for off-loopback /metrics with FERAL_METRICS_PUBLIC=1, "
        f"got {resp.status_code}: {resp.text[:200]}"
    )


def test_kill_switch_returns_404(monkeypatch):
    monkeypatch.setenv("FERAL_METRICS_ENDPOINT", "0")
    monkeypatch.setenv("FERAL_LOCAL_BYPASS", "1")
    monkeypatch.setenv("FERAL_API_KEY", "test-key-w13-killed")

    from api.server import app

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/metrics")
    assert resp.status_code == 404
