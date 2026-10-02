import Foundation
import CoreFoundation
import SwiftUI

enum NativeOperationsTab: String, CaseIterable { case jobs = "Jobs", timeline = "Timeline", checkpoints = "Checkpoints", coding = "Coding activity" }
struct NativeOperationJob: Identifiable {
    let connection: UUID, id: String, kind: String, name: String, status: String, session: String, route: String, detail: String
    let progress: Double?
    var stopRoute: (method: String, path: String)? {
        func valid(_ id: String) -> Bool { !id.isEmpty && id.unicodeScalars.allSatisfy { CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.").contains($0) } }
        let expected: String
        switch kind {
        case "taskflow": guard valid(id), ["queued", "running", "waiting", "paused"].contains(status) else { return nil }; expected = "POST /api/taskflows/\(id)/cancel"
        case "routine": guard id.hasPrefix("routine-") else { return nil }; let target = String(id.dropFirst(8)); guard !target.isEmpty, target.allSatisfy({ $0.isASCII && $0.isNumber }), status == "scheduled" else { return nil }; expected = "DELETE /api/routines/\(target)"
        case "tool_genesis": guard id.hasPrefix("draft-") else { return nil }; let target = String(id.dropFirst(6)); guard valid(target), ["pending_review", "pending", "draft", "sandbox"].contains(status) else { return nil }; expected = "POST /api/tool-genesis/\(target)/reject"
        default: return nil
        }
        guard route == expected else { return nil }; let parts = expected.split(separator: " "); return (String(parts[0]), String(parts[1]))
    }
    var stopLabel: String { kind == "routine" ? "Delete scheduled routine…" : kind == "tool_genesis" ? "Reject draft…" : "Cancel job…" }
}
struct NativeOperationEntry: Identifiable { let id: String, title: String, content: String, category: String, context: String; let date: Date }
struct NativeCheckpointTurn: Identifiable { let id: String, session: String; let files: Int, actions: Int; let date: Date }
struct NativeRevertEntry: Identifiable { let id: String, target: String, action: String, status: String, detail: String, kind: String }
struct NativeCheckpointReview {
    let connection: UUID, turn: String, signature: String, entries: [NativeRevertEntry], note: String
    var drifted: Bool { entries.contains { $0.status == "drifted" } }
}
struct NativeCodingRecord: Identifiable { let connection: UUID, id: String, agent: String, folder: String; let live: Bool; let turns: Int }
private struct OperationsFailure: LocalizedError { let message: String; var errorDescription: String? { message } }
private func operationsBool(_ value: Any?) -> Bool? { guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else { return nil }; return number.boolValue }
private func operationsText(_ value: Any) -> String { guard JSONSerialization.isValidJSONObject(value), let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys, .prettyPrinted]) else { return String(describing: value) }; return String(decoding: data, as: UTF8.self) }

