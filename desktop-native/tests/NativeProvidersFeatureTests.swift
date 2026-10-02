// Standalone fixture execution; no real provider/vault/keychain operations.
import Foundation

private final class ProviderWire: URLProtocol {
    static let lock = NSLock()
    static var responses: [String: (Int, [String: Any])] = [:]
    static var calls: [(URL, String, [String: Any])] = []
    static var heldPaths = Set<String>()
    static var callbacks: [() -> Void] = []
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        var bytes = request.httpBody ?? Data()
        if bytes.isEmpty, let stream = request.httpBodyStream { stream.open(); defer { stream.close() }; var buffer = [UInt8](repeating: 0, count: 4096); while stream.hasBytesAvailable { let count = stream.read(&buffer, maxLength: buffer.count); if count <= 0 { break }; bytes.append(buffer, count: count) } }
        let body = (try? JSONSerialization.jsonObject(with: bytes)) as? [String: Any] ?? [:]
        Self.lock.lock(); let path = request.url!.path; Self.calls.append((request.url!, request.httpMethod ?? "GET", body)); let reply = Self.responses[path] ?? (200, [:])
        let respond = { [self] in client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: reply.0, httpVersion: nil, headerFields: ["Content-Type": "application/json"])!, cacheStoragePolicy: .notAllowed); client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: reply.1)); client?.urlProtocolDidFinishLoading(self) }
        let held = Self.heldPaths.contains(path); if held { Self.callbacks.append(respond) }; Self.lock.unlock(); if !held { respond() }
    }
    override func stopLoading() {}
    static func reset(_ values: [String: (Int, [String: Any])]) { lock.lock(); defer { lock.unlock() }; responses = values; calls = []; heldPaths = []; callbacks = [] }
    static func set(_ path: String, _ value: [String: Any], status: Int = 200) { lock.lock(); defer { lock.unlock() }; responses[path] = (status, value) }
    static func count(_ path: String? = nil) -> Int { lock.lock(); defer { lock.unlock() }; return calls.filter { path == nil || $0.0.path == path }.count }
    static func body(_ path: String) -> [String: Any] { lock.lock(); defer { lock.unlock() }; return calls.last(where: { $0.0.path == path && $0.1 != "GET" })?.2 ?? [:] }
    static func passiveOnly() -> Bool { lock.lock(); defer { lock.unlock() }; return calls.allSatisfy { $0.1 == "GET" && !$0.0.path.contains("probe") && (!($0.0.path.hasSuffix("/models")) || URLComponents(url: $0.0, resolvingAgainstBaseURL: false)?.queryItems?.contains(where: { $0.name == "live" && $0.value == "false" }) == true) } }
    static func hold(_ path: String) { lock.lock(); defer { lock.unlock() }; heldPaths.insert(path) }
    static func release() { lock.lock(); let values = callbacks; callbacks = []; heldPaths = []; lock.unlock(); values.forEach { $0() } }
}
private struct ProviderAssertion: Error { let message: String }
private func check(_ condition: @autoclosure () -> Bool, _ message: String) throws { if !condition() { throw ProviderAssertion(message: message) } }

