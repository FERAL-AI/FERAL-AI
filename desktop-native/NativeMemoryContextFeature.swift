import Foundation
import SwiftUI
import CoreFoundation

private struct MemoryContextFailure: Error { let message: String }
private func memoryContextNumber(_ value: Any?) -> Double? { guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(), number.doubleValue.isFinite else { return nil }; return number.doubleValue }
struct NativeMemoryContextLayer: Identifiable { let id: Int; let title: String; let body: String }
struct NativeMemoryContextSnapshot: Identifiable {
    let id: Int
    let session: String, query: String, specialist: String, context: String
    let latency: Double, timestamp: Date
    var layers: [NativeMemoryContextLayer] {
        guard !context.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return [] }
        var blocks: [(String, String)] = []
        var title = "Memory", body: [String] = []
        for line in context.components(separatedBy: "\n") {
            if line.hasPrefix("## ") {
                if !body.isEmpty { blocks.append((title, body.joined(separator: "\n"))) }
                title = String(line.dropFirst(3)); body = []
            } else { body.append(line) }
        }
        if !body.isEmpty { blocks.append((title, body.joined(separator: "\n"))) }
        return blocks.enumerated().map { NativeMemoryContextLayer(id: $0.offset, title: $0.element.0, body: $0.element.1) }
    }
}
final class NativeMemoryContextRedirectGuard: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
    static func session() -> URLSession { URLSession(configuration: .ephemeral, delegate: NativeMemoryContextRedirectGuard(), delegateQueue: nil) }
}
@MainActor final class NativeMemoryContextModel: ObservableObject {
    @Published private(set) var snapshots: [NativeMemoryContextSnapshot]?
    @Published private(set) var loading = false
    @Published private(set) var error: String?
    @Published private(set) var loadedAt: Date?
    @Published private(set) var stale = false
    private var baseURL: URL?
    private var generation = UUID()
    private let session: URLSession
    init(baseURL: URL?, session: URLSession? = nil) { self.baseURL = baseURL; self.session = session ?? NativeMemoryContextRedirectGuard.session() }
    func configure(_ url: URL?) { guard url != baseURL else { return }; baseURL = url; generation = UUID(); snapshots = nil; loadedAt = nil; error = nil; stale = false; loading = false }
    func refresh() async {
        guard !loading else { return }
        let start = generation; loading = true; error = nil
        defer { if generation == start { loading = false } }
        do {
            try Task.checkCancellation()
            guard let baseURL, ["http", "https"].contains(baseURL.scheme ?? ""), ["127.0.0.1", "::1", "[::1]"].contains(baseURL.host ?? ""), baseURL.user == nil, baseURL.password == nil, var parts = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else { throw MemoryContextFailure(message: "Connect to the app-owned loopback service.") }
            parts.path = "/api/memory/context"; parts.queryItems = [URLQueryItem(name: "limit", value: "20")]; parts.fragment = nil
            guard let url = parts.url else { throw MemoryContextFailure(message: "Invalid context inspector request.") }
            var request = URLRequest(url: url); request.httpMethod = "GET"; request.timeoutInterval = 30
            let (data, response) = try await session.feralLocalData(for: request); try Task.checkCancellation()
            guard generation == start else { return }
            guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode), data.count <= 2 * 1024 * 1024, let value = try JSONSerialization.jsonObject(with: data) as? [String: Any], value["error"] == nil, let count = memoryContextNumber(value["count"]), let rows = value["snapshots"] as? [[String: Any]], rows.count <= 20, count == Double(rows.count) else { throw MemoryContextFailure(message: "The context snapshot response is incomplete or exceeds its bounds.") }
            let parsed = try rows.enumerated().map { index, row -> NativeMemoryContextSnapshot in
                guard let session = row["session_id"] as? String, !session.isEmpty, session.utf8.count <= 1024, let query = row["query"] as? String, query.utf8.count <= 4096, let specialist = row["memory_filter"] as? String, specialist.utf8.count <= 4096, let context = row["memory_context"] as? String, context.utf8.count <= 128 * 1024, let latency = memoryContextNumber(row["latency_ms"]), latency >= 0, latency <= 100_000_000, let ts = memoryContextNumber(row["ts"]), ts > 0, ts <= 253_402_300_799 else { throw MemoryContextFailure(message: "A recorded snapshot is malformed or exceeds its bounds. No partial snapshot is shown.") }
                return NativeMemoryContextSnapshot(id: index, session: session, query: query, specialist: specialist, context: context, latency: latency, timestamp: Date(timeIntervalSince1970: ts))
            }
            snapshots = parsed; loadedAt = Date(); stale = false
        } catch {
            guard generation == start else { return }
            stale = snapshots != nil
            self.error = (error as? MemoryContextFailure)?.message ?? "Context snapshots could not be loaded. Private backend details are withheld."
        }
    }
}
struct NativeMemoryContextFeatureView: View {
    let baseURL: URL?
    @StateObject private var model: NativeMemoryContextModel
    init(baseURL: URL?) { self.baseURL = baseURL; _model = StateObject(wrappedValue: NativeMemoryContextModel(baseURL: baseURL)) }
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack { Text("Memory context").font(.title2.bold()); Spacer(); if model.loading { ProgressView().controlSize(.small) }; Button("Refresh recorded turns") { Task { await model.refresh() } }.disabled(model.loading) }
            Text("Read the latest 20 recorded memory assemblies across this brain’s sessions, including specialist turns. This local inspector does not generate a response or run a new memory search. Recorded queries and context can contain private information.").foregroundStyle(.secondary)
            Text("These are recorded memory blocks, not the complete model prompt or proof that the model used every source. The in-process ring holds up to 50 snapshots and is lost when the brain restarts. Missing layers mean none were recorded in that block.").font(.caption).foregroundStyle(.secondary)
            if let error = model.error { NativeSelectableText(error).foregroundStyle(.red) }
            if let loaded = model.loadedAt { Text("\(model.stale ? "Stale retained data — last successful read" : "Last successful read"): \(loaded.formatted())").font(.caption).foregroundStyle(model.stale ? Color.orange : Color.secondary) }
            if let rows = model.snapshots {
                if rows.isEmpty { Text("No memory assemblies are currently recorded. This does not prove memory was unused; earlier snapshots may have been cleared or expired from the ring.").foregroundStyle(.secondary) }
                ScrollView { LazyVStack(alignment: .leading, spacing: 12) { ForEach(rows) { snapshot in snapshotCard(snapshot) } }.frame(maxWidth: .infinity, alignment: .leading) }
            } else if !model.loading { Text("No recorded snapshots have been loaded.").foregroundStyle(.secondary) }
        }.padding().task(id: baseURL) { model.configure(baseURL); await model.refresh() }
    }
    private func snapshotCard(_ snapshot: NativeMemoryContextSnapshot) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(snapshot.timestamp.formatted()).font(.headline)
            NativeSelectableText("Session: \(snapshot.session) · Assembly: \(snapshot.latency.formatted()) ms").font(.caption)
            NativeSelectableText(snapshot.specialist.isEmpty ? "Specialist filter: none recorded" : "Specialist filter: \(snapshot.specialist)").font(.caption)
            DisclosureGroup("Recorded query") { NativeSelectableText(snapshot.query.isEmpty ? "No query recorded." : snapshot.query).frame(maxWidth: .infinity, alignment: .leading) }
            if snapshot.layers.isEmpty { Text("The recorded memory block is empty. Source success or failure cannot be inferred from this alone.").foregroundStyle(.secondary) }
            ForEach(snapshot.layers) { layer in DisclosureGroup(layer.title.isEmpty ? "Untitled recorded layer" : layer.title) { NativeSelectableText(layer.body).font(.system(.body, design: .monospaced)).frame(maxWidth: .infinity, alignment: .leading) } }
            DisclosureGroup("Complete recorded memory block") { NativeSelectableText(snapshot.context).font(.system(.body, design: .monospaced)).frame(maxWidth: .infinity, alignment: .leading) }
        }.padding(14).frame(maxWidth: .infinity, alignment: .leading).background(Color.secondary.opacity(0.07), in: RoundedRectangle(cornerRadius: 12))
    }
}
