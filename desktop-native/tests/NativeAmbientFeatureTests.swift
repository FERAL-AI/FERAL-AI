import Foundation

// Fully synthetic URLProtocol fixtures: no model, calendar, weather, vault or hardware calls.
private final class AmbientWire: URLProtocol {
    static var calls: [(String, String, [String: Any])] = []
    static var malformed: String?, failure = false, refuseWrite = false, failCalendar = false
    static var heldPath: String?, held: AmbientWire?
    static var policy: [[String: Any]] = [["domain": "mail", "mode": "draft_only", "time_windows": [], "max_per_day": 10, "requires_user_online": true, "label": "Mail", "wired": true]]
    static var approvals: [[String: Any]] = [["approval_id": "request-1", "domain": "mail", "action": "send", "status": "pending", "context": ["message": "Private fixture message"]]]
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() { if Self.heldPath == request.url?.path { Self.held = self; return }; respond() }
    func respond() {
        var data = request.httpBody ?? Data()
        if let stream = request.httpBodyStream { stream.open(); defer { stream.close() }; var bytes = [UInt8](repeating: 0, count: 4096); while stream.hasBytesAvailable { let n = stream.read(&bytes, maxLength: bytes.count); if n <= 0 { break }; data.append(bytes, count: n) } }
        let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        let path = request.url!.path, method = request.httpMethod ?? "GET"; Self.calls.append((path, method, body))
        if Self.failure { client?.urlProtocol(self, didFailWithError: NSError(domain: "fixture", code: 7, userInfo: [NSLocalizedDescriptionKey: "Bearer secret-sentinel"])); return }
        var result: [String: Any]
        switch path {
        case "/api/ambient/snapshot": result = ["time": "2026-10-01T12:00:00", "suggested_mode": "desk", "vitals": ["heart_rate": 0, "battery_pct": 100], "degraded": ["vitals:secret-sentinel"]]
        case "/api/ambient/wind_down": result = ["day_recap": ["completed_tasks": [], "active_durations_s": 0], "episodes": [], "sleep_prep": ["time_to_bed_min": 60, "hints": ["Dim lights"]], "journal_prompt": "Reflect", "degraded": []]
        case "/api/context/live": result = ["perception_text": "Recorded fixture context", "sensors": [:], "vision": ["active": false], "somatic": [:], "timestamp": 1000]
        case "/api/memory/context": result = ["snapshots": [["session_id": "other-session", "query": "Private question", "memory_context": "Private memory", "memory_filter": "recent", "ts": 10.0]]]
        case "/api/twin/status": result = ["policies": 1, "pending_approvals": 1, "supervisor_paused": false]
        case "/api/twin/policies":
            if method == "POST" { if Self.refuseWrite { result = ["success": false, "error": "secret-sentinel"] } else { Self.policy = [body]; result = ["success": true, "domain": body["domain"]!] } }
            else { result = ["policies": Self.policy, "disconnected": [], "available": [["domain": "mail", "label": "Mail"]]] }
        case "/api/twin/policies/mail": Self.policy = []; result = ["success": true]
        case "/api/twin/approvals": result = ["approvals": Self.approvals, "count": Self.approvals.count]
        case "/api/twin/approvals/request-1/approve", "/api/twin/approvals/request-1/reject": Self.approvals = []; result = ["success": true, "status": path.hasSuffix("/approve") ? "approved" : "rejected"]
        case "/api/ambient/briefing": result = ["greeting": "Good morning", "agenda": [], "goals": [], "weather": NSNull(), "sleep": ["hrv_ms": 45, "samples": 3, "trend": "stable"], "degraded": ["weather:secret-sentinel", "vip_emails:not_implemented"]]
        case "/api/ambient/next_event": result = Self.failCalendar ? ["event": NSNull(), "degraded": "secret-sentinel", "hint": "secret-sentinel"] : ["summary": "Fixture meeting", "start": "2026-10-01T13:00:00Z", "end": "2026-10-01T14:00:00Z"]
        case "/api/digital-twin/ask": let q = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems?.first(where: { $0.name == "question" })?.value ?? ""; result = ["question": q, "answer": "Fixture inference"]
        default: result = ["error": "unexpected fixture route"]
        }
        if Self.malformed == path { result = ["unexpected": "secret-sentinel"] }
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: [:])!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: result)); client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
@main private struct NativeAmbientFeatureTests {
    @MainActor static func main() async throws {
        var count = 0
        func check(_ value: Bool, _ reason: String) { precondition(value, reason); count += 1 }
        func refuses(_ action: () throws -> Void) { do { try action(); preconditionFailure("Expected refusal") } catch { count += 1 } }
        let configuration = URLSessionConfiguration.ephemeral; configuration.protocolClasses = [AmbientWire.self]; let session = URLSession(configuration: configuration)
        let url = URL(string: "http://127.0.0.1:19990")!
        let model = NativeAmbientModel(baseURL: url, session: session)
        await model.refresh()
        check(AmbientWire.calls.count == 7 && AmbientWire.calls.allSatisfy { $0.1 == "GET" }, "Navigation reads only seven local endpoints")
        check(!AmbientWire.calls.contains { ["/api/ambient/briefing", "/api/ambient/next_event", "/api/digital-twin/ask"].contains($0.0) }, "No automatic weather calendar or model action")
        check(model.contexts.count == 1 && model.contexts[0].session == "other-session", "Global memory context source session retained")
        check(model.contexts[0].time == Date(timeIntervalSince1970: 10), "Source context timestamp retained distinct from receipt")
        check(model.degraded("snapshot") == ["vitals"], "Raw degraded exception removed from UI-safe section list")
        let prior = model.fetched["live"]
        AmbientWire.malformed = "/api/context/live"; await model.refresh()
        check(model.errors["live"] != nil && model.fetched["live"] == prior && model.payloads["live"]?["perception_text"] as? String == "Recorded fixture context", "Malformed refresh keeps prior data explicitly stale")
        check(model.payloads["wind"] != nil && model.errors["wind"] == nil, "One failed section does not discard partial success")
        AmbientWire.malformed = "/api/memory/context"; await model.refresh()
        check(model.errors["memory"] != nil && model.contexts.count == 1, "Malformed memory ring does not impersonate empty memory")
        AmbientWire.malformed = nil; AmbientWire.failure = true; await model.refresh()
        check(model.errors.values.allSatisfy { !$0.contains("secret-sentinel") }, "Transport exceptions cannot expose credentials")
        AmbientWire.failure = false; await model.refresh()
        let briefing = try model.briefingReview()
        check(briefing.scope.contains("OpenWeather") && briefing.scope.contains("vault") && briefing.scope.contains("defaults"), "Weather transmission and vault access reviewed explicitly")
        check(await model.perform(briefing), "Explicit briefing returned")
        check(model.degraded("briefing") == ["vip_emails", "weather"], "Partial briefing uses safe known section verdicts")
        let beforeReplay = AmbientWire.calls.count
        check(await model.perform(briefing) == false && AmbientWire.calls.count == beforeReplay, "Reviewed action is single use")
        let calendar = try model.calendarReview()
        check(calendar.scope.contains("refresh tokens") && calendar.scope.contains("external"), "Calendar lookup side effects disclosed")
        check(await model.perform(calendar), "Explicit next event fetch")
        AmbientWire.failCalendar = true
        check(await model.perform(try model.calendarReview()) == false && model.errors["calendar"] != nil, "Degraded no-event is not a successful empty calendar")
        check(model.actionError?.contains("secret-sentinel") == false, "Calendar raw exception never rendered")
        AmbientWire.failCalendar = false
        let ask = try model.askReview("What should I prioritize?")
        check(ask.scope.contains("global episodes") && ask.scope.contains("Cloud") && ask.scope.contains("URL") && ask.scope.contains("inference"), "Private global context/model URL disclosure")
        check(await model.perform(ask) && model.answerQuestion == "What should I prioritize?" && model.answerAt != nil, "Exact question receipt and inference timestamp")
        refuses { _ = try model.askReview("   ") }
        refuses { _ = try model.askReview(String(repeating: "x", count: 4001)) }
        refuses { _ = try model.policyReview(domain: "../mail", mode: "auto_send", windows: "", cap: "0", online: false) }
        refuses { _ = try model.policyReview(domain: "mail", mode: "auto_send", windows: "25:00-26:00", cap: "0", online: false) }
        let change = try model.policyReview(domain: "mail", mode: "auto_send", windows: "09:00-17:00", cap: "0", online: false)
        check(change.scope.contains("without individual approval") && change.scope.contains("zero is unlimited"), "Authorization expansion and unlimited cap reviewed")
        AmbientWire.policy[0]["max_per_day"] = 11
        let writes = AmbientWire.calls.filter { $0.1 == "POST" }.count
        check(await model.perform(change) == false && AmbientWire.calls.filter { $0.1 == "POST" }.count == writes, "Changed policy inventory prevents authorization mutation")
        await model.refresh()
        let saved = try model.policyReview(domain: "mail", mode: "draft_only", windows: "", cap: "5", online: true)
        check(await model.perform(saved), "Exact policy save with verified readback")
        let body = AmbientWire.calls.first { $0.0 == "/api/twin/policies" && $0.1 == "POST" }!.2
        check(Set(body.keys) == Set(["domain", "mode", "time_windows", "max_per_day", "requires_user_online"]), "Only real policy contract fields written")
        let approve = try model.approvalReview("request-1", allow: true)
        check(approve.previous.contains("Private fixture message") && approve.scope.contains("not confirmed"), "Exact private action context reviewed without execution claim")
        AmbientWire.approvals[0]["action"] = "changed"
        let decisions = AmbientWire.calls.filter { $0.0.hasSuffix("/approve") }.count
        check(await model.perform(approve) == false && AmbientWire.calls.filter { $0.0.hasSuffix("/approve") }.count == decisions, "Changed approval context blocks stale decision")
        await model.refresh()
        check(await model.perform(try model.approvalReview("request-1", allow: true)) && model.receipt?.contains("execution is not confirmed") == true, "Decision receipt never claims action executed")
        check(await model.perform(try model.removePolicyReview("mail")) && AmbientWire.policy.isEmpty, "Policy deletion verifies absence")
        let staleConnection = try model.askReview("Stale question")
        model.configure(URL(string: "http://127.0.0.1:19991"))
        let beforeStale = AmbientWire.calls.count
        check(await model.perform(staleConnection) == false && AmbientWire.calls.count == beforeStale, "Connection change invalidates all reviews before network")
        check(model.answer == nil && model.contexts.isEmpty && model.payloads.isEmpty, "Connection change clears private prior-agent state")
        let remote = NativeAmbientModel(baseURL: URL(string: "https://example.com"), session: session); await remote.refresh()
        check(AmbientWire.calls.count == beforeStale && remote.errors.count == 7, "Non-loopback denied before request")
        let credentials = NativeAmbientModel(baseURL: URL(string: "http://user:password@127.0.0.1:19990"), session: session); await credentials.refresh()
        check(AmbientWire.calls.count == beforeStale, "Credential-bearing URL denied")
        AmbientWire.heldPath = "/api/ambient/snapshot"
        let pending = Task { await model.refresh() }
        while AmbientWire.held == nil { await Task.yield() }
        model.configure(nil); AmbientWire.heldPath = nil; AmbientWire.held?.respond(); AmbientWire.held = nil
        await pending.value
        check(model.payloads.isEmpty && model.errors.isEmpty && !model.busy, "Late old-connection response cannot repopulate disconnected state")
        print("NativeAmbientFeatureTests PASS \(count) assertions (synthetic only; no provider/hardware verification)")
    }
}
