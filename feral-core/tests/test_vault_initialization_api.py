"""9.20 reviewed initialization tests with fake OS and real AEAD/disk."""

import asyncio
import base64
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI

from api.routes.vault_initialization import create_vault_initialization_router
from security.vault_coordinator import (
    VaultCoordinator,
    initialization_factory,
    _Unavailable,
)
from security.vault_initialization_api import (
    VaultInitializationAPI,
    VaultInitializationAPIRefusal,
    BoundVaultInitializer,
)


class AtomicFakeKeychain:
    """Models add-if-absent; never updates or deletes an existing entry."""

    def __init__(self, existing=None):
        self.key = existing
        self.adds = 0
        self.deletes = 0

    def add(self, value):
        self.adds += 1
        if self.key is not None:
            raise _Unavailable("key_already_present")
        self.key = value


@pytest.fixture
def setup(tmp_path):
    storage = tmp_path / "vault-storage"
    storage.mkdir()
    path = storage / "credentials.json"
    adapter = AtomicFakeKeychain()
    coordinator = VaultCoordinator(
        fresh_initializer=BoundVaultInitializer(path, add_master_key=adapter.add)
    )
    controller = VaultInitializationAPI(
        coordinator, path, platform="darwin", signed_release_accepted=True
    )
    return controller, coordinator, adapter, path


def test_default_release_gate_is_passive_and_local_use_never_requires_vault(setup):
    controller, coordinator, adapter, path = setup
    gated = VaultInitializationAPI(coordinator, path, platform="darwin")
    for _ in range(3):
        status = gated.status()
        assert not status["supported"] and status["requires_signed_acceptance"]
        assert status["local_use_requires_vault"] is False
    with pytest.raises(VaultInitializationAPIRefusal):
        gated.review()
    assert adapter.adds == 0 and list(path.parent.iterdir()) == []


def test_linux_has_no_automatic_weaker_fallback(setup):
    controller, coordinator, adapter, path = setup
    unsupported = VaultInitializationAPI(
        coordinator, path, platform="linux", signed_release_accepted=True
    )
    assert unsupported.status()["code"] == "platform_unsupported"
    with pytest.raises(VaultInitializationAPIRefusal):
        unsupported.review()
    assert adapter.adds == 0


@pytest.mark.asyncio
async def test_actual_router_review_initialize_encrypt_readback_one_use(setup):
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    from security.vault import _AEAD_AAD

    controller, coordinator, adapter, path = setup
    app = FastAPI()
    app.include_router(create_vault_initialization_router(lambda: controller))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        status = (await client.get("/api/security/vault/initialize/status")).json()
        assert status["supported"] and adapter.adds == 0
        review = (
            await client.post("/api/security/vault/initialize/review", json={})
        ).json()
        assert (
            review["expires_in_seconds"] == 300 and review["previous_status"] == status
        )
        assert adapter.adds == 0
        response = await client.post(
            "/api/security/vault/initialize",
            json={"review_token": review["review_token"]},
        )
        assert response.status_code == 200
        receipt = response.json()
        assert (
            receipt["operation"] == "initialize"
            and receipt["review_token"] == review["review_token"]
        )
        assert receipt["ok"] is receipt["vault_status"]["credentials_available"] is True
        assert receipt["status"]["credential_storage_available"] is True
        assert (
            await client.post(
                "/api/security/vault/initialize",
                json={"review_token": review["review_token"]},
            )
        ).status_code == 409
    blob = path.with_suffix(".enc").read_bytes()
    key = base64.b64decode(adapter.key, validate=True)
    payload = json.loads(ChaCha20Poly1305(key).decrypt(blob[:12], blob[12:], _AEAD_AAD))
    assert payload["data"] == {"credentials": {}}
    assert adapter.adds == 1 and adapter.deletes == 0
    assert not path.exists()
    assert path.with_suffix(".enc").stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_existing_shared_key_is_never_overwritten(setup):
    controller, coordinator, adapter, path = setup
    adapter.key = "existing-global-master-key-sentinel"
    receipt = await controller.confirm(controller.review()["review_token"])
    assert (
        receipt["ok"] is False
        and receipt["vault_status"]["code"] == "key_already_present"
    )
    assert adapter.key == "existing-global-master-key-sentinel" and adapter.deletes == 0
    assert list(path.parent.iterdir()) == []
    assert not controller.status()["supported"]


