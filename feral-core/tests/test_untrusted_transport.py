"""Trust is a property of the listener, not of ``client.host``.

Every remote-access design for this brain terminates the tunnel on the
brain's own machine. That means the proxied request arrives from
``127.0.0.1``, and ``api/server.py`` grants loopback a complete
exemption from auth: HTTP requests skip ``APIKeyMiddleware`` entirely,
and ``/v1/session`` marks itself authenticated without a token. Exposing
the brain through any tunnel would therefore publish an unauthenticated
chat socket and an unauthenticated API to whoever reached the far end.

``api.server.untrusted_app`` wraps the same ASGI app and stamps
``feral.untrusted`` into the scope. The bypasses are conditioned on that
flag, so a listener serving a tunnel gets the strict behaviour while the
local dashboard keeps working with no configuration.

The flag is set by the *server instance*, never by a client and never
from a header, so it cannot be forged.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def apps():
    """Two clients over the same app, differing only in the scope flag.

    Deliberately not used as a context manager: entering one runs the
    app lifespan, which opens the vault and the memory DB. These tests
    are about the auth decision, and the auto FERAL_HOME fixture in
    conftest keeps that decision reachable — it monkeypatches
    ``is_localhost`` so `testclient` counts as loopback, which is what
    makes the bypass fire in the first place.
    """
    from api.server import app, untrusted_app

    yield TestClient(app), TestClient(untrusted_app)


# ── The flag itself ────────────────────────────────────────────────


def test_untrusted_app_wraps_the_same_application():
    """Not a copy. The same routes, the same state, one extra scope key."""
    from api.server import app, untrusted_app

    assert untrusted_app.app is app


def test_scope_key_is_absent_on_the_main_app():
    """The main listener must not be marked untrusted by accident."""
    from security.session_auth import transport_is_trusted

    assert transport_is_trusted({}) is True
    assert transport_is_trusted({"feral.untrusted": True}) is False


def test_a_client_cannot_forge_trust():
    """No header spells 'feral.untrusted'. Only the server sets it."""
    from security.session_auth import transport_is_trusted

    forged = {
        "type": "http",
        "headers": [(b"feral-untrusted", b"false"), (b"x-forwarded-for", b"1.2.3.4")],
    }
    # Absent key means trusted, and headers cannot change that either way:
    # the untrusted listener is the one that sets it, so a forged header
    # can only ever fail to grant privilege it never had.
    assert transport_is_trusted(forged) is True


# ── HTTP ───────────────────────────────────────────────────────────


def test_loopback_bypass_applies_on_the_trusted_app(apps):
    trusted, _ = apps
    r = trusted.get("/api/dashboard")
    assert r.status_code == 200, r.text


def test_loopback_bypass_does_not_apply_on_the_untrusted_app(apps):
    """The whole point: same request, same source IP, different verdict."""
    _, untrusted = apps
    r = untrusted.get("/api/dashboard")
    assert r.status_code == 401, r.text


def test_open_paths_stay_open_on_the_untrusted_app(apps):
    """`/health` is deliberately unauthenticated on every transport."""
    _, untrusted = apps
    assert untrusted.get("/health").status_code == 200


def test_pair_token_issuance_is_gated_over_an_untrusted_transport(apps):
    """Minting a pairing token remotely must require a credential.

    `/api/devices/pair/url` is not in the open-path allowlists, so on the
    main app it rides the loopback bypass. Over a tunnel that would let
    anyone who reached the brain mint a pairing token for it.
    """
    _, untrusted = apps
    r = untrusted.get("/api/devices/pair/url?name=phone")
    assert r.status_code == 401, r.text


def test_local_bypass_env_does_not_rescue_an_untrusted_transport(apps, monkeypatch):
    """FERAL_LOCAL_BYPASS is a dev escape hatch for the LOCAL listener."""
    monkeypatch.setenv("FERAL_LOCAL_BYPASS", "1")
    _, untrusted = apps
    assert untrusted.get("/api/dashboard").status_code == 401


# ── WebSocket ──────────────────────────────────────────────────────
#
# BaseHTTPMiddleware never sees websocket scopes, which is why the
# wrapper is pure ASGI. These two tests are the reason that mattered.


def test_session_socket_is_open_on_the_trusted_app(apps):
    trusted, _ = apps
    with trusted.websocket_connect("/v1/session") as ws:
        assert ws is not None


# ── Trust must not come from a header either ───────────────────────


def test_uvicorn_proxy_header_kwargs_are_explicit():
    """Pin the values rather than inheriting whatever uvicorn defaults to.

    ``_spawn_brain_server`` used to build ``uvicorn.Config`` without
    naming these, silently inheriting ``proxy_headers=True`` with
    ``forwarded_allow_ips="127.0.0.1"``. That default is load-bearing:
    it is the only reason Tailscale Funnel traffic does not present as
    loopback and take the auth bypass. A uvicorn release flipping it
    would open the entire API with no code change here and no test
    failing, so the values are asserted at the source level.
    """
    import inspect

    from cli import main as cli_main

    src = inspect.getsource(cli_main._spawn_brain_server)
    assert "proxy_headers=True" in src, src
    assert 'forwarded_allow_ips="127.0.0.1"' in src, src


def test_session_socket_requires_a_token_over_an_untrusted_transport(apps):
    """Without this, remote access publishes an open chat socket."""
    from starlette.websockets import WebSocketDisconnect

    _, untrusted = apps
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with untrusted.websocket_connect("/v1/session") as ws:
            # The server waits 5s for an `auth` frame before closing.
            # Send something that is not one so the refusal is prompt.
            ws.send_json({"type": "chat", "text": "hello"})
            ws.receive_json()
    assert excinfo.value.code == 4001


# ── The boundary must have no holes ────────────────────────────────


def test_local_key_endpoint_is_refused_over_an_untrusted_transport(apps):
    """The master API key must not be reachable through a tunnel.

    /api/auth/local-key is in _OPEN_PATHS, so APIKeyMiddleware returns
    before the transport check that guards every other loopback bypass.
    Its own gate is therefore the only thing in front of the key, and
    the key satisfies every check this whole boundary exists to enforce:
    with it, an attacker gets the dashboard, /v1/session, and the power
    to mint pairing tokens. One request would defeat the control.

    Asserts only the untrusted side. Whether the trusted client gets the
    key depends on conftest's ``is_localhost`` patch, which other tests
    legitimately tighten, and that is not the claim being made here.
    """
    _, untrusted = apps

    r = untrusted.get("/api/auth/local-key")
    assert r.status_code == 403, r.text
    assert "api_key" not in r.text


def test_no_open_path_self_gates_on_loopback_without_the_transport_check():
    """Structural guard so the next such route cannot drift out.

    A route in _OPEN_PATHS bypasses the middleware entirely, so any of
    them that makes its own trust decision has to consult
    transport_is_trusted itself. Nothing else will do it for them.
    """
    import inspect
    import re

    from api import server as server_mod

    offenders = []
    for module_name in ("auth", "devices", "config", "access"):
        try:
            mod = __import__(f"api.routes.{module_name}", fromlist=["*"])
        except Exception:
            continue
        src = inspect.getsource(mod)
        for match in re.finditer(r"is_localhost\(", src):
            window = src[max(0, match.start() - 400):match.start() + 400]
            if "transport_is_trusted" not in window:
                offenders.append(f"api/routes/{module_name}.py")

    assert not offenders, (
        "These modules gate on is_localhost without consulting "
        f"transport_is_trusted: {sorted(set(offenders))}. A loopback check "
        "alone is not evidence of trust; a tunnel terminates locally."
    )
    assert server_mod is not None
