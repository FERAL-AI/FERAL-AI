import XCTest
@testable import FeralBridge

/// Behavioural tests for `BLEPeripheralScanner`. CoreBluetooth is
/// not exercised here — the test injects a fake
/// `BLECentralManaging` and drives discoveries directly so the
/// dedupe / re-announce / lost-sweep contract can be pinned
/// deterministically.
final class BLEPeripheralScannerTests: XCTestCase {

    // MARK: - Helpers

    private final class FakeCentral: BLECentralManaging {
        var bleState: BLEManagerState = .poweredOn
        var didStart = false
        var didStop = false
        func startScan() { didStart = true }
        func stopScan() { didStop = true }
    }

    /// Capture wire JSON emitted by the brain client.
    private func captureSink(_ client: FeralBrainClient) -> () -> [[String: Any]] {
        let lock = NSLock()
        var emitted: [[String: Any]] = []
        client.debugMessageSink = { json in
            guard let data = json.data(using: .utf8),
                  let env = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            else { return }
            lock.lock(); defer { lock.unlock() }
            emitted.append(env)
        }
        return {
            lock.lock(); defer { lock.unlock() }
            return emitted
        }
    }

    private func makeAdvertisement(
        id: UUID = UUID(),
        name: String = "AirPods Pro",
        rssi: Int = -42,
        services: [String] = ["110A"],
        at date: Date = Date()
    ) -> BLEPeripheralAdvertisement {
        BLEPeripheralAdvertisement(
            identifier: id,
            localName: name,
            rssi: rssi,
            serviceUUIDs: services,
            manufacturerDataHex: "4c001907",
            isConnectable: true,
            timestamp: date
        )
    }

    // MARK: - Tests

    func testFirstDiscoveryEmitsDeviceAnnounce() {
        let client = FeralBrainClient(host: "localhost", port: 9090, nodeId: "iphone-test")
        let observed = captureSink(client)
        let scanner = BLEPeripheralScanner(brainClient: client)
        let fake = FakeCentral()
        scanner.start(central: fake)
        XCTAssertTrue(fake.didStart)

        scanner.handleDiscovery(makeAdvertisement())

        let exp = expectation(description: "frame emitted")
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { exp.fulfill() }
        wait(for: [exp], timeout: 1.0)

        let frames = observed()
        XCTAssertEqual(frames.count, 1)
        let env = frames[0]
        XCTAssertEqual(env["type"] as? String, "device_announce")
        let payload = env["payload"] as? [String: Any]
        XCTAssertEqual(payload?["device_kind"] as? String, "bluetooth_le")
        XCTAssertEqual(payload?["scanner_node_id"] as? String, "iphone-test")
        XCTAssertEqual(payload?["name"] as? String, "AirPods Pro")
        XCTAssertEqual(payload?["rssi_dbm"] as? Int, -42)
        XCTAssertEqual((payload?["advertised_services"] as? [String])?.first, "110A")
        let metadata = payload?["metadata"] as? [String: Any]
        let mfg = metadata?["manufacturer_data"] as? [String: Any]
        XCTAssertEqual(mfg?["raw_hex"] as? String, "4c001907")
    }

    func testDedupeWithinReannounceWindow() {
        let client = FeralBrainClient(host: "localhost", port: 9090, nodeId: "iphone-test")
        let observed = captureSink(client)
        let scanner = BLEPeripheralScanner(
            brainClient: client,
            settings: BLEPeripheralScannerSettings(reannounceInterval: 60, lostThreshold: 300, sweepInterval: 5)
        )
        scanner.start(central: FakeCentral())

        let id = UUID()
        let base = Date()
        scanner.handleDiscovery(makeAdvertisement(id: id, at: base))
        scanner.handleDiscovery(makeAdvertisement(id: id, at: base.addingTimeInterval(5)))
        scanner.handleDiscovery(makeAdvertisement(id: id, at: base.addingTimeInterval(30)))

        let exp = expectation(description: "settle")
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { exp.fulfill() }
        wait(for: [exp], timeout: 1.0)

        // Three observations within the 60 s window → exactly one
        // device_announce frame on the wire.
        XCTAssertEqual(observed().count, 1)
    }

    func testReAnnounceAfterIntervalElapses() {
        let client = FeralBrainClient(host: "localhost", port: 9090, nodeId: "iphone-test")
        let observed = captureSink(client)
        let scanner = BLEPeripheralScanner(
            brainClient: client,
            settings: BLEPeripheralScannerSettings(reannounceInterval: 60, lostThreshold: 300, sweepInterval: 5)
        )
        scanner.start(central: FakeCentral())

        let id = UUID()
        let base = Date()
        scanner.handleDiscovery(makeAdvertisement(id: id, at: base))
        scanner.handleDiscovery(makeAdvertisement(id: id, at: base.addingTimeInterval(75)))

        let exp = expectation(description: "settle")
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { exp.fulfill() }
        wait(for: [exp], timeout: 1.0)

        // Second observation past the 60 s window → re-announce.
        XCTAssertEqual(observed().count, 2)
    }

    func testFeralOwnGlassesAreSuppressed() {
        let client = FeralBrainClient(host: "localhost", port: 9090, nodeId: "iphone-test")
        let observed = captureSink(client)
        let scanner = BLEPeripheralScanner(
            brainClient: client,
            ownFilter: .feralGlasses
        )
        scanner.start(central: FakeCentral())

        scanner.handleDiscovery(makeAdvertisement(services: ["FEE0"]))
        scanner.handleDiscovery(makeAdvertisement(services: ["0000FEE0-0000-1000-8000-00805F9B34FB"]))

        let exp = expectation(description: "settle")
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { exp.fulfill() }
        wait(for: [exp], timeout: 1.0)

        XCTAssertEqual(observed().count, 0, "own glasses must not double-announce")
    }

    func testLostSweepEmitsDeviceLostMetadata() {
        let client = FeralBrainClient(host: "localhost", port: 9090, nodeId: "iphone-test")
        let observed = captureSink(client)

        // Pin the "now" clock so the sweep can deterministically see
        // the peripheral as silent for > lostThreshold.
        let pinnedNow = Date().addingTimeInterval(500)
        let scanner = BLEPeripheralScanner(
            brainClient: client,
            settings: BLEPeripheralScannerSettings(
                reannounceInterval: 60,
                lostThreshold: 120,
                sweepInterval: 5
            ),
            ownFilter: .none,
            now: { pinnedNow }
        )
        scanner.start(central: FakeCentral())

        let id = UUID()
        scanner.handleDiscovery(makeAdvertisement(id: id, at: Date()))

        let warmup = expectation(description: "warmup")
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { warmup.fulfill() }
        wait(for: [warmup], timeout: 1.0)

        scanner.runSweep()

        let settle = expectation(description: "settle")
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { settle.fulfill() }
        wait(for: [settle], timeout: 1.0)

        let frames = observed()
        XCTAssertEqual(frames.count, 2, "announce + lost-marker")
        let lost = frames.last
        XCTAssertEqual(lost?["type"] as? String, "device_announce")
        let payload = lost?["payload"] as? [String: Any]
        let metadata = payload?["metadata"] as? [String: Any]
        XCTAssertEqual(metadata?["lost"] as? Bool, true)
    }

    func testStopStopsUnderlyingCentral() {
        let client = FeralBrainClient(host: "localhost", port: 9090, nodeId: "iphone-test")
        let scanner = BLEPeripheralScanner(brainClient: client)
        let fake = FakeCentral()
        scanner.start(central: fake)
        scanner.stop()
        XCTAssertTrue(fake.didStop)
    }
}
