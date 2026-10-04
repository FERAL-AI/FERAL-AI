// Standalone mocked-wire tests; not linked into the application.
import Foundation

private final class ConfigurationWire: URLProtocol {
    static var calls: [(String, String, [String: Any])] = []
    static var configuration: [String: Any] = ["version": "fixture", "features": ["streaming": true, "proactive": false, "self_learning": false, "multi_agent": false, "vision": false, "future_feature": true], "security": ["untouched": true]]
    static var mode = "hybrid"
    static var persisted = true
    static var failMemory = false
    static var failGeneral = false
    static var configuredBackend = "sqlite_vec"
    static var beforeReply: (() -> Void)?
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let path = request.url!.path, method = request.httpMethod ?? "GET"
        var data = request.httpBody ?? Data()
        if let stream = request.httpBodyStream {
            stream.open(); defer { stream.close() }
            var buffer = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable { let count = stream.read(&buffer, maxLength: buffer.count); if count <= 0 { break }; data.append(buffer, count: count) }
        }
        let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        Self.calls.append((path, method, body))
        let response: [String: Any]
        switch (path, method) {
        case ("/api/config", "GET"): response = Self.failGeneral ? ["error": "fixture read refused"] : Self.configuration
        case ("/api/config/update", "POST"):
            var features = Self.configuration["features"] as! [String: Any]; features[body["key"] as! String] = body["value"]; Self.configuration["features"] = features
            response = ["ok": true]
        case ("/api/autonomy", "GET"): response = ["mode": Self.mode]
        case ("/api/autonomy", "POST"): Self.mode = body["mode"] as! String; response = ["success": true, "mode": Self.mode, "persisted": Self.persisted]
        case ("/api/memory/backend", "POST"):
            if Self.failMemory { response = ["ok": false, "error": "fixture preflight failed; NOT saved"] }
            else { Self.configuredBackend = body["backend"] as! String; response = ["ok": true, "note": "Saved; restart needed. Existing embeddings remain in the old store."] }
        case ("/api/memory/backend", "GET"):
            response = ["backend": Self.configuredBackend, "runtime": "numpy_fallback", "constructed_backend": "sqlite_vec", "pending_unapplied": Self.configuredBackend != "sqlite_vec", "available": ["sqlite_vec": true, "lance": true, "chroma": false], "vector_index_degraded": true, "vector_index_degraded_reason": "fixture extension unavailable", "semantic_search": "unknown"]
        default: response = ["error": "unexpected fixture request"]
        }
        let emit = {
            self.client?.urlProtocol(self, didReceive: HTTPURLResponse(url: self.request.url!, statusCode: 200, httpVersion: nil, headerFields: [:])!, cacheStoragePolicy: .notAllowed)
            self.client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: response))
            self.client?.urlProtocolDidFinishLoading(self)
        }
        if let hook = Self.beforeReply { Self.beforeReply = nil; hook(); DispatchQueue.global().asyncAfter(deadline: .now() + 0.03, execute: emit) } else { emit() }
    }
    override func stopLoading() {}
}
private struct ConfigurationAssertion: Error { let message: String }
private func expect(_ condition: @autoclosure () -> Bool, _ message: String) throws { if !condition() { throw ConfigurationAssertion(message: message) } }

