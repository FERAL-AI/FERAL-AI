import Foundation
import AppKit

private final class BrowserViewTestProtocol: URLProtocol {
    private static let lock = NSLock()
    private static var stored: [URLRequest] = []
    private static var action: ((BrowserViewTestProtocol, URLRequest) -> Void)?
    static var requests: [URLRequest] { lock.lock(); defer { lock.unlock() }; return stored }
    static func reset(_ handler: @escaping (BrowserViewTestProtocol, URLRequest) -> Void) {
        lock.lock(); stored = []; action = handler; lock.unlock()
    }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.lock.lock(); Self.stored.append(request); let handler = Self.action; Self.lock.unlock()
        handler?(self, request)
    }
    override func stopLoading() { }
    func reply(_ body: [String: Any], status: Int = 200) {
        let data = try! JSONSerialization.data(withJSONObject: body)
        let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: "HTTP/1.1", headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }
}
private final class BrowserViewHeldResponse {
    private let lock = NSLock()
    private var item: BrowserViewTestProtocol?
    func hold(_ value: BrowserViewTestProtocol) { lock.lock(); item = value; lock.unlock() }
    var ready: Bool { lock.lock(); defer { lock.unlock() }; return item != nil }
    func release(_ body: [String: Any], status: Int = 200) {
        lock.lock(); let value = item; item = nil; lock.unlock(); value?.reply(body, status: status)
    }
}

