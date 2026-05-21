"""Wave 2 Lane 09 — LLM router fix-pack regression tests.

Pins the surgical changes called out in
``ASOS/AUDIT-r14/findings/13-llm-core.md`` and the Lane 09 prompt:

1. Anthropic stream uses ``self.base_url`` (not the hard-coded URL)
   AND triggers ``_stream_via_nonstream_failover`` on pre-token failure.
2. ``reconfigure()`` threads ``base_url`` into ``switch_provider()``.
3. DeepSeek registry + adapter init both end with ``/v1``.
4. LM Studio failover honours configured base URL.
5. ``chat_with_failover`` returns ``last_failover`` metadata after a
   successful cross-provider hop.
6. ``BaseProvider.pricing_per_1k`` reads from ``model_catalog.json``.
7. Multi-key vault helper round-trips labeled keys.
8. ``LLMProvider.route_call`` returns the documented ``ProviderRef``
   shape for every (call_site, tier) combination.
9. ``CostBudget`` pre-flight surfaces structured ``budget_exceeded``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


pytestmark = pytest.mark.no_auto_feral_home


# ─────────────────────────────────────────────────────────────────────
# WS1 — Anthropic stream parity
# ─────────────────────────────────────────────────────────────────────


def test_anthropic_stream_uses_self_base_url_not_hardcoded():
    """The hard-coded ``https://api.anthropic.com/v1/messages`` literal
    is gone — every test that grepped the source for it must NOT
    find it inside ``_chat_stream_anthropic``."""
    src_path = Path(__file__).parent.parent / "agents" / "llm_provider.py"
    src = src_path.read_text(encoding="utf-8")
    # Locate _chat_stream_anthropic body.
    marker = "async def _chat_stream_anthropic"
    idx = src.find(marker)
    assert idx != -1, "_chat_stream_anthropic must exist"
    end = src.find("async def ", idx + len(marker))
    body = src[idx:end if end != -1 else len(src)]
    assert "https://api.anthropic.com/v1/messages" not in body, (
        "Anthropic stream MUST resolve the URL from self.base_url; "
        "hard-coded literal regressed (findings/13-llm-core.md fix #1)"
    )
    assert "self.base_url" in body, (
        "Anthropic stream body must reference self.base_url"
    )


def test_anthropic_stream_invokes_failover_on_pre_token_error():
    """Pre-token HTTPStatusError → ``_stream_via_nonstream_failover``
    fires and yields converted events (parity with OpenAI branch)."""
    from agents.llm_provider import LLMProvider

    llm = LLMProvider.__new__(LLMProvider)
    llm.provider = "anthropic"
    llm.model = "claude-opus-4-7"
    llm.api_key = "sk-ant-test"
    llm.base_url = "https://wrong.example/v1"
    llm._config = {"fallback_providers": ["openai"]}
    llm._messages_contain_vision = lambda m: False  # type: ignore
    llm._cooldown = MagicMock()
    llm._cooldown.record_failure = MagicMock()
    llm._cooldown._last_probe = {}

    fake_failover = AsyncMock(return_value=[
        {"type": "text_delta", "content": "from-failover"},
        {"type": "done"},
    ])
    llm._stream_via_nonstream_failover = fake_failover  # type: ignore

    # Patch httpx so the Anthropic stream raises a 401 BEFORE any token.
    class _FakeResp:
        status_code = 401
        text = "{\"error\": {\"type\": \"authentication_error\"}}"

        async def aread(self):
            return self.text.encode()

        def raise_for_status(self):
            req = httpx.Request("POST", "https://wrong.example/v1/messages")
            resp = httpx.Response(401, request=req, text=self.text)
            raise httpx.HTTPStatusError("401", request=req, response=resp)

        async def aiter_lines(self):
            return
            yield  # pragma: no cover

    class _FakeStream:
        async def __aenter__(self_inner):
            return _FakeResp()

        async def __aexit__(self_inner, *_args, **_kwargs):
            return False

    class _FakeClient:
        async def __aenter__(self_inner):
            return self_inner

        async def __aexit__(self_inner, *_args, **_kwargs):
            return False

        def stream(self_inner, *_args, **_kwargs):
            return _FakeStream()

    with patch("agents.llm_provider.httpx.AsyncClient", lambda *a, **k: _FakeClient()):
        async def _drain():
            events = []
            async for ev in llm._chat_stream_anthropic(
                [{"role": "user", "content": "hi"}],
            ):
                events.append(ev)
            return events

        events = asyncio.run(_drain())

    fake_failover.assert_awaited_once()
    assert {"type": "text_delta", "content": "from-failover"} in events
    assert {"type": "done"} in events


# ─────────────────────────────────────────────────────────────────────
# WS2 — base_url propagation, DeepSeek /v1, LM Studio failover URL
# ─────────────────────────────────────────────────────────────────────


def test_reconfigure_threads_base_url_into_switch_provider():
    """``reconfigure(base_url=...)`` MUST pass the kwarg through to
    ``switch_provider`` — the pre-W2 implementation set the env var
    only, leaving the running adapter pointed at the old URL."""
    from agents.llm_provider import LLMProvider

    llm = LLMProvider.__new__(LLMProvider)
    llm.provider = "openai"
    llm.model = "gpt-5"
    llm.api_key = "sk-old"
    llm.base_url = "https://api.openai.com/v1"
    llm.available = True
    llm.switch_provider = AsyncMock()  # type: ignore[attr-defined]

    asyncio.run(llm.reconfigure(
        provider="lmstudio",
        model="local-model",
        api_key="lm-studio",
        base_url="http://192.168.1.100:1234/v1",
    ))

    llm.switch_provider.assert_awaited_once()
    kwargs = llm.switch_provider.await_args.kwargs
    assert kwargs.get("base_url") == "http://192.168.1.100:1234/v1", (
        "reconfigure MUST forward base_url into switch_provider; "
        "see findings/13-llm-core.md fix #3"
    )


def test_deepseek_registry_ends_with_v1():
    """``_PROVIDER_REGISTRY['deepseek']`` MUST be normalised to ``/v1``
    so the failover candidate path agrees with the primary."""
    from agents.llm_provider import _PROVIDER_REGISTRY

    base, _ = _PROVIDER_REGISTRY["deepseek"]
    assert base.endswith("/v1"), (
        f"DeepSeek base URL must end with /v1 — got {base!r}; "
        "see findings/13-llm-core.md fix #3"
    )


def test_lmstudio_failover_honours_configured_base_url():
    """When the primary IS lmstudio with a non-default URL, the
    failover candidate config must reuse that URL (not hard-code
    localhost:1234)."""
    from agents.llm_provider import LLMProvider

    llm = LLMProvider.__new__(LLMProvider)
    llm.provider = "lmstudio"
    llm.base_url = "http://192.168.1.100:1234/v1"
    llm.api_key = "lm-studio"
    llm.model = "local-model"
    llm._config = {}

    cfg = llm._get_provider_config("lmstudio")
    assert cfg["base_url"] == "http://192.168.1.100:1234/v1", (
        "_get_provider_config(lmstudio) must echo self.base_url when "
        "lmstudio is the primary; see findings/13-llm-core.md fix #3"
    )


# ─────────────────────────────────────────────────────────────────────
# WS3 — last_failover metadata
# ─────────────────────────────────────────────────────────────────────


def test_chat_with_failover_records_last_failover_on_hop():
    """When the primary fails and a fallback succeeds, the response
    carries ``last_failover: {from, to, reason, candidates_tried}``
    AND ``self._last_failover`` is updated for the health snapshot."""
    from agents.llm_provider import LLMProvider, FailoverReason

    llm = LLMProvider.__new__(LLMProvider)
    llm.provider = "anthropic"
    llm.model = "claude-opus-4-7"
    llm.base_url = "https://api.anthropic.com/v1"
    llm.api_key = "sk-ant"
    llm.available = True
    llm._config = {"fallback_providers": ["openrouter"]}
    llm._messages_contain_vision = lambda m: False  # type: ignore
    llm._local_engine = None
    llm._cost_budget = None

    cooldown = MagicMock()
    cooldown.should_probe.return_value = True
    cooldown.record_success = MagicMock()
    cooldown.record_failure = MagicMock()
    llm._cooldown = cooldown

    candidates = [
        ("anthropic", {
            "base_url": "https://api.anthropic.com/v1",
            "api_key": "sk-ant", "model": "claude-opus-4-7",
            "supported": True,
        }),
        ("openrouter", {
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "or-test", "model": "openai/gpt-5",
            "supported": True,
        }),
    ]
    llm._build_candidate_list = MagicMock(return_value=candidates)  # type: ignore
    llm._route_candidates_with_budget = MagicMock(return_value=(candidates, {}))  # type: ignore

    primary_err = httpx.HTTPStatusError(
        "401",
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        response=httpx.Response(
            401,
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
            text="{\"error\": {\"message\": \"key 401\"}}",
        ),
    )

    async def _fake_call_provider(provider_name, config, messages, tools, **kwargs):
        if provider_name == "anthropic":
            raise primary_err
        return {
            "choices": [{"message": {"content": "from-or"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

    llm._call_provider = _fake_call_provider  # type: ignore[assignment]

    out = asyncio.run(llm.chat_with_failover(
        [{"role": "user", "content": "hi"}],
    ))
    assert out["choices"][0]["message"]["content"] == "from-or"
    assert "last_failover" in out, (
        "chat_with_failover MUST surface last_failover metadata when a "
        "cross-provider hop happened; see findings/13-llm-core.md fix #5"
    )
    lf = out["last_failover"]
    assert lf["from"] == "anthropic"
    assert lf["to"] == "openrouter"
    assert lf["reason"] in {r.value for r in FailoverReason}
    assert any(c["provider"] == "anthropic" for c in lf["candidates_tried"])
    assert llm._last_failover is not None


def test_health_snapshot_exposes_last_failover():
    from agents.llm_provider import LLMProvider, ProviderCooldownTracker

    llm = LLMProvider.__new__(LLMProvider)
    llm.provider = "openai"
    llm.model = "gpt-5"
    llm.api_key = "sk-x"
    llm.base_url = "https://api.openai.com/v1"
    llm.available = True
    llm._config = {"fallback_providers": []}
    llm._cooldown = ProviderCooldownTracker()
    llm._build_candidate_list = MagicMock(return_value=[  # type: ignore
        ("openai", {"model": "gpt-5", "api_key": "sk-x", "base_url": "https://api.openai.com/v1", "supported": True}),
    ])
    llm._last_budget_routing = {}
    llm._last_failover = {
        "from": "anthropic",
        "to": "openai",
        "reason": "auth_permanent",
        "candidates_tried": [{"provider": "anthropic", "reason": "auth_permanent"}],
    }
    snap = llm.health_snapshot()
    assert snap["last_failover"]["from"] == "anthropic"
    assert snap["last_failover"]["to"] == "openai"


# ─────────────────────────────────────────────────────────────────────
# WS5 — Pricing source-of-truth = model_catalog.json
# ─────────────────────────────────────────────────────────────────────


def test_base_provider_pricing_reads_catalog_first(tmp_path, monkeypatch):
    """``BaseProvider.pricing_per_1k`` MUST resolve through the
    canonical catalog before falling back to adapter ``_pricing``."""
    from cost import pricing as pricing_mod
    from providers.base import BaseProvider

    catalog = {
        "providers": {
            "test_provider": {
                "models": ["test-model"],
                "pricing": {
                    "test-model": {"input": 0.001, "output": 0.002},
                },
            }
        }
    }
    catalog_path = tmp_path / "model_catalog.json"
    catalog_path.write_text(json.dumps(catalog))

    fake_pricing = pricing_mod.ModelPricing(catalog_path=catalog_path)
    monkeypatch.setattr(pricing_mod, "_SHARED_PRICING", fake_pricing)

    class _Adapter(BaseProvider):
        provider_id = "test_provider"
        # Adapter-local override that should be IGNORED when catalog has
        # the entry — that's the entire point of the consolidation.
        _pricing = {"test-model": {"input": 99.0, "output": 99.0}}

    rates = _Adapter().pricing_per_1k("test-model")
    assert rates == {"input": 0.001, "output": 0.002}


def test_base_provider_pricing_falls_back_to_adapter_for_unknown(tmp_path, monkeypatch):
    """Catalog has no entry → adapter override is consulted."""
    from cost import pricing as pricing_mod
    from providers.base import BaseProvider

    catalog_path = tmp_path / "model_catalog.json"
    catalog_path.write_text(json.dumps({"providers": {}}))

    fake_pricing = pricing_mod.ModelPricing(catalog_path=catalog_path)
    monkeypatch.setattr(pricing_mod, "_SHARED_PRICING", fake_pricing)

    class _Adapter(BaseProvider):
        provider_id = "x"
        _pricing = {"community-model": {"input": 0.01, "output": 0.02}}

    rates = _Adapter().pricing_per_1k("community-model")
    assert rates == {"input": 0.01, "output": 0.02}


# ─────────────────────────────────────────────────────────────────────
# WS7 — Multi-key vault helper
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def isolated_vault(tmp_path, monkeypatch):
    """Build a fresh BlindVault rooted at tmp_path so the labeled-keys
    namespace isolation is observable without touching ~/.feral."""
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    from security.vault import BlindVault

    return BlindVault(vault_path=str(tmp_path / "vault.json"))


def test_vault_keys_round_trip_add_list_remove(isolated_vault):
    from security import vault_keys

    entry = vault_keys.add_provider_key(
        "openai", "prod", "sk-prod-test", vault=isolated_vault,
    )
    assert entry.provider_id == "openai"
    assert entry.label == "prod"
    assert entry.fingerprint  # not empty
    assert entry.is_active is False  # explicit set_active not requested

    listing = vault_keys.list_provider_keys("openai", vault=isolated_vault)
    assert len(listing) == 1
    assert listing[0].label == "prod"

    raw = vault_keys.get_provider_key("openai", "prod", vault=isolated_vault)
    assert raw == "sk-prod-test"

    removed = vault_keys.remove_provider_key(
        "openai", "prod", vault=isolated_vault,
    )
    assert removed is True
    assert vault_keys.list_provider_keys("openai", vault=isolated_vault) == []


def test_vault_keys_set_active_label_round_trip(isolated_vault):
    from security import vault_keys

    vault_keys.add_provider_key("anthropic", "dev", "sk-dev", vault=isolated_vault)
    vault_keys.add_provider_key("anthropic", "prod", "sk-prod", vault=isolated_vault)

    vault_keys.set_active_label("anthropic", "prod", vault=isolated_vault)
    assert vault_keys.get_active_label("anthropic", vault=isolated_vault) == "prod"

    active_secret = vault_keys.get_active_provider_key(
        "anthropic", vault=isolated_vault,
    )
    assert active_secret == "sk-prod"


def test_vault_keys_validates_provider_id_and_label():
    from security import vault_keys

    with pytest.raises(vault_keys.InvalidProviderId):
        vault_keys.add_provider_key("openai!", "prod", "sk-x")
    with pytest.raises(vault_keys.InvalidLabel):
        vault_keys.add_provider_key("openai", "prod with space", "sk-x")


# ─────────────────────────────────────────────────────────────────────
# WS8 — route_call(call_site, prompt) -> ProviderRef
# ─────────────────────────────────────────────────────────────────────


class TestRouteCall:
    def _llm(self, config: dict | None = None):
        from agents.llm_provider import LLMProvider

        llm = LLMProvider.__new__(LLMProvider)
        llm.provider = "openai"
        llm.model = "gpt-5"
        llm._config = config or {}
        return llm

    def test_route_call_default_chat_returns_balanced_tier(self):
        ref = self._llm().route_call("chat")
        assert ref["call_site"] == "chat"
        assert ref["tier"] == "balanced"
        assert ref["provider"]
        assert ref["model"]
        assert isinstance(ref["supported"], bool)
        assert isinstance(ref["fallback_providers"], list)

    def test_route_call_default_routing_is_cheap(self):
        ref = self._llm().route_call("routing")
        assert ref["tier"] == "cheap"

    def test_route_call_explicit_tier_override(self):
        ref = self._llm().route_call("chat", tier="premium")
        assert ref["tier"] == "premium"
        # Premium chat default targets a frontier model — anthropic
        # opus by the bundled defaults.
        assert ref["provider"] in {"anthropic", "openai"}

    def test_route_call_unknown_call_site_raises(self):
        with pytest.raises(ValueError):
            self._llm().route_call("not-a-real-call-site")

    def test_route_call_settings_override(self):
        cfg = {
            "tier_map": {
                "chat": {
                    "balanced": {"provider": "openrouter", "model": "anthropic/claude-haiku-4-5"},
                }
            },
            "fallback_providers": ["openai"],
        }
        ref = self._llm(cfg).route_call("chat")
        assert ref["provider"] == "openrouter"
        assert ref["model"] == "anthropic/claude-haiku-4-5"
        assert ref["fallback_providers"] == ["openai"]


# ─────────────────────────────────────────────────────────────────────
# WS9 — CostBudget gate surfaces structured response
# ─────────────────────────────────────────────────────────────────────


def test_chat_with_failover_returns_budget_exceeded_block():
    from agents.llm_provider import LLMProvider

    llm = LLMProvider.__new__(LLMProvider)
    llm.provider = "openai"
    llm.model = "gpt-5"
    llm._config = {"fallback_providers": []}
    llm._messages_contain_vision = lambda m: False  # type: ignore
    llm._local_engine = None

    fake_budget = MagicMock()
    fake_budget.ensure_ready = AsyncMock()
    fake_budget.check_and_reserve = MagicMock(return_value=False)
    fake_budget._cap_for = MagicMock(return_value=0.10)
    fake_budget.current_spend = MagicMock(return_value=0.42)
    llm._cost_budget = fake_budget

    out = asyncio.run(llm.chat_with_failover(
        [{"role": "user", "content": "hi"}],
        call_site="chat",
        max_tokens=128,
    ))
    assert out["choices"] == []
    assert "budget_exceeded" in out
    payload = out["budget_exceeded"]
    assert payload["call_site"] == "chat"
    assert payload["window"] == "hour"
    assert payload["cap_dollars"] == pytest.approx(0.10)
    assert payload["current_dollars"] == pytest.approx(0.42)


def test_extract_usage_picks_up_reasoning_tokens():
    """Reasoning tokens land in ``completion_tokens_details.reasoning_tokens``
    on OpenAI Responses-style outputs and are billed at the output rate
    (Wave 1 Lane 04 + W2 fix #5)."""
    from agents.llm_provider import LLMProvider

    result = {
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 200,
            "completion_tokens_details": {"reasoning_tokens": 50},
        }
    }
    prompt, completion, reasoning = LLMProvider._extract_usage(result)
    assert prompt == 100
    assert completion == 200
    assert reasoning == 50


def test_extract_usage_anthropic_shape():
    from agents.llm_provider import LLMProvider

    result = {"usage": {"input_tokens": 7, "output_tokens": 11}}
    prompt, completion, reasoning = LLMProvider._extract_usage(result)
    assert (prompt, completion, reasoning) == (7, 11, 0)
