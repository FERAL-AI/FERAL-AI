import Foundation
import SwiftUI
import CoreFoundation
import AppKit

private struct RoutingFailure: Error { let message: String }
private func routingBool(_ value: Any?) -> Bool? { guard let n = value as? NSNumber, CFGetTypeID(n) == CFBooleanGetTypeID() else { return nil }; return n.boolValue }
private func routingJSON(_ value: Any) throws -> Data { try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) }
private func routingError(_ error: Error) -> String { (error as? RoutingFailure)?.message ?? "The local request failed. Private service details are withheld." }
final class NativeRoutingRedirectGuard: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
    static func session() -> URLSession { URLSession(configuration: .ephemeral, delegate: NativeRoutingRedirectGuard(), delegateQueue: nil) }
}
struct NativeRoutingPreset: Identifiable { let id: String, provider: String, model: String, description: String; let vision: Bool; let raw: [String: Any] }
struct NativeRoutingProvider: Identifiable { let id: String, name: String; let configured: Bool, chatReady: Bool }
struct NativeRoutingResult: Identifiable { let id: String, tier: String, provider: String, model: String, source: String; let supported: Bool; let fallbacks: [String] }
enum NativeRoutingAction { case preset(String), setting(key: String, value: [String: Any], site: String, tier: String?) }
struct NativeRoutingReview: Identifiable {
    let id = UUID(), generation: UUID, createdUptime = ProcessInfo.processInfo.systemUptime
    let action: NativeRoutingAction
    let title: String, scope: String, previous: String, proposed: String
    let llm: [String: Any], vision: [String: Any]
    let preset: [String: Any]?
}
@MainActor final class NativeProviderRoutingModel: ObservableObject {
    static let sites = ["chat", "routing", "vision", "embedding"]
    static let tiers = ["cheap", "balanced", "premium"]
    @Published private(set) var providers: [NativeRoutingProvider] = []
    @Published private(set) var presets: [NativeRoutingPreset] = []
    @Published private(set) var routes: [NativeRoutingResult] = []
    @Published private(set) var routeErrors: [String: String] = [:]
    @Published private(set) var config: [String: Any]?
    @Published private(set) var runtime: String?
    @Published private(set) var busy = false
    @Published private(set) var error: String?
    @Published private(set) var receipt: String?
    @Published private(set) var inspectedTier: String?
    private var baseURL: URL?
    private var generation = UUID()
    private var reviews: [UUID: NativeRoutingReview] = [:]
    private let session: URLSession
    init(baseURL: URL?, session: URLSession? = nil) { self.baseURL = baseURL; self.session = session ?? NativeRoutingRedirectGuard.session() }
    func configure(_ url: URL?) { guard url != baseURL else { return }; baseURL = url; generation = UUID(); reviews = [:]; providers = []; presets = []; routes = []; routeErrors = [:]; config = nil; runtime = nil; busy = false; error = nil; receipt = nil; inspectedTier = nil }
    private func request(_ path: String, query: [URLQueryItem] = [], body: [String: Any]? = nil) async throws -> [String: Any] {
        try Task.checkCancellation(); let start = generation
        guard let baseURL, ["http", "https"].contains(baseURL.scheme ?? ""), ["127.0.0.1", "::1", "[::1]"].contains(baseURL.host ?? ""), baseURL.user == nil, baseURL.password == nil, var parts = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else { throw RoutingFailure(message: "Connect to the app-owned loopback service.") }
        parts.path = path; parts.queryItems = query.isEmpty ? nil : query; parts.fragment = nil
        guard let url = parts.url else { throw RoutingFailure(message: "Invalid routing request.") }
        var req = URLRequest(url: url); req.timeoutInterval = 180
        if let body { req.httpMethod = "POST"; req.httpBody = try routingJSON(body); req.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        let (data, response) = try await session.data(for: req); try Task.checkCancellation()
        guard start == generation else { throw RoutingFailure(message: "Agent changed. Review again.") }
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode), data.count <= 2 * 1024 * 1024, let value = try JSONSerialization.jsonObject(with: data) as? [String: Any], value["error"] == nil, routingBool(value["ok"]) != false, routingBool(value["success"]) != false else { throw RoutingFailure(message: "The backend refused this request or returned unreadable routing state.") }
        return value
    }
    private func block(_ value: [String: Any], _ key: String) throws -> [String: Any] {
        guard !value.keys.contains(key) || value[key] is [String: Any] else { throw RoutingFailure(message: "Saved \(key) routing state is malformed; no block can safely be replaced.") }; return value[key] as? [String: Any] ?? [:]
    }
    var llm: [String: Any] { config?["llm"] as? [String: Any] ?? [:] }
    var savedTiers: [String: Any] { llm["call_site_tiers"] as? [String: Any] ?? [:] }
    func savedTarget(site: String, tier: String) -> (String, String) { let leaf = ((llm["tier_map"] as? [String: Any])?[site] as? [String: Any])?[tier] as? [String: Any] ?? [:]; return (leaf["provider"] as? String ?? "", leaf["model"] as? String ?? "") }
    private func parsePresets(_ value: [String: Any]) throws -> [NativeRoutingPreset] {
        guard let rows = value["presets"] as? [[String: Any]], rows.count <= 100 else { throw RoutingFailure(message: "Preset catalogue is malformed.") }
        let parsed = try rows.map { row -> NativeRoutingPreset in guard let id = row["id"] as? String, !id.isEmpty, id.utf8.count <= 256, let provider = row["provider"] as? String, !provider.isEmpty, provider.utf8.count <= 128, let model = row["model"] as? String, model.utf8.count <= 512, let description = row["description"] as? String, description.utf8.count <= 4096, let vision = routingBool(row["vision_supported"]) else { throw RoutingFailure(message: "Preset metadata is incomplete or exceeds its bounds.") }; return NativeRoutingPreset(id: id, provider: provider, model: model, description: description, vision: vision, raw: row) }
        guard Set(parsed.map(\.id)).count == parsed.count else { throw RoutingFailure(message: "Preset identifiers are duplicated.") }; return parsed
    }
    private func status(_ value: [String: Any]) throws -> String { guard let provider = value["provider"] as? String, let available = routingBool(value["available"]), let supported = routingBool(value["supported"]) else { throw RoutingFailure(message: "Runtime status is incomplete.") }; return "\(provider) / \(value["model"] as? String ?? "unreported") · \(supported ? "adapter supported" : "adapter unsupported") · \(available ? "runtime reports available" : "runtime reports unavailable") (status read does not test connectivity)" }
    func refresh() async {
        guard !busy else { return }; let start = generation; busy = true; error = nil; reviews = [:]; defer { if generation == start { busy = false } }
        do {
            let saved = try await request("/api/config"); _ = try block(saved, "llm"); _ = try block(saved, "vision"); config = saved
            presets = try parsePresets(await request("/api/llm/presets"))
            let catalog = try await request("/api/llm/providers")
            guard let rows = catalog["providers"] as? [[String: Any]], rows.count <= 200 else { throw RoutingFailure(message: "Provider catalogue is malformed.") }
            providers = try rows.map { row in guard let id = row["id"] as? String, !id.isEmpty, let name = row["display_name"] as? String, let configured = routingBool(row["configured"]), let ready = routingBool(row["chat_ready"]) else { throw RoutingFailure(message: "Provider metadata is incomplete.") }; return NativeRoutingProvider(id: id, name: name, configured: configured, chatReady: ready) }
            guard Set(providers.map(\.id)).count == providers.count else { throw RoutingFailure(message: "Provider identifiers are duplicated.") }
            runtime = try status(await request("/api/llm/status"))
        } catch { if generation == start { self.error = routingError(error); config = nil; presets = []; providers = []; runtime = nil } }
    }
    private func route(_ site: String, tier: String?) async throws -> NativeRoutingResult {
        var query = [URLQueryItem(name: "call_site", value: site)]; if let tier { query.append(URLQueryItem(name: "tier", value: tier)) }
        let value = try await request("/api/llm/route", query: query)
        guard value["call_site"] as? String == site, let resolved = value["tier"] as? String, Self.tiers.contains(resolved), tier == nil || resolved == tier, let provider = value["provider"] as? String, !provider.isEmpty, provider.utf8.count <= 128, let model = value["model"] as? String, !model.isEmpty, model.utf8.count <= 512, let supported = routingBool(value["supported"]), let fallbacks = value["fallback_providers"] as? [String], fallbacks.count <= 100, fallbacks.allSatisfy({ $0.utf8.count <= 128 }), let source = value["source"] as? String, ["settings", "default", "explicit", "budget_downshift", "local_first"].contains(source) else { throw RoutingFailure(message: "Route response does not match the inspected call site and tier or exceeds its bounds.") }
        return NativeRoutingResult(id: site, tier: resolved, provider: provider, model: model, source: source, supported: supported, fallbacks: fallbacks)
    }
    func inspect(tier: String?) async {
        guard !busy, tier == nil || Self.tiers.contains(tier!) else { return }; let start = generation; busy = true; routes = []; routeErrors = [:]; inspectedTier = tier; defer { if start == generation { busy = false } }
        for site in Self.sites { do { let result = try await route(site, tier: tier); routes.append(result) } catch { if generation != start { return }; routeErrors[site] = routingError(error) } }
    }
    private func held(_ action: NativeRoutingAction, title: String, scope: String, old: String, new: String, preset: [String: Any]? = nil) throws -> NativeRoutingReview {
        guard !busy, let config, baseURL != nil else { throw RoutingFailure(message: "Refresh saved routing state first.") }
        let review = NativeRoutingReview(generation: generation, action: action, title: title, scope: scope, previous: old, proposed: new, llm: try block(config, "llm"), vision: try block(config, "vision"), preset: preset); reviews[review.id] = review; return review
    }
    private let writeScope = "This saves one llm configuration key while preserving its unknown sibling fields. The backend also switches/reopens the current provider client; this can refresh Codex models or auto-detect local models using configured endpoints. Future agent calls may use this route and send content to its provider/fallbacks. Saved readback and passive runtime resolution are reported separately; neither proves a successful model request."
    func reviewTier(site: String, tier: String) throws -> NativeRoutingReview {
        guard Self.sites.contains(site), Self.tiers.contains(tier) else { throw RoutingFailure(message: "Select a supported call site and tier.") }
        var map = try block(llm, "call_site_tiers"); let old = map[site] as? String ?? "backend default"; map[site] = tier
        return try held(.setting(key: "call_site_tiers", value: map, site: site, tier: nil), title: "Save \(site) default tier", scope: writeScope, old: old, new: tier)
    }
    func reviewTarget(site: String, tier: String, provider: String, model: String, remove: Bool = false) throws -> NativeRoutingReview {
        guard Self.sites.contains(site), Self.tiers.contains(tier) else { throw RoutingFailure(message: "Select a supported call site and tier.") }
        var map = try block(llm, "tier_map"); var sites = try block(map, site); var leaf = try block(sites, tier)
        let old = "\(leaf["provider"] as? String ?? "automatic") / \(leaf["model"] as? String ?? "provider default")"
        if remove { leaf.removeValue(forKey: "provider"); leaf.removeValue(forKey: "model"); sites[tier] = leaf }
        else { guard providers.contains(where: { $0.id == provider }), !model.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, model.utf8.count <= 512, model.utf8.allSatisfy({ $0 >= 32 && $0 != 127 }) else { throw RoutingFailure(message: "Choose a catalogued provider and explicit model up to 512 bytes.") }; leaf["provider"] = provider; leaf["model"] = model; sites[tier] = leaf }
        map[site] = sites
        return try held(.setting(key: "tier_map", value: map, site: site, tier: tier), title: remove ? "Remove \(site)/\(tier) override" : "Save \(site)/\(tier) target", scope: writeScope, old: old, new: remove ? "Automatic resolution; unknown metadata retained" : "\(provider) / \(model)")
    }
    func reviewPreset(_ id: String) throws -> NativeRoutingReview {
        guard let preset = presets.first(where: { $0.id == id }) else { throw RoutingFailure(message: "Choose a loaded preset.") }
        return try held(.preset(id), title: "Apply \(id) preset", scope: "This changes the live primary provider and persists provider/model. Local presets may query installed models and substitute an available model; empty model means provider default/auto-detection. ollama_vision also enables and configures vision. The live switch selects the provider’s default endpoint; this backend does not clear saved base_url or fallback configuration, so saved endpoint state can differ. Credentials remain managed by the provider configuration screen. Future requests may transmit content to the selected provider. Readback verifies the reported selection, not connectivity.", old: "\(llm["provider"] as? String ?? "unreported") / \(llm["model"] as? String ?? "unreported")", new: "\(preset.provider) / \(preset.model.isEmpty ? "default or detected model" : preset.model) · vision capable: \(preset.vision)", preset: preset.raw)
    }
    func cancel(_ review: NativeRoutingReview) { reviews[review.id] = nil }
    func discardReviews() { reviews = [:] }
    func perform(_ review: NativeRoutingReview) async -> Bool {
        guard !busy, review.generation == generation, ProcessInfo.processInfo.systemUptime - review.createdUptime <= 300, let held = reviews.removeValue(forKey: review.id) else { error = "Review expired or was canceled. Review again."; return false }
        let review = held, start = generation; busy = true; error = nil; receipt = nil; var dispatched = false; defer { if generation == start { busy = false } }
        do {
            let fresh = try await request("/api/config")
            guard try routingJSON(block(fresh, "llm")) == routingJSON(review.llm), try routingJSON(block(fresh, "vision")) == routingJSON(review.vision) else { throw RoutingFailure(message: "Saved provider/routing/vision state changed. No write was sent; review again.") }
            switch review.action {
            case .setting(let key, let value, let site, let tier):
                guard ProcessInfo.processInfo.systemUptime - review.createdUptime <= 300 else { throw RoutingFailure(message: "Review expired during preflight. No write was sent.") }
                dispatched = true; _ = try await request("/api/config/update", body: ["section": "llm", "key": key, "value": value])
                let saved = try await request("/api/config"); let llm = try block(saved, "llm")
                guard try routingJSON(llm[key] as? [String: Any] ?? [:]) == routingJSON(value) else { throw RoutingFailure(message: "Saved readback differs from the reviewed routing block.") }
                config = saved
                do { let resolved = try await route(site, tier: tier); routes = [resolved]; routeErrors = [:]; inspectedTier = tier; receipt = "Saved \(key). Runtime currently resolves \(site)/\(resolved.tier) to \(resolved.provider)/\(resolved.model). This observation is separate from persisted configuration and does not prove connectivity." } catch { guard start == generation else { return false }; receipt = "Saved \(key) verified; live route resolution is unavailable. Runtime application is not confirmed."; routeErrors[site] = routingError(error) }
                let liveStatus = try? status(await request("/api/llm/status")); guard start == generation else { return false }; runtime = liveStatus
            case .preset(let id):
                let presets = try parsePresets(await request("/api/llm/presets")); guard let preset = presets.first(where: { $0.id == id }), let original = review.preset, try routingJSON(preset.raw) == routingJSON(original) else { throw RoutingFailure(message: "Preset changed. No preset was applied; review again.") }
                guard ProcessInfo.processInfo.systemUptime - review.createdUptime <= 300 else { throw RoutingFailure(message: "Review expired during preflight. No preset was applied.") }
                dispatched = true; let result = try await request("/api/llm/presets/apply", body: ["preset": id])
                guard routingBool(result["ok"]) == true, result["preset"] as? String == id, let provider = result["provider"] as? String, provider == preset.provider, let model = result["model"] as? String, !model.isEmpty else { throw RoutingFailure(message: "Preset receipt does not identify the selected provider/model.") }
                let saved = try await request("/api/config"); let llm = try block(saved, "llm")
                let live = try await request("/api/llm/status")
                guard llm["provider"] as? String == provider, llm["model"] as? String == model, live["provider"] as? String == provider, live["model"] as? String == model else { throw RoutingFailure(message: "Saved or runtime selection differs from the preset receipt.") }
                if id == "ollama_vision" { let vision = try block(saved, "vision"); guard routingBool(vision["enabled"]) == true, vision["provider"] as? String == "ollama", vision["model"] as? String == model else { throw RoutingFailure(message: "Vision readback differs from the applied vision preset.") } }
                config = saved; runtime = try status(live); routes = []; receipt = "Preset selected \(provider)/\(model); saved and runtime model selection verified. A model may have been substituted. Saved base URL/fallbacks were not cleared; connectivity and inference remain untested."
            }
            reviews = [:]; return true
        } catch { if start == generation { self.error = routingError(error) + (dispatched ? " Changes may already have occurred. Refresh before retrying." : "") }; return false }
    }
}

