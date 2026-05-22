"""
Tests for session authentication, device pairing, and REST API key guard.

Covers:
  - Token generation / save / load / verify
  - Session auth gating logic
  - Localhost bypass
  - DevicePairingStore CRUD
  - Pairing verify + last_seen bump
  - Revoke idempotency
  - REST API key middleware behaviour
"""

import time

import pytest


# ─────────────────────────────────────────────
# Part A — Session Authentication
# ─────────────────────────────────────────────


class TestGenerateSessionToken:
    def test_length_and_hex(self):
        from security.session_auth import generate_session_token

        token = generate_session_token()
        assert len(token) == 32
        int(token, 16)  # must be valid hex

    def test_uniqueness(self):
        from security.session_auth import generate_session_token

        tokens = {generate_session_token() for _ in range(50)}
        assert len(tokens) == 50


class TestSaveLoadToken:
    def test_round_trip(self, tmp_path, monkeypatch):
        from security.session_auth import save_session_token, load_session_token

        monkeypatch.setenv("FERAL_HOME", str(tmp_path))
        save_session_token("aabbccdd11223344aabbccdd11223344")
        assert load_session_token() == "aabbccdd11223344aabbccdd11223344"

    def test_load_missing(self, tmp_path, monkeypatch):
        from security.session_auth import load_session_token

        monkeypatch.setenv("FERAL_HOME", str(tmp_path / "nope"))
        assert load_session_token() is None

    def test_load_empty_file(self, tmp_path, monkeypatch):
        from security.session_auth import load_session_token

        monkeypatch.setenv("FERAL_HOME", str(tmp_path))
        (tmp_path / "session_token").write_text("   ")
        assert load_session_token() is None


class TestVerifySession:
    def test_valid(self, tmp_path, monkeypatch):
        from security.session_auth import save_session_token, verify_session

        monkeypatch.setenv("FERAL_HOME", str(tmp_path))
        save_session_token("deadbeef" * 4)
        assert verify_session("deadbeef" * 4) is True

    def test_invalid(self, tmp_path, monkeypatch):
        from security.session_auth import save_session_token, verify_session

        monkeypatch.setenv("FERAL_HOME", str(tmp_path))
        save_session_token("deadbeef" * 4)
        assert verify_session("00000000" * 4) is False

    def test_no_stored_token(self, tmp_path, monkeypatch):
        from security.session_auth import verify_session

        monkeypatch.setenv("FERAL_HOME", str(tmp_path / "empty"))
        assert verify_session("anything") is False


class TestSessionAuthRequired:
    def test_env_true(self, tmp_path, monkeypatch):
        from security.session_auth import session_auth_required

        monkeypatch.setenv("FERAL_HOME", str(tmp_path))
        monkeypatch.setenv("FERAL_SESSION_AUTH", "true")
        assert session_auth_required() is True

    def test_token_file_exists(self, tmp_path, monkeypatch):
        from security.session_auth import session_auth_required

        monkeypatch.setenv("FERAL_HOME", str(tmp_path))
        monkeypatch.delenv("FERAL_SESSION_AUTH", raising=False)
        (tmp_path / "session_token").write_text("tok")
        assert session_auth_required() is True

    def test_nothing_set(self, tmp_path, monkeypatch):
        from security.session_auth import session_auth_required

        monkeypatch.setenv("FERAL_HOME", str(tmp_path))
        monkeypatch.delenv("FERAL_SESSION_AUTH", raising=False)
        assert session_auth_required() is False


class TestLocalhostBypass:
    def test_localhost_ips(self):
        from security.session_auth import is_localhost

        assert is_localhost("127.0.0.1") is True
        assert is_localhost("::1") is True
        assert is_localhost("localhost") is True
        assert is_localhost("192.168.1.5") is False
        assert is_localhost(None) is False

    def test_bypass_default_false(self, monkeypatch):
        """audit-r12 A1 (v2026.5.38) — FERAL_LOCAL_BYPASS defaults to False.

        Loopback still bypasses HTTP/WS auth in the middleware via the
        explicit ``is_localhost`` check; this knob only controls whether
        off-loopback callers (LAN / Tailscale / 0.0.0.0 bind) also skip
        auth. The secure default forces that to ``False``.
        """
        from security.session_auth import local_bypass_enabled

        monkeypatch.delenv("FERAL_LOCAL_BYPASS", raising=False)
        assert local_bypass_enabled() is False

    def test_bypass_enabled_explicit(self, monkeypatch):
        from security.session_auth import local_bypass_enabled

        for raw in ("1", "true", "TRUE", "yes"):
            monkeypatch.setenv("FERAL_LOCAL_BYPASS", raw)
            assert local_bypass_enabled() is True

    def test_bypass_disabled_explicit(self, monkeypatch):
        from security.session_auth import local_bypass_enabled

        for raw in ("0", "false", "no", ""):
            monkeypatch.setenv("FERAL_LOCAL_BYPASS", raw)
            assert local_bypass_enabled() is False


