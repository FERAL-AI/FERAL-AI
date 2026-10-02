import Foundation
import SwiftUI
import CoreFoundation
import CryptoKit

// Local management reads are automatic. Registry traffic is a separate reviewed action.
private struct CapabilitiesFailure: LocalizedError { let message: String; var errorDescription: String? { message } }
private func capabilitiesBool(_ value: Any?) -> Bool? { guard let n = value as? NSNumber, CFGetTypeID(n) == CFBooleanGetTypeID() else { return nil }; return n.boolValue }
private func capabilitiesJSON(_ value: Any) -> String { guard let bytes = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys, .prettyPrinted]) else { return "Unreadable disclosure" }; return String(decoding: bytes, as: UTF8.self) }
private func capabilitiesCount(_ value: Any?) -> Int? {
    guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(), ["c","s","i","l","q","C","S","I","L","Q"].contains(String(cString:number.objCType)),let count = Int(number.stringValue),count >= 0 else { return nil };return count
}
private func forgePrefix(_ value:String,_ length:Int) -> String { String(String.UnicodeScalarView(value.unicodeScalars.prefix(length))) }
final class NativeCapabilitiesRedirectGuard: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        guard let from = task.originalRequest?.url, let to = request.url, from.scheme == to.scheme, from.host == to.host, from.port == to.port else { completionHandler(nil); return }; completionHandler(request)
    }
    static func session() -> URLSession { URLSession(configuration: .ephemeral, delegate: NativeCapabilitiesRedirectGuard(), delegateQueue: nil) }
}
enum NativeCapabilitiesTab: String, CaseIterable { case skills = "Skills", forge = "Forge", marketplace = "Marketplace", apps = "Apps" }
struct NativeCapabilityRow: Identifiable {
    let source: String, key: String, raw: [String: Any]
    var id: String { source + ":" + key }
    var name: String { raw["name"] as? String ?? (raw["brand"] as? [String: Any])?["name"] as? String ?? key }
    var description: String { raw["description"] as? String ?? "No description returned" }
}
enum NativeCapabilityAction {
    case reload(String), decision(source: String, id: String, approve: Bool), uninstall(source: String, id: String)
    case browse(kind: String, query: String), preview(kind: String, id: String), install
    case propose(intent:String), generate(sequenceID:String)
}
struct NativeCapabilityReview: Identifiable { let id = UUID(); let generation: UUID; let action: NativeCapabilityAction; let title: String, explanation: String, signature: String; let disclosure: NativeCapabilityRow?;let expires:Date }
struct NativeForgeOutcome {
    let state:String,toolID:String,input:String,preview:String
}
struct NativeCapabilityInstall {
    let generation: UUID, kind: String, id: String, token: String, payload: [String: Any], expires: Date
}

