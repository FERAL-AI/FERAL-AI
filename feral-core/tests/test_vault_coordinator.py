"""Deferred unlock lifecycle tests. Never read or modify a real keychain."""
import asyncio
import base64
import json
import threading
from unittest.mock import Mock

import pytest

from security.vault_coordinator import VaultCoordinator, VaultLockedRefusal, readonly_vault_factory


def test_construct_and_passive_status_do_not_invoke_any_dependencies():
    factory = Mock(side_effect=AssertionError("must not unlock"))
    hydrate = Mock(side_effect=AssertionError("must not hydrate"))
    disable = Mock()
    coordinator = VaultCoordinator(vault_factory=factory, credential_hydrator=hydrate, disable_dependents=disable)
    for _ in range(5):
        assert coordinator.status()["state"] == "locked"
        assert coordinator.status()["credentials_available"] is False
    with pytest.raises(VaultLockedRefusal) as refusal:
        coordinator.require_ready()
    assert refusal.value.code == "vault_locked"
    factory.assert_not_called(); hydrate.assert_not_called(); disable.assert_not_called()


@pytest.mark.asyncio
async def test_timeout_and_repeated_requests_keep_one_original_worker():
    release = threading.Event()
    opened = object()
    def work():
        release.wait(2)
        return opened
    factory = Mock(side_effect=work)
    hydrate = Mock()
    coordinator = VaultCoordinator(vault_factory=factory, credential_hydrator=hydrate)
    try:
        results = await asyncio.gather(*(coordinator.unlock(timeout=.01) for _ in range(8)))
        assert all(r["in_flight"] and r["state"] == "unlocking" for r in results)
        factory.assert_called_once(); hydrate.assert_not_called()
        with pytest.raises(VaultLockedRefusal): coordinator.require_ready()
    finally:
        release.set()
        await coordinator.unlock(timeout=1)
    assert coordinator.require_ready() is opened
    hydrate.assert_called_once_with(opened)
    factory.assert_called_once()


@pytest.mark.asyncio
async def test_caller_cancellation_does_not_start_second_read():
    release = threading.Event()
    factory = Mock(side_effect=lambda: (release.wait(2), object())[1])
    coordinator = VaultCoordinator(vault_factory=factory)
    attempt = asyncio.create_task(coordinator.unlock(timeout=1))
    await asyncio.sleep(.01)
    attempt.cancel()
    with pytest.raises(asyncio.CancelledError): await attempt
    try:
        assert (await coordinator.unlock(timeout=.01))["in_flight"]
        factory.assert_called_once()
    finally:
        release.set()
        await coordinator.unlock(timeout=1)


@pytest.mark.asyncio
async def test_lock_during_worker_prevents_late_hydration():
    release = threading.Event()
    factory = Mock(side_effect=lambda: (release.wait(2), object())[1])
    hydrate = Mock(); disable = Mock()
    coordinator = VaultCoordinator(vault_factory=factory, credential_hydrator=hydrate, disable_dependents=disable)
    await coordinator.unlock(timeout=.01)
    await coordinator.lock()
    assert coordinator.status()["state"] == "locked" and coordinator.status()["in_flight"]
    release.set()
    await coordinator._operation
    hydrate.assert_not_called(); disable.assert_called_once()
    with pytest.raises(VaultLockedRefusal): coordinator.require_ready()


@pytest.mark.asyncio
async def test_hydration_failure_remains_closed_and_cleans_dependents():
    disable = Mock()
    coordinator = VaultCoordinator(vault_factory=lambda: object(), credential_hydrator=lambda vault: False, disable_dependents=disable)
    status = await coordinator.unlock(timeout=1)
    assert status["state"] == "unavailable" and status["code"] == "hydration_failed"
    disable.assert_called_once()
    with pytest.raises(VaultLockedRefusal): coordinator.require_ready()


@pytest.mark.asyncio
async def test_unknown_failure_is_private_and_retry_is_explicit():
    factory = Mock(side_effect=RuntimeError("private-fixture-path-and-secret"))
    disable = Mock()
    coordinator = VaultCoordinator(vault_factory=factory, disable_dependents=disable)
    status = await coordinator.unlock(timeout=1)
    assert status["code"] == "unlock_failed"
    assert "private-fixture" not in json.dumps(status)
    for _ in range(5): coordinator.status()
    factory.assert_called_once(); disable.assert_called_once()
    factory.side_effect = None; factory.return_value = object()
    assert (await coordinator.unlock(timeout=1))["state"] == "ready"
    assert factory.call_count == 2


