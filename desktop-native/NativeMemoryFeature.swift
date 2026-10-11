import SwiftUI
import UniformTypeIdentifiers

// Standalone native Memory-page port. Does not migrate or rewrite existing records.
// Parent supplies its owned backend URL; nil deliberately performs no requests.
enum NativeMemorySection: String, CaseIterable, Identifiable {
    case recent = "Recent", search = "Search", episodes = "Episodes", log = "Execution history", knowledge = "Knowledge"
    var id: String { rawValue }
    var endpoint: String {
        switch self {
        case .recent: return "/internal/memory/recent"
        case .search: return "/api/memory/search"
        case .episodes: return "/internal/episodes/recent"
        case .log: return "/internal/execution-log"
        case .knowledge: return "/api/knowledge/entities"
        }
    }
    var keys: [String] {
        switch self {
        case .recent: return ["memories", "notes"]
        case .search: return ["results"]
        case .episodes: return ["episodes"]
        case .log: return ["entries", "log"]
        case .knowledge: return ["entities"]
        }
    }
    var limit: Int {
        switch self {
        case .recent: return 30
        case .log: return 60
        default: return 50
        }
    }
}

struct NativeMemoryRecord: Identifiable {
    let id: String
    let raw: [String: Any]
    var backendID: String? { raw["id"] as? String }
    var text: String {
        for key in ["content", "summary", "text", "name", "entity", "tool", "skill_id"] {
            if let value = raw[key] as? String, !value.isEmpty { return value }
        }
        if let subject = raw["subject"] as? String {
            return [subject, raw["predicate"] as? String ?? "", raw["object"] as? String ?? ""].joined(separator: " ")
        }
        return NativeMemoryWire.json(raw)
    }
    var tier: String { raw["tier"] as? String ?? "unknown" }
    var score: Double? { (raw["score"] as? NSNumber)?.doubleValue ?? (raw["relevance_score"] as? NSNumber)?.doubleValue }
    var date: Date? {
        for key in ["created_at", "start_time", "timestamp"] {
            if let seconds = raw[key] as? NSNumber { return Date(timeIntervalSince1970: seconds.doubleValue) }
        }
        return nil
    }
}

enum NativeMemoryWire {
    static func json(_ value: Any) -> String {
        guard JSONSerialization.isValidJSONObject(value),
              let data = try? JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys]),
              let text = String(data: data, encoding: .utf8) else { return String(describing: value) }
        return text
    }
    static func records(_ value: Any, keys: [String]) throws -> [NativeMemoryRecord] {
        let rows: [Any]
        if let list = value as? [Any] { rows = list }
        else if let object = value as? [String: Any], let key = keys.first(where: { object[$0] is [Any] }), let list = object[key] as? [Any] { rows = list }
        else { throw NativeMemoryFailure("The memory service returned an unreadable list.") }
        return try rows.enumerated().map { index, value in
            let row: [String: Any]
            if let dict = value as? [String: Any] { row = dict }
            else if let text = value as? String { row = ["name": text] }
            else { throw NativeMemoryFailure("The memory service returned an unreadable record.") }
            // UI identity also includes index: search may return the same ID in multiple tiers.
            return NativeMemoryRecord(id: "\(row["tier"] ?? "row"):\(row["id"] ?? index):\(index)", raw: row)
        }
    }
    static func saveBody(content: String, tags: String, scope: String) -> [String: Any] {
        var body: [String: Any] = ["content": content.trimmingCharacters(in: .whitespacesAndNewlines),
                                 "tags": tags.split(separator: ",").map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }]
        if !scope.isEmpty { body["scope"] = scope }
        return body
    }
    static func segment(_ value: String) -> String {
        value.addingPercentEncoding(withAllowedCharacters: CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")) ?? ""
    }
}

struct NativeMemoryFailure: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}