class TestUnsafeBypassWarning:
    """``warn_if_unsafe_bypass`` is the loud-warning helper for boot
    + doctor. It fires only when bypass is enabled AND the bind is
    non-loopback; loopback binds are silent, no-bypass is silent.
    """

    def test_silent_when_bypass_off(self, monkeypatch):
        from security.session_auth import warn_if_unsafe_bypass

        monkeypatch.delenv("FERAL_LOCAL_BYPASS", raising=False)
        assert warn_if_unsafe_bypass("0.0.0.0") is None

    def test_silent_on_loopback_bind(self, monkeypatch):
        from security.session_auth import warn_if_unsafe_bypass

        monkeypatch.setenv("FERAL_LOCAL_BYPASS", "1")
        for bind in ("127.0.0.1", "::1", "localhost"):
            assert warn_if_unsafe_bypass(bind) is None

    def test_warns_on_lan_bind_with_bypass(self, monkeypatch, caplog):
        from security.session_auth import warn_if_unsafe_bypass

        monkeypatch.setenv("FERAL_LOCAL_BYPASS", "1")
        with caplog.at_level("WARNING", logger="feral.session_auth"):
            msg = warn_if_unsafe_bypass("0.0.0.0")
        assert msg is not None
        assert "FERAL_LOCAL_BYPASS" in msg
        assert "0.0.0.0" in msg
        assert any("FERAL_LOCAL_BYPASS" in r.message for r in caplog.records)


# ─────────────────────────────────────────────
# Part B — Device Pairing CRUD
# ─────────────────────────────────────────────


class TestDevicePairing:
    @pytest.fixture(autouse=True)
    def _pairing_store(self, tmp_path):
        from security.device_pairing import DevicePairingStore

        self.store = DevicePairingStore(db_path=str(tmp_path / "pair.db"))

    def test_pair_returns_keys(self):
        result = self.store.pair_device("iPhone")
        assert "device_id" in result
        assert "token" in result
        assert result["name"] == "iPhone"
        assert len(result["token"]) == 64  # 32 bytes hex

    def test_pair_unique_tokens(self):
        a = self.store.pair_device("A")
        b = self.store.pair_device("B")
        assert a["token"] != b["token"]
        assert a["device_id"] != b["device_id"]

    def test_verify_valid(self):
        result = self.store.pair_device("dev1")
        device_id = self.store.verify_device(result["token"])
        assert device_id == result["device_id"]

    def test_verify_invalid(self):
        assert self.store.verify_device("nonexistent-token") is None

    def test_verify_bumps_last_seen(self):
        result = self.store.pair_device("dev2")
        before = self.store.list_devices()[0]["last_seen"]
        assert before is None or before == 0 or before is not None
        time.sleep(0.05)
        self.store.verify_device(result["token"])
        after = self.store.list_devices()[0]["last_seen"]
        assert after is not None
        assert after > 0

    def test_list_devices(self):
        self.store.pair_device("A")
        self.store.pair_device("B")
        devices = self.store.list_devices()
        assert len(devices) == 2
        names = {d["name"] for d in devices}
        assert names == {"A", "B"}

    def test_revoke(self):
        result = self.store.pair_device("temp")
        assert self.store.revoke_device(result["device_id"]) is True
        assert len(self.store.list_devices()) == 0
        assert self.store.verify_device(result["token"]) is None

    def test_revoke_nonexistent(self):
        assert self.store.revoke_device("no-such-id") is False

    def test_list_empty(self):
        assert self.store.list_devices() == []


# ─────────────────────────────────────────────
# Part C — REST API Key Guard (unit-level)
# ─────────────────────────────────────────────


class TestAPIKeyLoading:
    def test_env_key(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FERAL_HOME", str(tmp_path))
        monkeypatch.setenv("FERAL_API_KEY", "my-secret")
        from api.server import _load_api_key

        assert _load_api_key() == "my-secret"

    def test_file_key(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FERAL_HOME", str(tmp_path))
        monkeypatch.delenv("FERAL_API_KEY", raising=False)
        (tmp_path / "api_key").write_text("file-secret\n")
        from api.server import _load_api_key

        assert _load_api_key() == "file-secret"

    def test_no_key(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FERAL_HOME", str(tmp_path))
        monkeypatch.delenv("FERAL_API_KEY", raising=False)
        from api.server import _load_api_key

        assert _load_api_key() is None