@main struct NativeProvidersFeatureTests {
    static let base = URL(string: "http://127.0.0.1:9465")!
    static var fixtures: [String: (Int, [String: Any])] {
        ["/api/llm/providers": (200, ["providers": [["id": "ollama", "display_name": "Ollama", "requires_api_key": false, "configured": true, "chat_ready": true], ["id": "openai", "display_name": "OpenAI", "requires_api_key": true, "configured": true, "chat_ready": true]]]),
         "/api/llm/config": (200, ["provider": "ollama", "model": "local-test", "base_url": "http://127.0.0.1:11435/v1", "fallback_providers": []]),
         "/api/llm/status": (200, ["available": false, "supported": true]),
         "/api/llm/health": (200, ["candidates": [["provider": "ollama", "model": "local-test", "in_cooldown": true, "cooldown_remaining": 45, "probe_ok": false]]]),
         "/api/llm/providers/ollama/models": (200, ["models": [["id": "local-test"], ["id": "local-test"]], "source": "cache"]),
         "/api/llm/providers/openai/models": (200, ["models": [["id": "cloud-test"]], "source": "fallback", "warning": "unavailable"]),
         "/api/llm/providers/openai/keys": (200, ["keys": [["label": "work", "fingerprint": "sha256:fixture", "is_active": true, "last_probe_ok": false, "api_key": "SHOULD_NEVER_DISPLAY"]]])]
    }
    @MainActor static func make(_ url: URL? = base) -> NativeProvidersModel { let configuration = URLSessionConfiguration.ephemeral; configuration.protocolClasses = [ProviderWire.self]; return NativeProvidersModel(baseURL: url, session: URLSession(configuration: configuration)) }
    @MainActor static func waitFor(_ path: String) async throws { for _ in 0..<100 { if ProviderWire.count(path) > 0 { return }; try await Task.sleep(nanoseconds: 10_000_000) }; throw ProviderAssertion(message: "Held request missing") }
    @MainActor static func main() async {
        do {
            ProviderWire.reset(fixtures); let unavailable = make(nil); await unavailable.refresh()
            try check(ProviderWire.count() == 0 && unavailable.error != nil, "nil base cannot call API or claim availability")
            print("PASS unavailable connection makes zero requests")

            ProviderWire.reset(fixtures); let model = make(); await model.refresh()
            try check(ProviderWire.passiveOnly() && model.models == ["local-test"], "refresh uses passive cached discovery and deduplicates actual models")
            try check(model.runtimeState.contains("unavailable") && model.health[0].contains("45s") && model.health[0].contains("failed"), "configured catalog must not fake runtime/probe availability")
            await model.select("openai")
            try check(model.keys.count == 1 && model.keys[0].fingerprint == "sha256:fixture" && !String(describing: model.keys).contains("SHOULD_NEVER_DISPLAY"), "key metadata must exclude returned secret")
            print("PASS passive discovery, truthful health, and secret-free key metadata")

            ProviderWire.reset(fixtures); let activate = make(); await activate.refresh(); await activate.select("openai"); activate.model = "cloud-test"; activate.fallbacks = ""
            ProviderWire.set("/api/llm/config", ["success": true, "provider": "openai", "reconfigured": ["ok": false, "available": false], "persisted": ["ok": true]])
            _ = await activate.execute(activate.review(.activate, secret: "test-secret"))
            let body = ProviderWire.body("/api/llm/config")
            try check(body["api_key"] as? String == "test-secret" && (body["fallback_providers"] as? [String]) == [] && body["base_url"] as? String == "", "activation must use actual secret POST and explicit no-fallback/default URL semantics")
            try check(activate.notice?.contains("not confirmed") == true && !(activate.notice ?? "").contains("test-secret"), "save success must not imply runtime activation")
            print("PASS activation wire, explicit fallback policy, and partial failure")

            ProviderWire.reset(fixtures); let unknown = make(); await unknown.refresh(); unknown.fallbacks = "missing-provider"
            _ = await unknown.execute(unknown.review(.activate))
            try check(ProviderWire.count("/api/llm/config") == 1 && unknown.error?.contains("Unknown fallback") == true, "unknown fallback must fail before mutation")
            unknown.endpoint = "https://name:password@api.example/v1"
            _ = await unknown.execute(unknown.review(.configure))
            try check(ProviderWire.count("/api/llm/providers/ollama/configure") == 0 && !(unknown.error ?? "").contains("password"), "embedded endpoint credentials must not dispatch or echo")
            print("PASS invalid fallback and credential URL rejected before mutation")

            ProviderWire.reset(fixtures); let save = make(); await save.refresh(); await save.select("openai")
            ProviderWire.set("/api/llm/providers/openai/configure", ["success": true, "persisted": ["ok": false]])
            _ = await save.execute(save.review(.configure, secret: "temporary-test-secret"))
            try check(save.notice?.contains("persistence failed") == true && ProviderWire.count("/api/llm/config") == 2, "save-only action must report persistence failure and not issue activation POST")
            ProviderWire.set("/api/llm/providers/openai/keys", ["detail": "echo temporary-test-secret"], status: 422)
            _ = await save.execute(save.review(.addKey, label: "work", secret: "temporary-test-secret"))
            try check(save.error?.contains("422") == true && !(save.error ?? "").contains("temporary-test-secret"), "credential HTTP error must not echo submitted or returned secret")
            print("PASS save-only persistence failure and credential error redaction")

            ProviderWire.reset(fixtures); let key = make(); await key.refresh(); await key.select("openai")
            ProviderWire.set("/api/llm/providers/openai/keys", ["success": true])
            _ = await key.execute(key.review(.addKey, label: "new-key", secret: "fixture-only"))
            let keyBody = ProviderWire.body("/api/llm/providers/openai/keys")
            try check(keyBody["set_active"] as? Bool == false && keyBody["label"] as? String == "new-key" && ProviderWire.count("/api/llm/providers/openai/probe") == 0, "adding key must not autoactivate or probe")
            print("PASS labeled key save never grants automatic activation/probe")

            ProviderWire.reset(fixtures); let probe = make(); await probe.refresh()
            ProviderWire.set("/api/llm/providers/ollama/probe", ["reachable": false, "error": ""])
            _ = await probe.execute(probe.review(.probe))
            try check(probe.notice?.contains("unreachable") == true, "failed reachability probe must remain failure")
            print("PASS explicit failed provider probe truthfulness")

            ProviderWire.reset(fixtures); let operations = make(); await operations.refresh(); await operations.select("openai")
            ProviderWire.set("/api/llm/providers/openai/keys/active", ["success": true, "active_label": "work", "reconfigured": ["ok": false]])
            _ = await operations.execute(operations.review(.activateKey("work")))
            try check(ProviderWire.body("/api/llm/providers/openai/keys/active")["label"] as? String == "work" && operations.notice?.contains("not confirmed") == true, "active key change must not hide runtime failure")
            ProviderWire.set("/api/llm/providers/openai/keys/work/probe", ["ok": false, "api_key": "SHOULD_NEVER_DISPLAY"])
            _ = await operations.execute(operations.review(.probeKey("work")))
            try check(operations.notice == "Selected key probe failed.", "failed key probe must not expose secret metadata or imply success")
            ProviderWire.set("/api/llm/providers/openai/keys/work", ["success": true])
            _ = await operations.execute(operations.review(.deleteKey("work")))
            try check(operations.notice?.contains("does not revoke") == true && operations.notice?.contains("runtime may retain") == true, "key deletion cannot promise upstream/runtime credential revocation")
            ProviderWire.set("/api/llm/cooldowns/reset", ["ok": true, "cleared": 1])
            _ = await operations.execute(operations.review(.resetCooldown))
            try check(ProviderWire.body("/api/llm/cooldowns/reset")["provider"] as? String == "openai" && operations.notice?.contains("does not fix") == true, "cooldown reset must target reviewed provider without implying billing repair")
            print("PASS active key, key probe, removal, and targeted cooldown contracts")

            ProviderWire.reset(fixtures); ProviderWire.hold("/api/llm/providers")
            let stale = make(); let old = Task { await stale.refresh() }; try await waitFor("/api/llm/providers")
            stale.configure(baseURL: nil); ProviderWire.release(); await old.value
            try check(stale.providers.isEmpty && stale.error == nil && ProviderWire.count() == 1, "late catalog must not overwrite reset connection or issue subsequent requests")
            print("PASS late old-backend discovery is discarded")

            ProviderWire.reset(fixtures); let oldReview = make(); await oldReview.refresh(); let reviewed = oldReview.review(.configure)
            oldReview.configure(baseURL: nil); oldReview.configure(baseURL: base); await oldReview.refresh()
            _ = await oldReview.execute(reviewed)
            try check(ProviderWire.count("/api/llm/providers/ollama/configure") == 0, "reconnected backend cannot reuse prior confirmation")
            print("PASS confirmation binds to backend generation")

            ProviderWire.reset(fixtures); let lateAction = make(); await lateAction.refresh()
            ProviderWire.set("/api/llm/providers/ollama/configure", ["success": true, "persisted": ["ok": true]])
            ProviderWire.hold("/api/llm/providers/ollama/configure")
            let action = Task { await lateAction.execute(lateAction.review(.configure)) }
            try await waitFor("/api/llm/providers/ollama/configure")
            let calls = ProviderWire.count(); lateAction.configure(baseURL: nil); ProviderWire.release(); let changed = await action.value
            try check(!changed && lateAction.notice == nil && lateAction.error == nil && !lateAction.busy && ProviderWire.count() == calls, "late action receipt must not mutate or refresh new backend")
            print("PASS late old-backend mutation receipt is discarded")
            print("NATIVE_PROVIDERS_TESTS_PASSED: 11 fixture groups; no real provider, vault or keychain calls")
        } catch { fputs("NATIVE_PROVIDERS_TESTS_FAILED: \(error)\n", stderr); exit(1) }
    }
}