final class OperationsRedirectGuard: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        guard let original = task.originalRequest?.url, let next = request.url,
              original.scheme == next.scheme, original.host == next.host, original.port == next.port,
              next.user == nil, next.password == nil else { completionHandler(nil); return }
        completionHandler(request)
    }
}
@MainActor final class NativeOperationsModel: ObservableObject {
    @Published var tab: NativeOperationsTab = .jobs
    @Published var timelineType = "memories"
    @Published var days = 7
    @Published private(set) var jobs: [NativeOperationJob] = []
    @Published private(set) var timeline: [NativeOperationEntry] = []
    @Published private(set) var turns: [NativeCheckpointTurn] = []
    @Published private(set) var coding: [NativeCodingRecord] = []
    @Published private(set) var activity: [NativeOperationEntry] = []
    @Published private(set) var preview: NativeCheckpointReview?
    @Published private(set) var revertEntries: [NativeRevertEntry] = []
    @Published private(set) var checkpointNote = ""
    @Published private(set) var degraded = ""
    @Published private(set) var errors: [NativeOperationsTab: String] = [:]
    @Published private(set) var fresh = Set<NativeOperationsTab>()
    @Published private(set) var receipt: String?
    @Published private(set) var busy = false
    private var baseURL: URL?
    private var generation = UUID()
    private let session: URLSession
    init(baseURL: URL?, session: URLSession? = nil) { self.baseURL = baseURL; self.session = session ?? URLSession(configuration: .ephemeral, delegate: OperationsRedirectGuard(), delegateQueue: nil) }
    func configure(baseURL: URL?) {
        guard self.baseURL != baseURL else { return }; self.baseURL = baseURL; generation = UUID()
        jobs = []; timeline = []; turns = []; coding = []; activity = []; preview = nil; revertEntries = []; errors = [:]; fresh = []; receipt = nil; busy = false; degraded = ""; checkpointNote = ""
    }
    private func safeID(_ id: String) throws -> String {
        guard !id.isEmpty, id.unicodeScalars.allSatisfy({ CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:").contains($0) }) else { throw OperationsFailure(message: "The agent returned an unsupported identifier.") }; return id
    }
    private func request(_ path: String, method: String = "GET", body: [String: Any]? = nil, query: [URLQueryItem] = []) async throws -> [String: Any] {
        try Task.checkCancellation(); let started = generation
        guard let baseURL, ["127.0.0.1", "::1", "[::1]"].contains(baseURL.host ?? ""), baseURL.user == nil, baseURL.password == nil, ["http", "https"].contains(baseURL.scheme ?? "") else { throw OperationsFailure(message: "Connect to the local agent to view operations.") }
        var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)!; components.path = path; components.queryItems = query.isEmpty ? nil : query
        guard let url = components.url else { throw OperationsFailure(message: "Invalid operation request.") }
        var req = URLRequest(url: url); req.httpMethod = method; req.timeoutInterval = 180
        if let body { req.setValue("application/json", forHTTPHeaderField: "Content-Type"); req.httpBody = try JSONSerialization.data(withJSONObject: body) }
        let (data, response) = try await session.data(for: req); try Task.checkCancellation()
        guard started == generation else { throw OperationsFailure(message: "Agent connection changed. Review again.") }
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else { throw OperationsFailure(message: "The request failed. Refresh to confirm state before retrying.") }
        guard let value = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { throw OperationsFailure(message: "The agent response is incomplete.") }
        if path != "/api/checkpoints/revert", let error = value["error"] as? String, !error.isEmpty { throw OperationsFailure(message: "The agent reported an operation error. State is not confirmed.") }
        return value
    }
    func refresh() async {
        guard !busy else { return }; let started = generation; let selected = tab; busy = true
        defer { if generation == started { busy = false } }
        do {
            switch selected {
            case .jobs:
                let data = try await request("/api/jobs", query: [URLQueryItem(name: "limit", value: "100")])
                guard let rows = data["items"] as? [[String: Any]] else { throw OperationsFailure(message: "Job listing is incomplete.") }
                var seen = Set<String>()
                jobs = try rows.map { row in
                    guard let id = row["id"] as? String, let kind = row["kind"] as? String, seen.insert(kind + ":" + id).inserted else { throw OperationsFailure(message: "Invalid job identity.") }
                    let raw = (row["progress"] as? NSNumber)?.doubleValue
                    return NativeOperationJob(connection: started, id: id, kind: kind, name: row["name"] as? String ?? id, status: row["status"] as? String ?? "unknown", session: row["context_session_id"] as? String ?? "", route: row["cancellable_via"] as? String ?? "", detail: operationsText(row["detail"] as? [String: Any] ?? [:]), progress: raw.flatMap { $0.isFinite ? max(0, min(1, $0)) : nil })
                }
                let missing = (data["degraded"] as? [String: Any] ?? [:]).keys.sorted(); degraded = missing.isEmpty ? "" : "Job sources unavailable: " + missing.joined(separator: ", ")
            case .timeline:
                guard (1...90).contains(days), ["all", "memories", "events", "health", "chat"].contains(timelineType) else { throw OperationsFailure(message: "Invalid timeline filter.") }
                let data = try await request("/api/timeline", query: [URLQueryItem(name: "days", value: String(days)), URLQueryItem(name: "type", value: timelineType)])
                guard let rows = data["entries"] as? [[String: Any]] else { throw OperationsFailure(message: "Timeline listing is incomplete.") }
                timeline = rows.enumerated().map { index, row in
                    let metadata = row["metadata"] as? [String: Any] ?? [:]
                    return NativeOperationEntry(id: "\(index)", title: row["title"] as? String ?? "Entry", content: row["content"] as? String ?? "", category: row["type"] as? String ?? "", context: metadata["session_id"] as? String ?? "", date: Date(timeIntervalSince1970: (row["timestamp"] as? NSNumber)?.doubleValue ?? 0))
                }
            case .checkpoints:
                let data = try await request("/api/checkpoints/turns", query: [URLQueryItem(name: "limit", value: "50")])
                guard let rows = data["turns"] as? [[String: Any]] else { throw OperationsFailure(message: "Checkpoint listing is incomplete.") }
                turns = try rows.map { row in
                    guard let id = row["turn_id"] as? String else { throw OperationsFailure(message: "Checkpoint turn has no identifier.") }; _ = try safeID(id)
                    return NativeCheckpointTurn(id: id, session: row["session_id"] as? String ?? "", files: row["files"] as? Int ?? 0, actions: row["actions"] as? Int ?? 0, date: Date(timeIntervalSince1970: (row["ended_at"] as? NSNumber)?.doubleValue ?? 0))
                }; checkpointNote = data["note"] as? String ?? "Shell changes are not covered by file checkpoints."
            case .coding:
                let data = try await request("/api/coding/activity")
                guard let rows = data["sessions"] as? [[String: Any]], let live = data["live_sessions"] as? [[String: Any]], let resumable = data["resumable_sessions"] as? [[String: Any]] else { throw OperationsFailure(message: "Coding activity listing is incomplete.") }
                let liveIDs = Set(live.compactMap { $0["handle"] as? String }); var seen = Set<String>()
                coding = try (live + resumable).compactMap { row in
                    guard let id = row["handle"] as? String, !id.isEmpty else { throw OperationsFailure(message: "Coding session has no handle.") }; _ = try safeID(id); guard seen.insert(id).inserted else { return nil }
                    return NativeCodingRecord(connection: started, id: id, agent: row["agent_id"] as? String ?? "", folder: row["cwd"] as? String ?? "", live: liveIDs.contains(id) && operationsBool(row["alive"]) == true, turns: row["turns"] as? Int ?? 0)
                }
                activity = rows.enumerated().map { index, row in NativeOperationEntry(id: String(describing: row["episode_id"] ?? index), title: row["summary"] as? String ?? "Recorded agent activity", content: row["detail"] as? String ?? "", category: row["agent_id"] as? String ?? "", context: row["workspace_dir"] as? String ?? "", date: Date(timeIntervalSince1970: (row["at"] as? NSNumber)?.doubleValue ?? 0)) }
                degraded = (data["degraded"] as? String ?? "").isEmpty ? "" : "Coding history is degraded; displayed records may be incomplete."
            }
            fresh.insert(selected); errors[selected] = nil
        } catch { if generation == started { fresh.remove(selected); errors[selected] = error.localizedDescription } }
    }
    func stop(_ reviewed: NativeOperationJob) async {
        guard !busy, fresh.contains(.jobs), reviewed.connection == generation, let current = jobs.first(where: { $0.id == reviewed.id && $0.kind == reviewed.kind }), current.route == reviewed.route, current.status == reviewed.status, let route = current.stopRoute else { errors[.jobs] = "Job changed or has no supported stop route. Refresh and review again."; return }
        let started = generation; busy = true; receipt = nil; defer { if generation == started { busy = false } }
        do {
            let result = try await request(route.path, method: route.method, body: route.method == "POST" ? [:] : nil)
            let confirmed = current.kind == "routine" ? operationsBool(result["ok"]) == true : current.kind == "tool_genesis" ? operationsBool(result["success"]) == true && result["rejected"] as? String == String(current.id.dropFirst(6)) : (result["id"] as? String ?? result["flow_id"] as? String) == current.id && result["status"] as? String == "cancelled"
            guard confirmed else { throw OperationsFailure(message: "The backend did not confirm the requested change.") }
            receipt = current.kind == "routine" ? "Scheduled routine deleted." : current.kind == "tool_genesis" ? "Skill draft rejected." : "Backend marked the TaskFlow cancelled. An already-running step may continue; refresh to confirm subsequent state."
            if current.kind == "taskflow" { fresh.remove(.jobs) } else { jobs.removeAll { $0.id == current.id && $0.kind == current.kind } }; errors[.jobs] = nil
        } catch { if started == generation { fresh.remove(.jobs); errors[.jobs] = "Change not confirmed. Refresh before retrying. " + error.localizedDescription } }
    }
    private func parsePreview(_ data: [String: Any], turn: String) throws -> NativeCheckpointReview {
        guard data["turn_id"] as? String == turn, operationsBool(data["dry_run"]) == true, let files = data["files"] as? [[String: Any]], let actions = data["actions"] as? [[String: Any]] else { throw OperationsFailure(message: "Revert preview is incomplete or names another turn.") }
        let rows = files + actions
        let entries = try rows.enumerated().map { index, row in
            let kind = row["kind"] as? String ?? "file"; let target = kind == "action" ? row["target"] as? String ?? "" : row["path"] as? String ?? ""
            guard !target.isEmpty, let action = row["action"] as? String, ["restore", "delete", "compensate", "skip"].contains(action) else { throw OperationsFailure(message: "A revert target or action is missing.") }
            return NativeRevertEntry(id: String(index), target: target, action: action, status: row["status"] as? String ?? "unknown", detail: row["detail"] as? String ?? "", kind: kind)
        }
        return NativeCheckpointReview(connection: generation, turn: turn, signature: operationsText(rows), entries: entries, note: data["note"] as? String ?? "Shell changes are not covered.")
    }
    func inspect(_ turn: String) async {
        guard !busy, fresh.contains(.checkpoints), turns.contains(where: { $0.id == turn }) else { return }; let started = generation; busy = true; preview = nil; receipt = nil; revertEntries = []
        defer { if generation == started { busy = false } }
        do { _ = try safeID(turn); preview = try parsePreview(await request("/api/checkpoints/revert", method: "POST", body: ["turn_id": turn, "dry_run": true, "force": false]), turn: turn); errors[.checkpoints] = nil }
        catch { if started == generation { errors[.checkpoints] = error.localizedDescription } }
    }
    func revert(_ reviewed: NativeCheckpointReview, force: Bool) async {
        guard !busy, reviewed.connection == generation, fresh.contains(.checkpoints), preview?.signature == reviewed.signature, preview?.turn == reviewed.turn else { errors[.checkpoints] = "Revert review is stale. Inspect this turn again."; return }
        let started = generation; busy = true; receipt = nil
        defer { if generation == started { busy = false } }
        do {
            let latest = try parsePreview(await request("/api/checkpoints/revert", method: "POST", body: ["turn_id": reviewed.turn, "dry_run": true, "force": false]), turn: reviewed.turn)
            guard latest.signature == reviewed.signature else { preview = latest; throw OperationsFailure(message: "Affected targets or revert status changed. Review the refreshed plan before confirming.") }
            let result = try await request("/api/checkpoints/revert", method: "POST", body: ["turn_id": reviewed.turn, "dry_run": false, "force": force])
            guard result["turn_id"] as? String == reviewed.turn, operationsBool(result["dry_run"]) == false else { throw OperationsFailure(message: "Revert outcome is not confirmed for this turn.") }
            guard let restored = result["reverted"] as? [String], let undone = result["reverted_actions"] as? [[String: Any]], let rows = result["entries"] as? [[String: Any]] else { throw OperationsFailure(message: "Revert outcome entries are incomplete.") }; let files = restored.count; let actions = undone.count
            if operationsBool(result["refused"]) == true { receipt = "Revert refused. Nothing was restored; newer file changes are protected." }
            else if operationsBool(result["partial"]) == true { receipt = "Revert incomplete: \(files) file(s) restored, \(actions) action(s) undone. Review failed entries."; preview = nil }
            else if operationsBool(result["success"]) == true { receipt = "Revert confirmed: \(files) file(s) restored, \(actions) action(s) undone."; preview = nil }
            else { receipt = "Revert did not complete. Review its reported outcomes." }
            revertEntries = rows.enumerated().map { index, row in NativeRevertEntry(id: String(index), target: row["kind"] as? String == "action" ? row["target"] as? String ?? "" : row["path"] as? String ?? "", action: row["action"] as? String ?? "", status: row["status"] as? String ?? "", detail: row["detail"] as? String ?? "", kind: row["kind"] as? String ?? "file") }; errors[.checkpoints] = nil
        } catch { if started == generation { errors[.checkpoints] = "Revert not confirmed. " + error.localizedDescription } }
    }
    func closeCoding(_ reviewed: NativeCodingRecord) async {
        guard !busy, fresh.contains(.coding), reviewed.connection == generation, coding.contains(where: { $0.id == reviewed.id && $0.live }), reviewed.live else { errors[.coding] = "Live session is no longer confirmed. Refresh first."; return }
        let started = generation; busy = true; defer { if generation == started { busy = false } }
        do {
            let result = try await request("/api/coding/sessions/\(try safeID(reviewed.id))/cancel", method: "POST", body: [:])
            guard result["handle"] as? String == reviewed.id, operationsBool(result["closed"]) == true else { throw OperationsFailure(message: "Session closure was not confirmed.") }
            receipt = "Coding session closure confirmed. Cancellation was requested for any in-flight work."; coding.removeAll { $0.id == reviewed.id }; errors[.coding] = nil
        } catch { if started == generation { fresh.remove(.coding); errors[.coding] = error.localizedDescription } }
    }
    func canOpen(_ record: NativeCodingRecord) -> Bool { fresh.contains(.coding) && record.connection == generation && record.agent == "opencode" && !record.folder.isEmpty && coding.contains(where: { $0.id == record.id }) }
}

struct NativeOperationsFeatureView: View {
    let baseURL: URL?
    let onOpenCodingSession: ((String, String) -> Void)?
    @StateObject private var model: NativeOperationsModel
    @State private var stopReview: NativeOperationJob?
    @State private var codingReview: NativeCodingRecord?
    @State private var reverting: NativeCheckpointReview?
    @State private var force = false
    init(baseURL: URL?, onOpenCodingSession: ((String, String) -> Void)? = nil) { self.baseURL = baseURL; self.onOpenCodingSession = onOpenCodingSession; _model = StateObject(wrappedValue: NativeOperationsModel(baseURL: baseURL)) }
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack { Text("Operations").font(.largeTitle.weight(.semibold)); Spacer(); Button("Refresh") { Task { await model.refresh() } }.disabled(model.busy); if model.busy { ProgressView().controlSize(.small) } }.padding(.horizontal, 24).padding(.top, 24)
            Picker("View", selection: $model.tab) { ForEach(NativeOperationsTab.allCases, id: \.self) { Text($0.rawValue).tag($0) } }.pickerStyle(.segmented).padding(.horizontal, 24).disabled(model.busy)
            ScrollView(.vertical) {
                LazyVStack(alignment: .leading, spacing: 18) {
                    if let error = model.errors[model.tab] { NativeSelectableText(error).foregroundStyle(.red) }
                    if let receipt = model.receipt { NativeSelectableText(receipt) }
                    if !model.fresh.contains(model.tab) { Text("This view is not confirmed. Refresh to load current state.").foregroundStyle(.secondary) }
                    switch model.tab {
                    case .jobs:
                        if !model.degraded.isEmpty { Text(model.degraded).foregroundStyle(.orange) }
                        if model.jobs.isEmpty && model.fresh.contains(.jobs) { Text("No jobs returned. Some sources may be unavailable if noted above.").foregroundStyle(.secondary) }
                        ForEach(model.jobs, id: \.selfID) { job in
                            card {
                                NativeSelectableText(job.name).font(.headline); Text("\(job.kind) · \(job.status)").font(.caption)
                                if let progress = job.progress { ProgressView(value: progress) }
                                if !job.session.isEmpty { NativeSelectableText("Session: \(job.session)").font(.caption) }
                                DisclosureGroup("Details") { NativeSelectableText(job.detail).font(.system(.callout, design: .monospaced)) }
                                if job.stopRoute != nil { Button(job.stopLabel) { stopReview = job }.disabled(model.busy || !model.fresh.contains(.jobs)) }
                                else { Text("No supported stop route for this job.").font(.caption).foregroundStyle(.secondary) }
                            }
                        }
                    case .timeline:
                        HStack {
                            Picker("Type", selection: $model.timelineType) { Text("Memories").tag("memories"); Text("Chat").tag("chat"); Text("Health").tag("health"); Text("Events").tag("events"); Text("All").tag("all") }
                            Stepper("\(model.days) days", value: $model.days, in: 1...90)
                            Button("Apply") { Task { await model.refresh() } }.disabled(model.busy)
                        }
                        Text("Events and All can read your connected calendar. The backend returns a bounded history, not a complete export.").font(.caption).foregroundStyle(.secondary)
                        ForEach(model.timeline) { entry in entryView(entry) }
                        if model.timeline.isEmpty && model.fresh.contains(.timeline) { Text("No timeline entries returned for this window.").foregroundStyle(.secondary) }
                    case .checkpoints:
                        Text(model.checkpointNote).font(.callout).foregroundStyle(.orange)
                        Text("A revert restores recorded file writes and may undo created calendar/reminder/routine objects. It does not undo shell changes.").font(.callout)
                        ForEach(model.turns) { turn in card {
                            NativeSelectableText(turn.id).font(.headline); Text("\(turn.files) file(s) · \(turn.actions) reversible action(s) · \(turn.session)").font(.caption)
                            Text(turn.date.formatted()).font(.caption); Button("Review exact revert targets") { Task { await model.inspect(turn.id) } }.disabled(model.busy || !model.fresh.contains(.checkpoints))
                        } }
                        if let preview = model.preview {
                            Text("Review turn \(preview.turn)").font(.headline); Text(preview.note).font(.callout)
                            ForEach(preview.entries) { entry in revertEntry(entry) }
                            HStack {
                                Button("Revert reviewed turn…") { force = false; reverting = preview }.disabled(model.busy)
                                if preview.drifted { Button("Overwrite newer changes and revert…") { force = true; reverting = preview }.disabled(model.busy) }
                            }
                        }
                        ForEach(model.revertEntries) { entry in revertEntry(entry) }
                        if model.turns.isEmpty && model.fresh.contains(.checkpoints) { Text("No checkpointed turns returned.").foregroundStyle(.secondary) }
                    case .coding:
                        if !model.degraded.isEmpty { Text(model.degraded).foregroundStyle(.orange) }
                        Text("Sessions can be live or indexed for possible resumption. Continuing still requires a configured model and granted project. Only OpenCode sessions can open in this coding workspace.").font(.callout).foregroundStyle(.secondary)
                        ForEach(model.coding) { record in card {
                            Text(record.agent + " · " + (record.live ? "live" : "indexed")).font(.headline); NativeSelectableText(record.folder); NativeSelectableText("\(record.turns) turns · \(record.id)").font(.caption)
                            HStack {
                                if onOpenCodingSession != nil && model.canOpen(record) { Button("Open in Coding") { if model.canOpen(record) { onOpenCodingSession?(record.id, record.folder) } }.disabled(model.busy) }
                                if record.live { Button("Close session…") { codingReview = record }.disabled(model.busy || !model.fresh.contains(.coding)) }
                            }
                        } }
                        Text("Recorded coding activity").font(.title2)
                        ForEach(model.activity) { entry in entryView(entry) }
                        if model.activity.isEmpty && model.fresh.contains(.coding) { Text("No recorded coding activity returned.").foregroundStyle(.secondary) }
                    }
                }.padding(24).frame(maxWidth: .infinity, alignment: .leading)
            }.frame(maxWidth: .infinity, maxHeight: .infinity)
        }.frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .task(id: baseURL) { stopReview = nil; codingReview = nil; reverting = nil; model.configure(baseURL: baseURL); await model.refresh(); while !Task.isCancelled { do { try await Task.sleep(nanoseconds: 4_000_000_000) } catch { break }; if model.tab == .jobs || model.tab == .coding { await model.refresh() } } }
        .onChange(of: model.tab) { _ in Task { await model.refresh() } }
        .alert("Confirm job change", isPresented: Binding(get: { stopReview != nil }, set: { if !$0 { stopReview = nil } })) { if let job = stopReview { Button("Confirm", role: .destructive) { stopReview = nil; Task { await model.stop(job) } }; Button("Cancel", role: .cancel) { stopReview = nil } } } message: { Text(stopReview.map { "\($0.name)\n\($0.kind == "routine" ? "This permanently deletes the scheduled routine, not only its next run." : "This changes only the backend-identified job.")" } ?? "") }
        .alert(force ? "Overwrite newer changes?" : "Revert this reviewed turn?", isPresented: Binding(get: { reverting != nil }, set: { if !$0 { reverting = nil } })) { if let review = reverting { Button(force ? "Overwrite and revert" : "Revert", role: .destructive) { let desired = force; reverting = nil; Task { await model.revert(review, force: desired) } }; Button("Cancel", role: .cancel) { reverting = nil } } } message: { Text(reverting.map { "Turn: \($0.turn)\nExact affected targets:\n\($0.entries.map(\.target).joined(separator: "\n"))\n\(force ? "Newer content in drifted files will be lost." : "Newer file changes cause refusal.")\nShell changes are not covered. External object reversals can partially fail." } ?? "") }
        .alert("Close this coding session?", isPresented: Binding(get: { codingReview != nil }, set: { if !$0 { codingReview = nil } })) { if let record = codingReview { Button("Close", role: .destructive) { codingReview = nil; Task { await model.closeCoding(record) } }; Button("Cancel", role: .cancel) { codingReview = nil } } } message: { Text(codingReview.map { "\($0.id)\n\($0.folder)\nCancellation is requested for any in-flight task. Closing does not revert its file edits." } ?? "") }
    }
    private func card<Content: View>(@ViewBuilder _ content: () -> Content) -> some View { VStack(alignment: .leading, spacing: 10, content: content).padding(16).frame(maxWidth: .infinity, alignment: .leading).background(RoundedRectangle(cornerRadius: 12).fill(Color.secondary.opacity(0.06))) }
    private func entryView(_ entry: NativeOperationEntry) -> some View { card { NativeSelectableText(entry.title).font(.headline); Text("\(entry.category) · \(entry.date.formatted())").font(.caption); if !entry.context.isEmpty { NativeSelectableText(entry.context).font(.caption) }; if !entry.content.isEmpty { DisclosureGroup("Read details") { NativeSelectableText(entry.content) } } } }
    private func revertEntry(_ entry: NativeRevertEntry) -> some View { card { NativeSelectableText(entry.target).font(.headline); Text("\(entry.action == "restore" ? "Restore earlier file content" : entry.action == "delete" ? "Delete file created by turn" : entry.action == "compensate" ? "Undo created external object" : "Leave unchanged") · \(entry.status)"); if !entry.detail.isEmpty { NativeSelectableText(entry.detail).font(.callout) } } }
}
private extension NativeOperationJob { var selfID: String { kind + ":" + id } }
