"""HUP_SPEC.md section 5 (envelope) and section 6 (capability grants).

Both sections described behaviour the brain did not have.

**Section 5** says every HUP frame carries ``hup_version``, ``type``,
``ts`` and ``payload``. Five brain-to-node sends did; twelve did not,
including all five ``hup_action_request`` builders -- the actuator
command frame. ``tests/test_hup_version_unified.py`` guards the source
against a regression; the tests here drive the real code and read what
lands on the socket.

**Section 6** says gating happens per device in the UI, that the brain
MUST NOT issue an ``hup_action_request`` for a capability outside
``granted_capabilities``, and that it MUST drop camera / microphone
frames from a node whose tier is disabled. Nothing implemented any of it:
``node_ack`` echoed the node's own declaration back, no store existed,
and both node SDKs read an empty grant list as "everything I declared".
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from models.protocol import HUP_VERSION, hup_frame, stamp_hup_envelope
from security.capability_grants import (
    TIER_AUDIO,
    TIER_CAMERA,
    TIER_MOTOR,
    TIER_PASSIVE_SENSOR,
    CapabilityGrantStore,
    action_denied,
    frame_tier_enabled,
    tier_for,
)


@pytest.fixture()
def grants(tmp_path):
    return CapabilityGrantStore(db_path=str(tmp_path / "grants.db"))


def _assert_envelope(frame: dict, expected_type: str = "") -> None:
    assert isinstance(frame, dict), f"not a frame: {frame!r}"
    for key in ("hup_version", "type", "ts", "payload"):
        assert key in frame, (
            f"frame is missing the HUP_SPEC section 5 key {key!r}: "
            f"{sorted(frame)}"
        )
    assert frame["hup_version"] == HUP_VERSION
    assert isinstance(frame["ts"], float) and frame["ts"] > 0
    assert isinstance(frame["payload"], dict)
    if expected_type:
        assert frame["type"] == expected_type


# ─────────────────────────────────────────────
# Section 5 — the envelope builders
# ─────────────────────────────────────────────

class TestEnvelopeBuilders:
    def test_hup_frame_is_complete(self):
        _assert_envelope(hup_frame("node_ack", {"node_id": "n1"}), "node_ack")

    def test_hup_frame_defaults_payload_to_an_object(self):
        # ``payload`` is required by section 5, so a frame with nothing to
        # say still carries an empty object rather than omitting the key.
        _assert_envelope(hup_frame("node_bye"), "node_bye")

    def test_hup_frame_keeps_extras_outside_the_payload(self):
        frame = hup_frame("hup_action_request", {"name": "buzz"}, hop="brain")
        assert frame["hop"] == "brain"
        assert "hop" not in frame["payload"]

    def test_stamp_fills_in_what_is_missing(self):
        _assert_envelope(
            stamp_hup_envelope({"type": "somatic_state", "payload": {"a": 1}}),
            "somatic_state",
        )

    def test_stamp_never_overwrites_a_value_the_caller_set(self):
        # Forwarding a frame must not restamp it with the brain's clock,
        # or a replayed frame's ordering is silently rewritten.
        frame = stamp_hup_envelope(
            {"type": "x", "ts": 1.5, "hup_version": "1.0.0", "payload": {}},
        )
        assert frame["ts"] == 1.5
        assert frame["hup_version"] == "1.0.0"

    def test_stamp_mutates_and_returns_the_same_dict(self):
        original = {"type": "x"}
        assert stamp_hup_envelope(original) is original


# ─────────────────────────────────────────────
# Section 5 — real senders
# ─────────────────────────────────────────────

class TestNodeBoundSendersCarryTheEnvelope:
    """Each of these built a frame by hand with only type + payload."""

    @pytest.mark.asyncio
    async def test_send_dict_to_node_stamps(self):
        # The voice pipeline's whole outbound path: RealtimeProxy and
        # GeminiRealtimeProxy are both constructed with this as their
        # ``send_to_node``. It was a bare passthrough that added nothing.
        from api.state import BrainState

        st = BrainState()
        ws = MagicMock()
        ws.send_json = AsyncMock()
        st.daemons["n1"] = ws
        await st._send_dict_to_node("n1", {"type": "tts_chunk", "payload": {}})
        _assert_envelope(ws.send_json.await_args.args[0], "tts_chunk")

    @pytest.mark.asyncio
    async def test_send_to_daemon_stamps_a_feral_message(self):
        from api.state import BrainState
        from models.protocol import FeralMessage

        st = BrainState()
        ws = MagicMock()
        ws.send_json = AsyncMock()
        st.daemons["n1"] = ws
        await st.send_to_daemon(
            "n1", FeralMessage(hop="brain", type="genui_push", payload={"a": 1}),
        )
        sent = ws.send_json.await_args.args[0]
        _assert_envelope(sent, "genui_push")
        # The legacy FeralMessage fields survive alongside the envelope;
        # nothing that reads them breaks.
        assert sent["hop"] == "brain"
        assert "timestamp_ms" in sent

    @pytest.mark.asyncio
    async def test_mesh_invoke_sends_an_enveloped_action_request(self):
        from hardware.mesh import HardwareMesh
        from hardware.protocol import DeviceRegistry

        daemons: dict = {}
        mesh = HardwareMesh(DeviceRegistry(), daemons)
        sent: list = []

        async def _send_and_resolve(msg: dict):
            sent.append(msg)
            mesh.resolve_invoke(msg["payload"]["action_id"], {"success": True})

        ws = MagicMock()
        ws.send_json = AsyncMock(side_effect=_send_and_resolve)
        daemons["n1"] = ws

        await mesh.invoke("n1", "buzz", {"ms": 200}, timeout=1.0)
        assert sent, "mesh.invoke sent nothing"
        _assert_envelope(sent[0], "hup_action_request")
        assert sent[0]["payload"]["name"] == "buzz"

    @pytest.mark.asyncio
    async def test_websocket_device_adapter_sends_an_enveloped_frame(self):
        from hardware.protocol import HUPAction, HUPActionType, WebSocketDeviceAdapter

        ws = MagicMock()
        ws.send_json = AsyncMock()
        adapter = WebSocketDeviceAdapter(ws, "n1")
        await adapter.execute(HUPAction(device_id="n1", capability_id="buzz", action_type=HUPActionType.EXECUTE))
        _assert_envelope(ws.send_json.await_args.args[0], "hup_action_request")

    @pytest.mark.asyncio
    async def test_gateway_fallback_no_longer_sends_the_removed_alias(self):
        # It sent {"type": "command", "request_id", "command", "args"}:
        # not a HUP frame in any version, no payload, no envelope, and no
        # SDK branch for it. It returned {"dispatched": true} regardless.
        from gateway.protocol import MethodRegistry, register_core_methods

        registry = MethodRegistry()
        st = MagicMock()
        ws = MagicMock()
        ws.send_json = AsyncMock()
        st.daemons = {"n1": ws}
        st.hardware_mesh = None
        register_core_methods(registry, st)
        handler = registry.get("node.invoke")
        assert handler is not None
        result = await handler(
            "sess", {"node_id": "n1", "command": "buzz", "params": {}},
            MagicMock(),
        )
        sent = ws.send_json.await_args.args[0]
        _assert_envelope(sent, "hup_action_request")
        assert sent["payload"]["name"] == "buzz"
        assert result["request_id"] == sent["payload"]["action_id"]


# ─────────────────────────────────────────────
# Section 6 — the grant store
# ─────────────────────────────────────────────

class TestTierMapping:
    @pytest.mark.parametrize("cap,tier", [
        ("heart_rate", TIER_PASSIVE_SENSOR),
        ("read_heart_rate", TIER_PASSIVE_SENSOR),
        ("camera", TIER_CAMERA),
        ("camera_snap", TIER_CAMERA),
        ("capture_photo", TIER_CAMERA),
        ("microphone", TIER_AUDIO),
        ("speaker", TIER_AUDIO),
        ("motor", TIER_MOTOR),
        ("valve", TIER_MOTOR),
    ])
    def test_capability_maps_to_its_spec_tier(self, cap, tier):
        assert tier_for(cap) == tier


class TestGrantStore:
    def test_default_is_granted(self, grants):
        # Not deny. Defaulting camera/audio to denied would take
        # vision-context-attach and ambient transcription offline on every
        # already-paired phone at upgrade, with no operator action and no
        # error. See the module docstring in security/capability_grants.
        assert grants.is_granted("n1", "camera") is True

    def test_a_denial_persists_and_is_readable(self, grants):
        grants.set_grant("n1", "camera", False)
        assert grants.is_granted("n1", "camera") is False
        assert grants.denied_for("n1") == {"camera"}

    def test_a_denial_is_scoped_to_one_device(self, grants):
        # The whole reason this exists: ``hardware.cameras.allowed`` in
        # the operator policy is global, so there was no way to say "no
        # camera on the work phone, yes on the personal one".
        grants.set_grant("work-phone", "camera", False)
        assert grants.is_granted("work-phone", "camera") is False
        assert grants.is_granted("home-phone", "camera") is True

    def test_re_granting_restores_it(self, grants):
        grants.set_grant("n1", "camera", False)
        grants.set_grant("n1", "camera", True)
        assert grants.is_granted("n1", "camera") is True
        assert grants.denied_for("n1") == set()

    def test_partition_is_what_node_ack_puts_on_the_wire(self, grants):
        grants.set_grant("n1", "camera", False)
        granted, denied = grants.partition(
            "n1", ["heart_rate", "camera", "buzzer"],
        )
        assert granted == ["heart_rate", "buzzer"]
        assert denied == ["camera"]

    def test_grants_for_distinguishes_an_answer_from_a_default(self, grants):
        grants.set_grant("n1", "camera", False)
        rows = {r["capability"]: r for r in grants.grants_for("n1", ["camera", "buzzer"])}
        assert rows["camera"]["granted"] is False
        assert rows["camera"]["explicit"] is True
        assert rows["buzzer"]["granted"] is True
        assert rows["buzzer"]["explicit"] is False
        assert rows["camera"]["tier"] == TIER_CAMERA

    def test_set_tier_toggles_every_declared_capability_in_it(self, grants):
        declared = ["camera", "camera_snap", "heart_rate", "buzzer"]
        changed = grants.set_tier("n1", TIER_CAMERA, False, declared)
        assert sorted(changed) == ["camera", "camera_snap"]
        assert grants.is_granted("n1", "heart_rate") is True
        assert grants.is_granted("n1", "camera_snap") is False

    def test_tier_enabled_follows_the_denials(self, grants):
        assert grants.tier_enabled("n1", TIER_CAMERA) is True
        grants.set_grant("n1", "camera", False)
        assert grants.tier_enabled("n1", TIER_CAMERA) is False
        assert grants.tier_enabled("n1", TIER_AUDIO) is True

    def test_a_tier_with_one_capability_still_allowed_stays_enabled(self, grants):
        grants.set_grant("n1", "camera", False)
        grants.set_grant("n1", "camera_ir", True)
        assert grants.tier_enabled("n1", TIER_CAMERA) is True

    def test_clear_node_forgets_everything(self, grants):
        grants.set_grant("n1", "camera", False)
        assert grants.clear_node("n1") == 1
        assert grants.is_granted("n1", "camera") is True

    def test_a_write_is_visible_to_the_next_read(self, grants):
        # The read path is cached per node -- frame_tier_enabled runs once
        # per inbound frame, and a 30fps stream would otherwise open thirty
        # SQLite connections a second. A toggle has to invalidate it or the
        # operator's click does nothing until restart.
        assert grants.is_granted("n1", "camera") is True
        grants.set_grant("n1", "camera", False)
        assert grants.is_granted("n1", "camera") is False
        assert grants.tier_enabled("n1", TIER_CAMERA) is False
        grants.set_grant("n1", "camera", True)
        assert grants.is_granted("n1", "camera") is True
        assert grants.tier_enabled("n1", TIER_CAMERA) is True
        grants.set_grant("n1", "camera", False)
        assert grants.clear_node("n1") == 1
        assert grants.is_granted("n1", "camera") is True

    def test_a_grant_survives_a_new_store_over_the_same_file(self, tmp_path):
        path = str(tmp_path / "g.db")
        CapabilityGrantStore(db_path=path).set_grant("n1", "camera", False)
        assert CapabilityGrantStore(db_path=path).is_granted("n1", "camera") is False


class TestEnforcementPredicates:
    def test_action_denied_reads_the_store(self, grants):
        grants.set_grant("n1", "camera_snap", False)
        assert action_denied("n1", "buzz", store=grants) is None
        reason = action_denied("n1", "camera_snap", store=grants)
        assert reason and "camera_snap" in reason
        # The reason names the screen the operator fixes it on, because
        # "blocked by policy" with no further detail gives nobody
        # anything to act on.
        assert "Devices" in reason

    def test_action_denied_fails_open_with_no_store(self):
        # Deliberately the opposite direction from
        # ``hardware_policy.permits_unattended``, which fails closed.
        # That one decides whether a human sees an approval card; this one
        # decides whether a capability nobody has denied gets sent at all,
        # and failing closed would take every action offline for the
        # window between process start and AppState.initialize.
        assert action_denied("n1", "buzz", store=None) is None

    def test_a_store_that_raises_does_not_take_actions_offline(self):
        broken = MagicMock()
        broken.is_granted.side_effect = RuntimeError("disk gone")
        assert action_denied("n1", "buzz", store=broken) is None

    def test_frame_tier_enabled_reads_the_store(self, grants):
        assert frame_tier_enabled("n1", TIER_CAMERA, store=grants) is True
        grants.set_grant("n1", "camera", False)
        assert frame_tier_enabled("n1", TIER_CAMERA, store=grants) is False


# ─────────────────────────────────────────────
# Section 6 — the brain refuses to send
# ─────────────────────────────────────────────

class TestBrainWillNotIssueADeniedAction:
    """"Brains MUST NOT issue hup_action_request for a capability that is
    not in granted_capabilities." Nothing checked anything."""

    def test_build_action_request_refuses(self, grants):
        from hardware.action_frames import build_action_request

        grants.set_grant("n1", "camera_snap", False)
        gate = build_action_request(
            "n1", "camera_snap", {}, grant_store=grants,
        )
        assert gate.allowed is False
        assert gate.frame is None
        assert "camera_snap" in gate.denied_reason

    def test_build_action_request_allows_what_is_granted(self, grants):
        from hardware.action_frames import build_action_request

        grants.set_grant("n1", "camera_snap", False)
        gate = build_action_request("n1", "buzz", {"ms": 10}, grant_store=grants)
        assert gate.allowed is True
        _assert_envelope(gate.frame, "hup_action_request")
        assert gate.frame["payload"]["params"] == {"ms": 10}

    @pytest.mark.asyncio
    async def test_mesh_invoke_sends_nothing_for_a_denied_capability(
        self, grants, monkeypatch,
    ):
        import security.capability_grants as cg
        from hardware.mesh import HardwareMesh

        monkeypatch.setattr(cg, "live_grants", lambda: grants)
        grants.set_grant("n1", "camera_snap", False)

        from hardware.protocol import DeviceRegistry

        ws = MagicMock()
        ws.send_json = AsyncMock()
        mesh = HardwareMesh(DeviceRegistry(), {"n1": ws})
        result = await mesh.invoke("n1", "camera_snap", {}, timeout=0.05)

        assert result["success"] is False
        assert result.get("capability_denied") is True
        ws.send_json.assert_not_awaited()
        # And no orphan ledger row: node_heartbeat replays pending command
        # ids back to the node, so a SUBMITTED record for a command that
        # was never sent is a row nothing can resolve.
        assert mesh.ledger.get_pending("n1") == []

    @pytest.mark.asyncio
    async def test_websocket_adapter_sends_nothing_for_a_denied_capability(
        self, grants, monkeypatch,
    ):
        import security.capability_grants as cg
        from hardware.protocol import HUPAction, HUPActionType, WebSocketDeviceAdapter

        monkeypatch.setattr(cg, "live_grants", lambda: grants)
        grants.set_grant("n1", "camera_snap", False)

        ws = MagicMock()
        ws.send_json = AsyncMock()
        adapter = WebSocketDeviceAdapter(ws, "n1")
        result = await adapter.execute(
            HUPAction(device_id="n1", capability_id="camera_snap", action_type=HUPActionType.EXECUTE),
        )
        assert result["sent"] is False
        assert result["denied"] is True
        ws.send_json.assert_not_awaited()


