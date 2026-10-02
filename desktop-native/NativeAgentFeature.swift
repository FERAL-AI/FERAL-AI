import SwiftUI
import Foundation
import CoreFoundation

struct NativeAgentFailure: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}
enum NativeAgentWire {
    static func bool(_ value: Any?) -> Bool? {
        guard let n = value as? NSNumber, CFGetTypeID(n) == CFBooleanGetTypeID() else { return nil }
        return n.boolValue
    }
    static func number(_ value:Any?) -> Double? {
        guard let n = value as? NSNumber,CFGetTypeID(n) != CFBooleanGetTypeID(),n.doubleValue.isFinite else { return nil }
        return n.doubleValue
    }
    static func json(_ value: Any) -> String {
        guard let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys,.prettyPrinted]), let text = String(data:data,encoding:.utf8) else { return "Unreadable data" }
        return text
    }
    static func same(_ a: Any, _ b: Any) -> Bool { json(a) == json(b) }
    static func rows(_ value:[String:Any], key:String, idKey:String) throws -> [[String:Any]] {
        guard let rows = value[key] as? [[String:Any]], rows.count <= 1000 else { throw NativeAgentFailure("The specialist service returned an unreadable inventory.") }
        let ids = rows.compactMap { $0[idKey] as? String }
        guard ids.count == rows.count, ids.allSatisfy({ !$0.isEmpty && $0.count <= 256 }), Set(ids).count == ids.count else { throw NativeAgentFailure("The inventory contains missing or duplicate identifiers; changes are blocked.") }
        return rows
    }
    static func manifest(_ body:[String:Any]) throws -> [String:Any] {
        guard let name = body["name"] as? String, !name.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty, name.count <= 256,
              let prompt = body["system_prompt"] as? String, !prompt.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty, prompt.utf8.count <= 65536,
              let id = body["agent_id"] as? String, !id.isEmpty, id.count <= 256,
              let permissions = body["tool_permissions"] as? [String], permissions.count <= 256, permissions.allSatisfy({ !$0.isEmpty && $0.count <= 512 }) else { throw NativeAgentFailure("Enter an explicit specialist ID, name, system prompt and tool permission list.") }
        // These are the fields the registration route actually consumes. Keep optional
        // nulls intact, and never silently pretend version/tags/unknown keys were applied.
        let fields = ["agent_id","name","description","system_prompt","tool_permissions","source_pattern","schedule","memory_filter"]
        for key in ["description","source_pattern","schedule","memory_filter"] {
            if let value = body[key], !(value is String), !(value is NSNull) { throw NativeAgentFailure("Optional specialist fields must be text or null.") }
        }
        return body.filter { fields.contains($0.key) }
    }
}
final class NativeAgentRedirectGuard: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session:URLSession,task:URLSessionTask,willPerformHTTPRedirection response:HTTPURLResponse,newRequest request:URLRequest,completionHandler:@escaping(URLRequest?) -> Void) { completionHandler(nil) }
    static func session() -> URLSession {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 30; config.timeoutIntervalForResource = 180
        return URLSession(configuration:config,delegate:NativeAgentRedirectGuard(),delegateQueue:nil)
    }
}
struct NativeAgentClient {
    let baseURL:URL
    let session:URLSession
    func request(_ path:String,method:String = "GET",body:[String:Any]? = nil) async throws -> [String:Any] {
        guard baseURL.scheme == "http", ["127.0.0.1","::1","[::1]"].contains(baseURL.host ?? ""),baseURL.user == nil,baseURL.password == nil,
              var components = URLComponents(url:baseURL,resolvingAgainstBaseURL:false) else { throw NativeAgentFailure("Specialists require the app’s local service.") }
        components.percentEncodedPath = path;components.query = nil;components.fragment = nil
        guard let url = components.url else { throw NativeAgentFailure("Invalid specialist request.") }
        var request = URLRequest(url:url);request.httpMethod = method
        if let body = body { request.httpBody = try JSONSerialization.data(withJSONObject:body);request.setValue("application/json",forHTTPHeaderField:"Content-Type") }
        let (data,response) = try await session.data(for:request)
        guard let http = response as? HTTPURLResponse,(200..<300).contains(http.statusCode) else { throw NativeAgentFailure("Specialist request failed (HTTP \((response as? HTTPURLResponse)?.statusCode ?? 0)). Details withheld because they can contain private prompts.") }
        guard let value = (try? JSONSerialization.jsonObject(with:data)) as? [String:Any] else { throw NativeAgentFailure("The specialist service returned invalid JSON.") }
        guard value["error"] == nil, NativeAgentWire.bool(value["success"]) != false else { throw NativeAgentFailure("The service did not confirm this specialist action.") }
        return value
    }
}
struct NativeAgentRow: Identifiable {
    let id:String
    let raw:[String:Any]
    var title:String { raw["name"] as? String ?? id }
}
enum NativeAgentAction {
    case persona(String), proposal(String), custom([String:Any]), feedback(id:String,positive:Bool)
}
struct NativeAgentReview: Identifiable {
    let id = UUID()
    let generation:UUID
    let action:NativeAgentAction
    let source:[String:Any]
    let existing:[String:Any]?
    let title:String
    let explanation:String
}
@MainActor final class NativeAgentModel: ObservableObject {
    @Published private(set) var inventories:[String:[[String:Any]]] = [:]
    @Published private(set) var errors:[String:String] = [:]
    @Published private(set) var statistics:[String:Any]?
    @Published private(set) var loading = false
    @Published private(set) var acting = false
    @Published private(set) var actionError:String?
    @Published private(set) var receipt:String?
    private var client:NativeAgentClient?
    private let session:URLSession
    private var generation = UUID(), read = UUID()
    private var consumed:Set<UUID> = []
    static let paths = ["personas":"/api/agents/personas","agents":"/api/agents/list","proposals":"/api/agents/proposals"]
    init(session:URLSession? = nil) { self.session = session ?? NativeAgentRedirectGuard.session() }
    var available:Bool { client != nil }
    func configure(baseURL:URL?) async {
        generation = UUID();read = UUID();consumed = [];client = baseURL.map { NativeAgentClient(baseURL:$0,session:session) }
        inventories = [:];errors = [:];statistics = nil;loading = false;acting = false;actionError = nil;receipt = nil
        if available { await refresh() }
    }
    func rows(_ key:String) -> [NativeAgentRow] {
        let idKey = key == "proposals" ? "pattern_id" : "agent_id"
        return (inventories[key] ?? []).compactMap { row in (row[idKey] as? String).map { NativeAgentRow(id:$0,raw:row) } }
    }
    func refresh() async {
        guard let client = client,!acting else { return }
        generation = UUID();read = UUID();let revision = read, connection = generation
        loading = true
        defer { if revision == read && connection == generation { loading = false } }
        for key in Self.paths.keys.sorted() {
            do {
                let value = try await client.request(Self.paths[key]!)
                let rows = try NativeAgentWire.rows(value,key:key,idKey:key == "proposals" ? "pattern_id" : "agent_id")
                guard revision == read,connection == generation else { return }
                inventories[key] = rows;errors[key] = nil
            } catch { guard revision == read,connection == generation else { return };inventories[key] = nil;errors[key] = "Could not read \(key). Refresh before making changes." }
        }
        do {
            let value = try await client.request("/api/agents/stats")
            guard revision == read,connection == generation else { return }
            statistics = value;errors["stats"] = nil
        } catch { guard revision == read,connection == generation else { return };statistics = nil;errors["stats"] = "Runtime statistics could not be read." }
    }
    func review(_ action:NativeAgentAction) throws -> NativeAgentReview {
        guard available,!loading,!acting,inventories["agents"] != nil else { throw NativeAgentFailure("Refresh the specialist inventory before reviewing a change.") }
        var source:[String:Any], existing:[String:Any]?, title:String, explanation:String
        switch action {
        case .persona(let id):
            guard let row = rows("personas").first(where:{$0.id == id}) else { throw NativeAgentFailure("Refresh this persona before registering it.") }
            source = try NativeAgentWire.manifest(row.raw);existing = rows("agents").first(where:{$0.id == id})?.raw
            title = existing == nil ? "Register \(row.title)?" : "Replace \(row.title)?"
            explanation = "Creates a persisted specialist definition from this exact persona; no model call is needed for registration. It does not start a task or prove automatic routing. Existing core policy still applies to tools."
        case .custom(let body):
            source = try NativeAgentWire.manifest(body);let id = source["agent_id"] as! String
            existing = rows("agents").first(where:{$0.id == id})?.raw
            title = existing == nil ? "Register custom specialist?" : "Replace existing specialist?"
            explanation = "Stores this prompt, tool permissions, memory filter and schedule metadata. Registration does not execute a task or arm a scheduler. Only the fields displayed below are sent; additional manifest keys are not supported by the backend route."
        case .proposal(let id):
            guard let row = rows("proposals").first(where:{$0.id == id}),let tools = row.raw["tools"] as? [String],let topic = row.raw["topic"] as? String,!topic.isEmpty,
                  row.raw["sample_prompts"] is [String] else { throw NativeAgentFailure("Refresh this recurring-pattern proposal before using it.") }
            source = row.raw;existing = rows("agents").first(where:{$0.id == "specialist_" + topic})?.raw
            title = "Generate and register \(row.title)?"
            explanation = "Calls the configured AI provider with this topic, tools (\(tools.joined(separator:", "))) and sample prompts below. This can spend provider budget and send those prompts to a cloud provider/fallback. The generated prompt cannot be inspected before this endpoint persists it. No task is executed; generated schedule metadata does not prove a scheduler is armed."
        case .feedback(let id,let positive):
            guard let row = rows("agents").first(where:{$0.id == id}),let tasks = NativeAgentWire.number(row.raw["tasks"]),tasks >= 0,tasks < Double(Int.max),tasks.rounded(.towardZero) == tasks,
                  let score = NativeAgentWire.number(row.raw["satisfaction"]),(0...1).contains(score) else { throw NativeAgentFailure("Refresh this specialist’s feedback counters first.") }
            source = row.raw;existing = row.raw;title = "Record \(positive ? "positive" : "negative") feedback?"
            explanation = "Records feedback for \(row.title), increments its backend task counter and adjusts its satisfaction score. This is your rating, not verification that a task succeeded."
        }
        if existing != nil && !(action.isFeedback) { explanation += " WARNING: the same-ID registration replaces an existing specialist definition and resets its runtime counters; this is not a second agent. Existing row: \(NativeAgentWire.json(existing!))." }
        if !action.isFeedback { explanation += "\nExact reviewed definition/proposal:\n" + NativeAgentWire.json(source) }
        explanation += "\nFresh reads are checked before sending. The backend has no atomic revision guard, so a simultaneous change by another client can still race the write."
        return NativeAgentReview(generation:generation,action:action,source:source,existing:existing,title:title,explanation:explanation)
    }
    func perform(_ review:NativeAgentReview) async -> Bool {
        guard let client = client,review.generation == generation,!loading,!acting,!consumed.contains(review.id) else { actionError = "This review expired or was already used. Refresh and review again.";return false }
        consumed.insert(review.id);acting = true;actionError = nil;receipt = nil
        let connection = generation
        defer { if connection == generation { acting = false } }
        var dispatched = false
        do {
            let latest = try NativeAgentWire.rows(try await client.request("/api/agents/list"),key:"agents",idKey:"agent_id")
            guard connection == generation else { return false }
            let path:String,body:[String:Any],expectedID:String
            switch review.action {
            case .persona(let id):
                let rows = try NativeAgentWire.rows(try await client.request("/api/agents/personas"),key:"personas",idKey:"agent_id")
                guard let row = rows.first(where:{$0["agent_id"] as? String == id}),NativeAgentWire.same(try NativeAgentWire.manifest(row),review.source) else { throw NativeAgentFailure("The persona changed after review. Refresh and review its new permissions.") }
                expectedID = id;path = "/api/agents/spawn";body = review.source
            case .custom:
                expectedID = review.source["agent_id"] as! String;path = "/api/agents/spawn";body = review.source
            case .proposal(let id):
                let rows = try NativeAgentWire.rows(try await client.request("/api/agents/proposals"),key:"proposals",idKey:"pattern_id")
                guard let row = rows.first(where:{$0["pattern_id"] as? String == id}),NativeAgentWire.same(row,review.source) else { throw NativeAgentFailure("The proposal changed or disappeared. Review it again before contacting the model.") }
                expectedID = "specialist_" + (review.source["topic"] as! String);path = "/api/agents/spawn";body = ["pattern_id":id]
            case .feedback(let id,let positive):
                expectedID = id;path = "/api/agents/feedback";body = ["agent_id":id,"positive":positive]
            }
            let current = latest.first(where:{$0["agent_id"] as? String == expectedID})
            if let existing = review.existing {
                guard let current = current,NativeAgentWire.same(current,existing) else { throw NativeAgentFailure("The specialist changed after review. No write was sent.") }
            } else if current != nil { throw NativeAgentFailure("Another specialist now uses this ID. Review replacement explicitly; no write was sent.") }
            guard connection == generation else { return false }
            dispatched = true
            let result = try await client.request(path,method:"POST",body:body)
            guard connection == generation else { return false }
            guard NativeAgentWire.bool(result["success"]) == true else { throw NativeAgentFailure("The service returned no explicit success receipt.") }
            if !review.action.isFeedback {
                guard let agent = result["agent"] as? [String:Any],agent["agent_id"] as? String == expectedID,
                      result["source"] as? String == (review.action.isProposal ? "pattern" : "persona_manifest") else { throw NativeAgentFailure("The registration receipt did not identify the reviewed specialist.") }
                if !review.action.isProposal {
                    guard NativeAgentWire.same(agent["tool_permissions"] ?? [],review.source["tool_permissions"] ?? []),
                          NativeAgentWire.same(["value":agent["memory_filter"] ?? NSNull()],["value":review.source["memory_filter"] ?? NSNull()]) else { throw NativeAgentFailure("The registration receipt does not match the reviewed tool or memory scope.") }
                }
            }
            let readback = try NativeAgentWire.rows(try await client.request("/api/agents/list"),key:"agents",idKey:"agent_id")
            guard connection == generation else { return false }
            guard let stored = readback.first(where:{$0["agent_id"] as? String == expectedID}) else { throw NativeAgentFailure("The service acknowledged the action but the specialist could not be read back.") }
            if case .feedback(_,let positive) = review.action {
                let tasks = (review.source["tasks"] as? NSNumber)?.intValue ?? -1
                let score = (review.source["satisfaction"] as? NSNumber)?.doubleValue ?? -1
                let expectedScore = max(0,min(1,score + (positive ? 0.05 : -0.1)))
                guard NativeAgentWire.number(stored["tasks"]) == Double(tasks + 1),let actual = NativeAgentWire.number(stored["satisfaction"]),abs(actual - expectedScore) < 0.000001 else { throw NativeAgentFailure("Feedback receipt arrived, but the expected counter/score change could not be verified.") }
                receipt = "Feedback counters verified for \(expectedID). This does not verify task execution."
            } else {
                let tools = review.action.isProposal ? review.source["tools"] : review.source["tool_permissions"]
                guard NativeAgentWire.same(stored["tools"] ?? [],tools ?? []) else { throw NativeAgentFailure("Registered specialist tool scope differs from the review.") }
                if !review.action.isProposal {
                    guard stored["name"] as? String == review.source["name"] as? String,
                          NativeAgentWire.same(["value":stored["schedule"] ?? NSNull()],["value":review.source["schedule"] ?? NSNull()]) else { throw NativeAgentFailure("Registered name/schedule differs from the review.") }
                }
                receipt = "Specialist definition \(expectedID) registered and tool scope read back. Prompt/memory-filter persistence is not exposed by the list endpoint; no task was started."
            }
            inventories["agents"] = readback;generation = UUID();acting = false
            return true
        } catch {
            guard connection == generation else { return false }
            let detail = (error as? NativeAgentFailure)?.message ?? "The local specialist request failed."
            actionError = detail + (dispatched ? " The write may already have happened; refresh before deciding to retry." : "")
            return false
        }
    }
}
private extension NativeAgentAction {
    var isFeedback:Bool { if case .feedback = self { return true };return false }
    var isProposal:Bool { if case .proposal = self { return true };return false }
}

