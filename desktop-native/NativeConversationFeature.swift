import SwiftUI
import Foundation
import CoreFoundation

struct NativeConversationFailure: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}

struct NativeConversationSummary: Identifiable {
    let id: String
    let title: String
    let preview: String
    let messageCount: Int
    let pinned: Bool
    let updatedAt: Date?
    let raw: [String: Any]
}

struct NativeConversationDocument {
    let id: String
    let title: String
    // Keep every row and every field. Inspection must never flatten attachments/tool rows for save.
    let messages: [[String: Any]]
    let raw: [String: Any]
}

struct NativeConversationPage {
    let conversations: [NativeConversationSummary]
    let total: Int
    let nextOffset: Int
    let hasMore: Bool
}

enum NativeConversationWire {
    static func json(_ value: Any) -> String {
        guard JSONSerialization.isValidJSONObject(value),
              let data = try? JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys]),
              let string = String(data: data, encoding: .utf8) else { return String(describing: value) }
        return string
    }
    static func segment(_ value: String) -> String {
        value.addingPercentEncoding(withAllowedCharacters: CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")) ?? ""
    }
    static func bool(_ value: Any?) -> Bool? {
        guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else { return nil }
        return number.boolValue
    }
    static func integer(_ value: Any?) -> Int? {
        guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
              number.doubleValue.isFinite, number.doubleValue >= 0, number.doubleValue < Double(Int.max),
              number.doubleValue.rounded(.towardZero) == number.doubleValue else { return nil }
        return number.intValue
    }
    static func page(_ object: [String: Any], offset: Int, query: String) throws -> NativeConversationPage {
        guard let rows = object["conversations"] as? [[String: Any]],
              let total = integer(object["total"]), let returnedOffset = integer(object["offset"]), returnedOffset == offset,
              let limit = integer(object["limit"]), limit > 0, limit <= 200, rows.count <= limit,
              let more = bool(object["has_more"]), !more || !rows.isEmpty,
              total >= rows.count else { throw NativeConversationFailure("Conversation history returned an unreadable page. Refresh to try again.") }
        if let returnedQuery = object["query"] as? String, returnedQuery != query { throw NativeConversationFailure("Conversation search returned a different query. Search again.") }
        var seen = Set<String>()
        let summaries = try rows.map { row -> NativeConversationSummary in
            guard let id = row["id"] as? String, !id.isEmpty, seen.insert(id).inserted,
                  let title = row["title"] as? String,
                  let count = integer(row["message_count"]), let pinned = bool(row["pinned"]) else {
                throw NativeConversationFailure("Conversation history contains an unreadable thread. No changes were made.")
            }
            let date = (row["updated_at"] as? NSNumber).map { Date(timeIntervalSince1970: $0.doubleValue) }
            return NativeConversationSummary(id: id, title: title, preview: row["preview"] as? String ?? "", messageCount: count, pinned: pinned, updatedAt: date, raw: row)
        }
        // Advance by the actual server slice, not deduplicated UI count.
        return NativeConversationPage(conversations: summaries, total: total, nextOffset: offset + rows.count, hasMore: more)
    }
    static func document(_ object: [String: Any], requestedID: String) throws -> NativeConversationDocument {
        guard let id = object["id"] as? String, id == requestedID,
              let title = object["title"] as? String, let rows = object["messages"] as? [[String: Any]] else {
            throw NativeConversationFailure("This conversation returned unreadable messages or a different thread. It was not opened.")
        }
        return NativeConversationDocument(id: id, title: title, messages: rows, raw: object)
    }
    static func messageText(_ row: [String: Any]) -> String {
        if let text = row["content"] as? String, !text.isEmpty { return text }
        if let text = row["text"] as? String, !text.isEmpty { return text }
        if let blocks = row["content"] as? [[String: Any]] {
            let texts = blocks.compactMap { $0["text"] as? String }.filter { !$0.isEmpty }
            if !texts.isEmpty { return texts.joined(separator: "\n") }
        }
        return "No text body. Inspect the original message fields below."
    }
}

