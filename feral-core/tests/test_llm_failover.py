"""Universal LLM failover — 401 on the primary never stalls the agent.

Tests:
  * classify_error promotes invalid-api-key 401 to AUTH_PERMANENT.
  * chat() with fallback_providers configured delegates to
    chat_with_failover automatically (transparent upgrade).
  * chat_with_failover skips a provider whose cooldown hasn't expired.
  * health_snapshot reports every candidate + cooldown state.
  * GET /api/llm/health returns the snapshot.
  * DigitalTwin.ask() returns a graceful degraded string on provider
    failure instead of bubbling the raw httpx error.
  * POST /api/llm/config auto-prepends the previous primary into
    fallback_providers when the user switches.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


pytestmark = pytest.mark.no_auto_feral_home


# ── classify_error -----------------------------------------------


def test_classify_invalid_key_is_auth_permanent():
    from agents.llm_provider import classify_error, FailoverReason

    exc = type("E", (Exception,), {"status_code": 401})("Incorrect API key provided: sk-...")
    assert classify_error(exc) == FailoverReason.AUTH_PERMANENT


def test_classify_plain_401_without_hard_key_stays_auth():
    from agents.llm_provider import classify_error, FailoverReason

    exc = type("E", (Exception,), {"status_code": 401})("unauthorized")
    assert classify_error(exc) == FailoverReason.AUTH


def test_classify_429_is_rate_limit():
    from agents.llm_provider import classify_error, FailoverReason

    exc = type("E", (Exception,), {"status_code": 429})("rate limit hit")
    assert classify_error(exc) == FailoverReason.RATE_LIMIT


def test_classify_anthropic_credit_balance_400_is_billing():
    """A provider billing 400 (e.g. Anthropic "credit balance too low")
    carries the signal in the JSON BODY, not in str(error). classify_error
    must fold the response body in and classify it as BILLING (1h cooldown)
    instead of UNKNOWN (10s) — otherwise the dead provider is re-tried every
    turn and floods the log."""
    from agents.llm_provider import classify_error, FailoverReason

    resp = type("R", (), {
        "status_code": 400,
        "text": (
            "Your credit balance is too low to access the Anthropic API. "
            "Please go to Plans & Billing to upgrade or purchase credits."
        ),
    })()
    # str(error) is only the bare status line — the body must still classify.
    exc = type("E", (Exception,), {"response": resp})(
        "Client error '400 Bad Request' for url 'https://api.anthropic.com/v1/messages'"
    )
    assert classify_error(exc) == FailoverReason.BILLING


# ── chat() auto-delegates to failover ---------------------------


@pytest.mark.asyncio
async def test_chat_delegates_when_fallbacks_configured():
    from agents.llm_provider import LLMProvider
    llm = LLMProvider.__new__(LLMProvider)
    llm._config = {"fallback_providers": ["ollama"]}
    llm._local_engine = None
    llm.provider = "openai"
    llm._messages_contain_vision = lambda m: False  # type: ignore
    llm.chat_with_failover = AsyncMock(return_value={"choices": [{"message": {"content": "from-failover"}}]})
    out = await llm.chat([{"role": "user", "content": "hi"}])
    llm.chat_with_failover.assert_awaited_once()
    assert out["choices"][0]["message"]["content"] == "from-failover"


@pytest.mark.asyncio
async def test_chat_returns_error_dict_when_failover_exhausts():
    from agents.llm_provider import LLMProvider
    llm = LLMProvider.__new__(LLMProvider)
    llm._config = {"fallback_providers": ["ollama"]}
    llm._local_engine = None
    llm.provider = "openai"
    llm._messages_contain_vision = lambda m: False  # type: ignore
    llm.chat_with_failover = AsyncMock(side_effect=RuntimeError("All providers exhausted"))
    out = await llm.chat([{"role": "user", "content": "hi"}])
    assert "error" in out
    assert out["choices"] == []


# ── cooldown tracker ---------------------------------------------


def test_cooldown_auth_permanent_skips_for_day():
    from agents.llm_provider import FailoverReason, ProviderCooldownTracker

    ct = ProviderCooldownTracker()
    ct.record_failure("openai", FailoverReason.AUTH_PERMANENT)
    assert ct.is_available("openai") is False
    until = ct._cooldowns.get("openai", 0)
    assert until - time.time() > 60 * 60 * 23  # ≥23h


# ── health_snapshot + REST ---------------------------------------


def test_health_snapshot_reports_candidates_and_cooldowns():
    from agents.llm_provider import LLMProvider, FailoverReason
    llm = LLMProvider.__new__(LLMProvider)
    llm.provider = "openai"
    llm.model = "gpt-4o-mini"
    llm.api_key = "sk-x"
    llm.base_url = "https://api.openai.com/v1"
    llm.available = True
    llm._config = {"fallback_providers": ["anthropic", "ollama"]}
    llm._cooldown = __import__("agents.llm_provider", fromlist=["ProviderCooldownTracker"]).ProviderCooldownTracker()
    llm._cooldown.record_failure("anthropic", FailoverReason.AUTH_PERMANENT)
    llm._build_candidate_list = MagicMock(return_value=[  # type: ignore
        ("openai", {"model": "gpt-4o-mini", "api_key": "sk-x", "base_url": "https://api.openai.com/v1"}),
        ("anthropic", {"model": "claude", "api_key": "a-x", "base_url": "https://api.anthropic.com/v1"}),
        ("ollama", {"model": "llama3", "api_key": "ollama", "base_url": "http://127.0.0.1:11434/v1"}),
    ])

    snap = llm.health_snapshot()
    assert snap["active"]["provider"] == "openai"
    assert len(snap["candidates"]) == 3
    providers = [c["provider"] for c in snap["candidates"]]
    assert providers == ["openai", "anthropic", "ollama"]
    # Anthropic must show as in cooldown.
    anthropic = next(c for c in snap["candidates"] if c["provider"] == "anthropic")
    assert anthropic["in_cooldown"] is True
    assert anthropic["cooldown_remaining"] > 0


@pytest.fixture
def health_client():
    from agents.llm_provider import LLMProvider, FailoverReason, ProviderCooldownTracker
    llm = LLMProvider.__new__(LLMProvider)
    llm.provider = "openai"
    llm.model = "gpt-4o-mini"
    llm.api_key = "sk-x"
    llm.base_url = "https://api.openai.com/v1"
    llm.available = True
    llm._config = {"fallback_providers": ["ollama"]}
    llm._cooldown = ProviderCooldownTracker()
    llm._cooldown.record_failure("openai", FailoverReason.AUTH_PERMANENT)
    llm._build_candidate_list = MagicMock(return_value=[  # type: ignore
        ("openai", {"model": "gpt-4o-mini", "api_key": "sk-x", "base_url": "https://api.openai.com/v1"}),
        ("ollama", {"model": "llama3", "api_key": "ollama", "base_url": "http://127.0.0.1:11434/v1"}),
    ])

    mock = MagicMock()
    mock.orchestrator = MagicMock()
    mock.orchestrator.llm = llm
    with patch("api.state.state", mock), patch("api.routes.llm.state", mock):
        from api.server import app
        yield TestClient(app, raise_server_exceptions=False)


def test_rest_health_endpoint(health_client):
    r = health_client.get("/api/llm/health")
    assert r.status_code == 200
    body = r.json()
    assert body["active"]["provider"] == "openai"
    assert any(c["provider"] == "openai" and c["in_cooldown"] for c in body["candidates"])


# ── DigitalTwin graceful degrade --------------------------------


@pytest.mark.asyncio
async def test_digital_twin_ask_returns_graceful_message_on_error_dict():
    from agents.digital_twin import DigitalTwin

    mem = MagicMock()
    mem.episode_recent.return_value = []
    mem.knowledge_search.return_value = []

    ident = MagicMock()
    ident.load_identity.return_value = "You are FERAL."

    llm = MagicMock()
    llm.chat = AsyncMock(return_value={"error": "401 Unauthorized", "choices": []})

    twin = DigitalTwin(memory=mem, identity_loader=ident, llm=llm)
    result = await twin.ask("what do I do on monday")
    assert "Configure a working provider" in result
    assert "401" not in result


# ── /api/llm/config auto-prepends previous primary ---------------


@pytest.fixture
def config_client(tmp_path):
    from providers.catalog import ProviderCatalog
    from security.vault import BlindVault

    store: dict = {}
    cfg = MagicMock()
    cfg.get.side_effect = lambda section, key, default=None: store.get(f"{section}.{key}", default)
    def _set(section, key, value):
        store[f"{section}.{key}"] = value
    cfg.update_settings.side_effect = _set
    cfg.save_credentials = MagicMock(return_value=True)

    mock = MagicMock()
    mock.provider_catalog = ProviderCatalog()
    mock.config = cfg
    mock.vault = BlindVault(vault_path=str(tmp_path / "credentials.json"))
    orch = MagicMock()
    orch.llm = MagicMock()
    orch.llm.reconfigure = AsyncMock(return_value={
        "ok": True, "provider": "anthropic", "model": "claude",
        "available": True, "base_url": "", "reason": "ok",
    })
    orch.llm._config = {"fallback_providers": []}
    orch.llm.set_config = MagicMock()
    mock.orchestrator = orch

    with patch("api.state.state", mock), patch("api.routes.llm.state", mock):
        from api.server import app
        yield TestClient(app, raise_server_exceptions=False), store, orch


def test_auto_prepend_previous_primary_on_switch(config_client):
    c, store, orch = config_client
    # Start with openai as primary
    store["llm.provider"] = "openai"
    store["llm.fallback_providers"] = []

    r = c.post("/api/llm/config", json={
        "provider": "anthropic",
        "model": "claude-sonnet-4-20250514",
    })
    assert r.status_code == 200
    # Previous primary (openai) should have been auto-added.
    assert "openai" in store["llm.fallback_providers"]
    assert store["llm.provider"] == "anthropic"
    # The running LLMProvider should have received the new fallbacks via
    # set_config.
    orch.llm.set_config.assert_called_once()
    new_cfg = orch.llm.set_config.call_args.args[0]
    assert "openai" in new_cfg.get("fallback_providers", [])


def test_explicit_fallbacks_override_auto_prepend(config_client):
    c, store, _orch = config_client
    store["llm.provider"] = "openai"
    r = c.post("/api/llm/config", json={
        "provider": "anthropic",
        "model": "claude",
        "fallback_providers": [],  # explicit empty = user opted out
    })
    assert r.status_code == 200
    assert store["llm.fallback_providers"] == []


@pytest.mark.asyncio
async def test_stream_nonstream_failover_helper_converts_response_to_stream_events():
    from agents.llm_provider import LLMProvider, ProviderCooldownTracker

    llm = LLMProvider.__new__(LLMProvider)
    llm._config = {"fallback_providers": ["anthropic"]}
    llm._local_engine = None
    llm.provider = "openai"
    llm.model = "gpt-4o-mini"
    llm._cooldown = ProviderCooldownTracker()
    llm.chat_with_failover = AsyncMock(
        return_value={"choices": [{"message": {"content": "fallback text"}}]}
    )

    events = await llm._stream_via_nonstream_failover(
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        temperature=0.7,
        max_tokens=256,
        primary_error=RuntimeError("400 bad request"),
    )

    assert events is not None
    assert events[0] == {"type": "text_delta", "content": "fallback text"}
    assert events[-1] == {"type": "done"}
    llm.chat_with_failover.assert_awaited_once()


@pytest.mark.asyncio
async def test_stream_nonstream_failover_helper_skips_context_overflow():
    from agents.llm_provider import LLMProvider, ProviderCooldownTracker

    llm = LLMProvider.__new__(LLMProvider)
    llm._config = {"fallback_providers": ["anthropic"]}
    llm._local_engine = None
    llm.provider = "openai"
    llm.model = "gpt-4o-mini"
    llm._cooldown = ProviderCooldownTracker()
    llm.chat_with_failover = AsyncMock()

    events = await llm._stream_via_nonstream_failover(
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        temperature=0.7,
        max_tokens=256,
        primary_error=RuntimeError("context length exceeded"),
    )

    assert events is None
    llm.chat_with_failover.assert_not_awaited()


# ── Retry-After header parsing -----------------------------------


def _err_with_headers(status: int, headers: dict[str, str], message: str = "") -> Exception:
    """Build a duck-typed exception that exposes ``.response.headers``.

    Mirrors the surface ``parse_retry_after`` reads from
    ``httpx.HTTPStatusError`` without forcing tests to construct a real
    ``httpx.Response`` for every case.
    """
    response = type("R", (), {"status_code": status, "headers": dict(headers)})()
    exc = Exception(message or f"HTTP {status}")
    exc.status_code = status  # type: ignore[attr-defined]
    exc.response = response  # type: ignore[attr-defined]
    return exc


def test_parse_retry_after_numeric_seconds():
    from agents.llm_failover import parse_retry_after

    exc = _err_with_headers(429, {"Retry-After": "12"}, "rate limit")
    assert parse_retry_after(exc) == 12.0


def test_parse_retry_after_lowercase_header():
    from agents.llm_failover import parse_retry_after

    exc = _err_with_headers(429, {"retry-after": "3.5"}, "rate limit")
    assert parse_retry_after(exc) == 3.5


def test_parse_retry_after_http_date():
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime
    from agents.llm_failover import parse_retry_after

    target = datetime.now(timezone.utc) + timedelta(seconds=20)
    exc = _err_with_headers(429, {"Retry-After": format_datetime(target)})
    parsed = parse_retry_after(exc)
    assert parsed is not None
    # Allow a small tolerance for clock drift between produce + parse.
    assert 15 <= parsed <= 25


def test_parse_retry_after_returns_none_without_header():
    from agents.llm_failover import parse_retry_after

    exc = _err_with_headers(429, {}, "rate limit")
    assert parse_retry_after(exc) is None


def test_parse_retry_after_returns_none_when_no_response_attr():
    from agents.llm_failover import parse_retry_after

    exc = type("E", (Exception,), {"status_code": 429})("rate limit hit")
    assert parse_retry_after(exc) is None


def test_parse_retry_after_clamps_negative_to_zero():
    from agents.llm_failover import parse_retry_after

    exc = _err_with_headers(429, {"Retry-After": "-5"})
    assert parse_retry_after(exc) == 0.0


def test_parse_retry_after_clamps_to_max_seconds():
    from agents.llm_failover import parse_retry_after

    exc = _err_with_headers(429, {"Retry-After": "999999"})
    # Default cap is 24h.
    assert parse_retry_after(exc) == 24 * 3600.0


def test_parse_retry_after_invalid_header_returns_none():
    from agents.llm_failover import parse_retry_after

    exc = _err_with_headers(429, {"Retry-After": "later please"})
    assert parse_retry_after(exc) is None


# ── ProviderCooldownTracker honours Retry-After ------------------


def test_record_failure_uses_retry_after_when_longer_than_default():
    from agents.llm_failover import (
        FailoverReason, ProviderCooldownTracker,
    )

    ct = ProviderCooldownTracker()
    # Default RATE_LIMIT cooldown is 60s; upstream wants 180s.
    ct.record_failure("openai", FailoverReason.RATE_LIMIT, retry_after=180)
    until = ct._cooldowns["openai"]
    remaining = until - time.time()
    assert 170 < remaining <= 181


def test_record_failure_keeps_default_when_retry_after_shorter():
    from agents.llm_failover import (
        FailoverReason, ProviderCooldownTracker,
    )

    ct = ProviderCooldownTracker()
    ct.record_failure("openai", FailoverReason.RATE_LIMIT, retry_after=5)
    remaining = ct._cooldowns["openai"] - time.time()
    # Default 60s wins because it's longer than the 5s upstream hint.
    assert 55 < remaining <= 61


def test_record_failure_ignores_retry_after_for_auth_reasons():
    from agents.llm_failover import (
        FailoverReason, ProviderCooldownTracker,
    )

    ct = ProviderCooldownTracker()
    ct.record_failure("openai", FailoverReason.AUTH_PERMANENT, retry_after=10)
    remaining = ct._cooldowns["openai"] - time.time()
    # AUTH_PERMANENT is 24h, the 10s hint must not shrink it.
    assert remaining > 60 * 60 * 23


def test_record_failure_caps_retry_after_at_max_cooldown():
    from agents.llm_failover import (
        FailoverReason, ProviderCooldownTracker, RETRY_AFTER_MAX_COOLDOWN,
    )

    ct = ProviderCooldownTracker()
    ct.record_failure(
        "openai", FailoverReason.RATE_LIMIT, retry_after=99 * 24 * 3600,
    )
    remaining = ct._cooldowns["openai"] - time.time()
    assert remaining <= RETRY_AFTER_MAX_COOLDOWN + 1
    assert remaining > RETRY_AFTER_MAX_COOLDOWN - 5


def test_cooldown_tracker_persists_state_to_disk(tmp_path):
    from agents.llm_failover import FailoverReason, ProviderCooldownTracker

    path = tmp_path / "cooldown-state.json"
    first = ProviderCooldownTracker(storage_path=str(path))
    first.record_failure("openai", FailoverReason.RATE_LIMIT, retry_after=120)
    assert path.exists()

    second = ProviderCooldownTracker(storage_path=str(path))
    assert second.is_available("openai") is False


def test_cooldown_tracker_success_clears_persisted_state(tmp_path):
    from agents.llm_failover import FailoverReason, ProviderCooldownTracker

    path = tmp_path / "cooldown-state.json"
    first = ProviderCooldownTracker(storage_path=str(path))
    first.record_failure("openai", FailoverReason.RATE_LIMIT, retry_after=120)
    first.record_success("openai")

    second = ProviderCooldownTracker(storage_path=str(path))
    assert second.is_available("openai") is True
    assert "openai" not in second._cooldowns


# ── _retry_llm_call honours Retry-After / overrides --------------


@pytest.mark.asyncio
async def test_retry_llm_call_honors_short_retry_after(monkeypatch):
    from agents import llm_failover

    sleeps: list[float] = []

    async def fake_sleep(seconds: float):
        sleeps.append(seconds)

    monkeypatch.setattr(llm_failover.asyncio, "sleep", fake_sleep)

    attempts = {"n": 0}

    async def coro():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _err_with_headers(429, {"Retry-After": "3"}, "429 rate limited")
        return {"ok": True}

    result = await llm_failover._retry_llm_call(coro)
    assert result == {"ok": True}
    # Static delay would have been 1s; Retry-After=3s wins (still under
    # the 5s inline-sleep cap so we honour it instead of raising).
    assert sleeps == [3.0]


@pytest.mark.asyncio
async def test_retry_llm_call_aborts_when_retry_after_exceeds_inline_cap(monkeypatch):
    from agents import llm_failover

    sleeps: list[float] = []

    async def fake_sleep(seconds: float):
        sleeps.append(seconds)

    monkeypatch.setattr(llm_failover.asyncio, "sleep", fake_sleep)

    async def coro():
        raise _err_with_headers(429, {"Retry-After": "60"}, "429 rate limited")

    with pytest.raises(Exception) as info:
        await llm_failover._retry_llm_call(coro)
    # Original 429 is propagated unchanged.
    assert "429" in str(info.value)
    # No inline sleep was attempted — we handed control back immediately.
    assert sleeps == []


@pytest.mark.asyncio
async def test_retry_llm_call_overrides_max_retries(monkeypatch):
    from agents import llm_failover

    sleeps: list[float] = []

    async def fake_sleep(seconds: float):
        sleeps.append(seconds)

    monkeypatch.setattr(llm_failover.asyncio, "sleep", fake_sleep)

    attempts = {"n": 0}

    async def coro():
        attempts["n"] += 1
        raise RuntimeError("503 overloaded")

    with pytest.raises(RuntimeError):
        await llm_failover._retry_llm_call(
            coro, max_retries=2, delays=[0.5],
        )

    # Only 2 attempts (down from default 3) and only 1 sleep between them.
    assert attempts["n"] == 2
    assert sleeps == [0.5]


# ── chat_with_failover wiring ------------------------------------


def _make_failover_llm(fallbacks: list[str], call_side_effect=None):
    """Build a minimally-wired LLMProvider for chat_with_failover tests."""
    from agents.llm_provider import LLMProvider, ProviderCooldownTracker

    llm = LLMProvider.__new__(LLMProvider)
    llm._config = {"fallback_providers": fallbacks}
    llm._local_engine = None
    llm.provider = "openai"
    llm.model = "gpt-4o-mini"
    llm.api_key = "sk-x"
    llm.base_url = "https://api.openai.com/v1"
    llm._last_budget_routing = {}
    llm._messages_contain_vision = lambda m: False  # type: ignore
    llm._cooldown = ProviderCooldownTracker()

    candidates: list[tuple[str, dict]] = [
        ("openai", {
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-x",
            "model": "gpt-4o-mini",
            "supported": True,
        }),
    ]
    for fb in fallbacks:
        candidates.append((fb, {
            "base_url": "https://example/" + fb,
            "api_key": "k",
            "model": "m",
            "supported": True,
        }))
    llm._build_candidate_list = MagicMock(return_value=candidates)  # type: ignore[attr-defined]

    llm._call_provider = AsyncMock(side_effect=call_side_effect)  # type: ignore[attr-defined]
    return llm


@pytest.mark.asyncio
async def test_chat_with_failover_uses_fast_retry_when_fallback_present():
    """Multi-candidate path must dial down same-provider retry budget."""
    async def first_provider_500_then_fallback_ok(provider_name, *a, **kw):
        if provider_name == "openai":
            raise RuntimeError("503 overloaded")
        return {"choices": [{"message": {"content": "from-fb"}}]}

    llm = _make_failover_llm(["ollama"], call_side_effect=first_provider_500_then_fallback_ok)
    out = await llm.chat_with_failover([{"role": "user", "content": "hi"}])
    assert out["choices"][0]["message"]["content"] == "from-fb"
    # Two _call_provider attempts: primary then fallback.
    assert llm._call_provider.await_count == 2
    primary_call = llm._call_provider.await_args_list[0]
    # Fast-retry overrides MUST be plumbed through to _call_provider.
    assert primary_call.kwargs.get("_retry_max") == 2
    assert primary_call.kwargs.get("_retry_delays") == [0.5]


@pytest.mark.asyncio
async def test_chat_with_failover_uses_default_retry_when_no_fallback():
    """Single-candidate path keeps the historical retry policy."""
    async def ok(provider_name, *a, **kw):
        return {"choices": [{"message": {"content": "ok"}}]}

    llm = _make_failover_llm([], call_side_effect=ok)
    await llm.chat_with_failover([{"role": "user", "content": "hi"}])
    call = llm._call_provider.await_args_list[0]
    # No retry overrides => _call_provider falls through to its
    # historical _retry_llm_call(... ) defaults.
    assert "_retry_max" not in call.kwargs
    assert "_retry_delays" not in call.kwargs


@pytest.mark.asyncio
async def test_chat_with_failover_records_retry_after_against_cooldown():
    """A 429 with Retry-After must extend the rate-limit cooldown."""
    rate_limited = _err_with_headers(
        429, {"Retry-After": "180"}, "429 too many requests",
    )

    async def primary_429_then_fb_ok(provider_name, *a, **kw):
        if provider_name == "openai":
            raise rate_limited
        return {"choices": [{"message": {"content": "fb"}}]}

    llm = _make_failover_llm(["ollama"], call_side_effect=primary_429_then_fb_ok)
    out = await llm.chat_with_failover([{"role": "user", "content": "hi"}])
    assert out["choices"][0]["message"]["content"] == "fb"

    # The primary's cooldown must reflect the upstream's 180s hint
    # (well above the 60s rate-limit default).
    remaining = llm._cooldown._cooldowns["openai"] - time.time()
    assert 170 < remaining <= 181


@pytest.mark.asyncio
async def test_budget_routing_defers_over_budget_primary():
    async def ok(provider_name, *a, **kw):
        return {"choices": [{"message": {"content": provider_name}}]}

    llm = _make_failover_llm(["ollama"], call_side_effect=ok)
    llm._config = {
        "fallback_providers": ["ollama"],
        "daily_budget_usd": 1.0,
        "daily_spend_usd": 0.95,
        "budget_tight_ratio": 0.25,
    }
    llm._estimate_candidate_cost_usd = lambda provider, *_: {  # type: ignore[attr-defined]
        "openai": 0.20,
        "ollama": 0.01,
    }[provider]

    out = await llm.chat_with_failover([{"role": "user", "content": "hi"}])
    assert out["choices"][0]["message"]["content"] == "ollama"
    assert llm._call_provider.await_args_list[0].args[0] == "ollama"


@pytest.mark.asyncio
async def test_budget_routing_prefers_cheapest_when_headroom_is_tight():
    async def ok(provider_name, *a, **kw):
        return {"choices": [{"message": {"content": provider_name}}]}

    llm = _make_failover_llm(["ollama"], call_side_effect=ok)
    llm._config = {
        "fallback_providers": ["ollama"],
        "daily_budget_usd": 1.0,
        "daily_spend_usd": 0.90,  # headroom ratio = 0.1
        "budget_tight_ratio": 0.50,
    }
    llm._estimate_candidate_cost_usd = lambda provider, *_: {  # type: ignore[attr-defined]
        "openai": 0.05,
        "ollama": 0.01,
    }[provider]

    out = await llm.chat_with_failover([{"role": "user", "content": "hi"}])
    assert out["choices"][0]["message"]["content"] == "ollama"
    assert llm._call_provider.await_args_list[0].args[0] == "ollama"


@pytest.mark.asyncio
async def test_budget_routing_preserves_priority_when_headroom_is_healthy():
    async def ok(provider_name, *a, **kw):
        return {"choices": [{"message": {"content": provider_name}}]}

    llm = _make_failover_llm(["ollama"], call_side_effect=ok)
    llm._config = {
        "fallback_providers": ["ollama"],
        "daily_budget_usd": 10.0,
        "daily_spend_usd": 1.0,   # headroom ratio = 0.9
        "budget_tight_ratio": 0.25,
    }
    llm._estimate_candidate_cost_usd = lambda provider, *_: {  # type: ignore[attr-defined]
        "openai": 0.50,
        "ollama": 0.01,
    }[provider]

    out = await llm.chat_with_failover([{"role": "user", "content": "hi"}])
    assert out["choices"][0]["message"]["content"] == "openai"
    assert llm._call_provider.await_args_list[0].args[0] == "openai"
