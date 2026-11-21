import Foundation
import SwiftUI
import CoreFoundation

private struct AmbientFailure: LocalizedError { let message: String; var errorDescription: String? { message } }
private func ambientBool(_ value: Any?) -> Bool? { guard let n = value as? NSNumber, CFGetTypeID(n) == CFBooleanGetTypeID() else { return nil }; return n.boolValue }
private func ambientNumber(_ value: Any?) -> Double? { guard let n = value as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID(), n.doubleValue.isFinite else { return nil }; return n.doubleValue }
private func ambientJSON(_ value: Any) -> String { guard let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys, .fragmentsAllowed]) else { return "Unreadable" }; return String(decoding: data, as: UTF8.self) }
private func ambientError(_ error: Error) -> String { (error as? AmbientFailure)?.message ?? "The local request failed. Sensitive error details are withheld; refresh before retrying." }
final class NativeAmbientRedirectGuard: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
    static func session() -> URLSession { URLSession(configuration: .ephemeral, delegate: NativeAmbientRedirectGuard(), delegateQueue: nil) }
}
enum NativeAmbientTab: String, CaseIterable { case overview = "Overview", briefing = "Briefing", evening = "Wind-down", context = "Context", twin = "Twin" }
enum NativeAmbientAction { case briefing, calendar, ask(String), policy([String: Any]), removePolicy(String), approval(String, Bool) }
struct NativeAmbientReview: Identifiable { let id = UUID(); let generation: UUID; let action: NativeAmbientAction; let title: String, scope: String, previous: String, proposed: String, fingerprint: String }
struct NativeAmbientContext: Identifiable { let id: String, session: String, query: String, memory: String, filter: String; let time: Date?, latency: Double? }