struct NativeMemoryClient {
    let baseURL: URL
    var session: URLSession = .shared
    func request(_ path: String, query: [URLQueryItem] = [], method: String = "GET", body: [String: Any]? = nil) async throws -> Any {
        guard baseURL.scheme == "http", let host = baseURL.host, ["127.0.0.1", "localhost", "::1", "[::1]"].contains(host), baseURL.user == nil, baseURL.password == nil else { throw NativeMemoryFailure("Memory is available only through the app’s local service.") }
        guard var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else { throw NativeMemoryFailure("Invalid memory-service address.") }
        components.percentEncodedPath = path
        components.queryItems = query.isEmpty ? nil : query
        components.fragment = nil
        guard let url = components.url else { throw NativeMemoryFailure("Invalid memory request.") }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = 30
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let body = body {
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let (data, response) = try await session.feralLocalData(for: request)
        let object = try? JSONSerialization.jsonObject(with: data)
        guard let http = response as? HTTPURLResponse else { throw NativeMemoryFailure("The memory service did not return an HTTP response.") }
        let dict = object as? [String: Any]
        if !(200..<300).contains(http.statusCode) {
            let reason = dict?["detail"] ?? dict?["error"] ?? String(data: data, encoding: .utf8) ?? "Request failed"
            throw NativeMemoryFailure("Memory service (\(http.statusCode)): \(reason)")
        }
        guard let object = object else { throw NativeMemoryFailure("The memory service returned invalid JSON.") }
        // Some existing internal routes report errors with HTTP 200.
        if let error = dict?["error"] { throw NativeMemoryFailure(String(describing: error)) }
        return object
    }
}

@MainActor
final class NativeMemoryFeatureStore: ObservableObject {
    @Published var section: NativeMemorySection = .recent {
        didSet {
            guard section != oldValue else { return }
            revision += 1; detailRevision += 1
            records = nil; selection = nil; detail = nil; detailError = nil
            loading = false; detailLoading = false; error = nil; notice = nil; degradations = []; loadedQuery = ""
        }
    }
    @Published var query = ""
    @Published var tier = "all"
    @Published private(set) var records: [NativeMemoryRecord]?
    @Published var selection: String?
    @Published private(set) var detail: Any?
    @Published private(set) var detailError: String?
    @Published private(set) var loading = false
    @Published private(set) var detailLoading = false
    @Published private(set) var busy = false
    @Published private(set) var error: String?
    @Published private(set) var stats: [String: Any]?
    @Published private(set) var statsError: String?
    @Published private(set) var observability: [String: Any]?
    @Published private(set) var scopes: [String] = []
    @Published private(set) var scopeError: String?
    @Published private(set) var degradations: [Any] = []
    @Published private(set) var loadedQuery = ""
    @Published private(set) var notice: String?
    private var client: NativeMemoryClient?
    private var revision = 0
    private var detailRevision = 0
    private var connectionRevision = 0
    private var actionRevision = 0
    init(session: URLSession = .shared) { self.session = session }
    private let session: URLSession
    var available: Bool { client != nil }
    var selected: NativeMemoryRecord? { records?.first { $0.id == selection } }
    var filtered: [NativeMemoryRecord] { (records ?? []).filter { section != .search || tier == "all" || $0.tier == tier } }
    var tiers: [String] { Array(Set((records ?? []).map(\.tier))).sorted() }

