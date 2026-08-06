"""An unauthenticated LAN peer must not be able to mint a credential.

This is a regression test for a live hole, written from a working
exploit rather than from a threat model.

``/api/devices/pair/announce`` and ``/api/devices/pair/code/claim`` were
both in ``_OPEN_PATHS``, which ``APIKeyMiddleware`` consults *before* the
loopback and trusted-transport gates. So both were reachable by anyone,
on any transport, with no credential. Three requests were enough:

    announce a code I chose  -> 200 {"accepted": true}
    claim it                 -> 200 {"token": "..."}
    complete with that token -> 200 {"phone_bearer": "..."}

and the resulting bearer read /api/context/live, /api/conversations and
/api/timeline.

The entropy argument in the comment that justified opening them did not
apply: the caller supplies the code, so there is nothing to guess, and
the five-wrong-attempts limiter never charges because a correct code is
not a wrong attempt.

Because ``_OPEN_PATHS`` short-circuits before the transport check, this
also worked through a relay tunnel, which is to say from the internet.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest


def _lan_request(method: str, path: str, **kw):
    """One request whose peer address is a LAN address, not loopback.

    TestClient cannot set the peer, and the peer is the whole point:
    every gate in APIKeyMiddleware keys off it. httpx's ASGITransport
    takes a ``client`` tuple and drives the real middleware stack.
    """
    from api.server import app

    async def _go():
        transport = httpx.ASGITransport(app=app, client=("192.168.18.10", 44444))
        async with httpx.AsyncClient(transport=transport, base_url="http://brain") as c:
            return await c.request(method, path, **kw)

    return asyncio.run(_go())


class TestTheChainIsBroken:
    def test_claiming_a_code_requires_a_credential(self):
        """The step that mints the token. This is the fix."""
        r = _lan_request("POST", "/api/devices/pair/code/claim",
                         json={"code": "AUDITAAA"})
        assert r.status_code == 401, r.text
        assert "token" not in r.text

    def test_the_full_exploit_chain_no_longer_reaches_a_bearer(self):
        """Replays the exploit end to end and asserts it dies."""
        _lan_request(
            "POST", "/api/devices/pair/announce",
            json={"code": "AUDITAAA", "node_id": "attacker", "name": "attacker"},
        )
        claim = _lan_request("POST", "/api/devices/pair/code/claim",
                             json={"code": "AUDITAAA"})
        assert claim.status_code == 401, (
            "an unauthenticated LAN peer minted a device token: " + claim.text
        )

    def test_claim_is_not_in_the_open_path_set(self):
        """Structural guard. _OPEN_PATHS bypasses every later check, so
        membership is the whole decision."""
        from api.server import _OPEN_PATHS

        assert "/api/devices/pair/code/claim" not in _OPEN_PATHS


class TestAnnounceStaysUsableButBounded:
    """Announce is still open on purpose: the node SDKs call it from
    other machines. It mints nothing, so open is defensible, but it
    stored whatever it was given."""

    def test_announce_is_still_reachable_off_loopback(self):
        from api.server import _OPEN_PATHS

        assert "/api/devices/pair/announce" in _OPEN_PATHS

    @pytest.mark.parametrize(
        "code",
        ["a", "!", "AUDIT AA", "A" * 5000,
         "'; DROP TABLE paired_devices;--", ""],
    )
    def test_a_code_that_is_not_the_sdk_shape_is_refused(self, code, tmp_path):
        from security.device_pairing import DevicePairingStore

        store = DevicePairingStore(db_path=str(tmp_path / "pairs.db"))
        with pytest.raises(ValueError):
            store.announce_pending_code(code=code, node_id="n", name="n")

    @pytest.mark.parametrize("code", ["042195", "000000", "999999", "ABCDEFGH"])
    def test_the_shape_the_sdks_actually_generate_is_accepted(self, code, tmp_path):
        """The fix must not break the flow it is protecting.

        The shipped SDKs emit a 6-digit decimal string, not the 8-char
        base32 the docstrings claim. A first attempt at this validator
        pinned the documented shape and would have rejected every real
        node. These are the values the SDKs actually produce.
        """
        from security.device_pairing import DevicePairingStore

        store = DevicePairingStore(db_path=str(tmp_path / "pairs.db"))
        store.announce_pending_code(code=code, node_id="n", name="n")

    def test_an_implausible_node_id_is_refused(self, tmp_path):
        from security.device_pairing import DevicePairingStore

        store = DevicePairingStore(db_path=str(tmp_path / "pairs.db"))
        with pytest.raises(ValueError):
            store.announce_pending_code(code="042195", node_id="x" * 500, name="n")
