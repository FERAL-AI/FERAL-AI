import SwiftUI
import Foundation

struct NativeGlobalApproval: Identifiable {
    let connectionID: UUID
    let id: String
    let sessionID: String
    let tool: String
    let safety: String
    let created: Date
    let arguments: String
    let policy: String
    let status: String
}

struct NativeOversightEvent: Identifiable {
    let id: String
    let date: Date
    let source: String
    let actor: String
    let decision: String
    let kind: String
    let sessionID: String
    let summary: String
    let detail: String
}

private struct OversightFailure: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

private func oversightJSON(_ value: Any) -> String {
    guard JSONSerialization.isValidJSONObject(value), let data = try? JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys]) else { return String(describing: value) }
    return String(decoding: data, as: UTF8.self)
}

private func oversightBool(_ value: Any?) -> Bool? {
    guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else { return nil }
    return number.boolValue
}

@MainActor final class NativeOversightModel: ObservableObject {
    @Published private(set) var approvals: [NativeGlobalApproval] = []
    @Published private(set) var events: [NativeOversightEvent] = []
    @Published private(set) var paused: Bool?
    @Published private(set) var loading = false
    @Published private(set) var acting = false
    @Published private(set) var queueFresh = false
    @Published private(set) var queueError: String?
    @Published private(set) var eventsError: String?
    @Published private(set) var controlError: String?
    @Published private(set) var decisionError: String?
    @Published private(set) var receipt: String?
    @Published var source = ""
    @Published var actor = ""
    @Published var decision = ""
    private var baseURL: URL?
    private var generation = UUID()
    private let session: URLSession

    init(baseURL: URL?, session: URLSession? = nil) {
        self.baseURL = baseURL
        self.session = session ?? URLSession(configuration: .ephemeral)
    }

    func configure(baseURL: URL?) {
        guard self.baseURL != baseURL else { return }
        self.baseURL = baseURL
        generation = UUID(); loading = false; acting = false
        approvals = []; events = []; paused = nil; queueFresh = false
        queueError = nil; eventsError = nil; controlError = nil; decisionError = nil; receipt = nil
    }

