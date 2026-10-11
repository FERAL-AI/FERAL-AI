import SwiftUI
import Foundation

struct NativeProviderChoice: Identifiable, Equatable {
    let id: String, name: String, defaultModel: String, defaultURL: String, note: String
    let needsKey: Bool, configured: Bool, chatReady: Bool
    let reachable: Bool?
    let runtimeSupported: Bool?, setupSelectable: Bool?
    var automaticChoice: Bool { runtimeSupported == true && setupSelectable == true }
}
struct NativeProviderKey: Identifiable, Equatable {
    let id: String, fingerprint: String
    let active: Bool
    let probe: Bool?
}
enum NativeProviderOperation { case configure, activate, refreshModels, probe, addKey, activateKey(String), deleteKey(String), probeKey(String), resetCooldown }
struct NativeProviderReview {
    let id = UUID()
    let createdUptime: TimeInterval
    let origin: URL?
    let connection: UUID, provider: String, model: String, endpoint: String, fallbacks: [String], label: String, secret: String
    let operation: NativeProviderOperation
    let config: [String: Any], descriptor: NativeProviderChoice?, keys: [NativeProviderKey]
    let vault: [String: Any]
}
private struct ProviderFeatureError: LocalizedError { let message: String; var errorDescription: String? { message } }
private func providerBool(_ value: Any?) -> Bool? {
    guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else { return nil }; return number.boolValue
}
private final class NativeProviderRedirectGuard: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
    static func session() -> URLSession { let configuration = URLSessionConfiguration.ephemeral; configuration.timeoutIntervalForResource = 180; return URLSession(configuration: configuration, delegate: NativeProviderRedirectGuard(), delegateQueue: nil) }
}

