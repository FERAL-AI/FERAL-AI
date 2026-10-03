import SwiftUI
import Foundation
import CoreFoundation

struct NativeWorkflowFailure: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}
enum NativeWorkflowWire {
    static func bool(_ value: Any?) -> Bool? { guard let n = value as? NSNumber, CFGetTypeID(n) == CFBooleanGetTypeID() else { return nil }; return n.boolValue }
    static func id(_ value: Any?) -> String? {
        if let s = value as? String, !s.isEmpty { return s }
        if let n = value as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID(), n.doubleValue > 0, n.doubleValue < Double(Int.max), n.doubleValue.rounded(.towardZero) == n.doubleValue { return n.stringValue }; return nil
    }
    static func segment(_ value: String) -> String { value.addingPercentEncoding(withAllowedCharacters:CharacterSet(charactersIn:"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")) ?? "" }
    static func json(_ value: Any) -> String { guard let data = try? JSONSerialization.data(withJSONObject:value,options:[.prettyPrinted,.sortedKeys]),let text = String(data:data,encoding:.utf8) else { return String(describing:value) }; return text }
    static func routineID(_ value:Any?) -> String? {
        guard let id = id(value),let number = Int(id),number > 0,String(number) == id else { return nil };return id
    }
    static func terms(_ row:[String:Any]) throws -> Data {
        let stable = row.filter { !["last_run","next_run","run_count"].contains($0.key) }
        let data = try JSONSerialization.data(withJSONObject:stable,options:[.sortedKeys])
        guard data.count <= 32_000 else { throw NativeWorkflowFailure("This schedule is too large to review safely.") };return data
    }
    static func automationText(minutes:Int,action:String) throws -> String {
        guard (1...10_080).contains(minutes),!action.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty,action.utf8.count <= 8_000,
              !action.unicodeScalars.contains(where:{CharacterSet.controlCharacters.contains($0) && $0 != "\n" && $0 != "\t"}) else {
            throw NativeWorkflowFailure("Choose 1–10080 minutes and describe the task in no more than 8000 bytes.")
        }
        return "every \(minutes) minutes, " + action
    }
    static func steps(_ body: [String: Any]) throws -> [[String: Any]] {
        guard let title = body["title"] as? String, !title.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty,
              let steps = body["steps"] as? [[String:Any]], !steps.isEmpty, steps.count <= 50 else { throw NativeWorkflowFailure("Enter a title and 1–50 steps.") }
        for step in steps {
            guard let type = step["type"] as? String, ["noop","sleep","note.save","wiki.compile","memory.search","http.get","skill.invoke","llm.chat","condition"].contains(type) else { throw NativeWorkflowFailure("A workflow step has an unsupported type.") }
            if type == "llm.chat", (step["prompt"] as? String)?.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty != false { throw NativeWorkflowFailure("An AI step requires a prompt.") }
            if type == "note.save", (step["content"] as? String)?.isEmpty != false { throw NativeWorkflowFailure("A note step requires content.") }
            if type == "sleep", let n = step["seconds"] as? NSNumber, n.doubleValue <= 0 { throw NativeWorkflowFailure("A wait step requires positive seconds.") }
        }
        return steps
    }
}
final class NativeWorkflowRedirectGuard: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session:URLSession,task:URLSessionTask,willPerformHTTPRedirection response:HTTPURLResponse,newRequest request:URLRequest,completionHandler:@escaping(URLRequest?) -> Void) { completionHandler(nil) }
    static func session() -> URLSession { let config = URLSessionConfiguration.ephemeral; config.timeoutIntervalForRequest = 30; config.timeoutIntervalForResource = 300; return URLSession(configuration:config,delegate:NativeWorkflowRedirectGuard(),delegateQueue:nil) }
}
struct NativeWorkflowClient {
    let baseURL: URL
    let session: URLSession
    func request(_ path:String,method:String = "GET",body:[String:Any]? = nil,missingRoutineOK:Bool = false) async throws -> [String:Any] {
        guard baseURL.scheme == "http", ["127.0.0.1","::1","[::1]"].contains(baseURL.host ?? ""),baseURL.user == nil,baseURL.password == nil,var components = URLComponents(url:baseURL,resolvingAgainstBaseURL:false) else { throw NativeWorkflowFailure("Workflows are available only from the app’s local service.") }
        components.percentEncodedPath = path; components.query = nil; components.fragment = nil
        guard let url = components.url else { throw NativeWorkflowFailure("Invalid workflow request.") }
        var request = URLRequest(url:url); request.httpMethod = method; request.timeoutInterval = 180
        if let body = body { request.httpBody = try JSONSerialization.data(withJSONObject:body); request.setValue("application/json",forHTTPHeaderField:"Content-Type") }
        let (data,response) = try await session.data(for:request)
        let value = (try? JSONSerialization.jsonObject(with:data)) as? [String:Any]
        guard let http = response as? HTTPURLResponse,(200..<300).contains(http.statusCode) else { throw NativeWorkflowFailure("Workflow request failed (\((response as? HTTPURLResponse)?.statusCode ?? 0)): \(value?["error"] ?? value?["detail"] ?? "No valid response")") }
        guard let value = value else { throw NativeWorkflowFailure("The workflow service returned invalid JSON.") }
        if let error = value["error"] as? String, !error.isEmpty, !(value["id"] is String && value["status"] is String) {
            if missingRoutineOK,method == "GET",path == "/api/routines/" + (NativeWorkflowWire.routineID(String(path.split(separator:"/").last ?? "")) ?? ""),error == "Routine not found" { return value }
            throw NativeWorkflowFailure(error)
        }
        if NativeWorkflowWire.bool(value["ok"]) == false || NativeWorkflowWire.bool(value["success"]) == false { throw NativeWorkflowFailure("The workflow service did not confirm the action.") }
        return value
    }
}
struct NativeWorkflowRow: Identifiable {
    let id: String
    let raw: [String:Any]
    var title: String { raw["name"] as? String ?? raw["title"] as? String ?? raw["description"] as? String ?? raw["intent"] as? String ?? raw["action"] as? String ?? id }
}
enum NativeWorkflowAction {
    case pack(id:String,context:[String:Any])
    case createFlow([String:Any]), flow(id:String,verb:String)
    case loadRoutines, createRoutine([String:Any]), routine(id:String,verb:String)
    case createAutomation(minutes:Int,action:String), deleteAutomation(String)
    case compile(String), complete(plan:String,action:String,result:String)
}
struct NativeWorkflowReview: Identifiable {
    let id = UUID()
    let generation: UUID
    let contextRevision:UUID
    let action: NativeWorkflowAction
    let title: String
    let explanation: String
    let expiresAt:Date
    let expected:[String:Any]?
}
@MainActor final class NativeWorkflowModel: ObservableObject {
    @Published private(set) var payloads:[String:[String:Any]] = [:]
    @Published private(set) var errors:[String:String] = [:]
    @Published private(set) var loading = false
    @Published private(set) var acting = false
    @Published private(set) var actionError:String?
    @Published private(set) var receipt:String?
    @Published private(set) var compiledPlan:[String:Any]?
    @Published private(set) var detail:[String:Any]?
    @Published private(set) var detailError:String?
    @Published private(set) var inspecting = false
    @Published var selectedFlow = ""
    private var client:NativeWorkflowClient?
    private var contextGate = NativeContextActionGate()
    @Published private(set) var contextPolicy = NativeSelectedContextPolicy.legacy
    func setContextPolicy(_ policy:NativeSelectedContextPolicy) {
        if contextGate.update(policy) { contextPolicy = policy }
    }
    private let session:URLSession
    private var generation = UUID(), readGeneration = UUID(), detailGeneration = UUID(), operation = UUID()
    private var sessionID:String?
    private var issued:[UUID:Date] = [:]
    private var automationDetails:[String:[String:Any]] = [:]
    private let now:() -> Date
    static let paths = ["packs":"/api/workflows/packs","flows":"/api/taskflows","plans":"/api/intents/list","today":"/api/intents/today","stats":"/api/intents/stats","automations":"/api/automations"]
    init(session:URLSession? = nil,now:@escaping() -> Date = Date.init) { self.session = session ?? NativeWorkflowRedirectGuard.session();self.now = now }
    var available:Bool { client != nil }
    func configure(baseURL:URL?,sessionID:String?) async {
        generation = UUID(); readGeneration = UUID(); detailGeneration = UUID(); operation = UUID()
        client = baseURL.map { NativeWorkflowClient(baseURL:$0,session:session) }; self.sessionID = sessionID
        payloads = [:]; errors = [:]; loading = false; acting = false; actionError = nil; receipt = nil; compiledPlan = nil; detail = nil; detailError = nil; selectedFlow = ""; inspecting = false;issued = [:];automationDetails = [:]
        if available { await refresh() }
    }
    func refresh() async {
        guard let client = client else { return }
        readGeneration = UUID(); let read = readGeneration, connection = generation
        loading = true
        defer { if read == readGeneration && connection == generation { loading = false } }
        // Deliberately exclude /api/routines: that GET can restart the scheduler.
        for key in Self.paths.keys.sorted() {
            guard read == readGeneration && connection == generation else { return }
            do {
                let value = try await client.request(Self.paths[key]!)
                try validateList(key,value)
                guard read == readGeneration && connection == generation else { return }; payloads[key] = value; errors[key] = nil
            } catch { if read == readGeneration && connection == generation { payloads[key] = nil; errors[key] = error.localizedDescription } }
        }
    }
    private func validateList(_ resource:String,_ value:[String:Any]) throws {
        guard resource != "stats" else { return }
        let key = resource == "today" ? "actions" : resource
        guard let rows = value[key] as? [[String:Any]] else { throw NativeWorkflowFailure("This section returned an unreadable list.") }
        let idKey = resource == "packs" ? "workflow_id" : resource == "plans" ? "plan_id" : resource == "today" ? "action_id" : "id"
        guard rows.allSatisfy({ NativeWorkflowWire.id($0[idKey]) != nil }) else { throw NativeWorkflowFailure("This section contains an unreadable identifier.") }
        if ["routines","automations"].contains(resource) {
            guard rows.count <= 500,rows.allSatisfy({ NativeWorkflowWire.routineID($0["id"]) != nil }),Set(rows.compactMap { NativeWorkflowWire.routineID($0["id"]) }).count == rows.count else { throw NativeWorkflowFailure("This schedule inventory is unreadable or too large.") }
        }
    }
    func rows(_ resource:String) -> [NativeWorkflowRow] {
        let key = resource == "today" ? "actions" : resource
        let idKey = resource == "packs" ? "workflow_id" : resource == "plans" ? "plan_id" : resource == "today" ? "action_id" : "id"
        return (payloads[resource]?[key] as? [[String:Any]] ?? []).compactMap { raw in NativeWorkflowWire.id(raw[idKey]).map { NativeWorkflowRow(id:$0,raw:raw) } }
    }
    func inspectFlow() async {
        detailGeneration = UUID(); let request = detailGeneration, connection = generation, id = selectedFlow
        detail = nil; detailError = nil; inspecting = false
        guard !id.isEmpty, let client = client else { return }; inspecting = true
        defer { if request == detailGeneration && connection == generation { inspecting = false } }
        do {
            let value = try await client.request("/api/taskflows/" + NativeWorkflowWire.segment(id))
            guard value["id"] as? String == id, value["status"] is String,value["steps"] is [[String:Any]] else { throw NativeWorkflowFailure("The service did not return the selected flow and its steps.") }
            guard request == detailGeneration && connection == generation && selectedFlow == id else { return }; detail = value
        } catch { if request == detailGeneration && connection == generation && selectedFlow == id { detailError = error.localizedDescription } }
    }
    func prepareAutomationDeletion(_ id:String) async throws -> NativeWorkflowReview {
        guard let client,!loading,!acting,NativeWorkflowWire.routineID(id) != nil,let row = rows("automations").first(where:{$0.id == id}) else { throw NativeWorkflowFailure("Refresh this scheduled automation before reviewing deletion.") }
        let connection = generation
        let value = try await client.request("/api/routines/" + id)
        guard connection == generation,!loading,!acting else { throw NativeWorkflowFailure("The connection changed. Review again.") }
        let detail = try checkedRoutine(value,id:id)
        try validateAutomation(detail,inventory:row.raw)
        automationDetails[id] = detail
        return try review(.deleteAutomation(id))
    }
    private func checkedRoutine(_ value:[String:Any],id:String) throws -> [String:Any] {
        guard let row = value["routine"] as? [String:Any],NativeWorkflowWire.routineID(row["id"]) == id,row["session_id"] is String,row["job_type"] is String,row["cron_expr"] is String,row["payload"] is [String:Any],NativeWorkflowWire.bool(row["enabled"]) != nil else { throw NativeWorkflowFailure("The exact schedule and action could not be read back.") };return row
    }
    private func validateAutomation(_ row:[String:Any],inventory:[String:Any]? = nil) throws {
        guard let sessionID,!sessionID.isEmpty,row["session_id"] as? String == sessionID,row["job_type"] as? String == "custom" else { throw NativeWorkflowFailure("This automation is not a CUSTOM schedule owned by the selected conversation.") }
        if let inventory {
            guard NativeWorkflowWire.routineID(row["id"]) == NativeWorkflowWire.routineID(inventory["id"]),row["cron_expr"] as? String == inventory["cron"] as? String,row["description"] as? String == inventory["description"] as? String,NativeWorkflowWire.bool(row["enabled"]) == NativeWorkflowWire.bool(inventory["enabled"]) else { throw NativeWorkflowFailure("The automation changed since its inventory was loaded. Refresh and review again.") }
        }
    }
    func review(_ action:NativeWorkflowAction) throws -> NativeWorkflowReview {
        let reviewed = try makeReview(action)
        issued = issued.filter { $0.value >= now() }
        guard issued.count < 100 else { throw NativeWorkflowFailure("Too many outstanding reviews. Reconnect before reviewing more actions.") }
        issued[reviewed.id] = reviewed.expiresAt;return reviewed
    }
    func startsWork(_ action:NativeWorkflowAction)->Bool {
        switch action {
        case .pack,.createFlow,.loadRoutines,.createRoutine,.createAutomation,.compile:return true
        case .flow(_,let verb),.routine(_,let verb):return verb == "resume"
        case .deleteAutomation,.complete:return false
        }
    }
    private func assertContext(_ reviewed:NativeWorkflowReview) throws {
        guard contextGate.accepts(reviewed.contextRevision),!startsWork(reviewed.action) || contextPolicy.taskReady else { throw NativeWorkflowFailure("Selected chat, connection or readiness changed. No new work can be started from this review; inspect stored state before another attempt.") }
    }
    private func storedScope(_ row:[String:Any]?,kind:String) -> String {
        guard let row,let sid = row["session_id"] as? String else { return "Owner not confirmed by this response; no conversation scope was reassigned." }
        if !sid.isEmpty { return "Response identifies session \(sid). No existing conversation was converted." }
        guard let id = NativeWorkflowWire.id(row["id"]) else { return "Response reports a blank session; the dedicated execution owner is not confirmed." }
        return "Response reports a blank session; execution uses the existing \(kind)-\(id) fallback. No existing conversation was converted."
    }
    private func reviewScope(_ action:NativeWorkflowAction) -> String {
        switch action {
        case .pack(let id,_):return "This request omits a session; the backend defaults to pack-\(id), independently of the selected chat."
        case .createFlow(let body):
            if let sid = body["session_id"] as? String,!sid.isEmpty { return "Requested session: \(sid). The response must be inspected to confirm ownership." }
            return "This request uses a blank session; execution defaults to taskflow-{new flow ID}, independently of the selected chat."
        case .flow(let id,_):return storedScope(rows("flows").first(where:{$0.id == id})?.raw,kind:"taskflow")
        case .createRoutine(let body):
            if let sid = body["session_id"] as? String,!sid.isEmpty { return "Requested session: \(sid). The response must be inspected to confirm ownership." }
            return "This request uses a blank session; execution defaults to routine-{new routine ID}, independently of the selected chat."
        case .routine(let id,_):return storedScope(rows("routines").first(where:{$0.id == id})?.raw,kind:"routine")
        case .createAutomation:return "This background schedule explicitly targets the selected conversation \(sessionID ?? ""). Creation does not convert existing chat context."
        case .deleteAutomation(let id):return storedScope(automationDetails[id],kind:"routine")
        case .loadRoutines:return "Global scheduler and routine inventory; enabled schedules may belong to other sessions."
        case .compile,.complete:return "Global intent-plan metadata. This request supplies no selected-chat session."
        }
    }
    private func makeReview(_ action:NativeWorkflowAction) throws -> NativeWorkflowReview {
        guard !startsWork(action) || contextPolicy.taskReady else { throw NativeWorkflowFailure("Verify the selected chat before starting work. Inspection, cancellation and pause controls remain available.") }
        guard available,!loading,!acting else { throw NativeWorkflowFailure("Wait for the local workflow service to finish loading.") }
        let title:String, explanation:String
        var expected:[String:Any]?
        switch action {
        case .pack(let id,let context):
            guard let row = rows("packs").first(where:{$0.id == id}),let steps = row.raw["steps"] as? [[String:Any]],!steps.isEmpty else { throw NativeWorkflowFailure("Refresh this workflow pack before running it.") }
            title = "Start “\(row.title)” now?"; explanation = "Instantiates \(steps.count) steps as live queued background work, not a saved draft. Steps may use your configured AI provider, write memory/wiki, invoke skills or contact integrations under existing policy. Context: \(NativeWorkflowWire.json(context)). No success is promised before the flow runs."
        case .createFlow(let body):
            let steps = try NativeWorkflowWire.steps(body)
            title = "Start this background workflow?"; explanation = "Creates queued work immediately: \(steps.count) ordered steps. AI/skill steps can consume provider budget or perform tool/integration actions under existing policy. Closing this window does not stop the workflow. Review the steps: \(NativeWorkflowWire.json(steps))"
        case .flow(let id,let verb):
            guard let row = rows("flows").first(where:{$0.id == id}),let status = row.raw["status"] as? String,["resume","cancel"].contains(verb),!["completed","cancelled"].contains(status) else { throw NativeWorkflowFailure("Refresh this active flow before changing it.") }
            if verb == "resume", !["failed","waiting"].contains(status) { throw NativeWorkflowFailure("This flow is not failed or waiting; no resume is needed.") }
            title = verb == "resume" ? "Resume “\(row.title)” execution?" : "Cancel “\(row.title)” future steps?"
            explanation = verb == "resume" ? "Requeues the flow and resets failed/waiting steps for retry. This may repeat tool/provider actions; completed steps are retained." : "Marks the flow cancelled. It does not undo completed writes or guarantee that an already executing external request stops immediately."
        case .loadRoutines:
            title = "Load routines and check scheduler?"; explanation = "The backend’s routine-list endpoint can restart its scheduler. Existing enabled routines may then run. This creates no new routine, but is not guaranteed to be a passive read."
        case .createRoutine(let body):
            guard let schedule = body["cron_expr"] as? String,!schedule.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty,
                  let tz = body["tz_name"] as? String, TimeZone(identifier:tz) != nil,
                  ["prompt","skill"].contains(body["job_type"] as? String ?? ""),let payload = body["payload"] as? [String:Any] else { throw NativeWorkflowFailure("Enter a schedule, valid IANA timezone and an AI prompt or skill payload.") }
            if body["job_type"] as? String == "prompt", (payload["prompt"] as? String)?.isEmpty != false { throw NativeWorkflowFailure("Enter a routine prompt.") }
            if body["job_type"] as? String == "skill", (payload["skill"] as? String)?.isEmpty != false { throw NativeWorkflowFailure("Enter an installed skill ID.") }
            title = "Create an enabled routine?"; explanation = "Arms schedule “\(schedule)” in \(tz). \(NativeWorkflowWire.bool(body["recurring"]) == false ? "Runs once." : "Runs repeatedly.") It may use provider budget and execute tools/integrations without this window open, subject to core policy. Payload: \(NativeWorkflowWire.json(payload)). Schedule validity is checked by the server, not assumed here."
        case .routine(let id,let verb):
            guard NativeWorkflowWire.routineID(id) != nil,let row = rows("routines").first(where:{$0.id == id}),["pause","resume","delete"].contains(verb) else { throw NativeWorkflowFailure("Load and review the routine before changing it.") }
            expected = try checkedRoutine(["routine":row.raw],id:id);_ = try NativeWorkflowWire.terms(row.raw)
            title = "\(verb.capitalized) this routine?"
            let effect = verb == "resume" ? "Re-enables future scheduled execution, including provider/tool actions under existing policy." : verb == "delete" ? "Permanently deletes the schedule. It does not undo run history or guarantee that an in-flight run stops. There is no one-click undo." : "Disables future scheduled runs. An already running operation may continue."
            explanation = effect + " Exact routine \(id): \(row.title). Schedule: \(row.raw["cron_expr"] as? String ?? ""). Conversation: \(row.raw["session_id"] as? String ?? ""). Action: \(NativeWorkflowWire.json(row.raw["payload"] ?? [:]))."
        case .createAutomation(let minutes,let action):
            let text = try NativeWorkflowWire.automationText(minutes:minutes,action:action)
            guard let sessionID,(contextPolicy.sessionID == nil || contextPolicy.sessionID == sessionID),!sessionID.isEmpty,sessionID.utf8.count <= 1024,!sessionID.unicodeScalars.contains(where:{CharacterSet.controlCharacters.contains($0)}) else { throw NativeWorkflowFailure("Select a conversation before creating a scheduled automation.") }
            title = "Arm this scheduled automation?"
            explanation = "Repeats every \(minutes) minutes in conversation \(sessionID). The exact instruction sent to the existing scheduler is: \(text). It may consume provider budget and perform actions under core policy while this window is closed. This is a scheduled prompt, not an event trigger. Creation does not establish a completed run."
        case .deleteAutomation(let id):
            guard NativeWorkflowWire.routineID(id) != nil,let row = rows("automations").first(where:{$0.id == id}),let detail = automationDetails[id] else { throw NativeWorkflowFailure("Read this exact automation before reviewing deletion.") }
            try validateAutomation(detail,inventory:row.raw);expected = detail;_ = try NativeWorkflowWire.terms(detail)
            title = "Delete this scheduled automation?"
            explanation = "Permanently removes schedule \(id): \(detail["description"] as? String ?? id), \(detail["cron_expr"] as? String ?? ""). Conversation: \(detail["session_id"] as? String ?? ""). Exact action: \(NativeWorkflowWire.json(detail["payload"] ?? [:])). Already running work may continue; deletion does not undo effects."
        case .compile(let intent):
            guard !intent.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty else { throw NativeWorkflowFailure("Enter a goal before compiling a plan.") }
            title = "Compile this intent into a plan?"; explanation = "May send this goal to your configured AI provider and consume its budget: \(intent). It records a plan and suggestions; it does not execute its suggested tools."
        case .complete(let plan,let id,let result):
            guard rows("today").contains(where:{$0.id == id && $0.raw["plan_id"] as? String == plan}) else { throw NativeWorkflowFailure("Refresh today’s exact action before marking it done.") }
            title = "Record this action as completed?"; explanation = "Marks action \(id) in plan \(plan) completed, with result “\(result)”. This button does not execute the suggested tool or verify the work happened. No completion-undo endpoint exists."
        }
        return NativeWorkflowReview(generation:generation,contextRevision:contextGate.revision,action:action,title:title,explanation:explanation + "\nScope: " + reviewScope(action),expiresAt:now().addingTimeInterval(120),expected:expected)
    }
    func perform(_ reviewed:NativeWorkflowReview) async -> Bool {
        guard reviewed.generation == generation,contextGate.accepts(reviewed.contextRevision),let client = client else { actionError = "The agent connection changed. Review again."; return false }
        guard !loading,!acting,issued.removeValue(forKey:reviewed.id) == reviewed.expiresAt,now() <= reviewed.expiresAt else { actionError = "This review is expired, already used or no longer available. Review again.";return false }
        do { try assertContext(reviewed); _ = try makeReview(reviewed.action) } catch { actionError = error.localizedDescription; return false }
        let connection = generation; operation = UUID();let op = operation
        acting = true; actionError = nil; receipt = nil
        var mutationAttempted = false
        defer { if connection == generation && operation == op { acting = false } }
        do {
            let response:[String:Any], text:String
            var confirmedRoutine:[String:Any]?
            var scope = "Owner not confirmed by this response; no conversation scope was reassigned."
            switch reviewed.action {
            case .pack(let id,let context):
                try assertContext(reviewed)
                mutationAttempted = true
                response = try await client.request("/api/workflows/packs/" + NativeWorkflowWire.segment(id) + "/instantiate",method:"POST",body:["context":context])
                guard NativeWorkflowWire.bool(response["success"]) == true,response["workflow_id"] as? String == id,let flow = response["flow"] as? [String:Any], let created = NativeWorkflowWire.id(flow["id"]),flow["status"] is String else { throw NativeWorkflowFailure("The pack did not confirm a new flow.") }; text = "Pack instantiated as \(created). Runtime state: \(flow["status"]!). This is not proof of completed execution.";scope = storedScope(flow,kind:"taskflow")
            case .createFlow(let body):
                try assertContext(reviewed)
                mutationAttempted = true
                response = try await client.request("/api/taskflows",method:"POST",body:body)
                guard let id = NativeWorkflowWire.id(response["id"]),response["title"] as? String == body["title"] as? String,response["status"] is String else { throw NativeWorkflowFailure("Workflow creation was not confirmed.") }; text = "Workflow \(id) created. Runtime state: \(response["status"]!).";scope = storedScope(response,kind:"taskflow")
            case .flow(let id,let verb):
                try assertContext(reviewed)
                mutationAttempted = true
                response = try await client.request("/api/taskflows/" + NativeWorkflowWire.segment(id) + "/" + verb,method:"POST")
                guard response["id"] as? String == id,let status = response["status"] as? String,verb == "cancel" ? status == "cancelled" : ["queued","running","waiting","completed"].contains(status) else { throw NativeWorkflowFailure("This flow’s state change was not confirmed.") }; text = "Flow \(id): backend reports \(status).";scope = storedScope(response,kind:"taskflow")
            case .loadRoutines:
                try assertContext(reviewed)
                mutationAttempted = true
                response = try await client.request("/api/routines");try validateList("routines",response);text = "Routine inventory loaded; review scheduler state before assuming execution is healthy.";scope = reviewScope(reviewed.action)
            case .createRoutine(let body):
                try assertContext(reviewed)
                mutationAttempted = true
                response = try await client.request("/api/routines",method:"POST",body:body)
                guard NativeWorkflowWire.bool(response["ok"]) == true,let routine = response["routine"] as? [String:Any],let id = NativeWorkflowWire.id(routine["id"]),NativeWorkflowWire.bool(routine["enabled"]) == true,routine["cron_expr"] as? String == body["cron_expr"] as? String else { throw NativeWorkflowFailure("An enabled routine with that schedule was not confirmed.") };text = "Enabled routine \(id) created. No completed run is established by this receipt.";scope = storedScope(routine,kind:"routine")
            case .routine(let id,let verb):
                let fresh = try checkedRoutine(try await client.request("/api/routines/" + id),id:id)
                guard connection == generation,op == operation,now() <= reviewed.expiresAt,let expected = reviewed.expected,try NativeWorkflowWire.terms(fresh) == NativeWorkflowWire.terms(expected) else { throw NativeWorkflowFailure("This routine changed or its review expired. Refresh and review the exact action again.") }
                try assertContext(reviewed)
                mutationAttempted = true
                response = try await client.request("/api/routines/" + NativeWorkflowWire.segment(id) + (verb == "delete" ? "" : "/" + verb),method:verb == "delete" ? "DELETE" : "POST")
                guard NativeWorkflowWire.bool(response["ok"]) == true else { throw NativeWorkflowFailure("The routine change was not confirmed.") }
                if verb != "delete" {
                    let detail = try await client.request("/api/routines/" + NativeWorkflowWire.segment(id))
                    let row = try checkedRoutine(detail,id:id);var expectedAfter = fresh;expectedAfter["enabled"] = verb == "resume";if verb == "resume" { expectedAfter["disabled_reason"] = "" }
                    guard NativeWorkflowWire.bool(row["enabled"]) == (verb == "resume"),try NativeWorkflowWire.terms(row) == NativeWorkflowWire.terms(expectedAfter) else { throw NativeWorkflowFailure("The routine action was acknowledged but its exact enabled state and terms were not confirmed.") };confirmedRoutine = row
                } else {
                    let absent = try await client.request("/api/routines/" + id,missingRoutineOK:true)
                    guard absent["error"] as? String == "Routine not found",absent["routine"] == nil else { throw NativeWorkflowFailure("Deletion was acknowledged, but the exact routine is still present. Its absence is not confirmed.") }
                }
                text = "Routine \(verb) verified by readback. Already running effects may remain.";scope = storedScope(confirmedRoutine ?? fresh,kind:"routine")
            case .createAutomation(let minutes,let action):
                let composed = try NativeWorkflowWire.automationText(minutes:minutes,action:action),sid = sessionID!
                try assertContext(reviewed)
                mutationAttempted = true
                let acknowledgement = try await client.request("/api/automations",method:"POST",body:["interval_minutes":minutes,"action":action,"session_id":sid])
                guard connection == generation,op == operation,NativeWorkflowWire.bool(acknowledgement["success"]) == true,acknowledgement["creation_mode"] as? String == "explicit_interval",let id = NativeWorkflowWire.routineID(acknowledgement["job_id"]),acknowledgement["cron"] as? String == "every \(minutes)m",acknowledgement["description"] as? String == composed else { throw NativeWorkflowFailure("The requested automation was not acknowledged with its exact schedule.") }
                let row = try checkedRoutine(try await client.request("/api/routines/" + id),id:id)
                try validateAutomation(row)
                guard NativeWorkflowWire.bool(row["enabled"]) == true,row["cron_expr"] as? String == "every \(minutes)m",row["description"] as? String == composed,let payload = row["payload"] as? [String:Any],payload.count == 3,payload["source"] as? String == "natural_language",payload["action_text"] as? String == composed,payload["original_text"] as? String == composed else { throw NativeWorkflowFailure("The exact enabled CUSTOM action and schedule were not found in persisted storage.") }
                response = try await client.request("/api/automations");try validateList("automations",response)
                guard let saved = (response["automations"] as? [[String:Any]])?.first(where:{NativeWorkflowWire.routineID($0["id"]) == id}) else { throw NativeWorkflowFailure("The new schedule is not confirmed in the automation inventory.") }
                try validateAutomation(row,inventory:saved)
                text = "Enabled scheduled automation \(id) saved: every \(minutes) minutes. No completed run is established by this receipt.";scope = "Readback verified selected conversation \(sid) as this schedule’s owner. No existing conversation was converted."
            case .deleteAutomation(let id):
                let fresh = try checkedRoutine(try await client.request("/api/routines/" + id),id:id)
                try validateAutomation(fresh)
                guard connection == generation,op == operation,now() <= reviewed.expiresAt,let expected = reviewed.expected,try NativeWorkflowWire.terms(fresh) == NativeWorkflowWire.terms(expected) else { throw NativeWorkflowFailure("This automation changed or its review expired. Refresh and review again.") }
                try assertContext(reviewed)
                mutationAttempted = true
                let acknowledgement = try await client.request("/api/automations/" + id,method:"DELETE")
                guard NativeWorkflowWire.bool(acknowledgement["success"]) == true else { throw NativeWorkflowFailure("Automation deletion was not acknowledged.") }
                response = try await client.request("/api/automations");try validateList("automations",response)
                guard !(response["automations"] as! [[String:Any]]).contains(where:{NativeWorkflowWire.routineID($0["id"]) == id}) else { throw NativeWorkflowFailure("Deletion was acknowledged but this automation is still listed.") }
                let absent = try await client.request("/api/routines/" + id,missingRoutineOK:true)
                guard absent["error"] as? String == "Routine not found",absent["routine"] == nil else { throw NativeWorkflowFailure("The exact automation schedule is still present. Absence is not confirmed.") }
                text = "Scheduled automation \(id) deletion verified by readback. Already running work may continue.";scope = "Preflight verified selected conversation \(fresh["session_id"]!) as owner; readback verified deletion."
            case .compile(let intent):
                try assertContext(reviewed)
                mutationAttempted = true
                response = try await client.request("/api/intents/compile",method:"POST",body:["intent":intent.trimmingCharacters(in:.whitespacesAndNewlines)])
                guard NativeWorkflowWire.bool(response["success"]) == true,let plan = response["plan"] as? [String:Any],let id = NativeWorkflowWire.id(plan["plan_id"]),plan["intent"] as? String == intent.trimmingCharacters(in:.whitespacesAndNewlines),plan["actions"] is [[String:Any]] else { throw NativeWorkflowFailure("The service did not confirm the compiled intent.") };text = "Plan \(id) recorded. Suggested tools have not been executed by this action.";scope = reviewScope(reviewed.action)
            case .complete(let plan,let id,let result):
                try assertContext(reviewed)
                mutationAttempted = true
                response = try await client.request("/api/intents/" + NativeWorkflowWire.segment(plan) + "/complete/" + NativeWorkflowWire.segment(id),method:"POST",body:["result":result])
                guard NativeWorkflowWire.bool(response["success"]) == true else { throw NativeWorkflowFailure("Completion was not acknowledged.") }
                let today = try await client.request("/api/intents/today");try validateList("today",today)
                guard !(today["actions"] as! [[String:Any]]).contains(where:{$0["plan_id"] as? String == plan && $0["action_id"] as? String == id}) else { throw NativeWorkflowFailure("The backend acknowledged completion, but this exact action is still listed. Completion is not confirmed.") };text = "Action completion recorded and no longer listed for today. No tool execution is claimed.";scope = reviewScope(reviewed.action)
            }
            guard connection == generation && op == operation else { return false }
            try assertContext(reviewed)
            if case .loadRoutines = reviewed.action { payloads["routines"] = response; errors["routines"] = nil }
            if case .createRoutine = reviewed.action,let row = response["routine"] as? [String:Any] { var list = payloads["routines"]?["routines"] as? [[String:Any]] ?? [];list.append(row);payloads["routines"] = ["routines":list] }
            if case .routine(let id,let verb) = reviewed.action { var list = payloads["routines"]?["routines"] as? [[String:Any]] ?? [];if verb == "delete" { list.removeAll { NativeWorkflowWire.id($0["id"]) == id } } else if let index = list.firstIndex(where:{ NativeWorkflowWire.id($0["id"]) == id }) { if let confirmed = confirmedRoutine { list[index] = confirmed } };var data = payloads["routines"] ?? [:];data["routines"] = list;payloads["routines"] = data }
            switch reviewed.action { case .createAutomation,.deleteAutomation:payloads["automations"] = response;errors["automations"] = nil;automationDetails = [:];default:break }
            if case .compile = reviewed.action { compiledPlan = response["plan"] as? [String:Any] }
            await refresh()
            guard connection == generation && op == operation else { return false }
            try assertContext(reviewed)
            receipt = text + " Scope: " + scope;return true
        } catch { if connection == generation && op == operation { actionError = mutationAttempted ? "Action outcome is unknown or unverified. Inspect its stored state before any new attempt; no automatic retry was made. " + error.localizedDescription : error.localizedDescription };return false }
    }
}