    func connect(_ baseURL: URL?) async {
        revision += 1; detailRevision += 1; connectionRevision += 1; actionRevision += 1
        client = baseURL.map { NativeMemoryClient(baseURL: $0, session: session) }
        records = nil; selection = nil; detail = nil; detailError = nil; stats = nil; observability = nil
        scopes = []; scopeError = nil; statsError = nil; error = nil; notice = nil; degradations = []; loadedQuery = ""
        loading = false; detailLoading = false; busy = false
        guard available else { return }
        await reload()
    }
    func reload() async {
        revision += 1; detailRevision += 1
        let token = revision, current = section
        let searchedText = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let client = client else { return }
        selection = nil; detail = nil; detailError = nil; detailLoading = false; error = nil; notice = nil; tier = "all"
        if current == .search && searchedText.isEmpty {
            records = nil; degradations = []; loading = false
            await loadSupporting(client, token: token); return
        }
        loading = true; records = nil; degradations = []
        do {
            var parameters = [URLQueryItem(name: "limit", value: String(current.limit))]
            if current == .search { parameters.append(URLQueryItem(name: "q", value: searchedText)) }
            let response = try await client.request(current.endpoint, query: parameters)
            let decoded = try NativeMemoryWire.records(response, keys: current.keys)
            guard token == revision else { return }
            records = decoded
            loadedQuery = current == .search ? searchedText : ""
            degradations = (response as? [String: Any])?["degradations"] as? [Any] ?? []
        } catch {
            guard token == revision else { return }
            self.error = error.localizedDescription
        }
        guard token == revision else { return }
        loading = false
        await loadSupporting(client, token: token)
    }
    private func loadSupporting(_ client: NativeMemoryClient, token: Int) async {
        do {
            let response = try await client.request("/api/memory/stats")
            guard token == revision else { return }
            guard let value = response as? [String: Any], value["ok"] as? Bool == false || value["totals"] is [String: Any] else { throw NativeMemoryFailure("Memory counts returned an unreadable response.") }
            stats = value
            statsError = value["ok"] as? Bool == false ? "Memory counts unavailable: \(value["reason"] ?? "unknown reason")" : nil
        } catch { if token == revision { statsError = error.localizedDescription } }
        do {
            let value = try await client.request("/internal/memory/stats") as? [String: Any]
            guard token == revision else { return }
            guard let details = value?["observability"] as? [String: Any] else { throw NativeMemoryFailure("Search engine details unavailable.") }
            observability = details
        } catch { if token == revision { observability = ["unavailable": error.localizedDescription] } }
        do {
            let value = try await client.request("/api/sync/scopes") as? [String: Any]
            guard token == revision else { return }
            guard value?["ok"] as? Bool != false, let grants = value?["grants"] as? [[String: Any]] else { throw NativeMemoryFailure("Sharing grants unavailable. New notes can still remain private.") }
            scopes = Array(Set(grants.compactMap { $0["scope"] as? String }.filter { !$0.isEmpty })).sorted()
            scopeError = nil
        } catch { if token == revision { scopes = []; scopeError = error.localizedDescription } }
    }
    func inspect() async {
        detailRevision += 1
        let token = detailRevision
        detail = nil; detailError = nil; detailLoading = false
        guard section == .knowledge, let record = selected, let client = client else { return }
        detailLoading = true
        do {
            let value = try await client.request("/internal/knowledge/about/" + NativeMemoryWire.segment(record.text))
            guard token == detailRevision else { return }
            detail = value
        } catch { if token == detailRevision { detailError = error.localizedDescription } }
        if token == detailRevision { detailLoading = false }
    }
    func save(content: String, tags: String, scope: String) async -> Bool {
        guard !busy, let client = client else { return false }
        guard !content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { error = "Enter a memory before saving."; return false }
        guard scope.isEmpty || scopes.contains(scope) else { error = "This sharing scope is no longer available. Choose Private or refresh grants."; return false }
        let connection = connectionRevision
        actionRevision += 1
        let operation = actionRevision
        busy = true; error = nil
        defer { if operation == actionRevision && connection == connectionRevision { busy = false } }
        do {
            let value = try await client.request("/internal/memory/save", method: "POST", body: NativeMemoryWire.saveBody(content: content, tags: tags, scope: scope))
            guard operation == actionRevision && connection == connectionRevision else { return false }
            guard let result = value as? [String: Any], let savedID = result["id"] as? String, !savedID.isEmpty, result["ok"] as? Bool != false else { throw NativeMemoryFailure("The memory service did not confirm the saved note.") }
            await reload()
            guard operation == actionRevision && connection == connectionRevision else { return false }
            notice = "Memory saved."
            return true
        } catch { if operation == actionRevision && connection == connectionRevision { self.error = error.localizedDescription }; return false }
    }
    // Deliberately only recent-note IDs, never a guessed search result or entity ID.
    func deleteConfirmed(_ record: NativeMemoryRecord) async {
        guard !busy, section == .recent, let id = record.backendID, let client = client,
              records?.contains(where: { $0.id == record.id }) == true else { return }
        let connection = connectionRevision
        actionRevision += 1
        let operation = actionRevision
        busy = true; error = nil
        defer { if operation == actionRevision && connection == connectionRevision { busy = false } }
        do {
            let value = try await client.request("/internal/memory/" + NativeMemoryWire.segment(id), method: "DELETE") as? [String: Any]
            guard operation == actionRevision && connection == connectionRevision else { return }
            guard value?["deleted"] as? Bool == true else { throw NativeMemoryFailure("The memory service did not confirm deletion. The note may have already been removed.") }
            await reload()
            guard operation == actionRevision && connection == connectionRevision else { return }
            notice = "Note deleted."
        } catch { if operation == actionRevision && connection == connectionRevision { self.error = error.localizedDescription } }
    }
    func snapshot() throws -> Data {
        guard let records = records, error == nil else { throw NativeMemoryFailure("Load a successful list before exporting.") }
        // Snapshot of loaded rows only. No claim of full-store backup; all fields retained.
        return try JSONSerialization.data(withJSONObject: ["format": "feral-memory-loaded-snapshot-v1", "section": section.rawValue,
            "query": loadedQuery, "exported_at": ISO8601DateFormatter().string(from: Date()),
            "complete_store_backup": false, "degradations": degradations, "records": records.map(\.raw)], options: [.prettyPrinted, .sortedKeys])
    }
}

struct NativeMemorySnapshot: FileDocument {
    static var readableContentTypes: [UTType] { [.json] }
    var data: Data
    init(data: Data) { self.data = data }
    init(configuration: ReadConfiguration) throws { data = configuration.file.regularFileContents ?? Data() }
    func fileWrapper(configuration: WriteConfiguration) throws -> FileWrapper { FileWrapper(regularFileWithContents: data) }
}

struct NativeMemoryFeatureView: View {
    let baseURL: URL?
    @StateObject private var store = NativeMemoryFeatureStore()
    @State private var showSave = false
    @State private var pendingDelete: NativeMemoryRecord?
    @State private var exportDocument: NativeMemorySnapshot?
    @State private var exporting = false
    @State private var exportError: String?
    init(baseURL: URL?) { self.baseURL = baseURL }
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Memory").font(.largeTitle.bold())
                    Text("Notes, experiences, execution history and what FERAL has learned.").foregroundStyle(.secondary)
                }
                Spacer()
                Button { showSave = true } label: { Label("Save memory", systemImage: "plus") }.disabled(!store.available || store.busy)
                Button { Task { await store.reload() } } label: { Label("Refresh", systemImage: "arrow.clockwise") }.disabled(!store.available || store.loading || store.busy)
            }
            Picker("Memory section", selection: $store.section) {
                ForEach(NativeMemorySection.allCases) { Text($0.rawValue).tag($0) }
            }.pickerStyle(.segmented).disabled(store.busy)
            if !store.available {
                Label("Memory will be available when the local service is ready.", systemImage: "clock").foregroundStyle(.secondary)
                Spacer()
            } else {
                if store.section == .search {
                    HStack {
                        TextField("Search across all memory tiers", text: $store.query).textFieldStyle(.roundedBorder)
                            .onSubmit { Task { await store.reload() } }.disabled(store.loading || store.busy)
                        Button("Search") { Task { await store.reload() } }.disabled(store.query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || store.loading || store.busy)
                        Picker("Result tier", selection: $store.tier) {
                            Text("All tiers").tag("all")
                            ForEach(store.tiers, id: \.self) { Text($0.capitalized).tag($0) }
                        }.frame(maxWidth: 170)
                    }
                }
                if store.section == .search, store.records != nil { NativeSelectableText("Results for: " + store.loadedQuery).font(.caption).foregroundStyle(.secondary) }
                counts
                if let error = store.error { errorBanner(error) }
                if let notice = store.notice { Text(notice).foregroundStyle(.secondary).accessibilityLabel(notice) }
                if !store.degradations.isEmpty {
                    NativeSelectableText("Partial search: some tiers could not answer. \(NativeMemoryWire.json(store.degradations))").font(.callout).foregroundStyle(.orange)
                }
                HSplitView {
                    VStack(alignment: .leading) {
                        if store.loading { ProgressView("Loading memory…") }
                        else if store.records == nil, store.error == nil { Text("Enter a query and choose Search.").foregroundStyle(.secondary).padding() }
                        else if store.records != nil, store.filtered.isEmpty, store.error == nil {
                            Text(store.degradations.isEmpty ? "No records in this view." : "No results from the tiers that answered.").foregroundStyle(.secondary).padding()
                        }
                        List(store.filtered, selection: $store.selection) { record in
                            VStack(alignment: .leading, spacing: 5) {
                                Text(record.text).lineLimit(3)
                                HStack {
                                    if store.section == .search {
                                        Text(record.tier.capitalized)
                                        if let score = record.score { Text(String(format: "Score %.3f%@", score, score < 0.5 ? " · weak proximity" : "")) }
                                    }
                                    if let date = record.date { Text(date, style: .date) }
                                    if let tags = record.raw["tags"] as? [String], !tags.isEmpty { Text(tags.joined(separator: ", ")) }
                                }.font(.caption).foregroundStyle(.secondary)
                            }.padding(.vertical, 5).tag(record.id)
                        }.listStyle(.inset)
                    }.frame(minWidth: 220, idealWidth: 330)
                    inspector.frame(minWidth: 260, maxWidth: .infinity, maxHeight: .infinity)
                }
                HStack {
                    Text("Showing at most \(store.section.limit) records; this is a bounded view.").font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    Button("Export loaded records…") {
                        do { exportDocument = NativeMemorySnapshot(data: try store.snapshot()); exporting = true }
                        catch { exportError = error.localizedDescription }
                    }.disabled(store.records == nil || store.error != nil || store.loading || store.busy)
                    .help("Export the currently loaded rows with their metadata. This is not a complete memory backup.")
                }
                if let error = exportError { errorBanner(error) }
            }
        }.padding(24)
        .task(id: baseURL) { await store.connect(baseURL) }
        .onChange(of: store.section) { _ in Task { await store.reload() } }
        .onChange(of: store.selection) { _ in Task { await store.inspect() } }
        .sheet(isPresented: $showSave) { NativeMemorySaveSheet(store: store) }
        .alert("Permanently delete this note?", isPresented: Binding(get: { pendingDelete != nil }, set: { if !$0 { pendingDelete = nil } })) {
            Button("Cancel", role: .cancel) { pendingDelete = nil }
            Button("Delete note", role: .destructive) {
                if let record = pendingDelete { Task { await store.deleteConfirmed(record) } }
                pendingDelete = nil
            }
        } message: {
            Text("\(pendingDelete?.text.prefix(180) ?? "")\n\nThis cannot be undone. Derived knowledge and historical episodes are not erased. If the note was shared, its deletion is queued for peers in its original sharing scope.")
        }
        .fileExporter(isPresented: $exporting, document: exportDocument, contentType: .json, defaultFilename: "FERAL-loaded-memory") { result in
            if case .failure(let error) = result { exportError = error.localizedDescription }
        }
    }
    @ViewBuilder private var counts: some View {
        if let error = store.statsError { Text(error).font(.caption).foregroundStyle(.orange) }
        else if let totals = store.stats?["totals"] as? [String: Any] {
            Text(String(describing: totals["notes"] ?? "?") + " notes · " + String(describing: totals["episodes"] ?? "?") + " episodes · " + String(describing: totals["knowledge_triples"] ?? totals["knowledge"] ?? "?") + " knowledge records").font(.caption).foregroundStyle(.secondary)
        }
    }
    private func errorBanner(_ text: String) -> some View {
        HStack(alignment: .top) { Image(systemName: "exclamationmark.triangle").accessibilityHidden(true); NativeSelectableText(text).foregroundStyle(.red) }.foregroundStyle(.red).accessibilityElement(children: .contain)
    }
    @ViewBuilder private var inspector: some View {
        if let record = store.selected {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    NativeSelectableText(record.text).font(.title3)
                    if store.section == .recent, record.backendID != nil {
                        Button("Delete note…", role: .destructive) { pendingDelete = record }.disabled(store.busy || store.loading)
                    }
                    if store.section == .knowledge {
                        Text("What FERAL knows").font(.headline)
                        if store.detailLoading { ProgressView("Loading entity…") }
                        if let error = store.detailError {
                            errorBanner(error)
                            Button("Retry entity lookup") { Task { await store.inspect() } }
                        }
                        if let detail = store.detail { NativeSelectableText(NativeMemoryWire.json(detail)).font(.system(.body, design: .monospaced)) }
                    }
                    DisclosureGroup("Record metadata") {
                        NativeSelectableText(NativeMemoryWire.json(record.raw)).font(.system(.caption, design: .monospaced)).frame(maxWidth: .infinity, alignment: .leading)
                    }
                    if let observability = store.observability {
                        DisclosureGroup("Search engine details") {
                            NativeSelectableText(NativeMemoryWire.json(observability)).font(.system(.caption, design: .monospaced))
                            Text("A fallback index is a configuration fact; it does not by itself mean poorer search results.").font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }.padding(16).frame(maxWidth: .infinity, alignment: .leading)
            }
        } else { Text("Select a record to inspect it.").foregroundStyle(.secondary).frame(maxWidth: .infinity, maxHeight: .infinity) }
    }
}

