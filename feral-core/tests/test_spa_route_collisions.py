"""Two API paths are also SPA page names, and the API was winning.

`GET /skills` (api/routes/skills.py) and `GET /health`
(api/routes/dashboard.py) are registered without the `/api` prefix. The
v2 dashboard has a page at each of those names, and FastAPI matches a
registered route before the SPA catch-all, so a browser asking for the
page got the API.

Measured against a running brain before this change:

    /skills  -> application/json 200, 33KB of manifests, 0 <a> elements
    /health  -> application/json 200
    the other 26 SPA routes -> text/html 200

Clicking Skills in the dock worked, because that never reaches the
server. Reloading, bookmarking, or opening a shared link left the user
on a wall of JSON with nothing to click and no way back but editing the
URL.

Neither path could move: `/health` is the Docker HEALTHCHECK and the
load-balancer probe, `/skills` is a published alias. So the request
decides. These tests pin both halves of that, because a fix that
serves HTML to the health probe would take the container down.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient


BROWSER = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "sec-fetch-mode": "navigate",
    "sec-fetch-dest": "document",
}
CURL = {"accept": "*/*"}
# `fetch()` and XHR default to `*/*`, which is what separates them from
# a navigation. The dashboard's own calls all look like this.
SPA_FETCH = {
    "accept": "*/*",
    "sec-fetch-mode": "cors",
    "sec-fetch-dest": "empty",
}


@pytest.fixture(scope="module")
def app_module():
    from api import server
    return server


@pytest.fixture(scope="module")
def client(app_module):
    with TestClient(app_module.app, raise_server_exceptions=False) as c:
        yield c


class TestTheNavigationTest:
    """The predicate, exhaustively. Everything else rests on it."""

    @staticmethod
    def _req(app_module, headers):
        from starlette.requests import Request
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/health",
            "headers": [
                (k.encode(), v.encode()) for k, v in headers.items()
            ],
        }
        return Request(scope)

    def test_a_browser_navigation_is_one(self, app_module):
        assert app_module._is_document_navigation(self._req(app_module, BROWSER))

    def test_curl_is_not(self, app_module):
        """The Docker HEALTHCHECK. Getting this wrong takes the container down."""
        assert not app_module._is_document_navigation(self._req(app_module, CURL))

    def test_the_dashboards_own_fetch_is_not(self, app_module):
        assert not app_module._is_document_navigation(self._req(app_module, SPA_FETCH))

    def test_a_navigation_behind_the_service_worker_is_one(self, app_module):
        """The case that broke the first version of this rule.

        This app registers `sw.js`, whose network-first branch handles
        the navigation by calling `fetch(req)` itself. Chromium rewrites
        the fetch metadata on that reissued request, so a genuine page
        load arrives with `sec-fetch-dest: empty` and
        `sec-fetch-mode: cors`. Measured both ways against a live brain:
        with the service worker blocked, `/health` served `text/html`
        and the dashboard rendered; with it active and the old rule that
        required `navigate`, the same navigation got `application/json`
        and a blank page.

        `Accept` survives the reissue, which is why it is the fallback.
        """
        assert app_module._is_document_navigation(self._req(app_module, {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
        }))

    def test_an_older_browser_without_sec_fetch_still_gets_the_page(self, app_module):
        """The Sec-Fetch headers are a fast path, not a requirement."""
        assert app_module._is_document_navigation(self._req(app_module, {
            "accept": "text/html,application/xhtml+xml",
        }))

    def test_sec_fetch_dest_document_is_enough_on_its_own(self, app_module):
        assert app_module._is_document_navigation(self._req(app_module, {
            "accept": "*/*", "sec-fetch-dest": "document",
        }))

    def test_no_accept_header_at_all_is_not(self, app_module):
        assert not app_module._is_document_navigation(self._req(app_module, {}))


@pytest.mark.parametrize("path", ["/skills", "/health"])
class TestBothHalvesHold:
    def test_a_browser_gets_a_page(self, client, path):
        r = client.get(path, headers=BROWSER)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html"), (
            f"{path} served {r.headers['content-type']} to a browser "
            "navigation; the user lands on raw JSON with no way back"
        )

    def test_a_probe_still_gets_json(self, client, path):
        r = client.get(path, headers=CURL)
        assert "json" in r.headers["content-type"], (
            f"{path} served {r.headers['content-type']} to a curl-style "
            "probe; this is the Docker HEALTHCHECK path"
        )

    def test_the_dashboards_own_fetch_still_gets_json(self, client, path):
        r = client.get(path, headers=SPA_FETCH)
        assert "json" in r.headers["content-type"]


def test_health_still_reports_what_it_always_did(client):
    body = client.get("/health", headers=CURL).json()
    assert body["status"] == "ok"
    assert "version" in body
