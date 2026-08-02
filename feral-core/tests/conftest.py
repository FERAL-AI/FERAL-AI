import pytest
import os


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
            os.environ.clear()
            os.environ.update(snapshot)


@pytest.fixture(autouse=True)
def reset_probe_cache():
    """Clear the integration probe cache around every test.

    ``integrations/_probe_status`` keeps a process-local dict of the most
    recent probe result per provider, and ``connected`` on the Calendar
    and Email integrations reads it through ``is_connected_cached``. A
    test that marks "google" reachable therefore makes every later test
    believe Google is connected, no matter what env it clears.

    Found through ``pytest-randomly``: ``TestCalendarIntegration`` and
    ``TestEmailIntegration``'s ``test_init_no_credentials`` assert
    ``connected is False`` after deleting their env var, and both failed
    with ``assert True is False`` once a shuffled order put a
    cache-seeding test first. They pass alone and in file order, which is
    exactly why this went unnoticed.

    The module already exposed ``clear()`` for tests; nothing was calling
    it between them. Cleared before AND after, so a test that seeds the
    cache neither inherits nor exports a dirty one.
    """
    try:
        from integrations import _probe_status
    except Exception:  # integrations optional in some test contexts
        yield
        return
    _probe_status.clear()
    try:
        yield
    finally:
        _probe_status.clear()


@pytest.fixture(autouse=True)
def reset_transcript_order():
    """Reset the process-wide voice transcript ordering singleton.

    ``voice.transcript_order.TRANSCRIPT_ORDER`` is shared by design (see
    that module's docstring) and hands out a monotonic per-session
    ``seq`` starting at 0. Tests reuse friendly session ids like
    "sess-web", so the second test to use one gets ``seq == 1`` and an
    assertion of ``seq == 0`` fails.

    Caught by ``pytest-randomly``:
    ``test_web_transcript_payload_carries_ordering_metadata`` failed with
    ``assert 1 == 0`` in one shuffled order and passed in two others.
    """
    try:
        from voice.transcript_order import TRANSCRIPT_ORDER
    except Exception:  # voice extras optional
        yield
        return

    def _wipe():
        TRANSCRIPT_ORDER._seq.clear()
        TRANSCRIPT_ORDER._prev.clear()
        TRANSCRIPT_ORDER._items.clear()

    _wipe()
    try:
        yield
    finally:
        _wipe()
