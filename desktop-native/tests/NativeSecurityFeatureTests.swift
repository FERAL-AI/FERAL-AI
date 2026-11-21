// Standalone URLProtocol fixtures. No real settings, filesystem grants,
// Keychain, provider billing or native folder dialogs are touched.
import Foundation

private final class SecurityWire: URLProtocol {
    static var calls: [(String, String, [String: Any], [URLQueryItem])] = []
    static var grants: [[String: Any]] = []
    static var tier = "active"
    static var policy: [String: Any] = ["execution": ["full_authority": false], "filesystem": ["read_paths": ["/fixture"]], "future": ["keep": true]]
    static var cost: [String: Any] = ["chat": ["per_hour_usd": 2, "per_day_usd": 9, "future": "keep"], "global_per_day_usd": 50, "per_call_site_caps": ["vision": ["per_hour_usd": 4, "per_day_usd": 20]]]
    static var auditFailure = false
    static var failMutation = false
    static var failCostKey: String?
    static var omitCost = false, malformedCost = false
    static var hook: (() -> Void)?
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        var bytes = request.httpBody ?? Data()
        if let stream = request.httpBodyStream {
            stream.open(); defer { stream.close() }; var buffer = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable { let n = stream.read(&buffer, maxLength: buffer.count); if n <= 0 { break }; bytes.append(buffer, count: n) }
        }
        let body = (try? JSONSerialization.jsonObject(with: bytes)) as? [String: Any] ?? [:]
        let path = request.url!.path, method = request.httpMethod ?? "GET"
        let query = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!.queryItems ?? []
        Self.calls.append((path, method, body, query))
        let response: [String: Any]
        if method != "GET" && (Self.failMutation || (path == "/api/config/update" && body["key"] as? String == Self.failCostKey && Self.failCostKey != nil)) { response = ["ok": false, "error": "fixture mutation refused"] }
        else {
            switch (path, method) {
            case ("/api/security/grants", "GET"): response = ["grants": Self.grants]
            case ("/api/security/grants", "POST"):
                let path = body["path"] as! String; Self.grants.removeAll { $0["path"] as? String == path }; Self.grants.append(["path": path, "mode": body["mode"]!, "granted_at": 1700000000]); response = ["ok": true, "path": path]
            case ("/api/security/grants", "DELETE"):
                let path = query.first(where: {$0.name == "path"})!.value!; Self.grants.removeAll { $0["path"] as? String == path }; response = ["ok": true, "path": path]
            case ("/api/security/permissions", "GET"): response = ["max_tier": Self.tier, "tiers": ["passive", "active", "privileged", "dangerous"], "tier_descriptions": ["active": "fixture description"]]
            case ("/api/security/permissions/update", "POST"): Self.tier = body["max_tier"] as! String; response = ["ok": true, "max_tier": Self.tier]
            case ("/api/policy", "GET"): response = Self.policy
            case ("/api/policy/update", "POST"): Self.policy = body; response = ["ok": true]
            case ("/api/security/audit", "GET"): response = Self.auditFailure ? ["error": "audit_log_unreadable", "entries": []] : ["entries": [["action": "store", "key": "fixture-name", "actor": "fixture", "ts": 1700000000]]]
            case ("/api/config", "GET"):
                if Self.omitCost { response = ["unrelated": ["keep": true]] }
                else if Self.malformedCost { response = ["cost": "unreadable", "unrelated": ["keep": true]] }
                else { response = ["cost": Self.cost, "unrelated": ["keep": true]] }
            case ("/api/config/update", "POST"): Self.cost[body["key"] as! String] = body["value"]; Self.omitCost = false; response = ["ok": true]
            default: response = ["error": "unexpected fixture request"]
            }
        }
        let emit = {
            self.client?.urlProtocol(self, didReceive: HTTPURLResponse(url: self.request.url!, statusCode: 200, httpVersion: nil, headerFields: [:])!, cacheStoragePolicy: .notAllowed)
            self.client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: response))
            self.client?.urlProtocolDidFinishLoading(self)
        }
        if let hook = Self.hook { Self.hook = nil; hook(); DispatchQueue.global().asyncAfter(deadline: .now() + 0.03, execute: emit) } else { emit() }
    }
    override func stopLoading() {}
    static func writes() -> Int { calls.filter { $0.1 != "GET" }.count }
}
private struct SecurityAssertion: Error { let message: String }
private var count = 0
private func expect(_ value: @autoclosure () -> Bool, _ message: String) throws { count += 1; if !value() { throw SecurityAssertion(message: message) } }
private func refuses(_ action: () throws -> Void) throws { do { try action(); throw SecurityAssertion(message: "expected local refusal") } catch is SecurityAssertion { throw SecurityAssertion(message: "expected local refusal") } catch { count += 1 } }