    private func request(_ path: String, body: [String: Any]? = nil, query: [URLQueryItem] = []) async throws -> [String: Any] {
        try Task.checkCancellation()
        let started = generation
        guard let baseURL, ["127.0.0.1", "localhost", "::1"].contains(baseURL.host ?? ""), ["http", "https"].contains(baseURL.scheme ?? "") else {
            throw OversightFailure(message: "Connect to the local agent to review oversight.")
        }
        var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)!
        components.path = path; components.queryItems = query.isEmpty ? nil : query
        guard let url = components.url else { throw OversightFailure(message: "Invalid local request.") }
        var request = URLRequest(url: url)
        request.timeoutInterval = body == nil ? 20 : 180
        if let body {
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, response) = try await session.feralLocalData(for: request)
        try Task.checkCancellation()
        guard generation == started else { throw OversightFailure(message: "The agent connection changed. Refresh and review again.") }
        guard let http = response as? HTTPURLResponse else { throw OversightFailure(message: "The agent returned an invalid HTTP response.") }
        let value = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        guard (200..<300).contains(http.statusCode) else {
            let detail = value?["detail"] as? String ?? (value?["detail"] as? [String: Any])?["message"] as? String
            throw OversightFailure(message: "HTTP \(http.statusCode): \(detail ?? "The local agent could not complete the request.")")
        }
        guard let value else { throw OversightFailure(message: "The agent returned malformed JSON. State is not confirmed.") }
        if let error = value["error"] as? String { throw OversightFailure(message: error) }
        return value
    }

    func refresh() async {
        guard !loading && !acting else { return }
        let started = generation
        loading = true
        defer { if generation == started { loading = false } }
        await loadQueue()
        guard generation == started, !Task.isCancelled else { return }
        await loadControl()
        guard generation == started, !Task.isCancelled else { return }
        await loadEvents()
    }

    private func loadQueue() async {
        let started = generation
        do {
            let value = try await request("/api/approvals", query: [URLQueryItem(name: "limit", value: "500")])
            guard let rows = value["approvals"] as? [[String: Any]] else { throw OversightFailure(message: "Approval queue response is incomplete.") }
            var seen = Set<String>()
            let parsed = try rows.map { row -> NativeGlobalApproval in
                guard let id = row["request_id"] as? String, !id.isEmpty, seen.insert(id).inserted,
                      let status = row["status"] as? String, status == "pending",
                      let tool = row["tool_name"] as? String, !tool.isEmpty,
                      let sessionID = row["session_id"] as? String,
                      let args = row["args"] as? [String: Any] else {
                    throw OversightFailure(message: "The approval queue contains an invalid request. Refresh before deciding.")
                }
                return NativeGlobalApproval(connectionID: started, id: id, sessionID: sessionID, tool: tool, safety: row["safety_level"] as? String ?? "Unspecified", created: Date(timeIntervalSince1970: (row["created_at"] as? NSNumber)?.doubleValue ?? 0), arguments: oversightJSON(args), policy: oversightJSON(row["policy_sources"] as? [String: Any] ?? [:]), status: status)
            }
            approvals = parsed; queueFresh = true; queueError = nil
        } catch { if generation == started { queueFresh = false; queueError = error.localizedDescription } }
    }

    private func loadControl() async {
        let started = generation
        do {
            let value = try await request("/api/supervisor/stats")
            guard let state = oversightBool(value["paused"]) else { throw OversightFailure(message: "Supervisor paused state is not confirmed.") }
            paused = state; controlError = nil
        } catch { if generation == started { paused = nil; controlError = error.localizedDescription } }
    }

    private func loadEvents() async {
        let started = generation
        do {
            var query = [URLQueryItem(name: "limit", value: "80")]
            for (key, value) in [("source", source), ("actor", actor), ("decision", decision)] where !value.isEmpty { query.append(URLQueryItem(name: key, value: value)) }
            let value = try await request("/api/supervisor/events", query: query)
            guard let rows = value["events"] as? [[String: Any]] else { throw OversightFailure(message: "Audit response is incomplete.") }
            var seen = Set<String>()
            events = try rows.map { row in
                guard let id = row["event_id"] as? String, !id.isEmpty, seen.insert(id).inserted else { throw OversightFailure(message: "Audit response contains an invalid event.") }
                return NativeOversightEvent(id: id, date: Date(timeIntervalSince1970: (row["ts"] as? NSNumber)?.doubleValue ?? 0), source: row["source"] as? String ?? "", actor: row["actor"] as? String ?? "", decision: row["decision"] as? String ?? "", kind: row["kind"] as? String ?? "", sessionID: row["session_id"] as? String ?? "", summary: row["payload_summary"] as? String ?? "", detail: oversightJSON(row["detail"] as? [String: Any] ?? [:]))
            }
            eventsError = nil
        } catch { if generation == started { eventsError = error.localizedDescription } }
    }

    func setPaused(_ desired: Bool) async {
        guard paused != nil, !acting && !loading else { return }
        let started = generation
        acting = true; controlError = nil
        defer { if generation == started { acting = false } }
        do {
            let value = try await request("/api/supervisor/pause", body: ["paused": desired])
            guard let actual = oversightBool(value["paused"]) else { throw OversightFailure(message: "Pause change was not confirmed. Refresh supervisor state.") }
            paused = actual
            receipt = actual ? "Supervisor reports paused. New supervised calls are blocked." : "Supervisor reports resumed."
        } catch { if generation == started { paused = nil; controlError = "Pause change is not confirmed. " + error.localizedDescription } }
        guard generation == started, !Task.isCancelled else { return }
        await loadEvents()
    }

    func decide(_ reviewed: NativeGlobalApproval, approve: Bool) async {
        guard !acting && !loading, queueFresh,
              reviewed.connectionID == generation,
              let current = approvals.first(where: { $0.id == reviewed.id }), current.status == "pending",
              current.sessionID == reviewed.sessionID, current.arguments == reviewed.arguments,
              current.tool == reviewed.tool, current.policy == reviewed.policy, current.safety == reviewed.safety,
              !approve || paused == false else {
            decisionError = "This request or supervisor state changed. Refresh and review it again."
            return
        }
        acting = true; decisionError = nil; receipt = nil
        let started = generation
        defer { if generation == started { acting = false } }
        do {
            // Real route IDs are opaque path components, never user paths.
            guard !current.id.contains("/"), !current.id.contains("?"), !current.id.contains("#") else { throw OversightFailure(message: "The approval request ID is invalid.") }
            let expected = approve ? "approved" : "rejected"
            let value = try await request("/api/approvals/\(current.id)/\(approve ? "approve" : "reject")", body: ["session_id": current.sessionID])
            guard value["success"] as? Bool == true, value["status"] as? String == expected,
                  value["request_id"] as? String == current.id, value["session_id"] as? String == current.sessionID else {
                throw OversightFailure(message: "The decision response does not confirm this request. Refresh before retrying.")
            }
            approvals.removeAll { $0.id == current.id }
            if approve {
                receipt = "Approved \(current.tool) for session \(current.sessionID)."
                if let result = value["result"] as? [String: Any] {
                    let rawError = result["error"]
                    let hasError = (rawError as? String).map { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty } ?? (rawError != nil && !(rawError is NSNull))
                    if result["success"] as? Bool == false || hasError || (result["status_code"] as? Int ?? 0) >= 400 {
                        receipt! += " The tool reported a failure; approval is not proof of execution success."
                    }
                    receipt! += "\n" + oversightJSON(result)
                }
            } else { receipt = "Denied request for \(current.tool)." }
        } catch {
            if generation == started {
                queueFresh = false
                decisionError = "Decision not confirmed. Refresh the queue before retrying. " + error.localizedDescription
            }
        }
        guard generation == started, !Task.isCancelled else { return }
        await loadQueue()
        guard generation == started, !Task.isCancelled else { return }
        await loadEvents()
    }
}

