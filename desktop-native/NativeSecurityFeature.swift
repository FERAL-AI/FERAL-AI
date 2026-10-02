import SwiftUI
import AppKit
import Foundation
import CoreFoundation

private struct SecurityFeatureFailure: LocalizedError { let message: String; var errorDescription: String? { message } }
struct NativeSecurityReview: Identifiable {
    let id = UUID()
    let generation: UUID
    let section: String
    let path: String
    let method: String
    let query: [URLQueryItem]
    let body: [String: Any]?
    let preparatoryBodies: [[String: Any]]
    let previous: String
    let proposed: String
    let scope: String
}
enum NativeSecurityWire {
    static func json(_ value: Any) -> String { guard let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys, .prettyPrinted, .fragmentsAllowed]) else { return "Unavailable" }; return String(decoding: data, as: UTF8.self) }
    static func bool(_ value: Any?) -> Bool? { guard let n = value as? NSNumber, CFGetTypeID(n) == CFBooleanGetTypeID() else { return nil }; return n.boolValue }
    static func cap(_ raw: String) throws -> Any {
        if raw.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return NSNull() }
        guard let value = Double(raw), value.isFinite, value >= 0 else { throw SecurityFeatureFailure(message: "Enter a finite non-negative amount, or leave it empty to remove the limit.") }
        return value
    }
}

// Security writes must never be replayed at another URL, including via 307/308.
final class NativeSecurityRedirectGuard: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
    static func session() -> URLSession {
        let config = URLSessionConfiguration.ephemeral; config.timeoutIntervalForRequest = 30; config.timeoutIntervalForResource = 45
        return URLSession(configuration: config, delegate: NativeSecurityRedirectGuard(), delegateQueue: nil)
    }
}