# ─────────────────────────────────────────────
# Section 6 — frame ingress
# ─────────────────────────────────────────────

class TestCameraAndMicrophoneFramesAreDropped:
    """"Brains MUST drop camera_frame and microphone_chunk events from
    nodes whose camera/audio tier is disabled, even if the daemon sends
    them." ``api/server.py`` ingested camera_frame with no check at all."""

    @pytest.fixture(autouse=True)
    def _use_test_store(self, grants, monkeypatch):
        import api.server as srv

        monkeypatch.setattr(srv.state, "capability_grants", grants, raising=False)
        self.grants = grants

    def test_nothing_is_dropped_by_default(self):
        import api.server as srv

        assert srv._frame_tier_refused("n1", "camera_frame", {}) == ""
        assert srv._frame_tier_refused("n1", "audio_frame", {}) == ""

    @pytest.mark.parametrize("msg_type", [
        "camera_frame", "video_frame", "vision_frame", "glasses_frame", "frame",
    ])
    def test_every_image_type_is_dropped_when_camera_is_denied(self, msg_type):
        import api.server as srv

        self.grants.set_grant("n1", "camera", False)
        assert srv._frame_tier_refused("n1", msg_type, {}) == TIER_CAMERA

    @pytest.mark.parametrize("msg_type", [
        "microphone_chunk", "audio_frame", "audio_chunk",
    ])
    def test_every_audio_type_is_dropped_when_audio_is_denied(self, msg_type):
        import api.server as srv

        self.grants.set_grant("n1", "microphone", False)
        assert srv._frame_tier_refused("n1", msg_type, {}) == TIER_AUDIO

    def test_the_v1_1_device_event_wrapper_is_dropped_too(self):
        # Dropping only the two v1.0 names the spec happens to mention
        # would leave the rule bypassed by any daemon speaking v1.1, which
        # wraps the same frames in ``device_event``.
        import api.server as srv

        self.grants.set_grant("n1", "camera", False)
        raw = {"payload": {"event_type": "camera_frame"}}
        assert srv._frame_tier_refused("n1", "device_event", raw) == TIER_CAMERA

    def test_a_denied_camera_does_not_stop_the_microphone(self):
        import api.server as srv

        self.grants.set_grant("n1", "camera", False)
        assert srv._frame_tier_refused("n1", "audio_frame", {}) == ""

    def test_unrelated_frames_are_never_touched(self):
        import api.server as srv

        self.grants.set_grant("n1", "camera", False)
        assert srv._frame_tier_refused("n1", "node_heartbeat", {}) == ""
        assert srv._frame_tier_refused("n1", "chat_request", {}) == ""


# ─────────────────────────────────────────────
# Section 6 — node_ack tells the daemon the truth
# ─────────────────────────────────────────────

class TestNodeAckReportsRealGrants:
    def test_denied_capabilities_is_no_longer_hardcoded_empty(self, grants):
        # api/server.py answered every register with
        #   granted_capabilities = list(payload.capabilities)
        #   denied_capabilities  = []
        # -- the node's own self-declaration echoed back.
        grants.set_grant("phone-1", "camera", False)
        granted, denied = grants.partition(
            "phone-1", ["heart_rate", "camera", "microphone"],
        )
        assert denied == ["camera"]
        assert granted == ["heart_rate", "microphone"]

    def test_the_ack_frame_carries_the_envelope(self, grants):
        granted, denied = grants.partition("phone-1", ["heart_rate"])
        frame = hup_frame("node_ack", {
            "node_id": "phone-1",
            "granted_capabilities": granted,
            "denied_capabilities": denied,
        })
        _assert_envelope(frame, "node_ack")