@pytest.fixture
def ciphertext_fixture(tmp_path, monkeypatch):
    tmp_path = tmp_path / "vault-storage"
    tmp_path.mkdir()
    import keyring
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    from security import vault
    key = bytes(range(32)); nonce = bytes(range(12))
    data = {"version": vault._VAULT_VERSION, "data": {"credentials": {"OPENAI_API_KEY": "fixture-secret"}}}
    raw = nonce + ChaCha20Poly1305(key).encrypt(nonce, json.dumps(data).encode(), vault._AEAD_AAD)
    path = tmp_path / "credentials.json"
    path.with_suffix(".enc").write_bytes(raw)
    # Existing legacy and rotation files must remain byte-identical.
    path.write_text('{"legacy":"retain"}')
    path.with_suffix(".enc").with_name("credentials.enc.prev").write_bytes(b"retain previous")
    get = Mock(return_value=base64.b64encode(key).decode())
    monkeypatch.setattr(keyring, "get_password", get)
    monkeypatch.setattr(keyring, "set_password", Mock(side_effect=AssertionError("no keychain writes")))
    monkeypatch.setattr(keyring, "delete_password", Mock(side_effect=AssertionError("no keychain deletes")))
    monkeypatch.setenv(vault.RECOVERY_ENV, "invalid-recovery-must-not-be-consulted")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    return path, get, before


@pytest.mark.asyncio
async def test_real_ciphertext_authentication_and_artifacts_preserved(ciphertext_fixture):
    path, get, before = ciphertext_fixture
    hydrate = Mock()
    coordinator = VaultCoordinator(vault_factory=readonly_vault_factory(path), credential_hydrator=hydrate)
    assert (await coordinator.unlock(timeout=1))["state"] == "ready"
    assert coordinator.require_ready().get_credential("OPENAI_API_KEY") == "fixture-secret"
    assert {p.name: p.read_bytes() for p in path.parent.iterdir()} == before
    get.assert_called_once()
    hydrate.assert_called_once()
    assert "fixture-secret" not in json.dumps(coordinator.status())


@pytest.mark.asyncio
async def test_wrong_key_never_hydrates_or_changes_ciphertext(ciphertext_fixture):
    path, get, before = ciphertext_fixture
    get.return_value = base64.b64encode(bytes(reversed(range(32)))).decode()
    hydrate = Mock(); disable = Mock()
    coordinator = VaultCoordinator(vault_factory=readonly_vault_factory(path), credential_hydrator=hydrate, disable_dependents=disable)
    assert (await coordinator.unlock(timeout=1))["code"] == "authentication_failed"
    hydrate.assert_not_called(); disable.assert_called_once()
    assert {p.name: p.read_bytes() for p in path.parent.iterdir()} == before


@pytest.mark.asyncio
@pytest.mark.parametrize("stored,code", [(None, "key_unavailable"), ("not base64!", "key_invalid"), (base64.b64encode(b"short").decode(), "key_invalid")])
async def test_missing_invalid_keys_fail_closed(ciphertext_fixture, stored, code):
    path, get, before = ciphertext_fixture
    get.return_value = stored
    coordinator = VaultCoordinator(vault_factory=readonly_vault_factory(path))
    assert (await coordinator.unlock(timeout=1))["code"] == code
    assert {p.name: p.read_bytes() for p in path.parent.iterdir()} == before


@pytest.mark.asyncio
async def test_fresh_or_legacy_only_vault_does_not_bootstrap(tmp_path, monkeypatch):
    tmp_path = tmp_path / "vault-storage"
    tmp_path.mkdir()
    import keyring
    get = Mock(side_effect=AssertionError("no keychain access without ciphertext"))
    monkeypatch.setattr(keyring, "get_password", get)
    path = tmp_path / "credentials.json"
    for legacy in (False, True):
        if legacy: path.write_text('{"legacy":"untouched"}')
        coordinator = VaultCoordinator(vault_factory=readonly_vault_factory(path))
        assert (await coordinator.unlock(timeout=1))["code"] == "vault_uninitialized"
        assert not path.with_suffix(".enc").exists()
    assert path.read_text() == '{"legacy":"untouched"}'
    get.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [True, 0, -1, float("inf"), float("nan"), 121])
