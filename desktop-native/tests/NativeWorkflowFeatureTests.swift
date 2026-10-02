import Foundation

final class WorkflowFixture: URLProtocol {
    static var requests:[URLRequest] = []
    static var flowStatus = "waiting"
    static var routineEnabled = true
    static var routineExists = true
    static var automations:[String:[String:Any]] = [:]
    static var nextAutomationID = 20
    static var done = false
    static var delayPath:String?
    static var delayed = false
    static var handler:(URLRequest) -> (Int,Any) = normal
    static var flow:[String:Any] { ["id":"f1","title":"Existing flow","status":flowStatus,"current_step":0,"context":["future_metadata":["keep":true]],"steps":[["id":1,"step_type":"sleep","status":"waiting","payload":["seconds":10,"future_field":true],"error":NSNull()]],"error":flowStatus == "failed" ? "Provider failed" : NSNull()] }
    static var routine:[String:Any] { ["id":7,"description":"Existing routine","cron_expr":"every 60m","job_type":"scheduled","session_id":"thread-a","enabled":routineEnabled,"payload":["prompt":"Read local notes","future_field":true],"disabled_reason":"","future_metadata":["keep":true]] }
    static func automationInventory(_ row:[String:Any]) -> [String:Any] { ["id":row["id"]!,"description":row["description"]!,"cron":row["cron_expr"]!,"enabled":row["enabled"]!,"next_run":1234567890,"run_count":0] }
    static func normal(_ request:URLRequest) -> (Int,Any) {
        let path = request.url!.path, body = (try? JSONSerialization.jsonObject(with:request.httpBody ?? Data())) as? [String:Any] ?? [:]
        switch path {
        case "/api/workflows/packs": return (200,["packs":[["workflow_id":"pack-a","name":"Local pack","description":"Fixture","steps":[["type":"note.save","content":"{{ context.topic }}"]],"future_field":true]]])
        case "/api/taskflows":
            if request.httpMethod == "POST" { return (200,["id":"new-flow","title":body["title"]!,"status":"queued","steps":body["steps"]!,"context":body["context"] ?? [:]]) }
            return (200,["flows":[flow]])
        case "/api/taskflows/f1": return (200,flow)
        case "/api/taskflows/f1/resume": flowStatus = "queued";return (200,flow)
        case "/api/taskflows/f1/cancel": flowStatus = "cancelled";return (200,flow)
        case "/api/workflows/packs/pack-a/instantiate": return (200,["success":true,"workflow_id":"pack-a","flow":["id":"pack-flow","status":"queued","title":"Local pack"]])
        case "/api/routines":
            if request.httpMethod == "POST" { return (200,["ok":true,"routine":["id":8,"description":body["description"] ?? "","cron_expr":body["cron_expr"]!,"enabled":true,"payload":body["payload"]!]]) }
            return (200,["routines":routineExists ? [routine] : [],"scheduler":["running":true,"scheduled":true]])
        case "/api/routines/7":
            if request.httpMethod == "DELETE" { routineExists = false;return (200,["ok":true]) }
            if routineExists { return (200,["routine":routine,"runs":[]] as [String:Any]) };return (200,["error":"Routine not found"])
        case "/api/routines/7/pause": routineEnabled = false;return (200,["ok":true])
        case "/api/routines/7/resume": routineEnabled = true;return (200,["ok":true])
        case "/api/intents/list": return (200,["plans":[["plan_id":"p1","intent":"Read a book","status":done ? "completed" : "active","progress":done ? 1.0 : 0.0,"actions_total":1,"actions_done":done ? 1 : 0]]])
        case "/api/intents/today": return (200,["actions":done ? [] : [["plan_id":"p1","action_id":"a1","intent":"Read a book","action":"Read one chapter","tool_hint":"manual","future_field":["keep":true]]]])
        case "/api/intents/stats": return (200,["total_plans":1,"active_plans":done ? 0 : 1])
        case "/api/intents/compile": return (200,["success":true,"plan":["plan_id":"new-plan","intent":body["intent"]!,"status":"active","progress":0.0,"actions":[["action_id":"n1","description":"Suggestion only","tool_hint":"manual","completed":false,"future_field":true]]]])
        case "/api/intents/p1/complete/a1": done = true;return (200,["success":true])
        case "/api/automations":
            if request.httpMethod == "POST" {
                let minutes = body["interval_minutes"] as! Int,text = "every \(minutes) minutes, " + (body["action"] as! String),id = nextAutomationID;nextAutomationID += 1
                let row:[String:Any] = ["id":id,"description":text,"cron_expr":"every \(minutes)m","job_type":"custom","session_id":body["session_id"]!,"enabled":true,"payload":["source":"natural_language","original_text":text,"action_text":text],"disabled_reason":""]
                automations[String(id)] = row
                return (200,["success":true,"job_id":id,"cron":"every \(minutes)m","description":text,"creation_mode":"explicit_interval"])
            }
            return (200,["automations":automations.values.map(automationInventory)])
        default:
            if path.hasPrefix("/api/routines/"),request.httpMethod == "GET",let id = path.split(separator:"/").last.map(String.init),Int(id) != nil {
                if let row = automations[id] { return (200,["routine":row,"runs":[]] as [String:Any]) };return (200,["error":"Routine not found"])
            }
            if path.hasPrefix("/api/automations/"),request.httpMethod == "DELETE",let id = path.split(separator:"/").last.map(String.init) { automations[id] = nil;return (200,["success":true]) }
            return (404,["detail":"Fixture path not found"])
        }
    }
    static func data(_ request:URLRequest) -> Data {
        if let data = request.httpBody { return data };guard let stream = request.httpBodyStream else { return Data() }
        stream.open();defer { stream.close() };var data = Data(),buffer = [UInt8](repeating:0,count:4096)
        while stream.hasBytesAvailable { let n = stream.read(&buffer,maxLength:buffer.count);if n <= 0 { break };data.append(buffer,count:n) };return data
    }
    override class func canInit(with request:URLRequest) -> Bool { true }
    override class func canonicalRequest(for request:URLRequest) -> URLRequest { request }
    override func startLoading() {
        var captured = request;if request.httpMethod == "POST" { captured.httpBody = Self.data(request) };Self.requests.append(captured)
        let (status,value) = Self.handler(captured)
        if status == -1 { client?.urlProtocol(self,didFailWithError:URLError(.networkConnectionLost));return }
        let response = HTTPURLResponse(url:request.url!,statusCode:status,httpVersion:nil,headerFields:nil)!
        let data = try! JSONSerialization.data(withJSONObject:value)
        let deliver = { self.client?.urlProtocol(self,didReceive:response,cacheStoragePolicy:.notAllowed);self.client?.urlProtocol(self,didLoad:data);self.client?.urlProtocolDidFinishLoading(self) }
        if Self.delayPath == request.url!.path { Self.delayPath = nil;Self.delayed = true;DispatchQueue.global().asyncAfter(deadline:.now()+0.1,execute:deliver) } else { deliver() }
    }
    override func stopLoading() {}
}
@main struct NativeWorkflowFeatureTests {
    static var count = 0
    @MainActor static func check(_ valid:Bool,_ name:String) { if !valid { fatalError("FAIL: \(name)") };count += 1 }
    @MainActor static func delay() async throws { for _ in 0..<1000 { if WorkflowFixture.delayed { return };try await Task.sleep(nanoseconds:1_000_000) };fatalError("Fixture delay not reached") }
    @MainActor static func parityTests(session:URLSession,base:URL) async throws {
        WorkflowFixture.handler = WorkflowFixture.normal;WorkflowFixture.automations = [:];WorkflowFixture.routineExists = true;WorkflowFixture.routineEnabled = true
        var instant = Date(timeIntervalSince1970:1000)
        let model = NativeWorkflowModel(session:session,now:{ instant })
        await model.configure(baseURL:base,sessionID:"thread-a")
        check(model.rows("automations").isEmpty && model.errors["automations"] == nil,"new scheduled-automation section uses existing passive inventory")
        let normal = WorkflowFixture.normal
        let attempted = WorkflowFixture.requests.count
        for action in [String(repeating:"x",count:8001),"bad\u{0000}action", "   "] {
            do { _ = try model.review(.createAutomation(minutes:60,action:action));fatalError("invalid action accepted") } catch {}
        }
        for action in ["Read notes every morning","Read daily tasks","weekly review"] {
            check(try model.review(.createAutomation(minutes:60,action:action)).explanation.contains(action),"ordinary schedule words remain task text in explicit interval review")
        }
        for minutes in [0,10081] { do { _ = try model.review(.createAutomation(minutes:minutes,action:"Read notes"));fatalError("unbounded schedule accepted") } catch {} }
        check(WorkflowFixture.requests.count == attempted,"invalid/ambiguous automation input refuses before any request")
        let reviewed = try model.review(.createAutomation(minutes:60,action:"Read local notes"))
        check(reviewed.explanation.contains("scheduled prompt, not an event trigger") && WorkflowFixture.requests.count == attempted,"scheduled automation review inert and truthful")
        check(await model.perform(reviewed) && model.receipt?.contains("No completed run") == true,"CUSTOM creation requires persisted schedule receipt, not effect success")
        let createRequest = WorkflowFixture.requests.last { $0.url!.path == "/api/automations" && $0.httpMethod == "POST" }!
        let body = try JSONSerialization.jsonObject(with:createRequest.httpBody!) as! [String:Any]
        check(body.count == 3 && body["interval_minutes"] as? Int == 60 && body["action"] as? String == "Read local notes" && body["session_id"] as? String == "thread-a","structured interval/action/session contract exact with no bypass fields")
        let usedCount = WorkflowFixture.requests.count
        check(!(await model.perform(reviewed)) && WorkflowFixture.requests.count == usedCount,"creation review consumed once, no duplicate write")
        let id = model.rows("automations")[0].id
        let deletion = try await model.prepareAutomationDeletion(id)
        check(deletion.explanation.contains("Read local notes"),"delete review captures exact stored action and schedule")
        WorkflowFixture.automations[id]!["payload"] = ["action_text":"changed"]
        let mutations = WorkflowFixture.requests.filter { $0.httpMethod != "GET" }.count
        check(!(await model.perform(deletion)) && WorkflowFixture.requests.filter { $0.httpMethod != "GET" }.count == mutations,"changed action blocked by fresh terms before deletion")
        WorkflowFixture.automations[id]!["session_id"] = "thread-b"
        do { _ = try await model.prepareAutomationDeletion(id);fatalError("foreign automation owned") } catch {}
        check(WorkflowFixture.automations[id] != nil,"foreign conversation schedule preserved")
        WorkflowFixture.automations[id]!["session_id"] = "thread-a";WorkflowFixture.automations[id]!["job_type"] = "scheduled"
        do { _ = try await model.prepareAutomationDeletion(id);fatalError("noncustom automation accepted") } catch {}
        WorkflowFixture.automations[id]!["job_type"] = "custom"
        await model.refresh()
        let lying = try await model.prepareAutomationDeletion(id)
        WorkflowFixture.handler = { request in request.httpMethod == "DELETE" && request.url!.path == "/api/automations/" + id ? (200,["success":true]) : normal(request) }
        check(!(await model.perform(lying)) && model.receipt == nil && model.actionError?.contains("unknown or unverified") == true,"lying delete acknowledgement never establishes absence")
        WorkflowFixture.handler = normal
        check(await model.perform(try await model.prepareAutomationDeletion(id)) && model.rows("automations").isEmpty && WorkflowFixture.automations[id] == nil,"exact automation inventory and safe detail prove deletion")
        let expired = try model.review(.createAutomation(minutes:60,action:"Read notes")),beforeExpiry = WorkflowFixture.requests.count
        instant = instant.addingTimeInterval(121)
        check(!(await model.perform(expired)) && WorkflowFixture.requests.count == beforeExpiry,"expired review cannot dispatch")
        WorkflowFixture.handler = { request in
            if request.url!.path == "/api/automations",request.httpMethod == "GET" { return (200,["error":"Scheduler unavailable","automations":[]]) };return normal(request)
        }
        await model.refresh()
        check(model.payloads["automations"] == nil && model.errors["automations"] != nil,"HTTP200 error is unavailable rather than false empty inventory")
        WorkflowFixture.handler = { request in request.url!.path == "/api/automations" ? (200,["automations":[["id":1],["id":1]]]) : normal(request) }
        await model.refresh();check(model.errors["automations"] != nil,"duplicate inventory identifiers fail closed")
        WorkflowFixture.handler = normal;await model.refresh()
        let failed = try model.review(.createAutomation(minutes:90,action:"Read local notes"))
        WorkflowFixture.handler = { request in
            let response = normal(request)
            if request.url!.path == "/api/automations",request.httpMethod == "POST" { return (-1,[:]) };return response
        }
        let beforeLost = WorkflowFixture.requests.filter { $0.httpMethod == "POST" && $0.url!.path == "/api/automations" }.count
        check(!(await model.perform(failed)) && model.receipt == nil && model.actionError?.contains("unknown or unverified") == true && WorkflowFixture.automations.count == 1,"lost reply after fixture commit preserves unknown state")
        check(!(await model.perform(failed)) && WorkflowFixture.requests.filter { $0.httpMethod == "POST" && $0.url!.path == "/api/automations" }.count == beforeLost + 1,"uncertain creation cannot replay its consumed review")
        WorkflowFixture.handler = normal
        for field in ["enabled","job_type","session_id","cron_expr","payload","unexpected_policy"] {
            WorkflowFixture.handler = { request in
                let response = normal(request)
                if request.httpMethod == "GET",request.url!.path.hasPrefix("/api/routines/"),let raw = response.1 as? [String:Any],var row = raw["routine"] as? [String:Any] {
                    let replacement:[String:Any] = ["enabled":false,"job_type":"scheduled","session_id":"thread-b","cron_expr":"every 1h","payload":["action_text":"another action"]]
                    if field == "unexpected_policy" { var payload = row["payload"] as! [String:Any];payload["auto_confirm"] = true;row["payload"] = payload } else { row[field] = replacement[field] };return (200,["routine":row,"runs":[]])
                };return response
            }
            check(!(await model.perform(try model.review(.createAutomation(minutes:120,action:"Read notes")))) && model.receipt == nil,"persisted mismatched \(field) cannot confirm creation")
        }
        WorkflowFixture.handler = normal;await model.configure(baseURL:base,sessionID:nil)
        let noSessionCount = WorkflowFixture.requests.count
        do { _ = try model.review(.createAutomation(minutes:60,action:"Read notes"));fatalError("unbound creation accepted") } catch {}
        check(WorkflowFixture.requests.count == noSessionCount,"scheduled creation requires selected conversation")
        await model.configure(baseURL:base,sessionID:"thread-a")
        check(await model.perform(try model.review(.loadRoutines)),"routine inventory remains explicitly reviewed")
        let pause = try model.review(.routine(id:"7",verb:"pause"))
        check(pause.explanation.contains("Read local notes") && pause.explanation.contains("thread-a") && pause.explanation.contains("every 60m"),"routine review displays captured action, conversation and schedule")
        WorkflowFixture.handler = { request in
            if request.url!.path == "/api/routines/7",request.httpMethod == "GET" { var row = WorkflowFixture.routine;row["payload"] = ["prompt":"changed"];return (200,["routine":row,"runs":[]]) };return normal(request)
        }
        let beforePause = WorkflowFixture.requests.filter { $0.httpMethod == "POST" }.count
        check(!(await model.perform(pause)) && WorkflowFixture.requests.filter { $0.httpMethod == "POST" }.count == beforePause,"routine changed payload refuses before pause")
        WorkflowFixture.handler = normal
        WorkflowFixture.delayPath = "/api/routines/7";WorkflowFixture.delayed = false
        let deferredPause = try model.review(.routine(id:"7",verb:"pause"))
        let beforeDeferred = WorkflowFixture.requests.filter { $0.httpMethod == "POST" }.count
        let deferred = Task { await model.perform(deferredPause) }
        try await delay();await model.configure(baseURL:base,sessionID:"thread-b")
        check(!(await deferred.value) && WorkflowFixture.requests.filter { $0.httpMethod == "POST" }.count == beforeDeferred,"connection change during fresh preflight prevents later mutation")
        await model.configure(baseURL:base,sessionID:"thread-a")
        check(await model.perform(try model.review(.loadRoutines)),"new connection explicitly reloads routine inventory")
        let delete = try model.review(.routine(id:"7",verb:"delete"))
        WorkflowFixture.handler = { request in request.url!.path == "/api/routines/7" && request.httpMethod == "DELETE" ? (200,["ok":true]) : normal(request) }
        check(!(await model.perform(delete)) && model.rows("routines").count == 1 && model.actionError?.contains("still present") == true,"routine delete acknowledgement without absence retains row")
        WorkflowFixture.handler = normal
        let stale = try await model.prepareAutomationDeletion(String(WorkflowFixture.nextAutomationID - 1))
        await model.configure(baseURL:base,sessionID:"thread-b")
        let connectionCount = WorkflowFixture.requests.count
        check(!(await model.perform(stale)) && WorkflowFixture.requests.count == connectionCount,"changed conversation cannot use earlier schedule review")
        check(!WorkflowFixture.requests.contains { $0.url!.path.contains("run_now") || $0.url!.path.hasSuffix("/run") },"no invented Run Now route or direct execution request")
    }
    @MainActor static func main() async throws {
        let config = URLSessionConfiguration.ephemeral;config.protocolClasses = [WorkflowFixture.self]
        let session = URLSession(configuration:config), base = URL(string:"http://127.0.0.1:9464")!,model = NativeWorkflowModel(session:session)
        await model.configure(baseURL:nil,sessionID:nil)
        check(!model.available && WorkflowFixture.requests.isEmpty,"nil readiness no requests")
        await model.configure(baseURL:base,sessionID:"thread-a")
        check(model.errors.isEmpty && model.rows("packs").count == 1 && model.rows("flows").count == 1 && model.rows("today").count == 1,"six genuine passive read resources")
        check(WorkflowFixture.requests.allSatisfy { $0.httpMethod == "GET" && $0.url!.path != "/api/routines" },"passive refresh never starts jobs or routine-list scheduler self-heal")
        model.selectedFlow = "f1";await model.inspectFlow()
        check((model.detail?["context"] as? [String:Any])?["future_metadata"] != nil && NativeWorkflowWire.json(model.detail!).contains("future_field"),"inspect preserves original flow context and step metadata")
        let body:[String:Any] = ["title":"Reviewed workflow","steps":[["type":"note.save","content":"Disposable fixture note","future_field":true],["type":"sleep","seconds":2]],"context":["keep":true],"session_id":""]
        let beforeReview = WorkflowFixture.requests.count
        let creation = try model.review(.createFlow(body))
        check(creation.explanation.contains("queued work immediately") && WorkflowFixture.requests.count == beforeReview,"create review is inert and explains live queued execution")
        check(await model.perform(creation) && model.receipt?.contains("new-flow") == true,"queued workflow creation not completed execution")
        let createdRequest = WorkflowFixture.requests.last { $0.httpMethod == "POST" && $0.url!.path == "/api/taskflows" }!
        let createdBody = try JSONSerialization.jsonObject(with:createdRequest.httpBody!) as! [String:Any]
        check((createdBody["steps"] as! [[String:Any]])[0]["future_field"] as? Bool == true && createdBody["session_id"] as? String == "","step metadata preserved and dedicated empty session explicit")
        let invalidCount = WorkflowFixture.requests.count
        do { _ = try model.review(.createFlow(["title":"bad","steps":[["type":"invented"]]]));fatalError("unsupported step accepted") } catch {}
        check(WorkflowFixture.requests.count == invalidCount,"invalid workflow review made no network request")
        let pack = try model.review(.pack(id:"pack-a",context:["topic":"local testing","future_context":true]))
        let packSucceeded = await model.perform(pack)
        check(pack.explanation.contains("live queued") && packSucceeded,"pack template reviewed before instantiating real flow")
        let packReq = WorkflowFixture.requests.last { $0.url!.path.hasSuffix("instantiate") }!
        check((try JSONSerialization.jsonObject(with:packReq.httpBody!) as! [String:Any])["context"] != nil,"pack context passes through actual endpoint")
        check(await model.perform(try model.review(.flow(id:"f1",verb:"resume"))) && model.rows("flows")[0].raw["status"] as? String == "queued","resume confirms requeue not task success")
        check(await model.perform(try model.review(.flow(id:"f1",verb:"cancel"))) && model.rows("flows")[0].raw["status"] as? String == "cancelled","cancel receipt exact flow/status")
        let cancelledCount = WorkflowFixture.requests.count
        do { _ = try model.review(.flow(id:"f1",verb:"resume"));fatalError("terminal flow resumed") } catch {}
        check(WorkflowFixture.requests.count == cancelledCount,"terminal flow change blocked before dispatch")
        let routineRead = try model.review(.loadRoutines)
        check(routineRead.explanation.contains("restart its scheduler") && model.payloads["routines"] == nil,"routine read side effect explicitly reviewed")
        check(await model.perform(routineRead) && model.rows("routines").count == 1,"explicitly reviewed scheduler inventory loaded")
        let routineGets = WorkflowFixture.requests.filter { $0.url!.path == "/api/routines" && $0.httpMethod == "GET" }.count
        await model.refresh()
        check(WorkflowFixture.requests.filter { $0.url!.path == "/api/routines" && $0.httpMethod == "GET" }.count == routineGets,"subsequent safe refresh does not silently restart scheduler again")
        let routineBody:[String:Any] = ["description":"Fixture routine","cron_expr":"0 9 * * 1-5","tz_name":"America/Los_Angeles","recurring":false,"job_type":"prompt","payload":["prompt":"Local fixture goal","future_field":true],"session_id":""]
        let routineCreate = try model.review(.createRoutine(routineBody))
        check(routineCreate.explanation.contains("Arms schedule") && routineCreate.explanation.contains("Runs once"),"routine enabled schedule/timezone/recurrence review")
        check(await model.perform(routineCreate) && model.receipt?.contains("Enabled routine 8") == true,"routine receipt confirms enabled creation not completed run")
        let routineRequest = WorkflowFixture.requests.last { $0.url!.path == "/api/routines" && $0.httpMethod == "POST" }!
        let routineData = try JSONSerialization.jsonObject(with:routineRequest.httpBody!) as! [String:Any]
        check(routineData["cron_expr"] as? String == "0 9 * * 1-5" && routineData["tz_name"] as? String == "America/Los_Angeles" && routineData["recurring"] as? Bool == false && (routineData["payload"] as? [String:Any])?["future_field"] as? Bool == true,"canonical routine wire and payload metadata retained")
        check(await model.perform(try model.review(.routine(id:"7",verb:"pause"))) && model.rows("routines").first { $0.id == "7" }!.raw["enabled"] as? Bool == false,"pause verified against safe per-routine detail read")
        check(await model.perform(try model.review(.routine(id:"7",verb:"resume"))) && model.rows("routines").first { $0.id == "7" }!.raw["enabled"] as? Bool == true,"resume verified enabled state")
        check(await model.perform(try model.review(.routine(id:"7",verb:"delete"))) && !model.rows("routines").contains { $0.id == "7" },"confirmed schedule deletion removes only exact row")
        let compiled = try model.review(.compile("  Read a book  "))
        let compileSucceeded = await model.perform(compiled)
        check(compiled.explanation.contains("does not execute") && compileSucceeded,"intent compile warns provider call but no action execution")
        let compileRequest = WorkflowFixture.requests.last { $0.url!.path == "/api/intents/compile" }!
        let compileBody = try JSONSerialization.jsonObject(with:compileRequest.httpBody!) as! [String:Any]
        check(compileBody.count == 1 && compileBody["intent"] as? String == "Read a book" && model.compiledPlan?["actions"] != nil,"intent uses actual intent key and stores suggestions")
        check(await model.perform(try model.review(.complete(plan:"p1",action:"a1",result:"Completed manually"))) && model.rows("today").isEmpty,"manual completion verified absent from today, never executes suggested tool")
        let completeRequest = WorkflowFixture.requests.last { $0.url!.path.contains("/complete/") }!
        check((try JSONSerialization.jsonObject(with:completeRequest.httpBody!) as! [String:Any])["result"] as? String == "Completed manually","completion result exact recorded wire")
        let completedCount = WorkflowFixture.requests.count
        do { _ = try model.review(.complete(plan:"p1",action:"wrong",result:""));fatalError("unknown action accepted") } catch {}
        check(WorkflowFixture.requests.count == completedCount,"unknown plan/action never dispatched")
        WorkflowFixture.done = false;await model.refresh()
        let normal = WorkflowFixture.normal
        WorkflowFixture.handler = { request in request.url!.path.contains("/complete/") ? (200,["success":true]) : normal(request) }
        check(!(await model.perform(try model.review(.complete(plan:"p1",action:"a1",result:"")))) && model.actionError?.contains("still listed") == true,"lying completion acknowledgement not native success")
        WorkflowFixture.handler = { request in request.url!.path == "/api/intents/compile" ? (200,["error":"Intent runtime unavailable"]) : normal(request) }
        check(!(await model.perform(try model.review(.compile("test")))) && model.actionError == "Intent runtime unavailable","HTTP200 error not plan compiled")
        WorkflowFixture.handler = { request in request.url!.path == "/api/taskflows" ? (200,["flows":[["title":"missing ID"]]]) : normal(request) }
        await model.refresh()
        check(model.errors["flows"] != nil && model.payloads["flows"] == nil && model.payloads["today"] != nil,"malformed list unavailable, other resources preserved")
        WorkflowFixture.handler = normal;WorkflowFixture.flowStatus = "failed";await model.refresh();model.selectedFlow = "f1";await model.inspectFlow()
        check(model.detail?["status"] as? String == "failed" && model.detail?["error"] as? String == "Provider failed" && model.detailError == nil,"failed flow error is inspectable document, not swallowed fetch failure")
        let stale = try model.review(.compile("stale"));await model.configure(baseURL:base,sessionID:"another-thread")
        let staleCount = WorkflowFixture.requests.count
        check(!(await model.perform(stale)) && WorkflowFixture.requests.count == staleCount,"session change invalidates reviewed action")
        WorkflowFixture.delayPath = "/api/intents/compile";WorkflowFixture.delayed = false
        let lateReview = try model.review(.compile("late response")), late = Task { await model.perform(lateReview) }
        try await delay();await model.configure(baseURL:URL(string:"http://127.0.0.1:9465")!,sessionID:"another-thread")
        check(!(await late.value) && model.compiledPlan == nil && model.receipt == nil && !model.acting,"late old-backend action cannot leak plan/receipt or strand busy")
        let remoteCount = WorkflowFixture.requests.count
        for url in ["http://localhost:9464","https://example.com","http://credential@127.0.0.1:9464"] { do { _ = try await NativeWorkflowClient(baseURL:URL(string:url)!,session:session).request("/api/taskflows");fatalError("unsafe URL accepted") } catch {} }
        check(WorkflowFixture.requests.count == remoteCount,"DNS/external/credential endpoints refused before transport")
        let delegate = NativeWorkflowRedirectGuard(), source = URL(string:"http://127.0.0.1:9464/api/taskflows")!,task = session.dataTask(with:URLRequest(url:source)),response = HTTPURLResponse(url:source,statusCode:307,httpVersion:nil,headerFields:nil)!
        var forwarded = true
        delegate.urlSession(session,task:task,willPerformHTTPRedirection:response,newRequest:URLRequest(url:URL(string:"https://example.com")!)) { forwarded = $0 != nil }
        check(!forwarded,"redirect cannot forward reviewed action")
        try await parityTests(session:session,base:base)
        await model.configure(baseURL:nil,sessionID:nil)
        check(model.payloads.isEmpty && model.compiledPlan == nil && model.detail == nil,"disconnect clears private workflow/plan records")
        task.cancel();session.invalidateAndCancel()
        print("PASS: \(count) native Workflow fixture assertions; no real jobs, providers or integration transmissions")
    }
}