// Use AppKit popup controls to avoid nested SwiftUI menu accessibility wrappers.
// Values stay distinct from labels; programmatic updates never dispatch a selection action.
struct NativeRoutingOption: Equatable {
    let value: String
    let title: String
}
struct NativeRoutingPopup: NSViewRepresentable {
    let label: String
    let options: [NativeRoutingOption]
    @Binding var selection: String
    var enabled = true
    func makeCoordinator() -> Coordinator { Coordinator(self) }
    func makeNSView(context: Context) -> NSPopUpButton {
        let popup = NSPopUpButton(frame: .zero, pullsDown: false)
        popup.target = context.coordinator
        popup.action = #selector(Coordinator.choose(_:))
        popup.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return popup
    }
    func updateNSView(_ popup: NSPopUpButton, context: Context) {
        context.coordinator.parent = self
        if context.coordinator.rendered != options {
            popup.removeAllItems()
            for option in options {
                let item = NSMenuItem(title: option.title, action: nil, keyEquivalent: "")
                item.representedObject = option.value
                popup.menu?.addItem(item)
            }
            context.coordinator.rendered = options
        }
        if let index = options.firstIndex(where: { $0.value == selection }) {
            popup.selectItem(at: index)
        } else { popup.select(nil) }
        popup.isEnabled = enabled
        popup.setAccessibilityLabel(label)
    }
    final class Coordinator: NSObject {
        var parent: NativeRoutingPopup
        var rendered: [NativeRoutingOption] = []
        init(_ parent: NativeRoutingPopup) { self.parent = parent }
        @objc func choose(_ sender: NSPopUpButton) {
            guard parent.enabled, let value = sender.selectedItem?.representedObject as? String,
                  parent.options.contains(where: { $0.value == value }) else { return }
            parent.selection = value
        }
    }
}