@MainActor final class NativeSecurityModel: ObservableObject {
    @Published private(set) var data: [String: [String: Any]] = [:]
    @Published private(set) var errors: [String: String] = [:]
    @Published private(set) var receipt: String?
    @Published private(set) var busy = false
    private var baseURL: URL?
    private var generation = UUID()
    private var issuedReviews: [UUID: NativeSecurityReview] = [:]
    private let session: URLSession
    static let paths = ["grants": "/api/security/grants", "permissions": "/api/security/permissions", "policy": "/api/policy", "audit": "/api/security/audit", "cost": "/api/config"]
    static let tiers = ["passive", "active", "privileged", "dangerous"]
    static let sites = ["chat", "vision", "embedding", "routing", "screen_loop", "proactive", "learner", "compaction"]
    var costSites: [String] {
        let cost = data["cost"]?["cost"] as? [String: Any] ?? [:]
        let legacy = cost["per_call_site_caps"] as? [String: Any] ?? [:]
        let extra = cost.compactMap { key, value -> String? in
            guard key != "per_call_site_caps", let row = value as? [String: Any], row.keys.contains("per_hour_usd") else { return nil }; return key
        }
        return Set(Self.sites + extra + Array(legacy.keys)).sorted()
    }
    init(baseURL: URL?, session: URLSession? = nil) {
        self.baseURL = baseURL
        self.session = session ?? NativeSecurityRedirectGuard.session()
    }
    func configure(_ url: URL?) { guard url != baseURL else { return }; generation = UUID(); issuedReviews = [:]; baseURL = url; data = [:]; errors = [:]; receipt = nil; busy = false }
    private func request(_ path: String, method: String = "GET", query: [URLQueryItem] = [], body: [String: Any]? = nil) async throws -> [String: Any] {
        let current = generation; try Task.checkCancellation()
        guard let baseURL, ["127.0.0.1", "::1", "[::1]"].contains(baseURL.host ?? ""), ["http", "https"].contains(baseURL.scheme ?? ""), baseURL.user == nil, baseURL.password == nil else { throw SecurityFeatureFailure(message: "Connect to the local agent's loopback address to inspect security settings.") }
        var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)!; components.path = path; components.queryItems = query.isEmpty ? nil : query
        var request = URLRequest(url: components.url!); request.httpMethod = method
        if let body { request.setValue("application/json", forHTTPHeaderField: "Content-Type"); request.httpBody = try JSONSerialization.data(withJSONObject: body) }
        let (bytes, response) = try await session.data(for: request)
        try Task.checkCancellation(); guard current == generation else { throw CancellationError() }
        guard let object = try JSONSerialization.jsonObject(with: bytes) as? [String: Any] else { throw SecurityFeatureFailure(message: "The security response is unreadable.") }
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else { throw SecurityFeatureFailure(message: object["detail"] as? String ?? (object["detail"] as? [String: Any])?["message"] as? String ?? "The agent refused this security request.") }
        if let error = object["error"] as? String, !error.isEmpty { throw SecurityFeatureFailure(message: error) }
        return object
    }
    private func validated(_ object: [String: Any], section: String) throws -> [String: Any] {
        var normalized = object
        let valid: Bool
        switch section {
        case "grants": valid = object["grants"] is [[String: Any]]
        case "permissions": valid = (object["max_tier"] as? String).map { Self.tiers.contains($0) } == true && object["tiers"] is [String]
        case "audit": valid = object["entries"] is [[String: Any]]
        case "cost":
            // An absent optional section means no configured caps; malformed present data must still fail.
            if !object.keys.contains("cost") { normalized["cost"] = [String: Any](); valid = true }
            else { valid = object["cost"] is [String: Any] }
        default: valid = !object.isEmpty
        }
        guard valid else { throw SecurityFeatureFailure(message: "The agent did not report a usable \(section) configuration.") }; return normalized
    }
    func refresh() async {
        guard !busy else { return }; let current = generation; busy = true; receipt = nil
        defer { if generation == current { busy = false } }
        for section in Self.paths.keys.sorted() {
            do { let value = try validated(await request(Self.paths[section]!), section: section); guard generation == current else { return }; data[section] = value; errors.removeValue(forKey: section) }
            catch { guard generation == current else { return }; errors[section] = error.localizedDescription }
        }
    }
    private func snapshot(_ section: String, _ value: [String: Any]) -> String { NativeSecurityWire.json(section == "cost" ? value["cost"] ?? [:] : value) }
    private func review(section: String, path: String, method: String = "POST", query: [URLQueryItem] = [], body: [String: Any]?, preparatoryBodies: [[String: Any]] = [], proposed: String, scope: String) throws -> NativeSecurityReview {
        guard !busy, errors[section] == nil, let existing = data[section] else { throw SecurityFeatureFailure(message: "Refresh this section successfully before reviewing a change.") }
        let result = NativeSecurityReview(generation: generation, section: section, path: path, method: method, query: query, body: body, preparatoryBodies: preparatoryBodies, previous: snapshot(section, existing), proposed: proposed, scope: scope)
        issuedReviews = [result.id: result]
        return result
    }
    func grantReview(path: String, mode: String) throws -> NativeSecurityReview {
        guard path.hasPrefix("/"), ["read", "readwrite"].contains(mode) else { throw SecurityFeatureFailure(message: "Choose an absolute folder path and a supported access mode.") }
        return try review(section: "grants", path: Self.paths["grants"]!, body: ["path": path, "mode": mode], proposed: NativeSecurityWire.json(["path": path, "mode": mode]), scope: "Grant \(mode == "read" ? "read-only" : "read and write") access to this folder and its descendants. Other tool permissions still apply. A workspace grant can also satisfy the strict developer-shell workspace requirement.")
    }
    func revokeReview(path: String) throws -> NativeSecurityReview {
        guard (data["grants"]?["grants"] as? [[String: Any]] ?? []).contains(where: { $0["path"] as? String == path }) else { throw SecurityFeatureFailure(message: "Refresh the grant before revoking it.") }
        return try review(section: "grants", path: Self.paths["grants"]!, method: "DELETE", query: [URLQueryItem(name: "path", value: path)], body: nil, proposed: "Revoke explicit grant: " + path, scope: "Remove this explicit workspace grant. Other policy paths or broader grants may still allow access; this does not establish that all access is denied.")
    }
    func tierReview(_ tier: String) throws -> NativeSecurityReview {
        guard Self.tiers.contains(tier), (data["permissions"]?["tiers"] as? [String] ?? []).contains(tier) else { throw SecurityFeatureFailure(message: "Unsupported permission tier.") }
        return try review(section: "permissions", path: "/api/security/permissions/update", body: ["max_tier": tier], proposed: NativeSecurityWire.json(["max_tier": tier]), scope: "Change the live sandbox permission ceiling. Raising it permits higher-tier operations subject to other checks. This endpoint does not persist this ceiling for restart and does not change autonomy or OS permissions.")
    }
    func policyReview(_ text: String) throws -> NativeSecurityReview {
        guard let bytes = text.data(using: .utf8), let object = try JSONSerialization.jsonObject(with: bytes) as? [String: Any], !object.isEmpty else { throw SecurityFeatureFailure(message: "Enter a nonempty JSON policy object. Nothing has been sent.") }
        let old = (data["policy"]?["execution"] as? [String: Any])?["full_authority"]
        let next = (object["execution"] as? [String: Any])?["full_authority"]
        if NativeSecurityWire.bool(next) == true && NativeSecurityWire.bool(old) != true { throw SecurityFeatureFailure(message: "Full authority cannot be enabled through this interface.") }
        return try review(section: "policy", path: "/api/policy/update", body: object, proposed: NativeSecurityWire.json(object), scope: "Replace the complete sandbox policy with the reviewed JSON, after backend validation and persistence. Removed fields will be removed. Inspect all path, command and execution boundaries before confirming. This does not alter folder grants or OS permissions.")
    }
    func costReview(key: String, amount: String) throws -> NativeSecurityReview {
        guard costSites.contains(key) || ["global_per_hour_usd", "global_per_day_usd"].contains(key) else { throw SecurityFeatureFailure(message: "Unsupported cost cap.") }
        let cap = try NativeSecurityWire.cap(amount)
        var value: Any = cap
        var preparatory: [[String: Any]] = []
        if costSites.contains(key) {
            let cost = data["cost"]?["cost"] as? [String: Any] ?? [:]
            var existing = cost[key] as? [String: Any] ?? (cost["per_call_site_caps"] as? [String: Any])?[key] as? [String: Any] ?? [:]
            existing["per_hour_usd"] = cap; value = existing
            var legacy = cost["per_call_site_caps"] as? [String: Any] ?? [:]
            if cap is NSNull, var oldSite = legacy[key] as? [String: Any], let oldHour = oldSite["per_hour_usd"], !(oldHour is NSNull) {
                oldSite["per_hour_usd"] = NSNull(); legacy[key] = oldSite
                preparatory = [["section": "cost", "key": "per_call_site_caps", "value": legacy]]
            }
        }
        let proposed: [String: Any] = preparatory.isEmpty ? [key: value] : [key: value, "per_call_site_caps": preparatory[0]["value"]!]
        return try review(section: "cost", path: "/api/config/update", body: ["section": "cost", "key": key, "value": value], preparatoryBodies: preparatory, proposed: NativeSecurityWire.json(proposed), scope: "Change cost.\(key). \(cap is NSNull ? "Remove this limit; it becomes unlimited." : "Save this USD spending cap.")\(preparatory.isEmpty ? "" : " The legacy hourly cap for this site must also be cleared in a separate config write, before the flat override. These writes are not atomic; a failure may leave a partial change, which must be refreshed.") Zero remains a zero-dollar cap. Other cost keys and per-site sibling fields are preserved. Reported configuration is not a provider billing guarantee.")
    }
    func commit(_ review: NativeSecurityReview, confirmed: Bool) async {
        guard confirmed, !busy, review.generation == generation else { return }
        guard let issued = issuedReviews.removeValue(forKey: review.id) else { return }
        let review = issued
        let current = generation; busy = true; receipt = nil; errors.removeValue(forKey: review.section)
        var wrote = false
        defer { if current == generation { busy = false } }
        do {
            let fresh = try validated(await request(Self.paths[review.section]!), section: review.section)
            guard snapshot(review.section, fresh) == review.previous else { data[review.section] = fresh; throw SecurityFeatureFailure(message: "The configuration changed after review. Review the refreshed state before trying again.") }
            for body in review.preparatoryBodies {
                let response = try await request("/api/config/update", method: "POST", body: body)
                guard NativeSecurityWire.bool(response["ok"]) == true else { throw SecurityFeatureFailure(message: "The agent did not confirm the legacy cap change.") }
                wrote = true
            }
            let result = try await request(review.path, method: review.method, query: review.query, body: review.body)
            guard NativeSecurityWire.bool(result["ok"]) == true else { throw SecurityFeatureFailure(message: "The agent did not confirm this action. Refresh before retrying.") }
            wrote = true
            let after = try validated(await request(Self.paths[review.section]!), section: review.section)
            data[review.section] = after
            if review.section == "permissions", after["max_tier"] as? String != review.body?["max_tier"] as? String { throw SecurityFeatureFailure(message: "The running permission tier differs from the request.") }
            if review.section == "policy", NativeSecurityWire.json(after) != review.proposed { throw SecurityFeatureFailure(message: "The running policy differs from the reviewed replacement.") }
            if review.section == "grants" {
                let rows = after["grants"] as? [[String: Any]] ?? []
                let path = result["path"] as? String ?? review.body?["path"] as? String ?? review.query.first?.value ?? ""
                let found = rows.first { $0["path"] as? String == path }
                if review.method == "DELETE" ? found != nil : found?["mode"] as? String != review.body?["mode"] as? String { throw SecurityFeatureFailure(message: "The refreshed folder grants do not confirm the reviewed change.") }
            }
            if review.section == "cost", let key = review.body?["key"] as? String {
                let cost = after["cost"] as? [String: Any] ?? [:]
                guard NativeSecurityWire.json(cost[key] ?? NSNull()) == NativeSecurityWire.json(review.body?["value"] ?? NSNull()) else { throw SecurityFeatureFailure(message: "The refreshed cost cap differs from your request.") }
                for body in review.preparatoryBodies {
                    guard let key = body["key"] as? String, NativeSecurityWire.json(cost[key] ?? NSNull()) == NativeSecurityWire.json(body["value"] ?? NSNull()) else { throw SecurityFeatureFailure(message: "The refreshed legacy cap differs from your request.") }
                }
            }
            receipt = "The agent acknowledged the action and returned refreshed \(review.section) state.\(review.section == "permissions" ? " The permission ceiling was changed live; restart persistence is not provided by this endpoint." : "")"
        } catch {
            guard current == generation else { return }
            let message = error.localizedDescription
            if wrote, let fresh = try? await request(Self.paths[review.section]!), let valid = try? validated(fresh, section: review.section) { data[review.section] = valid }
            if current == generation { errors[review.section] = message + (wrote ? " An acknowledged write may have applied; inspect the refreshed state before retrying." : "") }
        }
    }
    var policyText: String { data["policy"].map { NativeSecurityWire.json($0) } ?? "" }
}

