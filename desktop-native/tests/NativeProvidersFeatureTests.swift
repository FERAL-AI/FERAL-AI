// Standalone fixture execution; no real provider/vault/keychain operations.
import Foundation

private final class ProviderWire: URLProtocol {
    static let lock = NSLock()
    static var responses: [String: (Int, [String: Any])] = [:]
    static var mutationResponses: [String: (Int, [String: Any])] = [:]
    static var calls: [(URL, String, [String: Any])] = []
    static var heldPaths = Set<String>()
    static var callbacks: [() -> Void] = []
    static var applyActivation = true
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        var bytes = request.httpBody ?? Data()
        if bytes.isEmpty, let stream = request.httpBodyStream { stream.open(); defer { stream.close() }; var buffer = [UInt8](repeating: 0, count: 4096); while stream.hasBytesAvailable { let count = stream.read(&buffer, maxLength: buffer.count); if count <= 0 { break }; bytes.append(buffer, count: count) } }
        let body = (try? JSONSerialization.jsonObject(with: bytes)) as? [String: Any] ?? [:]
        Self.lock.lock(); let path = request.url!.path, method = request.httpMethod ?? "GET"; Self.calls.append((request.url!, method, body)); let reply = method == "GET" ? Self.responses[path] ?? (200, [:]) : Self.mutationResponses[path] ?? Self.responses[path] ?? (200, [:])
        if Self.applyActivation, method == "POST", path == "/api/llm/config", reply.0 == 200, reply.1["success"] as? Bool == true, body["provider"] != nil, body["model"] != nil {
            Self.responses[path] = (200, ["provider": body["provider"]!, "model": body["model"]!, "base_url": body["base_url"] ?? "", "fallback_providers": body["fallback_providers"] ?? []])
        }
        let respond = { [self] in client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: reply.0, httpVersion: nil, headerFields: ["Content-Type": "application/json"])!, cacheStoragePolicy: .notAllowed); client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: reply.1)); client?.urlProtocolDidFinishLoading(self) }
        let held = Self.heldPaths.contains(path); if held { Self.callbacks.append(respond) }; Self.lock.unlock(); if !held { respond() }
    }
    override func stopLoading() {}
    static func reset(_ values: [String: (Int, [String: Any])]) { lock.lock(); defer { lock.unlock() }; responses = values; mutationResponses = [:]; calls = []; heldPaths = []; callbacks = []; applyActivation = true }
    static func set(_ path: String, _ value: [String: Any], status: Int = 200) { lock.lock(); defer { lock.unlock() }; responses[path] = (status, value) }
    static func setMutation(_ path: String, _ value: [String: Any], status: Int = 200) { lock.lock(); defer { lock.unlock() }; mutationResponses[path] = (status, value) }
    static func count(_ path: String? = nil) -> Int { lock.lock(); defer { lock.unlock() }; return calls.filter { path == nil || $0.0.path == path }.count }
    static func body(_ path: String) -> [String: Any] { lock.lock(); defer { lock.unlock() }; return calls.last(where: { $0.0.path == path && $0.1 != "GET" })?.2 ?? [:] }
    static func mutations() -> Int { lock.lock(); defer { lock.unlock() }; return calls.filter { $0.1 != "GET" }.count }
    static func passiveOnly() -> Bool { lock.lock(); defer { lock.unlock() }; return calls.allSatisfy { $0.1 == "GET" && !$0.0.path.contains("probe") && (!($0.0.path.hasSuffix("/models")) || URLComponents(url: $0.0, resolvingAgainstBaseURL: false)?.queryItems?.contains(where: { $0.name == "live" && $0.value == "false" }) == true) } }
    static func hold(_ path: String) { lock.lock(); defer { lock.unlock() }; heldPaths.insert(path) }
    static func release() { lock.lock(); let values = callbacks; callbacks = []; heldPaths = []; lock.unlock(); values.forEach { $0() } }
}
private struct ProviderAssertion: Error { let message: String }
private var assertionCount = 0
private func check(_ condition: @autoclosure () -> Bool, _ message: String) throws { assertionCount += 1; if !condition() { throw ProviderAssertion(message: message) } }