private struct NativeMemorySaveSheet: View {
    @ObservedObject var store: NativeMemoryFeatureStore
    @Environment(\.dismiss) private var dismiss
    @State private var content = ""
    @State private var tags = ""
    @State private var scope = ""
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Save a memory").font(.title2.bold())
            Text("Content").font(.headline)
            TextEditor(text: $content).frame(minHeight: 150).border(Color.secondary.opacity(0.3)).accessibilityLabel("Memory content")
            TextField("Tags, separated by commas", text: $tags).textFieldStyle(.roundedBorder)
            Picker("Sharing", selection: $scope) {
                Text("Private to this device").tag("")
                ForEach(store.scopes, id: \.self) { Text("Peers granted \($0)").tag($0) }
            }
            Text("Private is the default. A shared note is eligible for exchange only with peers granted that exact scope.").font(.caption).foregroundStyle(.secondary)
            if let error = store.scopeError { Text("Sharing unavailable: \(error)").font(.caption).foregroundStyle(.orange) }
            if let error = store.error { NativeSelectableText(error).foregroundStyle(.red) }
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction).disabled(store.busy)
                Button(store.busy ? "Saving…" : "Save") {
                    Task { if await store.save(content: content, tags: tags, scope: scope) { dismiss() } }
                }.keyboardShortcut(.defaultAction).disabled(store.busy || !store.available || content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }.padding(24).frame(width: 540).disabled(store.busy).interactiveDismissDisabled(store.busy)
    }
}