async def test_invalid_wait_bounds_never_start_unlock(timeout):
    factory = Mock()
    coordinator = VaultCoordinator(vault_factory=factory)
    with pytest.raises(ValueError): await coordinator.unlock(timeout=timeout)
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_lock_during_async_hydration_clears_late_dependents():
    entered = asyncio.Event(); release = asyncio.Event()
    dependents = []
    async def hydrate(vault):
        entered.set()
        await release.wait()
        dependents.append("late credential")
    def disable(): dependents.clear()
    coordinator = VaultCoordinator(vault_factory=lambda: object(), credential_hydrator=hydrate, disable_dependents=disable)
    attempt = asyncio.create_task(coordinator.unlock(timeout=1))
    await entered.wait()
    await coordinator.lock()
    release.set()
    await attempt
    assert dependents == []
    assert coordinator.status()["state"] == "locked"
    with pytest.raises(VaultLockedRefusal): coordinator.require_ready()


def test_event_loop_shutdown_does_not_cancel_late_worker_result():
    release = threading.Event(); completed = threading.Event()
    errors = []
    async def scenario():
        # Exercise the actual worker while deliberately cancelling its task
        # as asyncio.run does on shutdown. The OS worker must still settle
        # its Future without InvalidStateError on a daemon thread.
        def factory():
            release.wait(2)
            completed.set()
            return object()
        coordinator = VaultCoordinator(vault_factory=factory)
        await coordinator.unlock(timeout=.01)
        coordinator._operation.cancel()
        with pytest.raises(asyncio.CancelledError): await coordinator._operation
    import threading as module
    original = module.excepthook
    module.excepthook = lambda args: errors.append(args.exc_value)
    try:
        asyncio.run(scenario())
        release.set()
        assert completed.wait(1)
        # Completion is signalled immediately before factory returns; join
        # the identified bounded worker to observe Future settlement.
        for worker in module.enumerate():
            if worker.name == "vault-explicit-unlock": worker.join(1)
        assert errors == []
    finally:
        release.set()
        module.excepthook = original


@pytest.mark.asyncio
async def test_reviewed_fresh_initialization_encrypts_and_verifies_new_vault(tmp_path):
    tmp_path = tmp_path / "vault-storage"
    tmp_path.mkdir()
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    from security.vault import _AEAD_AAD
    from security.vault_coordinator import initialization_factory, VaultInitializationRefusal
    path = tmp_path / "credentials.json"
    keys = []
    def add(encoded): keys.append(base64.b64decode(encoded, validate=True))
    hydrate = Mock()
    coordinator = VaultCoordinator(fresh_initializer=initialization_factory(path, add_master_key=add), credential_hydrator=hydrate)
    review = coordinator.review_initialization()
    assert list(tmp_path.iterdir()) == [] and keys == []
    assert "partial" in review.scope and "not be replaced" in review.scope
    assert (await coordinator.initialize(review, timeout=1))["state"] == "ready"
    raw = path.with_suffix(".enc").read_bytes()
    data = json.loads(ChaCha20Poly1305(keys[0]).decrypt(raw[:12], raw[12:], _AEAD_AAD))
    assert data["data"] == {"credentials": {}}
    assert len(keys) == 1 and len(keys[0]) == 32
    assert path.with_suffix(".enc").stat().st_mode & 0o777 == 0o600
    assert coordinator.require_ready().list_keys() == []
    hydrate.assert_called_once()
    with pytest.raises(VaultInitializationRefusal): await coordinator.initialize(review)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["credentials.json", "credentials.enc", "credentials.enc.prev", "credentials.enc.new", "credentials.json.bak.legacy"])
async def test_initializer_preserves_any_existing_artifact(tmp_path, name):
    tmp_path = tmp_path / "vault-storage"
    tmp_path.mkdir()
    from security.vault_coordinator import initialization_factory
    artifact = tmp_path / name; artifact.write_bytes(b"existing private fixture")
    add = Mock(side_effect=AssertionError("existing artifacts cannot add key"))
    coordinator = VaultCoordinator(fresh_initializer=initialization_factory(tmp_path / "credentials.json", add_master_key=add))
    status = await coordinator.initialize(coordinator.review_initialization(), timeout=1)
    assert status["code"] == "vault_artifacts_present"
    assert artifact.read_bytes() == b"existing private fixture"
    assert list(tmp_path.iterdir()) == [artifact]
    add.assert_not_called()