struct NativeSecurityFeatureView: View {
    let baseURL: URL?
    @StateObject private var model: NativeSecurityModel
    @State private var tab = "grants"
    @State private var proposed: NativeSecurityReview?
    @State private var localError: String?
    @State private var folder = ""
    @State private var folderMode = "read"
    @State private var policyDraft = ""
    @State private var costKey = "global_per_hour_usd"
    @State private var amount = ""
    init(baseURL: URL?) { self.baseURL = baseURL; _model = StateObject(wrappedValue: NativeSecurityModel(baseURL: baseURL)) }
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack { Text("Security and cost").font(.title2.bold()); Spacer(); if model.busy { ProgressView().controlSize(.small) }; Button("Refresh") { Task { await model.refresh() } }.disabled(model.busy || baseURL == nil) }
            Picker("Section", selection: $tab) { Text("Folders").tag("grants"); Text("Permissions").tag("permissions"); Text("Policy").tag("policy"); Text("Audit").tag("audit"); Text("Cost caps").tag("cost") }.pickerStyle(.segmented)
            if let error = localError { Text(error).foregroundStyle(.orange) }
            if let error = model.errors[tab] { Text(error + " Displayed previous state may be outdated.").foregroundStyle(.orange) }
            if let receipt = model.receipt { Text(receipt).font(.caption).foregroundStyle(.secondary) }
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    if tab == "grants" { grants }
                    else if tab == "permissions" { permissions }
                    else if tab == "policy" { policy }
                    else if tab == "audit" { audit }
                    else { costs }
                }.frame(maxWidth: .infinity, alignment: .leading)
            }
        }.padding(20)
        .task(id: baseURL) { proposed = nil; localError = nil; folder = ""; policyDraft = ""; amount = ""; model.configure(baseURL); await model.refresh(); policyDraft = model.policyText }
        .sheet(item: $proposed) { review in
            VStack(alignment: .leading, spacing: 14) {
                Text("Review \(review.section) change").font(.title2.bold())
                Text(review.scope).fixedSize(horizontal: false, vertical: true)
                ScrollView { VStack(alignment: .leading, spacing: 12) { Text("Before").font(.headline); Text(review.previous).font(.system(.caption, design: .monospaced)).textSelection(.enabled); Text("Proposed").font(.headline); Text(review.proposed).font(.system(.caption, design: .monospaced)).textSelection(.enabled) } }
                HStack { Spacer(); Button("Cancel") { proposed = nil }; Button("Confirm reviewed change") { proposed = nil; Task { await model.commit(review, confirmed: true) } }.keyboardShortcut(.defaultAction) }
            }.padding(24).frame(width: 640, height: 560)
        }
    }
    private func propose(_ make: () throws -> NativeSecurityReview) { do { proposed = try make(); localError = nil } catch { localError = error.localizedDescription } }
    private var grants: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Explicit workspace grants").font(.headline)
            if let rows = model.data["grants"]?["grants"] as? [[String: Any]] {
                if rows.isEmpty { Text("No explicit folder grants returned. Other policy access may still exist.").foregroundStyle(.secondary) }
                ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                    HStack { VStack(alignment: .leading) { Text(row["path"] as? String ?? "Unknown folder").textSelection(.enabled); Text(row["mode"] as? String ?? "Unknown mode").font(.caption).foregroundStyle(.secondary) }; Spacer(); Button("Review revoke") { propose { try model.revokeReview(path: row["path"] as? String ?? "") } }.disabled(model.busy || model.errors["grants"] != nil) }
                }
            }
            Divider()
            Button("Choose folder…") { let panel = NSOpenPanel(); panel.canChooseFiles = false; panel.canChooseDirectories = true; panel.allowsMultipleSelection = false; if panel.runModal() == .OK { folder = panel.url?.path ?? "" } }.disabled(model.busy)
            Text(folder.isEmpty ? "No folder selected" : folder).textSelection(.enabled)
            Picker("Access", selection: $folderMode) { Text("Read only").tag("read"); Text("Read and write").tag("readwrite") }.pickerStyle(.segmented)
            Button("Review folder grant") { propose { try model.grantReview(path: folder, mode: folderMode) } }.disabled(folder.isEmpty || model.busy || model.data["grants"] == nil || model.errors["grants"] != nil)
        }
    }
    private var permissions: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Live maximum tool tier: " + (model.data["permissions"]?["max_tier"] as? String ?? "Unavailable")).font(.headline)
            Text("This is a tool ceiling, separate from autonomy, filesystem policy, device permissions and OS consent. Changes here are live-only.").font(.caption).foregroundStyle(.secondary)
            ForEach(NativeSecurityModel.tiers, id: \.self) { tier in
                HStack { VStack(alignment: .leading) { Text(tier.capitalized); Text((model.data["permissions"]?["tier_descriptions"] as? [String: String])?[tier] ?? "Description unavailable").font(.caption).foregroundStyle(.secondary) }; Spacer(); Button("Review tier") { propose { try model.tierReview(tier) } }.disabled(model.busy || model.data["permissions"] == nil || model.errors["permissions"] != nil || model.data["permissions"]?["max_tier"] as? String == tier) }
            }
        }
    }
    private var policy: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Sandbox policy document").font(.headline)
            Text("The API replaces the complete policy. Preserve fields you intend to keep. Full authority cannot be enabled here.").font(.caption).foregroundStyle(.secondary)
            Button("Load current policy into editor") { policyDraft = model.policyText }.disabled(model.data["policy"] == nil || model.errors["policy"] != nil)
            NativePolicyTextEditor(text: $policyDraft).frame(height: 320).border(Color.secondary.opacity(0.25))
            Button("Review complete replacement") { propose { try model.policyReview(policyDraft) } }.disabled(model.busy || model.data["policy"] == nil || model.errors["policy"] != nil)
        }
    }
    private var audit: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Recent security audit entries").font(.headline)
            Text("The endpoint returns up to 100 entries. A failed read is not an empty audit log.").font(.caption).foregroundStyle(.secondary)
            if let rows = model.data["audit"]?["entries"] as? [[String: Any]] {
                if rows.isEmpty { Text("No entries returned.").foregroundStyle(.secondary) }
                ForEach(Array(rows.reversed().enumerated()), id: \.offset) { _, row in
                    VStack(alignment: .leading, spacing: 4) { Text((row["action"] as? String ?? "Event") + " · " + (row["key"] as? String ?? "Unknown key")); Text("Actor: " + (row["actor"] as? String ?? "Unavailable")).font(.caption); if let seconds = row["ts"] as? Double { Text(Date(timeIntervalSince1970: seconds).formatted(date: .abbreviated, time: .standard)).font(.caption).foregroundStyle(.secondary) } }.padding(10).background(Color.secondary.opacity(0.06))
                }
            }
        }
    }
    private var costs: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Configured spending caps").font(.headline)
            Text("Current spend and reset times are unavailable here; the backend has no cost snapshot endpoint. These are configuration values, not measured spend or billing guarantees.").font(.caption).foregroundStyle(.secondary)
            if let cost = model.data["cost"]?["cost"] { Text(NativeSecurityWire.json(cost)).font(.system(.caption, design: .monospaced)).textSelection(.enabled) }
            Picker("Cap", selection: $costKey) { Text("Global / hour").tag("global_per_hour_usd"); Text("Global / day").tag("global_per_day_usd"); ForEach(model.costSites, id: \.self) { Text($0 + " / hour").tag($0) } }
            TextField("USD amount; empty removes limit", text: $amount).textFieldStyle(.roundedBorder)
            Button("Review cap change") { propose { try model.costReview(key: costKey, amount: amount) } }.disabled(model.busy || model.data["cost"] == nil || model.errors["cost"] != nil)
        }
    }
}