struct NativeOversightFeatureView: View {
    private let baseURL: URL?
    @StateObject private var model: NativeOversightModel
    @State private var confirmation: NativeGlobalApproval?
    @State private var approve = false
    @State private var pauseConfirmation = false

    init(baseURL: URL?) { self.baseURL = baseURL; _model = StateObject(wrappedValue: NativeOversightModel(baseURL: baseURL)) }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
                HStack {
                    VStack(alignment: .leading, spacing: 5) {
                        Text("Oversight").font(.largeTitle.weight(.semibold))
                        Text("Review pending tool requests and the supervisor audit.").foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Refresh") { Task { await model.refresh() } }.disabled(model.loading || model.acting)
                    if model.loading || model.acting { ProgressView().controlSize(.small) }
                }.padding(24)
            ScrollView(.vertical) {
              LazyVStack(alignment: .leading, spacing: 22) {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("Supervisor").font(.headline)
                        HStack {
                            Text(model.paused.map { $0 ? "Paused" : "Running" } ?? "State not confirmed")
                            Spacer()
                            Button(model.paused == true ? "Resume supervised calls…" : "Pause supervised calls…") { pauseConfirmation = true }
                                .disabled(model.paused == nil || model.loading || model.acting)
                        }
                        Text("Pause blocks new supervised calls. It does not cancel an action already running; stop coding tasks in Coding.").font(.callout).foregroundStyle(.secondary)
                        if let error = model.controlError { oversightNotice(error) }
                    }.frame(maxWidth: .infinity, alignment: .leading).padding(16)
                        .background(RoundedRectangle(cornerRadius: 12).fill(Color.secondary.opacity(0.06)))
                if let error = model.decisionError { oversightNotice(error) }
                if let receipt = model.receipt { NativeSelectableText(receipt).font(.callout) }
                Text("Pending approvals").font(.title2.weight(.semibold))
                Text("Up to 500 pending core tool approvals across sessions. Coding engine approvals remain in Coding. Approving a core tool grants it for that session and runs this request.").font(.callout).foregroundStyle(.secondary)
                if let error = model.queueError { oversightNotice("Queue could not be refreshed. " + error) }
                if model.approvals.isEmpty && model.queueFresh { Text("No core tool requests are waiting.").foregroundStyle(.secondary) }
                ForEach(model.approvals) { request in
                        VStack(alignment: .leading, spacing: 12) {
                            Text(request.tool).font(.headline)
                            NativeSelectableText("Session: \(request.sessionID) · Safety: \(request.safety)").font(.caption)
                            Text(request.created.formatted(date: .abbreviated, time: .standard)).font(.caption).foregroundStyle(.secondary)
                            oversightCode("Proposed arguments", text: request.arguments)
                            DisclosureGroup("Policy that held this request") { oversightCode("Policy sources", text: request.policy) }
                            HStack {
                                Button("Approve for this session…") { confirmation = request; approve = true }
                                    .disabled(!model.queueFresh || model.paused != false || model.loading || model.acting)
                                Button("Deny request…") { confirmation = request; approve = false }
                                    .disabled(!model.queueFresh || model.loading || model.acting)
                            }
                        }.frame(maxWidth: .infinity, alignment: .leading).padding(16)
                            .background(RoundedRectangle(cornerRadius: 12).fill(Color.secondary.opacity(0.06)))
                }
                HStack {
                    Text("Audit").font(.title2.weight(.semibold))
                    Spacer()
                    TextField("Source", text: $model.source).frame(width: 120)
                    TextField("Actor", text: $model.actor).frame(width: 120)
                    Picker("Decision", selection: $model.decision) {
                        Text("All").tag(""); Text("Allowed").tag("allowed"); Text("Denied").tag("denied"); Text("Queued").tag("queued"); Text("Error").tag("error")
                    }.frame(width: 150)
                    Button("Apply filters") { Task { await model.refresh() } }.disabled(model.loading || model.acting)
                }
                if let error = model.eventsError { oversightNotice("Audit could not be refreshed. Displayed rows are last known. " + error) }
                if model.events.isEmpty && model.eventsError == nil && !model.loading { Text("No audit events returned for these filters.").foregroundStyle(.secondary) }
                ForEach(model.events) { event in
                    DisclosureGroup {
                        NativeSelectableText(event.summary)
                        NativeSelectableText(event.date.formatted(date: .abbreviated, time: .standard)).font(.caption)
                        NativeSelectableText("\(event.source) · \(event.actor)").font(.caption).foregroundStyle(.secondary)
                        NativeSelectableText("Session: \(event.sessionID)").font(.caption)
                        oversightCode("Detail", text: event.detail)
                    } label: {
                        Text("\(event.kind) · \(event.decision) · \(event.summary)")
                            .font(.callout)
                    }.padding(12).background(RoundedRectangle(cornerRadius: 10).fill(Color.secondary.opacity(0.06)))
                }
              }.padding(.horizontal, 24).padding(.bottom, 24).frame(maxWidth: .infinity, alignment: .leading)
            }.frame(maxWidth: .infinity, maxHeight: .infinity)
        }.frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .task(id: baseURL) {
            model.configure(baseURL: baseURL)
            await model.refresh()
            guard baseURL != nil else { return }
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 4_000_000_000)
                if !Task.isCancelled { await model.refresh() }
            }
        }
        .confirmationDialog(approve ? "Approve this tool for its session?" : "Deny this pending request?", isPresented: Binding(get: { confirmation != nil }, set: { if !$0 { confirmation = nil } }), titleVisibility: .visible) {
            if let request = confirmation {
                Button(approve ? "Approve for this session" : "Deny request", role: approve ? nil : .destructive) {
                    let choice = approve; confirmation = nil
                    Task { await model.decide(request, approve: choice) }
                }
                Button("Cancel", role: .cancel) { confirmation = nil }
            }
        } message: {
            Text(approve ? "The core tool will run using the reviewed arguments. This also grants the same tool for this session. It does not change your global autonomy policy." : "This denies only the pending request. It does not change your global autonomy policy.")
        }
        .confirmationDialog(model.paused == true ? "Resume supervised calls?" : "Pause new supervised calls?", isPresented: $pauseConfirmation, titleVisibility: .visible) {
            Button(model.paused == true ? "Resume" : "Pause") { let desired = model.paused != true; Task { await model.setPaused(desired) } }
            Button("Cancel", role: .cancel) {}
        }
    }

    private func oversightNotice(_ text: String) -> some View { NativeSelectableText(text).font(.callout).foregroundStyle(.red) }
    private func oversightCode(_ title: String, text: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.caption.weight(.semibold))
            ScrollView(.vertical) { NativeSelectableText(text).font(.system(.callout, design: .monospaced)).frame(maxWidth: .infinity, alignment: .leading).padding(10) }
                .frame(height: 160)
        }
    }
}
