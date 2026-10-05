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

            for invalidSession in ["", " ", " caller", "caller ", "caller\u{0000}", String(repeating: "x", count: 1025)] {
                var unbound = approval; unbound["session_id"] = invalidSession
                var invalidFixtures = fixtures
                invalidFixtures["/api/approvals"] = (200, ["approvals": [unbound], "count": 1])
                OversightWire.reset(invalidFixtures)
                let invalid = model(); await invalid.refresh()
                try require(invalid.queueFresh && invalid.approvals.count == 1, "legacy invalid-session rows must remain visible")
                let request = invalid.approvals[0]
                try require(request.sessionIssue?.contains("original caller") == true, "invalid-session row must explain exact-session resubmission without replay")
                await invalid.decide(request, approve: true)
                await invalid.decide(request, approve: false)
                try require(OversightWire.count("/api/approvals/req-real/approve") == 0 && OversightWire.count("/api/approvals/req-real/reject") == 0, "invalid-session decisions must not dispatch or manufacture an identity")
                try require(invalid.decisionError != nil && invalid.receipt == nil && !invalid.acting, "invalid session cannot claim a decision receipt")
            }
            print("PASS six invalid-session rows remain visible and produce zero decision dispatches")

            var unicodeApproval = approval; unicodeApproval["session_id"] = String(repeating: "🦦", count: 1024)
            var unicodeFixtures = fixtures
            unicodeFixtures["/api/approvals"] = (200, ["approvals": [unicodeApproval], "count": 1])
            OversightWire.reset(unicodeFixtures)
            let unicode = model(); await unicode.refresh()
            try require(unicode.approvals[0].sessionIssue == nil, "canonical Unicode sessions must use backend scalar-length limits, not bytes")
            print("PASS canonical Unicode session remains decision-capable")

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

            let resource: [String: Any] = ["connection_id": "a2345678-1234-4234-9234-123456789abc", "target_id": "tab-A", "owner_session_id": "phone-device"]
            var browserApproval = approval; browserApproval["tool_name"] = "browser__click"; browserApproval["browser_resource"] = resource
            var browserFixtures = fixtures; browserFixtures["/api/approvals"] = (200, ["approvals": [browserApproval]])
            OversightWire.reset(browserFixtures)
            let browser = model(); await browser.refresh(); let browserReview = browser.approvals[0]
            try require(browserReview.browserResource?.targetID == "tab-A" && browserReview.approvalActionTitle == "Approve this request" && browserReview.approvalDialogTitle.contains("exact browser"), "bound request has request-only action and dialog titles")
            try require(browserReview.approvalExplanation.contains("grants no ongoing tool permission") && browserReview.approvalExplanation.contains(resource["connection_id"] as! String) && browserReview.approvalExplanation.contains("tab-A"), "bound approval visibly reviews exact connection/tab without promising session grant")
            OversightWire.set("/api/approvals", ["approvals": []])
            OversightWire.set("/api/approvals/req-real/approve", ["success": true, "status": "approved", "request_id": "req-real", "session_id": "phone-device", "result": ["success": false, "error": "synthetic failure"]])
            await browser.decide(browserReview, approve: true)
            try require(browser.receipt?.contains("Approved this request") == true && browser.receipt?.contains("No ongoing tool permission was granted") == true && browser.receipt?.contains("reported a failure") == true, "bound result receipt distinguishes exact approval, no grant, and execution failure")
            try require(Set(OversightWire.body("/api/approvals/req-real/approve").keys) == ["session_id"], "native scope display creates no new client-selected authority")
            print("PASS exact browser request scope, reviewed identity, and failure receipt")

            OversightWire.reset(fixtures)
            let ordinary = model(); await ordinary.refresh(); let ordinaryReview = ordinary.approvals[0]
            try require(ordinaryReview.browserResource == nil && ordinaryReview.approvalActionTitle == "Approve for this session" && ordinaryReview.approvalExplanation.contains("grants the same tool for this session"), "ordinary queue preserves session-grant explanation")
            print("PASS absent browser metadata retains ordinary session scope copy")

            for replacementResource in [["connection_id": "b2345678-1234-4234-9234-123456789abc", "target_id": "tab-A", "owner_session_id": "phone-device"], ["connection_id": "a2345678-1234-4234-9234-123456789abc", "target_id": "tab-B", "owner_session_id": "phone-device"]] {
                OversightWire.reset(browserFixtures)
                let changed = model(); await changed.refresh(); let oldScope = changed.approvals[0]
                var replacement = browserApproval; replacement["browser_resource"] = replacementResource
                OversightWire.set("/api/approvals", ["approvals": [replacement]]); await changed.refresh()
                await changed.decide(oldScope, approve: true)
                try require(changed.queueFresh && changed.decisionError != nil && OversightWire.count("/api/approvals/req-real/approve") == 0, "same request/args cannot reuse review after browser resource changes")
            }
            print("PASS changed connection or target invalidates reviewed approval")

            var malformedResources: [Any] = [NSNull(), [:], ["connection_id": "bad", "target_id": "tab-A", "owner_session_id": "phone-device"]]
            for (key, value): (String, Any) in [("connection_id", "A2345678-1234-4234-9234-123456789ABC"), ("target_id", "tab/A"), ("target_id", "tab-A\n"), ("target_id", String(repeating: "x", count: 129)), ("target_id", 1), ("owner_session_id", "foreign"), ("extra", "not-a-scope-field")] {
                var invalid = resource; invalid[key] = value; malformedResources.append(invalid)
            }
            for metadata in malformedResources {
                OversightWire.reset(fixtures)
                let invalid = model(); await invalid.refresh(); let retained = invalid.approvals[0]
                var malformed = browserApproval; malformed["browser_resource"] = metadata
                OversightWire.set("/api/approvals", ["approvals": [malformed]]); await invalid.refresh()
                await invalid.decide(retained, approve: true)
                try require(!invalid.queueFresh && invalid.queueError != nil && OversightWire.count("/api/approvals/req-real/approve") == 0, "malformed present resource must never become ordinary grant scope")
            }
            print("PASS malformed bound scope disables decisions without granting fallback")

            if CommandLine.arguments.count == 2 {
                let fixtureURL = URL(fileURLWithPath: CommandLine.arguments[1])
                let data = try Data(contentsOf: fixtureURL)
                let actual = try JSONSerialization.jsonObject(with: data) as! [String: Any]
                var actualFixtures = fixtures; actualFixtures["/api/approvals"] = (200, actual)
                OversightWire.reset(actualFixtures)
                let actualModel = model(); await actualModel.refresh()
                try require(actualModel.queueFresh && actualModel.approvals.count == 1 && actualModel.approvals[0].browserResource?.targetID == "tab-A" && actualModel.approvals[0].sessionID == "s-resource" && actualModel.approvals[0].approvalActionTitle == "Approve this request", "real registered REST/ToolRunner pending fixture parses as exact request scope in native")
                print("PASS actual Python registered pending response to native scope parser")
            }

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
            print("NATIVE_OVERSIGHT_TESTS_PASSED: 16 fixture groups; mocked HTTP only, no real approvals executed")
        } catch { fputs("NATIVE_OVERSIGHT_TESTS_FAILED: \(error)\n", stderr); exit(1) }
    }
}