struct NativeAgentFeatureView: View {
    let baseURL:URL?
    let sessionID:String?
    @StateObject private var model = NativeAgentModel()
    @State private var tab = "personas"
    @State private var review:NativeAgentReview?
    @State private var localError:String?
    @State private var custom = "{\n  \"agent_id\": \"my-specialist\",\n  \"name\": \"My specialist\",\n  \"description\": \"\",\n  \"system_prompt\": \"Help with a specific task.\",\n  \"tool_permissions\": [],\n  \"schedule\": null,\n  \"memory_filter\": null\n}"
    init(baseURL:URL?,sessionID:String? = nil) { self.baseURL = baseURL;self.sessionID = sessionID }
    var body:some View {
        VStack(alignment:.leading,spacing:14) {
            HStack {
                Text("Specialists").font(.title2.bold());Spacer()
                Button("Refresh") { Task { await model.refresh() } }.disabled(!model.available || model.loading || model.acting)
            }
            Text("Browse persona templates and persisted specialist definitions. Registration does not mean an agent is running, and it does not start a task.").font(.callout).foregroundStyle(.secondary)
            if !model.available { Text("Waiting for the app’s local service.").foregroundStyle(.secondary) }
            Picker("Section",selection:$tab) { Text("Personas").tag("personas");Text("Registered").tag("agents");Text("Proposals").tag("proposals");Text("Custom").tag("custom");Text("Runtime").tag("stats") }.pickerStyle(.segmented).disabled(model.acting)
            if model.loading || model.acting { ProgressView(model.acting ? "Checking review and service receipt…" : "Reading specialist inventory…") }
            if let error = localError ?? model.actionError { Text(error).foregroundStyle(.red).textSelection(.enabled) }
            if let receipt = model.receipt { Text(receipt).foregroundStyle(.secondary).textSelection(.enabled) }
            ScrollView {
                VStack(alignment:.leading,spacing:14) {
                    if tab == "custom" { customForm }
                    else if tab == "stats" { runtime }
                    else {
                        if let error = model.errors[tab] { Text(error).foregroundStyle(.red) }
                        else if model.inventories[tab] == nil { Text("Inventory not read yet.").foregroundStyle(.secondary) }
                        else if model.rows(tab).isEmpty { Text("The service returned no \(tab). An empty list alone does not establish that Agent Mitosis is initialized.").foregroundStyle(.secondary) }
                        ForEach(model.rows(tab)) { row in specialistCard(row) }
                    }
                }.frame(maxWidth:.infinity,alignment:.leading)
            }
        }.padding(20)
        .task(id:baseURL?.absoluteString) { review = nil;localError = nil;await model.configure(baseURL:baseURL) }
        .onChange(of:sessionID) { _ in review = nil }
        .sheet(item:$review) { item in
            VStack(alignment:.leading,spacing:16) {
                Text(item.title).font(.title2.bold())
                ScrollView { Text(item.explanation).font(.callout).textSelection(.enabled).frame(maxWidth:.infinity,alignment:.leading) }
                HStack { Spacer();Button("Cancel") { review = nil };Button("Confirm reviewed change") { review = nil;Task { _ = await model.perform(item) } }.disabled(model.loading || model.acting || !model.available) }
            }.padding(24).frame(width:680,height:540)
        }
    }
    private var customForm:some View {
        VStack(alignment:.leading,spacing:10) {
            Text("Define a specialist").font(.headline)
            Text("Use an explicit ID. Tool permissions are real scope, not decorative tags. Same-ID registration replaces the existing definition. Schedule is stored metadata; no scheduler activation is promised.").foregroundStyle(.secondary)
            NativePlainTextEditor(text:$custom, label:"Specialist definition JSON", monospaced:true).frame(minHeight:300).disabled(model.acting)
            Button("Review registration…") {
                do {
                    guard let data = custom.data(using:.utf8),let body = try JSONSerialization.jsonObject(with:data) as? [String:Any] else { throw NativeAgentFailure("Enter a JSON object.") }
                    prepare(.custom(body))
                } catch { localError = (error as? NativeAgentFailure)?.message ?? "Enter valid JSON. No request was sent." }
            }.disabled(!model.available || model.loading || model.acting)
        }
    }
    private var runtime:some View {
        VStack(alignment:.leading,spacing:10) {
            Text("Backend counters").font(.headline)
            Text("specialists_active counts registered definitions, not running processes. total_tasks is a feedback counter. The API exposes no specialist run/pause/delete operation here.").foregroundStyle(.secondary)
            if let error = model.errors["stats"] { Text(error).foregroundStyle(.red) }
            if let stats = model.statistics {
                ForEach(["patterns_tracked","proposals_ready","specialists_active","total_tasks"],id:\.self) { key in
                    HStack { Text(key.replacingOccurrences(of:"_",with:" "));Spacer();Text((stats[key] as? NSNumber)?.stringValue ?? "Not reported") }
                }
            } else { Text("Runtime counters not read.") }
        }
    }
    private func specialistCard(_ row:NativeAgentRow) -> some View {
        VStack(alignment:.leading,spacing:9) {
            Text(row.title).font(.headline)
            Text(row.id).font(.caption.monospaced()).foregroundStyle(.secondary)
            if let description = row.raw["description"] as? String { Text(description).textSelection(.enabled) }
            let tools = (row.raw["tool_permissions"] ?? row.raw["tools"]) as? [String] ?? []
            Text("Tool scope: " + (tools.isEmpty ? "No tools listed" : tools.joined(separator:", "))).font(.callout).textSelection(.enabled)
            if let schedule = row.raw["schedule"] as? String { Text("Schedule metadata: \(schedule)").font(.caption) }
            DisclosureGroup("Definition and observed metadata") { Text(NativeAgentWire.json(row.raw)).font(.system(.caption,design:.monospaced)).textSelection(.enabled) }
            HStack {
                if tab == "personas" { Button("Review registration…") { prepare(.persona(row.id)) } }
                else if tab == "proposals" { Button("Review model generation…") { prepare(.proposal(row.id)) } }
                else { Button("Positive feedback…") { prepare(.feedback(id:row.id,positive:true)) };Button("Negative feedback…") { prepare(.feedback(id:row.id,positive:false)) } }
            }.disabled(!model.available || model.loading || model.acting)
        }.padding(14).frame(maxWidth:.infinity,alignment:.leading).background(Color.secondary.opacity(0.06),in:RoundedRectangle(cornerRadius:12))
    }
    private func prepare(_ action:NativeAgentAction) {
        do { localError = nil;review = try model.review(action) }
        catch { localError = (error as? NativeAgentFailure)?.message ?? "This change could not be reviewed." }
    }
}