@MainActor final class NativeProvidersModel: ObservableObject {
    @Published private(set) var providers: [NativeProviderChoice] = []
    @Published private(set) var models: [String] = []
    @Published private(set) var keys: [NativeProviderKey] = []
    @Published private(set) var selected = ""
    @Published private(set) var activeProvider = ""
    @Published private(set) var activeModel = ""
    @Published private(set) var runtimeState = "Not confirmed"
    @Published private(set) var modelsSource = "Not loaded"
    @Published private(set) var health: [String] = []
    @Published private(set) var keysError: String?
    @Published private(set) var error: String?
    @Published private(set) var notice: String?
    @Published private(set) var busy = false
    @Published var model = "" { didSet { if model != oldValue { clearProbe() } } }
    @Published var endpoint = "" { didSet { if endpoint != oldValue { clearProbe() } } }
    @Published var fallbacks = "" { didSet { if fallbacks != oldValue { clearProbe() } } }
    @Published private(set) var lastProbeReachable: Bool?
    private var baseURL: URL?
    private var generation = UUID()
    private var cachedConfig: [String: Any] = [:]
    private var cachedVault: [String: Any] = [:]
    private var usedReviews = Set<UUID>()
    private var probeConfig: [String: Any] = [:]
    private var probeDescriptor: NativeProviderChoice?
    var probeStatusText: String {
        if let verdict = lastProbeReachable { return "Last explicit probe: \(verdict ? "reachable" : "unreachable"). Model inference is not verified by this probe." }
        return providers.first(where: { $0.id == selected })?.reachable.map { $0 ? "Backend cached status: reachable" : "Backend cached status: unreachable" } ?? "No probe result available in this view."
    }
    private func clearProbe() { lastProbeReachable = nil; probeConfig = [:]; probeDescriptor = nil }
    private func sameProbeDescriptor(_ a: NativeProviderChoice, _ b: NativeProviderChoice) -> Bool {
        a.id == b.id && a.name == b.name && a.defaultURL == b.defaultURL && a.note == b.note && a.needsKey == b.needsKey && a.configured == b.configured && a.chatReady == b.chatReady && a.runtimeSupported == b.runtimeSupported && a.setupSelectable == b.setupSelectable
    }
    private func reconcileProbe() {
        guard lastProbeReachable != nil else { return }
        guard let descriptor = probeDescriptor, descriptor.id == selected, sameJSON(cachedConfig, probeConfig), let current = providers.first(where: { $0.id == selected }), sameProbeDescriptor(current, descriptor) else { clearProbe(); return }
    }
    private let uptime: () -> TimeInterval
    private let session: URLSession
    init(baseURL: URL?, session: URLSession? = nil, uptime: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime }) { self.baseURL = baseURL; self.session = session ?? NativeProviderRedirectGuard.session(); self.uptime = uptime }
    func configure(baseURL: URL?) {
        guard self.baseURL != baseURL else { return }; clearProbe(); self.baseURL = baseURL; generation = UUID()
        providers = []; models = []; keys = []; selected = ""; activeProvider = ""; activeModel = ""; runtimeState = "Not confirmed"
        model = ""; endpoint = ""; fallbacks = ""; cachedConfig = [:]; cachedVault = [:]; usedReviews = []; modelsSource = "Not loaded"; health = []; keysError = nil; error = nil; notice = nil; busy = false
    }
    private func request(_ path: String, method: String = "GET", body: [String: Any]? = nil, query: [URLQueryItem] = [], probeProvider: String? = nil) async throws -> [String: Any] {
        try Task.checkCancellation(); let started = generation
        guard let baseURL, baseURL.user == nil, baseURL.password == nil, baseURL.query == nil, baseURL.fragment == nil, ["127.0.0.1", "localhost", "::1", "[::1]"].contains(baseURL.host ?? ""), baseURL.scheme == "http" || baseURL.scheme == "https" else { throw ProviderFeatureError(message: "Connect to the local agent to configure providers.") }
        var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)!
        components.path = path; components.queryItems = query.isEmpty ? nil : query
        guard let url = components.url else { throw ProviderFeatureError(message: "Invalid provider request.") }
        var request = URLRequest(url: url); request.httpMethod = method; request.timeoutInterval = 180
        if let body { request.setValue("application/json", forHTTPHeaderField: "Content-Type"); request.httpBody = try JSONSerialization.data(withJSONObject: body) }
        let data: Data; let response: URLResponse
        do { (data, response) = try await session.feralLocalData(for: request) }
        catch { throw ProviderFeatureError(message: "Provider request could not be completed. Refresh to confirm state before retrying.") }
        try Task.checkCancellation()
        guard generation == started else { throw ProviderFeatureError(message: "Agent connection changed. Review again.") }
        guard data.count <= 524288, let http = response as? HTTPURLResponse, http.url == url else { throw ProviderFeatureError(message: "Invalid provider response.") }
        // Never render arbitrary backend diagnostics: credential routes can
        // echo submitted secrets in validation errors or upstream exceptions.
        guard (200..<300).contains(http.statusCode) else { throw ProviderFeatureError(message: "HTTP \(http.statusCode). Provider action is not confirmed. Refresh before retrying.") }
        guard let value = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { throw ProviderFeatureError(message: "Provider response is incomplete.") }
        if let probeProvider {
            guard providerBool(value["reachable"]) != nil, value["id"] != nil || value["provider_id"] != nil else { throw ProviderFeatureError(message: "Provider probe did not identify the reviewed provider.") }
            for field in ["id", "provider_id"] { if let actual = value[field] { guard actual as? String == probeProvider else { throw ProviderFeatureError(message: "Provider probe did not identify the reviewed provider.") } } }
            if let issue = value["error"], !(issue is NSNull) {
                guard let detail = issue as? String, detail.utf8.count <= 4096, providerBool(value["reachable"]) == false || detail.isEmpty else { throw ProviderFeatureError(message: "Provider probe status is inconsistent. Private details are withheld.") }
            }
        } else if let issue = value["error"] as? String, !issue.isEmpty { throw ProviderFeatureError(message: "Provider reported an error. Refresh to confirm state.") }
        return value
    }
    private func safeID(_ value: String) throws -> String {
        guard !value.isEmpty, value.utf8.count <= 80, value.unicodeScalars.allSatisfy({ CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_.")).contains($0) }) else { throw ProviderFeatureError(message: "Invalid provider or key label. Use bounded letters, numbers, dash, underscore or dot.") }; return value
    }
    private func clean(_ value: String, secret: String = "") -> String { secret.isEmpty ? value : value.replacingOccurrences(of: secret, with: "[redacted]") }
    private func configSnapshot(_ raw: [String: Any]) throws -> [String: Any] {
        guard let provider = raw["provider"] as? String, let model = raw["model"] as? String,
              let endpoint = raw["base_url"] as? String, let fallbacks = raw["fallback_providers"] as? [String],
              provider.utf8.count <= 80, model.utf8.count <= 256, endpoint.utf8.count <= 2048, fallbacks.count <= 20 else { throw ProviderFeatureError(message: "Saved provider configuration is unsupported.") }
        var result: [String: Any] = ["provider": provider, "model": model, "base_url": endpoint, "fallback_providers": fallbacks]
        if raw["configured"] != nil { guard let configured = providerBool(raw["configured"]) else { throw ProviderFeatureError(message: "Saved credential presence is unsupported.") }; result["configured"] = configured }
        return result
    }
    private func sameJSON(_ a: [String: Any], _ b: [String: Any]) -> Bool {
        guard let x = try? JSONSerialization.data(withJSONObject: a, options: .sortedKeys), let y = try? JSONSerialization.data(withJSONObject: b, options: .sortedKeys) else { return false }; return x == y
    }
    private func decodeProviders(_ raw: [String: Any]) throws -> [NativeProviderChoice] {
        guard let rows = raw["providers"] as? [[String: Any]], rows.count <= 100 else { throw ProviderFeatureError(message: "Provider catalog is incomplete.") }
        var seen = Set<String>()
        return try rows.map { row in
            guard let id = row["id"] as? String, seen.insert(id).inserted else { throw ProviderFeatureError(message: "Invalid provider catalog entry.") }; _ = try safeID(id)
            guard let needsKey = providerBool(row["requires_api_key"]), let configured = providerBool(row["configured"]), let ready = providerBool(row["chat_ready"]) else { throw ProviderFeatureError(message: "Provider catalog flags are unsupported.") }
            for field in ["runtime_supported", "setup_selectable"] {
                if row[field] != nil && providerBool(row[field]) == nil { throw ProviderFeatureError(message: "Provider runtime selection flags are unsupported.") }
            }
            return NativeProviderChoice(id: id, name: row["display_name"] as? String ?? id, defaultModel: row["default_model"] as? String ?? "", defaultURL: row["default_base_url"] as? String ?? "", note: row["stub_reason"] as? String ?? "", needsKey: needsKey, configured: configured, chatReady: ready, reachable: providerBool(row["reachable"]), runtimeSupported: providerBool(row["runtime_supported"]), setupSelectable: providerBool(row["setup_selectable"]))
        }
    }
    private func keySnapshot(_ raw: [String: Any]) throws -> [NativeProviderKey] {
        guard let rows = raw["keys"] as? [[String: Any]], rows.count <= 100 else { throw ProviderFeatureError(message: "Key metadata is incomplete.") }; var seen = Set<String>()
        return try rows.map { row in
            guard let label = row["label"] as? String, seen.insert(label).inserted, let active = providerBool(row["is_active"]) else { throw ProviderFeatureError(message: "Invalid key metadata.") }; _ = try safeID(label)
            return NativeProviderKey(id: label, fingerprint: row["fingerprint"] as? String ?? "", active: active, probe: providerBool(row["last_probe_ok"]))
        }.sorted { $0.id < $1.id }
    }
    private func vaultSnapshot(_ raw: [String: Any]) throws -> [String: Any] {
        guard let state = raw["state"] as? String, state.utf8.count <= 80, let available = providerBool(raw["credentials_available"]), let inFlight = providerBool(raw["in_flight"]) else { throw ProviderFeatureError(message: "Credential storage state is unsupported.") }
        return ["state": state, "credentials_available": available, "in_flight": inFlight]
    }
    private func sameDraft(_ item: NativeProviderReview) -> Bool {
        let age = uptime() - item.createdUptime
        return item.connection == generation && item.origin == baseURL && item.provider == selected && item.model == model && item.endpoint == endpoint && item.fallbacks == fallbacks.split(separator: ",").map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty } && age >= 0 && age <= 300
    }
    func canUse(_ item: NativeProviderReview) -> Bool { !busy && sameDraft(item) && !usedReviews.contains(item.id) && usedReviews.count < 1000 && !item.config.isEmpty && item.descriptor.map { providers.contains($0) } == true }
    func refresh(resetSelection: Bool = false) async {
        guard !busy else { return }
        generation = UUID(); usedReviews = []
        if resetSelection {
            clearProbe()
            generation = UUID(); selected = ""; model = ""; endpoint = ""; fallbacks = ""; cachedConfig = [:]; cachedVault = [:]; usedReviews = []
            activeProvider = ""; activeModel = ""; runtimeState = "Not confirmed"; notice = nil
            models = []; keys = []; keysError = nil; modelsSource = "Not loaded"; health = []
        }
        let started = generation; busy = true; error = nil; notice = nil
        defer { if started == generation { busy = false } }
        do {
            let list = try await request("/api/llm/providers")
            providers = try decodeProviders(list)
            cachedConfig = try configSnapshot(await request("/api/llm/config"))
            reconcileProbe()
            activeProvider = cachedConfig["provider"] as? String ?? ""; activeModel = cachedConfig["model"] as? String ?? ""
            let status = try await request("/api/llm/status")
            runtimeState = providerBool(status["available"]).map { $0 ? "Runtime reports available" : "Runtime reports unavailable" } ?? "Not confirmed"
            if providerBool(status["supported"]) == false { runtimeState = "No runtime adapter reported" }
            try await loadHealth()
            if selected.isEmpty { selected = providers.contains(where: { $0.id == activeProvider }) ? activeProvider : providers.first(where: { $0.automaticChoice })?.id ?? ""; applySelection() }
            if !selected.isEmpty { try await loadDetails(selected) }
        } catch { if started == generation { clearProbe(); self.error = error.localizedDescription; runtimeState = "Not confirmed" } }
    }
    private func applySelection() {
        model = selected == activeProvider ? cachedConfig["model"] as? String ?? "" : providers.first(where: { $0.id == selected })?.defaultModel ?? ""
        endpoint = selected == activeProvider ? cachedConfig["base_url"] as? String ?? "" : ""
        if let url = URLComponents(string: endpoint), url.user != nil || url.password != nil || url.query != nil {
            endpoint = ""; notice = "Saved endpoint contains embedded credentials or a query and was not displayed. Enter a clean endpoint before activating."
        }
        fallbacks = (cachedConfig["fallback_providers"] as? [String] ?? []).joined(separator: ", ")
        models = []; keys = []; cachedVault = [:]; keysError = nil; modelsSource = "Not loaded"
    }
    private func loadHealth() async throws {
        let value = try await request("/api/llm/health")
        guard let rows = value["candidates"] as? [[String: Any]] else { throw ProviderFeatureError(message: "Provider health snapshot is incomplete.") }
        health = rows.map { row in
            let id = row["provider"] as? String ?? "Unknown provider"
            let model = row["model"] as? String ?? ""
            let remaining = (row["cooldown_remaining"] as? NSNumber)?.doubleValue ?? 0
            let seconds = remaining.isFinite ? Int(max(0, min(remaining, 86_400_000))) : 0
            let cooldown = providerBool(row["in_cooldown"]) == true ? "cooldown \(seconds)s" : "not in cooldown"
            let probe = providerBool(row["probe_ok"]).map { $0 ? "cached probe passed" : "cached probe failed" } ?? "not probed"
            return "\(id) · \(model) · \(cooldown) · \(probe)"
        }
    }
    func select(_ id: String) async {
        guard !busy, providers.contains(where: { $0.id == id }) else { return }
        clearProbe()
        generation = UUID(); usedReviews = []
        selected = id; applySelection(); let started = generation; busy = true; error = nil
        defer { if started == generation { busy = false } }
        do { try await loadDetails(id) } catch { if started == generation { self.error = error.localizedDescription } }
    }
    private func loadDetails(_ id: String) async throws {
        let started = generation
        let value = try await request("/api/llm/providers/\(try safeID(id))/models", query: [URLQueryItem(name: "live", value: "false"), URLQueryItem(name: "recommended", value: "true"), URLQueryItem(name: "model_class", value: "chat")])
        let parsed = try modelCatalog(value, provider: id)
        models = parsed.ids
        modelsSource = parsed.source
        if !(value["warning"] as? String ?? "").isEmpty { modelsSource += " · backend reports a discovery warning" }
        if providers.first(where: { $0.id == id })?.needsKey == true {
            do {
                let result = try await request("/api/llm/providers/\(try safeID(id))/keys")
                keys = try keySnapshot(result); keysError = nil
            } catch { if started == generation { keys = []; keysError = error.localizedDescription } }
        }
        do { cachedVault = try vaultSnapshot(await request("/api/security/vault/status")) }
        catch { if started == generation { cachedVault = [:]; notice = (notice.map { $0 + " " } ?? "") + "Credential storage status is unavailable. Credential-free local use remains available; key writes require a fresh authenticated storage readback." } }
    }
    private func modelCatalog(_ raw: [String: Any], provider: String) throws -> (ids: [String], source: String) {
        guard let rows = raw["models"] as? [Any], rows.count <= 2000,
              let source = raw["source"] as? String, ["live", "cache", "fallback"].contains(source),
              raw["provider_id"] == nil || raw["provider_id"] as? String == provider,
              raw["warning"] == nil || (raw["warning"] as? String).map({ $0.utf8.count <= 4096 }) == true else {
            throw ProviderFeatureError(message: "Model catalog is unsupported.")
        }
        // The registered backend returns string IDs. Retain homogeneous
        // object/id rows for historical clients, never silently drop bad rows.
        let ids: [String]
        if let strings = rows as? [String] { ids = strings }
        else if let objects = rows as? [[String: Any]] {
            ids = try objects.map { row in
                guard let id = row["id"] as? String else { throw ProviderFeatureError(message: "Model catalog is unsupported.") }
                return id
            }
        } else { throw ProviderFeatureError(message: "Model catalog is unsupported.") }
        guard ids.allSatisfy({ !$0.isEmpty && $0.utf8.count <= 256 && !$0.unicodeScalars.contains(where: { CharacterSet.whitespacesAndNewlines.union(.controlCharacters).contains($0) }) }) else {
            throw ProviderFeatureError(message: "Model catalog is unsupported.")
        }
        var seen = Set<String>()
        return (ids.filter { seen.insert($0).inserted }, source)
    }
    func review(_ operation: NativeProviderOperation, label: String = "", secret: String = "") -> NativeProviderReview {
        NativeProviderReview(createdUptime: uptime(), origin: baseURL, connection: generation, provider: selected, model: model, endpoint: endpoint, fallbacks: fallbacks.split(separator: ",").map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }, label: label, secret: secret, operation: operation, config: cachedConfig, descriptor: providers.first { $0.id == selected }, keys: keys, vault: cachedVault)
    }
    func execute(_ review: NativeProviderReview) async -> Bool {
        guard canUse(review), let descriptor = review.descriptor else { clearProbe(); error = "Provider review changed, expired or was already used. Refresh and review the action again. No action was sent."; return false }
        clearProbe()
        usedReviews.insert(review.id)
        let started = generation; busy = true; error = nil; notice = nil
        defer { if started == generation { busy = false } }
        var changed = false
        var sent = false
        var probeResult: Bool?
        do {
            let id = try safeID(review.provider); let prefix = "/api/llm/providers/" + id
            if !review.endpoint.isEmpty {
                guard let url = URLComponents(string: review.endpoint), ["http", "https"].contains(url.scheme ?? ""), url.host != nil, url.user == nil, url.password == nil, url.query == nil, url.fragment == nil else { throw ProviderFeatureError(message: "Use an HTTP(S) endpoint without embedded credentials, query or fragment.") }
            }
            if case .activate = review.operation {
                guard descriptor.automaticChoice || !review.endpoint.isEmpty else { throw ProviderFeatureError(message: "This provider has no confirmed runtime adapter. Enter an explicit compatible gateway endpoint before activation; catalogue defaults do not establish support.") }
                guard !review.model.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { throw ProviderFeatureError(message: "Choose or enter a model first.") }
                guard review.fallbacks.allSatisfy({ fallback in providers.contains(where: { $0.id == fallback }) }) else { throw ProviderFeatureError(message: "Unknown fallback provider.") }
            }
            let freshConfig = try configSnapshot(await request("/api/llm/config"))
            let freshProviders = try decodeProviders(await request("/api/llm/providers"))
            guard sameDraft(review), sameJSON(freshConfig, review.config), freshProviders.first(where: { $0.id == id }) == descriptor else { throw ProviderFeatureError(message: "Saved provider settings or catalogue changed. Refresh and review again. No action was sent.") }
            var requiresStorage = !review.secret.isEmpty
            switch review.operation { case .addKey, .activateKey, .deleteKey: requiresStorage = true; default: break }
            if descriptor.needsKey {
                guard keysError == nil else { throw ProviderFeatureError(message: "Public key metadata is unavailable. Refresh before reviewing an action.") }
                let freshKeys = try keySnapshot(await request(prefix + "/keys"))
                guard freshKeys == review.keys else { throw ProviderFeatureError(message: "Public key metadata changed. Refresh and review again. No action was sent.") }
            }
            if descriptor.needsKey || requiresStorage {
                let freshVault = try vaultSnapshot(await request("/api/security/vault/status"))
                guard !review.vault.isEmpty, sameJSON(freshVault, review.vault) else { throw ProviderFeatureError(message: "Credential storage changed. Refresh and review again. No action was sent.") }
                if requiresStorage { guard freshVault["state"] as? String == "ready", providerBool(freshVault["credentials_available"]) == true, providerBool(freshVault["in_flight"]) == false else { throw ProviderFeatureError(message: "Unlock existing encrypted credential storage before reviewing this write. No action was sent.") } }
            }
            try Task.checkCancellation()
            guard sameDraft(review) else { throw ProviderFeatureError(message: "Provider review changed during verification. No action was sent.") }
            switch review.operation {
            case .configure:
                var body: [String: Any] = ["base_url": review.endpoint]; if !review.secret.isEmpty { body["api_key"] = review.secret }
                sent = true; let value = try await request(prefix + "/configure", method: "POST", body: body)
                guard providerBool(value["success"]) == true else { throw ProviderFeatureError(message: "Provider save was not confirmed.") }
                changed = true
                notice = "Provider configuration saved. Choose Activate to apply it to chat."
                if let persisted = value["persisted"] as? [String: Any], providerBool(persisted["ok"]) == false { notice = "Configuration applied in memory, but credential persistence failed. It may not survive restart." }
            case .activate:
                guard !review.model.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { throw ProviderFeatureError(message: "Choose or enter a model first.") }
                guard review.fallbacks.allSatisfy({ fallback in providers.contains(where: { $0.id == fallback }) }) else { throw ProviderFeatureError(message: "Unknown fallback provider.") }
                var body: [String: Any] = ["provider": id, "model": review.model, "base_url": review.endpoint, "fallback_providers": review.fallbacks]
                if !review.secret.isEmpty { body["api_key"] = review.secret }
                sent = true; let value = try await request("/api/llm/config", method: "POST", body: body)
                guard providerBool(value["success"]) == true, value["provider"] as? String == id, value["model"] as? String == review.model else { throw ProviderFeatureError(message: "Provider activation was not confirmed.") }
                changed = true
                let runtime = value["reconfigured"] as? [String: Any] ?? [:]
                notice = providerBool(runtime["ok"]) == true && providerBool(runtime["available"]) == true ? "Configuration saved; runtime reports available." : "Configuration saved, but runtime activation or availability is not confirmed. Refresh status before chatting."
                if let persisted = value["persisted"] as? [String: Any], providerBool(persisted["ok"]) == false { notice! += " Credential persistence failed." }
            case .refreshModels:
                sent = true; let value = try await request(prefix + "/models", query: [URLQueryItem(name: "live", value: "true"), URLQueryItem(name: "force", value: "true"), URLQueryItem(name: "recommended", value: "true"), URLQueryItem(name: "model_class", value: "chat")])
                let parsed = try modelCatalog(value, provider: id)
                models = parsed.ids; modelsSource = parsed.source
                notice = "Model list returned from \(modelsSource)."; if !(value["warning"] as? String ?? "").isEmpty { notice! += " Discovery failed; this may be cached or fallback data." }
            case .probe:
                sent = true; let value = try await request(prefix + "/probe", method: "POST", body: [:], probeProvider: id)
                probeResult = providerBool(value["reachable"])
                notice = providerBool(value["reachable"]).map { $0 ? "Provider probe reports reachable. This is not proof every model supports chat." : "Provider probe reports unreachable." } ?? "Probe did not report reachability."
            case .addKey:
                _ = try safeID(review.label); guard !review.secret.isEmpty else { throw ProviderFeatureError(message: "Enter a key to save.") }
                sent = true; let value = try await request(prefix + "/keys", method: "POST", body: ["label": review.label, "api_key": review.secret, "set_active": false])
                guard providerBool(value["success"]) == true else { throw ProviderFeatureError(message: "Key save was not confirmed.") }; notice = "Labeled key saved without activating or probing it."
            case .activateKey(let label):
                guard keys.contains(where: { $0.id == label }) else { throw ProviderFeatureError(message: "Refresh key metadata before selecting it.") }
                sent = true; let value = try await request(prefix + "/keys/active", method: "POST", body: ["label": label])
                guard providerBool(value["success"]) == true, value["active_label"] as? String == label else { throw ProviderFeatureError(message: "Active key change was not confirmed.") }; changed = true; notice = "Active key label updated."
                if let runtime = value["reconfigured"] as? [String: Any], providerBool(runtime["ok"]) != true { notice! += " Runtime reconfiguration is not confirmed." }
            case .deleteKey(let label):
                guard keys.contains(where: { $0.id == label }) else { throw ProviderFeatureError(message: "Refresh key metadata before removing it.") }
                sent = true; let value = try await request(prefix + "/keys/\(try safeID(label))", method: "DELETE")
                guard providerBool(value["success"]) == true else { throw ProviderFeatureError(message: "Key removal was not confirmed.") }; notice = "Labeled key removed. This does not revoke the upstream key. The runtime may retain its current credential and other legacy credentials may remain configured."; changed = true
            case .probeKey(let label):
                guard keys.contains(where: { $0.id == label }) else { throw ProviderFeatureError(message: "Refresh key metadata before probing it.") }
                sent = true; let value = try await request(prefix + "/keys/\(try safeID(label))/probe", method: "POST", body: [:])
                notice = providerBool(value["ok"]).map { $0 ? "Selected key probe passed." : "Selected key probe failed." } ?? "Key probe verdict is not confirmed."
            case .resetCooldown:
                sent = true; let value = try await request("/api/llm/cooldowns/reset", method: "POST", body: ["provider": id])
                guard providerBool(value["ok"]) == true else { throw ProviderFeatureError(message: "Cooldown reset was not confirmed.") }; notice = "Cooldown reset. The next request may retry; this does not fix credentials or billing."
            }
            notice = clean(notice ?? "", secret: review.secret)
            if changed {
                cachedConfig = try configSnapshot(await request("/api/llm/config"))
                if case .activate = review.operation {
                    guard cachedConfig["provider"] as? String == id, cachedConfig["model"] as? String == review.model, cachedConfig["base_url"] as? String == review.endpoint, cachedConfig["fallback_providers"] as? [String] == review.fallbacks else { throw ProviderFeatureError(message: "Saved provider readback differs from the reviewed activation. No successful activation is claimed.") }
                }
                providers = try decodeProviders(await request("/api/llm/providers"))
                activeProvider = cachedConfig["provider"] as? String ?? ""; activeModel = cachedConfig["model"] as? String ?? ""
                let status = try await request("/api/llm/status")
                runtimeState = providerBool(status["available"]).map { $0 ? "Runtime reports available" : "Runtime reports unavailable" } ?? "Not confirmed"
                if providerBool(status["supported"]) == false { runtimeState = "No runtime adapter reported" }
                try await loadHealth()
            }
            if started == generation { try await loadDetails(id) }
            if let result = probeResult {
                let readback = try configSnapshot(await request("/api/llm/config"))
                let providerReadback = try decodeProviders(await request("/api/llm/providers"))
                guard sameDraft(review), sameJSON(readback, review.config), let current = providerReadback.first(where: { $0.id == id }), sameProbeDescriptor(current, descriptor) else { throw ProviderFeatureError(message: "Provider settings changed during the probe. Its result cannot certify this selection.") }
                cachedConfig = readback; providers = providerReadback
                probeConfig = readback; probeDescriptor = descriptor; lastProbeReachable = result
            }
        } catch { changed = false; if started == generation { notice = nil; self.error = clean(error.localizedDescription, secret: review.secret) + (sent ? " The action may already have taken effect. Refresh saved state before preparing a new review; this review cannot be retried." : "") } }
        return started == generation && changed
    }
}

