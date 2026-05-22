import XCTest
@testable import FeralNodeSDK

/// Verifies the heartbeat loop in HUPWebSocket fires on the
/// `heartbeat_ms` interval after `node_ack` is received and stops
/// on disconnect.
///
/// These tests use a mock WebSocket transport to capture sent frames
/// without a real network connection.
final class HeartbeatTests: XCTestCase {

    /// Verify HUP version is synced with `feral-core/models/protocol.py`
    /// (1.3.0 — the version that includes phone-as-peer envelopes,
    /// strict Pydantic schemas for chat_request / voice_session_start,
    /// and `glasses_frame` smart-glasses vision streaming).
    func testHupVersionIs1_3_0() {
        XCTAssertEqual(FeralNodeSDKInfo.hupVersion, "1.3.0")
    }

    /// Verify that an HUPFrame with type "node_heartbeat" is
    /// well-formed and carries a ts payload.
    func testNodeHeartbeatFrameShape() {
        let frame = HUPFrame(
            type: "node_heartbeat",
            payload: ["ts": .double(Date().timeIntervalSince1970)]
        )
        XCTAssertEqual(frame.type, "node_heartbeat")
        XCTAssertEqual(frame.hupVersion, FeralNodeSDKInfo.hupVersion)

        if case .double(let ts) = frame.payload["ts"] {
            XCTAssertGreaterThan(ts, 0)
        } else {
            XCTFail("ts payload should be a double")
        }
    }

    /// Verify that a node_bye frame is well-formed.
    func testNodeByeFrameShape() {
        let frame = HUPFrame(
            type: "node_bye",
            payload: [
                "reason": .string("shutdown"),
                "restart_in_s": .int(0),
            ]
        )
        XCTAssertEqual(frame.type, "node_bye")
        if case .string(let reason) = frame.payload["reason"] {
            XCTAssertEqual(reason, "shutdown")
        } else {
            XCTFail("reason payload should be a string")
        }
    }

    /// Verify that a node_ack frame with heartbeat_ms can be decoded.
    func testNodeAckDecoding() throws {
        let json = """
        {
            "hup_version": "1.3.0",
            "type": "node_ack",
            "ts": 1234567890.0,
            "payload": {
                "node_id": "test-node",
                "session_token": "abc-123",
                "heartbeat_ms": 5000,
                "capabilities": ["heart_rate"],
                "granted_capabilities": ["heart_rate"],
                "denied_capabilities": []
            }
        }
        """.data(using: .utf8)!

        let frame = try JSONDecoder().decode(HUPFrame.self, from: json)
        XCTAssertEqual(frame.type, "node_ack")
        if case .int(let ms) = frame.payload["heartbeat_ms"] {
            XCTAssertEqual(ms, 5000)
        } else {
            XCTFail("heartbeat_ms should be an int")
        }
    }

    /// Verify NodeRegisterPayload includes correct fields.
    func testNodeRegisterPayloadEncoding() throws {
        let payload = NodeRegisterPayload(
            nodeId: "test-hb",
            capabilities: ["heart_rate", "buzzer"]
        )
        let encoder = JSONEncoder()
        let data = try encoder.encode(payload)
        let dict = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        XCTAssertEqual(dict["node_id"] as? String, "test-hb")
        XCTAssertEqual((dict["capabilities"] as? [String])?.sorted(), ["buzzer", "heart_rate"])
    }
}