struct NativeProviderRoutingFeatureView: View {
    let baseURL: URL?
    @StateObject private var model: NativeProviderRoutingModel
    @State private var site = "chat"
    @State private var tier = "balanced"
    @State private var provider = ""
    @State private var targetModel = ""
    @State private var inspection = "saved"
    @State private var review: NativeRoutingReview?
    @State private var localError: String?
    init(baseURL: URL?) { self.baseURL = baseURL; _model = StateObject(wrappedValue: NativeProviderRoutingModel(baseURL: baseURL)) }
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                HStack { Text("Provider routing").font(.title2.bold()); Spacer(); if model.busy { ProgressView().controlSize(.small) }; Button("Refresh saved state") { Task { await model.refresh() } }.disabled(model.busy) }
                Text("Inspect runtime target resolution and configure per-call-site tiers. Opening this screen reads saved/catalogue state only; it does not probe providers, discover models or submit model content.").foregroundStyle(.secondary)
                if let message = localError ?? model.error { Text(message).foregroundStyle(.red).textSelection(.enabled) }
                if let receipt = model.receipt { Text(receipt).foregroundStyle(.secondary).textSelection(.enabled) }
                if let status = model.runtime { Text("Runtime primary: \(status)").font(.caption).textSelection(.enabled) }
                VStack(alignment: .leading, spacing: 8) {
                    Text("Saved defaults").font(.headline)
                    VStack(alignment: .leading, spacing: 8) {
                        ForEach(NativeProviderRoutingModel.sites, id: \.self) { site in HStack { Text(site.capitalized).frame(width: 120, alignment: .leading); Text(model.savedTiers[site] as? String ?? "Backend default"); Spacer() } }
                        HStack { Text("Call site"); NativeRoutingPopup(label: "Call site", options: NativeProviderRoutingModel.sites.map { NativeRoutingOption(value: $0, title: $0.capitalized) }, selection: $site, enabled: !model.busy).frame(width: 160, height: 26); Text("Tier"); NativeRoutingPopup(label: "Tier", options: NativeProviderRoutingModel.tiers.map { NativeRoutingOption(value: $0, title: $0.capitalized) }, selection: $tier, enabled: !model.busy).frame(width: 130, height: 26); Button("Review default tier…") { prepare { try model.reviewTier(site: site, tier: tier) } }.disabled(model.busy) }
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }
                Divider()
                VStack(alignment: .leading, spacing: 8) {
                    Text("Explicit target override").font(.headline)
                    VStack(alignment: .leading, spacing: 8) {
                        let saved = model.savedTarget(site: site, tier: tier)
                        Text("\(site)/\(tier) saved override: \(saved.0.isEmpty ? "automatic" : saved.0) / \(saved.1.isEmpty ? "provider default" : saved.1)").font(.caption).textSelection(.enabled)
                        HStack { Text("Provider"); NativeRoutingPopup(label: "Target provider", options: [NativeRoutingOption(value: "", title: "Choose provider")] + model.providers.map { entry in NativeRoutingOption(value: entry.id, title: "\(entry.name)\(entry.chatReady ? "" : " · preview")\(entry.configured ? "" : " · not configured")") }, selection: $provider, enabled: !model.busy).frame(width: 360, height: 26) }
                        TextField("Explicit model identifier", text: $targetModel)
                        Text("Model IDs are entered explicitly. Availability, model capability, authentication and cost are not verified by this form.").font(.caption).foregroundStyle(.secondary)
                        HStack { Button("Review target override…") { prepare { try model.reviewTarget(site: site, tier: tier, provider: provider, model: targetModel) } }; Button("Review return to automatic…") { prepare { try model.reviewTarget(site: site, tier: tier, provider: "", model: "", remove: true) } } }.disabled(model.busy)
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }
                Divider()
                VStack(alignment: .leading, spacing: 8) {
                    Text("Passive route inspection").font(.headline)
                    VStack(alignment: .leading, spacing: 8) {
                        HStack { Text("Inspect"); NativeRoutingPopup(label: "Route inspection tier", options: [NativeRoutingOption(value: "saved", title: "Runtime saved/default tiers")] + NativeProviderRoutingModel.tiers.map { NativeRoutingOption(value: $0, title: "Explicit \($0) tier") }, selection: $inspection, enabled: !model.busy).frame(width: 300, height: 26); Button("Inspect four call sites") { Task { await model.inspect(tier: inspection == "saved" ? nil : inspection) } }.disabled(model.busy) }
                        Text("A pure routing lookup. Interactive requests can additionally apply budget downshifts, local-first policy and failover; this preview does not prove provider connectivity.").font(.caption).foregroundStyle(.secondary)
                        if !model.routes.isEmpty { Text("Results for: \(model.inspectedTier ?? "runtime saved/default")").font(.caption) }
                        ForEach(model.routes) { route in VStack(alignment: .leading, spacing: 3) { Text("\(route.id.capitalized) · \(route.tier) → \(route.provider) / \(route.model)").font(.headline).textSelection(.enabled); Text("\(route.supported ? "Runtime adapter supported" : "Runtime adapter unsupported") · source: \(route.source) · fallbacks: \(route.fallbacks.joined(separator: ", "))").font(.caption).textSelection(.enabled) } }
                        ForEach(model.routeErrors.keys.sorted(), id: \.self) { key in Text("\(key): \(model.routeErrors[key] ?? "Unavailable")").foregroundStyle(.red) }
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }
                Divider()
                VStack(alignment: .leading, spacing: 8) {
                    Text("Backend presets").font(.headline)
                    VStack(alignment: .leading, spacing: 10) { ForEach(model.presets) { preset in VStack(alignment: .leading, spacing: 3) { Text(preset.id).font(.headline); Text(preset.description).foregroundStyle(.secondary); Text("\(preset.provider) / \(preset.model.isEmpty ? "default or detected model" : preset.model)").font(.caption); Button("Review preset…") { prepare { try model.reviewPreset(preset.id) } }.disabled(model.busy) } }; if model.presets.isEmpty { Text("No presets loaded.").foregroundStyle(.secondary) } }.frame(maxWidth: .infinity, alignment: .leading)
                }
            }.padding()
        }.task(id: baseURL) { review = nil; localError = nil; provider = ""; targetModel = ""; model.configure(baseURL); await model.refresh() }
        .sheet(item: $review, onDismiss: { model.discardReviews() }) { item in reviewPanel(item) }
    }
    private func prepare(_ make: () throws -> NativeRoutingReview) { do { localError = nil; review = try make() } catch { localError = routingError(error) } }
    private func reviewPanel(_ item: NativeRoutingReview) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(item.title).font(.title2.bold())
            Text(item.scope).textSelection(.enabled)
            Text("Previous: \(item.previous)\nProposed: \(item.proposed)").font(.system(.body, design: .monospaced)).textSelection(.enabled)
            Text("This review expires after five minutes.").font(.caption).foregroundStyle(.secondary)
            if model.busy { ProgressView(); Text("Applying changes. Closing cannot undo a completed write.").font(.caption) }
            HStack { Button("Cancel") { model.cancel(item); review = nil }.disabled(model.busy); Spacer(); Button("Confirm") { Task { _ = await model.perform(item); review = nil } }.disabled(model.busy) }
        }.padding(24).frame(width: 650).interactiveDismissDisabled(model.busy)
    }
}