@pytest.mark.parametrize(
    "name",
    [
        "credentials.json",
        "credentials.enc",
        "credentials.enc.prev",
        "credentials.enc.new",
        "credentials.json.bak.legacy",
    ],
)
def test_existing_artifacts_refuse_review_without_os_operations(setup, name):
    controller, coordinator, adapter, path = setup
    existing = path.parent / name
    existing.write_bytes(b"private existing artifact")
    with pytest.raises(VaultInitializationAPIRefusal):
        controller.review()
    assert existing.read_bytes() == b"private existing artifact"
    assert adapter.adds == 0 and adapter.deletes == 0


@pytest.mark.asyncio
async def test_artifact_arriving_after_review_blocks_dispatch(setup):
    controller, coordinator, adapter, path = setup
    review = controller.review()
    path.with_suffix(".enc").write_bytes(b"newly arrived artifact")
    with pytest.raises(VaultInitializationAPIRefusal) as error:
        await controller.confirm(review["review_token"])
    assert error.value.code == "storage_changed"
    assert adapter.adds == 0
    assert path.with_suffix(".enc").read_bytes() == b"newly arrived artifact"


@pytest.mark.asyncio
async def test_replaced_parent_directory_invalidates_review(setup):
    controller, coordinator, adapter, path = setup
    review = controller.review()
    old = path.parent.with_name("original-vault-storage")
    path.parent.rename(old)
    path.parent.mkdir()
    with pytest.raises(VaultInitializationAPIRefusal) as error:
        await controller.confirm(review["review_token"])
    assert error.value.code == "storage_changed" and adapter.adds == 0


@pytest.mark.asyncio
async def test_cancel_expiry_and_unknown_tokens_never_dispatch(setup, monkeypatch):
    controller, coordinator, adapter, path = setup
    review = controller.review()
    controller.cancel(review["review_token"])
    with pytest.raises(VaultInitializationAPIRefusal):
        await controller.confirm(review["review_token"])
    review = controller.review()
    import security.vault_initialization_api as module

    original = module.time.monotonic
    now = original()
    monkeypatch.setattr(module.time, "monotonic", lambda: now + 301)
    with pytest.raises(VaultInitializationAPIRefusal):
        await controller.confirm(review["review_token"])
    assert adapter.adds == 0


@pytest.mark.asyncio
async def test_partial_key_success_disk_failure_is_reported_no_compensation(setup):
    controller, coordinator, adapter, path = setup

    def persist_then_race(value):
        adapter.add(value)
        path.with_suffix(".enc").write_bytes(b"concurrent disk artifact")

    coordinator._initializer = BoundVaultInitializer(
        path, add_master_key=persist_then_race
    )
    receipt = await controller.confirm(controller.review()["review_token"])
    assert not receipt["ok"]
    assert (
        receipt["vault_status"]["code"]
        == receipt["status"]["code"]
        == "initialization_partial"
    )
    assert adapter.key is not None and adapter.adds == 1 and adapter.deletes == 0
    assert path.with_suffix(".enc").read_bytes() == b"concurrent disk artifact"
    with pytest.raises(VaultInitializationAPIRefusal):
        controller.review()


@pytest.mark.asyncio
async def test_pending_initialization_blocks_second_init_or_unlock_worker(setup):
    controller, coordinator, adapter, path = setup
    release = threading.Event()

    def blocked_add(value):
        release.wait(2)
        adapter.add(value)

    coordinator._initializer = BoundVaultInitializer(path, add_master_key=blocked_add)
    try:
        receipt = await controller.confirm(
            controller.review()["review_token"], timeout=0.01
        )
        assert not receipt["ok"] and receipt["vault_status"]["in_flight"]
        assert controller.status()["code"] == "operation_in_progress"
        with pytest.raises(VaultInitializationAPIRefusal):
            controller.review()
        assert (await coordinator.unlock(timeout=0.01))["in_flight"]
    finally:
        release.set()
        await coordinator._operation
    assert adapter.adds == 1 and coordinator.status()["credentials_available"]