@MainActor final class NativeAmbientModel: ObservableObject {
    @Published private(set) var payloads: [String: [String: Any]] = [:]
    @Published private(set) var errors: [String: String] = [:]
    @Published private(set) var fetched: [String: Date] = [:]
    @Published private(set) var contexts: [NativeAmbientContext] = []
    @Published private(set) var busy = false
    @Published private(set) var answer: String?
    @Published private(set) var answerQuestion: String?
    @Published private(set) var answerAt: Date?
    @Published private(set) var receipt: String?
    @Published private(set) var actionError: String?
    private var baseURL: URL?
    private var generation = UUID()
    private var issued: [UUID: NativeAmbientReview] = [:]
    private let session: URLSession
    static let reads = [("snapshot", "/api/ambient/snapshot"), ("wind", "/api/ambient/wind_down"), ("live", "/api/context/live"), ("memory", "/api/memory/context"), ("twin", "/api/twin/status"), ("policies", "/api/twin/policies"), ("approvals", "/api/twin/approvals")]
    init(baseURL: URL?, session: URLSession? = nil) { self.baseURL = baseURL; self.session = session ?? NativeAmbientRedirectGuard.session() }
    func configure(_ url: URL?) { guard baseURL != url else { return }; baseURL = url; generation = UUID(); payloads = [:]; errors = [:]; fetched = [:]; contexts = []; issued = [:]; answer = nil; answerQuestion = nil; answerAt = nil; receipt = nil; actionError = nil; busy = false }
    private func segment(_ id: String) throws -> String { guard !id.isEmpty, id.count <= 256, id != ".", id != "..", id.unicodeScalars.allSatisfy({ CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:").contains($0) }) else { throw AmbientFailure(message: "Unsupported policy or approval identity.") }; return id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed)! }
    private func request(_ path: String, method: String = "GET", body: [String: Any]? = nil, query: [URLQueryItem] = []) async throws -> [String: Any] {
        try Task.checkCancellation(); let start = generation
        guard let baseURL, ["http", "https"].contains(baseURL.scheme ?? ""), ["127.0.0.1", "::1", "[::1]"].contains(baseURL.host ?? ""), baseURL.user == nil, baseURL.password == nil, var parts = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else { throw AmbientFailure(message: "Start the app-owned local agent to read ambient context.") }
        parts.percentEncodedPath = path; parts.queryItems = query.isEmpty ? nil : query; parts.fragment = nil
        guard let url = parts.url else { throw AmbientFailure(message: "Invalid local ambient request.") }
        var req = URLRequest(url: url); req.httpMethod = method; req.timeoutInterval = 60
        if let body { req.httpBody = try JSONSerialization.data(withJSONObject: body); req.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        let (data, response) = try await session.feralLocalData(for: req); try Task.checkCancellation()
        guard generation == start else { throw AmbientFailure(message: "The agent connection changed. Review again.") }
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode), let value = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { throw AmbientFailure(message: "The local agent returned unavailable or malformed ambient state.") }
        if value["error"] != nil && !(value["error"] is NSNull) || ambientBool(value["success"]) == false || ambientBool(value["ok"]) == false { throw AmbientFailure(message: "The backend could not confirm this ambient action. Error details are withheld.") }
        return value
    }
    private func validated(_ row: [String: Any], key: String) throws -> [String: Any] {
        let valid: Bool
        switch key {
        case "snapshot": valid = row["time"] is String && ["briefing", "desk", "wind_down"].contains(row["suggested_mode"] as? String ?? "") && row["vitals"] is [String: Any]
        case "wind": valid = row["day_recap"] is [String: Any] && row["episodes"] is [[String: Any]] && row["sleep_prep"] is [String: Any] && row["journal_prompt"] is String
        case "live": valid = row["perception_text"] is String && row["sensors"] is [String: Any] && row["vision"] is [String: Any] && row["somatic"] is [String: Any] && ambientNumber(row["timestamp"]) != nil
        case "memory": valid = row["snapshots"] is [[String: Any]]
        case "twin": valid = ambientNumber(row["policies"]) != nil && ambientNumber(row["pending_approvals"]) != nil && ambientBool(row["supervisor_paused"]) != nil
        case "policies": valid = row["policies"] is [[String: Any]] && row["disconnected"] is [[String: Any]] && row["available"] is [[String: Any]]
        case "approvals": valid = row["approvals"] is [[String: Any]]
        case "briefing": valid = row["greeting"] is String && row["agenda"] is [[String: Any]] && row["goals"] is [[String: Any]] && row["degraded"] is [String]
        default: valid = true
        }
        if key == "wind" {
            guard let recap = row["day_recap"] as? [String: Any], recap["completed_tasks"] is [[String: Any]], let prep = row["sleep_prep"] as? [String: Any], ambientNumber(prep["time_to_bed_min"]) != nil, prep["hints"] is [String], row["degraded"] is [String] else { throw AmbientFailure(message: "Wind-down sections are incomplete; empty activity is not confirmed.") }
        }
        if key == "policies" {
            let policies = (row["policies"] as? [[String: Any]] ?? []) + (row["disconnected"] as? [[String: Any]] ?? [])
            guard policies.allSatisfy({ item in item["domain"] is String && ["draft_only", "auto_send", "disabled"].contains(item["mode"] as? String ?? "") && item["time_windows"] is [String] && ambientNumber(item["max_per_day"]) != nil && ambientBool(item["requires_user_online"]) != nil }) else { throw AmbientFailure(message: "Delegation policy fields are malformed; authorization is unconfirmed.") }
        }
        guard valid else { throw AmbientFailure(message: "\(key.capitalized) state is incomplete; it is not confirmed empty.") }; return row
    }
    func refresh() async { guard !busy else { return }; let start = generation; busy = true; defer { if start == generation { busy = false } }; await passive(start) }
    private func passive(_ start: UUID) async {
        for (key, path) in Self.reads {
            guard start == generation else { return }
            do { let query = key == "memory" ? [URLQueryItem(name: "limit", value: "20")] : key == "approvals" ? [URLQueryItem(name: "status", value: "pending"), URLQueryItem(name: "limit", value: "50")] : []
                let row = try validated(await request(path, query: query), key: key)
                let parsed = key == "memory" ? try parseContexts(row) : []
                guard start == generation else { return }; payloads[key] = row; fetched[key] = Date(); errors[key] = nil; if key == "memory" { contexts = parsed }
            } catch { if start == generation { errors[key] = ambientError(error) } }
        }
    }
    private func parseContexts(_ row: [String: Any]) throws -> [NativeAmbientContext] {
        guard let rows = row["snapshots"] as? [[String: Any]], rows.count <= 50 else { throw AmbientFailure(message: "Memory context ring is unreadable or exceeds its documented bound.") }
        return try rows.enumerated().map { index, value in guard let sid = value["session_id"] as? String, let memory = value["memory_context"] as? String, let query = value["query"] as? String else { throw AmbientFailure(message: "A memory-context row is malformed.") }; let stamp = ambientNumber(value["ts"]); return NativeAmbientContext(id: "\(sid):\(stamp ?? -1):\(index)", session: sid.isEmpty ? "unreported" : sid, query: query, memory: memory, filter: value["memory_filter"] as? String ?? "", time: stamp.flatMap { $0 > 0 ? Date(timeIntervalSince1970: $0) : nil }, latency: ambientNumber(value["latency_ms"])) }
    }
    func rows(_ section: String, _ key: String) -> [[String: Any]] { payloads[section]?[key] as? [[String: Any]] ?? [] }
    func degraded(_ section: String) -> [String] { let raw = payloads[section]?["degraded"] as? [String] ?? []; let known = ["sleep", "agenda", "weather", "goals", "vip_emails", "completed_tasks", "key_episodes", "vitals"]; return Array(Set(raw.map { text in known.first(where: { text == $0 || text.hasPrefix($0 + ":") }) ?? "another section" })).sorted() }
    private func make(_ action: NativeAmbientAction, title: String, scope: String, previous: String = "", proposed: String = "", fingerprint: String = "") -> NativeAmbientReview { if issued.count >= 20 { issued.removeAll() }; let review = NativeAmbientReview(generation: generation, action: action, title: title, scope: scope, previous: previous, proposed: proposed, fingerprint: fingerprint); issued[review.id] = review; return review }
    func briefingReview() throws -> NativeAmbientReview { guard !busy else { throw AmbientFailure(message: "Wait for the current request.") }; return make(.briefing, title: "Fetch your briefing and optional weather?", scope: "Reads saved goals, rolling baselines and optional VIP recall. If a weather key exists, the backend reads its vault and sends configured coordinates (or server defaults) to OpenWeather. It does not generate an LLM summary. Weather location is not returned; it must not be assumed to be your current location. No microphone or sensor is started.") }
    func calendarReview() throws -> NativeAmbientReview { guard !busy else { throw AmbientFailure(message: "Wait for the current request.") }; return make(.calendar, title: "Look up the next calendar event?", scope: "Calls your configured calendar integration using its existing account credentials. This can refresh tokens and contact the external calendar service. The result stays in this local view; no event is created or changed.") }
    func askReview(_ question: String) throws -> NativeAmbientReview { let question = question.trimmingCharacters(in: .whitespacesAndNewlines); guard !busy, !question.isEmpty, question.count <= 4000 else { throw AmbientFailure(message: "Enter a question up to 4,000 characters.") }; return make(.ask(question), title: "Ask the memory-based twin?", scope: "The backend assembles identity/personality, up to 20 recent global episodes and knowledge-graph context, then sends those records and this question to the configured model and any fallbacks. Cloud models may receive private data and consume budget. This GET carries the question in its URL, which may be logged. The answer is an inference, not your verified preference or a medical diagnosis, and does not execute an action.", proposed: question) }
    private func policyFields(_ row: [String: Any]) -> [String: Any] { var result: [String: Any] = [:]; for key in ["domain", "mode", "time_windows", "max_per_day", "requires_user_online"] { if let value = row[key] { result[key] = value } }; return result }
    func policyReview(domain: String, mode: String, windows: String, cap: String, online: Bool) throws -> NativeAmbientReview {
        _ = try segment(domain)
        guard !busy, errors["policies"] == nil, payloads["policies"] != nil, ["draft_only", "auto_send", "disabled"].contains(mode), let maximum = Int(cap), maximum >= 0 else { throw AmbientFailure(message: "Refresh policies and enter a supported mode and non-negative daily cap.") }
        let original = rows("policies", "policies").first { $0["domain"] as? String == domain }
        guard original != nil || rows("policies", "available").contains(where: { $0["domain"] as? String == domain }) else { throw AmbientFailure(message: "Choose a currently reported domain; disconnected policies cannot be enabled here.") }
        let ranges = windows.split(separator: ",").map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
        for range in ranges { let ends = range.split(separator: "-"); guard ends.count == 2 else { throw AmbientFailure(message: "Use HH:MM-HH:MM windows, comma separated.") }; for end in ends { let parts = end.split(separator: ":"); guard parts.count == 2, let h = Int(parts[0]), let m = Int(parts[1]), (0...23).contains(h), (0...59).contains(m) else { throw AmbientFailure(message: "Time windows require valid hours and minutes.") } } }
        let body: [String: Any] = ["domain": domain, "mode": mode, "time_windows": ranges, "max_per_day": maximum, "requires_user_online": online]
        return make(.policy(body), title: "Change delegation for \(domain)?", scope: "\(mode == "auto_send" ? "Auto-send authorizes future domain actions without individual approval, subject to supervisor, windows, online condition and cap." : mode == "disabled" ? "Disabled denies future domain actions." : "Draft-only queues future actions for review.") Daily cap zero is unlimited. Empty windows allow every time; windows use backend-local clock. This does not revoke upstream credentials or cancel already-running requests. Domain wiring must be verified separately.", previous: ambientJSON(original.map(policyFields) ?? [:]), proposed: ambientJSON(body), fingerprint: ambientJSON(payloads["policies"]!))
    }
    func removePolicyReview(_ domain: String) throws -> NativeAmbientReview { _ = try segment(domain); guard !busy, errors["policies"] == nil, let row = (rows("policies", "policies") + rows("policies", "disconnected")).first(where: { $0["domain"] as? String == domain }) else { throw AmbientFailure(message: "Refresh this policy before deleting it.") }; return make(.removePolicy(domain), title: "Remove delegation policy for \(domain)?", scope: "Deletes the saved policy. A missing policy defaults to draft-only rather than disabling the domain. Existing approvals, credentials and external actions remain.", previous: ambientJSON(policyFields(row)), fingerprint: ambientJSON(payloads["policies"]!)) }
    func approvalReview(_ id: String, allow: Bool) throws -> NativeAmbientReview { _ = try segment(id); guard !busy, errors["approvals"] == nil, let row = rows("approvals", "approvals").first(where: { $0["approval_id"] as? String == id && $0["status"] as? String == "pending" }) else { throw AmbientFailure(message: "Refresh the exact pending delegation request.") }; return make(.approval(id, allow), title: allow ? "Approve this delegated action?" : "Reject this delegated action?", scope: "Domain: \(row["domain"] as? String ?? "unreported"). Action: \(row["action"] as? String ?? "unreported"). Approval records a decision; subsequent external execution can fail and is not confirmed by this receipt. Stored action context is shown below for review; it may contain private messages.", previous: ambientJSON(row["context"] as? [String: Any] ?? [:]), fingerprint: ambientJSON(row)) }
    func perform(_ review: NativeAmbientReview) async -> Bool {
        guard !busy, review.generation == generation, let held = issued.removeValue(forKey: review.id) else { actionError = "This review is stale or already used."; return false }
        let start = generation; busy = true; receipt = nil; actionError = nil; defer { if start == generation { busy = false } }
        do {
            switch held.action {
            case .briefing: let value = try validated(await request("/api/ambient/briefing"), key: "briefing"); payloads["briefing"] = value; fetched["briefing"] = Date(); errors["briefing"] = nil; receipt = "Briefing received; source measurement/event timestamps are not supplied by this endpoint."
            case .calendar:
                let value = try await request("/api/ambient/next_event")
                if value["degraded"] != nil { throw AmbientFailure(message: "Calendar lookup failed; existing results are stale. No account reconnection is assumed necessary.") }
                guard value["summary"] is String || value["event"] is NSNull || value["message"] is String else { throw AmbientFailure(message: "Calendar response is incomplete.") }
                payloads["calendar"] = value; fetched["calendar"] = Date(); errors["calendar"] = nil; receipt = value["summary"] is String ? "Next event received from the configured integration." : "No event was returned; this may mean no upcoming event or no connected calendar."
            case .ask(let question):
                answer = nil; answerQuestion = nil; answerAt = nil
                let value = try await request("/api/digital-twin/ask", query: [URLQueryItem(name: "question", value: question)])
                guard value["question"] as? String == question, let text = value["answer"] as? String, !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { throw AmbientFailure(message: "The twin did not return a usable answer for this question.") }
                answer = text; answerQuestion = question; answerAt = Date(); receipt = "Memory-based inference returned. The API does not prove provider success, preference accuracy or source completeness."
            case .policy, .removePolicy:
                let value = try validated(await request("/api/twin/policies"), key: "policies"); payloads["policies"] = value
                guard ambientJSON(value) == held.fingerprint else { throw AmbientFailure(message: "Delegation policies changed; review the refreshed inventory.") }
                if case .policy(let body) = held.action {
                    guard let domain = body["domain"] as? String else { throw AmbientFailure(message: "Missing policy domain.") }
                    let result = try await request("/api/twin/policies", method: "POST", body: body)
                    guard ambientBool(result["success"]) == true, result["domain"] as? String == domain else { throw AmbientFailure(message: "Policy save is not acknowledged.") }
                    let after = try validated(await request("/api/twin/policies"), key: "policies"); payloads["policies"] = after
                    guard let saved = ((after["policies"] as? [[String: Any]] ?? []) + (after["disconnected"] as? [[String: Any]] ?? [])).first(where: { $0["domain"] as? String == domain }), ambientJSON(policyFields(saved)) == ambientJSON(body) else { throw AmbientFailure(message: "Saved policy readback differs.") }
                    receipt = "Exact delegation policy saved and read back; real domain execution is unverified."
                } else if case .removePolicy(let domain) = held.action {
                    let result = try await request("/api/twin/policies/" + (try segment(domain)), method: "DELETE")
                    guard ambientBool(result["success"]) == true else { throw AmbientFailure(message: "Policy removal was not confirmed.") }
                    let after = try validated(await request("/api/twin/policies"), key: "policies"); payloads["policies"] = after
                    guard !(rows("policies", "policies") + rows("policies", "disconnected")).contains(where: { $0["domain"] as? String == domain }) else { throw AmbientFailure(message: "Policy removal readback still contains this domain.") }
                    receipt = "Saved policy removed and absence read back. Future actions fall back to draft-only, not disabled."
                }
                await passive(start)
            case .approval(let id, let allow):
                let latest = try validated(await request("/api/twin/approvals", query: [URLQueryItem(name: "status", value: "pending"), URLQueryItem(name: "limit", value: "50")]), key: "approvals"); payloads["approvals"] = latest
                guard let row = (latest["approvals"] as? [[String: Any]])?.first(where: { $0["approval_id"] as? String == id }), row["status"] as? String == "pending", ambientJSON(row) == held.fingerprint else { throw AmbientFailure(message: "Delegated action changed; review its current context.") }
                let result = try await request("/api/twin/approvals/" + (try segment(id)) + (allow ? "/approve" : "/reject"), method: "POST")
                guard ambientBool(result["success"]) == true, result["status"] as? String == (allow ? "approved" : "rejected") else { throw AmbientFailure(message: "Delegation decision was not confirmed.") }; receipt = allow ? "Delegation approved; execution is not confirmed by this decision receipt." : "Delegation rejected; past actions are not undone."
                await passive(start)
            }
            guard start == generation else { return false }; return true
        } catch { if start == generation { actionError = "Action not confirmed. " + ambientError(error); switch held.action { case .briefing: errors["briefing"] = ambientError(error); case .calendar: errors["calendar"] = ambientError(error); case .policy, .removePolicy: errors["policies"] = ambientError(error); case .approval: errors["approvals"] = ambientError(error); default: break } }; return false }
    }
}

struct NativeAmbientFeatureView: View {
    let baseURL: URL?
    @StateObject private var model: NativeAmbientModel
    @State private var tab = NativeAmbientTab.overview
    @State private var review: NativeAmbientReview?
    @State private var localError: String?
    @State private var question = ""
    @State private var domain = ""
    @State private var mode = "draft_only"
    @State private var windows = ""
    @State private var cap = "10"
    @State private var online = true
    init(baseURL: URL?) { self.baseURL = baseURL; _model = StateObject(wrappedValue: NativeAmbientModel(baseURL: baseURL)) }
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack { Text("Your day & context").font(.title2.bold()); Spacer(); Button("Refresh local state") { Task { await model.refresh() } }.disabled(model.busy || baseURL == nil) }
            Text("Navigation reads state through the local agent, including its configured memory storage. Weather, calendar and model requests require a separate review.").font(.caption).foregroundColor(.secondary)
            Picker("Section", selection: $tab) { ForEach(NativeAmbientTab.allCases, id: \.self) { Text($0.rawValue).tag($0) } }.pickerStyle(.segmented)
            if baseURL == nil { Text("Start the local agent to see saved context. No services are contacted while it is unavailable.") }
            if let text = model.receipt { NativeSelectableText(text).foregroundColor(.secondary) }
            if let text = model.actionError ?? localError { NativeSelectableText(text).foregroundColor(.red) }
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    switch tab {
                    case .overview: overview
                    case .briefing: briefing
                    case .evening: evening
                    case .context: context
                    case .twin: twin
                    }
                }.frame(maxWidth: .infinity, alignment: .leading).padding(2)
            }
        }.padding(18)
        .task { await model.refresh() }
        .onChange(of: baseURL) { value in review = nil; localError = nil; model.configure(value); Task { await model.refresh() } }
        .sheet(item: $review) { value in
            VStack(alignment: .leading, spacing: 14) {
                Text(value.title).font(.title3.bold())
                ScrollView { VStack(alignment: .leading, spacing: 12) { Text(value.scope); if !value.previous.isEmpty { Text("Current / stored context").bold(); NativeSelectableText(value.previous) }; if !value.proposed.isEmpty { Text("Proposed request").bold(); NativeSelectableText(value.proposed) } }.frame(maxWidth: .infinity, alignment: .leading) }.frame(maxHeight: 380)
                HStack { Spacer(); Button("Cancel") { review = nil }; Button("Confirm reviewed request") { review = nil; Task { _ = await model.perform(value) } }.disabled(model.busy || baseURL == nil) }
            }.padding(22).frame(width: 580)
        }
    }
    private func prepare(_ action: () throws -> NativeAmbientReview) { do { review = try action(); localError = nil } catch { localError = ambientError(error) } }
    private func card<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View { VStack(alignment: .leading, spacing: 8) { Text(title).font(.headline); content() }.frame(maxWidth: .infinity, alignment: .leading).padding(14).background(Color.primary.opacity(0.035)).cornerRadius(10) }
    @ViewBuilder private func freshness(_ section: String) -> some View {
        if let error = model.errors[section] { Text("Unavailable / prior data stale: " + error).font(.caption).foregroundColor(.orange) }
        if let date = model.fetched[section] { Text("Last received locally: " + date.formatted(date: .abbreviated, time: .standard)).font(.caption).foregroundColor(.secondary) }
        else { Text("No confirmed response yet.").font(.caption).foregroundColor(.secondary) }
        let degraded = model.degraded(section)
        if !degraded.isEmpty { Text("Partial response; unavailable sections: " + degraded.joined(separator: ", ")).font(.caption).foregroundColor(.orange) }
    }
    private func list(_ rows: [[String: Any]], keys: [String], empty: String) -> some View {
        VStack(alignment: .leading, spacing: 6) { if rows.isEmpty { Text(empty).foregroundColor(.secondary) }; ForEach(Array(rows.enumerated()), id: \.offset) { _, row in NativeSelectableText(keys.compactMap { row[$0] as? String }.first ?? "Unnamed record") } }
    }
    private var overview: some View {
        VStack(alignment: .leading, spacing: 14) {
            card("Day mode") { let snapshot = model.payloads["snapshot"]; Text(snapshot?["suggested_mode"] as? String ?? "Unavailable"); Text("A clock-based suggestion using backend-local time, not a detected activity or health assessment.").font(.caption); if let time = snapshot?["time"] as? String { Text("Server clock: " + time).font(.caption) }; freshness("snapshot") }
            card("Next event") { Button("Review calendar lookup…") { prepare { try model.calendarReview() } }.disabled(model.busy || baseURL == nil); if let event = model.payloads["calendar"] { if let title = event["summary"] as? String { Text(title).bold(); ForEach(["start", "end", "location"], id: \.self) { key in if let value = event[key] as? String, !value.isEmpty { Text(key.capitalized + ": " + value) } } } else { Text("No event returned. This may mean no upcoming event or no configured calendar.") } }; freshness("calendar") }
            card("Health context") { Text("Use Health for sourced measurements. Ambient snapshot defaults and missing sample timestamps cannot establish current heart rate, oxygen, temperature or battery; they are deliberately omitted here.").font(.caption) }
        }
    }
    private var briefing: some View {
        VStack(alignment: .leading, spacing: 14) {
            Button("Review briefing & optional weather…") { prepare { try model.briefingReview() } }.disabled(model.busy || baseURL == nil)
            card("Briefing") { if let row = model.payloads["briefing"] { Text(row["greeting"] as? String ?? "Briefing").bold(); list(model.rows("briefing", "agenda"), keys: ["title", "description", "action", "intent"], empty: "No agenda items returned; this is not proof all integrations are connected."); Text("Goals").bold(); list(model.rows("briefing", "goals"), keys: ["title"], empty: "No active goals returned.") }; freshness("briefing") }
            card("Weather") { if let weather = model.payloads["briefing"]?["weather"] as? [String: Any] { Text(weather["description"] as? String ?? "Condition unreported"); if let temperature = ambientNumber(weather["temp_c"]) { Text(String(format: "Reported temperature %.1f °C", temperature)) }; if let hint = weather["outfit_hint"] as? String { Text(hint) } } else { Text("Weather unavailable or not configured.") }; Text("Location and observation time are not returned. Backend default coordinates can differ from your location.").font(.caption).foregroundColor(.secondary) }
            card("Rolling HRV baseline") { if let sleep = model.payloads["briefing"]?["sleep"] as? [String: Any], let hrv = ambientNumber(sleep["hrv_ms"]) { Text(String(format: "Rolling mean %.1f ms", hrv)); Text("Samples: \(ambientNumber(sleep["samples"]).map { String(Int($0)) } ?? "unreported"); trend: \(sleep["trend"] as? String ?? "unreported")") } else { Text("No rolling HRV baseline returned.") }; Text("This backend field is called sleep, but it is not a sleep score, diagnosis or current sample. Source timestamps are unavailable.").font(.caption).foregroundColor(.secondary) }
        }
    }
    private var evening: some View {
        VStack(alignment: .leading, spacing: 14) {
            card("Recorded day recap") { let recap = model.payloads["wind"]?["day_recap"] as? [String: Any] ?? [:]; list(recap["completed_tasks"] as? [[String: Any]] ?? [], keys: ["action", "title", "intent", "description"], empty: "No completed task records returned."); Text("Recorded completion is not independent proof an external action succeeded.").font(.caption); list(model.rows("wind", "episodes"), keys: ["summary"], empty: "No recent same-day episodes returned."); freshness("wind") }
            card("Wind-down suggestions") { let prep = model.payloads["wind"]?["sleep_prep"] as? [String: Any] ?? [:]; ForEach(prep["hints"] as? [String] ?? [], id: \.self) { Text($0) }; if let minutes = ambientNumber(prep["time_to_bed_min"]) { Text("Backend countdown: \(Int(minutes)) minutes") }; Text("Uses a fixed 23:00 backend-local bedtime, not your personal schedule or medical advice.").font(.caption).foregroundColor(.secondary); if let prompt = model.payloads["wind"]?["journal_prompt"] as? String { Text(prompt).italic() }; Text("Static prompts; this view does not generate or save a journal entry.").font(.caption) }
        }
    }
    private var context: some View {
        VStack(alignment: .leading, spacing: 14) {
            card("Local live context") { Text("This endpoint selects the first active backend session, which may differ from your selected conversation. Text is inference / recorded context, not a medical measurement.").font(.caption); if let live = model.payloads["live"] { NativeSelectableText(live["perception_text"] as? String ?? ""); if let hardware = live["hardware_context"] as? String { NativeSelectableText(hardware) }; if let vision = live["vision"] as? [String: Any] { Text(ambientBool(vision["active"]) == true ? "Vision marked active by backend; this view does not start capture." : "Vision not marked active."); if let scene = vision["scene_description"] as? String { NativeSelectableText(scene) } } }; freshness("live") }
            card("Recent memory context assemblies") { Text("Private prompt-context records across sessions. Local inspection only; refreshing does not send these records to a model. This in-memory ring clears on backend restart.").font(.caption); freshness("memory"); if model.contexts.isEmpty { Text("No confirmed context records to display.") }; ForEach(model.contexts) { item in DisclosureGroup("Session \(item.session) • \(item.time?.formatted(date: .abbreviated, time: .standard) ?? "source time unknown")") { VStack(alignment: .leading, spacing: 8) { NativeSelectableText("Query: " + item.query); NativeSelectableText("Filter: " + item.filter); NativeSelectableText(item.memory) }.frame(maxWidth: .infinity, alignment: .leading) } } }
        }
    }
    private var twin: some View {
        VStack(alignment: .leading, spacing: 14) {
            card("Memory-based twin") { Text("Answers infer preferences from identity and globally recent memory; they can be wrong and do not execute actions.").font(.caption); TextField("What would you like to ask?", text: $question); Button("Review model request…") { prepare { try model.askReview(question) } }.disabled(model.busy || baseURL == nil); if let answer = model.answer { Text(model.answerQuestion ?? "").bold(); NativeSelectableText(answer); if let at = model.answerAt { Text("Answer received " + at.formatted(date: .abbreviated, time: .standard)).font(.caption) } } }
            card("Delegation status") { if let paused = ambientBool(model.payloads["twin"]?["supervisor_paused"]) { Text(paused ? "Supervisor paused" : "Supervisor not paused") }; Text("Pending requests shown: \(model.rows("approvals", "approvals").count) (up to 50). Status endpoint's pending count is presence-only, not a total.").font(.caption); freshness("twin"); freshness("approvals"); ForEach(Array(model.rows("approvals", "approvals").enumerated()), id: \.offset) { _, row in if let id = row["approval_id"] as? String { Text("\(row["domain"] as? String ?? "Unknown domain"): \(row["action"] as? String ?? "Unknown action")"); HStack { Button("Review approval…") { prepare { try model.approvalReview(id, allow: true) } }; Button("Review rejection…") { prepare { try model.approvalReview(id, allow: false) } } }.disabled(model.busy) } } }
            card("Delegation policies") { freshness("policies"); ForEach(Array((model.rows("policies", "policies") + model.rows("policies", "disconnected")).enumerated()), id: \.offset) { _, row in if let id = row["domain"] as? String { HStack { Text(id + " • " + (row["mode"] as? String ?? "Unknown mode")); Spacer(); Button("Edit") { domain = id; mode = row["mode"] as? String ?? "draft_only"; windows = (row["time_windows"] as? [String] ?? []).joined(separator: ", "); cap = ambientNumber(row["max_per_day"]).map { String(Int($0)) } ?? "10"; online = ambientBool(row["requires_user_online"]) ?? true }; Button("Review removal…") { prepare { try model.removePolicyReview(id) } } }.disabled(model.busy) } }; Text("Available domains: " + model.rows("policies", "available").compactMap { $0["domain"] as? String }.joined(separator: ", ")).font(.caption); TextField("Reported domain", text: $domain); Picker("Future action mode", selection: $mode) { Text("Draft for review").tag("draft_only"); Text("Auto-send").tag("auto_send"); Text("Disabled").tag("disabled") }; TextField("Time windows HH:MM-HH:MM, comma separated; empty = all time", text: $windows); TextField("Daily cap; zero = unlimited", text: $cap); Toggle("Require user online", isOn: $online); Button("Review policy change…") { prepare { try model.policyReview(domain: domain, mode: mode, windows: windows, cap: cap, online: online) } }.disabled(model.busy || baseURL == nil) }
        }
    }
}
