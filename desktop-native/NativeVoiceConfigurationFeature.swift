import Foundation
import SwiftUI
import CoreFoundation

private struct VoiceConfigurationFailure: LocalizedError { let message: String; var errorDescription: String? { message } }
private func voiceConfigurationBool(_ value: Any?) -> Bool? { guard let n = value as? NSNumber, CFGetTypeID(n) == CFBooleanGetTypeID() else { return nil }; return n.boolValue }
private func voiceConfigurationSafeError(_ error: Error) -> String { (error as? VoiceConfigurationFailure)?.message ?? "The local request could not be completed. Details are withheld; refresh before retrying." }
private func voiceConfigurationVerdict(_ raw: Any?) -> String {
    guard let value = raw as? String else { return "unreported" }
    let known: Set<String> = ["ok", "unauthorized", "no_key", "not_configured", "not_installed", "unavailable", "unsupported", "timeout", "network_error", "probe_error", "rate_limited", "quota_exceeded", "model_missing", "missing_dependency", "missing_model", "unknown_provider"]
    return known.contains(value) ? value : "unrecognized verdict (details withheld)"
}
private func voiceConfigurationJSON(_ value: Any) -> String { guard let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys, .fragmentsAllowed]) else { return "Unreadable" }; return String(decoding: data, as: UTF8.self) }
final class NativeVoiceConfigurationRedirectGuard: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
    static func session() -> URLSession { URLSession(configuration: .ephemeral, delegate: NativeVoiceConfigurationRedirectGuard(), delegateQueue: nil) }
}
struct NativeSpeechProvider: Identifiable { let id: String, kind: String, name: String, verdict: String, detail: String; let configured: Bool; let models: [String]; let raw: [String: Any] }
enum NativeSpeechConfigurationAction { case catalogue, probe(String), setting(section: String, key: String, value: Any), wake(Bool) }
struct NativeSpeechConfigurationReview: Identifiable { let id = UUID(); let generation: UUID; let action: NativeSpeechConfigurationAction; let title: String, scope: String, previous: String, proposed: String; let fingerprint: String }

