import SwiftUI
import AppKit
import UniformTypeIdentifiers
import CoreFoundation
import CoreImage.CIFilterBuiltins

struct NativeConnectionsFailure: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}

enum NativeConnectionsWire {
    static func boolean(_ value: Any?) -> Bool? {
        guard let n = value as? NSNumber, CFGetTypeID(n) == CFBooleanGetTypeID() else { return nil }; return n.boolValue
    }
    static func integer(_ value: Any?) -> Int? {
        guard let n = value as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID(), n.doubleValue.isFinite,
              n.doubleValue >= 0, n.doubleValue < Double(Int.max), n.doubleValue.rounded(.towardZero) == n.doubleValue else { return nil }; return n.intValue
    }
    static func segment(_ value: String) -> String { value.addingPercentEncoding(withAllowedCharacters: CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")) ?? "" }
    static func text(_ value: Any?) -> String { value.map { String(describing: $0) } ?? "Unavailable" }
    static func json(_ value: Any) -> String { guard let data = try? JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys]), let text = String(data: data, encoding: .utf8) else { return String(describing: value) }; return text }
    static func bundle(_ data: Data) throws -> [String: Any] {
        guard data.count <= 10 * 1024 * 1024, let value = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let node = value["node_id"] as? String, !node.isEmpty, value["vector_clock"] is [String: Any],
              let operations = value["operations"] as? [[String: Any]], operations.count <= 50_000 else {
            throw NativeConnectionsFailure("Choose a sync bundle with a node ID, vector clock and operations (maximum 10 MB / 50,000 operations).")
        }
        for op in operations {
            guard ["op_id", "table", "op_type", "row_id", "hlc", "origin_node"].allSatisfy({ (op[$0] as? String)?.isEmpty == false }),
                  ["insert", "update", "delete"].contains(op["op_type"] as? String ?? ""), op["data"] is [String: Any], op["timestamp"] is NSNumber else {
                throw NativeConnectionsFailure("The sync bundle contains an unreadable operation. Nothing was imported.")
            }
        }
        return value
    }
}

// Never follow a redirect from the app-owned API to a different origin.
final class NativeConnectionsRedirectGuard: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        guard let source = task.originalRequest?.url, let target = request.url,
              source.scheme == target.scheme, source.host == target.host, source.port == target.port else { completionHandler(nil); return }
        completionHandler(request)
    }
    static func session() -> URLSession {
        let config = URLSessionConfiguration.ephemeral; config.timeoutIntervalForRequest = 30; config.timeoutIntervalForResource = 180
        return URLSession(configuration: config, delegate: NativeConnectionsRedirectGuard(), delegateQueue: nil)
    }
}

