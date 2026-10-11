import Foundation

private final class LocalGateFixture: URLProtocol {
    private static let lock = NSLock()
    private static var requests: [URLRequest] = []
    private static var heldPath: String?
    private static var held: LocalGateFixture?
    private static var registered = false
    static func reset() { lock.lock(); defer { lock.unlock() }; requests = []; heldPath = nil; held = nil; registered = false }
    static func hold(_ path: String) { lock.lock(); defer { lock.unlock() }; heldPath = path }
    static var hasHeld: Bool { lock.lock(); defer { lock.unlock() }; return held != nil }
    static var postCount: Int { lock.lock(); defer { lock.unlock() }; return requests.filter { $0.httpMethod == "POST" }.count }
    static var requestCount: Int { lock.lock(); defer { lock.unlock() }; return requests.count }
    static func release() { lock.lock(); let pending = held; held = nil; lock.unlock(); pending?.deliver() }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.lock.lock(); Self.requests.append(request)
        if request.url?.path == Self.heldPath { Self.heldPath = nil; Self.held = self; Self.lock.unlock(); return }
        Self.lock.unlock(); deliver()
    }
    private func deliver() {
        Self.lock.lock()
        let value: [String: Any]
        let manifest: [String: Any] = ["agent_id": "gate-fixture", "name": "Fixture", "system_prompt": "Synthetic local fixture", "tool_permissions": []]
        switch request.url?.path {
        case "/api/agents/list": value = ["agents": Self.registered ? [["agent_id": "gate-fixture", "name": "Fixture", "tools": [], "schedule": NSNull(), "tasks": 0, "satisfaction": 0.5]] : []]
        case "/api/agents/personas": value = ["personas": []]
        case "/api/agents/proposals": value = ["proposals": []]
        case "/api/agents/stats": value = ["patterns_tracked": 0, "proposals_ready": 0, "specialists_active": 0, "total_tasks": 0]
        case "/api/agents/spawn": Self.registered = true; value = ["success": true, "source": "persona_manifest", "agent": manifest]
        default: value = ["ok": true]
        }
        Self.lock.unlock()
        let data = try! JSONSerialization.data(withJSONObject: value)
        let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data); client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {
        Self.lock.lock(); defer { Self.lock.unlock() }
        if Self.held === self { Self.held = nil }
    }
}

