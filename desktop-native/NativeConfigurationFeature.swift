import SwiftUI
import Foundation

private struct NativeConfigurationFailure: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

@MainActor final class NativeConfigurationModel: ObservableObject {
    @Published var config: [String: Any]?
    @Published var autonomy: String?
    @Published var memory: [String: Any]?
    @Published var errors: [String: String] = [:]
    @Published var notes: [String: String] = [:]
    @Published var busy = false
    private var baseURL: URL?
    private var generation = UUID()
    private let session: URLSession
    static let featureKeys = ["streaming", "proactive", "self_learning", "multi_agent", "vision"]
    static let modes = ["strict", "hybrid", "loose"]

    init(baseURL: URL?, session: URLSession? = nil) {
        self.baseURL = baseURL
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 30
        configuration.timeoutIntervalForResource = 45
        self.session = session ?? URLSession(configuration: configuration)
    }
    func setBaseURL(_ url: URL?) {
        guard baseURL != url else { return }
        generation = UUID(); baseURL = url
        config = nil; autonomy = nil; memory = nil; errors = [:]; notes = [:]; busy = false
    }
    private func request(_ path: String, body: [String: Any]? = nil) async throws -> [String: Any] {
        guard let baseURL else { throw NativeConfigurationFailure(message: "The local agent is not ready yet.") }
        guard ["127.0.0.1", "::1", "[::1]"].contains(baseURL.host ?? ""), ["http", "https"].contains(baseURL.scheme ?? ""), baseURL.user == nil, baseURL.password == nil else { throw NativeConfigurationFailure(message: "Settings are available only from the local agent's loopback address.") }
        var request = URLRequest(url: URL(string: path, relativeTo: baseURL)!)
        if let body {
            request.httpMethod = "POST"; request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, response) = try await session.feralLocalData(for: request)
        guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { throw NativeConfigurationFailure(message: "The agent returned an unreadable settings response.") }
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            let detail = object["detail"] as? String ?? (object["detail"] as? [String: Any])?["message"] as? String
            throw NativeConfigurationFailure(message: detail ?? "The agent refused this settings request.")
        }
        if let error = object["error"] as? String, !error.isEmpty { throw NativeConfigurationFailure(message: error) }
        return object
    }
    func refresh() async {
        guard !busy else { return }; busy = true
        let current = generation
        defer { if current == generation { busy = false } }
        for (section, path) in [("general", "/api/config"), ("autonomy", "/api/autonomy"), ("memory", "/api/memory/backend")] {
            guard current == generation, !Task.isCancelled else { return }
            do {
                let object = try await request(path)
                guard current == generation, !Task.isCancelled else { return }
                try accept(object, section: section); errors.removeValue(forKey: section)
            } catch {
                guard current == generation, !Task.isCancelled else { return }
                errors[section] = error.localizedDescription
            }
        }
    }
    private func accept(_ object: [String: Any], section: String) throws {
        switch section {
        case "general":
            guard object["features"] is [String: Any] else { throw NativeConfigurationFailure(message: "Feature settings are unavailable.") }
            config = object
        case "autonomy":
            guard let mode = object["mode"] as? String, Self.modes.contains(mode) else { throw NativeConfigurationFailure(message: "The agent did not report a recognized autonomy mode.") }
            autonomy = mode
        default:
            guard object["backend"] is String, object["runtime"] is String, object["available"] is [String: Any] else { throw NativeConfigurationFailure(message: "Memory backend status is unavailable.") }
            memory = object
        }
    }
    func feature(_ key: String) -> Bool? { (config?["features"] as? [String: Any])?[key] as? Bool }
    func setFeature(_ key: String, enabled: Bool) async {
        guard Self.featureKeys.contains(key), feature(key) != nil, !busy else { return }
        let expected = generation
        await mutate(section: "general") {
            let result = try await self.request("/api/config/update", body: ["section": "features", "key": key, "value": enabled])
            guard result["ok"] as? Bool == true else { throw NativeConfigurationFailure(message: "The agent did not acknowledge the feature update.") }
        } verify: {
            let object = try await self.request("/api/config")
            guard expected == self.generation else { throw CancellationError() }
            try self.accept(object, section: "general")
            guard self.feature(key) == enabled else { throw NativeConfigurationFailure(message: "The agent's saved feature value differs from your request.") }
        }
    }
    func changeAutonomy(to mode: String, confirmed: Bool) async {
        guard confirmed else { errors["autonomy"] = "Review and confirm the scope of the autonomy change first."; return }
        guard Self.modes.contains(mode), autonomy != nil, !busy else { return }
        let expected = generation
        await mutate(section: "autonomy") {
            let result = try await self.request("/api/autonomy", body: ["mode": mode])
            guard expected == self.generation else { throw CancellationError() }
            guard result["success"] as? Bool == true else { throw NativeConfigurationFailure(message: "The agent did not acknowledge the autonomy change.") }
            self.notes["autonomy"] = result["persisted"] as? Bool == true ? "The agent reports that the live mode changed and was saved for restart." : "The agent reports that the live mode changed, but it was not saved. A restart may restore the previous setting."
        } verify: {
            let object = try await self.request("/api/autonomy")
            guard expected == self.generation else { throw CancellationError() }
            try self.accept(object, section: "autonomy")
            guard self.autonomy == mode else { throw NativeConfigurationFailure(message: "The agent reports a different autonomy mode than requested.") }
        }
    }
    func switchMemory(to backend: String, confirmed: Bool) async {
        guard confirmed else { errors["memory"] = "Confirm the memory backend selection first."; return }
        guard (memory?["available"] as? [String: Any])?[backend] as? Bool == true, !busy else { return }
        let expected = generation
        await mutate(section: "memory") {
            let result = try await self.request("/api/memory/backend", body: ["backend": backend])
            guard expected == self.generation else { throw CancellationError() }
            guard result["ok"] as? Bool == true else { throw NativeConfigurationFailure(message: "Memory backend preflight did not succeed; the selection was not confirmed.") }
            self.notes["memory"] = result["note"] as? String ?? "The backend selection was saved. Refreshing the running memory status…"
        } verify: {
            let object = try await self.request("/api/memory/backend")
            guard expected == self.generation else { throw CancellationError() }
            try self.accept(object, section: "memory")
            guard object["backend"] as? String == backend else { throw NativeConfigurationFailure(message: "The agent reports a different configured memory backend than requested.") }
        }
    }
    private func mutate(section: String, write: () async throws -> Void, verify: () async throws -> Void) async {
        busy = true; errors.removeValue(forKey: section); notes.removeValue(forKey: section)
        let current = generation
        defer { if generation == current { busy = false } }
        do {
            try await write()
            guard current == generation, !Task.isCancelled else { return }
            try await verify()
        } catch {
            guard current == generation, !Task.isCancelled else { return }
            errors[section] = error.localizedDescription
        }
    }
}

