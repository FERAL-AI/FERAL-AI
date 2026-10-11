"""Signed release receipt verification without actual signing or OS key access."""
from pathlib import Path
import plistlib
from unittest.mock import Mock

import pytest

from security.vault_release_acceptance import inspect_vault_release_acceptance


@pytest.fixture
def bundle(tmp_path):
    app = tmp_path / "FERAL.app"
    interpreter = app / "Contents/Resources/python/bin/python3"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"fixture")
    info = {
        "CFBundleIdentifier": "ai.feral.native",
        "CFBundleVersion": "fixture-1",
        "FERALVaultInitializationAcceptance": {
            "accepted": True, "scope": "macos-keychain-add-if-absent",
            "contract_version": "1", "bundle_id": "ai.feral.native",
            "build": "fixture-1", "team_id": "ABCDEFGHIJ",
            "acceptance_id": "isolated-contract-fixture",
        },
    }
    path = app / "Contents/Info.plist"
    path.write_bytes(plistlib.dumps(info))
    return interpreter, path, info


def test_matching_version_contract_and_verified_team_enable_gate(bundle):
    executable, _, _ = bundle
    verifier = Mock(return_value="ABCDEFGHIJ")
    result = inspect_vault_release_acceptance(interpreter=executable, verify_team=verifier)
    assert result.accepted and result.code == "signed_release_accepted"
    verifier.assert_called_once_with(executable.parents[4])


@pytest.mark.parametrize("field,value", [
    ("accepted", False), ("accepted", 1), ("build", "old"),
    ("bundle_id", "other"), ("team_id", "invalid"),
    ("scope", "keyring-update"), ("contract_version", "2"),
    ("contract_version", 1), ("acceptance_id", "../private"),
])
def test_unbound_or_invalid_receipt_never_invokes_signature_verifier(bundle, field, value):
    executable, path, info = bundle
    info["FERALVaultInitializationAcceptance"][field] = value
    path.write_bytes(plistlib.dumps(info))
    verifier = Mock(side_effect=AssertionError("not eligible"))
    assert not inspect_vault_release_acceptance(interpreter=executable, verify_team=verifier).accepted
    verifier.assert_not_called()


@pytest.mark.parametrize("team", [None, "OTHERTEAM1X"])
def test_adhoc_unsigned_tampered_or_other_publisher_fails_closed(bundle, team):
    executable, _, _ = bundle
    assert not inspect_vault_release_acceptance(interpreter=executable, verify_team=lambda _: team).accepted


def test_arbitrary_environment_and_checkout_interpreter_cannot_enable_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_VAULT_INITIALIZATION_ACCEPTED", "1")
    monkeypatch.setenv("FERAL_SIGNED_RELEASE_ACCEPTED", "1")
    verifier = Mock(side_effect=AssertionError("not a bundle"))
    assert not inspect_vault_release_acceptance(interpreter=tmp_path / "python3", verify_team=verifier).accepted
    verifier.assert_not_called()


def test_external_symlinked_interpreter_cannot_borrow_bundle_receipt(bundle, tmp_path):
    executable, _, _ = bundle
    executable.unlink()
    outside = tmp_path / "external-python"
    outside.write_bytes(b"fixture")
    executable.symlink_to(outside)
    verifier = Mock(side_effect=AssertionError("outside signed resources"))
    assert not inspect_vault_release_acceptance(interpreter=executable, verify_team=verifier).accepted


def test_symlinked_or_malformed_receipt_refused(bundle, tmp_path):
    executable, path, _ = bundle
    copied = tmp_path / "info.plist"
    copied.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(copied)
    verifier = Mock(side_effect=AssertionError("unsafe receipt"))
    assert not inspect_vault_release_acceptance(interpreter=executable, verify_team=verifier).accepted
    path.unlink()
    path.write_bytes(b"not a plist")
    assert not inspect_vault_release_acceptance(interpreter=executable, verify_team=verifier).accepted


def test_production_signature_checker_is_bounded_and_requires_developer_id(monkeypatch, tmp_path):
    from security import vault_release_acceptance as module
    from subprocess import CompletedProcess, TimeoutExpired
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        if "--verify" in args:
            return CompletedProcess(args, 0, b"", b"")
        return CompletedProcess(args, 0, b"", b"Authority=Developer ID Application: Fixture\nTeamIdentifier=ABCDEFGHIJ\n")
    monkeypatch.setattr(module.subprocess, "run", run)
    assert module._verified_developer_team(tmp_path) == "ABCDEFGHIJ"
    assert len(calls) == 2 and all(options["timeout"] == 5 for _, options in calls)
    assert "--test-requirement" in calls[0][0] and "--deep" in calls[0][0]
    monkeypatch.setattr(module.subprocess, "run", Mock(side_effect=TimeoutExpired("codesign", 5)))
    assert module._verified_developer_team(tmp_path) is None
    monkeypatch.setattr(module.subprocess, "run", lambda args, **kw: CompletedProcess(args, 0, b"", b"Signature=adhoc\nTeamIdentifier=ABCDEFGHIJ\n"))
    assert module._verified_developer_team(tmp_path) is None


def test_actual_deferred_state_binds_same_storage_and_preserves_local_use(monkeypatch, tmp_path):
    from api import state as module
    from security import vault
    from security.vault_initialization_api import BoundVaultInitializer
    from security.vault_release_acceptance import VaultReleaseAcceptance
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    monkeypatch.setenv("FERAL_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("FERAL_NATIVE_DEFER_VAULT", "1")
    monkeypatch.setattr(vault, "_deferred_coordinator", None)
    monkeypatch.setattr(vault, "_keyring_get_password", Mock(side_effect=AssertionError("no OS read")))
    monkeypatch.setattr(vault, "_keyring_set_password", Mock(side_effect=AssertionError("no OS write")))
    from security import vault_release_acceptance as acceptance
    monkeypatch.setattr(acceptance, "inspect_vault_release_acceptance", lambda: VaultReleaseAcceptance(False, "release_acceptance_required"))
    brain = module.BrainState()
    try:
        controller = brain.vault_initialization_controller
        assert controller.coordinator is brain.vault_coordinator
        assert isinstance(brain.vault_coordinator._initializer, BoundVaultInitializer)
        assert controller.vault_path == brain.vault_coordinator._initializer.vault_path == tmp_path / "credentials.json"
        assert controller.status()["code"] == "release_acceptance_required"
        assert brain.memory is not None and brain.vault is None
        assert not (tmp_path / "credentials.enc").exists()
    finally:
        brain.memory.close()


@pytest.mark.asyncio
async def test_initialization_routes_are_mounted_in_actual_server(monkeypatch, tmp_path):
    import httpx
    from api import server
    from security.vault_coordinator import VaultCoordinator
    from security.vault_initialization_api import BoundVaultInitializer, VaultInitializationAPI
    coordinator = VaultCoordinator(fresh_initializer=BoundVaultInitializer(tmp_path / "credentials.json", add_master_key=Mock(side_effect=AssertionError("release gate must not dispatch"))))
    controller = VaultInitializationAPI(coordinator, tmp_path / "credentials.json")
    monkeypatch.setattr(server.state, "vault_initialization_controller", controller)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://127.0.0.1") as client:
        response = await client.get("/api/security/vault/initialize/status")
        assert response.status_code == 200
        assert response.json()["code"] == "release_acceptance_required"
        review = await client.post("/api/security/vault/initialize/review", json={})
        assert review.status_code == 409
        assert review.json()["detail"]["code"] == "release_acceptance_required"
    assert not (tmp_path / "credentials.enc").exists()