@main struct NativeLocalActionGateTests {
    private static var count = 0
    @MainActor private static func check(_ value: Bool, _ label: String) {
        guard value else { fatalError("FAIL: " + label) }; count += 1
    }
    @MainActor private static func refuses(_ action: () async throws -> Void) async {
        do { try await action(); fatalError("Expected an admission refusal") }
        catch is NativeLocalActionGateFailure { count += 1 }
        catch { fatalError("Unexpected error: \(error)") }
    }
    private static func session() -> URLSession {
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [LocalGateFixture.self]
        return URLSession(configuration: config)
    }
    @MainActor private static func waitForHold() async throws {
        for _ in 0..<400 {
            if LocalGateFixture.hasHeld { return }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        fatalError("Bounded fixture read was never held")
    }
    @MainActor private static func retainedAgent(port: Int, reopenBeforeRelease: Bool) async throws {
        LocalGateFixture.reset()
        let base = URL(string: "http://127.0.0.1:\(port)")!, oldSession = session()
        defer { oldSession.invalidateAndCancel() }
        let model = NativeAgentModel(session: oldSession)
        await model.configure(baseURL: base)
        let review = try model.review(.custom(["agent_id": "gate-fixture", "name": "Fixture", "system_prompt": "Synthetic local fixture", "tool_permissions": []]))
        LocalGateFixture.hold("/api/agents/list")
        // The actual model remains retained, as a detached button task does
        // after leaving its view. No configure(nil) or generation reset occurs.
        let operation = Task { await model.perform(review) }
        try await waitForHold()
        try NativeLocalActionGate.shared.pause(origin: base)
        if reopenBeforeRelease { try NativeLocalActionGate.shared.activate(origin: base) }
        LocalGateFixture.release()
        let result = await operation.value
        print("Retained Agent read: replacementBeforeRelease=\(reopenBeforeRelease), completed=\(result), observedPOSTs=\(LocalGateFixture.postCount)")
        check(!result && LocalGateFixture.postCount == 0, "retained Agent.perform cannot turn a held read into spawn after pause/replacement")
        check(model.actionError != nil, "actual retained model reports refusal")
        if !reopenBeforeRelease { try NativeLocalActionGate.shared.activate(origin: base) }
        var post = URLRequest(url: base.appendingPathComponent("/late-post")); post.httpMethod = "POST"
        await refuses { _ = try await oldSession.feralLocalData(for: post) }
        check(LocalGateFixture.postCount == 0, "same old session cannot silently acquire replacement epoch")
        let freshSession = session(); defer { freshSession.invalidateAndCancel() }
        let fresh = NativeAgentModel(session: freshSession)
        await fresh.configure(baseURL: base)
        let freshReview = try fresh.review(.custom(["agent_id": "gate-fixture", "name": "Fixture", "system_prompt": "Synthetic local fixture", "tool_permissions": []]))
        check(await fresh.perform(freshReview), "fresh session on explicitly activated origin completes actual model registration/readback")
        check(LocalGateFixture.postCount == 1, "only fresh reviewed registration dispatches")
    }
    @MainActor private static func edgeCases() async throws {
        LocalGateFixture.reset()
        let client = session(); defer { client.invalidateAndCancel() }
        let base = URL(string: "http://localhost:47983")!
        try NativeLocalActionGate.shared.pause(origin: base)
        let read = URLRequest(url: URL(string: "http://LOCALHOST:47983/path?query=ignored")!)
        await refuses { _ = try await client.feralLocalData(for: read) }
        check(LocalGateFixture.requestCount == 0, "canonical host/path/query cannot bypass paused origin")
        try NativeLocalActionGate.shared.activate(origin: base)
        await refuses { _ = try await client.feralLocalData(for: read) }
        check(LocalGateFixture.requestCount == 0, "client first attempted while paused is also stale after activation")
        let fresh = session(); defer { fresh.invalidateAndCancel() }
        _ = try await fresh.feralLocalData(for: read)
        check(LocalGateFixture.requestCount == 1, "fresh client captures activated epoch")
        let cancelled = Task { try Task.checkCancellation(); _ = try await fresh.feralLocalData(for: read) }
        cancelled.cancel()
        do { try await cancelled.value; fatalError("Cancelled request was admitted") } catch is CancellationError { count += 1 }
        check(LocalGateFixture.requestCount == 1, "cancelled task dispatches nothing")
        let external = URLRequest(url: URL(string: "https://example.invalid/fixture")!)
        _ = try await fresh.feralLocalData(for: external)
        check(LocalGateFixture.requestCount == 2, "unmanaged nonlocal transport behavior is preserved (mock only)")

        let writing = session(); defer { writing.invalidateAndCancel() }
        let writingOrigin = URL(string: "http://127.0.0.1:47987")!
        var dispatched = URLRequest(url: writingOrigin.appendingPathComponent("/held-write")); dispatched.httpMethod = "POST"
        LocalGateFixture.hold("/held-write")
        let write = Task { try await writing.feralLocalData(for: dispatched) }
        try await waitForHold()
        let postsBeforePause = LocalGateFixture.postCount
        try NativeLocalActionGate.shared.pause(origin: writingOrigin)
        LocalGateFixture.release()
        await refuses { _ = try await write.value }
        check(LocalGateFixture.postCount == postsBeforePause, "late write receipt is refused without replaying the already dispatched effect")
        try NativeLocalActionGate.shared.activate(origin: writingOrigin)
        await refuses { _ = try await writing.feralLocalData(for: dispatched) }
        check(LocalGateFixture.postCount == postsBeforePause, "unknown write cannot be automatically replayed by the old session")

        let gate = NativeLocalActionGate(maximumOrigins: 1, maximumSessions: 2, maximumSessionOrigins: 1)
        let a = URL(string: "http://127.0.0.1:47984")!, b = URL(string: "http://127.0.0.1:47985")!
        let bound = try gate.capture(session: client, url: a)
        try gate.pause(origin: a)
        do { try gate.validate(bound); fatalError("Old captured lease survived pause") } catch is NativeLocalActionGateFailure { count += 1 }
        do { try gate.activate(origin: b); fatalError("Managed origin capacity accepted") } catch is NativeLocalActionGateFailure { count += 1 }
        do { _ = try gate.capture(session: fresh, url: b); fatalError("Exhausted gate admitted unmanaged local origin") } catch is NativeLocalActionGateFailure { count += 1 }
        let bounded = NativeLocalActionGate(maximumSessions: 1)
        var transient: URLSession? = session()
        _ = try bounded.capture(session: transient!, url: a)
        do { _ = try bounded.capture(session: client, url: a); fatalError("Live session capacity accepted") } catch is NativeLocalActionGateFailure { count += 1 }
        transient?.invalidateAndCancel(); weak var departed = transient; transient = nil
        for _ in 0..<400 { if departed == nil { break }; try await Task.sleep(nanoseconds: 5_000_000) }
        check(departed == nil, "gate leases do not retain URLSession")
        departed = nil
        _ = try bounded.capture(session: client, url: a)
        check(true, "dead weak key releases capacity for a distinct session")
        let originLimit = NativeLocalActionGate(maximumSessionOrigins: 1)
        _ = try originLimit.capture(session: client, url: a)
        do { _ = try originLimit.capture(session: client, url: b); fatalError("Session origin capacity accepted") } catch is NativeLocalActionGateFailure { count += 1 }
        let ipv6 = NativeLocalActionGate()
        let v6 = URL(string: "http://[::1]:47986/a")!
        try ipv6.pause(origin: v6)
        do { _ = try ipv6.capture(session: client, url: URL(string: "http://[::1]:47986/b")!); fatalError("IPv6 pause bypassed") } catch is NativeLocalActionGateFailure { count += 1 }
        let defaultPort = NativeLocalActionGate()
        try defaultPort.pause(origin: URL(string: "http://localhost")!)
        do { _ = try defaultPort.capture(session: client, url: URL(string: "http://localhost:80/a")!); fatalError("Explicit default port bypassed pause") } catch is NativeLocalActionGateFailure { count += 1 }
        do { try defaultPort.activate(origin: URL(string: "https://example.invalid")!); fatalError("External runtime management accepted") } catch is NativeLocalActionGateFailure { count += 1 }
    }
    @MainActor static func main() async throws {
        try await retainedAgent(port: 47981, reopenBeforeRelease: false)
        try await retainedAgent(port: 47982, reopenBeforeRelease: true)
        try await edgeCases()
        try await passiveSuspension()
        print("NativeLocalActionGate: \(count) assertions passed; actual retained Agent model/mock HTTP, no real agent effects")
    }
    @MainActor private static func passiveSuspension() async throws {
        LocalGateFixture.reset()
        let base = URL(string: "http://127.0.0.1:47988")!, retained = session()
        defer { retained.invalidateAndCancel() }
        var write = URLRequest(url: base.appendingPathComponent("/held-passive-write")); write.httpMethod = "POST"
        LocalGateFixture.hold("/held-passive-write")
        let inFlight = Task { try await retained.feralLocalData(for: write) }
        try await waitForHold()
        try NativeLocalActionGate.shared.suspend(origin: base)
        let requests = LocalGateFixture.requestCount
        await refuses { _ = try await retained.feralLocalData(for: write) }
        check(LocalGateFixture.requestCount == requests, "suspension refuses new retained-client dispatch")
        try NativeLocalActionGate.shared.resume(origin: base)
        LocalGateFixture.release()
        await refuses { _ = try await inFlight.value }
        check(LocalGateFixture.postCount == 1, "receipt crossing suspension is refused without retrying the effect")
        _ = try await retained.feralLocalData(for: URLRequest(url: base.appendingPathComponent("/fresh-read")))
        check(LocalGateFixture.requestCount == requests + 1, "matching host resume permits fresh capture on same retained client")
        try NativeLocalActionGate.shared.pause(origin: base)
        await refuses { try NativeLocalActionGate.shared.resume(origin: base) }
        try NativeLocalActionGate.shared.activate(origin: base)
        await refuses { _ = try await retained.feralLocalData(for: URLRequest(url: base)) }
        check(LocalGateFixture.postCount == 1, "passive resume cannot reactivate a retired epoch or replay an old review")
        let bounded = NativeLocalActionGate(maximumOrigins: 1)
        let other = URL(string: "http://127.0.0.1:47989")!
        try bounded.activate(origin: base)
        await refuses { try bounded.suspend(origin: other) }
        await refuses { _ = try bounded.capture(session: retained, url: other) }
        await refuses { _ = try bounded.capture(session: retained, url: base) }
        check(LocalGateFixture.postCount == 1, "suspension capacity exhaustion fails closed on all origins without dispatch")
    }
}
