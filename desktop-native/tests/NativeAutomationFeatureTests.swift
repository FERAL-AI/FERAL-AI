import Foundation

final class AutomationFixture:URLProtocol {
    static var requests:[URLRequest] = []
    static var delayPath:String?
    static var delayed = false
    static var delayMethod:String?
    static var records:[String:[[String:Any]]] = ["/api/geofences":[],"/api/custom-webhooks/list":[],"/api/outgoing-webhooks":[]]
    static var handler:(URLRequest) -> (Int,[String:Any]) = normal
    static func normal(_ req:URLRequest) -> (Int,[String:Any]) {
        let path = req.url!.path,body = (try? JSONSerialization.jsonObject(with:req.httpBody ?? Data())) as? [String:Any] ?? [:]
        if req.httpMethod == "GET",let rows = records[path] {return (200,[path == "/api/geofences" ? "geofences" : "webhooks":rows])}
        if req.httpMethod == "POST",["/api/geofences","/api/custom-webhooks/create","/api/outgoing-webhooks"].contains(path) {
            let list = path == "/api/custom-webhooks/create" ? "/api/custom-webhooks/list" : path
            var row = body;row["id"] = path == "/api/geofences" ? body["name"] : "created-hook"
            records[list,default:[]].append(row);return (200,["success":true,path == "/api/geofences" ? "geofence" : "webhook":row])
        }
        if req.httpMethod == "DELETE" {
            let id = path.components(separatedBy:"/").last!,key = path.hasPrefix("/api/custom-webhooks/") ? "/api/custom-webhooks/list" : path.hasPrefix("/api/geofences/") ? "/api/geofences" : "/api/outgoing-webhooks"
            records[key]?.removeAll { $0["id"] as? String == id };return (200,["success":true,"id":id])
        }
        if path.hasSuffix("/test") {return (200,["delivered":false,"event_type":"test.ping"])}
        if path == "/api/routines/7" {return (200,["routine":["id":7,"session_id":"fixture-session","payload":["secret":"withhold"]],"runs":[["id":1,"job_id":7,"status":"success","started_at":123,"result":"private-result","error":"private-token"]]])}
        return (404,["error":"private-server-detail"])
    }
    override class func canInit(with request:URLRequest) -> Bool { true }
    override class func canonicalRequest(for request:URLRequest) -> URLRequest { request }
    override func startLoading() {
        var req = request
        if req.httpBody == nil,let stream = req.httpBodyStream {stream.open();defer {stream.close()};var data = Data(),buffer = [UInt8](repeating:0,count:4096);while stream.hasBytesAvailable {let n = stream.read(&buffer,maxLength:buffer.count);if n <= 0 {break};data.append(buffer,count:n)};req.httpBody = data}
        Self.requests.append(req);let (code,value) = Self.handler(req)
        let deliver = { [self] in
        client?.urlProtocol(self,didReceive:HTTPURLResponse(url:req.url!,statusCode:code,httpVersion:nil,headerFields:nil)!,cacheStoragePolicy:.notAllowed)
        client?.urlProtocol(self,didLoad:try! JSONSerialization.data(withJSONObject:value));client?.urlProtocolDidFinishLoading(self)
        }
        if Self.delayPath == req.url!.path && (Self.delayMethod == nil || Self.delayMethod == req.httpMethod) { Self.delayPath = nil;Self.delayed = true;DispatchQueue.global().asyncAfter(deadline:.now()+0.1,execute:deliver) } else { deliver() }
    }
    override func stopLoading() {}
}
@main struct NativeAutomationFeatureTests {
    static var count = 0
    @MainActor static func check(_ value:Bool,_ label:String) {if !value {fatalError("FAIL: \(label)")};count += 1}
    @MainActor static func main() async throws {
        let config = URLSessionConfiguration.ephemeral;config.protocolClasses = [AutomationFixture.self]
        let session = URLSession(configuration:config),base = URL(string:"http://127.0.0.1:9464")!,model = NativeAutomationModel(session:session)
        model.configure(base);check(AutomationFixture.requests.isEmpty && model.rows.isEmpty,"configure does no lazy-init GET or scheduler start")
        let load = try model.review(.load(.geofences));check(AutomationFixture.requests.isEmpty,"inventory review inert")
        check(await model.perform(load) && model.rows[.geofences]?.isEmpty == true && model.receipt?.contains("not proven") == true,"empty reported inventory distinct from engine availability")
        let countBefore = AutomationFixture.requests.count
        check(!(await model.perform(load)) && AutomationFixture.requests.count == countBefore,"read approval single use")
        let fence:[String:Any] = ["name":"home","lat":12.5,"lon":-25.25,"radius_m":200.0,"on_enter":"Remind me","on_exit":""]
        let create = try model.review(.create(.geofences,fence))
        check(create.detail.contains("12.5") && create.detail.contains("permissions") && create.detail.contains("Remind me"),"location/action review exact privacy scope no GPS proof")
        check(await model.perform(create) && model.rows[.geofences]?.count == 1,"create exact geofence full fields readback")
        do {_ = try model.review(.create(.geofences,fence));fatalError("same name replacement accepted")} catch {}
        check(AutomationFixture.requests.filter {$0.httpMethod == "POST"}.count == 1,"existing geofence requires new name no blind replacement")
        var invalid = fence;invalid["name"] = "bad";invalid["lat"] = 91.0
        do {_ = try model.review(.create(.geofences,invalid));fatalError("invalid latitude accepted")} catch {}
        check(AutomationFixture.requests.filter {$0.httpMethod == "POST"}.count == 1,"invalid coordinates blocked before wire")
        let deletion = try model.review(.delete(.geofences,"home"));AutomationFixture.records["/api/geofences"]?[0]["radius_m"] = 400.0
        check(!(await model.perform(deletion)) && !AutomationFixture.requests.contains {$0.httpMethod == "DELETE"},"fresh changed geofence blocks stale delete")
        check(await model.perform(try model.review(.load(.geofences))),"reload changed inventory")
        check(await model.perform(try model.review(.delete(.geofences,"home"))) && model.rows[.geofences]?.isEmpty == true,"exact deletion confirmed absent readback")
        check(await model.perform(try model.review(.load(.inbound))),"inbound inventory reviewed")
        let hook:[String:Any] = ["name":"signed-in","secret":"private-signing-secret","action":"chat","action_params":["prefix":"Fixture: "]]
        let inbound = try model.review(.create(.inbound,hook))
        check(!inbound.detail.contains("private-signing-secret") && inbound.detail.contains("first active") && inbound.detail.contains("reachability"),"signed ingress review hides new secret and exact session/exposure caveats")
        check(await model.perform(inbound) && model.rows[.inbound]?.first?.fields["secret"] == nil && model.rows[.inbound]?.first?.fields["signed"] as? Bool == true,"inbound secret retained privately never rendered")
        var unsigned = hook;unsigned["secret"] = ""
        do {_ = try model.review(.create(.inbound,unsigned));fatalError("unsigned create accepted")} catch {}
        check(AutomationFixture.requests.filter {$0.url!.path == "/api/custom-webhooks/create"}.count == 1,"new unsigned ingress refused before wire")
        check(await model.perform(try model.review(.load(.outgoing))),"outgoing reviewed inventory")
        let outgoing:[String:Any] = ["name":"signed-out","target_url":"https://recipient.fixture/ingest?token=private-query","secret":"private-secret","event_types":["memory.*"],"enabled":true]
        let subscription = try model.review(.create(.outgoing,outgoing))
        check(subscription.detail.contains("recipient.fixture") && subscription.detail.contains("memory.*") && subscription.detail.contains("health") && !subscription.detail.contains("private-query") && !subscription.detail.contains("private-secret"),"outgoing privilege and event scope reviewed without secret URL query")
        check(await model.perform(subscription) && model.rows[.outgoing]?.first?.fields["target_host"] as? String == "recipient.fixture" && model.rows[.outgoing]?.first?.fields["target_url"] == nil,"outgoing exact persisted fields and only target host displayed")
        let normalBefore = AutomationFixture.normal
        var mismatchSubscription = outgoing;mismatchSubscription["name"] = "secret-mismatch"
        AutomationFixture.handler = { req in
            let result = normalBefore(req)
            if req.url!.path == "/api/outgoing-webhooks",req.httpMethod == "POST" { var value = result.1;var record = value["webhook"] as! [String:Any];record["id"] = "mismatch-hook";record["secret"] = "wrong-private-secret";value["webhook"] = record;AutomationFixture.records["/api/outgoing-webhooks"]?.removeLast();var stored = record;stored["secret"] = "••••••••";stored["has_secret"] = true;AutomationFixture.records["/api/outgoing-webhooks"]?.append(stored);return (200,value) }
            return result
        }
        check(!(await model.perform(try model.review(.create(.outgoing,mismatchSubscription)))) && model.receipt == nil && model.error?.contains("exact supplied") == true && model.error?.contains("wrong-private-secret") == false,"secret-present readback cannot hide mismatched private creation-secret receipt")
        AutomationFixture.handler = normalBefore
        let safeRecord = AutomationFixture.records["/api/outgoing-webhooks"]![0]
        for unsafeTarget in ["http://recipient.fixture/ingest","https://user:secret@recipient.fixture/ingest","https://recipient.fixture/ingest#fragment"] {
            var legacy = safeRecord;legacy["target_url"] = unsafeTarget;AutomationFixture.records["/api/outgoing-webhooks"]?[0] = legacy
            _ = await model.perform(try model.review(.load(.outgoing)))
            let before = AutomationFixture.requests.count
            do { _ = try model.review(.testOutgoing("created-hook"));fatalError("unsafe legacy test target accepted") } catch {}
            check(AutomationFixture.requests.count == before,"legacy unsafe target refuses synthetic delivery before wire")
        }
        AutomationFixture.records["/api/outgoing-webhooks"]?[0] = safeRecord
        _ = await model.perform(try model.review(.load(.outgoing)))
        var insecure = outgoing;insecure["target_url"] = "http://recipient.fixture"
        do {_ = try model.review(.create(.outgoing,insecure));fatalError("HTTP recipient accepted")} catch {}
        check(AutomationFixture.requests.filter {$0.url!.path == "/api/outgoing-webhooks" && $0.httpMethod == "POST"}.count == 2,"unencrypted outgoing endpoint refused")
        check(await model.perform(try model.review(.testOutgoing("created-hook"))) && model.receipt?.contains("failed") == true,"false synthetic delivery is valid failed verdict not fake success")
        let testReq = AutomationFixture.requests.last {$0.url!.path.hasSuffix("/test")}!,testBody = try JSONSerialization.jsonObject(with:testReq.httpBody!) as! [String:Any]
        check(testBody["event_type"] as? String == "test.ping" && (testBody["payload"] as? [String:String]) == ["message":"FERAL synthetic connection test"],"test sends only fixed synthetic fixture no personal state")
        let normal = AutomationFixture.normal
        AutomationFixture.handler = {req in req.url!.path.hasSuffix("/test") ? (200,["delivered":true,"event_type":"foreign.event"]) : normal(req)}
        check(!(await model.perform(try model.review(.testOutgoing("created-hook")))) && model.receipt == nil && model.error?.contains("uncertain") == true,"foreign event receipt uncertain not claimed success")
        AutomationFixture.handler = {req in req.httpMethod == "DELETE" ? (200,["success":true]) : normal(req)}
        check(!(await model.perform(try model.review(.delete(.outgoing,"created-hook")))) && model.error?.contains("readback") == true,"lying deletion receipt rejected by readback")
        AutomationFixture.handler = {req in req.url!.path == "/api/geofences" ? (200,["geofences":[],"error":"private-location-secret"]) : normal(req)}
        check(!(await model.perform(try model.review(.load(.geofences)))) && model.rows[.geofences] == nil && model.errors[.geofences] != nil && model.error?.contains("private-location") == false,"failure clears inventory unknown versus none and hides private errors")
        AutomationFixture.handler = {req in req.url!.path == "/api/outgoing-webhooks" ? (200,["webhooks":[["id":"duplicate"],["id":"duplicate"]]]) : normal(req)}
        check(!(await model.perform(try model.review(.load(.outgoing)))) && model.rows[.outgoing] == nil,"duplicate IDs fail closed")
        AutomationFixture.handler = normal
        check(await model.perform(try model.review(.runs("7"))) && model.runRows?.count == 1 && model.runRows?.first?["result"] == nil && model.runRows?.first?["error"] == nil,"bounded exact routine metadata excludes private raw output/error")
        check(model.routineDispatch?.state == "unavailable","legacy response never invents available occurrence tracking")
        let occurrence:[String:Any] = ["occurrence_id":"12345678-1234-1234-1234-123456789012","job_id":7,"status":"outcome_unknown","rearmed_at":NSNull(),"payload":"private-occurrence-content"]
        let dispatch:[String:Any] = ["tracking_available":true,"dispatch_state":"reconciliation_required","reconciliation_required":true,"occurrence":occurrence]
        AutomationFixture.handler = { req in
            if req.url!.path == "/api/routines/7" { return (200,["routine":["id":7],"runs":[],"dispatch":dispatch]) }
            return normal(req)
        }
        check(await model.perform(try model.review(.runs("7"))) && model.runRows?.isEmpty == true && model.routineDispatch?.requiresReconciliation == true,"empty run history retains unresolved action instead of implying successful or idle automation")
        check(model.routineDispatch?.message.contains("Automatic replay is blocked") == true && model.routineDispatch?.message.contains("private-occurrence-content") == false,"review-needed message explains replay fence without displaying raw action content")
        for invalid in [
            ["tracking_available":1,"dispatch_state":"scheduled","reconciliation_required":false,"occurrence":NSNull()],
            ["tracking_available":true,"dispatch_state":"scheduled","reconciliation_required":false,"occurrence":occurrence],
            ["tracking_available":true,"dispatch_state":"reconciliation_required","reconciliation_required":false,"occurrence":occurrence],
            ["tracking_available":true,"dispatch_state":"in_progress","reconciliation_required":false,"occurrence":occurrence],
            ["tracking_available":true,"dispatch_state":"reconciliation_required","reconciliation_required":true,"occurrence":["occurrence_id":"12345678-1234-1234-1234-123456789012","job_id":8,"status":"claimed"]]
        ] as [[String:Any]] {
            AutomationFixture.handler = { _ in (200,["routine":["id":7],"runs":[],"dispatch":invalid]) }
            check(!(await model.perform(try model.review(.runs("7")))) && model.routineDispatch == nil && model.runRows == nil,"malformed or foreign occurrence cannot establish ready/running/review state")
        }
        AutomationFixture.handler = normal
        check(!AutomationFixture.requests.contains {$0.url!.path == "/api/routines"},"run inspection never starts scheduler via list endpoint")
        AutomationFixture.handler = {req in req.url!.path == "/api/routines/7" ? (200,["routine":["id":8],"runs":[]]) : normal(req)}
        check(!(await model.perform(try model.review(.runs("7")))) && model.receipt == nil,"foreign routine ID never adopted")
        AutomationFixture.handler = normal
        let stale = try model.review(.load(.inbound));model.configure(nil);let staleCount = AutomationFixture.requests.count
        check(!(await model.perform(stale)) && AutomationFixture.requests.count == staleCount && model.runRows == nil && model.routineDispatch == nil,"connection clears private metadata and invalidates review")
        model.configure(URL(string:"https://external.fixture")!);check(!(await model.perform(try model.review(.load(.inbound)))) && AutomationFixture.requests.count == staleCount,"nonloopback refused before transport")
        AutomationFixture.handler = normal;model.configure(base)
        let blockedPolicy = NativeSelectedContextPolicy(sessionID:"blocked",connectionID:UUID(),taskReady:false,managed:true)
        model.setContextPolicy(blockedPolicy)
        let beforeBlocked = AutomationFixture.requests.count
        check(await model.perform(try model.review(.load(.geofences))),"blocked chat retains reviewed trigger inventory")
        check(await model.perform(try model.review(.runs("7"))),"blocked chat retains exact routine run inspection")
        var blockedFence = fence;blockedFence["name"] = "context-fence"
        do { _ = try model.review(.create(.geofences,blockedFence));fatalError("unready trigger permitted") } catch {}
        check(AutomationFixture.requests.dropFirst(beforeBlocked).allSatisfy { $0.httpMethod == "GET" },"blocked trigger creation sends no POST")
        check(await model.perform(try model.review(.load(.outgoing))),"blocked chat retains outgoing inventory")
        do { _ = try model.review(.testOutgoing("created-hook"));fatalError("unready real outbound test permitted") } catch {}
        check(await model.perform(try model.review(.delete(.outgoing,"created-hook"))),"blocked chat retains global trigger deletion")
        model.setContextPolicy(NativeSelectedContextPolicy(sessionID:"ready",connectionID:UUID(),taskReady:true,managed:false))
        let staleCreate = try model.review(.create(.geofences,blockedFence))
        AutomationFixture.delayPath = "/api/geofences";AutomationFixture.delayed = false
        let policyRace = Task { await model.perform(staleCreate) }
        for _ in 0..<1000 { if AutomationFixture.delayed { break };try await Task.sleep(nanoseconds:1_000_000) }
        let beforePolicySwitch = AutomationFixture.requests.filter { $0.httpMethod != "GET" }.count
        model.setContextPolicy(NativeSelectedContextPolicy(sessionID:"other",connectionID:UUID(),taskReady:true,managed:false))
        check(!(await policyRace.value) && AutomationFixture.requests.filter { $0.httpMethod != "GET" }.count == beforePolicySwitch,"context switch after trigger preflight prevents arming POST")
        AutomationFixture.handler = normal
        let lateCreation = try model.review(.create(.geofences,blockedFence))
        let beforeLateCreate = AutomationFixture.requests.filter { $0.httpMethod == "POST" && $0.url!.path == "/api/geofences" }.count
        AutomationFixture.delayed = false;AutomationFixture.delayMethod = "GET"
        AutomationFixture.handler = { request in
            let response = normal(request)
            if request.httpMethod == "POST",request.url!.path == "/api/geofences" { AutomationFixture.delayPath = "/api/geofences" }
            return response
        }
        let lateCreationTask = Task { await model.perform(lateCreation) }
        for _ in 0..<1000 { if AutomationFixture.delayed { break };try await Task.sleep(nanoseconds:1_000_000) }
        check(AutomationFixture.delayed && model.receipt == nil && AutomationFixture.requests.filter { $0.httpMethod == "POST" && $0.url!.path == "/api/geofences" }.count == beforeLateCreate + 1,"automation fixture holds post-create readback without early success")
        model.setContextPolicy(NativeSelectedContextPolicy(sessionID:"after-write",connectionID:UUID(),taskReady:false,managed:true))
        check(!(await lateCreationTask.value) && model.receipt == nil && model.error?.contains("partial or uncertain") == true && !model.busy,"stale automation readback cannot leave success receipt beside uncertain error")
        check(!(await model.perform(lateCreation)) && AutomationFixture.requests.filter { $0.httpMethod == "POST" && $0.url!.path == "/api/geofences" }.count == beforeLateCreate + 1,"late automation outcome cannot replay arming")
        AutomationFixture.handler = normal;AutomationFixture.delayMethod = nil
        print("PASS: \(count) native Automation fixture assertions; no real location samples, webhooks, external delivery, scheduled jobs or credentials")
        session.invalidateAndCancel()
    }
}