struct NativeProvidersFeatureView: View {
    let baseURL: URL?
    let onConfigurationChanged: () -> Void
    @StateObject private var model: NativeProvidersModel
    @State private var secret = ""
    @State private var label = ""
    @State private var confirmation: NativeProviderReview?
    @State private var showRouting = false
    init(baseURL: URL?, onConfigurationChanged: @escaping () -> Void) { self.baseURL = baseURL; self.onConfigurationChanged = onConfigurationChanged; _model = StateObject(wrappedValue: NativeProvidersModel(baseURL: baseURL)) }
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                HStack { Text("AI providers").font(.largeTitle.weight(.semibold)); Spacer(); Button("Refresh saved status") { Task { await model.refresh() } }.disabled(model.busy); if model.busy { ProgressView().controlSize(.small) } }
                Button("Provider routing and presets…") { secret = ""; label = ""; confirmation = nil; showRouting = true }.disabled(model.busy || baseURL == nil)
                NativeSelectableText("Active setting: \(model.activeProvider) · \(model.activeModel)")
                Text(model.runtimeState).font(.callout)
                ForEach(Array(model.health.enumerated()), id: \.offset) { _, value in NativeSelectableText(value).font(.caption).foregroundStyle(.secondary) }
                Text("Catalog and cached models load without a live provider request. Probes, live discovery and activation may contact the provider. Coding uses separate model configuration.").font(.callout).foregroundStyle(.secondary)
                if let error = model.error { NativeSelectableText(error).foregroundStyle(.red) }
                if let notice = model.notice { NativeSelectableText(notice) }
                Picker("Provider", selection: Binding(get: { model.selected }, set: { id in secret = ""; label = ""; Task { await model.select(id) } })) {
                    Text("Choose provider").tag(""); ForEach(model.providers) { Text($0.name + ($0.automaticChoice ? "" : " · manual gateway required")).tag($0.id) }
                }.disabled(model.busy)
                if let provider = model.providers.first(where: { $0.id == model.selected }) {
                    Text(provider.configured ? "Credential/configuration present." : "Provider needs configuration.").font(.callout)
                    Text(model.probeStatusText).font(.caption).foregroundStyle(model.lastProbeReachable == false ? .orange : .secondary)
                    if !provider.automaticChoice { Text("A runtime adapter is not confirmed for this catalogue entry. Activating it requires an explicit compatible gateway endpoint; the catalogue endpoint is informational.").foregroundStyle(.orange) }
                    Text("Successful inference, FERAL tool execution and voice are separately verified capabilities.").font(.caption).foregroundStyle(.secondary)
                    if !provider.chatReady { Text("Catalog marks this provider as not chat-ready. \(provider.note)").foregroundStyle(.orange) }
                    TextField("Model identifier", text: $model.model).textFieldStyle(.roundedBorder).disabled(model.busy)
                    Picker("Cached models (\(model.modelsSource))", selection: $model.model) {
                        Text(model.model.isEmpty ? "Enter a model above" : model.model).tag(model.model)
                        ForEach(model.models.filter { $0 != model.model }, id: \.self) { Text($0).tag($0) }
                    }.disabled(model.busy)
                    TextField("Endpoint override — blank uses runtime default", text: $model.endpoint).textFieldStyle(.roundedBorder).disabled(model.busy)
                    NativeSelectableText("Catalog endpoint: \(provider.defaultURL.isEmpty ? "not specified" : provider.defaultURL)").font(.caption).foregroundStyle(.secondary)
                    TextField("Fallback provider IDs, comma separated (blank disables fallbacks)", text: $model.fallbacks).textFieldStyle(.roundedBorder).disabled(model.busy)
                    if provider.needsKey { SecureField("New API key (optional; stored by backend vault)", text: $secret).textFieldStyle(.roundedBorder).disabled(model.busy) }
                    HStack { operationButton("Save provider configuration…", .configure); operationButton("Activate for chat…", .activate); operationButton("Probe provider…", .probe); operationButton("Discover live models…", .refreshModels) }
                    operationButton("Reset this provider’s cooldown…", .resetCooldown)
                    if provider.needsKey {
                        Divider(); Text("Labeled credentials").font(.title2)
                        Text("Saving a label replaces an existing key with that label. Removing it does not revoke the upstream key or guarantee the running model stopped using it. Legacy credentials may remain. Secrets are never displayed in returned metadata.").font(.callout).foregroundStyle(.secondary)
                        if let error = model.keysError { Text(error).foregroundStyle(.red) }
                        HStack { TextField("Key label", text: $label).textFieldStyle(.roundedBorder); operationButton("Save labeled key…", .addKey) }.disabled(model.busy)
                        ForEach(model.keys) { key in
                            HStack {
                                VStack(alignment: .leading) { Text(key.id + (key.active ? " · active" : "")); NativeSelectableText("Fingerprint: \(key.fingerprint) · Probe: \(key.probe.map { $0 ? "passed" : "failed" } ?? "not run")").font(.caption) }
                                Spacer(); operationButton("Use key…", .activateKey(key.id)); operationButton("Probe key…", .probeKey(key.id)); operationButton("Remove…", .deleteKey(key.id))
                            }
                        }
                    }
                }
            }.padding(28).frame(maxWidth: 1050, alignment: .leading)
        }
        .task(id: baseURL) { secret = ""; confirmation = nil; showRouting = false; model.configure(baseURL: baseURL); await model.refresh() }
        .sheet(isPresented: $showRouting, onDismiss: {
            secret = ""; label = ""; confirmation = nil
            Task { await model.refresh(resetSelection: true); onConfigurationChanged() }
        }) {
            VStack(alignment: .leading, spacing: 0) {
                HStack { Text("Routing and presets").font(.headline); Spacer(); Button("Done") { showRouting = false }.keyboardShortcut(.cancelAction).accessibilityLabel("Close routing and presets") }.padding()
                Divider()
                NativeProviderRoutingFeatureView(baseURL: baseURL)
            }.frame(width: 900, height: 760)
        }
        .confirmationDialog("Confirm provider action", isPresented: Binding(get: { confirmation != nil }, set: { if !$0 { confirmation = nil } }), titleVisibility: .visible) {
            if let review = confirmation { Button("Continue") { confirmation = nil; secret = ""; Task { if await model.execute(review) { onConfigurationChanged() } } }.disabled(!model.canUse(review)); Button("Cancel", role: .cancel) { confirmation = nil } }
        } message: {
            if let review = confirmation { Text("Local service: \(review.origin?.absoluteString ?? "unavailable")\nProvider: \(review.provider)\nModel: \(review.model)\nEndpoint: \(review.endpoint.isEmpty ? "runtime default" : review.endpoint)\nFallbacks: \(review.fallbacks.joined(separator: ", "))\nKey label: \(review.label.isEmpty ? "none entered" : review.label)\n\(operationDescription(review.operation))") }
        }
    }
    private func operationButton(_ title: String, _ operation: NativeProviderOperation) -> some View { Button(title) { confirmation = model.review(operation, label: label, secret: secret) }.disabled(model.busy || baseURL == nil) }
    private func operationDescription(_ operation: NativeProviderOperation) -> String {
        switch operation {
        case .activate: return "Saves these settings and changes the chat runtime. This may send a small model availability request to the chosen provider. Listed fallbacks are explicitly configured."
        case .configure: return "Saves provider configuration and any entered key. Does not activate this provider for chat."
        case .addKey: return "Stores/replaces this labeled key without activation or automatic probe."
        case .activateKey(let key): return "Selects key label \(key). If this provider is active, runtime credentials change and a model availability request may be sent."
        case .deleteKey(let key): return "Deletes label \(key). It does not revoke the upstream key or guarantee the current runtime stopped using it. Other legacy credentials can remain in use."
        case .probeKey(let key): return "Contacts the provider using key label \(key) to test authentication."
        case .probe: return "Contacts the selected provider using its saved configuration."
        case .refreshModels: return "Contacts the selected provider to refresh its model catalog. Failed discovery can return cached or fallback data."
        case .resetCooldown: return "Allows the next model request to retry this provider. Does not resolve an authentication or billing failure."
        }
    }
}