@main struct NativeSecurityFeatureTests {
    @MainActor static func main() async throws {
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [SecurityWire.self]
        let model = NativeSecurityModel(baseURL: nil, session: URLSession(configuration: config))
        await model.refresh(); try expect(SecurityWire.calls.isEmpty && model.errors.count == 5, "nil URL makes no calls")
        model.configure(URL(string: "https://remote.example")!); await model.refresh(); try expect(SecurityWire.calls.isEmpty, "remote URL makes no calls")
        model.configure(URL(string: "http://127.0.0.1:9999")!); await model.refresh()
        try expect(model.data.count == 5 && model.errors.isEmpty, "load all five real contracts")
        try expect(!SecurityWire.calls.contains(where: { $0.0.contains("vault") }), "phase must not query credentials or vault")
        let priorCost = SecurityWire.cost
        SecurityWire.omitCost = true; SecurityWire.cost = [:]; await model.refresh()
        try expect(model.errors["cost"] == nil && (model.data["cost"]?["cost"] as? [String: Any])?.isEmpty == true, "omitted optional cost normalizes to no configured caps")
        try expect((model.data["cost"]?["unrelated"] as? [String: Any])?["keep"] as? Bool == true && model.data["permissions"]?["max_tier"] as? String == "active", "normalization preserves unrelated configuration and security state")
        let firstCap = try model.costReview(key: "global_per_hour_usd", amount: "1.25")
        try expect(firstCap.previous == NativeSecurityWire.json([String: Any]()) && firstCap.preparatoryBodies.isEmpty && firstCap.body?["value"] as? Double == 1.25, "first cap review has no invented prior or defaults")
        await model.commit(firstCap, confirmed: true)
        let firstWrite = SecurityWire.calls.last(where: { $0.0 == "/api/config/update" })!.2
        try expect(Set(firstWrite.keys) == Set(["section", "key", "value"]) && firstWrite["section"] as? String == "cost" && firstWrite["key"] as? String == "global_per_hour_usd" && firstWrite["value"] as? Double == 1.25 && SecurityWire.cost.count == 1 && model.receipt != nil, "first cap writes exact per-key contract without fabricated caps")
        SecurityWire.malformedCost = true; await model.refresh()
        try expect(model.errors["cost"] != nil, "present malformed cost is rejected")
        try refuses { _ = try model.costReview(key: "global_per_hour_usd", amount: "2") }
        SecurityWire.malformedCost = false; SecurityWire.cost = priorCost; await model.refresh()
        var redirectAccepted = true
        let redirectGuard = NativeSecurityRedirectGuard()
        let redirectSession = NativeSecurityRedirectGuard.session()
        var policyRequest = URLRequest(url: URL(string: "http://127.0.0.1:9999/api/policy/update")!); policyRequest.httpMethod = "POST"; policyRequest.httpBody = Data("private-policy".utf8)
        let task = redirectSession.dataTask(with: policyRequest)
        redirectGuard.urlSession(redirectSession, task: task, willPerformHTTPRedirection: HTTPURLResponse(url: policyRequest.url!, statusCode: 307, httpVersion: nil, headerFields: [:])!, newRequest: URLRequest(url: URL(string: "https://external.invalid/collect")!)) { redirectAccepted = $0 != nil }
        try expect(!redirectAccepted, "security POST cross-origin redirect never receives a request to replay")
        redirectAccepted = true
        redirectGuard.urlSession(redirectSession, task: task, willPerformHTTPRedirection: HTTPURLResponse(url: policyRequest.url!, statusCode: 308, httpVersion: nil, headerFields: [:])!, newRequest: URLRequest(url: URL(string: "http://127.0.0.1:9999/other-action")!)) { redirectAccepted = $0 != nil }
        try expect(!redirectAccepted, "security mutation redirects also refused on same origin")
        task.cancel(); redirectSession.invalidateAndCancel()
        SecurityWire.calls.removeAll() // Existing grant scenarios count their own writes.
        let path = "/fixture/Folder & project"
        let grant = try model.grantReview(path: path, mode: "readwrite")
        await model.commit(grant, confirmed: false); try expect(SecurityWire.writes() == 0, "unconfirmed grant makes no writes")
        await model.commit(grant, confirmed: true)
        try expect(model.receipt != nil && SecurityWire.grants.first?["mode"] as? String == "readwrite", "grant acknowledged and reread")
        let grantBody = SecurityWire.calls.first(where: {$0.1 == "POST"})!.2
        try expect(Set(grantBody.keys) == Set(["path", "mode"]), "grant payload is scoped")
        let afterGrant = SecurityWire.calls.count; await model.commit(grant, confirmed: true)
        try expect(SecurityWire.calls.count == afterGrant, "consumed review cannot replay")
        let revoke = try model.revokeReview(path: path); await model.commit(revoke, confirmed: true)
        let delete = SecurityWire.calls.last(where: {$0.1 == "DELETE"})!
        try expect(delete.2.isEmpty && delete.3.first?.value == path && SecurityWire.grants.isEmpty, "DELETE uses exactly encoded path query")
        let stale = try model.grantReview(path: "/fixture/other", mode: "read")
        SecurityWire.grants.append(["path": "/fixture/concurrent", "mode": "read"])
        let oldWrites = SecurityWire.writes(); await model.commit(stale, confirmed: true)
        try expect(SecurityWire.writes() == oldWrites && model.errors["grants"]?.contains("changed after review") == true, "stale security review never writes")
        await model.refresh()
        let tier = try model.tierReview("dangerous")
        try expect(tier.scope.contains("does not persist") && tier.previous.contains("active") && tier.proposed.contains("dangerous"), "tier review exposes old/new and live-only scope")
        await model.commit(tier, confirmed: true)
        try expect(model.data["permissions"]?["max_tier"] as? String == "dangerous" && model.receipt?.contains("live") == true, "live permission tier confirmed by reread")
        try refuses { _ = try model.tierReview("arbitrary") }
        try refuses { _ = try model.policyReview("{broken") }
        try refuses { _ = try model.policyReview("{\"execution\":{\"full_authority\":true}}") }
        var replacement = SecurityWire.policy; replacement["filesystem"] = ["read_paths": ["/fixture/new"]]
        let policy = try model.policyReview(NativeSecurityWire.json(replacement)); await model.commit(policy, confirmed: true)
        try expect((SecurityWire.policy["future"] as? [String: Any])?["keep"] as? Bool == true && model.receipt != nil, "full policy review preserves supplied unrelated fields")
        let cap = try model.costReview(key: "chat", amount: "0")
        try expect((cap.body?["value"] as? [String: Any])?["per_day_usd"] as? Int == 9 && (cap.body?["value"] as? [String: Any])?["future"] as? String == "keep", "site edit preserves sibling fields")
        await model.commit(cap, confirmed: true)
        try expect((SecurityWire.cost["chat"] as? [String: Any])?["per_hour_usd"] as? Double == 0 && SecurityWire.cost["global_per_day_usd"] as? Int == 50, "zero-dollar limit and other keys retained")
        let clear = try model.costReview(key: "chat", amount: "")
        try expect(clear.scope.contains("unlimited"), "removing cap explicitly reviews unlimited scope")
        await model.commit(clear, confirmed: true)
        try expect((SecurityWire.cost["chat"] as? [String: Any])?["per_hour_usd"] is NSNull && (SecurityWire.cost["chat"] as? [String: Any])?["per_day_usd"] as? Int == 9, "remove hourly limit without losing daily cap")
        let legacy = try model.costReview(key: "vision", amount: "3")
        try expect((legacy.body?["value"] as? [String: Any])?["per_day_usd"] as? Int == 20, "legacy site sibling fields preserved when writing canonical flat override")
        let clearLegacy = try model.costReview(key: "vision", amount: "")
        try expect(clearLegacy.preparatoryBodies.count == 1 && clearLegacy.scope.contains("not atomic"), "legacy clear must review additional write and partial failure scope")
        await model.commit(clearLegacy, confirmed: true)
        try expect(((SecurityWire.cost["per_call_site_caps"] as? [String: Any])?["vision"] as? [String: Any])?["per_hour_usd"] is NSNull && (SecurityWire.cost["vision"] as? [String: Any])?["per_hour_usd"] is NSNull, "clear both effective legacy and flat hourly caps")
        try expect(((SecurityWire.cost["per_call_site_caps"] as? [String: Any])?["vision"] as? [String: Any])?["per_day_usd"] as? Int == 20, "legacy clear preserves other fields")
        SecurityWire.cost["custom_background"] = ["per_hour_usd": 7, "keep": true]
        await model.refresh()
        try expect(model.costSites.contains("custom_background"), "existing custom call-site caps remain reachable")
        let custom = try model.costReview(key: "custom_background", amount: "6"); await model.commit(custom, confirmed: true)
        try expect((SecurityWire.cost["custom_background"] as? [String: Any])?["keep"] as? Bool == true, "custom cap edit preserves unrelated fields")
        SecurityWire.cost["per_call_site_caps"] = ["vision": ["per_hour_usd": 4, "per_day_usd": 20]]
        SecurityWire.cost["vision"] = ["per_hour_usd": 3]
        await model.refresh()
        let partial = try model.costReview(key: "vision", amount: "")
        SecurityWire.failCostKey = "vision"; await model.commit(partial, confirmed: true); SecurityWire.failCostKey = nil
        try expect(model.errors["cost"]?.contains("acknowledged write") == true && model.receipt == nil, "sequential clear failure never reports complete success")
        try expect((model.data["cost"]?["cost"] as? [String: Any])?["vision"] is [String: Any] && (((model.data["cost"]?["cost"] as? [String: Any])?["per_call_site_caps"] as? [String: Any])?["vision"] as? [String: Any])?["per_hour_usd"] is NSNull, "partial failure rereads actual configuration")
        await model.refresh()
        try refuses { _ = try model.costReview(key: "chat", amount: "-1") }
        try refuses { _ = try model.costReview(key: "chat", amount: "nan") }
        try refuses { _ = try model.costReview(key: "network", amount: "1") }
        SecurityWire.auditFailure = true; await model.refresh()
        try expect(model.errors["audit"] != nil && (model.data["audit"]?["entries"] as? [[String: Any]])?.count == 1, "audit failure does not assert empty log")
        SecurityWire.failMutation = true
        let failed = try model.costReview(key: "global_per_hour_usd", amount: "2")
        await model.commit(failed, confirmed: true)
        try expect(model.errors["cost"] != nil && model.receipt == nil, "200 error/okfalse cannot count as success")
        SecurityWire.hook = { DispatchQueue.main.async { model.configure(nil) } }
        await model.refresh()
        try expect(model.data.isEmpty && model.errors.isEmpty && model.receipt == nil, "old runtime responses cannot restore reset security data")
        print("PASS: \(count) native security/cost fixture assertions; no real settings, grants, billing, dialogs or Keychain calls")
    }
}
