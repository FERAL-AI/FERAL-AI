import SwiftUI
import Foundation

struct NativeProviderChoice: Identifiable {
    let id: String, name: String, defaultModel: String, defaultURL: String, note: String
    let needsKey: Bool, configured: Bool, chatReady: Bool
    let reachable: Bool?
}
struct NativeProviderKey: Identifiable {
    let id: String, fingerprint: String
    let active: Bool
    let probe: Bool?
}
enum NativeProviderOperation { case configure, activate, refreshModels, probe, addKey, activateKey(String), deleteKey(String), probeKey(String), resetCooldown }
struct NativeProviderReview {
    let connection: UUID, provider: String, model: String, endpoint: String, fallbacks: [String], label: String, secret: String
    let operation: NativeProviderOperation
}
private struct ProviderFeatureError: LocalizedError { let message: String; var errorDescription: String? { message } }
private func providerBool(_ value: Any?) -> Bool? {
    guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else { return nil }; return number.boolValue
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
    @Published var model = ""
    @Published var endpoint = ""
    @Published var fallbacks = ""
    private var baseURL: URL?
    private var generation = UUID()
    private var cachedConfig: [String: Any] = [:]
    private let session: URLSession
    init(baseURL: URL?, session: URLSession? = nil) { self.baseURL = baseURL; self.session = session ?? URLSession(configuration: .ephemeral) }
    func configure(baseURL: URL?) {
        guard self.baseURL != baseURL else { return }; self.baseURL = baseURL; generation = UUID()
        providers = []; models = []; keys = []; selected = ""; activeProvider = ""; activeModel = ""; runtimeState = "Not confirmed"
        model = ""; endpoint = ""; fallbacks = ""; cachedConfig = [:]; modelsSource = "Not loaded"; health = []; keysError = nil; error = nil; notice = nil; busy = false
    }
    private func request(_ path: String, method: String = "GET", body: [String: Any]? = nil, query: [URLQueryItem] = []) async throws -> [String: Any] {
        try Task.checkCancellation(); let started = generation
        guard let baseURL, ["127.0.0.1", "localhost", "::1", "[::1]"].contains(baseURL.host ?? ""), baseURL.scheme == "http" || baseURL.scheme == "https" else { throw ProviderFeatureError(message: "Connect to the local agent to configure providers.") }
        var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)!
        components.path = path; components.queryItems = query.isEmpty ? nil : query
        guard let url = components.url else { throw ProviderFeatureError(message: "Invalid provider request.") }
        var request = URLRequest(url: url); request.httpMethod = method; request.timeoutInterval = 180
        if let body { request.setValue("application/json", forHTTPHeaderField: "Content-Type"); request.httpBody = try JSONSerialization.data(withJSONObject: body) }
        let data: Data; let response: URLResponse
        do { (data, response) = try await session.data(for: request) }
        catch { throw ProviderFeatureError(message: "Provider request could not be completed. Refresh to confirm state before retrying.") }
        try Task.checkCancellation()
        guard generation == started else { throw ProviderFeatureError(message: "Agent connection changed. Review again.") }
        guard let http = response as? HTTPURLResponse else { throw ProviderFeatureError(message: "Invalid provider response.") }
        // Never render arbitrary backend diagnostics: credential routes can
        // echo submitted secrets in validation errors or upstream exceptions.
        guard (200..<300).contains(http.statusCode) else { throw ProviderFeatureError(message: "HTTP \(http.statusCode). Provider action is not confirmed. Refresh before retrying.") }
        guard let value = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { throw ProviderFeatureError(message: "Provider response is incomplete.") }
        if let issue = value["error"] as? String, !issue.isEmpty { throw ProviderFeatureError(message: "Provider reported an error. Refresh to confirm state.") }
        return value
    }
    private func safeID(_ value: String) throws -> String {
        guard !value.isEmpty, value.unicodeScalars.allSatisfy({ CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_.")).contains($0) }) else { throw ProviderFeatureError(message: "Invalid provider or key label. Use letters, numbers, dash, underscore or dot.") }; return value
    }
    private func clean(_ value: String, secret: String = "") -> String { secret.isEmpty ? value : value.replacingOccurrences(of: secret, with: "[redacted]") }
    func refresh(resetSelection: Bool = false) async {
        guard !busy else { return }
        if resetSelection {
            generation = UUID(); selected = ""; model = ""; endpoint = ""; fallbacks = ""; cachedConfig = [:]
            activeProvider = ""; activeModel = ""; runtimeState = "Not confirmed"; notice = nil
            models = []; keys = []; keysError = nil; modelsSource = "Not loaded"; health = []
        }
        let started = generation; busy = true; error = nil
        defer { if started == generation { busy = false } }
        do {
            let list = try await request("/api/llm/providers")
            guard let rows = list["providers"] as? [[String: Any]] else { throw ProviderFeatureError(message: "Provider catalog is incomplete.") }
            var seen = Set<String>()
            providers = try rows.map { row in
                guard let id = row["id"] as? String, seen.insert(id).inserted else { throw ProviderFeatureError(message: "Invalid provider catalog entry.") }
                _ = try safeID(id)
                return NativeProviderChoice(id: id, name: row["display_name"] as? String ?? id, defaultModel: row["default_model"] as? String ?? "", defaultURL: row["default_base_url"] as? String ?? "", note: row["stub_reason"] as? String ?? "", needsKey: providerBool(row["requires_api_key"]) ?? true, configured: providerBool(row["configured"]) ?? false, chatReady: providerBool(row["chat_ready"]) ?? false, reachable: providerBool(row["reachable"]))
            }
            cachedConfig = try await request("/api/llm/config")
            activeProvider = cachedConfig["provider"] as? String ?? ""; activeModel = cachedConfig["model"] as? String ?? ""
            let status = try await request("/api/llm/status")
            runtimeState = providerBool(status["available"]).map { $0 ? "Runtime reports available" : "Runtime reports unavailable" } ?? "Not confirmed"
            if providerBool(status["supported"]) == false { runtimeState = "No runtime adapter reported" }
            try await loadHealth()
            if selected.isEmpty { selected = providers.contains(where: { $0.id == activeProvider }) ? activeProvider : providers.first?.id ?? ""; applySelection() }
            if !selected.isEmpty { try await loadDetails(selected) }
        } catch { if started == generation { self.error = error.localizedDescription; runtimeState = "Not confirmed" } }
    }
    private func applySelection() {
        model = selected == activeProvider ? cachedConfig["model"] as? String ?? "" : providers.first(where: { $0.id == selected })?.defaultModel ?? ""
        endpoint = selected == activeProvider ? cachedConfig["base_url"] as? String ?? "" : ""
        if let url = URLComponents(string: endpoint), url.user != nil || url.password != nil || url.query != nil {
            endpoint = ""; notice = "Saved endpoint contains embedded credentials or a query and was not displayed. Enter a clean endpoint before activating."
        }
        fallbacks = (cachedConfig["fallback_providers"] as? [String] ?? []).joined(separator: ", ")
        models = []; keys = []; keysError = nil; modelsSource = "Not loaded"
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
        selected = id; applySelection(); let started = generation; busy = true; error = nil
        defer { if started == generation { busy = false } }
        do { try await loadDetails(id) } catch { if started == generation { self.error = error.localizedDescription } }
    }
    private func loadDetails(_ id: String) async throws {
        let started = generation
        let value = try await request("/api/llm/providers/\(try safeID(id))/models", query: [URLQueryItem(name: "live", value: "false")])
        guard let rows = value["models"] as? [[String: Any]] else { throw ProviderFeatureError(message: "Model catalog is incomplete.") }
        var modelIDs = Set<String>()
        models = rows.compactMap { $0["id"] as? String }.filter { modelIDs.insert($0).inserted }
        modelsSource = value["source"] as? String ?? "Unknown source"
        if !(value["warning"] as? String ?? "").isEmpty { modelsSource += " · backend reports a discovery warning" }
        if providers.first(where: { $0.id == id })?.needsKey == true {
            do {
                let result = try await request("/api/llm/providers/\(try safeID(id))/keys")
                guard let entries = result["keys"] as? [[String: Any]] else { throw ProviderFeatureError(message: "Key metadata is incomplete.") }
                var seen = Set<String>()
                keys = try entries.map { entry in
                    guard let label = entry["label"] as? String, seen.insert(label).inserted else { throw ProviderFeatureError(message: "Invalid key metadata.") }
                    return NativeProviderKey(id: label, fingerprint: entry["fingerprint"] as? String ?? "", active: providerBool(entry["is_active"]) ?? false, probe: providerBool(entry["last_probe_ok"]))
                }; keysError = nil
            } catch { if started == generation { keys = []; keysError = error.localizedDescription } }
        }
    }
    func review(_ operation: NativeProviderOperation, label: String = "", secret: String = "") -> NativeProviderReview {
        NativeProviderReview(connection: generation, provider: selected, model: model, endpoint: endpoint, fallbacks: fallbacks.split(separator: ",").map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }, label: label, secret: secret, operation: operation)
    }
    func execute(_ review: NativeProviderReview) async -> Bool {
        guard !busy, review.connection == generation, review.provider == selected, providers.contains(where: { $0.id == selected }) else { error = "Provider or connection changed. Review the action again."; return false }
        let started = generation; busy = true; error = nil; notice = nil
        defer { if started == generation { busy = false } }
        var changed = false
        do {
            let id = try safeID(review.provider); let prefix = "/api/llm/providers/" + id
            if !review.endpoint.isEmpty {
                guard let url = URLComponents(string: review.endpoint), ["http", "https"].contains(url.scheme ?? ""), url.host != nil, url.user == nil, url.password == nil, url.query == nil, url.fragment == nil else { throw ProviderFeatureError(message: "Use an HTTP(S) endpoint without embedded credentials, query or fragment.") }
            }
            switch review.operation {
            case .configure:
                var body: [String: Any] = ["base_url": review.endpoint]; if !review.secret.isEmpty { body["api_key"] = review.secret }
                let value = try await request(prefix + "/configure", method: "POST", body: body)
                guard providerBool(value["success"]) == true else { throw ProviderFeatureError(message: "Provider save was not confirmed.") }
                changed = true
                notice = "Provider configuration saved. Choose Activate to apply it to chat."
                if let persisted = value["persisted"] as? [String: Any], providerBool(persisted["ok"]) == false { notice = "Configuration applied in memory, but credential persistence failed. It may not survive restart." }
            case .activate:
                guard !review.model.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { throw ProviderFeatureError(message: "Choose or enter a model first.") }
                guard review.fallbacks.allSatisfy({ fallback in providers.contains(where: { $0.id == fallback }) }) else { throw ProviderFeatureError(message: "Unknown fallback provider.") }
                var body: [String: Any] = ["provider": id, "model": review.model, "base_url": review.endpoint, "fallback_providers": review.fallbacks]
                if !review.secret.isEmpty { body["api_key"] = review.secret }
                let value = try await request("/api/llm/config", method: "POST", body: body)
                guard providerBool(value["success"]) == true, value["provider"] as? String == id else { throw ProviderFeatureError(message: "Provider activation was not confirmed.") }
                changed = true
                let runtime = value["reconfigured"] as? [String: Any] ?? [:]
                notice = providerBool(runtime["ok"]) == true && providerBool(runtime["available"]) == true ? "Configuration saved; runtime reports available." : "Configuration saved, but runtime activation or availability is not confirmed. Refresh status before chatting."
                if let persisted = value["persisted"] as? [String: Any], providerBool(persisted["ok"]) == false { notice! += " Credential persistence failed." }
            case .refreshModels:
                let value = try await request(prefix + "/models", query: [URLQueryItem(name: "live", value: "true"), URLQueryItem(name: "force", value: "true")])
                guard let rows = value["models"] as? [[String: Any]] else { throw ProviderFeatureError(message: "Live model response is incomplete.") }
                var modelIDs = Set<String>()
                models = rows.compactMap { $0["id"] as? String }.filter { modelIDs.insert($0).inserted }; modelsSource = value["source"] as? String ?? "Unknown source"
                notice = "Model list returned from \(modelsSource)."; if !(value["warning"] as? String ?? "").isEmpty { notice! += " Discovery failed; this may be cached or fallback data." }
            case .probe:
                let value = try await request(prefix + "/probe", method: "POST", body: [:])
                notice = providerBool(value["reachable"]).map { $0 ? "Provider probe reports reachable. This is not proof every model supports chat." : "Provider probe reports unreachable." } ?? "Probe did not report reachability."
            case .addKey:
                _ = try safeID(review.label); guard !review.secret.isEmpty else { throw ProviderFeatureError(message: "Enter a key to save.") }
                let value = try await request(prefix + "/keys", method: "POST", body: ["label": review.label, "api_key": review.secret, "set_active": false])
                guard providerBool(value["success"]) == true else { throw ProviderFeatureError(message: "Key save was not confirmed.") }; notice = "Labeled key saved without activating or probing it."
            case .activateKey(let label):
                guard keys.contains(where: { $0.id == label }) else { throw ProviderFeatureError(message: "Refresh key metadata before selecting it.") }
                let value = try await request(prefix + "/keys/active", method: "POST", body: ["label": label])
                guard providerBool(value["success"]) == true, value["active_label"] as? String == label else { throw ProviderFeatureError(message: "Active key change was not confirmed.") }; changed = true; notice = "Active key label updated."
                if let runtime = value["reconfigured"] as? [String: Any], providerBool(runtime["ok"]) != true { notice! += " Runtime reconfiguration is not confirmed." }
            case .deleteKey(let label):
                guard keys.contains(where: { $0.id == label }) else { throw ProviderFeatureError(message: "Refresh key metadata before removing it.") }
                let value = try await request(prefix + "/keys/\(try safeID(label))", method: "DELETE")
                guard providerBool(value["success"]) == true else { throw ProviderFeatureError(message: "Key removal was not confirmed.") }; notice = "Labeled key removed. This does not revoke the upstream key. The runtime may retain its current credential and other legacy credentials may remain configured."; changed = true
            case .probeKey(let label):
                guard keys.contains(where: { $0.id == label }) else { throw ProviderFeatureError(message: "Refresh key metadata before probing it.") }
                let value = try await request(prefix + "/keys/\(try safeID(label))/probe", method: "POST", body: [:])
                notice = providerBool(value["ok"]).map { $0 ? "Selected key probe passed." : "Selected key probe failed." } ?? "Key probe verdict is not confirmed."
            case .resetCooldown:
                let value = try await request("/api/llm/cooldowns/reset", method: "POST", body: ["provider": id])
                guard providerBool(value["ok"]) == true else { throw ProviderFeatureError(message: "Cooldown reset was not confirmed.") }; notice = "Cooldown reset. The next request may retry; this does not fix credentials or billing."
            }
            notice = clean(notice ?? "", secret: review.secret)
            if changed {
                cachedConfig = try await request("/api/llm/config")
                activeProvider = cachedConfig["provider"] as? String ?? ""; activeModel = cachedConfig["model"] as? String ?? ""
                let status = try await request("/api/llm/status")
                runtimeState = providerBool(status["available"]).map { $0 ? "Runtime reports available" : "Runtime reports unavailable" } ?? "Not confirmed"
                if providerBool(status["supported"]) == false { runtimeState = "No runtime adapter reported" }
                try await loadHealth()
            }
            if started == generation { try await loadDetails(id) }
        } catch { if started == generation { self.error = clean(error.localizedDescription, secret: review.secret) } }
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
                    Text("Choose provider").tag(""); ForEach(model.providers) { Text($0.name).tag($0.id) }
                }.disabled(model.busy)
                if let provider = model.providers.first(where: { $0.id == model.selected }) {
                    Text(provider.configured ? "Credential/configuration present; reachability is \(provider.reachable.map { $0 ? "reported reachable" : "reported unreachable" } ?? "not probed")." : "Provider needs configuration.").font(.callout)
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
            if let review = confirmation { Button("Continue") { confirmation = nil; secret = ""; Task { if await model.execute(review) { onConfigurationChanged() } } }; Button("Cancel", role: .cancel) { confirmation = nil } }
        } message: {
            if let review = confirmation { Text("Provider: \(review.provider)\nModel: \(review.model)\nEndpoint: \(review.endpoint.isEmpty ? "runtime default" : review.endpoint)\nFallbacks: \(review.fallbacks.joined(separator: ", "))\nKey label: \(review.label.isEmpty ? "none entered" : review.label)\n\(operationDescription(review.operation))") }
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
