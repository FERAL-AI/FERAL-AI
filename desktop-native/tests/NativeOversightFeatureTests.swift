// Compile separately with NativeOversightFeature.swift; excluded from app.
// Fixtures verify native state and HTTP contracts, not real action execution.
import Foundation

private final class OversightWire: URLProtocol {
    static let lock = NSLock()
    static var responses: [String: (Int, [String: Any])] = [:]
    static var calls: [(URL, [String: Any])] = []
    static var heldPaths = Set<String>()
    static var held: [() -> Void] = []
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        var bytes = request.httpBody ?? Data()
        if bytes.isEmpty, let stream = request.httpBodyStream {
            stream.open(); defer { stream.close() }
            var buffer = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable { let count = stream.read(&buffer, maxLength: buffer.count); if count <= 0 { break }; bytes.append(buffer, count: count) }
        }
        let body = (try? JSONSerialization.jsonObject(with: bytes)) as? [String: Any] ?? [:]
        Self.lock.lock()
        let path = request.url!.path
        Self.calls.append((request.url!, body))
        let reply = Self.responses[path] ?? (200, [:])
        let respond = { [self] in
            let data = try! JSONSerialization.data(withJSONObject: reply.1)
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: reply.0, httpVersion: nil, headerFields: ["Content-Type": "application/json"])!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data); client?.urlProtocolDidFinishLoading(self)
        }
        let shouldHold = Self.heldPaths.contains(path)
        if shouldHold { Self.held.append(respond) }
        Self.lock.unlock()
        if !shouldHold { respond() }
    }
    override func stopLoading() {}
    static func reset(_ responses: [String: (Int, [String: Any])]) {
        lock.lock(); defer { lock.unlock() }; Self.responses = responses; calls = []; heldPaths = []; held = []
    }
    static func set(_ path: String, _ status: Int, _ value: [String: Any]) { lock.lock(); defer { lock.unlock() }; responses[path] = (status, value) }
    static func set(_ path: String, _ value: [String: Any]) { set(path, 200, value) }
    static func count(_ path: String? = nil) -> Int { lock.lock(); defer { lock.unlock() }; return calls.filter { path == nil || $0.0.path == path }.count }
    static func body(_ path: String) -> [String: Any] { lock.lock(); defer { lock.unlock() }; return calls.last(where: { $0.0.path == path })?.1 ?? [:] }
    static func hold(_ path: String) { lock.lock(); defer { lock.unlock() }; heldPaths.insert(path) }
    static func release() { lock.lock(); let callbacks = held; held = []; heldPaths = []; lock.unlock(); callbacks.forEach { $0() } }
}

private struct OversightAssertion: Error { let message: String }
private func require(_ condition: @autoclosure () -> Bool, _ message: String) throws { if !condition() { throw OversightAssertion(message: message) } }

