import XCTest
@testable import FeralBridge

/// Regression coverage for the v2026.5.45 sensor-field fix.
///
/// Pre-v2026.5.45 the string overload
/// `FeralBrainClient.sendSensorData(type: String, data:)` emitted the
/// payload field as ``sensor_type``, which the brain's
/// `SensorTelemetryPayload` did not declare. HealthKit + Location
/// frames silently failed `parse_message` until Worker D landed a
/// transitional Pydantic alias on the brain side. New iOS code
/// emits the canonical ``sensor`` key directly; this test pins that
/// behaviour so a future refactor cannot regress the wire shape.
final class FeralBrainClientSensorTests: XCTestCase {

    // MARK: - Wire shape

    func testStringOverloadEmitsCanonicalSensorKey() {
        let client = FeralBrainClient(host: "localhost", port: 9090, nodeId: "iphone-test")
        let captured = XCTestExpectation(description: "captured wire JSON")
        var lastJSON: String?
        client.debugMessageSink = { json in
            lastJSON = json
            captured.fulfill()
        }
        client.sendSensorData(type: "heart_rate", data: ["bpm": 72])
        wait(for: [captured], timeout: 2.0)
        let json = try? XCTUnwrap(lastJSON)
        guard let raw = json,
              let data = raw.data(using: .utf8),
              let envelope = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let payload = envelope["payload"] as? [String: Any] else {
            XCTFail("could not decode wire envelope")
            return
        }
        XCTAssertEqual(envelope["type"] as? String, "sensor_telemetry")
        XCTAssertEqual(envelope["hop"] as? String, "node")
        XCTAssertEqual(payload["sensor"] as? String, "heart_rate",
                       "string overload must emit canonical 'sensor' key")
        XCTAssertNil(payload["sensor_type"],
                     "legacy 'sensor_type' key must not be present in new wire shape")
        XCTAssertEqual(payload["node_id"] as? String, "iphone-test")
        XCTAssertNotNil(payload["data"])
    }

    func testEnumOverloadStillEmitsCanonicalSensorKey() {
        let client = FeralBrainClient(host: "localhost", port: 9090, nodeId: "iphone-test")
        let captured = XCTestExpectation(description: "captured wire JSON")
        captured.expectedFulfillmentCount = 1
        captured.assertForOverFulfill = false
        var lastJSON: String?
        client.debugMessageSink = { json in
            lastJSON = json
            captured.fulfill()
        }
        // heartRate triggers immediate flush so the sink receives the
        // frame without waiting for the periodic flush timer.
        client.sendSensorData(.heartRate, value: ["bpm": 75])
        wait(for: [captured], timeout: 2.0)
        guard let json = lastJSON,
              let data = json.data(using: .utf8),
              let envelope = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let payload = envelope["payload"] as? [String: Any] else {
            XCTFail("could not decode wire envelope")
            return
        }
        XCTAssertEqual(payload["sensor"] as? String, "heart_rate")
        XCTAssertNil(payload["sensor_type"])
    }

    // MARK: - Enum coverage

    func testEnumCoversAllHealthKitAndLocationSensorNames() {
        // Pin the enum surface so HealthKitManager + FeralLocationManager
        // can move off the string overload entirely if a future refactor
        // chooses to. The brain side already accepts every name below
        // (test_parse_message_legacy_alias_works_for_every_healthkit_sensor
        // in feral-core/tests/test_sensor_telemetry_ingest.py).
        let expected: Set<String> = [
            "heart_rate", "spo2", "temperature", "uv", "steps",
            "gesture", "sleep", "location",
        ]
        let actual: Set<String> = [
            FeralSensorType.heartRate.rawValue,
            FeralSensorType.spo2.rawValue,
            FeralSensorType.temperature.rawValue,
            FeralSensorType.uv.rawValue,
            FeralSensorType.steps.rawValue,
            FeralSensorType.gesture.rawValue,
            FeralSensorType.sleep.rawValue,
            FeralSensorType.location.rawValue,
        ]
        XCTAssertEqual(actual, expected)
    }
}