// AppKit's plain text view keeps large policy documents editable without
// SwiftUI TextEditor's nested scroll/accessibility failure on macOS.
private struct NativePolicyTextEditor: NSViewRepresentable {
    @Binding var text: String
    func makeCoordinator() -> Coordinator { Coordinator(text: $text) }
    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.borderType = .noBorder
        let editor = NSTextView()
        editor.isRichText = false
        editor.isEditable = true
        editor.isSelectable = true
        editor.font = .monospacedSystemFont(ofSize: 13, weight: .regular)
        editor.textContainerInset = NSSize(width: 8, height: 8)
        editor.isVerticallyResizable = true
        editor.isHorizontallyResizable = false
        editor.autoresizingMask = [.width]
        editor.textContainer?.widthTracksTextView = true
        editor.textContainer?.containerSize = NSSize(width: 0, height: CGFloat.greatestFiniteMagnitude)
        editor.setAccessibilityLabel("Sandbox policy JSON")
        editor.delegate = context.coordinator
        editor.string = text
        scroll.documentView = editor
        return scroll
    }
    func updateNSView(_ scroll: NSScrollView, context: Context) {
        context.coordinator.text = $text
        if let editor = scroll.documentView as? NSTextView, editor.string != text { editor.string = text }
    }
    final class Coordinator: NSObject, NSTextViewDelegate {
        var text: Binding<String>
        init(text: Binding<String>) { self.text = text }
        func textDidChange(_ notification: Notification) {
            if let editor = notification.object as? NSTextView { text.wrappedValue = editor.string }
        }
    }
}