@MainActor final class NativeCapabilitiesModel: ObservableObject {
    @Published private(set) var rows: [String: [NativeCapabilityRow]] = [:]
    @Published private(set) var errors: [String: String] = [:]
    @Published private(set) var busy = false
    @Published private(set) var receipt: String?
    @Published private(set) var actionError: String?
    @Published private(set) var install: NativeCapabilityInstall?
    @Published var forgeIntent = ""
    @Published private(set) var forgeOutcome:NativeForgeOutcome?
    @Published private(set) var forgeStats:[String:Int]?
    private(set) var catalogueKind: String?
    private var baseURL: URL?
    private var generation = UUID()
    private let session: URLSession
    private let now:() -> Date
    private var issued: [UUID: NativeCapabilityReview] = [:]
    private var uncertainForgeTargets:Set<String> = []
    static let kinds = ["skill", "daemon", "mcp", "channel", "provider", "memory", "workflow", "agent", "app"]
    static let reads: [(String, String, String?)] = [("skills", "/skills", nil), ("skillDrafts", "/api/skills/pending", "pending"), ("toolDrafts", "/api/tool-genesis/pending", "proposals"), ("generated", "/api/tool-genesis/list", "tools"), ("installed", "/api/marketplace/installed", "skills"), ("apps", "/api/apps", "apps"),("proposals","/api/tool-genesis/proposals","proposals")]
    static let statKeys = ["sequences_tracked","proposals_ready","tools_generated","total_uses"]
    nonisolated static func intentToolID(_ intent:String) -> String {
        let digest = Insecure.MD5.hash(data:Data(("intent::" + forgePrefix(intent,200)).utf8)).map { String(format:"%02x",$0) }.joined()
        return "genesis_intent_" + digest.prefix(12)
    }
    init(baseURL: URL?, session: URLSession? = nil,now:@escaping () -> Date = Date.init) { self.baseURL = baseURL; self.session = session ?? NativeCapabilitiesRedirectGuard.session();self.now = now }
    func configure(baseURL: URL?) { guard self.baseURL != baseURL else { return }; self.baseURL = baseURL; generation = UUID(); rows = [:]; errors = [:]; install = nil; catalogueKind = nil; issued = [:]; busy = false; receipt = nil; actionError = nil;forgeIntent = "";forgeOutcome = nil;forgeStats = nil }
    private func segment(_ value: String) throws -> String {
        guard !value.isEmpty, value.count <= 256, value != ".", value != "..", value.unicodeScalars.allSatisfy({ CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_ .:@/").contains($0) }) else { throw CapabilitiesFailure(message: "Unsupported item identity. No change sent.") }
        return value.addingPercentEncoding(withAllowedCharacters: CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.~"))!
    }
    private func request(_ path: String, method: String = "GET", body: [String: Any]? = nil, query: [URLQueryItem] = []) async throws -> Any {
        try Task.checkCancellation(); let start = generation
        guard let baseURL, ["127.0.0.1", "::1", "[::1]"].contains(baseURL.host ?? ""), ["http", "https"].contains(baseURL.scheme ?? ""), baseURL.user == nil, baseURL.password == nil, var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else { throw CapabilitiesFailure(message: "Connect to the app-owned local agent.") }
        components.percentEncodedPath = path; components.queryItems = query.isEmpty ? nil : query; components.fragment = nil
        guard let url = components.url else { throw CapabilitiesFailure(message: "Invalid local request.") }
        var req = URLRequest(url: url); req.httpMethod = method; req.timeoutInterval = 180
        if let body { req.httpBody = try JSONSerialization.data(withJSONObject: body); req.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        let (data, response) = try await session.data(for: req); try Task.checkCancellation()
        guard generation == start else { throw CapabilitiesFailure(message: "Agent changed. Review again.") }
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode), let value = try? JSONSerialization.jsonObject(with: data) else { throw CapabilitiesFailure(message: "Local request failed. Refresh to confirm state before retrying.") }
        if let object = value as? [String: Any], capabilitiesBool(object["ok"]) == false || capabilitiesBool(object["success"]) == false || (object["error"] as? String)?.isEmpty == false { throw CapabilitiesFailure(message: "The backend refused or could not confirm this operation.") }
        return value
    }
    private func parse(_ value: Any, source: String, field: String?) throws -> [NativeCapabilityRow] {
        let list: [[String: Any]]?
        if let field { list = (value as? [String: Any])?[field] as? [[String: Any]] } else { list = value as? [[String: Any]] }
        guard let list else { throw CapabilitiesFailure(message: "This catalogue returned an unreadable list; it is not confirmed empty.") }
        var seen = Set<String>()
        return try list.map { row in
            guard let key = (row["skill_id"] ?? row["tool_id"] ?? row["app_id"] ?? row["id"] ?? row["item_id"] ?? row["sequence_id"]) as? String, !key.isEmpty, seen.insert(key).inserted else { throw CapabilitiesFailure(message: "Catalogue identities are missing or duplicated.") }
            if source == "proposals" {
                guard key.count == 12,key.allSatisfy({ "0123456789abcdef".contains($0) }),row["sequence_id"] as? String == key,let tools = row["tools"] as? [String],!tools.isEmpty,tools.count <= 6,tools.allSatisfy({ !$0.isEmpty && $0.utf8.count <= 256 }),capabilitiesCount(row["seen_count"]) != nil,row["name"] is String,row["description"] is String else { throw CapabilitiesFailure(message:"Sequence proposal terms are incomplete. Generation is unavailable.") }
            }
            _ = try segment(key); return NativeCapabilityRow(source: source, key: key, raw: row)
        }
    }
    func refresh() async {
        guard !busy else { return }; busy = true; let start = generation; defer { if start == generation { busy = false } }
        await refreshLocal(start: start)
    }
    private func refreshLocal(start: UUID) async {
        for (source, path, field) in Self.reads {
            guard start == generation else { return }
            do { let list = try parse(await request(path), source: source, field: field); guard start == generation else { return }; rows[source] = list; errors[source] = nil }
            catch { if start == generation { rows[source] = nil; errors[source] = error.localizedDescription } }
        }
        guard start == generation else { return }
        do {
            guard let value = try await request("/api/tool-genesis/stats") as? [String:Any] else { throw CapabilitiesFailure(message:"Tool Genesis statistics are unreadable.") }
            var counters:[String:Int] = [:]
            if !value.isEmpty { for key in Self.statKeys { guard let count = capabilitiesCount(value[key]) else { throw CapabilitiesFailure(message:"Tool Genesis statistics are incomplete or malformed; no counters are confirmed.") };counters[key] = count } }
            guard start == generation else { return };forgeStats = counters;errors["stats"] = nil
        } catch { if start == generation { forgeStats = nil;errors["stats"] = error.localizedDescription } }
    }
    private func requireNewTarget(_ id:String) throws {
        guard let stats = forgeStats,!stats.isEmpty else { throw CapabilitiesFailure(message:"Tool Genesis availability and statistics are not confirmed. Refresh before drafting.") }
        guard !uncertainForgeTargets.contains(id) else { throw CapabilitiesFailure(message:"An earlier request for \(id) has an unknown outcome. It cannot be resubmitted. Inspect the inventory or use an intent with a distinct first 200 characters.") }
        for source in ["generated","toolDrafts","skills"] {
            guard let inventory = rows[source],errors[source] == nil else { throw CapabilitiesFailure(message:"Refresh generated tools, pending drafts and loaded skills before drafting.") }
            guard !inventory.contains(where:{$0.key == id}) else { throw CapabilitiesFailure(message:"The deterministic draft ID \(id) already exists in \(source). Replacement is unavailable without an atomic backend conflict contract. Use a distinct intent or inspect the existing item.") }
        }
    }
    private func freshNewTarget(_ id:String,start:UUID) async throws {
        for source in ["generated","toolDrafts","skills"] {
            let entry = Self.reads.first(where:{$0.0 == source})!
            let list = try parse(await request(entry.1),source:source,field:entry.2)
            guard start == generation else { throw CapabilitiesFailure(message:"Agent changed. Review again.") };rows[source] = list;errors[source] = nil
        };try requireNewTarget(id)
    }
    func review(_ action: NativeCapabilityAction) throws -> NativeCapabilityReview {
        guard !busy else { throw CapabilitiesFailure(message: "Wait for the current request.") }
        let title: String, explanation: String, signature: String
        var disclosure: NativeCapabilityRow?
        switch action {
        case .propose(let intent):
            guard intent == forgeIntent,!intent.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty,intent.utf8.count <= 8_000 else { throw CapabilitiesFailure(message:"Review the current nonempty intent, at most 8,000 UTF-8 bytes.") }
            let id = Self.intentToolID(intent);try requireNewTarget(id)
            title = "Draft this capability?";explanation = "Exact intent sent to the configured language model:\n\(intent)\n\nExpected draft ID: \(id). This may use a remote provider and incur model costs. It AST-checks generated code and stores an unapproved draft; it does not sandbox-run, register or execute that code. IDs depend on the first 200 characters. Existing IDs are blocked, but the server has no atomic conflict check against concurrent callers.";signature = capabilitiesJSON(["intent":intent,"tool_id":id])
        case .generate(let id):
            guard let row = rows["proposals"]?.first(where:{$0.key == id}),errors["proposals"] == nil else { throw CapabilitiesFailure(message:"Refresh and choose an exact tracked sequence first.") }
            try requireNewTarget("genesis_" + id)
            title = "Draft this tracked sequence?";explanation = "Sequence \(id). The exact tool order and proposal terms below are sent to the configured language model. This may incur model costs. Code is AST-checked and stored pending separate registration approval; it is not sandbox-run or executed. Existing deterministic IDs are blocked. Concurrent callers are not protected by an atomic server conflict check.";signature = capabilitiesJSON(row.raw);disclosure = row
        case .browse(let kind, let query):
            guard Self.kinds.contains(kind), query.count <= 500 else { throw CapabilitiesFailure(message: "Choose a supported catalogue kind and a shorter query.") }
            title = "Contact the community registry?"; explanation = "The backend sends this kind and search text to its configured external registry and may try fallback registry addresses. Kind: \(kind). Search: \(query.isEmpty ? "none" : query). Catalogue metadata is unsigned and is not an install consent. Nothing is installed."; signature = ""
        case .preview(let kind, let id):
            guard Self.kinds.contains(kind), catalogueKind == kind, rows["catalogue"]?.contains(where: { $0.key == id }) == true else { throw CapabilitiesFailure(message: "Browse and choose a current catalogue item first.") }; _ = try segment(id)
            title = "Download and verify this package?"; explanation = "Item \(kind):\(id). The backend contacts the registry, downloads/stages a bundle and verifies its publisher signature. An app preview may also fetch missing skill dependencies. Nothing is installed; review verified permissions and dependencies next. No unsigned or high-trust override will be sent."; signature = ""
        case .install:
            guard let install, install.generation == generation, install.expires > Date() else { throw CapabilitiesFailure(message: "Verified preview expired or is unavailable. Download and review again.") }
            title = "Install the reviewed verified package?"; explanation = "\(install.kind):\(install.id). Installs exactly the staged bytes bound to this single-use token. These permissions describe potential access, not a sandbox guarantee. Live registration can immediately execute Python import/constructor code and arm manifest cron jobs for background execution. Apps may install the named dependencies below; unavailable dependencies can leave the app degraded. Existing versions may be replaced. Installation does not establish device consent or safe execution."; signature = capabilitiesJSON(install.payload)
        case .reload(let id):
            guard let row = rows["skills"]?.first(where: { $0.key == id }) else { throw CapabilitiesFailure(message: "Refresh loaded skills first.") }; title = "Reload this skill from disk?"; explanation = "\(row.name) (\(id)). Imports its current implementation into the running registry. Python import/constructor code can execute immediately, and registration can arm manifest cron jobs for background execution. Disk code may differ from this manifest summary. This is not a security review; no skill endpoint call is requested."; signature = capabilitiesJSON(row.raw); disclosure = row
        case .decision(let source, let id, let approve):
            guard ["skillDrafts", "toolDrafts"].contains(source), let row = rows[source]?.first(where: { $0.key == id }) else { throw CapabilitiesFailure(message: "Refresh this exact draft queue first.") }
            title = approve ? "Register this generated capability?" : "Discard this generated draft?"
            explanation = "\(row.name) (\(id)). " + (approve ? "Writes generated code to disk and attempts live registration. Python import/constructor code can execute immediately, and registration can arm manifest cron jobs for background execution. Generated code can perform privileged actions; approval is not proof of safety. Tool Genesis returns only a truncated code preview, not the full implementation. No explicit skill endpoint call is requested." : "Removes this draft from its exact source queue; this does not undo actions or delete external data.")
            signature = capabilitiesJSON(row.raw); disclosure = row
        case .uninstall(let source, let id):
            guard ["installed", "apps"].contains(source), let row = rows[source]?.first(where: { $0.key == id }) else { throw CapabilitiesFailure(message: "Refresh the installed inventory first.") }
            title = "Uninstall this \(source == "apps" ? "app" : "skill")?"; explanation = "\(row.name) (\(id)). Removes the installed bundle. It does not revoke upstream credentials, erase external data, undo file edits or stop every already-running task. Other apps may depend on it."; signature = capabilitiesJSON(row.raw); disclosure = row
        }
        issued = issued.filter { $0.value.expires > now() };if issued.count >= 20 { issued.removeAll() }; let review = NativeCapabilityReview(generation: generation, action: action, title: title, explanation: explanation, signature: signature, disclosure: disclosure,expires:now().addingTimeInterval(120)); issued[review.id] = review; return review
    }
    private func sameRow(source: String, id: String, signature: String, start: UUID) async throws {
        guard generation == start else { throw CapabilitiesFailure(message: "Agent changed. Review again.") }
        guard let entry = Self.reads.first(where: { $0.0 == source }) else { throw CapabilitiesFailure(message: "Unknown review source.") }
        let current = try parse(await request(entry.1), source: source, field: entry.2)
        guard generation == start else { throw CapabilitiesFailure(message: "Agent changed. Review again.") }
        rows[source] = current
        guard let row = current.first(where: { $0.key == id }), capabilitiesJSON(row.raw) == signature else { throw CapabilitiesFailure(message: "The item changed. Review its refreshed state before confirming.") }
    }
    private func verifiedPreview(_ value: [String: Any], kind: String, id: String) throws -> NativeCapabilityInstall {
        guard capabilitiesBool(value["success"]) == true, let token = value["install_token"] as? String, !token.isEmpty, let ttl = value["expires_in"] as? NSNumber, CFGetTypeID(ttl) != CFBooleanGetTypeID(), ttl.doubleValue.isFinite, ttl.doubleValue > 0, ttl.doubleValue <= 3600, let signature = value["signature"] as? [String: Any], capabilitiesBool(signature["verified"]) == true, let digest = signature["sha256"] as? String, digest.count == 64, digest.allSatisfy({ $0.isHexDigit }), value["permission_details"] is [[String: Any]] else { throw CapabilitiesFailure(message: "Verified signature, permission disclosure or expiry is missing. Install blocked.") }
        if kind == "app" { guard let appID = (value["app"] as? [String: Any])?["app_id"] as? String, !appID.isEmpty, let origin = value["source"] as? [String: Any], origin["origin"] as? String == "registry_id", origin["value"] as? String == id, let deps = value["skill_dependencies"] as? [String: Any], ["already_installed", "to_install", "unavailable"].allSatisfy({ deps[$0] is [[String: Any]] }) else { throw CapabilitiesFailure(message: "App identity or dependency disclosure is incomplete. Install blocked.") }
            for dep in deps["to_install"] as! [[String: Any]] { guard dep["skill_id"] is String, dep["permission_details"] is [[String: Any]], capabilitiesBool((dep["signature"] as? [String: Any])?["verified"]) == true else { throw CapabilitiesFailure(message: "An app dependency lacks verified permissions. Install blocked.") } }
        } else { guard value["id"] as? String == id, value["kind"] as? String == kind, value["permissions"] is [String] else { throw CapabilitiesFailure(message: "Verified package identity or permissions differ. Install blocked.") } }
        return NativeCapabilityInstall(generation: generation, kind: kind, id: id, token: token, payload: value, expires: Date().addingTimeInterval(ttl.doubleValue))
    }
    func perform(_ reviewed: NativeCapabilityReview) async -> Bool {
        guard !busy, reviewed.generation == generation, let issuedReview = issued.removeValue(forKey: reviewed.id), issuedReview.signature == reviewed.signature,issuedReview.expires > now() else { actionError = "Review is stale, expired or already used. Review again."; return false }
        let start = generation; busy = true; receipt = nil; actionError = nil; defer { if start == generation { busy = false } }
        var attemptedForge:NativeForgeOutcome?
        do {
            switch issuedReview.action {
            case .propose(let intent),.generate(let intent):
                let isIntent:Bool
                if case .propose = issuedReview.action { isIntent = true } else { isIntent = false }
                let toolID = isIntent ? Self.intentToolID(intent) : "genesis_" + intent
                var sequence:[String] = []
                if isIntent {
                    guard forgeIntent == intent,capabilitiesJSON(["intent":intent,"tool_id":toolID]) == issuedReview.signature else { throw CapabilitiesFailure(message:"The intent changed after review. No draft request was sent.") }
                } else {
                    try await sameRow(source:"proposals",id:intent,signature:issuedReview.signature,start:start)
                    sequence = rows["proposals"]?.first(where:{$0.key == intent})?.raw["tools"] as? [String] ?? []
                }
                try await freshNewTarget(toolID,start:start)
                guard start == generation,issuedReview.expires > now() else { throw CapabilitiesFailure(message:"The review expired during preflight. No draft request was sent.") }
                if isIntent,forgeIntent != intent { throw CapabilitiesFailure(message:"The intent changed during preflight. No draft request was sent.") }
                let drafting = NativeForgeOutcome(state:"drafting",toolID:toolID,input:intent,preview:"");attemptedForge = drafting;forgeOutcome = drafting
                let result = try await request(isIntent ? "/api/tool-genesis/propose" : "/api/tool-genesis/generate",method:"POST",body:[isIntent ? "intent" : "sequence_id":intent]) as? [String:Any]
                guard let result,capabilitiesBool(result["success"]) == true,let tool = isIntent ? result : result["tool"] as? [String:Any],tool["tool_id"] as? String == toolID,let preview = tool["preview"] as? String,!preview.isEmpty,preview.unicodeScalars.count <= 800 else { throw CapabilitiesFailure(message:"Draft response identity or code preview was not confirmed.") }
                attemptedForge = NativeForgeOutcome(state:"outcome_unknown",toolID:toolID,input:intent,preview:preview)
                let pending = try parse(await request("/api/tool-genesis/pending"),source:"toolDrafts",field:"proposals")
                let generated = try parse(await request("/api/tool-genesis/list"),source:"generated",field:"tools")
                guard start == generation,let draft = pending.first(where:{$0.key == toolID}),let stored = generated.first(where:{$0.key == toolID}),draft.raw["preview"] as? String == forgePrefix(preview,400),draft.raw["source_sequence"] as? [String] == sequence,stored.raw["source"] as? [String] == sequence,draft.raw["name"] as? String == stored.raw["name"] as? String,draft.raw["description"] as? String == stored.raw["description"] as? String else { throw CapabilitiesFailure(message:"Generated response could not be matched to the current unapproved queue and inventory.") }
                if !isIntent { guard tool["name"] as? String == draft.raw["name"] as? String,tool["description"] as? String == draft.raw["description"] as? String else { throw CapabilitiesFailure(message:"Generated sequence terms differ from readback.") } }
                forgeOutcome = NativeForgeOutcome(state:"pending",toolID:toolID,input:intent,preview:preview)
                receipt = "Draft \(toolID) is present in the unapproved queue and generated inventory. Code safety, durable restart recovery and execution are not verified. Registration needs a separate review."
                await refreshLocal(start:start)
            case .browse(let kind, let query):
                rows["catalogue"] = nil; catalogueKind = nil; install = nil
                let value = try await request("/api/marketplace/catalog", query: [URLQueryItem(name: "kind", value: kind), URLQueryItem(name: "q", value: query)])
                guard generation == start else { return false }
                rows["catalogue"] = try parse(value, source: "catalogue", field: "items"); catalogueKind = kind; install = nil; errors["catalogue"] = nil; receipt = "Registry metadata received. It is not verified install consent."
            case .preview(let kind, let id):
                install = nil
                let body: [String: Any] = kind == "app" ? ["registry_id": id] : ["kind": kind, "id": id]
                guard let value = try await request(kind == "app" ? "/api/apps/preview" : "/api/marketplace/preview", method: "POST", body: body) as? [String: Any] else { throw CapabilitiesFailure(message: "Preview is unreadable.") }
                guard generation == start else { return false }
                install = try verifiedPreview(value, kind: kind, id: id); receipt = "Verified preview ready. Review its permission and dependency disclosures before installing."
            case .install:
                guard let held = install, held.generation == generation, held.expires > Date(), capabilitiesJSON(held.payload) == reviewed.signature else { throw CapabilitiesFailure(message: "The verified preview changed or expired.") }
                install = nil // A request may have consumed the server token even if its receipt is lost.
                let body: [String: Any] = held.kind == "app" ? ["install_token": held.token] : ["kind": held.kind, "id": held.id, "install_token": held.token]
                guard let result = try await request(held.kind == "app" ? "/api/apps/install" : "/api/marketplace/install", method: "POST", body: body) as? [String: Any], capabilitiesBool(result["success"]) == true else { throw CapabilitiesFailure(message: "Installation is not confirmed. Refresh inventory; do not retry an old token.") }
                if held.kind == "app" { guard (result["app"] as? [String: Any])?["app_id"] as? String == (held.payload["app"] as? [String: Any])?["app_id"] as? String else { throw CapabilitiesFailure(message: "Installed app identity was not confirmed.") } } else { guard capabilitiesBool((result["signature"] as? [String: Any])?["verified"]) == true, (result["signature"] as? [String: Any])?["sha256"] as? String == (held.payload["signature"] as? [String: Any])?["sha256"] as? String else { throw CapabilitiesFailure(message: "Installed package digest was not confirmed.") } }
                receipt = capabilitiesBool(result["degraded"]) == true ? "Backend reports installation with unavailable dependencies. Some app actions will not work." : "Backend reports installation. Runtime functionality and permissions remain unverified."
                await refreshLocal(start: start)
            case .reload(let id):
                try await sameRow(source: "skills", id: id, signature: reviewed.signature, start: start)
                guard generation == start else { return false }
                guard let value = try await request("/api/skills/reload", method: "POST", query: [URLQueryItem(name: "skill_id", value: id)]) as? [String: Any], capabilitiesBool(value["ok"]) == true, value["skill_id"] as? String == id else { throw CapabilitiesFailure(message: "Skill reload is not confirmed.") }; receipt = "Backend confirms live reload. Endpoint execution was not tested."; await refreshLocal(start: start)
            case .decision(let source, let id, let approve):
                try await sameRow(source: source, id: id, signature: reviewed.signature, start: start)
                guard generation == start else { return false }
                let path = source == "skillDrafts" ? "/api/skills/" : "/api/tool-genesis/"
                let body: [String: Any] = [source == "skillDrafts" ? "skill_id" : "tool_id": id]
                guard let result = try await request(path + (approve ? "approve" : "reject"), method: "POST", body: body) as? [String: Any] else { throw CapabilitiesFailure(message: "Draft outcome is unreadable.") }
                if source == "skillDrafts" { guard capabilitiesBool(result["ok"]) == true, result["skill_id"] as? String == id, capabilitiesBool(result[approve ? "registered" : "rejected"]) == true else { throw CapabilitiesFailure(message: "Skill decision is not confirmed.") } }
                else if approve { guard capabilitiesBool(result["promoted"]) == true, result["skill_id"] as? String == id else { throw CapabilitiesFailure(message: "Tool promotion is not confirmed.") } }
                else { guard capabilitiesBool(result["success"]) == true, result["rejected"] as? String == id else { throw CapabilitiesFailure(message: "Tool rejection is not confirmed.") } }
                receipt = approve ? (source == "toolDrafts" && capabilitiesBool(result["reloaded"]) != true ? "Generated tool persisted; live registry reload was not confirmed. Refresh loaded skills before using it." : "Backend confirms registration. Generated code safety and actual execution are not verified.") : "Backend confirms the exact draft was discarded."; await refreshLocal(start: start)
            case .uninstall(let source, let id):
                try await sameRow(source: source, id: id, signature: reviewed.signature, start: start)
                guard generation == start else { return false }
                let path = source == "apps" ? "/api/apps/" : "/api/marketplace/uninstall/"
                guard let value = try await request(path + (try segment(id)), method: "DELETE") as? [String: Any], capabilitiesBool(value["success"]) == true else { throw CapabilitiesFailure(message: "Uninstall was not confirmed.") }; receipt = "Backend confirms bundle removal; external data and running work are not reverted."; await refreshLocal(start: start)
            }
            guard start == generation else { return false }; return true
        } catch { if let attemptedForge { uncertainForgeTargets.insert(attemptedForge.toolID) };if start == generation {
            if let attemptedForge { forgeOutcome = NativeForgeOutcome(state:"outcome_unknown",toolID:attemptedForge.toolID,input:attemptedForge.input,preview:attemptedForge.preview);actionError = "Draft outcome unknown. A draft may have been created. Inspect pending drafts and generated inventory before any new request; this review cannot retry. " + error.localizedDescription }
            else { actionError = "Action not confirmed. " + error.localizedDescription }
        }; return false }
    }
}

struct NativeCapabilitiesFeatureView: View {
    let baseURL: URL?
    @StateObject private var model: NativeCapabilitiesModel
    @State private var tab: NativeCapabilitiesTab = .skills
    @State private var kind = "skill"
    @State private var query = ""
    @State private var review: NativeCapabilityReview?
    @State private var localError: String?
    init(baseURL: URL?) { self.baseURL = baseURL; _model = StateObject(wrappedValue: NativeCapabilitiesModel(baseURL: baseURL)) }
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack { Text("Capabilities").font(.largeTitle.bold()); Spacer(); Button("Refresh local inventory") { Task { await model.refresh() } }.disabled(model.busy || baseURL == nil) }
            Picker("Section", selection: $tab) { ForEach(NativeCapabilitiesTab.allCases, id: \.self) { Text($0.rawValue).tag($0) } }.pickerStyle(.segmented)
            if baseURL == nil { Text("Start the local agent to inspect its capabilities.").foregroundStyle(.secondary) }
            if let error = localError ?? model.actionError { NativeSelectableText(error).foregroundStyle(.red) }
            if let receipt = model.receipt { NativeSelectableText(receipt) }
            if model.busy { ProgressView("Working with the local agent…") }
            ScrollView { LazyVStack(alignment: .leading, spacing: 14) {
                switch tab {
                case .skills: section("skills", title: "Loaded skills") { row in Button("Reload from disk…") { requestReview(.reload(row.key)) }.disabled(model.busy) }
                case .forge:
                    Text("Generated code is not proven safe. Tool Genesis returns a truncated code preview. Registration may execute import/constructor code immediately and arm background cron jobs; a persisted draft is not necessarily live.").foregroundStyle(.orange)
                    card {
                        Text("Draft a capability").font(.headline)
                        Text("Describe the exact capability to send to your configured model. Drafting may incur model costs. It stores AST-checked code for a separate registration review; it does not run the code.").foregroundStyle(.secondary)
                        NativePlainTextEditor(text:$model.forgeIntent,label:"Exact capability intent").frame(minHeight:110).disabled(model.busy)
                        Text("\(model.forgeIntent.utf8.count) / 8,000 UTF-8 bytes").font(.caption).foregroundStyle(.secondary)
                        Button("Review draft request…") { requestReview(.propose(intent:model.forgeIntent)) }.disabled(model.busy || baseURL == nil)
                    }
                    if let outcome = model.forgeOutcome {
                        card {
                            Text(outcome.state == "pending" ? "Draft pending registration review" : outcome.state == "drafting" ? "Draft request in progress" : "Draft outcome needs checking").font(.headline)
                            NativeSelectableText(outcome.toolID).font(.caption)
                            if outcome.state == "outcome_unknown" { Text("Inspect pending drafts and generated tools before making another request. Nothing was automatically retried.").foregroundStyle(.orange) }
                            if !outcome.preview.isEmpty { DisclosureGroup("Returned code preview, at most 800 characters") { NativeSelectableText(outcome.preview).font(.system(.caption,design:.monospaced)) } }
                        }
                    }
                    card {
                        Text("Tool Genesis statistics").font(.headline)
                        if let error = model.errors["stats"] { Text(error).foregroundStyle(.red) }
                        if let stats = model.forgeStats {
                            if stats.isEmpty { Text("The agent returned no statistics; Tool Genesis may be unavailable. No zero counts are inferred.").foregroundStyle(.secondary) }
                            else { ForEach(NativeCapabilitiesModel.statKeys,id:\.self) { key in Text("\(key.replacingOccurrences(of:"_",with:" ")): \(stats[key]!)") } }
                        }
                    }
                    section("proposals",title:"Tracked tool sequences") { row in Button("Review sequence draft…") { requestReview(.generate(sequenceID:row.key)) }.disabled(model.busy) }
                    section("skillDrafts", title: "Skill-generator drafts") { row in decisionButtons(row) }
                    section("toolDrafts", title: "Tool Genesis drafts") { row in decisionButtons(row) }
                    section("generated", title: "Generated tools") { _ in EmptyView() }
                    Text("The generated inventory includes drafts and does not prove approval, live registration or execution. Existing deterministic draft IDs are blocked; replacement needs an atomic backend contract.").font(.caption).foregroundStyle(.secondary)
                case .marketplace:
                    card { Text("External community registry").font(.headline); Text("Browsing sends your kind/search to the configured registry. Metadata is unsigned; verified preview and installation each require another review.").foregroundStyle(.secondary); HStack { Picker("Kind", selection: $kind) { ForEach(NativeCapabilitiesModel.kinds, id: \.self) { Text($0).tag($0) } }; TextField("Search", text: $query); Button("Review registry request…") { requestReview(.browse(kind: kind, query: query)) }.disabled(model.busy || baseURL == nil) } }
                    section("catalogue", title: "Remote metadata") { row in Button("Review download and verification…") { requestReview(.preview(kind: model.catalogueKind ?? "", id: row.key)) }.disabled(model.busy) }
                    if let install = model.install { card { Text("Verified staged package: \(install.kind):\(install.id)").font(.headline); Text("Expires \(install.expires.formatted())"); disclosure(install.payload); Button("Review installation…") { requestReview(.install) }.disabled(model.busy) } }
                    section("installed", title: "Marketplace-installed skills") { row in Button("Uninstall…", role: .destructive) { requestReview(.uninstall(source: "installed", id: row.key)) }.disabled(model.busy) }
                    Text("Updates use a fresh verified preview/install. The legacy update endpoint is intentionally not exposed because it bypasses renewed permission/signature review.").font(.caption).foregroundStyle(.secondary)
                case .apps:
                    Text("Installed app manifests and missing dependencies only. Native GenUI surfaces, publishing, arbitrary dispatch and app execution are not implemented here.").foregroundStyle(.secondary)
                    section("apps", title: "Installed apps") { row in Button("Uninstall…", role: .destructive) { requestReview(.uninstall(source: "apps", id: row.key)) }.disabled(model.busy) }
                }
            }.frame(maxWidth: .infinity, alignment: .leading) }
        }.padding(24).task(id: baseURL) { review = nil; model.configure(baseURL: baseURL); if baseURL != nil { await model.refresh() } }
        .sheet(item: $review) { held in VStack(alignment: .leading, spacing: 14) { Text(held.title).font(.title2.bold()); ScrollView { VStack(alignment: .leading, spacing: 12) { NativeSelectableText(held.explanation); if let disclosure = held.disclosure { rowDisclosure(disclosure) }; if case .install = held.action, let install = model.install { disclosure(install.payload) } } }; if let error = model.actionError { Text(error).foregroundStyle(.red) }; HStack { Spacer(); Button("Cancel") { review = nil }.disabled(model.busy).keyboardShortcut(.cancelAction); Button("Confirm") { Task { if await model.perform(held) { review = nil } } }.disabled(model.busy).keyboardShortcut(.defaultAction) } }.padding(24).frame(width: 640, height: 540).interactiveDismissDisabled(model.busy) }
    }
    private func requestReview(_ action: NativeCapabilityAction) { do { localError = nil; review = try model.review(action) } catch { localError = error.localizedDescription } }
    private func card<Content: View>(@ViewBuilder _ content: () -> Content) -> some View { VStack(alignment: .leading, spacing: 10, content: content).padding(16).frame(maxWidth: .infinity, alignment: .leading).background(Color.secondary.opacity(0.07), in: RoundedRectangle(cornerRadius: 12)) }
    @ViewBuilder private func section<Actions: View>(_ source: String, title: String, @ViewBuilder actions: @escaping (NativeCapabilityRow) -> Actions) -> some View {
        Text(title).font(.title2.bold())
        if let error = model.errors[source] { Text(error).foregroundStyle(.red) }
        if let list = model.rows[source], list.isEmpty { Text("No records returned.").foregroundStyle(.secondary) }
        ForEach(model.rows[source] ?? []) { row in
            card {
                Text(row.name).font(.headline)
                NativeSelectableText(row.key).font(.caption).foregroundStyle(.secondary)
                Text(row.description).lineLimit(3).foregroundStyle(.secondary)
                if let version = row.raw["version"] as? String { Text("Version \(version)").font(.caption) }
                DisclosureGroup("Details and actions") {
                    NativeSelectableText(row.description)
                    rowDisclosure(row)
                }
                actions(row)
            }
        }
    }
    private func decisionButtons(_ row: NativeCapabilityRow) -> some View { HStack { Button("Review registration…") { requestReview(.decision(source: row.source, id: row.key, approve: true)) }; Button("Discard…", role: .destructive) { requestReview(.decision(source: row.source, id: row.key, approve: false)) } }.disabled(model.busy) }
    @ViewBuilder private func rowDisclosure(_ row: NativeCapabilityRow) -> some View {
        if let tools = row.raw["tools"] as? [String] { NativeSelectableText("Exact order: " + tools.joined(separator:" → "));if let count = capabilitiesCount(row.raw["seen_count"]) { Text("Observed \(count) times").font(.caption) } }
        if let endpoints = row.raw["endpoints"] as? [[String: Any]] { ForEach(Array(endpoints.enumerated()), id: \.offset) { _, e in NativeSelectableText("\(e["method"] as? String ?? "") \(e["id"] as? String ?? "") — \(e["description"] as? String ?? "")").font(.caption) } }
        if let preview = row.raw["preview"] as? String ?? row.raw["python_impl"] as? String { DisclosureGroup(row.source == "toolDrafts" ? "Truncated implementation preview" : "Generated implementation") { NativeSelectableText(preview).font(.system(.caption, design: .monospaced)) } }
        disclosure(row.raw)
    }
    @ViewBuilder private func disclosure(_ payload: [String: Any]) -> some View {
        if let signature = payload["signature"] as? [String: Any] { Text(capabilitiesBool(signature["verified"]) == true ? "Backend verified publisher signature" : "Publisher signature not verified").foregroundStyle(capabilitiesBool(signature["verified"]) == true ? Color.secondary : Color.orange); if let hash = signature["sha256"] as? String { NativeSelectableText("SHA-256: \(hash)").font(.caption) } }
        if let permissions = payload["permission_details"] as? [[String: Any]] { Text("Declared permissions").font(.headline); if permissions.isEmpty { Text("None declared; this is not a runtime safety guarantee.").font(.caption) }; ForEach(Array(permissions.enumerated()), id: \.offset) { _, p in NativeSelectableText("\(p["label"] as? String ?? p["title"] as? String ?? p["id"] as? String ?? "Permission"): \(p["description"] as? String ?? p["detail"] as? String ?? "No explanation returned")") } }
        if let permissions = payload["permissions"] as? [String], !permissions.isEmpty { NativeSelectableText(permissions.joined(separator: ", ")).font(.caption) }
        if let dependencies = payload["skill_dependencies"] as? [String: Any] { ForEach(["already_installed", "to_install", "unavailable"], id: \.self) { group in if let deps = dependencies[group] as? [[String: Any]], !deps.isEmpty { Text(group == "to_install" ? "Skills to install" : group == "unavailable" ? "Unavailable dependencies" : "Existing dependencies").font(.headline); ForEach(Array(deps.enumerated()), id: \.offset) { _, dep in VStack(alignment: .leading) { Text(dep["skill_id"] as? String ?? "Unknown skill").bold(); if let reason = dep["reason"] as? String { Text(reason) }; if let impact = dep["impact"] as? String { Text(impact) }; permissionList(dep) } } } } }
        if let missing = payload["missing_skill_dependencies"] as? [[String: Any]], !missing.isEmpty { Text("Missing dependencies: " + missing.compactMap { $0["skill_id"] as? String }.joined(separator: ", ")).foregroundStyle(.orange) }
        if let warnings = payload["warnings"] as? [String] { ForEach(Array(warnings.enumerated()), id: \.offset) { _, warning in Text(warning).foregroundStyle(.orange) } }
    }
    @ViewBuilder private func permissionList(_ payload: [String: Any]) -> some View { if let permissions = payload["permission_details"] as? [[String: Any]] { ForEach(Array(permissions.enumerated()), id: \.offset) { _, p in NativeSelectableText("\(p["label"] as? String ?? p["id"] as? String ?? "Permission"): \(p["description"] as? String ?? "")").font(.caption) } } }
}