@main struct NativeBrowserViewFeatureTests {
    @MainActor private static var checks = 0
    private static let instant = Date(timeIntervalSince1970: 1_800_000_000)
    private static let viewID = "bc714579-a6b5-47c2-ae12-8da7713bd207"
    private static let token = String(repeating: "a", count: 43)
    private static let target = "target-1"
    @MainActor private static func expect(_ value: @autoclosure () -> Bool, _ message: String) {
        checks += 1
        if !value() { fatalError("Browser view check failed: " + message) }
    }
    @MainActor private static func rejects(_ message: String, _ operation: () throws -> Void) {
        do { try operation(); fatalError("Browser view should reject: " + message) }
        catch { checks += 1 }
    }
    @MainActor private static func wait(_ condition: () -> Bool) async {
        for _ in 0..<2000 { if condition() { return }; try? await Task.sleep(nanoseconds: 1_000_000) }
        fatalError("Browser view fixture barrier was not reached")
    }
    private static func inventory() -> [String: Any] {
        ["success": true, "connected": true, "targets": [["id": target, "label": "Connected browser tab"]], "active_target_id": target]
    }
    private static func grant() -> [String: Any] {
        ["success": true, "view_id": viewID, "token": token, "target_id": target, "expires_at": instant.timeIntervalSince1970 + 300]
    }
    private static func jpeg() -> Data {
        let image = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 8, pixelsHigh: 6, bitsPerSample: 8, samplesPerPixel: 3, hasAlpha: false, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 24, bitsPerPixel: 24)!
        image.bitmapData!.initialize(repeating: 160, count: image.bytesPerRow * image.pixelsHigh)
        return image.representation(using: .jpeg, properties: [.compressionFactor: 0.8])!
    }
    private static func frame(_ sequence: Int64 = 1) -> [String: Any] {
        ["success": true, "view_id": viewID, "target_id": target, "sequence": sequence,
         "captured_at": instant.timeIntervalSince1970, "format": "jpeg", "masked_password_fields": true, "width": 8, "height": 6,
         "image_b64": jpeg().base64EncodedString(), "cursor": ["x": 3.0, "y": 2.0, "phase": "click"]]
    }
    private static func stopReply() -> [String: Any] { ["success": true] }
    private static func body(_ request: URLRequest) -> [String: Any] {
        if let data = request.httpBody { return (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:] }
        guard let stream = request.httpBodyStream else { return [:] }
        stream.open(); defer { stream.close() }; var data = Data(); var bytes = [UInt8](repeating: 0, count: 1024)
        while stream.hasBytesAvailable { let count = stream.read(&bytes, maxLength: bytes.count); if count <= 0 { break }; data.append(contentsOf: bytes.prefix(count)) }
        return (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
    }
    private static func session() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral; configuration.protocolClasses = [BrowserViewTestProtocol.self]
        return URLSession(configuration: configuration)
    }
    private static func ordinary(_ protocolInstance: BrowserViewTestProtocol, _ request: URLRequest) {
        switch request.url!.path {
        case "/api/browser/view/targets": protocolInstance.reply(inventory())
        case "/api/browser/view/start": protocolInstance.reply(grant())
        case "/api/browser/view/frame": protocolInstance.reply(frame())
        case "/api/browser/view/stop": protocolInstance.reply(stopReply())
        default: fatalError("Unexpected viewer route")
        }
    }
    @MainActor private static func wireValidation() throws {
        let lease = try NativeBrowserViewWire.lease(grant(), target: target, now: instant)
        let actual = try NativeBrowserViewWire.frame(frame(), lease: lease, previous: nil, now: instant)
        expect(actual.image.isValid && actual.width == 8 && actual.height == 6 && actual.sequence == 1, "actual JPEG decoded with verified dimensions")
        expect(actual.cursor?.x == 3 && actual.cursor?.phase == "click", "image pixel marker retained")
        var noPhase = frame(); noPhase["cursor"] = ["x": 0, "y": 0]
        let optionalPhase = try NativeBrowserViewWire.frame(noPhase, lease: lease, previous: nil, now: instant)
        expect(optionalPhase.cursor?.phase == "move", "optional cursor phase")
        var noCursor = frame(); noCursor.removeValue(forKey: "cursor")
        let absentCursor = try NativeBrowserViewWire.frame(noCursor, lease: lease, previous: nil, now: instant)
        expect(absentCursor.cursor == nil, "absent marker is not fabricated")
        var unverifiedMask = frame(); unverifiedMask.removeValue(forKey: "masked_password_fields")
        rejects("missing masking confirmation") { _ = try NativeBrowserViewWire.frame(unverifiedMask, lease: lease, previous: nil, now: instant) }
        for (field, bad) in [("success", 1 as Any), ("masked_password_fields", false), ("masked_password_fields", 1), ("sequence", true), ("sequence", 1.5), ("sequence", Int64.max),
                             ("width", true), ("width", 1281), ("width", 9), ("height", 0), ("format", "png"),
                             ("view_id", UUID().uuidString.lowercased()), ("target_id", "foreign-target"),
                             ("captured_at", instant.timeIntervalSince1970 - 11), ("captured_at", instant.timeIntervalSince1970 + 3),
                             ("image_b64", Data("not a JPEG".utf8).base64EncodedString()), ("image_b64", String(repeating: "a", count: 2_097_153))] {
            var badFrame = frame(); badFrame[field] = bad
            rejects("strict frame " + field) { _ = try NativeBrowserViewWire.frame(badFrame, lease: lease, previous: nil, now: instant) }
        }
        for bad in [["x": Double.nan, "y": 0], ["x": 8.0, "y": 0], ["x": 0, "y": -1], ["x": 0, "y": 0, "phase": "os_cursor"], ["x": 0, "y": 0, "phase": true]] as [[String: Any]] {
            var badFrame = frame(); badFrame["cursor"] = bad
            rejects("cursor bounds/type") { _ = try NativeBrowserViewWire.frame(badFrame, lease: lease, previous: nil, now: instant) }
        }
        rejects("duplicate sequence") { _ = try NativeBrowserViewWire.frame(frame(), lease: lease, previous: 1, now: instant) }
        rejects("expired lease") { _ = try NativeBrowserViewWire.frame(frame(), lease: lease, previous: nil, now: instant.addingTimeInterval(300)) }
        for (field, bad) in [("expires_at", true as Any), ("expires_at", instant.timeIntervalSince1970 + 301), ("expires_at", instant.timeIntervalSince1970), ("token", "short"), ("token", String(repeating: ".", count: 43)), ("view_id", viewID.uppercased()), ("target_id", "other")] {
            var badGrant = grant(); badGrant[field] = bad
            rejects("strict grant " + field) { _ = try NativeBrowserViewWire.lease(badGrant, target: target, now: instant) }
        }
        var extraTarget = inventory(); extraTarget["targets"] = [["id": target, "label": "Connected browser tab"], ["id": "other", "label": "Other"]]
        rejects("inventory is attached target only") { _ = try NativeBrowserViewWire.targets(extraTarget) }
        var wrongActive = inventory(); wrongActive["active_target_id"] = "other"
        rejects("inventory active identity") { _ = try NativeBrowserViewWire.targets(wrongActive) }
    }
    @MainActor private static func transportAndConsent() async {
        BrowserViewTestProtocol.reset(ordinary)
        let model = NativeBrowserViewModel(session: session(), now: { instant }, automaticPolling: false)
        await model.configure(baseURL: URL(string: "http://127.0.0.1:49101")!)
        expect(BrowserViewTestProtocol.requests.count == 1 && BrowserViewTestProtocol.requests[0].httpMethod == "GET", "passive inventory does not initialize or start a browser")
        expect(model.canStart && model.frame == nil && !model.watching, "inventory does not admit media")
        await model.start()
        let start = BrowserViewTestProtocol.requests.last!
        expect(start.url!.path == "/api/browser/view/start" && Set(body(start).keys) == ["target_id", "consent"], "exact start body")
        expect(NativeBrowserViewWire.bool(body(start)["consent"]) == true && body(start)["target_id"] as? String == target, "reviewed consent bound to attached target")
        expect(model.watching && model.frame == nil && BrowserViewTestProtocol.requests.count == 2, "confirmed grant is not a fabricated frame")
        await model.pollFrame()
        expect(model.frame?.sequence == 1 && model.frame?.image.isValid == true, "transport renders actual JPEG")
        let frameRequest = BrowserViewTestProtocol.requests.last!
        expect(Set(body(frameRequest).keys) == ["view_id", "token"] && body(frameRequest)["view_id"] as? String == viewID, "exact frame credentials")
        await model.stop()
        expect(!model.watching && model.frame == nil, "stop clears media")
        expect(BrowserViewTestProtocol.requests.allSatisfy { $0.value(forHTTPHeaderField: "X-FERAL-Browser-View") == "native-v1" }, "operator header on every route")
        expect(BrowserViewTestProtocol.requests.last?.url?.path == "/api/browser/view/stop", "stop revokes its grant")
        expect(BrowserViewTestProtocol.requests.allSatisfy { $0.url!.path.hasPrefix("/api/browser/view/") }, "viewer performs no browser/task actions")
        BrowserViewTestProtocol.reset(ordinary)
        let remote = NativeBrowserViewModel(session: session(), now: { instant }, automaticPolling: false)
        await remote.configure(baseURL: URL(string: "http://example.com:9090")!)
        expect(BrowserViewTestProtocol.requests.isEmpty && !remote.canStart && remote.frame == nil, "remote origin is refused before dispatch")
    }
    @MainActor private static func refusalAndExpiry() async {
        BrowserViewTestProtocol.reset { instance, request in
            if request.url!.path.hasSuffix("/frame") { instance.reply(["success": false, "error_code": "view_target_changed", "error": "private page URL"], status: 409) }
            else { ordinary(instance, request) }
        }
        let model = NativeBrowserViewModel(session: session(), now: { instant }, automaticPolling: false)
        await model.configure(baseURL: URL(string: "http://127.0.0.1:49102")!); await model.start(); await model.pollFrame()
        expect(!model.watching && model.frame == nil && model.error?.contains("page changed") == true, "typed refusal clears media and explains target race")
        expect(model.error?.contains("private page URL") == false, "raw backend details are not exposed")
        expect(BrowserViewTestProtocol.requests.last?.url?.path.hasSuffix("/stop") == true, "refusal revokes grant")
        var clock = instant
        BrowserViewTestProtocol.reset(ordinary)
        let expired = NativeBrowserViewModel(session: session(), now: { clock }, automaticPolling: false)
        await expired.configure(baseURL: URL(string: "http://127.0.0.1:49103")!); await expired.start()
        clock = instant.addingTimeInterval(300); await expired.pollFrame()
        expect(!expired.watching && expired.frame == nil && expired.error?.contains("expired") == true, "expiry retires media")
        expect(!BrowserViewTestProtocol.requests.contains { $0.url!.path.hasSuffix("/frame") }, "expired grant is not sent for another frame")
    }
    @MainActor private static func duplicateAndForeignFrames() async {
        BrowserViewTestProtocol.reset(ordinary)
        let model = NativeBrowserViewModel(session: session(), now: { instant }, automaticPolling: false)
        await model.configure(baseURL: URL(string: "http://127.0.0.1:49104")!); await model.start(); await model.pollFrame(); await model.pollFrame()
        expect(model.frame == nil && !model.watching, "duplicate sequence clears previously displayed real image")
        BrowserViewTestProtocol.reset { instance, request in
            if request.url!.path.hasSuffix("/frame") { var other = frame(); other["target_id"] = "foreign"; instance.reply(other) }
            else { ordinary(instance, request) }
        }
        let foreign = NativeBrowserViewModel(session: session(), now: { instant }, automaticPolling: false)
        await foreign.configure(baseURL: URL(string: "http://127.0.0.1:49105")!); await foreign.start(); await foreign.pollFrame()
        expect(foreign.frame == nil && !foreign.watching, "foreign target content is never admitted")
    }
    @MainActor private static func delayedFrameAndNonoverlap() async {
        let held = BrowserViewHeldResponse()
        BrowserViewTestProtocol.reset { instance, request in
            if request.url!.path.hasSuffix("/frame") { held.hold(instance) } else { ordinary(instance, request) }
        }
        let model = NativeBrowserViewModel(session: session(), now: { instant }, automaticPolling: false)
        await model.configure(baseURL: URL(string: "http://127.0.0.1:49106")!); await model.start()
        let pending = Task { await model.pollFrame() }; await wait { held.ready }
        await model.pollFrame()
        expect(BrowserViewTestProtocol.requests.filter { $0.url!.path.hasSuffix("/frame") }.count == 1, "only one frame request can be outstanding")
        await model.stop(); held.release(frame()); await pending.value
        expect(model.frame == nil && !model.watching, "response arriving after Stop cannot restore media")
        expect(BrowserViewTestProtocol.requests.filter { $0.url!.path.hasSuffix("/stop") }.count == 1, "single cleanup for admitted grant")
    }
    @MainActor private static func cleanupAndOriginRace() async {
        let held = BrowserViewHeldResponse()
        BrowserViewTestProtocol.reset { instance, request in
            if request.url!.path.hasSuffix("/stop") { held.hold(instance) } else { ordinary(instance, request) }
        }
        let model = NativeBrowserViewModel(session: session(), now: { instant }, automaticPolling: false)
        await model.configure(baseURL: URL(string: "http://127.0.0.1:49107")!); await model.start(); await model.pollFrame()
        let switching = Task { await model.configure(baseURL: URL(string: "http://127.0.0.1:49108")!) }; await wait { held.ready }
        expect(model.frame == nil && !model.watching, "local media clears before remote Stop acknowledgement")
        await model.configure(baseURL: URL(string: "http://127.0.0.1:49109")!)
        held.release(stopReply()); await switching.value
        expect(BrowserViewTestProtocol.requests.last?.url?.port == 49109, "late cleanup cannot overwrite a newer configured origin")
    }
    @MainActor private static func staleGrantAndRuntimeFence() async {
        let held = BrowserViewHeldResponse()
        BrowserViewTestProtocol.reset { instance, request in
            if request.url!.path.hasSuffix("/start") { held.hold(instance) } else { ordinary(instance, request) }
        }
        let model = NativeBrowserViewModel(session: session(), now: { instant }, automaticPolling: false)
        await model.configure(baseURL: URL(string: "http://127.0.0.1:49110")!)
        let starting = Task { await model.start() }; await wait { held.ready }; await model.stop()
        held.release(grant()); await starting.value
        expect(!model.watching && model.frame == nil, "late start grant cannot enable retired viewer")
        expect(BrowserViewTestProtocol.requests.last?.url?.path.hasSuffix("/stop") == true, "late valid grant is explicitly revoked")
        let frameHeld = BrowserViewHeldResponse()
        BrowserViewTestProtocol.reset { instance, request in
            if request.url!.path.hasSuffix("/frame") { frameHeld.hold(instance) } else { ordinary(instance, request) }
        }
        let origin = URL(string: "http://127.0.0.1:49111")!
        let fenced = NativeBrowserViewModel(session: session(), now: { instant }, automaticPolling: false)
        await fenced.configure(baseURL: origin); await fenced.start()
        let polling = Task { await fenced.pollFrame() }; await wait { frameHeld.ready }
        try! NativeLocalActionGate.shared.pause(origin: origin); frameHeld.release(frame()); await polling.value
        expect(!fenced.watching && fenced.frame == nil, "runtime replacement fence refuses in-flight media")
        expect(BrowserViewTestProtocol.requests.filter { $0.url!.path.hasSuffix("/stop") }.isEmpty, "old runtime cannot receive another cleanup request")
    }
    @MainActor private static func automaticPollingStopsCleanly() async {
        let held = BrowserViewHeldResponse()
        BrowserViewTestProtocol.reset { instance, request in
            if request.url!.path.hasSuffix("/frame") { instance.reply(["success": false, "error_code": "view_consent_required", "error": "hidden"], status: 403) }
            else if request.url!.path.hasSuffix("/stop") { held.hold(instance) }
            else { ordinary(instance, request) }
        }
        let model = NativeBrowserViewModel(session: session(), now: { instant })
        await model.configure(baseURL: URL(string: "http://127.0.0.1:49112")!); await model.start(); await wait { held.ready }
        expect(!model.watching && model.frame == nil, "automatic poll failure immediately retires media")
        held.release(stopReply()); await wait { model.error?.contains("not permitted") == true }
        expect(BrowserViewTestProtocol.requests.filter { $0.url!.path.hasSuffix("/stop") }.count == 1, "self-cancelled polling still revokes on independent cleanup task")
    }
    @MainActor private static func expiryWhileFramePending() async {
        let held = BrowserViewHeldResponse()
        BrowserViewTestProtocol.reset { instance, request in
            if request.url!.path.hasSuffix("/start") {
                var short = grant(); short["expires_at"] = Date().timeIntervalSince1970 + 0.1; instance.reply(short)
            } else if request.url!.path.hasSuffix("/frame") { held.hold(instance) }
            else { ordinary(instance, request) }
        }
        let model = NativeBrowserViewModel(session: session(), automaticPolling: false)
        await model.configure(baseURL: URL(string: "http://127.0.0.1:49113")!); await model.start()
        let pending = Task { await model.pollFrame() }; await wait { held.ready }; await wait { !model.watching }
        expect(model.frame == nil, "expiry timer retires media while frame transport is still pending")
        held.release(frame()); await pending.value
        expect(model.frame == nil && !model.watching, "post-expiry response cannot re-enable expired lease")
        expect(BrowserViewTestProtocol.requests.contains { $0.url!.path.hasSuffix("/stop") }, "expiry independently revokes grant")
    }
    @MainActor private static func automaticPollingPace() async {
        let held = BrowserViewHeldResponse(), lock = NSLock()
        var arrivals: [Date] = []
        BrowserViewTestProtocol.reset { instance, request in
            if request.url!.path.hasSuffix("/frame") {
                lock.lock(); arrivals.append(Date()); let count = arrivals.count; lock.unlock()
                if count == 1 { instance.reply(frame()) } else { held.hold(instance) }
            } else { ordinary(instance, request) }
        }
        let model = NativeBrowserViewModel(session: session(), now: { instant })
        await model.configure(baseURL: URL(string: "http://127.0.0.1:49114")!); await model.start(); await wait { held.ready }
        let times: [Date] = lock.withLock { arrivals }
        expect(times.count == 2 && times[1].timeIntervalSince(times[0]) >= 0.49, "automatic transport is sequential and bounded to two frames per second")
        await model.pollFrame()
        expect(BrowserViewTestProtocol.requests.filter { $0.url!.path.hasSuffix("/frame") }.count == 2, "manual refresh cannot overlap automatic in-flight frame")
        await model.stop()
        expect(model.frame == nil && !model.watching, "Stop clears automatic frame immediately")
    }
    @MainActor static func main() async throws {
        try wireValidation(); await transportAndConsent(); await refusalAndExpiry(); await duplicateAndForeignFrames()
        await delayedFrameAndNonoverlap(); await cleanupAndOriginRace(); await staleGrantAndRuntimeFence(); await automaticPollingStopsCleanly()
        await expiryWhileFramePending(); await automaticPollingPace()
        print("NativeBrowserViewFeatureTests: \(checks) checks passed")
    }
}
