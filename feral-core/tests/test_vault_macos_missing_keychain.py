"""Missing macOS keychains must fail before an OS reset/create prompt."""
from unittest.mock import Mock

import keyring
import pytest

from security import vault

# Capture before the hermetic autouse fixture replaces keyring wrappers.
store_password = vault._keyring_set_password


class MacBackend:
    pass


MacBackend.__module__ = "keyring.backends.macOS"


@pytest.mark.parametrize("state", [(-25307, False), (0, False), (-25295, False)])
def test_missing_or_unavailable_default_blocks_real_macos_write(monkeypatch, state):
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(keyring, "get_keyring", lambda: MacBackend())
    monkeypatch.setattr(vault, "_macos_default_keychain_state", lambda: state)
    setter = Mock()
    monkeypatch.setattr(keyring, "set_password", setter)
    with pytest.raises(vault.VaultKeyUnavailableError, match="will not create or reset"):
        store_password("fixture", "fixture", "fake-secret")
    setter.assert_not_called()


def test_existing_default_keeps_secure_keyring_write(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(keyring, "get_keyring", lambda: MacBackend())
    monkeypatch.setattr(vault, "_macos_default_keychain_state", lambda: (0, True))
    setter = Mock()
    monkeypatch.setattr(keyring, "set_password", setter)
    store_password("fixture", "fixture", "fake-secret")
    setter.assert_called_once_with("fixture", "fixture", "fake-secret")


def test_test_backend_never_queries_os_keychain(monkeypatch):
    from keyring.backends.null import Keyring

    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(keyring, "get_keyring", lambda: Keyring())
    probe = Mock(side_effect=AssertionError("test backend must not query Security.framework"))
    monkeypatch.setattr(vault, "_macos_default_keychain_state", probe)
    monkeypatch.setattr(keyring, "set_password", Mock())
    store_password("fixture", "fixture", "fake-secret")
    probe.assert_not_called()


def test_custom_fake_backend_never_queries_os_keychain(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(keyring, "get_keyring", lambda: object())
    probe = Mock(side_effect=AssertionError("custom backend must not query Security.framework"))
    monkeypatch.setattr(vault, "_macos_default_keychain_state", probe)
    monkeypatch.setattr(keyring, "set_password", Mock())
    store_password("fixture", "fixture", "fake-secret")
    probe.assert_not_called()


def test_chainer_cannot_fall_through_to_missing_macos_keychain(monkeypatch):
    class Chainer:
        backends = [MacBackend()]

    Chainer.__module__ = "keyring.backends.chainer"
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(keyring, "get_keyring", lambda: Chainer())
    monkeypatch.setattr(vault, "_macos_default_keychain_state", lambda: (-25307, False))
    setter = Mock()
    monkeypatch.setattr(keyring, "set_password", setter)
    with pytest.raises(vault.VaultKeyUnavailableError):
        store_password("fixture", "fixture", "fake-secret")
    setter.assert_not_called()


def test_keychain_probe_failure_does_not_fall_through_to_write(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(keyring, "get_keyring", lambda: MacBackend())
    monkeypatch.setattr(vault, "_macos_default_keychain_state", Mock(side_effect=OSError("fixture")))
    setter = Mock()
    monkeypatch.setattr(keyring, "set_password", setter)
    with pytest.raises(vault.VaultKeyUnavailableError, match="No keychain was created or reset"):
        store_password("fixture", "fixture", "fake-secret")
    setter.assert_not_called()