@pytest.mark.asyncio
async def test_key_storage_failure_never_creates_ciphertext(tmp_path):
    tmp_path = tmp_path / "vault-storage"
    tmp_path.mkdir()
    from security.vault_coordinator import initialization_factory
    add = Mock(side_effect=RuntimeError("private keychain error"))
    coordinator = VaultCoordinator(fresh_initializer=initialization_factory(tmp_path / "credentials.json", add_master_key=add))
    status = await coordinator.initialize(coordinator.review_initialization(), timeout=1)
    assert status["code"] == "unlock_failed"
    assert "private keychain error" not in json.dumps(status)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_storage_race_after_key_add_is_partial_without_reset(tmp_path):
    tmp_path = tmp_path / "vault-storage"
    tmp_path.mkdir()
    from security.vault_coordinator import initialization_factory
    path = tmp_path / "credentials.json"
    keys = []
    def add(encoded):
        keys.append(encoded)
        # Simulate a concurrent writer arriving after key persistence.
        path.with_suffix(".enc").write_bytes(b"other process ciphertext")
    coordinator = VaultCoordinator(fresh_initializer=initialization_factory(path, add_master_key=add))
    status = await coordinator.initialize(coordinator.review_initialization(), timeout=1)
    assert status["code"] == "initialization_partial" and status["credentials_available"] is False
    assert len(keys) == 1
    assert path.with_suffix(".enc").read_bytes() == b"other process ciphertext"
    with pytest.raises(VaultLockedRefusal): coordinator.require_ready()


@pytest.mark.asyncio
async def test_symlink_parent_and_artifact_refused_without_key_storage(tmp_path):
    tmp_path = tmp_path / "vault-storage"
    tmp_path.mkdir()
    from security.vault_coordinator import initialization_factory
    real = tmp_path / "real"; real.mkdir()
    link = tmp_path / "link"; link.symlink_to(real, target_is_directory=True)
    add = Mock()
    coordinator = VaultCoordinator(fresh_initializer=initialization_factory(link / "credentials.json", add_master_key=add))
    assert (await coordinator.initialize(coordinator.review_initialization(), timeout=1))["state"] == "unavailable"
    (real / "credentials.enc").symlink_to(tmp_path / "missing")
    coordinator = VaultCoordinator(fresh_initializer=initialization_factory(real / "credentials.json", add_master_key=add))
    assert (await coordinator.initialize(coordinator.review_initialization(), timeout=1))["code"] == "vault_artifacts_present"
    add.assert_not_called()


@pytest.mark.asyncio
async def test_canceled_review_and_new_unlock_invalidate_initialization():
    from security.vault_coordinator import VaultInitializationRefusal
    initialize = Mock()
    coordinator = VaultCoordinator(vault_factory=lambda: object(), fresh_initializer=initialize)
    review = coordinator.review_initialization()
    coordinator.cancel_initialization(review)
    with pytest.raises(VaultInitializationRefusal): await coordinator.initialize(review)
    review = coordinator.review_initialization()
    await coordinator.unlock(timeout=1)
    with pytest.raises(VaultInitializationRefusal): await coordinator.initialize(review)
    initialize.assert_not_called()


@pytest.mark.asyncio
async def test_initialization_timeout_does_not_repeat_key_add(tmp_path):
    tmp_path = tmp_path / "vault-storage"
    tmp_path.mkdir()
    from security.vault_coordinator import initialization_factory, VaultInitializationRefusal
    release = threading.Event()
    add = Mock(side_effect=lambda encoded: release.wait(2))
    coordinator = VaultCoordinator(fresh_initializer=initialization_factory(tmp_path / "credentials.json", add_master_key=add))
    review = coordinator.review_initialization()
    try:
        assert (await coordinator.initialize(review, timeout=.01))["in_flight"]
        with pytest.raises(VaultInitializationRefusal): coordinator.review_initialization()
        assert (await coordinator.unlock(timeout=.01))["in_flight"]
        add.assert_called_once()
    finally:
        release.set()
        await coordinator._operation
    assert coordinator.status()["state"] == "ready"
    add.assert_called_once()


def test_macos_missing_default_guard_refuses_before_security_add(monkeypatch):
    from security import vault
    from security.vault_coordinator import _macos_add_master_key_no_replace, _Unavailable
    import ctypes
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(vault, "_macos_default_keychain_state", lambda: (-25307, False))
    load = Mock(side_effect=AssertionError("missing keychain must not reach Add"))
    monkeypatch.setattr(ctypes, "CDLL", load)
    with pytest.raises(_Unavailable) as refusal: _macos_add_master_key_no_replace("fixture")
    assert refusal.value.code == "key_unavailable"
    load.assert_not_called()


def test_initializer_linux_has_no_weaker_key_storage_fallback(monkeypatch):
    from security.vault_coordinator import _macos_add_master_key_no_replace, _Unavailable
    monkeypatch.setattr("sys.platform", "linux")
    with pytest.raises(_Unavailable) as refusal: _macos_add_master_key_no_replace("fixture")
    assert refusal.value.code == "initialization_unsupported"