def test_symlink_parent_refused_before_os_access(setup):
    controller, coordinator, adapter, path = setup
    link = path.parent.with_name("symlink-storage")
    link.symlink_to(path.parent, target_is_directory=True)
    coordinator._initializer = BoundVaultInitializer(
        link / "credentials.json", add_master_key=adapter.add
    )
    unsafe = VaultInitializationAPI(
        coordinator,
        link / "credentials.json",
        platform="darwin",
        signed_release_accepted=True,
    )
    assert unsafe.status()["code"] == "storage_unavailable"
    with pytest.raises(VaultInitializationAPIRefusal):
        unsafe.review()
    assert adapter.adds == 0


def test_actual_macos_add_uses_existing_reference_and_duplicate_fails_without_update(
    tmp_path, monkeypatch
):
    """Exercise real ctypes bridge shape through a fake Security framework."""
    import ctypes
    from security import vault
    from security.vault_coordinator import _macos_add_master_key_no_replace

    keychain = tmp_path / "existing.keychain-db"
    keychain.write_bytes(b"fixture only")
    calls = []

    class Function:
        def __init__(self, function):
            self.function = function

        def __call__(self, *args):
            return self.function(*args)

    def copy_default(pointer):
        pointer._obj.value = 73
        return 0

    def get_path(reference, length, buffer):
        buffer.value = str(keychain).encode()
        return 0

    def add(*args):
        calls.append(args)
        return -25299

    security = SimpleNamespace(
        SecKeychainCopyDefault=Function(copy_default),
        SecKeychainGetPath=Function(get_path),
        SecKeychainAddGenericPassword=Function(add),
    )
    core = SimpleNamespace(CFRelease=Function(lambda reference: None))
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(vault, "_macos_default_keychain_state", lambda: (0, True))
    monkeypatch.setattr(
        ctypes, "CDLL", lambda path: security if "Security.framework" in path else core
    )
    with pytest.raises(_Unavailable) as error:
        _macos_add_master_key_no_replace("fixture-encoded-key")
    assert error.value.code == "key_already_present"
    assert len(calls) == 1 and calls[0][0].value == 73
    # Only Add is exposed; no update/delete/reset/create API is callable.
    assert calls[0][-1] is None


def test_mismatched_initializer_target_is_never_advertised_or_reviewed(setup):
    controller, coordinator, adapter, path = setup
    coordinator._initializer = BoundVaultInitializer(
        path.parent / "other.json", add_master_key=adapter.add
    )
    assert controller.status()["code"] == "initializer_not_configured"
    with pytest.raises(VaultInitializationAPIRefusal):
        controller.review()
    assert adapter.adds == 0


@pytest.mark.asyncio
async def test_changed_initializer_identity_after_review_blocks_dispatch(setup):
    controller, coordinator, adapter, path = setup
    review = controller.review()
    coordinator._initializer = BoundVaultInitializer(path, add_master_key=adapter.add)
    with pytest.raises(VaultInitializationAPIRefusal) as error:
        await controller.confirm(review["review_token"])
    assert error.value.code == "storage_changed" and adapter.adds == 0


def test_outstanding_reviews_are_bounded_and_cancellation_releases_quota(setup):
    controller, coordinator, adapter, path = setup
    tokens = [controller.review()["review_token"] for _ in range(16)]
    with pytest.raises(VaultInitializationAPIRefusal) as error:
        controller.review()
    assert error.value.code == "too_many_reviews"
    controller.cancel(tokens[0])
    assert controller.review()["review_token"]
    assert adapter.adds == 0