@MainActor final class NativeVoiceConfigurationModel: ObservableObject {
    @Published private(set) var config: [String: Any]?
    @Published private(set) var status: [String: Any]?
    @Published private(set) var wake: [String: Any]?
    @Published private(set) var providers: [NativeSpeechProvider]?
    @Published private(set) var errors: [String: String] = [:]
    @Published private(set) var busy = false
    @Published private(set) var receipt: String?
    @Published private(set) var actionError: String?
    private var baseURL: URL?
    private var generation = UUID()
    private var reviews: [UUID: NativeSpeechConfigurationReview] = [:]
    private let session: URLSession
    static let realtimeIDs = ["openai", "openai_realtime", "gemini", "gemini_live", "google"]
    static let chainedKeys = ["stt_provider", "tts_provider", "stt_model", "tts_model", "tts_voice", "tts_voice_id"]
    init(baseURL: URL?, session: URLSession? = nil) { self.baseURL = baseURL; self.session = session ?? NativeVoiceConfigurationRedirectGuard.session() }
    func configure(_ baseURL: URL?) { guard self.baseURL != baseURL else { return }; self.baseURL = baseURL; generation = UUID(); config = nil; status = nil; wake = nil; providers = nil; errors = [:]; reviews = [:]; busy = false; receipt = nil; actionError = nil }
    var audio: [String: Any] { config?["audio"] as? [String: Any] ?? [:] }
    var chained: [String: Any] { (config?["voice"] as? [String: Any])?["chained"] as? [String: Any] ?? [:] }
    var fallback: [String: Any] { audio["chained_fallback"] as? [String: Any] ?? [:] }
    var realtimeOrder: [String] { let order = audio["realtime_providers"] as? [String] ?? []; if !order.isEmpty { return order }; if let legacy = audio["realtime_provider"] as? String, !legacy.isEmpty { return [legacy] }; return [] }
    func effectiveChained(_ key: String) -> String { if let saved = chained[key] as? String, !saved.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return saved }; if let saved = fallback[key] as? String, !saved.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return saved }; if key == "stt_provider" { return "deepgram (router default)" }; if key == "tts_provider" { return "elevenlabs (router default)" }; return "Provider default" }
    private func request(_ path: String, method: String = "GET", body: [String: Any]? = nil, probeResult: Bool = false) async throws -> [String: Any] {
        try Task.checkCancellation(); let start = generation
        guard let baseURL, ["http", "https"].contains(baseURL.scheme ?? ""), ["127.0.0.1", "::1", "[::1]"].contains(baseURL.host ?? ""), baseURL.user == nil, baseURL.password == nil, var parts = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else { throw VoiceConfigurationFailure(message: "Connect to the app-owned loopback service.") }
        parts.path = path; parts.query = nil; parts.fragment = nil
        guard let url = parts.url else { throw VoiceConfigurationFailure(message: "Invalid speech settings request.") }
        var req = URLRequest(url: url); req.httpMethod = method; req.timeoutInterval = 120
        if let body { req.httpBody = try JSONSerialization.data(withJSONObject: body); req.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        let (data, response) = try await session.data(for: req); try Task.checkCancellation()
        guard start == generation else { throw VoiceConfigurationFailure(message: "Agent changed. Review again.") }
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode), let value = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { throw VoiceConfigurationFailure(message: "The speech settings request failed or returned unreadable state.") }
        if (value["error"] as? String)?.isEmpty == false || (!probeResult && (voiceConfigurationBool(value["ok"]) == false || voiceConfigurationBool(value["success"]) == false)) { throw VoiceConfigurationFailure(message: "The backend refused this speech settings request.") }
        return value
    }
    private func validatedConfig(_ value: [String: Any]) throws -> [String: Any] {
        for key in ["audio", "voice"] { if value.keys.contains(key), !(value[key] is [String: Any]) { throw VoiceConfigurationFailure(message: "The saved \(key) block is malformed. No settings can safely be replaced.") } }
        let audio = value["audio"] as? [String: Any] ?? [:], voice = value["voice"] as? [String: Any] ?? [:]
        for (block, key) in [(audio, "chained_fallback"), (voice, "chained")] { if block.keys.contains(key), !(block[key] is [String: Any]) { throw VoiceConfigurationFailure(message: "The saved \(key) block is malformed.") } }
        if audio.keys.contains("realtime_providers"), !(audio["realtime_providers"] is [String]) { throw VoiceConfigurationFailure(message: "Saved realtime order is malformed.") }
        return value
    }
    private func validatedWake(_ value: [String: Any]) throws -> [String: Any] { guard voiceConfigurationBool(value["supported"]) != nil, voiceConfigurationBool(value["enabled"]) != nil else { throw VoiceConfigurationFailure(message: "Wake-word state is unavailable.") }; return value }
    private func loadPassive(start: UUID) async {
        do { let value = try validatedConfig(await request("/api/config")); guard start == generation else { return }; config = value; errors["config"] = nil } catch { if start == generation { errors["config"] = voiceConfigurationSafeError(error) } }
        do { let value = try await request("/api/voice/status"); guard voiceConfigurationBool(value["realtime_available"]) != nil, voiceConfigurationBool(value["audio_available"]) != nil else { throw VoiceConfigurationFailure(message: "Voice subsystem status is incomplete.") }; guard start == generation else { return }; status = value; errors["status"] = nil } catch { if start == generation { errors["status"] = voiceConfigurationSafeError(error) } }
        do { let value = try validatedWake(await request("/api/ambient/wake_word/status")); guard start == generation else { return }; wake = value; errors["wake"] = nil } catch { if start == generation { errors["wake"] = voiceConfigurationSafeError(error) } }
    }
    func refresh() async { guard !busy else { return }; let start = generation; busy = true; defer { if start == generation { busy = false } }; await loadPassive(start: start) }
    // Only known nonsecret routing fields belong in the user-facing old/new review.
    private func chainedDisclosure(_ block: [String: Any]) -> [String: Any] { var safe: [String: Any] = [:]; for key in Self.chainedKeys { if let text = block[key] as? String { safe[key] = text } }; return safe }
    private func savedDisclosure(_ value: [String: Any]) -> String {
        let audio = value["audio"] as? [String: Any] ?? [:], voice = value["voice"] as? [String: Any] ?? [:]
        var safeAudio: [String: Any] = [:]
        for key in ["realtime_primary", "realtime_provider", "realtime_model", "tts_voice"] { if let text = audio[key] as? String { safeAudio[key] = text } }
        if let order = audio["realtime_providers"] as? [String] { safeAudio["realtime_providers"] = order }
        if let fallback = audio["chained_fallback"] as? [String: Any] { safeAudio["chained_fallback"] = chainedDisclosure(fallback) }
        return voiceConfigurationJSON(["audio": safeAudio, "voice": ["chained": chainedDisclosure(voice["chained"] as? [String: Any] ?? [:])]])
    }
    private func wakeDisclosure(_ value: [String: Any]) -> String { var safe: [String: Any] = [:]; for key in ["enabled", "supported"] { if let bool = voiceConfigurationBool(value[key]) { safe[key] = bool } }; for key in ["phrase", "effective_phrase", "detector"] { if let text = value[key] as? String { safe[key] = text } }; return voiceConfigurationJSON(safe) }
    private func configSnapshot(_ value: [String: Any]) -> String { voiceConfigurationJSON(["audio": value["audio"] ?? [:], "voice": value["voice"] ?? [:]]) }
    private func issue(_ action: NativeSpeechConfigurationAction, title: String, scope: String, previous: String = "", proposed: String = "", fingerprint: String = "") -> NativeSpeechConfigurationReview { if reviews.count >= 20 { reviews.removeAll() }; let value = NativeSpeechConfigurationReview(generation: generation, action: action, title: title, scope: scope, previous: previous, proposed: proposed, fingerprint: fingerprint); reviews[value.id] = value; return value }
    func catalogueReview() throws -> NativeSpeechConfigurationReview { guard !busy else { throw VoiceConfigurationFailure(message: "Wait for the current request.") }; return issue(.catalogue, title: "Load and test speech provider catalogue?", scope: "GET voice/providers may probe every configured speech provider when its cache expires. This can contact cloud accounts using stored credentials, reveal network metadata, and inspect local engines. It does not capture your microphone or send a transcript. No provider is automatically selected.") }
    func probeReview(_ id: String) throws -> NativeSpeechConfigurationReview { guard !busy, providers?.contains(where: { $0.id == id }) == true else { throw VoiceConfigurationFailure(message: "Load the catalogue explicitly and choose one provider.") }; return issue(.probe(id), title: "Test \(id)?", scope: "Forces an uncached connection/credential check for this provider only. Cloud providers may receive a request using stored credentials; local providers may initialize or inspect their engine. This is not a real speech/session test and changes no saved selection.") }
    func audioReview(key: String, value: Any) throws -> NativeSpeechConfigurationReview {
        guard !busy, let config, errors["config"] == nil, ["realtime_providers", "realtime_primary", "realtime_model", "tts_voice"].contains(key) else { throw VoiceConfigurationFailure(message: "Refresh valid saved settings before reviewing this field.") }
        if key == "realtime_providers" { guard let list = value as? [String], list.count <= 20, Set(list.map { ["openai", "openai_realtime"].contains($0) ? "openai" : "gemini" }).count == list.count, list.allSatisfy({ Self.realtimeIDs.contains($0) }) else { throw VoiceConfigurationFailure(message: "Realtime order must contain unique supported OpenAI/Gemini IDs.") } }
        else { guard let text = value as? String, text.count <= 500 else { throw VoiceConfigurationFailure(message: "Enter a shorter saved value.") }; if key == "realtime_primary", !text.isEmpty, !Self.realtimeIDs.contains(text) { throw VoiceConfigurationFailure(message: "The router only supports OpenAI/Gemini realtime primary IDs; empty inherits surface policy.") } }
        let scope = key == "realtime_primary" ? "An explicit primary can override desktop local-first policy and send future speech to a cloud provider. Environment or per-session choices may still win." : key == "realtime_providers" ? "Changes the order of realtime fallbacks, not a guarantee that realtime leads on this surface. An empty list restores backend defaults; it does not disable cloud fallback." : "Changes the saved provider-specific model/voice identifier. The backend may reject, ignore or fall back from unsupported values; availability is not tested by saving."
        return issue(.setting(section: "audio", key: key, value: value), title: "Save audio.\(key)?", scope: scope + " Writes only this config key; unrelated fields are preserved. Existing sessions may keep old settings; restart/new-session adoption is not confirmed.", previous: savedDisclosure(config), proposed: voiceConfigurationJSON(value), fingerprint: configSnapshot(config))
    }
    func chainedReview(key: String, value: String) throws -> NativeSpeechConfigurationReview {
        guard !busy, let config, errors["config"] == nil, Self.chainedKeys.contains(key), value.count <= 500 else { throw VoiceConfigurationFailure(message: "Refresh valid chained settings and choose a supported field.") }
        var merged = chained; merged[key] = (key == "tts_provider" && value == "openai_tts") ? "openai" : value
        return issue(.setting(section: "voice", key: "chained", value: merged), title: "Save chained \(key)?", scope: "Replaces only voice.chained after merging the current sibling keys, including opaque future fields. STT receives speech and TTS receives response text; cloud engines may transmit that content during future sessions. Empty values inherit audio.chained_fallback or provider/router defaults; they do not disable speech. Saving is not a probe or proof of runtime adoption.", previous: savedDisclosure(config), proposed: voiceConfigurationJSON(chainedDisclosure(merged)), fingerprint: configSnapshot(config))
    }
    func wakeReview(_ enabled: Bool) throws -> NativeSpeechConfigurationReview {
        guard !busy, let wake, errors["wake"] == nil, voiceConfigurationBool(wake["supported"]) == true, voiceConfigurationBool(wake["enabled"]) != enabled else { throw VoiceConfigurationFailure(message: "Refresh a supported wake detector before changing it.") }
        return issue(.wake(enabled), title: enabled ? "Enable backend wake-word detection?" : "Disable backend wake-word detection?", scope: "The backend toggles its live detector and may load its ML model. This does not request native microphone access or guarantee that a background capture source exists. Effective phrase: \(wake["effective_phrase"] as? String ?? "unreported"). This endpoint is a toggle, not an atomic setter; a concurrent change can prevent the requested result. Persistence across restart is not promised.", previous: wakeDisclosure(wake), proposed: String(enabled), fingerprint: voiceConfigurationJSON(wake))
    }
    private func parseProviders(_ value: [String: Any]) throws -> [NativeSpeechProvider] {
        guard let rows = value["providers"] as? [[String: Any]] else { throw VoiceConfigurationFailure(message: "Speech provider catalogue is unreadable.") }; var seen = Set<String>()
        return try rows.map { row in guard let id = row["id"] as? String, !id.isEmpty, seen.insert(id).inserted, let kind = row["kind"] as? String, ["realtime", "stt", "tts", "vad"].contains(kind), let configured = voiceConfigurationBool(row["configured"]) else { throw VoiceConfigurationFailure(message: "Provider identities or probe states are incomplete.") }; var safeRaw: [String: Any] = ["id": id, "kind": kind, "name": row["name"] as? String ?? id, "configured": configured, "probe_status": voiceConfigurationVerdict(row["probe_status"])]
            if let models = row["models"] as? [String] { safeRaw["models"] = models }
            return NativeSpeechProvider(id: id, kind: kind, name: row["name"] as? String ?? id, verdict: voiceConfigurationVerdict(row["probe_status"]), detail: "", configured: configured, models: row["models"] as? [String] ?? [], raw: safeRaw) }
    }
    func perform(_ review: NativeSpeechConfigurationReview) async -> Bool {
        guard !busy, review.generation == generation, let issued = reviews.removeValue(forKey: review.id) else { actionError = "Review expired, changed or was already used."; return false }
        let start = generation; busy = true; actionError = nil; receipt = nil; defer { if start == generation { busy = false } }
        do {
            switch issued.action {
            case .catalogue: providers = try parseProviders(await request("/api/voice/providers")); errors["catalogue"] = nil; receipt = "Catalogue/probe statuses loaded. Results can become stale; no speech was tested."
            case .probe(let id):
                let result = try await request("/api/voice/providers/probe", method: "POST", body: ["provider_id": id], probeResult: true)
                guard result["provider_id"] as? String == id, let ok = voiceConfigurationBool(result["ok"]), let reason = result["reason"] as? String else { throw VoiceConfigurationFailure(message: "Probe response did not identify this provider.") }
                if let old = providers?.first(where: { $0.id == id }) { var raw = old.raw; raw["configured"] = ok; raw["probe_status"] = voiceConfigurationVerdict(reason); raw.removeValue(forKey: "probe_detail"); raw.removeValue(forKey: "detail"); raw.removeValue(forKey: "error"); providers?.removeAll { $0.id == id }; providers?.append(contentsOf: try parseProviders(["providers": [raw]])) }
                receipt = "\(id) probe: \(voiceConfigurationVerdict(reason)). " + (ok ? "Connection check passed; real speech is untested." : "Connection check failed; provider is not proven available.")
            case .setting(let section, let key, let value):
                let fresh = try validatedConfig(await request("/api/config")); config = fresh
                guard configSnapshot(fresh) == issued.fingerprint else { throw VoiceConfigurationFailure(message: "Saved voice/audio settings changed. Review the refreshed state.") }
                let response = try await request("/api/config/update", method: "POST", body: ["section": section, "key": key, "value": value])
                guard voiceConfigurationBool(response["ok"]) == true else { throw VoiceConfigurationFailure(message: "Settings save was not acknowledged.") }
                let after = try validatedConfig(await request("/api/config")); config = after
                guard voiceConfigurationJSON((after[section] as? [String: Any])?[key] ?? NSNull()) == voiceConfigurationJSON(value) else { throw VoiceConfigurationFailure(message: "Saved readback differs from the reviewed value.") }
                errors["config"] = nil; receipt = "Exact saved key acknowledged and read back. Existing sessions, environment overrides and actual speech are unverified."
            case .wake(let desired):
                let fresh = try validatedWake(await request("/api/ambient/wake_word/status")); wake = fresh
                guard voiceConfigurationJSON(fresh) == issued.fingerprint else { throw VoiceConfigurationFailure(message: "Wake detector changed. Review its refreshed state.") }
                let result = try await request("/api/ambient/wake_word/toggle", method: "POST")
                let after = try validatedWake(await request("/api/ambient/wake_word/status")); wake = after
                guard voiceConfigurationBool(result["enabled"]) == desired, voiceConfigurationBool(after["enabled"]) == desired, voiceConfigurationBool(after["supported"]) == true, ["detector", "phrase", "effective_phrase"].allSatisfy({ voiceConfigurationJSON(after[$0] ?? NSNull()) == voiceConfigurationJSON(fresh[$0] ?? NSNull()) }) else { throw VoiceConfigurationFailure(message: "Requested wake state was not confirmed after toggling. Refresh before retrying.") }
                receipt = "Backend live wake detector is \(desired ? "enabled" : "disabled"). Real phrase recognition, capture and restart persistence are unverified."
            }
            guard start == generation else { return false }; return true
        } catch { if start == generation { actionError = "Action not confirmed. " + voiceConfigurationSafeError(error); switch issued.action { case .catalogue: errors["catalogue"] = voiceConfigurationSafeError(error); providers = nil; case .setting: errors["config"] = voiceConfigurationSafeError(error); case .wake: errors["wake"] = voiceConfigurationSafeError(error); default: break } }; return false }
    }
}