@main struct NativeProvidersFeatureTests {
    static let base = URL(string: "http://127.0.0.1:9465")!
    static var fixtures: [String: (Int, [String: Any])] {
        ["/api/llm/providers": (200, ["providers": [["id": "ollama", "display_name": "Ollama", "requires_api_key": false, "configured": true, "chat_ready": true], ["id": "openai", "display_name": "OpenAI", "requires_api_key": true, "configured": true, "chat_ready": true]]]),
         "/api/llm/config": (200, ["provider": "ollama", "model": "local-test", "base_url": "http://127.0.0.1:11435/v1", "fallback_providers": []]),
         "/api/llm/status": (200, ["available": false, "supported": true]),
         "/api/llm/health": (200, ["candidates": [["provider": "ollama", "model": "local-test", "in_cooldown": true, "cooldown_remaining": 45, "probe_ok": false]]]),
         "/api/llm/providers/ollama/models": (200, ["models": [["id": "local-test"], ["id": "local-test"]], "source": "cache"]),
         "/api/llm/providers/openai/models": (200, ["models": [["id": "cloud-test"]], "source": "fallback", "warning": "unavailable"]),
         "/api/llm/providers/openai/keys": (200, ["keys": [["label": "work", "fingerprint": "sha256:fixture", "is_active": true, "last_probe_ok": false, "api_key": "SHOULD_NEVER_DISPLAY"]]]),
         "/api/security/vault/status": (200, ["state": "ready", "credentials_available": true, "in_flight": false])]
    }
    @MainActor static func make(_ url: URL? = base, uptime: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime }) -> NativeProvidersModel { let configuration = URLSessionConfiguration.ephemeral; configuration.protocolClasses = [ProviderWire.self]; return NativeProvidersModel(baseURL: url, session: URLSession(configuration: configuration), uptime: uptime) }
    @MainActor static func waitForCount(_ path: String, _ count: Int) async throws { for _ in 0..<100 { if ProviderWire.count(path) >= count { return }; try await Task.sleep(nanoseconds: 10_000_000) }; throw ProviderAssertion(message: "Held request count missing") }
    @MainActor static func waitFor(_ path: String) async throws { for _ in 0..<100 { if ProviderWire.count(path) > 0 { return }; try await Task.sleep(nanoseconds: 10_000_000) }; throw ProviderAssertion(message: "Held request missing") }
    @MainActor static func main() async {
        do {
            ProviderWire.reset(fixtures); let draftGuard = make(); await draftGuard.refresh()
            ProviderWire.set("/api/llm/providers/ollama/configure", ["success": true, "persisted": ["ok": true]])
            let staleDraft = draftGuard.review(.configure)
            draftGuard.endpoint = "http://127.0.0.1:12345/v1"
            _ = await draftGuard.execute(staleDraft)
            let staleDraftWrites = ProviderWire.count("/api/llm/providers/ollama/configure")

            ProviderWire.reset(fixtures); let replayGuard = make(); await replayGuard.refresh()
            ProviderWire.set("/api/llm/providers/ollama/configure", ["success": true, "persisted": ["ok": true]])
            let once = replayGuard.review(.configure)
            _ = await replayGuard.execute(once); _ = await replayGuard.execute(once)
            let repeatedWrites = ProviderWire.count("/api/llm/providers/ollama/configure")

            ProviderWire.reset(fixtures); let configGuard = make(); await configGuard.refresh()
            ProviderWire.set("/api/llm/providers/ollama/configure", ["success": true, "persisted": ["ok": true]])
            let drifted = configGuard.review(.configure)
            ProviderWire.set("/api/llm/config", ["provider": "ollama", "model": "changed-model", "base_url": "http://127.0.0.1:11435/v1", "fallback_providers": []])
            _ = await configGuard.execute(drifted)
            let driftWrites = ProviderWire.count("/api/llm/providers/ollama/configure")
            print("PROVIDER_REVIEW_REGRESSION stale_draft_writes=\(staleDraftWrites) repeated_writes=\(repeatedWrites) config_drift_writes=\(driftWrites)")
            try check(staleDraftWrites == 0 && repeatedWrites == 1 && driftWrites == 0, "stale drafts, consumed reviews and changed saved configuration must refuse before write")

            ProviderWire.reset(fixtures); var time: TimeInterval = 100
            let expired = make(uptime: { time }); await expired.refresh(); let timed = expired.review(.probe)
            time = 401; _ = await expired.execute(timed)
            try check(ProviderWire.mutations() == 0 && expired.error?.contains("expired") == true, "expired review cannot contact provider")
            time = 90; _ = await expired.execute(timed)
            try check(ProviderWire.mutations() == 0, "future or non-monotonic review age refuses")
            print("PASS monotonic bounded review expiry")

            ProviderWire.reset(fixtures); let keyDrift = make(); await keyDrift.refresh(); await keyDrift.select("openai")
            let keyed = keyDrift.review(.activateKey("work"))
            ProviderWire.set("/api/llm/providers/openai/keys", ["keys": [["label": "work", "fingerprint": "sha256:changed", "is_active": true, "last_probe_ok": false]]])
            _ = await keyDrift.execute(keyed)
            try check(ProviderWire.mutations() == 0 && keyDrift.error?.contains("metadata changed") == true, "changed public key fingerprint refuses before effect")

            ProviderWire.reset(fixtures); let vaultDrift = make(); await vaultDrift.refresh(); await vaultDrift.select("openai")
            let stored = vaultDrift.review(.configure, secret: "synthetic-secret")
            ProviderWire.set("/api/security/vault/status", ["state": "locked", "credentials_available": false, "in_flight": false])
            _ = await vaultDrift.execute(stored)
            try check(ProviderWire.mutations() == 0 && !(vaultDrift.error ?? "").contains("synthetic-secret"), "vault drift refuses key write without exposing secret")
            print("PASS public key and authenticated storage drift")

            ProviderWire.reset(fixtures); let malformed = make(); await malformed.refresh(); let unsupported = malformed.review(.probe)
            var badCatalogue = fixtures["/api/llm/providers"]!.1
            var rows = badCatalogue["providers"] as! [[String: Any]]; rows[0]["configured"] = 1; badCatalogue["providers"] = rows
            ProviderWire.set("/api/llm/providers", badCatalogue)
            _ = await malformed.execute(unsupported)
            try check(ProviderWire.mutations() == 0 && malformed.error?.contains("flags") == true, "numeric catalogue boolean cannot authorize action")
            print("PASS malformed authoritative flags refuse")

            ProviderWire.reset(fixtures); let during = make(); await during.refresh(); let checked = during.review(.configure)
            let previousCatalogues = ProviderWire.count("/api/llm/providers")
            ProviderWire.hold("/api/llm/providers")
            let deferred = Task { await during.execute(checked) }
            try await waitForCount("/api/llm/providers", previousCatalogues + 1)
            during.fallbacks = "openai"; ProviderWire.release(); _ = await deferred.value
            try check(ProviderWire.mutations() == 0, "draft edited during preflight refuses before dispatch")
            print("PASS asynchronous draft drift")

            ProviderWire.reset(fixtures); let uncertain = make(); await uncertain.refresh()
            ProviderWire.set("/api/llm/providers/ollama/configure", ["private": "synthetic-secret"], status: 503)
            let lost = uncertain.review(.configure); _ = await uncertain.execute(lost)
            try check(ProviderWire.mutations() == 1 && uncertain.error?.contains("may already have taken effect") == true && !(uncertain.error ?? "").contains("synthetic-secret"), "uncertain dispatched write stays explicit and redacted")
            _ = await uncertain.execute(lost)
            try check(ProviderWire.mutations() == 1, "uncertain write cannot replay the same review")
            print("PASS uncertain effect and single-use refusal")

            ProviderWire.reset(fixtures); let mismatch = make(); await mismatch.refresh(); mismatch.model = "reviewed-model"
            ProviderWire.setMutation("/api/llm/config", ["success": true, "provider": "ollama", "model": "reviewed-model", "reconfigured": ["ok": true, "available": true]])
            ProviderWire.applyActivation = false
            let didChange = await mismatch.execute(mismatch.review(.activate))
            try check(!didChange && mismatch.notice == nil && mismatch.error?.contains("readback differs") == true && ProviderWire.mutations() == 1, "successful POST/runtime availability cannot override mismatched exact saved terms")
            print("PASS independent exact activation readback")

            ProviderWire.reset(fixtures); let blocked = make(); await blocked.refresh(); let paused = blocked.review(.configure)
            try NativeLocalActionGate.shared.pause(origin: base)
            _ = await blocked.execute(paused)
            try check(ProviderWire.mutations() == 0, "central origin pause remains authoritative")
            try NativeLocalActionGate.shared.activate(origin: base)
            _ = await blocked.execute(paused)
            try check(ProviderWire.mutations() == 0, "same-address renewed owner cannot reuse review/client lease")
            print("PASS origin epoch pause and replacement fences")

            ProviderWire.reset(fixtures); let local = make(); await local.refresh()
            ProviderWire.set("/api/security/vault/status", ["state": "unavailable", "credentials_available": false, "in_flight": false])
            await local.refresh(); local.model = "credential-free-local"
            ProviderWire.setMutation("/api/llm/config", ["success": true, "provider": "ollama", "model": "credential-free-local", "reconfigured": ["ok": true, "available": false]])
            let localChanged = await local.execute(local.review(.activate))
            try check(localChanged && ProviderWire.body("/api/llm/config")["api_key"] == nil && local.activeModel == "credential-free-local", "credential-free local activation does not require vault initialization")
            print("PASS actual local-no-credential configuration contract")

            ProviderWire.reset(fixtures); let switching = make(); await switching.refresh(); let abandoned = switching.review(.probe)
            await switching.select("openai"); await switching.select("ollama"); _ = await switching.execute(abandoned)
            try check(ProviderWire.mutations() == 0, "switching away and back invalidates an unconsumed review")
            print("PASS provider selection review revocation")

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
            ProviderWire.setMutation("/api/llm/config", ["success": true, "provider": "openai", "model": "cloud-test", "reconfigured": ["ok": false, "available": false], "persisted": ["ok": true]])
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
            try check(save.notice?.contains("persistence failed") == true && ProviderWire.count("/api/llm/config") == 3 && ProviderWire.body("/api/llm/config").isEmpty, "save-only action must report persistence failure and not issue activation POST")
            ProviderWire.setMutation("/api/llm/providers/openai/keys", ["detail": "echo temporary-test-secret"], status: 422)
            _ = await save.execute(save.review(.addKey, label: "work", secret: "temporary-test-secret"))
            try check(save.error?.contains("422") == true && !(save.error ?? "").contains("temporary-test-secret"), "credential HTTP error must not echo submitted or returned secret")
            print("PASS save-only persistence failure and credential error redaction")

            ProviderWire.reset(fixtures); let key = make(); await key.refresh(); await key.select("openai")
            ProviderWire.setMutation("/api/llm/providers/openai/keys", ["success": true])
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
            print("NATIVE_PROVIDERS_TESTS_PASSED: 21 fixture groups; \(assertionCount) assertions; no real provider, vault or keychain calls")
        } catch { fputs("NATIVE_PROVIDERS_TESTS_FAILED: \(error)\n", stderr); exit(1) }
    }
}