@main struct NativeConfigurationFeatureTests {
    @MainActor static func main() async throws {
        let configuration = URLSessionConfiguration.ephemeral; configuration.protocolClasses = [ConfigurationWire.self]
        let model = NativeConfigurationModel(baseURL: nil, session: URLSession(configuration: configuration))
        await model.refresh()
        try expect(ConfigurationWire.calls.isEmpty && model.errors.count == 3, "nil URL must not make requests or invent settings")
        model.setBaseURL(URL(string: "https://remote.example")!); await model.refresh()
        try expect(ConfigurationWire.calls.isEmpty, "remote URL must be refused before any request")
        print("PASS nil and non-loopback endpoints make no calls")
        model.setBaseURL(URL(string: "http://127.0.0.1:9999")!); await model.refresh()
        try expect(model.errors.isEmpty && model.autonomy == "hybrid" && model.feature("streaming") == true && model.memory?["runtime"] as? String == "numpy_fallback", "load real General/autonomy/memory contracts")
        print("PASS actual three read contracts and distinct memory runtime")
        await model.setFeature("proactive", enabled: true)
        let update = ConfigurationWire.calls.last(where: { $0.0 == "/api/config/update" })!.2
        try expect(Set(update.keys) == Set(["section", "key", "value"]) && update["section"] as? String == "features" && update["key"] as? String == "proactive" && update["value"] as? Bool == true, "write only intended feature key")
        try expect(model.feature("proactive") == true && (model.config?["features"] as? [String: Any])?["future_feature"] as? Bool == true && (model.config?["security"] as? [String: Any])?["untouched"] as? Bool == true, "reread and preserve unrelated sections")
        let count = ConfigurationWire.calls.count; await model.setFeature("network.bind_host", enabled: true)
        try expect(ConfigurationWire.calls.count == count, "feature API may not become arbitrary config setter")
        print("PASS per-key feature write, reread and unrelated configuration preserved")
        await model.changeAutonomy(to: "loose", confirmed: false)
        try expect(ConfigurationWire.calls.count == count && model.autonomy == "hybrid", "unconfirmed autonomy must not mutate")
        ConfigurationWire.persisted = false
        await model.changeAutonomy(to: "strict", confirmed: true)
        try expect(model.autonomy == "strict" && model.notes["autonomy"]?.contains("not saved") == true, "live but unpersisted autonomy change remains explicit")
        let autonomyBody = ConfigurationWire.calls.last(where: {$0.0 == "/api/autonomy" && $0.1 == "POST"})!.2
        try expect(Set(autonomyBody.keys) == Set(["mode"]), "autonomy uses dedicated scoped endpoint")
        print("PASS explicit autonomy confirmation and persistence truth")
        let beforeMemory = ConfigurationWire.calls.count
        await model.switchMemory(to: "lance", confirmed: false)
        try expect(ConfigurationWire.calls.count == beforeMemory, "unconfirmed memory selection makes no request")
        await model.switchMemory(to: "lance", confirmed: true)
        try expect(model.memory?["backend"] as? String == "lance" && model.memory?["runtime"] as? String == "numpy_fallback" && model.memory?["pending_unapplied"] as? Bool == true, "saved memory backend must not pretend runtime switched")
        try expect(model.notes["memory"]?.contains("restart") == true, "real backend note retained")
        print("PASS memory preflight endpoint and pending restart")
        ConfigurationWire.failMemory = true
        await model.switchMemory(to: "sqlite_vec", confirmed: true)
        try expect(model.errors["memory"]?.contains("NOT saved") == true && model.memory?["backend"] as? String == "lance", "failed preflight cannot become a saved selection")
        let beforeMissing = ConfigurationWire.calls.count; await model.switchMemory(to: "chroma", confirmed: true)
        try expect(ConfigurationWire.calls.count == beforeMissing, "unavailable memory backend cannot be selected")
        print("PASS memory preflight failure and unavailable backend refusal")
        ConfigurationWire.failGeneral = true; await model.refresh()
        try expect(model.errors["general"] != nil && model.feature("proactive") == true, "failed refresh retains prior data qualified by an error")
        print("PASS failed settings refresh retains explicit error")
        ConfigurationWire.beforeReply = { DispatchQueue.main.async { model.setBaseURL(nil) } }
        await model.refresh()
        try expect(model.config == nil && model.autonomy == nil && model.memory == nil && !model.busy, "old generation responses may not restore settings after runtime change")
        print("PASS runtime generation change discards stale replies")
        model.setBaseURL(URL(string: "http://127.0.0.1:9999")!); await model.refresh()
        ConfigurationWire.beforeReply = { DispatchQueue.main.async { model.setBaseURL(nil) } }
        await model.changeAutonomy(to: "hybrid", confirmed: true)
        try expect(model.autonomy == nil && model.notes.isEmpty && model.errors.isEmpty, "old mutation acknowledgement may not populate a replacement agent's state")
        print("PASS runtime generation change discards mutation acknowledgements")
    }
}
