import Foundation
private final class RoutingWire: URLProtocol {
    static var calls: [(String, String, [String: Any], [URLQueryItem])] = []
    static var config: [String: Any] = ["llm": ["provider": "ollama", "model": "fixture-local", "base_url": "http://127.0.0.1:11435/v1", "future": ["private": "fixture-private-sentinel"], "call_site_tiers": ["chat": "balanced", "future_site": "future-tier"], "tier_map": ["chat": ["cheap": ["provider": "ollama", "model": "old-model", "opaque": ["keep": true, "private": "fixture-private-sentinel"]], "future-tier": ["keep": true]], "future-site": ["keep": true]]], "vision": ["enabled": false, "future": "retain"], "unrelated": "keep"]
    static var runtimeProvider = "ollama", runtimeModel = "fixture-local"
    static var fail = false, failRoute = false, wrongRoute = false, wrongPreset = false
    static var hook: (() -> Void)?
    static var preset: [String: Any] = ["id": "openai_default", "provider": "openai", "model": "", "description": "Fixture provider default", "vision_supported": true]
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        var data = request.httpBody ?? Data()
        if let stream = request.httpBodyStream { stream.open(); defer { stream.close() }; var buffer = [UInt8](repeating: 0, count: 4096); while stream.hasBytesAvailable { let n = stream.read(&buffer, maxLength: buffer.count); if n <= 0 { break }; data.append(buffer, count: n) } }
        let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        let path = request.url!.path, method = request.httpMethod ?? "GET", query = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
        Self.calls.append((path, method, body, query))
        var value: [String: Any]
        switch path {
        case "/api/config": value = Self.config
        case "/api/llm/presets": value = ["presets": [Self.preset]]
        case "/api/llm/providers": value = ["providers": [["id": "ollama", "display_name": "Ollama", "configured": true, "chat_ready": true], ["id": "openai", "display_name": "OpenAI", "configured": false, "chat_ready": true]]]
        case "/api/llm/status": value = ["provider": Self.runtimeProvider, "model": Self.runtimeModel, "available": true, "supported": true]
        case "/api/llm/route":
            let site = query.first(where: { $0.name == "call_site" })!.value!, tier = query.first(where: { $0.name == "tier" })?.value
            value = Self.failRoute ? ["error": "Bearer fixture-private-sentinel"] : ["call_site": Self.wrongRoute ? "wrong" : site, "tier": tier ?? "balanced", "provider": Self.runtimeProvider, "model": Self.runtimeModel, "supported": true, "fallback_providers": ["openai"], "source": tier == nil ? "settings" : "explicit"]
        case "/api/config/update":
            var block = Self.config["llm"] as! [String: Any]; block[body["key"] as! String] = body["value"]; Self.config["llm"] = block; value = ["ok": true]
        case "/api/llm/presets/apply":
            Self.runtimeProvider = "openai"; Self.runtimeModel = "fixture-resolved"
            var block = Self.config["llm"] as! [String: Any]; block["provider"] = Self.runtimeProvider; block["model"] = Self.runtimeModel; Self.config["llm"] = block
            value = ["ok": true, "preset": Self.wrongPreset ? "wrong" : "openai_default", "provider": Self.runtimeProvider, "model": Self.runtimeModel, "vision_supported": true, "warning": "Bearer fixture-private-sentinel"]
        default: value = ["error": "unexpected fixture request"]
        }
        if Self.fail { value = ["error": "Bearer fixture-private-sentinel"] }
        Self.hook?(); Self.hook = nil
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: [:])!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: value)); client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
@main private struct NativeProviderRoutingFeatureTests {
    @MainActor static func main() async throws {
        var count = 0
        func check(_ value: Bool, _ label: String) { precondition(value, label); count += 1 }
        func refuses(_ label: String, _ make: () throws -> Void) { do { try make(); preconditionFailure(label) } catch { count += 1 } }
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [RoutingWire.self]; let session = URLSession(configuration: config)
        let local = URL(string: "http://127.0.0.1:19998")!
        let model = NativeProviderRoutingModel(baseURL: local, session: session)
        await model.refresh()
        check(RoutingWire.calls.count == 4 && RoutingWire.calls.allSatisfy { $0.1 == "GET" && !$0.0.contains("/route") && !$0.0.contains("probe") && !$0.0.contains("models") }, "passive opening cannot probe/discover/inspect automatically")
        check(model.presets.count == 1 && model.providers.count == 2 && model.runtime?.contains("ollama") == true, "actual preset/provider/status schemas")
        await model.inspect(tier: "cheap")
        check(model.routes.count == 4 && model.routes.allSatisfy { $0.tier == "cheap" && $0.source == "explicit" }, "pure explicit tier routes for four call sites")
        check(RoutingWire.calls.suffix(4).allSatisfy { $0.3.contains(URLQueryItem(name: "tier", value: "cheap")) }, "explicit tier encoded as query only")
        let review = try model.reviewTier(site: "chat", tier: "premium")
        check(review.previous == "balanced" && review.proposed == "premium" && review.scope.contains("refresh Codex models"), "old/new tier and switch side effect disclosed")
        check(await model.perform(review), "targeted tier save")
        check(model.savedTiers["future_site"] as? String == "future-tier" && model.llm["future"] != nil && RoutingWire.config["unrelated"] as? String == "keep", "unknown map/llm/unrelated siblings retained")
        check(model.receipt?.contains("chat/balanced") == true && model.savedTiers["chat"] as? String == "premium", "saved and runtime tiers can differ without false live application")
        let calls = RoutingWire.calls.count; check(await model.perform(review) == false && RoutingWire.calls.count == calls, "review single use")
        let target = try model.reviewTarget(site: "chat", tier: "cheap", provider: "openai", model: "explicit-fixture")
        check(!target.previous.contains("fixture-private-sentinel") && !target.proposed.contains("fixture-private-sentinel"), "review excludes opaque private siblings")
        check(await model.perform(target), "explicit target merged save")
        let map = model.llm["tier_map"] as! [String: Any], sites = map["chat"] as! [String: Any], leaf = sites["cheap"] as! [String: Any]
        check((leaf["opaque"] as? [String: Any])?["keep"] as? Bool == true && sites["future-tier"] != nil && map["future-site"] != nil, "leaf/site/other-site opaque data retained")
        let remove = try model.reviewTarget(site: "chat", tier: "cheap", provider: "", model: "", remove: true); check(await model.perform(remove), "return to automatic reviewed save")
        let removed = (((model.llm["tier_map"] as! [String: Any])["chat"] as! [String: Any])["cheap"] as! [String: Any])
        check(removed["provider"] == nil && removed["model"] == nil && removed["opaque"] != nil, "remove routing override retains unknown metadata")
        refuses("unsupported tier") { _ = try model.reviewTier(site: "chat", tier: "fake") }
        refuses("unsupported site") { _ = try model.reviewTier(site: "fake", tier: "cheap") }
        refuses("unknown provider") { _ = try model.reviewTarget(site: "chat", tier: "cheap", provider: "fake", model: "x") }
        refuses("blank model") { _ = try model.reviewTarget(site: "chat", tier: "cheap", provider: "openai", model: "   ") }
        refuses("CRLF model") { _ = try model.reviewTarget(site: "chat", tier: "cheap", provider: "openai", model: "a\r\nb") }
        let stale = try model.reviewTier(site: "chat", tier: "cheap")
        var changed = RoutingWire.config["llm"] as! [String: Any]; changed["new_future"] = true; RoutingWire.config["llm"] = changed
        let beforeStale = RoutingWire.calls.filter { $0.1 == "POST" }.count
        check(await model.perform(stale) == false && RoutingWire.calls.filter { $0.1 == "POST" }.count == beforeStale, "fresh whole-llm snapshot preflight prevents stale merge")
        await model.refresh()
        RoutingWire.failRoute = true; let fallback = try model.reviewTier(site: "chat", tier: "balanced"); check(await model.perform(fallback) && model.receipt?.contains("Runtime application is not confirmed") == true, "persisted save with failed runtime inspection stated accurately")
        RoutingWire.failRoute = false; RoutingWire.wrongRoute = true; await model.inspect(tier: nil)
        check(model.routes.isEmpty && model.routeErrors.count == 4 && model.routeErrors.values.allSatisfy { !$0.contains("fixture-private-sentinel") }, "route identity mismatch and safe diagnostics")
        RoutingWire.wrongRoute = false
        let preset = try model.reviewPreset("openai_default")
        check(preset.scope.contains("does not clear saved base_url") && preset.proposed.contains("default or detected model"), "preset endpoint/model limitations disclosed")
        check(await model.perform(preset), "preset actual substituted model and selection readback")
        check(model.llm["base_url"] as? String == "http://127.0.0.1:11435/v1" && model.receipt?.contains("fixture-resolved") == true && model.receipt?.contains("fixture-private-sentinel") == false, "saved endpoint persistence and private warning handled honestly")
        let canceled = try model.reviewPreset("openai_default"); model.cancel(canceled); let beforeCancel = RoutingWire.calls.count; check(await model.perform(canceled) == false && RoutingWire.calls.count == beforeCancel, "cancel before any network")
        let switched = try model.reviewTier(site: "chat", tier: "cheap"); model.configure(nil); check(await model.perform(switched) == false && model.config == nil && model.runtime == nil, "agent switch revokes review and private state")
        model.configure(local); await model.refresh()
        let inFlight = try model.reviewTier(site: "chat", tier: "cheap"); RoutingWire.hook = { model.configure(nil) }; check(await model.perform(inFlight) == false && model.receipt == nil, "in-flight generation change cannot publish stale receipt")
        model.configure(URL(string: "https://example.com")!); let beforeExternal = RoutingWire.calls.count; await model.refresh(); check(RoutingWire.calls.count == beforeExternal, "non-loopback origins cannot receive settings")
        print("Native provider routing feature: \(count) assertions passed")
    }
}
