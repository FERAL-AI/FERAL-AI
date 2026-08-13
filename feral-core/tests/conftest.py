import pytest
import os
import warnings


# Raise the rate-limit ceiling well before the server module imports so
# CI test runs (120 req/min default is too low once we have 200+ route
# tests) don't hit 429s on one of the Hardware / config tests. Keep the
# legacy 120 in production by setting this only when FERAL_RATE_LIMIT_RPM
# is unset.
os.environ.setdefault("FERAL_RATE_LIMIT_RPM", "10000")


# Keep freezegun away from `transformers`, or the whole suite wedges.
#
# On entry, `freeze_time` walks every module in `sys.modules` and calls
# `getattr` on each attribute, looking for datetime objects to patch.
# `transformers` uses a LAZY module `__getattr__` that imports a submodule
# on attribute access, so that scan drags in `transformers.agents.agents`
# -> `pandas` -> the entire import tree, one heavyweight import per
# attribute name.
#
# Measured standalone with `transformers` already loaded: a bare
# `freeze_time(...)` entry does not complete in 120s; with this ignore it
# takes 0.3s. In a full-suite run the effect is worse, because any earlier
# test that imports `transformers` arms it for every freeze_time after --
# observed as `tests/test_pairing_hash.py::test_freezegun_simulates_ttl_
# passage` sitting at 100% CPU for 39 minutes with no output.
#
# The tests that freeze time care about this package's own TTL logic;
# none of them assert anything about datetimes inside `transformers`, so
# excluding it from the patch scan costs nothing. Set here rather than
# per-call so a future `freeze_time` cannot reintroduce the hang.
try:
    import freezegun

    freezegun.configure(extend_ignore_list=["transformers"])
except Exception:  # pragma: no cover - freezegun is a test-only dep
    pass