struct NativeVoiceConfigurationFeatureView: View {
    let baseURL: URL?
    @StateObject private var model: NativeVoiceConfigurationModel
    @State private var review: NativeSpeechConfigurationReview?
    @State private var realtime = ""
    @State private var primary = ""
    @State private var realtimeModel = ""
    @State private var legacyVoice = ""
    @State private var chainedDraft: [String: String] = [:]
    @State private var localError: String?
    init(baseURL: URL?) { self.baseURL = baseURL; _model = StateObject(wrappedValue: NativeVoiceConfigurationModel(baseURL: baseURL)) }
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack { Text("Speech settings").font(.title.bold()); Spacer(); Button("Refresh saved state") { Task { await model.refresh(); bindDrafts() } }.disabled(model.busy || baseURL == nil) }
            if let error = localError ?? model.actionError { Text(error).foregroundStyle(.red).textSelection(.enabled) }
            if let receipt = model.receipt { Text(receipt).textSelection(.enabled) }
            ForEach(model.errors.keys.sorted(), id: \.self) { key in Text("\(key): \(model.errors[key]!)").foregroundStyle(.red) }
            if model.busy { ProgressView("Working with the local agent…") }
            ScrollView { LazyVStack(alignment: .leading, spacing: 16) {
                card {
                    Text("Saved routing and subsystem status").font(.headline)
                    Text("Availability reports describe initialized backend components, not a working microphone, output device or selected account.").foregroundStyle(.secondary)
                    if let status = model.status { Text("Realtime: \(voiceConfigurationBool(status["realtime_available"]) == true ? "reported available" : "unavailable") · Audio: \(voiceConfigurationBool(status["audio_available"]) == true ? "reported available" : "unavailable")"); if let sessions = status["active_realtime_sessions"] as? Int { Text("Active realtime sessions: \(sessions)") } }
                    if baseURL == nil { Text("Start the app-owned agent to read speech settings.") }
                    TextField("Realtime fallback IDs, first to last, comma separated", text: $realtime)
                    Button("Review fallback order…") { prepare { try model.audioReview(key: "realtime_providers", value: realtime.split(separator: ",").map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }) } }
                    Text("Empty restores backend defaults, rather than disabling cloud fallback. Desktop may prefer local chained audio unless an explicit primary overrides surface policy.").font(.caption).foregroundStyle(.secondary)
                    TextField("Explicit realtime primary (empty inherits policy)", text: $primary)
                    Button("Review primary override…") { prepare { try model.audioReview(key: "realtime_primary", value: primary) } }
                    TextField("Realtime model identifier", text: $realtimeModel)
                    Button("Review realtime model…") { prepare { try model.audioReview(key: "realtime_model", value: realtimeModel) } }
                    TextField("Legacy audio TTS voice", text: $legacyVoice)
                    Button("Review legacy TTS voice…") { prepare { try model.audioReview(key: "tts_voice", value: legacyVoice) } }
                }
                card {
                    Text("Chained speech → AI → speech").font(.headline)
                    Text("Each edit merges the saved chained block. Empty fields inherit headless fallback settings or router/provider defaults. Identifiers are provider-specific; saving does not test them.").foregroundStyle(.secondary)
                    ForEach(NativeVoiceConfigurationModel.chainedKeys, id: \.self) { key in VStack(alignment: .leading) { TextField(key.replacingOccurrences(of: "_", with: " "), text: Binding(get: { chainedDraft[key] ?? "" }, set: { chainedDraft[key] = $0 })); Text("Saved resolution: \(model.effectiveChained(key))").font(.caption).foregroundStyle(.secondary); Button("Review \(key)…") { prepare { try model.chainedReview(key: key, value: chainedDraft[key] ?? "") } } } }
                }
                card {
                    Text("Provider catalogue and explicit tests").font(.headline)
                    Text("Loading this catalogue can probe every configured provider. It is never requested during passive refresh.").foregroundStyle(.secondary)
                    Button("Review catalogue connection checks…") { prepare { try model.catalogueReview() } }
                    if model.providers == nil { Text("Catalogue not loaded; saved values above remain visible without provider probes.").font(.caption) }
                    ForEach(model.providers ?? []) { provider in VStack(alignment: .leading, spacing: 6) { Text("\(provider.name) — \(provider.kind)").bold(); Text("\(provider.id): \(provider.verdict)").font(.caption); if !provider.models.isEmpty { Text("Catalogue models: " + provider.models.joined(separator: ", ")).font(.caption) }; HStack { Button("Review connection test…") { prepare { try model.probeReview(provider.id) } }; if provider.kind == "stt" || provider.kind == "tts" { Button("Review use for \(provider.kind)…") { prepare { try model.chainedReview(key: provider.kind + "_provider", value: provider.id) } } }; if provider.kind == "realtime" { Button("Review first fallback…") { prepare { try model.audioReview(key: "realtime_providers", value: [provider.id] + model.realtimeOrder.filter { $0 != provider.id }) } }; if model.realtimeOrder.contains(provider.id) { Button("Review removal…") { prepare { try model.audioReview(key: "realtime_providers", value: model.realtimeOrder.filter { $0 != provider.id }) } } } } } } }
                }
                card {
                    Text("Backend wake word").font(.headline)
                    if let wake = model.wake { Text(voiceConfigurationBool(wake["supported"]) == true ? "Detector \(wake["detector"] as? String ?? "unreported") is \(voiceConfigurationBool(wake["enabled"]) == true ? "enabled" : "disabled")" : "Wake detector unsupported or not initialized."); Text("Configured phrase: \(wake["phrase"] as? String ?? "unreported")"); Text("Effective recognized phrase: \(wake["effective_phrase"] as? String ?? "unreported")").bold(); if voiceConfigurationBool(wake["supported"]) == true { Button(voiceConfigurationBool(wake["enabled"]) == true ? "Review disabling wake detector…" : "Review enabling wake detector…") { prepare { try model.wakeReview(voiceConfigurationBool(wake["enabled"]) != true) } } } }
                    Text("This settings view does not request microphone access or start audio capture. Effective phrase may differ from the configured phrase.").font(.caption).foregroundStyle(.secondary)
                }
            }.disabled(model.busy || baseURL == nil).frame(maxWidth: .infinity, alignment: .leading) }
        }.padding(20).task(id: baseURL) { review = nil; model.configure(baseURL); if baseURL != nil { await model.refresh() }; bindDrafts() }
        .sheet(item: $review) { held in VStack(alignment: .leading, spacing: 14) { Text(held.title).font(.title2.bold()); ScrollView { VStack(alignment: .leading, spacing: 12) { Text(held.scope).textSelection(.enabled); if !held.previous.isEmpty { Text("Previous saved state").bold(); Text(held.previous).font(.system(.caption, design: .monospaced)).textSelection(.enabled) }; if !held.proposed.isEmpty { Text("Proposed value").bold(); Text(held.proposed).font(.system(.caption, design: .monospaced)).textSelection(.enabled) } } }; if let error = model.actionError { Text(error).foregroundStyle(.red) }; HStack { Spacer(); Button("Cancel") { review = nil }.disabled(model.busy).keyboardShortcut(.cancelAction); Button("Confirm") { Task { if await model.perform(held) { review = nil; bindDrafts() } } }.disabled(model.busy).keyboardShortcut(.defaultAction) } }.padding(24).frame(width: 620, height: 520).interactiveDismissDisabled(model.busy) }
    }
    private func bindDrafts() { realtime = model.realtimeOrder.joined(separator: ", "); primary = model.audio["realtime_primary"] as? String ?? ""; realtimeModel = model.audio["realtime_model"] as? String ?? ""; legacyVoice = model.audio["tts_voice"] as? String ?? ""; for key in NativeVoiceConfigurationModel.chainedKeys { chainedDraft[key] = model.chained[key] as? String ?? "" } }
    private func prepare(_ make: () throws -> NativeSpeechConfigurationReview) { do { localError = nil; review = try make() } catch { localError = voiceConfigurationSafeError(error) } }
    private func card<Content: View>(@ViewBuilder _ content: () -> Content) -> some View { VStack(alignment: .leading, spacing: 10, content: content).padding(16).frame(maxWidth: .infinity, alignment: .leading).background(Color.secondary.opacity(0.07), in: RoundedRectangle(cornerRadius: 12)) }
}