@main struct NativeOversightTests {
    static let base = URL(string: "http://127.0.0.1:9465")!
    static let approval: [String: Any] = ["request_id": "req-real", "session_id": "phone-device", "tool_name": "filesystem__write", "args": ["path": "/tmp/test.txt", "content": "proposed"], "safety_level": "high", "created_at": 1790831000, "status": "pending", "policy_sources": ["manifest": ["requires_user_approval": true]]]
    static var fixtures: [String: (Int, [String: Any])] {
        ["/api/approvals": (200, ["approvals": [approval], "count": 1]),
         "/api/supervisor/stats": (200, ["paused": false]),
         "/api/supervisor/events": (200, ["events": [["event_id": "event-real", "ts": 1790831000, "source": "node", "actor": "user", "decision": "queued", "kind": "tool_call", "session_id": "phone-device", "payload_summary": "held tool", "detail": ["request_id": "req-real"]]]])]
    }
    @MainActor static func model(_ url: URL? = base) -> NativeOversightModel {
        let configuration = URLSessionConfiguration.ephemeral; configuration.protocolClasses = [OversightWire.self]
        return NativeOversightModel(baseURL: url, session: URLSession(configuration: configuration))
    }
    @MainActor static func waitForCall(_ path: String) async throws {
        for _ in 0..<100 {
            if OversightWire.count(path) > 0 { return }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        throw OversightAssertion(message: "Expected held request: " + path)
    }
    @MainActor static func main() async {
        do {
            OversightWire.reset(fixtures)
            let unavailable = model(nil); await unavailable.refresh()
            try require(OversightWire.count() == 0 && unavailable.paused == nil && !unavailable.queueFresh, "nil connection must not make requests or claim empty queue")
            print("PASS unavailable local connection makes zero requests")

            OversightWire.reset(fixtures)
            let live = model(); await live.refresh()
            try require(live.approvals.count == 1 && live.queueFresh && live.paused == false && live.events.count == 1, "real approval/audit fixture must load")
            try require(live.approvals[0].arguments.contains("proposed") && live.approvals[0].policy.contains("requires_user_approval"), "actual action and policy must be reviewable")
            OversightWire.set("/api/supervisor/pause", ["paused": false])
            await live.setPaused(true)
            try require(live.paused == false && live.receipt?.contains("resumed") == true, "pause follows actual server response, not optimistic requested state")
            try require(OversightWire.body("/api/supervisor/pause")["paused"] as? Bool == true, "pause POST must use JSON boolean")
            print("PASS pending queue, audit, and authoritative pause result")

            OversightWire.reset(fixtures)
            let unknown = model(); await unknown.refresh(); let heldReview = unknown.approvals[0]
            OversightWire.set("/api/supervisor/stats", ["paused": 1]); await unknown.refresh()
            await unknown.decide(heldReview, approve: true)
            try require(unknown.paused == nil && OversightWire.count("/api/approvals/req-real/approve") == 0, "numeric pause state must be rejected and approval blocked")
            print("PASS unknown supervisor state cannot authorize tool")

            OversightWire.reset(fixtures)
            let stale = model(); await stale.refresh(); let staleReview = stale.approvals[0]
            OversightWire.set("/api/approvals", 503, ["detail": "ToolRunner unavailable"]); await stale.refresh()
            await stale.decide(staleReview, approve: false)
            try require(!stale.queueFresh && stale.queueError != nil && stale.approvals.count == 1 && OversightWire.count("/api/approvals/req-real/reject") == 0, "queue read failure retains last known data but disables decisions")
            print("PASS stale queue disables decisions without fake empty state")

            OversightWire.reset(fixtures)
            let execute = model(); await execute.refresh(); let reviewed = execute.approvals[0]
            OversightWire.set("/api/approvals", ["approvals": []])
            OversightWire.set("/api/approvals/req-real/approve", ["success": true, "status": "approved", "request_id": "req-real", "session_id": "phone-device", "result": ["success": false, "error": "write failed"]])
            await execute.decide(reviewed, approve: true)
            try require(OversightWire.body("/api/approvals/req-real/approve")["session_id"] as? String == "phone-device", "approval binds real session ID")
            try require(execute.receipt?.contains("reported a failure") == true && execute.receipt?.contains("write failed") == true && execute.approvals.isEmpty, "approval confirmation must not claim successful execution")
            print("PASS approved session scope and failed tool result receipt")

            OversightWire.reset(fixtures)
            let deny = model(); await deny.refresh(); let denyReview = deny.approvals[0]
            OversightWire.set("/api/approvals", ["approvals": []])
            OversightWire.set("/api/approvals/req-real/reject", ["success": true, "status": "rejected", "request_id": "req-real", "session_id": "phone-device"])
            await deny.decide(denyReview, approve: false)
            try require(deny.receipt?.contains("Denied request") == true && OversightWire.body("/api/approvals/req-real/reject")["session_id"] as? String == "phone-device", "deny must use actual supported route and scoped ID")
            print("PASS explicit denial contract")

            OversightWire.reset(fixtures)
            let mismatch = model(); await mismatch.refresh(); let mismatchReview = mismatch.approvals[0]
            OversightWire.set("/api/approvals/req-real/approve", ["success": true, "status": "approved", "request_id": "wrong-id", "session_id": "phone-device"])
            await mismatch.decide(mismatchReview, approve: true)
            try require(mismatch.receipt == nil && mismatch.decisionError != nil, "mismatched receipt cannot confirm decision")
            print("PASS unsupported or mismatched action receipt is not success")

            OversightWire.reset(fixtures); OversightWire.hold("/api/approvals")
            let reconnect = model()
            let oldRefresh = Task { await reconnect.refresh() }
            try await waitForCall("/api/approvals")
            reconnect.configure(baseURL: nil); OversightWire.release(); await oldRefresh.value
            try require(reconnect.approvals.isEmpty && reconnect.events.isEmpty && reconnect.paused == nil && reconnect.queueError == nil && !reconnect.queueFresh, "late old-backend read must not overwrite cleared state")
            try require(OversightWire.count() == 1, "old refresh must not issue subsequent requests after reconfigure")
            print("PASS delayed old-backend queue response is discarded")

            OversightWire.reset(fixtures)
            let previous = model(); await previous.refresh(); let previousReview = previous.approvals[0]
            previous.configure(baseURL: nil); previous.configure(baseURL: base); await previous.refresh()
            await previous.decide(previousReview, approve: true)
            try require(OversightWire.count("/api/approvals/req-real/approve") == 0, "same ID on new backend cannot authorize old review")
            print("PASS review identity binds to backend generation")

            OversightWire.reset(fixtures)
            let lateAction = model(); await lateAction.refresh(); let actionReview = lateAction.approvals[0]
            OversightWire.set("/api/approvals/req-real/approve", ["success": true, "status": "approved", "request_id": "req-real", "session_id": "phone-device"])
            OversightWire.hold("/api/approvals/req-real/approve")
            let oldAction = Task { await lateAction.decide(actionReview, approve: true) }
            try await waitForCall("/api/approvals/req-real/approve")
            let count = OversightWire.count(); lateAction.configure(baseURL: nil); OversightWire.release(); await oldAction.value
            try require(lateAction.receipt == nil && lateAction.decisionError == nil && lateAction.approvals.isEmpty && !lateAction.acting, "late action receipt must not mutate new backend state")
            try require(OversightWire.count() == count, "old action must not refresh new backend")
            print("PASS delayed old-backend action receipt is discarded")
            print("NATIVE_OVERSIGHT_TESTS_PASSED: 10 fixture groups; mocked HTTP only, no real approvals executed")
        } catch { fputs("NATIVE_OVERSIGHT_TESTS_FAILED: \(error)\n", stderr); exit(1) }
    }
}
