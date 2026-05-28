import XCTest
@testable import FeralBridge

/// Behavioural tests for `CameraGlassesAdapter`. The production
/// AVCaptureSession binding is not exercised here — the test
/// injects a `CameraGlassesFrameSource` fake and asserts that each
/// frame is converted to a HUP `glasses_frame` with the canonical
/// wire shape `state.glasses_buffer` consumes.
final class CameraGlassesAdapterTests: XCTestCase {

    // MARK: - Helpers

    private final class FakeFrameSource: CameraGlassesFrameSource {
        var handler: ((CameraGlassesFrame) -> Void)?
        var startCalls = 0
        var stopCalls = 0
        func start(handler: @escaping (CameraGlassesFrame) -> Void) throws {
            self.handler = handler
            startCalls += 1
        }
        func stop() { stopCalls += 1 }

        func emit(_ frame: CameraGlassesFrame) {
            handler?(frame)
        }
    }

    private func makeFrame(bytes: Int = 64, width: Int = 1280, height: Int = 720) -> CameraGlassesFrame {
        CameraGlassesFrame(
            jpegData: Data(repeating: 0xab, count: bytes),
            width: width,
            height: height,
            timestamp: Date()
        )
    }

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

    // MARK: - Tests

    func testInjectedFrameEmitsGlassesFrameEnvelope() {
        let client = FeralBrainClient(host: "localhost", port: 9090, nodeId: "iphone-glasses")
        let observed = captureSink(client)
        let source = FakeFrameSource()
        let adapter = CameraGlassesAdapter(brainClient: client, frameSource: source)
        adapter.start()
        XCTAssertEqual(source.startCalls, 1)

        source.emit(makeFrame())

        let exp = expectation(description: "settle")
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { exp.fulfill() }
        wait(for: [exp], timeout: 1.0)

        let frames = observed()
        XCTAssertEqual(frames.count, 1)
        let env = frames[0]
        XCTAssertEqual(env["type"] as? String, "glasses_frame")
        let payload = env["payload"] as? [String: Any]
        XCTAssertEqual(payload?["device_id"] as? String, "iphone-glasses")
        XCTAssertEqual(payload?["encoding"] as? String, "jpeg")
        XCTAssertEqual(payload?["source"] as? String, "camera_fallback")
        XCTAssertEqual(payload?["width"] as? Int, 1280)
        XCTAssertEqual(payload?["height"] as? Int, 720)
        XCTAssertEqual(payload?["sequence"] as? Int, 1)
        XCTAssertNotNil(payload?["data_b64"] as? String)
        XCTAssertNotNil(payload?["timestamp"])
    }

    func testSequenceIncrementsPerFrame() {
        let client = FeralBrainClient(host: "localhost", port: 9090, nodeId: "iphone-glasses")
        let observed = captureSink(client)
        let source = FakeFrameSource()
        let adapter = CameraGlassesAdapter(brainClient: client, frameSource: source)
        adapter.start()

        source.emit(makeFrame())
        source.emit(makeFrame())
        source.emit(makeFrame())

        let exp = expectation(description: "settle")
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { exp.fulfill() }
        wait(for: [exp], timeout: 1.0)

        let sequences = observed().compactMap { ($0["payload"] as? [String: Any])?["sequence"] as? Int }
        XCTAssertEqual(sequences, [1, 2, 3])
    }

    func testStopStopsUnderlyingFrameSource() {
        let client = FeralBrainClient(host: "localhost", port: 9090, nodeId: "iphone-glasses")
        let source = FakeFrameSource()
        let adapter = CameraGlassesAdapter(brainClient: client, frameSource: source)
        adapter.start()
        adapter.stop()
        XCTAssertEqual(source.stopCalls, 1)
    }

    func testDefaultOperatorNoteIsHonestAboutFallback() {
        // The Settings UI surfaces this string; it must explain that
        // QCSDK requires hardware and the phone camera is filling in.
        let note = CameraGlassesAdapterSettings.defaultOperatorNote
        XCTAssertTrue(note.contains("W610"))
        XCTAssertTrue(note.contains("phone"))
        XCTAssertTrue(note.contains("glasses_frame"))
    }
}