struct NativeConnectionsClient {
    let baseURL: URL
    let session: URLSession
    func request(_ path: String, query: [URLQueryItem] = [], method: String = "GET", body: [String: Any]? = nil) async throws -> [String: Any] {
        guard baseURL.scheme == "http", ["127.0.0.1", "::1", "[::1]"].contains(baseURL.host ?? ""), baseURL.user == nil, baseURL.password == nil,
              var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else { throw NativeConnectionsFailure("Connections are managed only through the app’s local service.") }
        components.percentEncodedPath = path; components.queryItems = query.isEmpty ? nil : query; components.fragment = nil
        guard let url = components.url else { throw NativeConnectionsFailure("Invalid connection request.") }
        var request = URLRequest(url: url); request.httpMethod = method; request.timeoutInterval = 120
        if let body = body { request.httpBody = try JSONSerialization.data(withJSONObject: body); request.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        let (data, response) = try await session.feralLocalData(for: request)
        let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        guard let http = response as? HTTPURLResponse else { throw NativeConnectionsFailure("The local service returned no HTTP response.") }
        guard (200..<300).contains(http.statusCode) else {
            let detail = object?["detail"] ?? object?["error"] ?? "Request failed"
            throw NativeConnectionsFailure("HTTP \(http.statusCode): \(NativeConnectionsWire.text(detail))")
        }
        guard let value = object else { throw NativeConnectionsFailure("The local service returned unreadable JSON.") }
        if let error = value["error"] { throw NativeConnectionsFailure(NativeConnectionsWire.text(error)) }
        if NativeConnectionsWire.boolean(value["ok"]) == false || NativeConnectionsWire.boolean(value["success"]) == false { throw NativeConnectionsFailure("The local service did not confirm the action.") }
        return value
    }
}

enum NativeConnectionAction {
    case accessMode(String), remote(Bool), pair(String), revoke(String), prune
    case capability(node: String, name: String, granted: Bool)
    case importBundle([String: Any]), exportBundle
    case handoff(session: String, type: String, depth: Int)
}
struct NativeConnectionReview: Identifiable {
    let id = UUID()
    let generation: UUID
    let contextRevision:UUID
    let action: NativeConnectionAction
    let title: String
    let explanation: String
}
struct NativeConnectionRow: Identifiable {
    let id: String
    let raw: [String: Any]
    var label: String { raw["label"] as? String ?? raw["name"] as? String ?? raw["node_id"] as? String ?? raw["device_id"] as? String ?? raw["session_id"] as? String ?? "Unnamed device" }
    var nodeID: String? { (raw["node_id"] as? String).flatMap { $0.isEmpty ? nil : $0 } }
}

@MainActor final class NativeConnectionsModel: ObservableObject {
    @Published private(set) var payloads: [String: [String: Any]] = [:]
    @Published private(set) var errors: [String: String] = [:]
    @Published private(set) var loading = false
    @Published private(set) var acting = false
    @Published private(set) var actionError: String?
    @Published private(set) var receipt: String?
    @Published private(set) var pairLink: [String: Any]?
    @Published private(set) var exportData: Data?
    @Published private(set) var capabilities: [[String: Any]]?
    @Published private(set) var capabilityError: String?
    @Published var selectedNode = ""
    private(set) var currentSession: String?
    private var client: NativeConnectionsClient?
    private var generation = UUID()
    private var readGeneration = UUID()
    private var capGeneration = UUID()
    private var operation = UUID()
    private var contextGate = NativeContextActionGate()
    @Published private(set) var contextPolicy = NativeSelectedContextPolicy.legacy
    func setContextPolicy(_ policy:NativeSelectedContextPolicy) {
        if contextGate.update(policy) { contextPolicy = policy }
    }
    private let session: URLSession
    static let paths = ["connected": "/api/devices/connected", "paired": "/api/devices/paired", "tokens": "/api/devices/paired", "mesh": "/api/hardware/mesh", "access": "/api/access/status", "sync": "/api/sync/status", "handoff": "/api/handoff/devices"]
    init(session: URLSession? = nil) { self.session = session ?? NativeConnectionsRedirectGuard.session() }
    var available: Bool { client != nil }
    func configure(baseURL: URL?, sessionID: String?) async {
        generation = UUID(); readGeneration = UUID(); capGeneration = UUID(); operation = UUID()
        client = baseURL.map { NativeConnectionsClient(baseURL: $0, session: session) }; currentSession = sessionID
        payloads = [:]; errors = [:]; loading = false; acting = false; actionError = nil; receipt = nil
        pairLink = nil; exportData = nil; selectedNode = ""; capabilities = nil; capabilityError = nil
        if available { await refresh() }
    }
    func refresh() async {
        guard let client = client else { return }
        readGeneration = UUID(); let read = readGeneration, connection = generation
        loading = true
        defer { if read == readGeneration && connection == generation { loading = false } }
        for key in Self.paths.keys.sorted() {
            guard connection == generation && read == readGeneration else { return }
            do {
                let params = key == "tokens" ? [URLQueryItem(name: "include_unclaimed", value: "true")] : []
                let value = try await client.request(Self.paths[key]!, query: params)
                try validateResource(key, value)
                guard connection == generation && read == readGeneration else { return }
                payloads[key] = value; errors.removeValue(forKey: key)
            } catch { if connection == generation && read == readGeneration { payloads.removeValue(forKey: key); errors[key] = error.localizedDescription } }
        }
    }
    private func validateResource(_ key: String, _ value: [String: Any]) throws {
        let lists: [String: [String]] = ["connected":["devices", "offline"], "paired":["devices"], "tokens":["devices"], "mesh":["nodes", "announced_devices"], "handoff":["devices"]]
        if let keys = lists[key], !keys.allSatisfy({ value[$0] is [[String: Any]] }) { throw NativeConnectionsFailure("This connection section returned an unreadable device list.") }
        if key == "access", value["pairing_mode"] as? String == nil { throw NativeConnectionsFailure("Access mode is unavailable.") }
        if key == "sync", NativeConnectionsWire.boolean(value["enabled"]) == nil { throw NativeConnectionsFailure("Sync status is unavailable.") }
    }
    func rows(_ resource: String, key: String = "devices") -> [NativeConnectionRow] {
        (payloads[resource]?[key] as? [[String: Any]] ?? []).enumerated().map { NativeConnectionRow(id: "\(resource):\(key):\($0.offset)", raw: $0.element) }
    }
    var unclaimed: [NativeConnectionRow] { rows("tokens").filter { NativeConnectionsWire.boolean($0.raw["is_device"]) == false || $0.raw["claimed_at"] == nil || $0.raw["claimed_at"] is NSNull } }
    func loadCapabilities() async {
        capGeneration = UUID(); let cap = capGeneration, connection = generation, node = selectedNode
        capabilities = nil; capabilityError = nil
        guard !node.isEmpty, let client = client else { return }
        do {
            let value = try await client.request("/api/devices/" + NativeConnectionsWire.segment(node) + "/capabilities")
            guard value["node_id"] as? String == node, let caps = value["capabilities"] as? [[String: Any]], caps.allSatisfy({ $0["capability"] is String && NativeConnectionsWire.boolean($0["granted"]) != nil && NativeConnectionsWire.boolean($0["explicit"]) != nil }) else { throw NativeConnectionsFailure("Capability permissions are unreadable. Nothing was changed.") }
            guard cap == capGeneration && connection == generation && selectedNode == node else { return }; capabilities = caps
        } catch { if cap == capGeneration && connection == generation && selectedNode == node { capabilityError = error.localizedDescription } }
    }
    func permits(_ action:NativeConnectionAction)->Bool {
        if case .handoff(let source,_,_) = action { return contextPolicy.taskReady && !contextPolicy.managed && (contextPolicy.sessionID == nil || contextPolicy.sessionID == source) }
        return true
    }
    private func assertContext(_ reviewed:NativeConnectionReview) throws {
        guard contextGate.accepts(reviewed.contextRevision),permits(reviewed.action) else { throw NativeConnectionsFailure("Selected chat, connection or readiness changed. Managed chat handoff is unavailable; review global settings separately.") }
    }
    func review(_ action: NativeConnectionAction) throws -> NativeConnectionReview {
        guard permits(action) else { throw NativeConnectionsFailure("Handoff requires a ready standard chat. Saved-context chats cannot hand off yet; global connection settings remain available.") }
        guard available, !acting, !loading else { throw NativeConnectionsFailure("Wait for the local connection to finish refreshing.") }
        let title: String, explanation: String
        switch action {
        case .accessMode(let mode):
            guard ["localhost", "local"].contains(mode), payloads["access"] != nil else { throw NativeConnectionsFailure("Refresh access status before changing mode.") }
            title = mode == "local" ? "Allow access on the local network?" : "Restrict future access to this Mac?"
            explanation = mode == "local" ? "Persists Same WiFi mode and a network listener. Devices on your network may reach the authenticated service after the required restart. This does not pair a device or grant camera/microphone permissions." : "Persists a loopback-only listener. A restart may be required; this action does not itself stop an existing Tailscale Funnel."
        case .remote(let enable):
            guard payloads["access"] != nil else { throw NativeConnectionsFailure("Refresh access status first.") }
            title = enable ? "Enable public Tailscale Funnel access?" : "Disable Tailscale Funnel?"
            explanation = enable ? "Runs the backend’s Tailscale Funnel command and persists its public URL. This exposes the authenticated service outside your local network; it does not grant a device identity or media permissions. Requires your configured Tailscale account and administrator permissions." : "Runs Funnel off and clears its saved URL. Existing LAN listener access may remain; review refreshed status rather than assuming this Mac is now local-only."
        case .pair(let name):
            guard !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, payloads["access"] != nil else { throw NativeConnectionsFailure("Enter a device label and refresh access status.") }
            title = "Create a PIN-protected pairing link?"; explanation = "Creates an unclaimed, expiring pairing credential for \(name). Anyone with the link and PIN can attempt pairing. Share only with your own device. Creating the link does not mean the device is connected."
        case .revoke(let id):
            guard rows("tokens").contains(where: { $0.raw["device_id"] as? String == id }) else { throw NativeConnectionsFailure("Refresh the pairing inventory before revoking this record.") }
            title = "Revoke this device’s pairing?"; explanation = "Revokes pairing record \(id) and clears its stored per-node capability answers. It does not erase conversation/memory history or claim to disconnect every existing socket."
        case .prune:
            guard payloads["tokens"] != nil, !unclaimed.isEmpty else { throw NativeConnectionsFailure("No confirmed unclaimed codes are available to clear.") }
            title = "Revoke all unclaimed pairing codes?"; explanation = "Revokes all currently unclaimed tokens, including newly created codes. Already claimed devices are excluded by the backend."
        case .capability(let node, let name, let granted):
            guard selectedNode == node, let cap = capabilities?.first(where: { $0["capability"] as? String == name }), NativeConnectionsWire.boolean(cap["granted"]) != granted else { throw NativeConnectionsFailure("Refresh this node’s capabilities before changing its permission.") }
            title = granted ? "Allow \(name) on this node?" : "Deny \(name) on this node?"
            explanation = "Changes only \(name) for node \(node). Global hardware policy still applies. The brain enforces the decision and attempts to notify the node; this is not an OS media/Bluetooth permission grant."
        case .importBundle(let bundle):
            _ = try NativeConnectionsWire.bundle(JSONSerialization.data(withJSONObject: bundle))
            guard NativeConnectionsWire.boolean(payloads["sync"]?["enabled"]) == true else { throw NativeConnectionsFailure("The sync engine must be enabled before importing.") }
            let ops = bundle["operations"] as! [[String: Any]], deletes = ops.filter { $0["op_type"] as? String == "delete" }.count
            let tables = Set(ops.compactMap { $0["table"] as? String }).sorted().joined(separator: ", ")
            title = "Merge this sync bundle?"; explanation = "Source \(NativeConnectionsWire.text(bundle["node_id"])): \(ops.count) operations, including \(deletes) deletions, across \(tables.isEmpty ? "no tables" : tables). This trusted-owner import can update or delete local records and is not an automatic sharing grant. It has no one-click undo; export your current sync bundle first. Only import a bundle from a brain you own."
        case .exportBundle:
            guard NativeConnectionsWire.boolean(payloads["sync"]?["enabled"]) == true else { throw NativeConnectionsFailure("The sync engine must be enabled before exporting.") }
            title = "Export private sync state?"; explanation = "Exports this brain’s operation log and vector clock, including private operations and their scopes. Store the file securely and share only between brains you own. It is not a complete backup of all application data."
        case .handoff(let source, let type, let depth):
            let devices = rows("handoff")
            guard currentSession == source, !source.isEmpty, devices.contains(where: { $0.raw["session_id"] as? String == source }), ["phone", "desktop", "glasses", "wristband", "channel", "browser_node"].contains(type), (1...100).contains(depth), devices.contains(where: { $0.raw["node_type"] as? String == type && $0.raw["session_id"] as? String != source }) else { throw NativeConnectionsFailure("Refresh handoff targets and choose another connected device class for the active thread.") }
            title = "Hand off this thread’s context?"; explanation = "Copies up to \(depth) working-memory messages from session \(source) to the first connected \(type) session chosen by the backend. You cannot select an exact device within that class. It may replace the receiving session’s working context; pending tools, saved transcript and hardware state are not promised to transfer. If the class disconnects before execution, the backend may queue this request."
        }
        return NativeConnectionReview(generation: generation, contextRevision: contextGate.revision, action: action, title: title, explanation: explanation + "\nScope: handoff uses the exact reviewed source; other actions affect global connections/settings, not selected-chat saved context.")
    }
    func perform(_ reviewed: NativeConnectionReview) async -> Bool {
        guard reviewed.generation == generation,contextGate.accepts(reviewed.contextRevision), let client = client else { actionError = "The connection changed. Review this action again."; return false }
        do { try assertContext(reviewed); _ = try review(reviewed.action) } catch { actionError = error.localizedDescription; return false }
        let connection = generation; operation = UUID(); let op = operation
        acting = true; actionError = nil; receipt = nil; exportData = nil
        defer { if connection == generation && op == operation { acting = false } }
        do {
            let response: [String: Any]
            var text: String
            try assertContext(reviewed)
            switch reviewed.action {
            case .accessMode(let mode):
                response = try await client.request("/api/access/mode", method: "POST", body: ["mode":mode])
                guard NativeConnectionsWire.boolean(response["ok"]) == true, response["mode"] as? String == mode, let restart = NativeConnectionsWire.boolean(response["restart_required"]) else { throw NativeConnectionsFailure("Access mode change was not confirmed.") }
                text = restart ? "Mode saved. Restart is required before the listener changes; current access may remain." : "Access mode saved; the backend reports no listener restart required."
            case .remote(let enable):
                response = try await client.request(enable ? "/api/access/remote-up" : "/api/access/remote-down", method: "POST")
                guard NativeConnectionsWire.boolean(response["ok"]) == true else { throw NativeConnectionsFailure("Remote access change was not confirmed.") }
                if enable { guard response["pairing_mode"] as? String == "remote", let url = response["remote_url"] as? String, URL(string:url)?.scheme == "https" else { throw NativeConnectionsFailure("A public Funnel address was not confirmed.") }; text = "Backend reports Funnel enabled at \(url). This does not prove a device can reach it." }
                else { text = "Backend reports Funnel disabled. Review refreshed access status; LAN access may remain." }
            case .pair(let name):
                response = try await client.request("/api/devices/pair/url", query:[URLQueryItem(name:"name",value:name),URLQueryItem(name:"pin",value:"true")])
                guard let url = response["url"] as? String, ["http", "https"].contains(URL(string:url)?.scheme ?? ""), response["device_id"] is String, NativeConnectionsWire.boolean(response["pin_required"]) == true, let pin = response["pin"] as? String, pin.count == 4, pin.allSatisfy({ $0.isASCII && $0.isNumber }), let expires = NativeConnectionsWire.integer(response["expires"]), expires > Int(Date().timeIntervalSince1970) else { throw NativeConnectionsFailure("The service did not return a complete unexpired PIN-protected link.") }
                text = "Pairing code created. The device is not paired until it completes the claim."
            case .revoke(let id):
                response = try await client.request("/api/devices/" + NativeConnectionsWire.segment(id), method:"DELETE")
                guard NativeConnectionsWire.boolean(response["ok"]) == true else { throw NativeConnectionsFailure("Pairing revocation was not confirmed.") }; text = "Pairing record revoked."
            case .prune:
                response = try await client.request("/api/devices/pair/prune",method:"POST",body:["older_than_seconds":0])
                guard NativeConnectionsWire.boolean(response["success"]) == true else { throw NativeConnectionsFailure("Pairing-code cleanup was not confirmed.") }; text = "Backend confirmed unclaimed-code cleanup."
            case .capability(let node, let name, let granted):
                response = try await client.request("/api/devices/" + NativeConnectionsWire.segment(node) + "/capabilities",method:"POST",body:["capability":name,"granted":granted])
                guard NativeConnectionsWire.boolean(response["ok"]) == true, response["node_id"] as? String == node, NativeConnectionsWire.boolean(response["granted"]) == granted, (response["changed"] as? [String])?.contains(name) == true else { throw NativeConnectionsFailure("This node’s capability change was not confirmed.") }
                text = "Capability permission saved. " + (NativeConnectionsWire.boolean(response["notified"]) == true ? "Node notification sent." : "Node notification was not confirmed; brain-side enforcement still applies.")
            case .importBundle(let bundle):
                response = try await client.request("/api/sync/import",method:"POST",body:bundle)
                guard let applied = NativeConnectionsWire.integer(response["applied"]), applied <= (bundle["operations"] as? [Any] ?? []).count else { throw NativeConnectionsFailure("The import did not return a valid applied-operation count.") }; text = "Backend applied \(applied) operations. Other operations may already exist or have been refused."
            case .exportBundle:
                response = try await client.request("/api/sync/export")
                _ = try NativeConnectionsWire.bundle(JSONSerialization.data(withJSONObject:response)); text = "Sync state fetched. Choose a secure location in the save dialog."
            case .handoff(let source, let type, let depth):
                response = try await client.request("/api/handoff",method:"POST",body:["from_session":source,"to_node_type":type,"history_depth":depth])
                guard NativeConnectionsWire.boolean(response["ok"]) == true, NativeConnectionsWire.boolean(response["success"]) == true, response["from_session_id"] as? String == source, response["to_node_type"] as? String == type, let pending = NativeConnectionsWire.boolean(response["pending"]), let transferred = NativeConnectionsWire.integer(response["messages_transferred"]) else { throw NativeConnectionsFailure("The handoff response did not confirm this source and device class.") }
                if pending { text = "Handoff queued for the next \(type) session. No completed transfer is confirmed." }
                else { guard let target = response["to_session_id"] as? String, !target.isEmpty, target != source else { throw NativeConnectionsFailure("No receiving session was confirmed.") }; text = "Backend reports \(transferred) working-memory messages copied to \(target). Receiving-device behavior is not verified here." }
            }
            guard connection == generation && op == operation else { return false }
            try assertContext(reviewed)
            if case .pair = reviewed.action { pairLink = response }
            if case .revoke(let id) = reviewed.action, pairLink?["device_id"] as? String == id { pairLink = nil }
            if case .prune = reviewed.action { pairLink = nil }
            if case .exportBundle = reviewed.action { exportData = try JSONSerialization.data(withJSONObject:response,options:[.prettyPrinted,.sortedKeys]) }
            await refresh()
            if case .capability = reviewed.action { await loadCapabilities() }
            guard connection == generation && op == operation else { return false }
            try assertContext(reviewed)
            receipt = text; return true
        } catch { if connection == generation && op == operation { actionError = error.localizedDescription }; return false }
    }
}

struct NativeConnectionsExport: FileDocument {
    static var readableContentTypes: [UTType] { [.json] }
    var data: Data
    init(data: Data) { self.data = data }
    init(configuration: ReadConfiguration) throws { data = configuration.file.regularFileContents ?? Data() }
    func fileWrapper(configuration: WriteConfiguration) throws -> FileWrapper { FileWrapper(regularFileWithContents:data) }
}

struct NativeConnectionsFeatureView: View {
    let baseURL: URL?
    let sessionID: String?
    let contextPolicy:NativeSelectedContextPolicy
    @StateObject private var model = NativeConnectionsModel()
    @State private var tab = "Devices"
    @State private var review: NativeConnectionReview?
    @State private var localError: String?
    @State private var pairName = "My device"
    @State private var importOpen = false
    @State private var exportOpen = false
    @State private var exportDocument: NativeConnectionsExport?
    init(baseURL: URL?, sessionID: String? = nil, contextPolicy:NativeSelectedContextPolicy = .legacy) { self.baseURL = baseURL; self.sessionID = sessionID;self.contextPolicy = contextPolicy }
    private var busy: Bool { model.loading || model.acting }
    var body: some View {
        VStack(alignment:.leading,spacing:16) {
            HStack {
                VStack(alignment:.leading,spacing:4) { Text("Connections").font(.largeTitle.bold()); Text("Your devices, access, memory sync and session handoff.").foregroundStyle(.secondary) }
                Spacer(); Button("Refresh") { Task { await model.refresh() } }.disabled(busy || baseURL == nil)
            }
            Picker("Connection section",selection:$tab) { ForEach(["Devices","Access","Sync","Handoff"],id:\.self) { Text($0).tag($0) } }.pickerStyle(.segmented).disabled(model.acting)
            Text(contextPolicy.message).font(.caption).foregroundStyle(.secondary)
            if baseURL == nil { Text("Connections will be available when the local service is ready.").foregroundStyle(.secondary); Spacer() }
            else {
                if let error = localError ?? model.actionError { HStack(alignment: .top) { Image(systemName:"exclamationmark.triangle").accessibilityHidden(true); NativeSelectableText(error).foregroundStyle(.red) }.foregroundStyle(.red).accessibilityElement(children: .contain) }
                if let receipt = model.receipt { NativeSelectableText(receipt).foregroundStyle(.secondary) }
                if model.loading { ProgressView("Refreshing connection status…") }
                ScrollView { VStack(alignment:.leading,spacing:18) { if tab == "Devices" { devices }; if tab == "Access" { access }; if tab == "Sync" { sync }; if tab == "Handoff" { handoff } }.frame(maxWidth:.infinity,alignment:.leading) }
            }
        }.padding(24)
        .task(id:(baseURL?.absoluteString ?? "") + "|" + (sessionID ?? "")) { review = nil; exportOpen = false; importOpen = false; exportDocument = nil; localError = nil; model.setContextPolicy(contextPolicy);await model.configure(baseURL:baseURL,sessionID:sessionID) }
        .onChange(of:model.selectedNode) { _ in Task { await model.loadCapabilities() } }
        .onChange(of:contextPolicy) { policy in review = nil;model.setContextPolicy(policy) }
        .sheet(item:$review) { reviewed in
            VStack(alignment:.leading,spacing:16) { Text(reviewed.title).font(.title2.bold()); NativeSelectableText(reviewed.explanation); if let error = model.actionError { Text(error).foregroundStyle(.red) }; HStack { Spacer(); Button("Cancel") { review = nil }.disabled(model.acting).keyboardShortcut(.cancelAction); Button(model.acting ? "Applying…" : "Confirm") { Task { model.setContextPolicy(contextPolicy);if await model.perform(reviewed) { if let data = model.exportData { exportDocument = NativeConnectionsExport(data:data); exportOpen = true }; review = nil } } }.disabled(busy).keyboardShortcut(.defaultAction) } }.padding(24).frame(width:560).interactiveDismissDisabled(model.acting)
        }
        .fileImporter(isPresented:$importOpen,allowedContentTypes:[.json]) { result in
            do { let url = try result.get(); let scoped = url.startAccessingSecurityScopedResource(); defer { if scoped { url.stopAccessingSecurityScopedResource() } }; let values = try url.resourceValues(forKeys:[.fileSizeKey]); guard (values.fileSize ?? 0) <= 10 * 1024 * 1024 else { throw NativeConnectionsFailure("Sync bundle exceeds 10 MB.") }; let bundle = try NativeConnectionsWire.bundle(Data(contentsOf:url)); requestReview(.importBundle(bundle)) } catch { localError = error.localizedDescription }
        }
        .fileExporter(isPresented:$exportOpen,document:exportDocument,contentType:.json,defaultFilename:"FERAL-private-sync-state") { result in if case .failure(let error) = result { localError = error.localizedDescription } }
    }
    private func requestReview(_ action: NativeConnectionAction) { do { localError = nil;model.setContextPolicy(contextPolicy); review = try model.review(action) } catch { localError = error.localizedDescription } }
    private func card<Content:View>(_ title:String,@ViewBuilder content:() -> Content) -> some View { VStack(alignment:.leading,spacing:12) { Text(title).font(.headline); content() }.padding(16).frame(maxWidth:.infinity,alignment:.leading).background(Color.secondary.opacity(0.07),in:RoundedRectangle(cornerRadius:12)) }
    @ViewBuilder private func failure(_ resource:String) -> some View { if let error = model.errors[resource] { NativeSelectableText("Unavailable: " + error).foregroundStyle(.red) } }
    private func field(_ label:String,_ value:String) -> some View { HStack(alignment:.top) { Text(label).foregroundStyle(.secondary).frame(width:150,alignment:.leading); NativeSelectableText(value); Spacer() } }
    @ViewBuilder private var devices: some View {
        card("Connected and offline") {
            failure("connected")
            let rows = model.rows("connected") + model.rows("connected",key:"offline")
            if model.payloads["connected"] != nil && rows.isEmpty { Text("No nodes were returned by the service.").foregroundStyle(.secondary) }
            ForEach(rows) { row in
                VStack(alignment:.leading,spacing:6) {
                    HStack { Text(row.label).font(.headline); Text(row.raw["status"] as? String ?? "Status unavailable").foregroundStyle(.secondary); Spacer(); if let id = row.nodeID { Button("Permissions") { model.selectedNode = id }.disabled(busy) } }
                    Text("\(NativeConnectionsWire.text(row.raw["type"])) · \(NativeConnectionsWire.text(row.raw["platform"]))").font(.caption).foregroundStyle(.secondary)
                    if let caps = row.raw["capabilities"] as? [String] { Text(caps.joined(separator:", ")).font(.caption) }
                    if let subs = row.raw["subdevices"] as? [[String:Any]] { ForEach(Array(subs.enumerated()),id:\.offset) { _, sub in Text("Peripheral: \(NativeConnectionsWire.text(sub["name"] ?? sub["capability"])) · \(NativeConnectionsWire.text(sub["status"]))").font(.caption).foregroundStyle(.secondary) } }
                }.padding(.vertical,6)
            }
        }
        if !model.selectedNode.isEmpty {
            card("Permissions for " + model.selectedNode) {
                Text("Declared capabilities default to allowed unless explicitly changed. Global hardware policy still applies.").font(.caption).foregroundStyle(.secondary)
                if let error = model.capabilityError { Text(error).foregroundStyle(.red) }
                if model.capabilities == nil && model.capabilityError == nil { ProgressView("Loading permissions…") }
                ForEach(Array((model.capabilities ?? []).enumerated()),id:\.offset) { _, cap in
                    let name = cap["capability"] as? String ?? "Unknown", granted = NativeConnectionsWire.boolean(cap["granted"]) == true
                    HStack { VStack(alignment:.leading) { Text(name); Text("\(NativeConnectionsWire.text(cap["tier"])) · \(NativeConnectionsWire.boolean(cap["explicit"]) == true ? "Explicit decision" : "Default decision")").font(.caption).foregroundStyle(.secondary) }; Spacer(); Text(granted ? "Allowed" : "Denied"); Button(granted ? "Deny…" : "Allow…") { requestReview(.capability(node:model.selectedNode,name:name,granted:!granted)) }.disabled(busy) }
                }
            }
        }
        card("Paired devices") {
            failure("paired"); failure("tokens")
            if model.payloads["paired"] != nil && model.rows("paired").isEmpty { Text("No claimed pairing records.").foregroundStyle(.secondary) }
            ForEach(model.rows("paired")) { row in HStack { VStack(alignment:.leading) { Text(row.label); Text(row.raw["explain"] as? String ?? "Claimed pairing record").font(.caption).foregroundStyle(.secondary) }; Spacer(); if let id = row.raw["device_id"] as? String { Button("Revoke…",role:.destructive) { requestReview(.revoke(id)) }.disabled(busy || model.payloads["tokens"] == nil) } } }
            Text("\(model.unclaimed.count) unclaimed pairing codes; these are not connected devices.").font(.caption).foregroundStyle(.secondary)
            Button("Clear unclaimed codes…",role:.destructive) { requestReview(.prune) }.disabled(busy || model.unclaimed.isEmpty)
        }
        card("Pair another device") {
            TextField("Device label",text:$pairName).textFieldStyle(.roundedBorder)
            Button("Create link and PIN…") { requestReview(.pair(pairName)) }.disabled(busy || model.payloads["access"] == nil)
            Text("The receiving device completes its own identity and media-consent flow. This Mac does not automatically enable its sensors.").font(.caption).foregroundStyle(.secondary)
            if let pair = model.pairLink, let url = pair["url"] as? String {
                Text("Sensitive pairing credential—share only with your own device.").foregroundStyle(.orange)
                if let image = qr(url) { Image(nsImage:image).interpolation(.none).resizable().frame(width:180,height:180).accessibilityLabel("PIN-protected pairing QR code") }
                field("Pairing link",url); field("PIN",NativeConnectionsWire.text(pair["pin"]))
                if let expires = NativeConnectionsWire.integer(pair["expires"]) { field("Expires",Date(timeIntervalSince1970:Double(expires)).formatted()) }
                if let id = pair["device_id"] as? String { Button("Revoke this code…",role:.destructive) { requestReview(.revoke(id)) }.disabled(busy) }
            }
        }
        card("Hardware mesh") {
            failure("mesh")
            field("Registered nodes",model.payloads["mesh"] == nil ? "Unavailable" : String(model.rows("mesh",key:"nodes").count))
            ForEach(model.rows("mesh",key:"announced_devices")) { row in VStack(alignment:.leading) { Text(row.label); Text("Observed by \(NativeConnectionsWire.text(row.raw["scanner_node_id"])) · \(NativeConnectionsWire.text(row.raw["device_kind"])) · RSSI \(NativeConnectionsWire.text(row.raw["rssi_dbm"])) dBm").font(.caption).foregroundStyle(.secondary) } }
            Text("Discovery does not establish pairing or working hardware commands. Inspect declared methods and reviewed passive reads in Device methods. Native BLE scanning remains unavailable.").font(.caption).foregroundStyle(.secondary)
        }
    }
    @ViewBuilder private var access: some View {
        card("Access to this service") {
            failure("access")
            if let data = model.payloads["access"] {
                field("Saved mode",NativeConnectionsWire.text(data["pairing_mode"]))
                field("Remote address",(data["remote_url"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "None reported")
                let ts = data["tailscale"] as? [String:Any] ?? [:], funnel = data["funnel"] as? [String:Any] ?? [:]
                field("Tailscale installed",truth(ts["installed"])); field("Daemon running",truth(ts["running"])); field("Signed in",truth(ts["logged_in"])); field("Funnel active",truth(funnel["active"]))
                if let error = ts["error"] as? String, !error.isEmpty { Text(error).foregroundStyle(.orange) }
                HStack { Button("This Mac only…") { requestReview(.accessMode("localhost")) }; Button("Same WiFi…") { requestReview(.accessMode("local")) } }.disabled(busy)
                HStack { Button("Enable Tailscale Funnel…") { requestReview(.remote(true)) }; Button("Disable Funnel…") { requestReview(.remote(false)) } }.disabled(busy)
                Text("Saved listener changes may require restart. No restart, account login, firewall change or remote exposure is performed automatically.").font(.caption).foregroundStyle(.secondary)
            }
        }
    }
    @ViewBuilder private var sync: some View {
        card("Memory synchronization") {
            failure("sync")
            if let data = model.payloads["sync"] {
                field("Engine enabled",truth(data["enabled"])); field("Engine running",truth(data["running"])); field("Node ID",NativeConnectionsWire.text(data["node_id"])); field("Peers",NativeConnectionsWire.text(data["peer_count"])); field("Logged operations",NativeConnectionsWire.text(data["wal_entries"])); field("Peer identity",NativeConnectionsWire.text(data["identity_mode"]))
                if let note = data["identity_note"] as? String { Text(note).font(.caption).foregroundStyle(.secondary) }
                if let peers = data["peers"] as? [String] { ForEach(peers,id:\.self) { Text($0).font(.caption) } }
                HStack { Button("Export private sync bundle…") { requestReview(.exportBundle) }; Button("Import and review bundle…") { importOpen = true } }.disabled(busy || NativeConnectionsWire.boolean(data["enabled"]) != true)
                Text("Bundles contain operation history and scopes, including private data. They are for your own brains, not public sharing or a complete app backup. Peer enrollment, scope grants and sync scheduling remain separate controls.").font(.caption).foregroundStyle(.secondary)
            }
        }
    }
    @ViewBuilder private var handoff: some View {
        card("Continue on another device class") {
            failure("handoff")
            field("Source thread",sessionID ?? "No active thread")
            Text("The current API selects the first live session of a device class. It cannot address an exact device. Review before replacing that receiver’s working context.").font(.caption).foregroundStyle(.secondary)
            let targets = model.rows("handoff").filter { $0.raw["session_id"] as? String != sessionID }
            if model.payloads["handoff"] != nil && targets.isEmpty { Text("No other connected sessions were returned.").foregroundStyle(.secondary) }
            ForEach(targets) { row in
                let type = row.raw["node_type"] as? String ?? "Unknown"
                HStack { VStack(alignment:.leading) { Text(type.capitalized); Text("Session \(NativeConnectionsWire.text(row.raw["session_id"])) · Node \(NativeConnectionsWire.text(row.raw["node_id"]))").font(.caption).foregroundStyle(.secondary) }; Spacer(); Button("Review \(type) handoff…") { requestReview(.handoff(session:sessionID ?? "",type:type,depth:20)) }.disabled(busy || sessionID?.isEmpty != false || !contextPolicy.taskReady || contextPolicy.managed) }
            }
        }
    }
    private func truth(_ value:Any?) -> String { NativeConnectionsWire.boolean(value).map { $0 ? "Yes" : "No" } ?? "Unavailable" }
    private func qr(_ url:String) -> NSImage? { let filter = CIFilter.qrCodeGenerator(); filter.message = Data(url.utf8); guard let output = filter.outputImage?.transformed(by:CGAffineTransform(scaleX:8,y:8)), let image = CIContext().createCGImage(output,from:output.extent) else { return nil }; return NSImage(cgImage:image,size:NSSize(width:image.width,height:image.height)) }
}
