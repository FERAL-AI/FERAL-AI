import SwiftUI
import Foundation
import CoreFoundation

struct NativeHardwareFailure: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}
struct NativeHardwareReview: Identifiable {
    let id = UUID()
    let generation: UUID
    let origin: String
    let deviceID: String
    let command: String
    let manifest: Data
    let node: Data
    let params: Data
    let timeout: Double
    let explanation: String
    var summary: NativeReviewSummary {
        NativeReviewSummary(action: "Read this sensor?", effect: "Collects the selected reading and returns its report to your local agent.",
            materialScope: ["May include private sensor or health data. The device's declaration and reported result are not physical verification.", "Uses shared local operator authority. No automatic retry, physical cancellation or cross-device sync is promised."],
            targets: [.init(label: "Device / method", value: deviceID + " / " + command),
                      .init(label: "Parameters", value: String(decoding: params, as: UTF8.self)),
                      .init(label: "Timeout", value: String(timeout) + " seconds"),
                      .init(label: "Local service", value: origin)], details: explanation)
    }
}
struct NativeHardwareServerReview {
    let reviewID: String
    let owner: String
    let nodeGeneration: String
    let expiresAt: Double
    let local: NativeHardwareReview
}
struct NativeHardwareReceipt {
    let state: String
    let explanation: String
    let commandID: String?
    let echoedScope: Bool
    let physicalOutcomeVerified = false
    let raw: [String: Any]
}
enum NativeHardwareWire {
    static func id(_ value: String) -> Bool { value.range(of: "^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$", options: .regularExpression) == (value.startIndex..<value.endIndex) }
    static func segment(_ value: String) -> String { value.addingPercentEncoding(withAllowedCharacters: CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")) ?? "" }
    static func bool(_ value: Any?) -> Bool? { guard let n = value as? NSNumber, CFGetTypeID(n) == CFBooleanGetTypeID() else { return nil }; return n.boolValue }
    static func number(_ value: Any?) -> Double? { guard let n = value as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID(), n.doubleValue.isFinite else { return nil }; return n.doubleValue }
    static func freeze(_ value: Any, budget: Int = 65_536) throws -> Data {
        guard JSONSerialization.isValidJSONObject(value), let data = try? JSONSerialization.data(withJSONObject: value, options: .sortedKeys), data.count <= budget else { throw NativeHardwareFailure("The declared hardware data exceeded its bounds or was not valid JSON.") }; return data
    }
    static func pretty(_ value: Any) -> String { guard let data = try? JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys]) else { return "Unavailable" }; return String(decoding: data, as: UTF8.self) }
    static func manifest(_ value: [String: Any], deviceID: String) throws -> [String: Any] {
        guard id(deviceID), value["error"] == nil, value["device_id"] as? String == deviceID,
              let caps = value["capabilities"] as? [[String: Any]], caps.count <= 128 else { throw NativeHardwareFailure("The registered device detail was unavailable or belonged to a different device.") }
        _ = try freeze(value)
        var seen = Set<String>()
        for cap in caps { guard let command = cap["id"] as? String, id(command), seen.insert(command).inserted else { throw NativeHardwareFailure("Device methods were malformed or duplicated.") } }
        return value
    }
    static func liveNode(_ value: [String: Any], deviceID: String) throws -> [String: Any]? {
        guard value["error"] == nil, let nodes = value["nodes"] as? [[String: Any]], nodes.count <= 512 else { throw NativeHardwareFailure("Live mesh availability was unknown.") }
        var seen = Set<String>()
        for row in nodes { guard let node = row["node_id"] as? String, id(node), seen.insert(node).inserted else { throw NativeHardwareFailure("Mesh identities were malformed or duplicated.") } }
        guard let node = nodes.first(where: { $0["node_id"] as? String == deviceID }) else { return nil }
        guard bool(node["online"]) == true, let registered = number(node["registered_at"]), registered > 0 else { throw NativeHardwareFailure("The mesh node did not establish live registration identity.") }
        _ = try freeze(node); return node
    }
    static func capability(_ manifest: [String: Any], command: String, params: [String: Any]) throws -> [String: Any] {
        guard manifest["connection_type"] as? String == "websocket", let caps = manifest["capabilities"] as? [[String: Any]],
              let cap = caps.first(where: { $0["id"] as? String == command }), cap["category"] as? String == "sensor",
              cap["permission_tier"] as? String == "passive", bool(cap["requires_confirmation"]) == false,
              cap["action_type"] == nil || cap["action_type"] is NSNull || cap["action_type"] as? String == "read",
              let fields = cap["parameters"] as? [[String: Any]], fields.count <= 32 else { throw NativeHardwareFailure("Only declared passive sensor reads without extra confirmation are supported. Other methods remain inspection only.") }
        _ = try freeze(params, budget: 16_384)
        var names = Set<String>()
        for field in fields {
            guard let name = field["name"] as? String, name.range(of: "^[A-Za-z_][A-Za-z0-9_]{0,63}$", options: .regularExpression) == (name.startIndex..<name.endIndex), names.insert(name).inserted,
                  let type = field["type"] as? String, ["string", "integer", "int", "number", "float", "boolean", "bool"].contains(type),
                  field["required"] == nil || bool(field["required"]) != nil else { throw NativeHardwareFailure("The parameter schema is unsupported; no invocation was enabled.") }
            guard Set(field.keys).isSubset(of: ["name", "type", "required", "description", "default", "enum", "minimum", "maximum"]) else { throw NativeHardwareFailure("The method has an unsupported parameter constraint and remains inspection only.") }
            for key in ["minimum", "maximum"] {
                if field[key] != nil { guard number(field[key]) != nil else { throw NativeHardwareFailure("A declared parameter bound was malformed.") } }
            }
            if let allowed = field["enum"] { guard let options = allowed as? [Any], !options.isEmpty, options.count <= 64 else { throw NativeHardwareFailure("A declared parameter enumeration was malformed.") }; _ = try freeze(options) }
            guard let value = params[name] else { if bool(field["required"]) == true { throw NativeHardwareFailure("A required declared parameter is missing.") }; continue }
            switch type {
            case "string": guard let text = value as? String, text.utf8.count <= 4096 else { throw NativeHardwareFailure("The sensor parameter must be a bounded string.") }
            case "boolean", "bool": guard bool(value) != nil else { throw NativeHardwareFailure("The sensor parameter must be a strict Boolean.") }
            default:
                guard let n = number(value), abs(n) <= 9_007_199_254_740_991, !["integer", "int"].contains(type) || n.rounded(.towardZero) == n else { throw NativeHardwareFailure("The sensor parameter must be a finite declared number.") }
                for key in ["minimum"] { if let lower = number(field[key]), n < lower { throw NativeHardwareFailure("The sensor parameter is below its declared minimum.") } }
                for key in ["maximum"] { if let upper = number(field[key]), n > upper { throw NativeHardwareFailure("The sensor parameter exceeds its declared maximum.") } }
            }
            if let allowed = field["enum"] {
                guard let options = allowed as? [Any], !options.isEmpty, options.count <= 64,
                      let encoded = try? freeze([value]), options.contains(where: { (try? freeze([$0])) == encoded }) else { throw NativeHardwareFailure("The sensor parameter is outside its declared enumeration.") }
            }
        }
        guard Set(params.keys).isSubset(of: names) else { throw NativeHardwareFailure("Undeclared sensor parameters are not sent.") }
        return cap
    }
    static func uuid(_ value: Any?) -> String? {
        guard let text = value as? String, text.count == 36, let id = UUID(uuidString: text), id.uuidString.lowercased() == text else { return nil }; return text
    }
    static func serverReview(_ body: [String: Any], local: NativeHardwareReview, primaryID: String, now: Double = Date().timeIntervalSince1970) throws -> NativeHardwareServerReview {
        guard number(body["contract_version"]) == 1, bool(body["separate_authorization_required"]) == false,
              let value = body["review"] as? [String: Any], let reviewID = uuid(value["review_id"]),
              let owner = value["owner"] as? String, owner == "operator:" + primaryID,
              value["node_id"] as? String == local.deviceID, value["command"] as? String == local.command,
              value["permission_tier"] as? String == "passive", bool(value["requires_confirmation"]) == false,
              number(value["timeout"]) == local.timeout, let nodeGeneration = uuid(value["generation"]),
              let node = try JSONSerialization.jsonObject(with: local.node) as? [String: Any], node["generation"] as? String == nodeGeneration,
              let created = number(value["created_at"]), let expires = number(value["expires_at"]), created > 0, created <= now + 5,
              expires > now, expires > created, expires - created <= 300,
              let manifestJSON = value["manifest_json"] as? String, let manifestData = manifestJSON.data(using: .utf8), manifestData.count <= 65_536,
              let paramsJSON = value["params_json"] as? String, let paramsData = paramsJSON.data(using: .utf8), paramsData.count <= 16_384,
              let manifest = try JSONSerialization.jsonObject(with: manifestData) as? [String: Any],
              let params = try JSONSerialization.jsonObject(with: paramsData) as? [String: Any],
              try freeze(manifest) == local.manifest, try freeze(params) == local.params else {
            throw NativeHardwareFailure("The server review did not match this exact passive read, connection generation, operator scope, schema or expiry. No dispatch was sent.")
        }
        return NativeHardwareServerReview(reviewID: reviewID, owner: owner, nodeGeneration: nodeGeneration, expiresAt: expires, local: local)
    }
    static func receipt(_ body: [String: Any], review: NativeHardwareServerReview, expectedCommandID: String? = nil) throws -> NativeHardwareReceipt {
        _ = try freeze(body)
        guard number(body["contract_version"]) == 1, body["owner"] as? String == review.owner,
              body["review_id"] as? String == review.reviewID, body["generation"] as? String == review.nodeGeneration,
              body["node_id"] as? String == review.local.deviceID, body["command"] as? String == review.local.command,
              body["permission_tier"] as? String == "passive", bool(body["requires_confirmation"]) == false,
              let params = body["params"], try freeze(params) == review.local.params,
              let commandID = uuid(body["command_id"]), expectedCommandID == nil || expectedCommandID == commandID,
              bool(body["physical_outcome_verified"]) == false, bool(body["automatic_retry"]) == false,
              body["effect"] as? String == "unknown", let acknowledged = bool(body["acknowledged"]),
              let rawState = body["state"] as? String else { throw NativeHardwareFailure("The command ledger receipt did not establish exact reviewed command ownership. Outcome remains unknown.") }
        guard let privateReport = body["device_report"] else { throw NativeHardwareFailure("The private device report availability was not established.") }
        if !(privateReport is NSNull) {
            guard let object = privateReport as? [String: Any] else { throw NativeHardwareFailure("The private device report was not a supported object.") }
            _ = try freeze(object, budget: 65_536)
            for (key, expected) in [("command_id", commandID), ("node_id", review.local.deviceID), ("command", review.local.command)] {
                if let echo = object[key] { guard echo as? String == expected else { throw NativeHardwareFailure("The private device report echoed a foreign command.") } }
            }
            if let params = object["params"] { guard try freeze(params) == review.local.params else { throw NativeHardwareFailure("The private device report echoed changed parameters.") } }
            guard bool(object["success"]) == bool(body["device_reported_success"]) else { throw NativeHardwareFailure("The private device report did not match the reported verdict.") }
        }
        let queued = bool(body["queued"]), reported = bool(body["device_reported_success"])
        let hasNoReport = body["device_reported_success"] is NSNull
        let state: String, text: String
        switch rawState {
        case "submitted":
            guard queued == true, !acknowledged, hasNoReport else { throw NativeHardwareFailure("Malformed queued command receipt.") }
            state = "queued_unverified"; text = "Queued on the captured connection. Device acknowledgement and physical outcome remain unknown."
        case "acked", "running":
            guard queued == true, acknowledged, hasNoReport else { throw NativeHardwareFailure("Malformed acknowledgement receipt.") }
            state = "acknowledged_unverified"; text = "The device acknowledged this exact command. Its measured or physical outcome remains unverified."
        case "succeeded":
            guard queued == true, reported == true else { throw NativeHardwareFailure("Success lacked an exact device report.") }
            state = "device_reported_success_unverified"; text = "The device reports success for this exact command. This does not verify the physical or measured outcome."
        case "failed", "timed_out", "cancelled":
            guard queued == true || body["queued"] is NSNull, reported == false || hasNoReport else { throw NativeHardwareFailure("Malformed failure or timeout receipt.") }
            state = "device_reported_failure_effect_unknown"; text = "The command ledger reports failure, timeout or transport stop. Earlier physical effects remain unknown. No automatic retry occurs."
        default: throw NativeHardwareFailure("Unknown command ledger state. Outcome remains unknown.")
        }
        guard (reported != nil) == !(privateReport is NSNull) else { throw NativeHardwareFailure("The private device report availability contradicted its verdict.") }
        return NativeHardwareReceipt(state: state, explanation: text + " Operator scope is shared by authenticated local clients; it is not a separate browser identity.", commandID: commandID, echoedScope: true, raw: body)
    }
}
private final class NativeHardwareRedirectGuard: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
}
@MainActor final class NativeHardwareModel: ObservableObject {
    typealias Transport = (URLRequest) async throws -> (Data, HTTPURLResponse)
    @Published private(set) var devices: [[String: Any]]?
    @Published private(set) var detail: [String: Any]?
    @Published private(set) var node: [String: Any]?
    @Published private(set) var context: String?
    @Published private(set) var stats: [String: Any]?
    @Published private(set) var receipt: NativeHardwareReceipt?
    @Published private(set) var error: String?
    @Published private(set) var busy = false
    private let transport: Transport
    private var baseURL: URL?
    private var generation = UUID()
    private var used = Set<UUID>()
    private var commandReview: NativeHardwareServerReview?
    private var commandGeneration: UUID?
    init(transport: Transport? = nil) {
        if let transport { self.transport = transport }
        else {
            let config = URLSessionConfiguration.ephemeral; config.timeoutIntervalForRequest = 40; config.timeoutIntervalForResource = 50
            let session = URLSession(configuration: config, delegate: NativeHardwareRedirectGuard(), delegateQueue: nil)
            self.transport = { request in let (data, response) = try await session.data(for: request); guard let http = response as? HTTPURLResponse else { throw NativeHardwareFailure("Hardware response unavailable.") }; return (data, http) }
        }
    }
    var available: Bool { baseURL != nil }
    func configure(baseURL: URL?) { generation = UUID(); self.baseURL = baseURL; used = []; commandReview = nil; commandGeneration = nil; devices = nil; detail = nil; node = nil; context = nil; stats = nil; receipt = nil; error = nil; busy = false }
    private func request(_ path: String, body: [String: Any]? = nil) async throws -> [String: Any] {
        guard let baseURL, baseURL.scheme == "http", ["127.0.0.1", "::1", "[::1]"].contains(baseURL.host ?? ""), baseURL.user == nil, baseURL.password == nil, baseURL.query == nil, baseURL.fragment == nil, ["", "/"].contains(baseURL.path), var parts = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else { throw NativeHardwareFailure("Hardware requires the local agent connection.") }
        let owner = generation
        parts.percentEncodedPath = path; parts.query = nil; parts.fragment = nil
        guard let url = parts.url else { throw NativeHardwareFailure("Invalid hardware route.") }
        var request = URLRequest(url: url); request.httpMethod = body == nil ? "GET" : "POST"
        if let body { request.httpBody = try NativeHardwareWire.freeze(body); request.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        let (data, response) = try await transport(request)
        guard owner == generation, self.baseURL == baseURL else { throw NativeHardwareFailure("The hardware connection changed. Stale data was not applied.") }
        guard response.url == url, (200..<300).contains(response.statusCode), data.count <= 1_048_576, let value = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { throw NativeHardwareFailure("Hardware response unavailable. Private service details are withheld.") }
        return value
    }
    func refresh() async {
        guard available, !busy else { return }; busy = true; devices = nil; context = nil; stats = nil; detail = nil; node = nil; receipt = nil; error = nil; generation = UUID(); let owner = generation
        defer { if owner == generation { busy = false } }
        do {
            let inventory = try await request("/api/hardware/devices")
            guard inventory["error"] == nil, let rows = inventory["devices"] as? [[String: Any]], rows.count <= 512 else { throw NativeHardwareFailure("Registered device inventory is unknown.") }
            var seen = Set<String>()
            for row in rows { guard let id = row["device_id"] as? String, NativeHardwareWire.id(id), seen.insert(id).inserted else { throw NativeHardwareFailure("Registered device identities were invalid.") } }
            devices = rows
            do {
                let summary = try await request("/api/hardware/context")
                guard summary["error"] == nil, let text = summary["context"] as? String, text.utf8.count <= 65_536 else { throw NativeHardwareFailure("Hardware context is unavailable.") }; context = text
            } catch { guard owner == generation else { return }; context = nil; self.error = "Hardware context is unknown; registered device inventory remains available." }
            do {
                let counters = try await request("/api/hardware/stats")
                guard counters["error"] == nil, !counters.isEmpty else { throw NativeHardwareFailure("Hardware counters are unknown, not zero.") }
                for key in ["device_count", "total_capabilities", "total_sensors", "total_actuators", "actions_executed"] { guard let n = NativeHardwareWire.number(counters[key]), n >= 0, n <= 9_007_199_254_740_991, n.rounded(.towardZero) == n else { throw NativeHardwareFailure("Hardware counters were malformed.") } }
                _ = try NativeHardwareWire.freeze(counters); stats = counters
            } catch { guard owner == generation else { return }; stats = nil; self.error = "Some hardware context or counters are unavailable; registered device inventory remains available." }
        } catch { guard owner == generation else { return }; devices = nil; context = nil; stats = nil; self.error = (error as? NativeHardwareFailure)?.message ?? "Hardware inventory unavailable." }
    }
    func inspect(_ deviceID: String) async {
        guard available, !busy, NativeHardwareWire.id(deviceID) else { return }
        busy = true; generation = UUID(); let owner = generation; detail = nil; node = nil; receipt = nil; commandReview = nil; commandGeneration = nil; error = nil
        defer { if owner == generation { busy = false } }
        do { let found = try NativeHardwareWire.manifest(try await request("/api/hardware/device/" + NativeHardwareWire.segment(deviceID)), deviceID: deviceID); let live = try NativeHardwareWire.liveNode(try await request("/api/hardware/mesh"), deviceID: deviceID); guard owner == generation else { return }; detail = found; node = live }
        catch { guard owner == generation else { return }; self.error = (error as? NativeHardwareFailure)?.message ?? "Device detail unavailable." }
    }
    func review(command: String, params: [String: Any], timeout: Double) throws -> NativeHardwareReview {
        guard !busy, let baseURL, let detail, let node, let deviceID = detail["device_id"] as? String, timeout.isFinite, (1...30).contains(timeout) else { throw NativeHardwareFailure("Inspect a live declared mesh device and a timeout from 1 to 30 seconds first.") }
        let cap = try NativeHardwareWire.capability(detail, command: command, params: params)
        return NativeHardwareReview(generation: generation, origin: baseURL.absoluteString, deviceID: deviceID, command: command, manifest: try NativeHardwareWire.freeze(detail), node: try NativeHardwareWire.freeze(node), params: try NativeHardwareWire.freeze(params), timeout: timeout,
            explanation: "Read exact device \(deviceID), method \(command), timeout \(timeout)s. Parameters: \(NativeHardwareWire.pretty(params)). Declared method: \(NativeHardwareWire.pretty(cap)). This can collect private sensor/health data from the device and return it to this local brain. The manifest is self-declared, not physical certification. Only a declared passive read is enabled; policy/grants can still refuse it. Receipt is a device report, not measured-outcome verification. The local API shares operator authority across local clients; it does not establish a separate browser identity. Dispatch uses a server-issued exact review and command ledger. No retry, physical cancellation or cross-device sync is promised.")
    }
    @discardableResult func perform(_ review: NativeHardwareReview) async -> Bool {
        guard !busy, review.generation == generation, review.origin == baseURL?.absoluteString, !used.contains(review.id), detail?["device_id"] as? String == review.deviceID else { error = "The hardware review expired or was already used."; return false }
        used.insert(review.id); busy = true; error = nil; receipt = nil; commandReview = nil; commandGeneration = nil; let owner = generation; var wrote = false
        defer { if owner == generation { busy = false } }
        do {
            let fresh = try NativeHardwareWire.manifest(try await request("/api/hardware/device/" + NativeHardwareWire.segment(review.deviceID)), deviceID: review.deviceID)
            guard try NativeHardwareWire.freeze(fresh) == review.manifest, let live = try NativeHardwareWire.liveNode(try await request("/api/hardware/mesh"), deviceID: review.deviceID), try NativeHardwareWire.freeze(live) == review.node else { throw NativeHardwareFailure("Device registration or declared method changed. No invocation was sent.") }
            guard owner == generation, review.origin == baseURL?.absoluteString else { return false }
            let params = try JSONSerialization.jsonObject(with: review.params) as! [String: Any]
            _ = try NativeHardwareWire.capability(fresh, command: review.command, params: params)
            let primary = try await request("/api/sessions/primary")
            guard let primaryID = primary["session_id"] as? String, NativeHardwareWire.id(primaryID), primary["error"] == nil else { throw NativeHardwareFailure("The operator's verified primary session was unavailable. No dispatch was sent.") }
            let held = try NativeHardwareWire.serverReview(try await request("/api/hardware/reviewed/review", body: ["node_id": review.deviceID, "command": review.command, "params": params, "timeout": review.timeout]), local: review, primaryID: primaryID)
            guard owner == generation, held.expiresAt > Date().timeIntervalSince1970 else { throw NativeHardwareFailure("The server review expired before dispatch.") }
            wrote = true
            let body = try await request("/api/hardware/reviewed/" + held.reviewID + "/dispatch", body: [:])
            guard owner == generation else { return false }; receipt = try NativeHardwareWire.receipt(body, review: held); commandReview = held; commandGeneration = owner
            return true
        } catch { guard owner == generation else { return false }; self.error = ((error as? NativeHardwareFailure)?.message ?? "The hardware request failed.") + (wrote ? " The request may have reached the device; inspect before a fresh review. It is not retried." : ""); return false }
    }
    func refreshReceipt() async {
        guard !busy, let held = commandReview, commandGeneration == generation, let commandID = receipt?.commandID else { return }
        busy = true; error = nil; let owner = generation
        defer { if owner == generation { busy = false } }
        do {
            let body = try await request("/api/hardware/reviewed/commands/" + commandID)
            guard owner == generation else { return }
            receipt = try NativeHardwareWire.receipt(body, review: held, expectedCommandID: commandID)
        } catch { guard owner == generation else { return }; self.error = "Exact command ledger readback is unavailable. The previous receipt remains; no action was retried." }
    }

}

struct NativeHardwareFeatureView: View {
    let baseURL: URL?
    let deviceID: String?
    init(baseURL: URL?, deviceID: String? = nil) { self.baseURL = baseURL; self.deviceID = deviceID }
    @StateObject private var model = NativeHardwareModel()
    @State private var command = ""
    @State private var params = "{}"
    @State private var timeout = "10"
    @State private var localError: String?
    @State private var review: NativeHardwareReview?
    @State private var showPrivateReport = false
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack { Text("Device methods").font(.title2.bold()); Spacer(); Button("Read registered devices") { Task { await model.refresh() } }.disabled(!model.available || model.busy) }
            Text("Declared interfaces and reviewed passive sensor reads. Physical actuators, privileged commands and methods needing another confirmation are inspection only. No hardware has been certified by this preview.").foregroundStyle(.secondary)
            if let issue = localError ?? model.error { NativeSelectableText(issue).foregroundStyle(.red) }
            if model.busy { ProgressView("Checking declared device and live registration…") }
            HSplitView {
                if deviceID == nil { ScrollView { VStack(alignment: .leading) {
                    if let devices = model.devices { if devices.isEmpty { Text("No registered devices returned.") }; ForEach(Array(devices.enumerated()), id: \.offset) { _, row in Button(row["name"] as? String ?? row["device_id"] as? String ?? "Device") { if let id = row["device_id"] as? String { command = ""; showPrivateReport = false; Task { await model.inspect(id) } } }.disabled(model.busy) } }
                    else { Text("Registered device inventory is unknown until read.") }
                    if let stats = model.stats { DisclosureGroup("Registry counters — not physical proof") { NativeSelectableText(NativeHardwareWire.pretty(stats)).font(.caption) } }
                    if let context = model.context { DisclosureGroup("Server hardware context") { NativeSelectableText(context) } }
                }.padding() }.frame(minWidth: 180, maxWidth: 300) }
                ScrollView { VStack(alignment: .leading, spacing: 10) {
                    if let detail = model.detail {
                        Text(detail["name"] as? String ?? "Declared device").font(.headline)
                        Text(model.node == nil ? "No matching live mesh node. Inspection only." : "Live registration returned; availability can change before dispatch.").foregroundStyle(.secondary)
                        ForEach(Array((detail["capabilities"] as? [[String: Any]] ?? []).enumerated()), id: \.offset) { _, cap in
                            DisclosureGroup(cap["name"] as? String ?? cap["id"] as? String ?? "Method") { NativeSelectableText(NativeHardwareWire.pretty(cap)).font(.caption.monospaced()); Button("Select declared method") { command = cap["id"] as? String ?? "" } }
                        }
                        Text("Method: " + (command.isEmpty ? "Choose a declaration" : command))
                        NativePlainTextEditor(text: $params, label: "Exact sensor parameters JSON", monospaced: true).frame(minHeight: 100)
                        TextField("Timeout seconds (1–30)", text: $timeout)
                        Button("Review sensor read…") { do { guard let data = params.data(using: .utf8), let object = try JSONSerialization.jsonObject(with: data) as? [String: Any], let seconds = Double(timeout) else { throw NativeHardwareFailure("Supply an exact JSON object and finite timeout.") }; review = try model.review(command: command, params: object, timeout: seconds); localError = nil } catch { localError = (error as? NativeHardwareFailure)?.message ?? "Parameters are not valid JSON." } }.disabled(model.busy || model.node == nil || command.isEmpty)
                    } else { Text("Select a registered device to inspect its declared methods.") }
                    if let receipt = model.receipt { Button("Check this command ledger") { Task { await model.refreshReceipt() } }.disabled(model.busy); NativeSelectableText(receipt.explanation).id(receipt.state); if let id = receipt.commandID { NativeSelectableText("Server command ID: " + id).font(.caption) }; Toggle("Reveal private device report", isOn: $showPrivateReport); if showPrivateReport { NativeSelectableText(NativeHardwareWire.pretty(receipt.raw)).font(.caption.monospaced()) } }
                }.padding() }
            }
        }.padding().task(id: (baseURL?.absoluteString ?? "") + "|" + (deviceID ?? "")) { model.configure(baseURL: baseURL); if let deviceID { await model.inspect(deviceID) } }
        .sheet(item: $review) { held in VStack(alignment: .leading, spacing: 16) { ScrollView { NativeReviewSummaryView(review: held.summary) }; HStack { Button("Cancel") { review = nil }; Spacer(); Button("Send this exact read") { review = nil; showPrivateReport = false; Task { _ = await model.perform(held) } } } }.padding(24).frame(width: 680, height: 540) }
    }
}
