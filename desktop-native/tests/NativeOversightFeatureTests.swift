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

            let exactScope: [String: Any] = ["contract_version": 1, "kind": "exact_request"]
            let sessionScope: [String: Any] = ["contract_version": 1, "kind": "session"]
            var taskApproval = approval; taskApproval["tool_name"] = "background_task__start"; taskApproval["approval_scope"] = exactScope
            var taskFixtures = fixtures; taskFixtures["/api/approvals"] = (200, ["approvals": [taskApproval]])
            OversightWire.reset(taskFixtures)
            let exactTask = model(); await exactTask.refresh(); let exactTaskReview = exactTask.approvals[0]
            try require(exactTaskReview.browserResource == nil && exactTaskReview.approvalScope.explicitlyProvided && exactTaskReview.approvalActionTitle == "Approve this request" && exactTaskReview.approvalDialogTitle == "Approve this exact request?", "explicit exact task must not display session grant")
            try require(exactTaskReview.approvalExplanation.contains("no ongoing tool permission") && exactTaskReview.approvalExplanation.contains("Each later action"), "exact task creation does not approve later actions")
            OversightWire.set("/api/approvals", ["approvals": []])
            OversightWire.set("/api/approvals/req-real/approve", ["success": true, "status": "approved", "request_id": "req-real", "session_id": "phone-device", "approval_scope": exactScope, "result": ["success": false, "error": "inert job refusal"]])
            await exactTask.decide(exactTaskReview, approve: true)
            try require(exactTask.receipt?.contains("No ongoing tool permission was granted") == true && exactTask.receipt?.contains("reported a failure") == true, "exact task receipt distinguishes scope and actual result")
            try require(Set(OversightWire.body("/api/approvals/req-real/approve").keys) == ["session_id"], "scope description cannot become client authority")
            print("PASS explicit exact task scope, matching echo, no grant and failed result")

            var explicitSessionApproval = approval; explicitSessionApproval["approval_scope"] = sessionScope
            var explicitSessionFixtures = fixtures; explicitSessionFixtures["/api/approvals"] = (200, ["approvals": [explicitSessionApproval]])
            OversightWire.reset(explicitSessionFixtures)
            let explicitSession = model(); await explicitSession.refresh()
            try require(explicitSession.approvals[0].approvalActionTitle == "Approve for this session" && explicitSession.approvals[0].approvalScope.explicitlyProvided, "explicit ordinary session retains historical grant labels")
            print("PASS explicit ordinary session descriptor preserves grant copy")

            OversightWire.reset(taskFixtures)
            let scopeChanged = model(); await scopeChanged.refresh(); let reviewedScope = scopeChanged.approvals[0]
            var replacementScope = taskApproval; replacementScope["approval_scope"] = sessionScope
            OversightWire.set("/api/approvals", ["approvals": [replacementScope]]); await scopeChanged.refresh()
            await scopeChanged.decide(reviewedScope, approve: true)
            try require(scopeChanged.decisionError != nil && OversightWire.count("/api/approvals/req-real/approve") == 0, "changed scope invalidates prior review even with identical request args")
            print("PASS changed approval scope requires fresh review")

            let invalidScopes: [Any] = [NSNull(), [:], ["contract_version": true, "kind": "exact_request"], ["contract_version": 2, "kind": "exact_request"], ["contract_version": "1", "kind": "exact_request"], ["contract_version": 1, "kind": "unknown"], ["contract_version": 1, "kind": 1], ["contract_version": 1, "kind": "exact_request", "origin": "not-public-authority"]]
            for metadata in invalidScopes {
                OversightWire.reset(taskFixtures)
                let invalid = model(); await invalid.refresh(); let retained = invalid.approvals[0]
                var malformed = taskApproval; malformed["approval_scope"] = metadata
                OversightWire.set("/api/approvals", ["approvals": [malformed]]); await invalid.refresh()
                await invalid.decide(retained, approve: true)
                try require(!invalid.queueFresh && invalid.queueError != nil && OversightWire.count("/api/approvals/req-real/approve") == 0, "malformed present scope cannot fall back to session permission")
            }
            var inconsistent = browserApproval; inconsistent["approval_scope"] = sessionScope
            OversightWire.reset(fixtures); let inconsistentModel = model(); await inconsistentModel.refresh()
            OversightWire.set("/api/approvals", ["approvals": [inconsistent]]); await inconsistentModel.refresh()
            try require(!inconsistentModel.queueFresh && inconsistentModel.queueError != nil, "browser binding cannot claim a session grant")
            print("PASS malformed or contradictory present scope disables decisions")

            for echo in [nil, sessionScope] as [[String: Any]?] {
                OversightWire.reset(taskFixtures)
                let unconfirmed = model(); await unconfirmed.refresh(); let request = unconfirmed.approvals[0]
                var response: [String: Any] = ["success": true, "status": "approved", "request_id": "req-real", "session_id": "phone-device"]
                if let echo { response["approval_scope"] = echo }
                OversightWire.set("/api/approvals/req-real/approve", response)
                await unconfirmed.decide(request, approve: true)
                try require(unconfirmed.receipt == nil && unconfirmed.decisionError?.contains("scope") == true, "missing or changed explicit echo cannot confirm reviewed scope")
            }
            print("PASS explicit scope requires matching decision echo")

            var unavailableTask = taskApproval; unavailableTask["approval_available"] = false
            var availableTask = taskApproval; availableTask["request_id"] = "req-live"; availableTask["approval_available"] = true
            var mixedFixtures = taskFixtures; mixedFixtures["/api/approvals"] = (200, ["approvals": [unavailableTask, availableTask]])
            OversightWire.reset(mixedFixtures)
            let mixed = model(); await mixed.refresh(); let unavailableReview = mixed.approvals[0]
            try require(mixed.queueFresh && mixed.approvals.count == 2 && !unavailableReview.approvalAvailable && mixed.approvals[1].approvalAvailable, "unavailable original stays visible without blocking live inbox")
            try require(unavailableReview.approvalExplanation.contains("Deny it or submit a fresh request"), "unavailable original has actionable explanation")
            await mixed.decide(unavailableReview, approve: true)
            try require(OversightWire.count("/api/approvals/req-real/approve") == 0 && mixed.receipt == nil, "unavailable original cannot dispatch approve")
            OversightWire.set("/api/approvals/req-real/reject", ["success": true, "status": "rejected", "request_id": "req-real", "session_id": "phone-device", "approval_scope": exactScope])
            OversightWire.set("/api/approvals", ["approvals": [availableTask]])
            await mixed.decide(unavailableReview, approve: false)
            try require(OversightWire.count("/api/approvals/req-real/reject") == 1 && mixed.receipt?.contains("Denied request") == true && mixed.approvals.count == 1, "valid historical scope can be denied without ongoing permission")
            try require(Set(OversightWire.body("/api/approvals/req-real/reject").keys) == ["session_id"], "availability is never client-selected authority")
            print("PASS unavailable original stays visible, blocks approval and permits scoped denial")

            OversightWire.reset(mixedFixtures)
            let availabilityChanged = model(); await availabilityChanged.refresh(); let oldAvailability = availabilityChanged.approvals[0]
            var nowAvailable = unavailableTask; nowAvailable["approval_available"] = true
            OversightWire.set("/api/approvals", ["approvals": [nowAvailable]]); await availabilityChanged.refresh()
            await availabilityChanged.decide(oldAvailability, approve: false)
            try require(OversightWire.count("/api/approvals/req-real/reject") == 0 && availabilityChanged.decisionError != nil, "changed availability requires refreshed review")
            print("PASS changed availability invalidates earlier review")

            for metadata in [NSNull(), 0, 1, "false", []] as [Any] {
                OversightWire.reset(taskFixtures)
                let invalid = model(); await invalid.refresh(); let retained = invalid.approvals[0]
                var malformed = taskApproval; malformed["approval_available"] = metadata
                OversightWire.set("/api/approvals", ["approvals": [malformed]]); await invalid.refresh()
                await invalid.decide(retained, approve: true)
                await invalid.decide(retained, approve: false)
                try require(!invalid.queueFresh && invalid.queueError != nil && OversightWire.count("/api/approvals/req-real/approve") == 0 && OversightWire.count("/api/approvals/req-real/reject") == 0, "malformed present availability cannot allow either decision")
            }
            print("PASS malformed present availability refuses decisions")

            if CommandLine.arguments.count >= 2 {
                let fixtureURL = URL(fileURLWithPath: CommandLine.arguments[1])
                let data = try Data(contentsOf: fixtureURL)
                let actual = try JSONSerialization.jsonObject(with: data) as! [String: Any]
                var actualFixtures = fixtures; actualFixtures["/api/approvals"] = (200, actual)
                OversightWire.reset(actualFixtures)
                let actualModel = model(); await actualModel.refresh()
                try require(actualModel.queueFresh && actualModel.approvals.count == 1 && actualModel.approvals[0].browserResource?.targetID == "tab-A" && actualModel.approvals[0].sessionID == "s-resource" && actualModel.approvals[0].approvalActionTitle == "Approve this request", "real registered REST/ToolRunner pending fixture parses as exact request scope in native")
                print("PASS actual Python registered pending response to native scope parser")
            }
            if CommandLine.arguments.count >= 3 {
                let data = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[2]))
                let actual = try JSONSerialization.jsonObject(with: data) as! [String: Any]
                var actualFixtures = fixtures; actualFixtures["/api/approvals"] = (200, actual)
                OversightWire.reset(actualFixtures)
                let actualModel = model(); await actualModel.refresh()
                try require(actualModel.queueFresh && actualModel.approvals.count == 1 && actualModel.approvals[0].browserResource == nil && actualModel.approvals[0].approvalScope.kind == .exactRequest && actualModel.approvals[0].approvalScope.explicitlyProvided && actualModel.approvals[0].approvalActionTitle == "Approve this request", "actual persisted TaskFlow API scope parses as exact without browser metadata")
                print("PASS actual Python TaskFlow pending response to native exact scope parser")
            }
            if CommandLine.arguments.count == 4 {
                let data = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[3]))
                let actual = try JSONSerialization.jsonObject(with: data) as! [String: Any]
                var actualFixtures = fixtures; actualFixtures["/api/approvals"] = (200, actual)
                OversightWire.reset(actualFixtures)
                let actualModel = model(); await actualModel.refresh()
                try require(actualModel.queueFresh && actualModel.approvals.count == 2, "actual stale and live tracked requests remain in usable inbox")
                let stale = actualModel.approvals.first(where: { !$0.approvalAvailable })!
                try require(stale.approvalScope.kind == .exactRequest && actualModel.approvals.filter({ $0.approvalAvailable }).count == 1, "actual stale task descriptor remains exact and non-approvable")
                await actualModel.decide(stale, approve: true)
                try require(OversightWire.count("/api/approvals/\(stale.id)/approve") == 0, "actual stale tracked task cannot dispatch approval")
                print("PASS actual Python stale and live tracked-task API to native availability parser")
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
            let fixtureGroups = 24 + (CommandLine.arguments.count >= 2 ? 1 : 0) + (CommandLine.arguments.count >= 3 ? 1 : 0) + (CommandLine.arguments.count == 4 ? 1 : 0)
            print("NATIVE_OVERSIGHT_TESTS_PASSED: \(fixtureGroups) fixture groups; mocked HTTP only, no real approvals executed")
        } catch { fputs("NATIVE_OVERSIGHT_TESTS_FAILED: \(error)\n", stderr); exit(1) }
    }
}
