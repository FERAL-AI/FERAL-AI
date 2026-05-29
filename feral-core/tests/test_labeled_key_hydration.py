"""v2026.5.46 — labeled vault keys hydrate into env at boot so
one key reaches every env-reading surface (probe, realtime proxy,
whisper/TTS), not just the chat router.

Background
----------

Cross-cut #1 of v2026.5.42 wired ``vault_keys.get_active_key`` into
the LLM hot path (chat). Commit ``01eda5d9`` extended the same
resolution to the voice router. But three surfaces still read
``os.getenv("OPENAI_API_KEY")`` (or equivalent) directly:

  * ``api/state.py::_load_stored_credentials`` (boot hydration)
  * ``security/probe.py::_resolve_env_or_vault`` (probe path)
  * ``voice/realtime_proxy.py`` (proxy + session construction)

Result: a ``feral key add --provider openai --label default --set-active``
flow that did NOT also touch the default namespace stranded the
key in ``provider_keys``. Chat worked (router fix), but
``/api/voice/providers`` reported every OpenAI surface as
``unauthorized`` and the realtime WS handshake started with no
auth header.

These tests pin the fix:

  * boot hydration picks up the active labeled key into env when
    no env / no default-namespace entry exists;
  * explicit env wins over labeled (highest-precedence source);
  * probe path resolves the labeled key BEFORE a restart so the
    operator's first "did I add it right?" probe succeeds;
  * with no labeled key configured, behavior is unchanged
    (regression guard for the existing default-namespace paths).
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.no_auto_feral_home


# ─────────────────────────────────────────────────────────────────────
# Fixture — isolated in-memory keychain + scrubbed env + reset vault.
# Same pattern as tests/test_llm_vault_hot_path.py so the labeled-key
# overlay lands on an isolated BlindVault rooted at tmp_path.
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_keychain(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    monkeypatch.delenv("FERAL_VAULT_RECOVERY_CODE", raising=False)
    for env_var in (
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
        "GROQ_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY",
        "MOONSHOT_API_KEY", "DASHSCOPE_API_KEY", "GOOGLE_API_KEY",
        "TAVILY_API_KEY",
    ):
        monkeypatch.delenv(env_var, raising=False)

    store: dict[tuple[str, str], str] = {}

    import security.vault as vault_mod
    monkeypatch.setattr(
        vault_mod, "_keyring_get_password",
        lambda service, username: store.get((service, username)),
    )
    monkeypatch.setattr(
        vault_mod, "_keyring_set_password",
        lambda service, username, password: store.__setitem__(
            (service, username), password,
        ),
    )
    monkeypatch.setattr(
        vault_mod, "_keyring_delete_password",
        lambda service, username: store.pop((service, username), None),
    )

    vault_mod.reset_vault()
    yield store
    vault_mod.reset_vault()


# ─────────────────────────────────────────────────────────────────────
# Boot hydration — the central fix.
# ─────────────────────────────────────────────────────────────────────


def test_boot_hydrates_labeled_only_key_into_env(fake_keychain, monkeypatch):
    """A ``feral key add --provider openai --label default --set-active``
    flow with NO env var AND NO default-namespace entry must still
    populate ``os.environ["OPENAI_API_KEY"]`` at boot. This is what
    makes every legacy ``os.getenv`` reader (probe, realtime proxy,
    whisper, TTS, third-party SDKs) see the same key as chat."""
    from security.vault_keys import add_provider_key
    add_provider_key("openai", "default", "sk-labeled-only", set_active=True)

    from api.state import BrainState
    BrainState._load_stored_credentials()

    import os
    assert os.environ.get("OPENAI_API_KEY") == "sk-labeled-only"


def test_boot_hydrates_each_provider_in_env_keys_map(fake_keychain, monkeypatch):
    """Iterate every provider in ``_PROVIDER_ENV_KEYS`` — not just
    OpenAI. A labeled-only Anthropic / Groq / Gemini / OpenRouter /
    DeepSeek / Kimi / Qwen key must hydrate the matching env var."""
    from security.vault_keys import add_provider_key, _PROVIDER_ENV_KEYS

    fixtures = {
        "openai": ("OPENAI_API_KEY", "sk-openai-l"),
        "anthropic": ("ANTHROPIC_API_KEY", "sk-ant-l"),
        "gemini": ("GEMINI_API_KEY", "gem-l"),
        "groq": ("GROQ_API_KEY", "gk-l"),
        "openrouter": ("OPENROUTER_API_KEY", "or-l"),
        "deepseek": ("DEEPSEEK_API_KEY", "ds-l"),
        "kimi": ("MOONSHOT_API_KEY", "moon-l"),
        "qwen": ("DASHSCOPE_API_KEY", "dash-l"),
    }
    # Regression guard: if a new provider lands in ``_PROVIDER_ENV_KEYS``
    # without a fixture here, this test should fail so we remember to
    # cover it. Use symmetric difference rather than `==` so the
    # failure message points at the missing id directly.
    assert set(fixtures.keys()) == set(_PROVIDER_ENV_KEYS.keys()), (
        f"fixture/provider drift: {set(fixtures.keys()) ^ set(_PROVIDER_ENV_KEYS.keys())}"
    )

    for pid, (env_var, secret) in fixtures.items():
        monkeypatch.delenv(env_var, raising=False)
        add_provider_key(pid, "default", secret, set_active=True)

    from api.state import BrainState
    BrainState._load_stored_credentials()

    import os
    for pid, (env_var, secret) in fixtures.items():
        assert os.environ.get(env_var) == secret, (
            f"labeled-active key for {pid} did not hydrate {env_var}"
        )


def test_boot_does_not_clobber_explicit_env(fake_keychain, monkeypatch):
    """Precedence rule 1: explicit env beats labeled active. A
    deployment-time / CI / shell override must not be overwritten by
    the labeled-keys overlay."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-explicit-env")

    from security.vault_keys import add_provider_key
    add_provider_key("openai", "default", "sk-labeled-loses", set_active=True)

    from api.state import BrainState
    BrainState._load_stored_credentials()

    import os
    assert os.environ["OPENAI_API_KEY"] == "sk-explicit-env"