struct NativeWorkflowStepDraft: Identifiable {
    let id = UUID()
    var type = "llm.chat"
    var text = ""
    var seconds = 60
    var value:[String:Any] { type == "sleep" ? ["type":type,"seconds":seconds] : type == "note.save" ? ["type":type,"content":text] : ["type":type,"prompt":text] }
}
struct NativeWorkflowFeatureView: View {
    let baseURL:URL?
    let sessionID:String?
    let contextPolicy:NativeSelectedContextPolicy
    @StateObject private var model = NativeWorkflowModel()
    @State private var tab = "Packs"
    @State private var review:NativeWorkflowReview?
    @State private var localError:String?
    @State private var draft = ""
    @State private var title = ""
    @State private var steps = [NativeWorkflowStepDraft()]
    @State private var schedule = "0 9 * * 1-5"
    @State private var timezone = TimeZone.current.identifier
    @State private var recurring = true
    @State private var routineKind = "prompt"
    @State private var routinePrompt = ""
    @State private var automationMinutes = 60
    @State private var skill = ""
    @State private var endpoint = ""
    @State private var intent = ""
    @State private var selectedPack:NativeWorkflowRow?
    @State private var packContext:[String:String] = [:]
    init(baseURL:URL?,sessionID:String? = nil,contextPolicy:NativeSelectedContextPolicy = .legacy) { self.baseURL = baseURL; self.sessionID = sessionID;self.contextPolicy = contextPolicy }
    private var busy:Bool { model.loading || model.acting }
    var body:some View {
        VStack(alignment:.leading,spacing:16) {
            HStack { VStack(alignment:.leading,spacing:4) { Text("Workflows").font(.largeTitle.bold());Text("Review background work, scheduled routines and intent plans.").foregroundStyle(.secondary) };Spacer();Button("Refresh") { Task { await model.refresh() } }.disabled(busy || baseURL == nil) }
            Text(contextPolicy.message).font(.caption).foregroundStyle(.secondary)
            Picker("Workflow section",selection:$tab) { ForEach(["Packs","TaskFlows","Routines","Automations","Intents"],id:\.self) { Text($0).tag($0) } }.pickerStyle(.segmented).disabled(model.acting)
            if baseURL == nil { Text("The local workflow service is not ready.").foregroundStyle(.secondary);Spacer() }
            else {
                if let error = localError ?? model.actionError { NativeSelectableText(error).foregroundStyle(.red) }
                if let receipt = model.receipt { NativeSelectableText(receipt).foregroundStyle(.secondary) }
                if model.loading { ProgressView("Refreshing safe workflow reads…") }
                ScrollView { VStack(alignment:.leading,spacing:16) { if tab == "Packs" { packs };if tab == "TaskFlows" { flows };if tab == "Routines" { routines };if tab == "Automations" { automations };if tab == "Intents" { intents } }.frame(maxWidth:.infinity,alignment:.leading) }
            }
        }.padding(24)
        .task(id:(baseURL?.absoluteString ?? "") + "|" + (sessionID ?? "")) { review = nil;draft = "";selectedPack = nil; model.setContextPolicy(contextPolicy);await model.configure(baseURL:baseURL,sessionID:sessionID) }
        .onChange(of:model.selectedFlow) { _ in Task { await model.inspectFlow() } }
        .onChange(of:contextPolicy) { policy in review = nil;draft = "";selectedPack = nil;model.setContextPolicy(policy) }
        .sheet(item:$review) { reviewed in VStack(alignment:.leading,spacing:16) { Text(reviewed.title).font(.title2.bold());ScrollView { NativeSelectableText(reviewed.explanation) }.frame(maxHeight:340);if let error = model.actionError { Text(error).foregroundStyle(.red) };HStack { Spacer();Button("Cancel") { review = nil }.disabled(model.acting);Button(model.acting ? "Applying…" : "Confirm") { Task { model.setContextPolicy(contextPolicy);if await model.perform(reviewed) { review = nil } } }.disabled(busy) } }.padding(24).frame(width:560).interactiveDismissDisabled(model.acting) }
        .sheet(isPresented:Binding(get:{ !draft.isEmpty },set:{ if !$0 { draft = "" } })) { draftSheet }
        .sheet(item:$selectedPack) { pack in packSheet(pack) }
    }
    private func request(_ action:NativeWorkflowAction) { do { localError = nil;model.setContextPolicy(contextPolicy);review = try model.review(action) } catch { localError = error.localizedDescription } }
    private func card<Content:View>(_ title:String,@ViewBuilder content:() -> Content) -> some View { VStack(alignment:.leading,spacing:12) { Text(title).font(.headline);content() }.padding(16).frame(maxWidth:.infinity,alignment:.leading).background(Color.secondary.opacity(0.07),in:RoundedRectangle(cornerRadius:12)) }
    @ViewBuilder private func state(_ resource:String) -> some View { if let error = model.errors[resource] { Text("Unavailable: " + error).foregroundStyle(.red) };if model.payloads[resource] != nil && model.rows(resource).isEmpty { Text("No records returned.").foregroundStyle(.secondary) } }
    @ViewBuilder private var packs:some View {
        state("packs")
        ForEach(model.rows("packs")) { pack in card(pack.title) { Text(pack.raw["description"] as? String ?? "No description provided.");Text("Template only. Instantiating creates live queued work.").font(.caption).foregroundStyle(.secondary);if let steps = pack.raw["steps"] as? [[String:Any]] { ForEach(Array(steps.enumerated()),id:\.offset) { i,step in Text("\(i+1). \(step["type"] as? String ?? "Unknown step")").font(.caption) } };Button("Configure and review…") { packContext = [:];selectedPack = pack }.disabled(busy) } }
    }
    @ViewBuilder private var flows:some View {
        Button("Create workflow…") { title = "";steps = [NativeWorkflowStepDraft()];draft = "flow" }.disabled(busy)
        state("flows")
        ForEach(model.rows("flows")) { flow in card(flow.title) {
            Text("Runtime state: \(flow.raw["status"] as? String ?? "Unavailable")").foregroundStyle(.secondary)
            if let error = flow.raw["error"] as? String,!error.isEmpty { Text(error).foregroundStyle(.red) }
            HStack { Button("Inspect steps") { model.selectedFlow = flow.id };Button("Resume…") { request(.flow(id:flow.id,verb:"resume")) }.disabled(!["failed","waiting"].contains(flow.raw["status"] as? String ?? ""));Button("Cancel future steps…",role:.destructive) { request(.flow(id:flow.id,verb:"cancel")) }.disabled(["completed","cancelled"].contains(flow.raw["status"] as? String ?? "")) }.disabled(busy)
        } }
        if model.inspecting { ProgressView("Inspecting flow…") }
        if let error = model.detailError { Text(error).foregroundStyle(.red) }
        if let detail = model.detail { card("Flow steps and results") { ForEach(Array((detail["steps"] as? [[String:Any]] ?? []).enumerated()),id:\.offset) { i,step in VStack(alignment:.leading,spacing:5) { Text("\(i+1). \(step["step_type"] as? String ?? "Unknown") · \(step["status"] as? String ?? "Unavailable")");if let error = step["error"] as? String { Text(error).foregroundStyle(.red) };DisclosureGroup("Original step fields") { NativeSelectableText(NativeWorkflowWire.json(step)).font(.system(.caption,design:.monospaced)) } } } } }
    }
    @ViewBuilder private var routines:some View {
        Text("Loading the backend routine list can restart its scheduler; enabled jobs may execute.").font(.caption).foregroundStyle(.orange)
        HStack { Button("Review and load routines…") { request(.loadRoutines) };Button("Create enabled routine…") { title = "";routinePrompt = "";draft = "routine" } }.disabled(busy)
        if let data = model.payloads["routines"],let health = data["scheduler"] as? [String:Any] { Text("Scheduler running: \(NativeWorkflowWire.bool(health["running"]).map { $0 ? "Yes" : "No" } ?? "Unavailable")").foregroundStyle(.secondary) }
        ForEach(model.rows("routines")) { routine in card(routine.title) {
            Text("Schedule: \(routine.raw["cron_expr"] as? String ?? "Unavailable") · \(NativeWorkflowWire.bool(routine.raw["enabled"]) == true ? "Enabled" : "Disabled")").foregroundStyle(.secondary)
            if let next = routine.raw["next_run"] as? NSNumber { Text("Next reported run: " + Date(timeIntervalSince1970:next.doubleValue).formatted()).font(.caption) }
            if let reason = routine.raw["disabled_reason"] as? String,!reason.isEmpty { Text(reason).foregroundStyle(.orange) }
            HStack { Button(NativeWorkflowWire.bool(routine.raw["enabled"]) == true ? "Pause…" : "Resume…") { request(.routine(id:routine.id,verb:NativeWorkflowWire.bool(routine.raw["enabled"]) == true ? "pause" : "resume")) };Button("Delete schedule…",role:.destructive) { request(.routine(id:routine.id,verb:"delete")) } }.disabled(busy)
            DisclosureGroup("Original payload") { NativeSelectableText(NativeWorkflowWire.json(routine.raw["payload"] ?? [:])).font(.system(.caption,design:.monospaced)) }
        } }
    }
    @ViewBuilder private var intents:some View {
        intentCompilationCard
        state("today")
        ForEach(model.rows("today")) { action in intentActionCard(action) }
        state("plans")
        ForEach(model.rows("plans")) { plan in intentPlanCard(plan) }
        if let plan = model.compiledPlan { compiledIntentCard(plan) }
    }
    @ViewBuilder private var automations:some View {
        Text("Scheduled automations repeat an AI instruction. Webhooks and geofences have separate controls in Automation.").font(.caption).foregroundStyle(.secondary)
        Button("Create scheduled automation…") { automationMinutes = 60;routinePrompt = "";draft = "automation" }.disabled(busy)
        state("automations")
        ForEach(model.rows("automations")) { row in automationCard(row) }
    }
    private func automationCard(_ row:NativeWorkflowRow) -> some View {
        card(row.title) {
            Text("Schedule: \(row.raw["cron"] as? String ?? "Unavailable")").foregroundStyle(.secondary)
            Text(NativeWorkflowWire.bool(row.raw["enabled"]).map { $0 ? "Enabled schedule" : "Disabled schedule" } ?? "Schedule state unavailable").font(.caption)
            Button("Review deletion…",role:.destructive) {
                Task { do { localError = nil;review = try await model.prepareAutomationDeletion(row.id) } catch { localError = error.localizedDescription } }
            }.disabled(busy)
        }
    }
    private var intentCompilationCard:some View {
        card("Compile a goal") {
            TextEditor(text:$intent).frame(height:90).accessibilityLabel("Intent goal")
            Button("Review plan compilation…") { request(.compile(intent)) }
                .disabled(busy || intent.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty)
            Text("Compilation may use the configured AI provider; suggestions are not automatically executed.")
                .font(.caption).foregroundStyle(.secondary)
        }
    }
    private func intentActionCard(_ action:NativeWorkflowRow) -> some View {
        card(action.title) {
            Text(action.raw["intent"] as? String ?? "").foregroundStyle(.secondary)
            Text("Suggestion: \(action.raw["tool_hint"] as? String ?? "Manual")").font(.caption)
            Button("Mark done…") {
                request(.complete(plan:action.raw["plan_id"] as? String ?? "",action:action.id,result:"Completed manually"))
            }.disabled(busy)
        }
    }
    private func intentPlanCard(_ plan:NativeWorkflowRow) -> some View {
        let status = plan.raw["status"] as? String ?? "Unavailable"
        let done = String(describing:plan.raw["actions_done"] ?? "?")
        let total = String(describing:plan.raw["actions_total"] ?? "?")
        let summary = "State: \(status) · \(done) / \(total) actions recorded done"
        return card(plan.title) {
            Text(summary).foregroundStyle(.secondary)
            if let progress = plan.raw["progress"] as? NSNumber {
                ProgressView(value:min(1,max(0,progress.doubleValue)))
                Text(String(format:"%.0f%%",progress.doubleValue * 100)).font(.caption)
            }
        }
    }
    private func compiledIntentCard(_ plan:[String:Any]) -> some View {
        let actions = plan["actions"] as? [[String:Any]] ?? []
        return card("Latest compiled suggestions") {
            ForEach(Array(actions.enumerated()),id:\.offset) { _,action in
                Text(action["description"] as? String ?? "No description provided.")
            }
        }
    }
    private var draftSheet:some View {
        VStack(alignment:.leading,spacing:16) {
            Text(draft == "flow" ? "Create background workflow" : draft == "automation" ? "Create scheduled automation" : "Create enabled routine").font(.title2.bold())
            if draft != "automation" { TextField("Title / description",text:$title).textFieldStyle(.roundedBorder) }
            ScrollView {
                if draft == "flow" { ForEach($steps) { $step in VStack(alignment:.leading) { Picker("Step",selection:$step.type) { Text("AI prompt").tag("llm.chat");Text("Save note").tag("note.save");Text("Wait").tag("sleep") };if step.type == "sleep" { Stepper("Wait \(step.seconds) seconds",value:$step.seconds,in:1...86400) } else { TextField(step.type == "note.save" ? "Note content" : "Prompt",text:$step.text).textFieldStyle(.roundedBorder) };Button("Remove step",role:.destructive) { steps.removeAll { $0.id == step.id } } }.padding(.vertical,8) };Button("Add step") { steps.append(NativeWorkflowStepDraft()) }.disabled(steps.count >= 50) }
                else if draft == "automation" { VStack(alignment:.leading,spacing:12) { Stepper("Repeat every \(automationMinutes) minutes",value:$automationMinutes,in:1...10_080);TextEditor(text:$routinePrompt).frame(height:120).accessibilityLabel("Scheduled automation action");Text("Describe the task. It will repeat at the interval above after you review and confirm it.").font(.caption).foregroundStyle(.secondary) } }
                else { VStack(alignment:.leading,spacing:12) { TextField("Cron or supported schedule",text:$schedule).textFieldStyle(.roundedBorder);TextField("IANA timezone",text:$timezone).textFieldStyle(.roundedBorder);Toggle("Repeat",isOn:$recurring);Picker("Routine action",selection:$routineKind) { Text("AI prompt").tag("prompt");Text("Installed skill").tag("skill") };if routineKind == "prompt" { TextEditor(text:$routinePrompt).frame(height:100).accessibilityLabel("Routine prompt") } else { TextField("Installed skill ID",text:$skill).textFieldStyle(.roundedBorder);TextField("Endpoint",text:$endpoint).textFieldStyle(.roundedBorder);Text("This form does not verify the installed skill’s availability or external account permissions.").font(.caption).foregroundStyle(.secondary) } } }
            }.frame(maxHeight:340)
            HStack { Spacer();Button("Cancel") { draft = "" };Button("Review execution…") { let action:NativeWorkflowAction;if draft == "flow" { action = .createFlow(["title":title.trimmingCharacters(in:.whitespacesAndNewlines),"steps":steps.map(\.value),"session_id":"","context":[:]]) } else if draft == "automation" { action = .createAutomation(minutes:automationMinutes,action:routinePrompt) } else { let payload:[String:Any] = routineKind == "prompt" ? ["prompt":routinePrompt] : ["skill":skill,"endpoint":endpoint];action = .createRoutine(["description":title,"cron_expr":schedule,"tz_name":timezone,"recurring":recurring,"job_type":routineKind,"payload":payload,"session_id":""]) };do { model.setContextPolicy(contextPolicy);let prepared = try model.review(action);draft = "";DispatchQueue.main.async { review = prepared } } catch { localError = error.localizedDescription } } }
        }.padding(24).frame(width:560)
    }
    private func packSheet(_ pack:NativeWorkflowRow) -> some View {
        let expression = try? NSRegularExpression(pattern:"context\\.([A-Za-z0-9_]+)")
        let raw = NativeWorkflowWire.json(pack.raw), ns = raw as NSString
        let keys = Array(Set((expression?.matches(in:raw,range:NSRange(location:0,length:ns.length)) ?? []).map { ns.substring(with:$0.range(at:1)) })).sorted()
        return VStack(alignment:.leading,spacing:16) { Text(pack.title).font(.title2.bold());Text("Provide the context referenced by this pack before reviewing execution.").foregroundStyle(.secondary);ForEach(keys,id:\.self) { key in TextField(key,text:Binding(get:{ packContext[key] ?? "" },set:{ packContext[key] = $0 })).textFieldStyle(.roundedBorder) };HStack { Spacer();Button("Cancel") { selectedPack = nil };Button("Review execution…") { do { model.setContextPolicy(contextPolicy);let prepared = try model.review(.pack(id:pack.id,context:packContext));selectedPack = nil;DispatchQueue.main.async { review = prepared } } catch { localError = error.localizedDescription } } } }.padding(24).frame(width:560)
    }
}