struct NativeConversationClient {
    let baseURL: URL
    var session: URLSession = .shared
    func request(_ path: String, query: [URLQueryItem] = [], method: String = "GET", body: [String: Any]? = nil) async throws -> [String: Any] {
        guard ["127.0.0.1", "localhost", "::1", "[::1]"].contains(baseURL.host ?? ""), baseURL.scheme == "http",
              baseURL.user == nil, baseURL.password == nil,
              var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else {
            throw NativeConversationFailure("Conversation history is available only through the app’s local service.")
        }
        components.percentEncodedPath = path; components.queryItems = query.isEmpty ? nil : query; components.fragment = nil
        guard let url = components.url else { throw NativeConversationFailure("Invalid conversation request.") }
        var request = URLRequest(url: url)
        request.httpMethod = method; request.timeoutInterval = 30
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let body = body {
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let (data, response) = try await session.data(for: request)
        let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        guard let http = response as? HTTPURLResponse else { throw NativeConversationFailure("Invalid conversation service response.") }
        guard (200..<300).contains(http.statusCode) else {
            let reason = object?["detail"] ?? object?["error"] ?? "Request failed"
            throw NativeConversationFailure("Conversation service (\(http.statusCode)): \(reason)")
        }
        guard let object = object else { throw NativeConversationFailure("Conversation service returned invalid JSON.") }
        if let error = object["error"] { throw NativeConversationFailure(String(describing: error)) }
        if NativeConversationWire.bool(object["ok"]) == false { throw NativeConversationFailure("The conversation service did not confirm this request.") }
        return object
    }
}

@MainActor
final class NativeConversationFeatureModel: ObservableObject {
    @Published var search = ""
    @Published var selection: String?
    @Published private(set) var threads: [NativeConversationSummary]?
    @Published private(set) var total: Int?
    @Published private(set) var hasMore = false
    @Published private(set) var loading = false
    @Published private(set) var loadingMore = false
    @Published private(set) var inspecting = false
    @Published private(set) var acting = false
    @Published private(set) var listError: String?
    @Published private(set) var detailError: String?
    @Published private(set) var actionError: String?
    @Published private(set) var receipt: String?
    @Published private(set) var loadedQuery = ""
    @Published private(set) var document: NativeConversationDocument?
    private var client: NativeConversationClient?
    private let session: URLSession
    private var connectionGeneration = UUID()
    private var listGeneration = UUID()
    private var detailGeneration = UUID()
    private var operationGeneration = UUID()
    private var nextOffset = 0
    init(session: URLSession = .shared) { self.session = session }
    var available: Bool { client != nil }
    var selected: NativeConversationSummary? { threads?.first { $0.id == selection } }
    var controlsBusy: Bool { acting || loading || loadingMore }
    func configure(_ baseURL: URL?) async {
        connectionGeneration = UUID(); listGeneration = UUID(); detailGeneration = UUID(); operationGeneration = UUID()
        client = baseURL.map { NativeConversationClient(baseURL: $0, session: session) }
        threads = nil; total = nil; hasMore = false; nextOffset = 0
        document = nil; selection = nil; listError = nil; detailError = nil; actionError = nil; receipt = nil; loadedQuery = ""
        loading = false; loadingMore = false; inspecting = false; acting = false
        if available { await refresh() }
    }
    func refresh() async {
        guard let client = client else { return }
        listGeneration = UUID()
        let generation = listGeneration, connection = connectionGeneration
        let query = search.trimmingCharacters(in: .whitespacesAndNewlines)
        loading = true; loadingMore = false; listError = nil; actionError = nil; receipt = nil; threads = nil; total = nil; hasMore = false; nextOffset = 0
        detailGeneration = UUID(); document = nil; selection = nil; detailError = nil; inspecting = false
        defer { if generation == listGeneration && connection == connectionGeneration { loading = false } }
        do {
            let response = try await client.request("/api/conversations", query: parameters(offset: 0, query: query))
            let page = try NativeConversationWire.page(response, offset: 0, query: query)
            guard generation == listGeneration && connection == connectionGeneration else { return }
            threads = page.conversations; total = page.total; hasMore = page.hasMore; nextOffset = page.nextOffset; loadedQuery = query
        } catch { if generation == listGeneration && connection == connectionGeneration { listError = error.localizedDescription } }
    }
    private func parameters(offset: Int, query: String) -> [URLQueryItem] {
        [URLQueryItem(name: "limit", value: "25"), URLQueryItem(name: "offset", value: String(offset)), URLQueryItem(name: "q", value: query)]
    }
    func more() async {
        guard !controlsBusy, hasMore, threads != nil, let client = client else { return }
        let generation = listGeneration, connection = connectionGeneration, offset = nextOffset, query = loadedQuery
        loadingMore = true; listError = nil
        defer { if generation == listGeneration && connection == connectionGeneration { loadingMore = false } }
        do {
            let response = try await client.request("/api/conversations", query: parameters(offset: offset, query: query))
            let page = try NativeConversationWire.page(response, offset: offset, query: query)
            guard generation == listGeneration && connection == connectionGeneration else { return }
            let seen = Set((threads ?? []).map(\.id))
            threads = (threads ?? []) + page.conversations.filter { !seen.contains($0.id) }
            total = page.total; hasMore = page.hasMore; nextOffset = page.nextOffset
        } catch { if generation == listGeneration && connection == connectionGeneration { listError = error.localizedDescription } }
    }
    func inspect() async {
        detailGeneration = UUID()
        let generation = detailGeneration, connection = connectionGeneration
        document = nil; detailError = nil; inspecting = false
        guard let id = selected?.id, let client = client else { return }
        inspecting = true
        defer { if generation == detailGeneration && connection == connectionGeneration { inspecting = false } }
        do {
            let response = try await client.request("/api/conversations/" + NativeConversationWire.segment(id))
            let parsed = try NativeConversationWire.document(response, requestedID: id)
            guard generation == detailGeneration && connection == connectionGeneration && selection == id else { return }
            document = parsed
        } catch { if generation == detailGeneration && connection == connectionGeneration && selection == id { detailError = error.localizedDescription } }
    }
    func rename(_ reviewed: NativeConversationSummary, title: String) async -> Bool {
        let trimmed = title.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { actionError = "Enter a conversation title."; return false }
        return await mutate(reviewed, suffix: "/rename", method: "POST", body: ["title": trimmed], receipt: "Conversation renamed.") { response in
            NativeConversationWire.bool(response["ok"]) == true && response["id"] as? String == reviewed.id && response["title"] as? String == trimmed && NativeConversationWire.bool(response["title_custom"]) == true
        }
    }
    func pin(_ reviewed: NativeConversationSummary) async -> Bool {
        let desired = !reviewed.pinned
        return await mutate(reviewed, suffix: "/pin", method: "POST", body: ["pinned": desired], receipt: desired ? "Conversation pinned." : "Conversation unpinned.") { response in
            NativeConversationWire.bool(response["ok"]) == true && response["id"] as? String == reviewed.id && NativeConversationWire.bool(response["pinned"]) == desired
        }
    }
    func deleteConfirmed(_ reviewed: NativeConversationSummary, canDelete: @escaping (String) -> Bool = { _ in true }, onDeleted: @escaping (String) -> Void = { _ in }) async -> Bool {
        await mutate(reviewed, suffix: "", method: "DELETE", body: nil, receipt: "Saved conversation deleted.", canDispatch: { canDelete(reviewed.id) }, onConfirmed: { onDeleted(reviewed.id) }) { NativeConversationWire.bool($0["ok"]) == true }
    }
    private func mutate(_ reviewed: NativeConversationSummary, suffix: String, method: String, body: [String: Any]?, receipt text: String, canDispatch: () -> Bool = { true }, onConfirmed: () -> Void = {}, verify: ([String: Any]) -> Bool) async -> Bool {
        guard !controlsBusy, let client = client,
              let current = threads?.first(where: { $0.id == reviewed.id }),
              current.title == reviewed.title, current.pinned == reviewed.pinned, current.updatedAt == reviewed.updatedAt else {
            actionError = "The conversation changed or is no longer available. Refresh and review again."; return false
        }
        // The parent supplies a live active-thread/switching guard, never a captured boolean.
        guard canDispatch() else { actionError = "Switch to another conversation before deleting this one."; return false }
        let connection = connectionGeneration
        operationGeneration = UUID(); let operation = operationGeneration
        acting = true; actionError = nil; receipt = nil
        defer { if operation == operationGeneration && connection == connectionGeneration { acting = false } }
        do {
            let response = try await client.request("/api/conversations/" + NativeConversationWire.segment(reviewed.id) + suffix, method: method, body: body)
            guard operation == operationGeneration && connection == connectionGeneration else { return false }
            guard verify(response) else { throw NativeConversationFailure("The response did not confirm this exact conversation change. Refresh before retrying.") }
            // Immediately notify the parent before refresh suspends: tombstone removed threads
            // before any later autosave, without claiming deletion on an unconfirmed response.
            onConfirmed()
            guard operation == operationGeneration && connection == connectionGeneration else { return false }
            await refresh()
            guard operation == operationGeneration && connection == connectionGeneration else { return false }
            receipt = text
            return true
        } catch { if operation == operationGeneration && connection == connectionGeneration { actionError = error.localizedDescription }; return false }
    }
}

struct NativeConversationFeatureView: View {
    let baseURL: URL?
    let onOpen: (String) -> Void
    let onNew: () -> Void
    let onDeleted: (String) -> Void
    let canDelete: (String) -> Bool
    @StateObject private var model = NativeConversationFeatureModel()
    @State private var renameTarget: NativeConversationSummary?
    @State private var renameTitle = ""
    @State private var deleteTarget: NativeConversationSummary?
    init(baseURL: URL?, onOpen: @escaping (String) -> Void, onNew: @escaping () -> Void, onDeleted: @escaping (String) -> Void = { _ in }, canDelete: @escaping (String) -> Bool = { _ in true }) {
        self.baseURL = baseURL; self.onOpen = onOpen; self.onNew = onNew; self.onDeleted = onDeleted; self.canDelete = canDelete
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Conversations").font(.largeTitle.bold())
                    Text("Find a conversation and continue where you left off.").foregroundStyle(.secondary)
                }
                Spacer()
                Button { onNew() } label: { Label("New conversation", systemImage: "square.and.pencil") }.disabled(!model.available || model.controlsBusy)
                Button { Task { await model.refresh() } } label: { Label("Refresh", systemImage: "arrow.clockwise") }.disabled(!model.available || model.controlsBusy)
            }
            if !model.available {
                Label("Conversation history will be available when the local service is ready.", systemImage: "clock").foregroundStyle(.secondary)
                Spacer()
            } else {
                HStack {
                    TextField("Search titles and message content", text: $model.search).textFieldStyle(.roundedBorder).onSubmit { Task { await model.refresh() } }.disabled(model.controlsBusy)
                    Button("Search") { Task { await model.refresh() } }.disabled(model.controlsBusy)
                }
                if let total = model.total { Text("\(total) conversations\(model.loadedQuery.isEmpty ? "" : " matching “\(model.loadedQuery)”")").font(.caption).foregroundStyle(.secondary) }
                if let error = model.listError { warning(error) }
                if let error = model.actionError { warning(error) }
                if let receipt = model.receipt { Text(receipt).foregroundStyle(.secondary).textSelection(.enabled) }
                HSplitView {
                    VStack(alignment: .leading) {
                        if model.loading { ProgressView("Loading conversations…") }
                        if model.threads?.isEmpty == true, model.listError == nil { Text(model.loadedQuery.isEmpty ? "No saved conversations." : "No conversations matched this search.").foregroundStyle(.secondary).padding() }
                        List(model.threads ?? [], selection: $model.selection) { thread in
                            VStack(alignment: .leading, spacing: 5) {
                                HStack {
                                    if thread.pinned { Image(systemName: "pin.fill").accessibilityLabel("Pinned") }
                                    Text(thread.title.isEmpty ? "Untitled conversation" : thread.title).font(.headline).lineLimit(2)
                                }
                                if !thread.preview.isEmpty { Text(thread.preview).lineLimit(2).foregroundStyle(.secondary) }
                                HStack {
                                    Text("\(thread.messageCount) messages")
                                    if let date = thread.updatedAt { Text(date, style: .date) }
                                }.font(.caption).foregroundStyle(.secondary)
                            }.padding(.vertical, 5).tag(thread.id)
                        }.listStyle(.inset).disabled(model.acting)
                        if model.hasMore { Button(model.loadingMore ? "Loading…" : "Load more conversations") { Task { await model.more() } }.disabled(model.controlsBusy) }
                    }.frame(minWidth: 220, idealWidth: 320)
                    inspector.frame(minWidth: 280, maxWidth: .infinity, maxHeight: .infinity)
                }
            }
        }.padding(24)
        .task(id: baseURL) { await model.configure(baseURL) }
        .onChange(of: model.selection) { _ in Task { await model.inspect() } }
        .sheet(item: $renameTarget) { thread in
            VStack(alignment: .leading, spacing: 16) {
                Text("Rename conversation").font(.title2.bold())
                TextField("Conversation title", text: $renameTitle).textFieldStyle(.roundedBorder)
                if let error = model.actionError { warning(error) }
                HStack {
                    Spacer()
                    Button("Cancel") { renameTarget = nil }.keyboardShortcut(.cancelAction).disabled(model.acting)
                    Button("Save title") { Task { if await model.rename(thread, title: renameTitle) { renameTarget = nil } } }.keyboardShortcut(.defaultAction).disabled(model.controlsBusy || renameTitle.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }.padding(24).frame(width: 460).interactiveDismissDisabled(model.acting)
        }
        .alert("Permanently delete this saved conversation?", isPresented: Binding(get: { deleteTarget != nil }, set: { if !$0 { deleteTarget = nil } })) {
            Button("Cancel", role: .cancel) { deleteTarget = nil }
            Button("Delete conversation", role: .destructive) {
                if let thread = deleteTarget { Task { _ = await model.deleteConfirmed(thread, canDelete: canDelete, onDeleted: onDeleted) } }
                deleteTarget = nil
            }
        } message: {
            Text("“\(deleteTarget?.title ?? "")” will be removed from saved history. This cannot be undone. Related memory, snapshots and any running session are not erased by this action.")
        }
    }
    private func warning(_ text: String) -> some View {
        Label(text, systemImage: "exclamationmark.triangle").foregroundStyle(.red).textSelection(.enabled)
    }
    @ViewBuilder private var inspector: some View {
        if let thread = model.selected {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    Text(thread.title).font(.title2.bold()).textSelection(.enabled)
                    HStack {
                        Button("Continue conversation") { onOpen(thread.id) }.disabled(model.document?.id != thread.id || model.inspecting || model.controlsBusy)
                        Menu("Manage") {
                            Button("Rename…") { renameTitle = thread.title; renameTarget = thread }
                            Button(thread.pinned ? "Unpin" : "Pin") { Task { _ = await model.pin(thread) } }
                            Divider()
                            Button("Delete…", role: .destructive) { deleteTarget = thread }.disabled(!canDelete(thread.id)).help("Switch to another conversation before deleting the active chat.")
                        }.disabled(model.controlsBusy)
                    }
                    if model.inspecting { ProgressView("Loading original messages…") }
                    if let error = model.detailError {
                        warning(error)
                        Button("Retry conversation lookup") { Task { await model.inspect() } }.disabled(model.inspecting || model.controlsBusy)
                    }
                    if let document = model.document {
                        Text("Read-only preview. Attachments and tool fields remain in the original metadata; opening this view does not save or rewrite the conversation.").font(.caption).foregroundStyle(.secondary)
                        if document.messages.isEmpty { Text("This saved conversation has no messages.").foregroundStyle(.secondary) }
                        ForEach(Array(document.messages.enumerated()), id: \.offset) { index, message in
                            VStack(alignment: .leading, spacing: 8) {
                                Text((message["role"] as? String ?? "Unknown role").capitalized).font(.caption.bold()).foregroundStyle(.secondary)
                                Text(NativeConversationWire.messageText(message)).textSelection(.enabled)
                                DisclosureGroup("Original message fields") { code(message) }
                            }.padding(12).frame(maxWidth: .infinity, alignment: .leading).background(Color.secondary.opacity(0.07), in: RoundedRectangle(cornerRadius: 10))
                        }
                        DisclosureGroup("Conversation metadata") { code(document.raw) }
                    }
                }.padding(16).frame(maxWidth: .infinity, alignment: .leading)
            }
        } else { Text("Select a conversation to inspect its original messages.").foregroundStyle(.secondary).frame(maxWidth: .infinity, maxHeight: .infinity) }
    }
    private func code(_ object: Any) -> some View {
        Text(NativeConversationWire.json(object)).font(.system(.caption, design: .monospaced)).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
    }
}