def test_boot_no_labeled_keys_behavior_unchanged(fake_keychain, monkeypatch):
    """Regression guard: with no labeled-active key configured at all,
    the existing default-namespace vault fallback continues to win.
    Pre-fix tests in tests/test_state.py pin this path; the labeled
    overlay must not perturb it."""
    from security.vault import get_vault
    get_vault().set_credential("OPENAI_API_KEY", "sk-legacy-default")

    from api.state import BrainState
    BrainState._load_stored_credentials()

    import os
    assert os.environ.get("OPENAI_API_KEY") == "sk-legacy-default"


def test_boot_labeled_beats_default_namespace_vault(fake_keychain, monkeypatch):
    """Precedence rule 2: an active labeled key beats a default-namespace
    vault entry. The default-namespace entry is typically a stale relic
    of the original setup wizard; ``feral key add --set-active`` is the
    operator's most recent explicit choice and should win.

    This is the scenario the bug brief described — pre-fix, the operator
    had to MANUALLY copy the labeled key into the default namespace to
    make probe / realtime / whisper / TTS go green. Post-fix that copy
    happens automatically and the labeled active wins over the older
    default-namespace value if both exist.
    """
    from security.vault import get_vault
    from security.vault_keys import add_provider_key

    get_vault().set_credential("OPENAI_API_KEY", "sk-stale-default")
    add_provider_key("openai", "prod", "sk-fresh-labeled", set_active=True)

    from api.state import BrainState
    BrainState._load_stored_credentials()

    import os
    assert os.environ.get("OPENAI_API_KEY") == "sk-fresh-labeled"


# ─────────────────────────────────────────────────────────────────────
# Probe path — resolves labeled key without a restart.
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_resolves_labeled_key_without_restart(fake_keychain, monkeypatch):
    """The operator's first "did I add this right?" probe runs BEFORE
    any restart, so boot hydration hasn't run for the new key yet.
    The probe path therefore has to resolve the labeled key itself —
    not just rely on env. Pre-fix this returned ``no_key``; post-fix
    it builds an Authorization header and the HTTP layer reports the
    real upstream verdict.
    """
    from security.vault_keys import add_provider_key
    add_provider_key("openai", "default", "sk-probe-labeled", set_active=True)

    captured: dict[str, object] = {}

    class _FakeResponse:
        status_code = 200
        reason_phrase = "OK"
        text = ""

    class _FakeAsyncClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def request(self, method, url, headers=None, params=None):
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = headers
            captured["params"] = params
            return _FakeResponse()

    monkeypatch.setattr("security.probe.httpx.AsyncClient", _FakeAsyncClient)

    from security.probe import clear_probe_cache, probe

    clear_probe_cache()

    result = await probe("openai_realtime", force=True)

    assert result.ok is True
    assert result.reason == "ok"
    auth = (captured.get("headers") or {}).get("Authorization", "")
    assert auth == "Bearer sk-probe-labeled", (
        f"probe did not pick up the labeled-active key: {auth!r}"
    )


@pytest.mark.asyncio
async def test_probe_unconfigured_provider_short_circuits_no_key(
    fake_keychain, monkeypatch,
):
    """Regression guard: with no labeled key, no env, no default-namespace,
    the probe still short-circuits to ``no_key`` (does NOT round-trip).
    The labeled-key overlay must not break the ``had_key=False`` path
    that prevents wasted ``Authorization: Bearer`` calls with no token.
    """
    from security.probe import clear_probe_cache, probe

    clear_probe_cache()
    result = await probe("openai_realtime", force=True)

    assert result.ok is False
    assert result.reason == "no_key"
    assert result.status_code is None


# ─────────────────────────────────────────────────────────────────────
# Realtime proxy — picks up labeled key at construction.
# ─────────────────────────────────────────────────────────────────────


def test_realtime_proxy_picks_up_labeled_key_at_construction(
    fake_keychain, monkeypatch,
):
    """``RealtimeProxy.__init__`` captures ``OPENAI_API_KEY`` once at
    construction. Pre-fix it called ``os.getenv("OPENAI_API_KEY", "")``
    directly, so a labeled-only key (no env) left
    ``self._api_key = ""`` and every voice session started without an
    auth header. Post-fix it routes through
    ``vault_keys.get_active_key`` and the labeled secret reaches the
    proxy without any explicit env hydration."""
    from security.vault_keys import add_provider_key
    add_provider_key("openai", "default", "sk-proxy-labeled", set_active=True)

    from voice.realtime_proxy import RealtimeProxy
    proxy = RealtimeProxy()

    assert proxy._api_key == "sk-proxy-labeled"
    assert proxy.available is True


def test_realtime_session_picks_up_labeled_key_when_no_explicit_arg(
    fake_keychain, monkeypatch,
):
    """Same contract for the per-session ``RealtimeSession``: when the
    caller doesn't pass an explicit ``api_key``, the session must
    resolve through the labeled overlay. The proxy normally hands the
    key in explicitly, but the construction-time default is the
    safety net for any caller that builds a session ad-hoc (tests,
    one-off scripts)."""
    from security.vault_keys import add_provider_key
    add_provider_key("openai", "default", "sk-session-labeled", set_active=True)

    from voice.realtime_proxy import RealtimeSession
    rs = RealtimeSession(session_id="s1", node_id="n1")

    assert rs._api_key == "sk-session-labeled"