private struct NativeConfigurationChange: Identifiable {
    let id = UUID()
    let kind: String
    let key: String
    let enabled: Bool
    let title: String
    let explanation: String
}

struct NativeConfigurationFeatureView: View {
    let baseURL: URL?
    @StateObject private var model: NativeConfigurationModel
    @State private var proposed: NativeConfigurationChange?
    init(baseURL: URL?) { self.baseURL = baseURL; _model = StateObject(wrappedValue: NativeConfigurationModel(baseURL: baseURL)) }
    static let descriptions = [
        "strict": "Non-read-only tool calls enter the approval gate; existing standing approvals may satisfy it. Developer shell execution requires an explicit workspace grant. Other sandbox, permission and safety checks still apply.",
        "hybrid": "Tool approval follows the tool's safety rules and learned approval history. Developer shell execution can use paths allowed by the workspace policy. Other sandbox and permission checks still apply.",
        "loose": "Tool calls run without the autonomy approval gate, including confirmation-level calls. Developer shell execution can use paths allowed by the workspace policy. Denied operations and other sandbox and permission checks still apply."
    ]
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack { Text("Agent settings").font(.title2.bold()); Spacer(); if model.busy { ProgressView().controlSize(.small) }; Button("Refresh") { Task { await model.refresh() } }.disabled(model.busy || baseURL == nil) }
            VStack(alignment: .leading, spacing: 12) {
                Text("General").font(.headline)
                VStack(alignment: .leading, spacing: 12) {
                    status("general")
                    if let config = model.config { Text("Version: " + (config["version"] as? String ?? "Unavailable")).foregroundStyle(.secondary) }
                    feature("streaming", "Streaming replies", "Show replies token by token.")
                    feature("proactive", "Proactive alerts", "Allow the agent to surface observations without a new question.")
                    feature("self_learning", "Self-learning", "Enable tool generation and pattern learning.")
                    feature("multi_agent", "Multi-agent", "Allow the orchestrator to spawn specialist agents.")
                    feature("vision", "Vision loop", "Enable periodic screen captioning for ambient context. Screen Recording permission is still required; this does not grant it.")
                }.frame(maxWidth: .infinity, alignment: .leading)
            }.padding(16).background(RoundedRectangle(cornerRadius: 12).fill(Color.secondary.opacity(0.06)))
            VStack(alignment: .leading, spacing: 12) {
                Text("Autonomy").font(.headline)
                VStack(alignment: .leading, spacing: 12) {
                    status("autonomy")
                    Text("Agent-reported mode: " + (model.autonomy ?? "Unavailable")).font(.headline)
                    ForEach(NativeConfigurationModel.modes, id: \.self) { mode in
                        HStack(alignment: .top) {
                            VStack(alignment: .leading) { Text(mode.capitalized).font(.headline); Text(Self.descriptions[mode]!).font(.caption).foregroundStyle(.secondary) }
                            Spacer()
                            Button(model.autonomy == mode ? "Selected" : "Review change") {
                                proposed = NativeConfigurationChange(kind: "autonomy", key: mode, enabled: false, title: "Change autonomy to \(mode)?", explanation: "Current agent-reported mode: \(model.autonomy ?? "unavailable").\n\n" + Self.descriptions[mode]! + "\n\nThis changes the running agent's tool and shell approval behavior and attempts to save the choice for future restarts.")
                            }.disabled(model.busy || model.autonomy == nil || model.autonomy == mode || model.errors["autonomy"] != nil)
                        }
                    }
                }.frame(maxWidth: .infinity, alignment: .leading)
            }.padding(16).background(RoundedRectangle(cornerRadius: 12).fill(Color.secondary.opacity(0.06)))
            VStack(alignment: .leading, spacing: 12) {
                Text("Memory backend").font(.headline)
                VStack(alignment: .leading, spacing: 12) {
                    status("memory")
                    if let memory = model.memory {
                        Text("In use now: " + (memory["runtime"] as? String ?? "Unavailable")).font(.headline)
                        Text("Configured: " + (memory["backend"] as? String ?? "Unavailable"))
                        Text("Constructed: " + (memory["constructed_backend"] as? String ?? "Unavailable")).font(.caption).foregroundStyle(.secondary)
                        if memory["pending_unapplied"] as? Bool == true { Label("Restart required to load the configured backend. The current runtime has not switched.", systemImage: "clock").foregroundStyle(.orange) }
                        if memory["fell_back"] as? Bool == true { Label("The configured backend failed at boot: " + (memory["boot_error"] as? String ?? "Reason unavailable"), systemImage: "exclamationmark.triangle").foregroundStyle(.orange) }
                        if memory["vector_index_degraded"] as? Bool == true { Text("Vector index degraded: " + (memory["vector_index_degraded_reason"] as? String ?? "Reason unavailable")).foregroundStyle(.orange) }
                        Text("Semantic search: " + (memory["semantic_search"] as? String ?? "Unknown") + " (reported runtime status; no new search probe)").font(.caption).foregroundStyle(.secondary)
                        if let error = memory["vector_leg_error"] as? String { Text(error).font(.caption).foregroundStyle(.orange) }
                        let available = memory["available"] as? [String: Any] ?? [:]
                        ForEach(available.keys.sorted(), id: \.self) { backend in
                            HStack {
                                Text(backend); Spacer()
                                if available[backend] as? Bool != true { Text("Not installed").foregroundStyle(.secondary) }
                                else { Button(memory["backend"] as? String == backend ? "Configured" : "Review selection") { proposed = NativeConfigurationChange(kind: "memory", key: backend, enabled: false, title: "Select \(backend) for memory?", explanation: "The agent will preflight this backend before saving. A restart may be required; the live store will not switch immediately. Existing embeddings remain in the current store and are not automatically migrated. A failed preflight does not save the new selection.") }.disabled(model.busy || memory["backend"] as? String == backend || model.errors["memory"] != nil) }
                            }
                        }
                    }
                }.frame(maxWidth: .infinity, alignment: .leading)
            }.padding(16).background(RoundedRectangle(cornerRadius: 12).fill(Color.secondary.opacity(0.06)))
        }
        .task(id: baseURL) { proposed = nil; model.setBaseURL(baseURL); await model.refresh() }
        .sheet(item: $proposed) { change in
            VStack(alignment: .leading, spacing: 18) {
                Text(change.title).font(.title2.bold())
                Text(change.explanation).fixedSize(horizontal: false, vertical: true)
                HStack { Spacer(); Button("Cancel") { proposed = nil }; Button("Confirm change") {
                    proposed = nil
                    Task {
                        if change.kind == "autonomy" { await model.changeAutonomy(to: change.key, confirmed: true) }
                        else if change.kind == "memory" { await model.switchMemory(to: change.key, confirmed: true) }
                        else { await model.setFeature(change.key, enabled: change.enabled) }
                    }
                }.keyboardShortcut(.defaultAction) }
            }.padding(24).frame(width: 520)
        }
    }
    @ViewBuilder private func status(_ section: String) -> some View {
        if let error = model.errors[section] { Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(.orange); Text("Any displayed previous state may be outdated. Refresh to verify.").font(.caption).foregroundStyle(.secondary) }
        if let note = model.notes[section] { Text(note).font(.caption).foregroundStyle(.secondary) }
        if model.busy { Text("Checking the agent…").font(.caption).foregroundStyle(.secondary) }
    }
    @ViewBuilder private func feature(_ key: String, _ title: String, _ explanation: String) -> some View {
        HStack {
            VStack(alignment: .leading) { Text(title); Text(explanation).font(.caption).foregroundStyle(.secondary) }
            Spacer()
            if let enabled = model.feature(key) {
                Toggle(title, isOn: Binding(get: { model.feature(key) ?? false }, set: { next in
                    proposed = NativeConfigurationChange(kind: "feature", key: key, enabled: next, title: "\(next ? "Enable" : "Disable") \(title.lowercased())?", explanation: explanation + "\n\nOnly the features.\(key) setting will change. Other permissions and settings stay in place.")
                })).labelsHidden().disabled(model.busy || model.errors["general"] != nil).accessibilityLabel(title + (enabled ? " enabled" : " disabled"))
            } else { Text("Unavailable").foregroundStyle(.secondary) }
        }
    }
}