# : soak tests are gated behind
# `--runsoak`. Without the flag every test marked `@pytest.mark.soak` is
# skipped so the regular CI run stays fast and deterministic.
def pytest_addoption(parser):
    parser.addoption(
        "--runsoak",
        action="store_true",
        default=False,
        help="run @pytest.mark.soak tests (long-duration voice/channel soak)",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--runsoak"):
        return
    skip_soak = pytest.mark.skip(reason="needs --runsoak option to run")
    for item in items:
        if "soak" in item.keywords:
            item.add_marker(skip_soak)


@pytest.fixture(autouse=True)
def _disable_api_key_middleware_for_tests(monkeypatch, isolate_feral_home):
    """Starlette TestClient reports client host as 'testclient'; accept that as localhost
    for tests so the auth middleware bypasses without every test needing to send a header.
    Real production hosts never report 'testclient'.

    Depends on ``isolate_feral_home`` on purpose, and the dependency is
    load-bearing rather than cosmetic. This fixture imports ``api.server``,
    which imports ``api.state``, which instantiates ``BrainState()`` at
    module scope (``api/state.py``) and pushes ``export_as_env()`` into
    ``os.environ``. That import happens exactly once per process, so
    whichever test runs first decides which home the whole session reads.
    Ordered by definition alone this fixture ran first and that home was
    the developer's real ``~/.feral``, leaking ``FERAL_LLM_PROVIDER``,
    ``FERAL_STREAMING`` and friends process-wide. Naming the fixture as a
    parameter makes pytest resolve it first regardless of definition order.
    """
    from security import session_auth as _sa
    orig_is_localhost = _sa.is_localhost

    def _is_localhost_test(host):
        if host == "testclient":
            return True
        return orig_is_localhost(host)

    monkeypatch.setattr(_sa, "is_localhost", _is_localhost_test)
    try:
        import api.server as _server
        monkeypatch.setattr(_server, "is_localhost", _is_localhost_test, raising=False)
    except Exception:
        pass


@pytest.fixture
def temp_db(tmp_path):
    """Provide a temporary SQLite database path."""
    return str(tmp_path / "test_memory.db")


@pytest.fixture
def mock_vault():
    """A mock vault that returns test keys."""
    class MockVault:
        def get(self, key, default=""):
            return f"test-{key}"
        def inject_headers(self, skill_id, headers):
            return headers
    return MockVault()


@pytest.fixture(autouse=True)
def isolate_feral_home(request, tmp_path, monkeypatch):
    """Isolate tests from real ~/.feral directory.
    Skips for tests that manage FERAL_HOME themselves.
    """
    markers = {m.name for m in request.node.iter_markers()}
    if "no_auto_feral_home" in markers:
        return
    module_markers = getattr(request.module, "pytestmark", [])
    if any(getattr(m, "name", "") == "no_auto_feral_home" for m in (module_markers if isinstance(module_markers, list) else [module_markers])):
        return
    feral_dir = tmp_path / ".feral-isolation"
    monkeypatch.setenv("FERAL_HOME", str(feral_dir))
    os.makedirs(feral_dir, exist_ok=True)


@pytest.fixture(autouse=True)
def restore_skill_implementations():
    """Snapshot + restore ``skills.impl.SKILL_IMPLEMENTATIONS`` around tests.

    CI-flake fix (P1, test order-independence): several test modules
    register fake skill instances via ``register_instance("cutebot", ...)``
    (or similar) without restoring the global registry afterwards. The
    next module that runs — for example ``tests/test_manifest_dispatch_contract.py``
    — then sees a leaked stub instance whose ``execute()`` has no
    dispatch dict and the contract validator reports "no backend
    dispatch table found", failing all ``cutebot-*`` parametrised
    cases (and only those — alone the test passes, which is exactly
    what made this an order-dependent flake).
    
    Snapshotting the dict reference and restoring its contents after
    every test makes the registry behave like any other shared
    fixture state. Tests that intentionally mutate the registry now
    do so within a per-test sandbox.
    """
    try:
        from skills.impl import SKILL_IMPLEMENTATIONS
    except Exception:
        # Importing ``skills.impl`` triggers auto-loading of every
        # backing implementation. If that fails (unrelated to this
        # fixture's concern), skip the snapshot — we're not in a test
        # that touches the registry.
        yield
        return
    snapshot = dict(SKILL_IMPLEMENTATIONS)
    try:
        yield
    finally:
        SKILL_IMPLEMENTATIONS.clear()
        SKILL_IMPLEMENTATIONS.update(snapshot)


@pytest.fixture(autouse=True)
def isolate_os_keychain(monkeypatch):
    """Replace the OS keychain with a per-test in-memory dict.

     made the OS keychain a hard dependency of the security path
    (BlindVault stores its master key there). Without this fixture the
    test suite would write `feral-ai/vault-master` into every developer's
    real macOS Keychain / Linux Secret Service / Windows Credential
    Manager during a normal `pytest` run — and then leave it behind.

    Each test gets its own dict so cross-test bleed is impossible. Tests
    that explicitly want to exercise the real keychain (none today) can
    monkeypatch the wrappers back in their own scope.
    """
    store: dict[tuple[str, str], str] = {}

    def fake_get(service, username):
        return store.get((service, username))

    def fake_set(service, username, password):
        store[(service, username)] = password

    def fake_delete(service, username):
        store.pop((service, username), None)

    try:
        from security import vault as _v
    except Exception:
        return
    monkeypatch.setattr(_v, "_keyring_get_password", fake_get)
    monkeypatch.setattr(_v, "_keyring_set_password", fake_set)
    monkeypatch.setattr(_v, "_keyring_delete_password", fake_delete)


@pytest.fixture(autouse=True)
def isolate_autonomy_env(monkeypatch):
    """Keep the developer's real autonomy tier out of unit tests.

    ``FERAL_AUTONOMY`` is the single source both ``agents/tool_runner``
    and ``security/exec_mode.current_autonomy_mode()`` read. Since
    ``ConfigLoader.export_as_env()`` started exporting it (so the
    wizard's autonomy choice stops being a dead write), a developer
    whose real ``~/.feral/settings.json`` says ``loose`` had that value
    pushed into ``os.environ`` the moment anything imported
    ``api.server``, which the autouse middleware fixture above does,
    before ``isolate_feral_home`` has had a chance to redirect the home
    directory. Tests asserting the shipped default then saw ``loose``.

    Defined LAST so it runs after that import and wins. Tests that care
    about a specific tier set it themselves (``patch.dict`` /
    ``monkeypatch.setenv`` inside the test body both run later).
    """
    monkeypatch.delenv("FERAL_AUTONOMY", raising=False)


@pytest.fixture(autouse=True)
def restore_process_env():
    """Snapshot and restore ``os.environ`` around every test.

    ``ConfigLoader.update_settings`` publishes changed settings straight
    into ``os.environ`` so a live toggle reaches env-only readers without
    a restart. That is correct at runtime and hostile to test isolation:
    monkeypatch can only undo writes it made itself, and
    ``monkeypatch.delenv(name, raising=False)`` on an already-absent
    variable registers no undo at all. So a test that deletes a variable
    and then calls ``update_settings`` leaves the published value behind
    for every test that follows.

    That is not hypothetical. It made 28 tests fail in the full suite
    while every one of them passed alone: a parametrised case in
    test_coding_settings_mirror leaked FERAL_POST_EDIT_DIAGNOSTICS as
    'enforce', and the post-edit-diagnostics suite then saw a disabled
    checker and got None back from every call.

    Snapshotting the whole environment rather than a named list because
    the funnel exports whatever the settings diff contains, so a list
    here would go stale the moment someone adds a key. Cooperates with
    monkeypatch: this restores after monkeypatch's own undo runs.
    """
    snapshot = dict(os.environ)
    try:
        yield
    finally:
        if os.environ != snapshot:
            leaked = sorted(
                set(os.environ) ^ set(snapshot)
                | {k for k in set(os.environ) & set(snapshot)
                   if os.environ[k] != snapshot[k]}
            )
            os.environ.clear()
            os.environ.update(snapshot)
            _record_env_leak(leaked)


# Variables a test is ALLOWED to leave changed, with the reason. Keep this
# empty if you can: every entry is a test that can reorder-break another.
_ENV_LEAK_ALLOWED = frozenset({
    # pytest sets this itself, once per test, by design.
    "PYTEST_CURRENT_TEST",
})

_ENV_LEAKS: dict[str, int] = {}


def _record_env_leak(names) -> None:
    for name in names:
        if name not in _ENV_LEAK_ALLOWED:
            _ENV_LEAKS[name] = _ENV_LEAKS.get(name, 0) + 1


def pytest_sessionfinish(session, exitstatus):
    """Report environment variables tests left changed. Warn, not fail.

    ``restore_process_env`` puts the environment back, so a leak can no
    longer break a later test. That also makes it invisible, which is how
    the class survived: settings writes reached ``os.environ`` directly,
    ``monkeypatch.delenv(..., raising=False)`` on an absent variable
    registered no undo, and 28 tests failed in the full suite while every
    one passed alone. This prints what the restore had to clean up so the
    next one cannot hide.

    **Why this warns instead of failing the build.** Measured across the
    full suite: 532 tests leave ``FERAL_HOME`` changed, 181
    ``OPENAI_API_KEY``, and ~70 each across the ``FERAL_*`` settings
    keys. That is not 532 sloppy tests. ``ConfigLoader.update_settings``
    publishes changed settings into ``os.environ`` on purpose, so a live
    toggle reaches env-only readers without a restart, and any test
    exercising it "leaks" by this measure while behaving correctly. A
    blocking gate here would fail the build for working production
    behaviour, and the honest fix (settings that do not write to the
    process environment) is a design change, not a test change.

    So: the restore is the real fix, and this is the visibility. Set
    ``FERAL_STRICT_ENV_LEAKS=1`` to make it blocking, which is worth
    doing on a targeted subset while cleaning one area up.
    """
    if _NETWORK_ATTEMPTS:
        detail = "\n".join(
            f"  {host}\n" + "\n".join(f"      {n}" for n in sorted(ids))
            for host, ids in sorted(_NETWORK_ATTEMPTS.items())
        )
        print(
            "\n[network guard] Tests attempted outbound calls to external "
            f"hosts:\n{detail}"
        )

    if not _ENV_LEAKS:
        return
    lines = "\n".join(
        f"  {name}  (left changed by {count} test(s))"
        for name, count in sorted(_ENV_LEAKS.items(), key=lambda kv: -kv[1])
    )
    message = (
        "Tests leaked environment variables into the process:\n"
        f"{lines}\n"
        "The environment was restored so nothing broke. Most of these come "
        "from ConfigLoader.update_settings publishing to os.environ by "
        "design. For a genuinely accidental one, use monkeypatch.setenv/"
        "delenv, and remember delenv on an ALREADY-ABSENT name registers "
        "no undo."
    )
    if os.environ.get("FERAL_STRICT_ENV_LEAKS"):
        session.exitstatus = 1
        print(f"\n[env-leak guard] STRICT: {message}")
        return
    warnings.warn(message, stacklevel=1)


def _reset_probe_status():
    """Integration probe cache behind Calendar/Email ``connected``.

    ``integrations/_probe_status`` keeps a process-local dict of the most
    recent probe result per provider. A test that marks "google"
    reachable made every later test believe Google was connected no
    matter what env it cleared, so ``TestCalendarIntegration`` and
    ``TestEmailIntegration``'s ``test_init_no_credentials`` both failed
    with ``assert True is False`` once a shuffled order put a
    cache-seeding test first. The module already exposed ``clear()`` for
    tests; nothing was calling it between them.
    """
    from integrations import _probe_status

    _probe_status.clear()


def _reset_transcript_order():
    """Voice transcript ordering singleton.

    ``TRANSCRIPT_ORDER`` is process-wide by design (see that module's
    docstring) and hands out a monotonic per-session ``seq`` starting at
    0. Tests reuse friendly session ids like "sess-web", so the second
    test to use one gets ``seq == 1``. Caught by ``pytest-randomly``:
    ``test_web_transcript_payload_carries_ordering_metadata`` failed with
    ``assert 1 == 0`` in one order and passed in two others.
    """
    from voice.transcript_order import TRANSCRIPT_ORDER

    TRANSCRIPT_ORDER._seq.clear()
    TRANSCRIPT_ORDER._prev.clear()
    TRANSCRIPT_ORDER._items.clear()


def _reset_security_probe_cache():
    """Provider reachability cache behind the "connected" badges."""
    from security.probe import clear_probe_cache

    clear_probe_cache()


def _reset_vault():
    """Process-wide credential vault singleton.

    THE root cause of the "no key configured" family. Keys resolve
    through the vault FIRST and the process env second, so a test that
    seeds the vault makes every later test believe a key exists no matter
    what env it clears. Under a shuffled order this failed
    ``test_realtime_proxy_available_reflects_api_key``,
    ``test_realtime_session_connect_skips_without_api_key``,
    ``test_available_false_without_keys_and_no_ollama`` and both
    ``test_..._no_openai_key...`` voice tests, all with a stray
    ``sk-test`` reaching a real handshake.

    ``reset_vault()`` was written for exactly this and nothing called it
    between tests. It was also invisible to a scan for mutable globals,
    because a lazily-initialised singleton starts life as ``None``.
    """
    from security.vault import reset_vault

    reset_vault()


def _reset_budget_cache():
    """Per-skill result-budget tier cache."""
    from skills.result_budget import reset_budget_cache

    reset_budget_cache()


def _reset_shared_catalog():
    """Shared provider/model catalog singleton."""
    from providers.catalog import reset_shared_catalog

    reset_shared_catalog()


def _reset_device_pairing_store():
    """Device pairing store singleton."""
    from security.device_pairing import reset_store

    reset_store()


def _reset_bound_host():
    """The recorded live-listener bind host.

    Set once per process by ``_spawn_brain_server``. A test that starts
    a server would otherwise leave it set, and every later test asking
    "does this need a restart?" would compare against a listener that is
    no longer running.
    """
    from config.runtime import _reset_bound_host as _reset

    _reset()


def _reset_glasses_buffer_registration():
    """The process-wide glasses buffer the vision read path resolves.

    ``BrainState.__init__`` builds a ``GlassesBuffer`` and registers it
    via ``set_glasses_buffer``, so the reader resolves the same object
    the ``glasses_frame`` write path fills. Correct for production,
    which builds exactly one ``BrainState``. But a test that constructs
    a throwaway one (``test_glasses_buffer``, ``test_primary_session_id``,
    ``test_e2e``) silently hands the registration to a buffer that is
    discarded when the test ends, while ``api.state.state`` keeps the
    real one. Every later reader then resolves the dead buffer.

    Re-point the registration at ``api.state.state``'s buffer rather
    than clearing it: cleared, the reader falls back to reading
    ``api.state`` anyway, so clearing would hide a genuine failure to
    register instead of restoring the canonical answer.

    Look both modules up in ``sys.modules`` instead of importing them.
    ``test_execution_audit_trail`` and ``test_external_agent_skill``
    ``monkeypatch.delitem(sys.modules, "api.state")`` on purpose. A plain
    ``from api.state import state`` here would re-import it, build a
    second ``BrainState``, and leave ``sys.modules["api.state"]`` and the
    ``api`` package attribute pointing at different modules for the rest
    of the session, which reads downstream as phone-bearer requests
    getting 401 from a session store nothing ever wrote to. Restoring one
    global is never worth importing a module a test just unloaded.
    """
    import sys

    api_state = sys.modules.get("api.state")
    glasses = sys.modules.get("perception.glasses_buffer")
    if api_state is None or glasses is None:
        return

    buf = getattr(getattr(api_state, "state", None), "glasses_buffer", None)
    if buf is not None:
        glasses.set_glasses_buffer(buf)


@pytest.fixture(autouse=True)
def _llm_provider_class_identity_is_stable():
    """Fail loudly if something rebuilt the ``LLMProvider`` class.

    Deliberately a fixture rather than an entry in
    ``_SHARED_STATE_RESETTERS``: that list is drained inside a
    ``try/except Exception: pass``, which is right for a resetter (a
    missing optional subsystem must not break the test) and fatal for an
    assertion, which would be swallowed exactly when it fired. This does
    not reset anything, so it does not belong there.

    ``importlib.reload`` re-executes a module in place: the module object
    in ``sys.modules`` survives, but every class it defines is a new
    object. Test modules import ``LLMProvider`` at collection time, before
    any test runs, so after a reload they hold the old class while
    production code that imports inside a function gets the new one, and
    ``monkeypatch.setattr(LLMProvider, ...)`` patches a class nothing will
    use. The stub is silently skipped and the real method runs.

    That cost four tests in ``test_agentic_cu_vlm_init`` and, because the
    real ``switch_provider`` then ran, a genuine call to OpenAI. It is
    invisible at the failure site, which is the whole problem: the test
    that breaks is nowhere near the reload that broke it. Checked rather
    than repaired, because rebinding the class afterwards would leave two
    live versions with instances of both already in flight.
    """
    import sys

    def _current():
        module = sys.modules.get("agents.llm_provider")
        return getattr(module, "LLMProvider", None) if module else None

    # Record the baseline on the way IN, not on the way out. Recorded at
    # teardown, the very first test to reload the module would install the
    # rebuilt class as the baseline and every later comparison would agree
    # with it, so the check would pass precisely when it should not.
    global _LLM_PROVIDER_CLASS
    if _LLM_PROVIDER_CLASS is None:
        _LLM_PROVIDER_CLASS = _current()

    yield

    current = _current()
    if current is None or _LLM_PROVIDER_CLASS is None:
        return

    if current is not _LLM_PROVIDER_CLASS:
        # Restore it, or every later test inherits the split too.
        sys.modules["agents.llm_provider"].LLMProvider = _LLM_PROVIDER_CLASS
        raise AssertionError(
            "agents.llm_provider.LLMProvider was rebuilt mid-session, almost "
            "certainly by importlib.reload(). Every test module that imported "
            "the class at collection time now patches a stale object, so their "
            "fakes are skipped and the real provider runs. Construct the class "
            "normally instead of reloading the module."
        )


_LLM_PROVIDER_CLASS = None


def _reset_rate_limit_store():
    """Per-client-IP request windows in the API rate limiter.

    Shared across tests, so one test's requests count against another's
    budget and a test asserting "not rate limited" fails depending on
    what ran before it.
    """
    from api.server import _rate_limit_store

    _rate_limit_store.clear()


# Every module-level mutable global a test can dirty for the next test.
#
# This list is not guesswork. A diagnostic plugin fingerprinted every
# candidate global after each of the 6606 tests and reported which ones
# actually changed across unrelated tests. Only these carried real
# cross-test state; the other candidates (provider registries, Prometheus
# collectors, FastAPI routers) are populated once at import and must NOT
# be cleared, which is why they are absent here.
#
# Add to this list rather than writing another autouse fixture, so there
# stays exactly one place that answers "what leaks between tests".
_SHARED_STATE_RESETTERS = (
    _reset_probe_status,
    _reset_transcript_order,
    _reset_security_probe_cache,
    _reset_rate_limit_store,
    _reset_vault,
    _reset_budget_cache,
    _reset_shared_catalog,
    _reset_device_pairing_store,
    _reset_bound_host,
    _reset_glasses_buffer_registration,
)

# Deliberately NOT reset here, so nobody adds them back "for symmetry":
#
# * ``observability/metrics`` collectors. Prometheus counters are meant
#   to be cumulative for the life of the process, and tests assert on
#   deltas rather than absolutes.
# * Provider/backend registries (``providers.base._REGISTRY``,
#   ``memory.backends.base._REGISTRY``, the STT/TTS
#   ``_PROVIDER_REGISTRY`` pairs). These are populated once at import by
#   decorators. Clearing them empties the registry for the rest of the
#   session, which breaks everything downstream.
#
# The distinction that matters: reset things filled at RUNTIME, never
# things filled at IMPORT.


_ALLOWED_HOSTS = frozenset({
    "localhost", "127.0.0.1", "::1", "0.0.0.0", "", "testserver",
})

# host -> {nodeid, ...} of tests that tried to reach it. Reported at the
# end of the session so the whole list comes out of one run.
_NETWORK_ATTEMPTS: dict[str, set] = {}


@pytest.fixture(autouse=True)
def block_outbound_network(monkeypatch, request):
    """Fail any test that opens a socket to a non-local host.

    ``memory/embeddings.py`` posts to ``https://api.openai.com/v1/embeddings``
    on the real code path. With a key reachable (the vault resolves before
    the env, and the vault used to leak between tests) the suite made
    genuine calls to OpenAI: real latency, real flakiness, and real money
    on the operator's account. Three shuffled runs each failed a
    different memory/KG test on ``httpcore.ReadTimeout``, which is what a
    live call looks like when the machine is loaded.

    Localhost is allowed, so a test that stands up its own server still
    works. FastAPI's TestClient never reaches this: it speaks ASGI
    in-process rather than opening a socket.

    Mark a test ``@pytest.mark.allow_network`` if it genuinely must dial
    out. There should be approximately none.
    """
    # `live` already means "talks to real external APIs" and is gated by
    # FERAL_LIVE_TESTS, so it implies the allowance without a second marker.
    if request.node.get_closest_marker("allow_network") or request.node.get_closest_marker("live"):
        yield
        return

    try:
        import httpx
    except Exception:  # pragma: no cover - httpx is a hard dep in practice
        yield
        return

    nodeid = request.node.nodeid

    def _is_mocked(client) -> bool:
        """True when the client can never reach the wire.

        ``httpx.MockTransport`` and ASGI/WSGI transports resolve requests
        in-process. ``send`` still runs for them, so without this check a
        properly stubbed test looks identical to one dialing out, and the
        report is worthless. This was not hypothetical: it initially
        reported six ``test_whoop_durable_sync`` tests as calling
        ``api.prod.whoop.com`` when every one of them was already wired to
        a ``MockTransport``.
        """
        seen = []
        transport = getattr(client, "_transport", None)
        if transport is not None:
            seen.append(transport)
        mounts = getattr(client, "_mounts", None) or {}
        seen.extend(t for t in mounts.values() if t is not None)
        if not seen:
            return False
        safe = ("MockTransport", "ASGITransport", "WSGITransport")
        return all(type(t).__name__ in safe for t in seen)

    def _check(client, request):
        host = (request.url.host or "").lower()
        if not host or host in _ALLOWED_HOSTS:
            return
        if _is_mocked(client):
            return
        message = (
            f"Test tried to reach {host!r} ({request.method} {request.url}). "
            "Tests must not call external services: it is slow, flaky, "
            "and on a provider endpoint it spends real money. Stub the "
            "client, or mark the test @pytest.mark.allow_network if it "
            "truly must dial out."
        )
        _NETWORK_ATTEMPTS.setdefault(host, set()).add(nodeid)
        if os.environ.get("FERAL_STRICT_TEST_NETWORK"):
            raise RuntimeError(message)
        # Default: RECORD ONLY, and let the call proceed.
        #
        # This is deliberately not a blanket block, and the reasoning
        # matters because the tempting version is worse. 112 tests dial
        # out today. Intercepting them all would silently change what
        # those tests exercise, and "the suite is green" would then mean
        # "green under a fake network" rather than green. That is a
        # workaround wearing a fix's clothes.
        #
        # So this reports, loudly, at the end of every run, and the
        # individual offenders get stubbed properly one at a time. The
        # list is in docs/handoff/WORKLOG.md. Set
        # FERAL_STRICT_TEST_NETWORK=1 to make it blocking, which is how
        # you keep an area clean once you have fixed it.
        return

    real_send = httpx.Client.send
    real_asend = httpx.AsyncClient.send

    def guarded_send(self, request, *a, **kw):
        _check(self, request)
        return real_send(self, request, *a, **kw)

    async def guarded_asend(self, request, *a, **kw):
        _check(self, request)
        return await real_asend(self, request, *a, **kw)

    # Deliberately at the httpx client layer rather than on raw sockets.
    # Patching ``socket.socket.connect`` for every test reaches every
    # thread in the process (aiosqlite workers, test servers) and killed
    # a full run with SIGKILL. This covers the actual egress path, since
    # every provider call in the codebase goes out through httpx.
    monkeypatch.setattr(httpx.Client, "send", guarded_send)
    monkeypatch.setattr(httpx.AsyncClient, "send", guarded_asend)
    yield


@pytest.fixture(autouse=True)
def reset_shared_process_state():
    """Reset shared process globals before AND after every test.

    Before as well as after, so a test neither inherits a dirty global
    nor exports one. Each resetter is isolated: an optional subsystem
    that is not importable in a given test context is skipped rather
    than failing every test in the suite.
    """

    def _wipe():
        for fn in _SHARED_STATE_RESETTERS:
            try:
                fn()
            except Exception:
                # Optional subsystem (voice extras, api server) absent in
                # this context. Never let cleanup break the test itself.
                pass

    _wipe()
    try:
        yield
    finally:
        _wipe()
