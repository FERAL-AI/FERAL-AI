import pytest

from security.proxy_auth import (
    ProxyAuthConfig,
    ProxyAuthError,
    ProxyIdentity,
    authenticate_proxy,
    authorize_browser_origin,
    config_from_env,
)


def cfg(**overrides):
    values = dict(
        enabled=True,
        trusted_proxies=("10.0.0.0/8", "192.168.1.10"),
        shared_secret="shared-secret",
        allowed_origins=("https://feral.example",),
    )
    values.update(overrides)
    return ProxyAuthConfig(**values)


def headers(**values):
    return {
        "X-FERAL-Proxy-Secret": "shared-secret",
        "X-FERAL-Proxy-User": "noah",
        "X-FERAL-Proxy-Groups": "operators|demo",
        **values,
    }


def test_authenticates_exact_socket_peer_and_returns_identity():
    identity = authenticate_proxy(cfg(), socket_client_ip="10.22.1.4", headers=headers())
    assert identity == ProxyIdentity(user="noah", groups=("operators", "demo"))


def test_never_uses_forwarded_for_as_trust_boundary():
    with pytest.raises(ProxyAuthError, match="trusted proxy"):
        authenticate_proxy(
            cfg(),
            socket_client_ip="203.0.113.7",
            headers=headers(**{"X-Forwarded-For": "10.22.1.4"}),
        )


@pytest.mark.parametrize(
    "override, message",
    [
        ({"enabled": False}, "disabled"),
        ({"shared_secret": ""}, "secret"),
        ({"shared_secret": "   "}, "secret"),
        ({"trusted_proxies": ()}, "trusted proxy list"),
        ({"trusted_proxies": ("not-an-ip",)}, "invalid trusted"),
        ({"allowed_origins": ()}, "allowed origin list"),
        ({"allowed_origins": ("feral.example",)}, "invalid canonical"),
        (
            {"allowed_origins": ("https://user:pass@feral.example",)},
            "invalid canonical",
        ),
        ({"allowed_origins": ("https://feral.example:bad",)}, "invalid canonical"),
    ],
)
def test_invalid_or_disabled_configuration_fails_closed(override, message):
    with pytest.raises(ProxyAuthError, match=message):
        authenticate_proxy(cfg(**override), socket_client_ip="10.0.0.2", headers=headers())


def test_secret_and_identity_are_required_and_allowlists_apply():
    with pytest.raises(ProxyAuthError, match="shared secret"):
        authenticate_proxy(cfg(), socket_client_ip="10.0.0.2", headers={})
    with pytest.raises(ProxyAuthError, match="identity"):
        authenticate_proxy(cfg(), socket_client_ip="10.0.0.2", headers={"X-FERAL-Proxy-Secret": "shared-secret"})
    with pytest.raises(ProxyAuthError, match="not allowed"):
        authenticate_proxy(cfg(allowed_users=("alice",)), socket_client_ip="10.0.0.2", headers=headers())
    with pytest.raises(ProxyAuthError, match="groups"):
        authenticate_proxy(cfg(allowed_groups=("admins",)), socket_client_ip="10.0.0.2", headers=headers())


def test_headers_are_case_insensitive_and_groups_deduplicate():
    identity = authenticate_proxy(
        cfg(),
        socket_client_ip="192.168.1.10",
        headers={
            "x-feral-proxy-secret": "shared-secret",
            "x-feral-proxy-user": "noah",
            "x-feral-proxy-groups": "operators|operators|demo",
        },
    )
    assert identity.groups == ("operators", "demo")


def test_group_separator_is_configurable_for_other_proxies():
    identity = authenticate_proxy(
        cfg(groups_separator=","),
        socket_client_ip="10.0.0.2",
        headers=headers(**{"X-FERAL-Proxy-Groups": "operators,demo"}),
    )
    assert identity.groups == ("operators", "demo")


@pytest.mark.parametrize(
    "override",
    [
        {"secret_header": "Bad Header"},
        {"identity_header": "X:Injected"},
        {"groups_header": "X-Bad\r\nHeader"},
        {"identity_header": "Authorization"},
        {"groups_header": "X-Forwarded-For"},
        {"identity_header": "X-FERAL-Proxy-Secret"},
        {"groups_separator": ""},
        {"groups_separator": "||"},
    ],
)
def test_invalid_or_ambiguous_header_configuration_fails_closed(override):
    with pytest.raises(ProxyAuthError):
        authenticate_proxy(
            cfg(**override), socket_client_ip="10.0.0.2", headers=headers()
        )


def test_origin_rules_allow_same_origin_and_reject_cross_site():
    authorize_browser_origin(cfg(), headers={"Origin": "https://feral.example"})
    authorize_browser_origin(cfg(), headers={"Origin": "HTTPS://FERAL.EXAMPLE:443"})
    with pytest.raises(ProxyAuthError, match="Origin"):
        authorize_browser_origin(cfg(), headers={"Origin": "https://evil.example"})
    with pytest.raises(ProxyAuthError, match="required"):
        authorize_browser_origin(cfg(), headers={}, method="POST")
    with pytest.raises(ProxyAuthError, match="required"):
        authorize_browser_origin(cfg(), headers={}, websocket=True)
    authorize_browser_origin(cfg(), headers={}, method="GET")


def test_fetch_metadata_rejects_cross_site_except_safe_top_level_navigation():
    with pytest.raises(ProxyAuthError, match="cross-site"):
        authorize_browser_origin(
            cfg(),
            headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors"},
        )
    with pytest.raises(ProxyAuthError, match="cross-site"):
        authorize_browser_origin(
            cfg(),
            headers={
                "Origin": "https://feral.example",
                "Sec-Fetch-Site": "cross-site",
            },
            websocket=True,
        )
    authorize_browser_origin(
        cfg(),
        headers={
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "document",
        },
    )


def test_origin_config_is_required_for_unsafe_and_websocket_requests():
    config = cfg(allowed_origins=())
    with pytest.raises(ProxyAuthError, match="allowed origin list"):
        authorize_browser_origin(config, headers={"Origin": "https://feral.example"}, method="POST")


def test_env_loader_is_disabled_by_default_and_parses_explicit_values():
    assert config_from_env({}).enabled is False
    loaded = config_from_env(
        {
            "FERAL_PROXY_AUTH_ENABLED": "true",
            "FERAL_PROXY_AUTH_TRUSTED_PROXIES": "10.0.0.0/8, 192.168.1.10",
            "FERAL_PROXY_AUTH_SECRET": "secret",
            "FERAL_PROXY_AUTH_ALLOWED_ORIGINS": "https://feral.example",
            "FERAL_PROXY_AUTH_GROUPS_SEPARATOR": ",",
        }
    )
    assert loaded.enabled is True
    assert loaded.trusted_proxies == ("10.0.0.0/8", "192.168.1.10")
    assert loaded.groups_separator == ","
