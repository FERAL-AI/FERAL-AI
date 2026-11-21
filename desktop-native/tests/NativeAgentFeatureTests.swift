import Foundation

final class AgentFixture: URLProtocol {
    static var requests:[URLRequest] = []
    static var agents:[[String:Any]] = []
    static var persona:[String:Any] = ["agent_id":"research","name":"Research","description":"Local fixture","system_prompt":"Research a topic.","tool_permissions":["memory.search"],"memory_filter":"research-only","schedule":NSNull(),"version":"future-version","future_field":["keep":true]]
    static var proposal:[String:Any] = ["pattern_id":"pattern-a","name":"Notes Agent","topic":"notes","tools":["memory.search"],"sample_prompts":["Private fixture topic"],"seen_count":7,"time_pattern":"morning"]
    static var delayPath:String?
    static var delayed = false
    static var delayMethod:String?
    static var handler:(URLRequest) -> (Int,Any) = normal
    static func normal(_ request:URLRequest) -> (Int,Any) {
        let path = request.url!.path
        let body = (try? JSONSerialization.jsonObject(with:request.httpBody ?? Data())) as? [String:Any] ?? [:]
        switch path {
        case "/api/agents/personas": return (200,["personas":[persona]])
        case "/api/agents/list": return (200,["agents":agents])
        case "/api/agents/proposals": return (200,["proposals":[proposal]])
        case "/api/agents/stats": return (200,["patterns_tracked":1,"proposals_ready":1,"specialists_active":agents.count,"total_tasks":0])
        case "/api/agents/spawn":
            if body["pattern_id"] != nil {
                let row:[String:Any] = ["agent_id":"specialist_notes","name":"Notes Agent","tools":proposal["tools"]!,"schedule":"0 9 * * *","tasks":0,"satisfaction":0.5]
                agents.removeAll { $0["agent_id"] as? String == "specialist_notes" };agents.append(row)
                return (200,["success":true,"source":"pattern","agent":["agent_id":"specialist_notes","name":"Notes Agent"]])
            }
            let id = body["agent_id"] as! String
            let row:[String:Any] = ["agent_id":id,"name":body["name"]!,"description":body["description"] ?? "","tools":body["tool_permissions"]!,"schedule":body["schedule"] ?? NSNull(),"tasks":0,"satisfaction":0.5,"last_active":0,"opaque_counter":"keep"]
            agents.removeAll { $0["agent_id"] as? String == id };agents.append(row)
            return (200,["success":true,"source":"persona_manifest","agent":["agent_id":id,"name":body["name"]!,"tool_permissions":body["tool_permissions"]!,"memory_filter":body["memory_filter"] ?? NSNull()]])
        case "/api/agents/feedback":
            if let index = agents.firstIndex(where:{$0["agent_id"] as? String == body["agent_id"] as? String}) {
                let tasks = (agents[index]["tasks"] as! NSNumber).intValue
                let score = (agents[index]["satisfaction"] as! NSNumber).doubleValue
                agents[index]["tasks"] = tasks + 1
                agents[index]["satisfaction"] = max(0,min(1,score + (body["positive"] as? Bool == true ? 0.05 : -0.1)))
                agents[index]["last_active"] = 1
            }
            return (200,["success":true])
        default:return (404,["error":"Unknown fixture route"])
        }
    }
    static func body(_ request:URLRequest) -> Data {
        if let data = request.httpBody { return data }
        guard let stream = request.httpBodyStream else { return Data() }
        stream.open();defer { stream.close() };var data = Data(),buffer = [UInt8](repeating:0,count:4096)
        while stream.hasBytesAvailable { let n = stream.read(&buffer,maxLength:buffer.count);if n <= 0 { break };data.append(buffer,count:n) }
        return data
    }
    override class func canInit(with request:URLRequest) -> Bool { true }
    override class func canonicalRequest(for request:URLRequest) -> URLRequest { request }
    override func startLoading() {
        var captured = request
        if captured.httpMethod == "POST" { captured.httpBody = Self.body(request) }
        Self.requests.append(captured)
        let (status,value) = Self.handler(captured)
        let response = HTTPURLResponse(url:request.url!,statusCode:status,httpVersion:nil,headerFields:nil)!
        let data = try! JSONSerialization.data(withJSONObject:value)
        let deliver = { self.client?.urlProtocol(self,didReceive:response,cacheStoragePolicy:.notAllowed);self.client?.urlProtocol(self,didLoad:data);self.client?.urlProtocolDidFinishLoading(self) }
        if Self.delayPath == request.url!.path && (Self.delayMethod == nil || Self.delayMethod == request.httpMethod) { Self.delayPath = nil;Self.delayed = true;DispatchQueue.global().asyncAfter(deadline:.now()+0.1,execute:deliver) }
        else { deliver() }
    }
    override func stopLoading() {}
    static var posts:Int { requests.filter { $0.httpMethod == "POST" }.count }
}

@main struct NativeAgentFeatureTests {
    static var count = 0
    @MainActor static func check(_ value:Bool,_ name:String) { guard value else { fatalError("FAIL: \(name)") };count += 1 }
    @MainActor static func main() async throws {
        let config = URLSessionConfiguration.ephemeral;config.protocolClasses = [AgentFixture.self]
        let session = URLSession(configuration:config),model = NativeAgentModel(session:session),base = URL(string:"http://127.0.0.1:9464")!
        await model.configure(baseURL:nil)
        check(!model.available && AgentFixture.requests.isEmpty,"nil readiness has no requests")
        await model.configure(baseURL:base)
        check(model.errors.isEmpty && model.rows("personas").count == 1 && model.rows("agents").isEmpty,"actual catalogue contracts read independently")
        check(AgentFixture.requests.count == 4 && AgentFixture.requests.allSatisfy { $0.httpMethod == "GET" },"passive inventory never spawns or probes providers")
        check(model.rows("personas")[0].raw["future_field"] != nil,"catalogue preserves unknown metadata for inspection")
        let before = AgentFixture.requests.count
        let first = try model.review(.persona("research"))
        check(AgentFixture.requests.count == before && first.explanation.contains("no model call"),"inert persona review distinguishes registration from execution")
        check(first.source["future_field"] == nil && first.source["version"] == nil && first.source["memory_filter"] as? String == "research-only","review sends supported manifest fields and includes memory scope")
        check(await model.perform(first) && model.receipt?.contains("no task was started") == true,"genuine registration receipt followed by exact scope readback")
        let post = AgentFixture.requests.last { $0.httpMethod == "POST" }!
        let sent = try JSONSerialization.jsonObject(with:post.httpBody!) as! [String:Any]
        check(sent["agent_id"] as? String == "research" && sent["memory_filter"] as? String == "research-only" && sent["version"] == nil,"persona route exact manifest body")
        let replayPosts = AgentFixture.posts
        check(!(await model.perform(first)) && AgentFixture.posts == replayPosts,"single-use successful review cannot replay")
        let replace = try model.review(.persona("research"))
        check(replace.title.contains("Replace") && replace.explanation.contains("resets its runtime counters"),"same-ID overwrite explicitly disclosed")
        AgentFixture.persona["tool_permissions"] = ["shell.execute"]
        check(!(await model.perform(replace)) && AgentFixture.posts == replayPosts,"changed catalogue scope blocks overwrite before POST")
        check(model.actionError?.contains("persona changed") == true,"changed scope error explicit")
        AgentFixture.persona["tool_permissions"] = ["memory.search"]
        await model.refresh()
        let collision = try model.review(.custom(["agent_id":"new","name":"New","system_prompt":"Help","tool_permissions":[]]))
        AgentFixture.agents.append(["agent_id":"new","name":"Unexpected","tools":[],"tasks":0,"satisfaction":0.5])
        check(!(await model.perform(collision)) && AgentFixture.posts == replayPosts,"new same-ID collision requires a fresh replacement review")
        AgentFixture.agents.removeAll { $0["agent_id"] as? String == "new" };await model.refresh()
        let custom:[String:Any] = ["agent_id":"custom","name":"Custom","description":"User authored","system_prompt":"Exact custom prompt","tool_permissions":[],"schedule":"0 9 * * *","memory_filter":"private","unknown_key":"not-applied"]
        let customReview = try model.review(.custom(custom))
        check(customReview.explanation.contains("does not execute") && customReview.source["unknown_key"] == nil,"custom scopes and unsupported keys are honest")
        check(await model.perform(customReview) && model.rows("agents").contains { $0.id == "custom" },"custom registration uses supported route")
        let invalidPosts = AgentFixture.posts
        do { _ = try model.review(.custom(["agent_id":"bad","name":"Bad","system_prompt":"Help","tool_permissions":[1]]));fatalError("bad scope accepted") } catch {}
        check(AgentFixture.posts == invalidPosts,"invalid custom permissions rejected without dispatch")
        let feedback = try model.review(.feedback(id:"research",positive:true))
        check(feedback.explanation.contains("not verification"),"feedback does not claim task success")
        check(await model.perform(feedback) && model.receipt?.contains("counters verified") == true,"feedback counter and bounded score read back")
        let feedbackBody = try JSONSerialization.jsonObject(with:AgentFixture.requests.last { $0.httpMethod == "POST" }!.httpBody!) as! [String:Any]
        check(feedbackBody["agent_id"] as? String == "research" && feedbackBody["positive"] as? Bool == true && feedbackBody.count == 2,"feedback actual backend body only")
        AgentFixture.agents[0]["satisfaction"] = 1.0;await model.refresh()
        check(await model.perform(try model.review(.feedback(id:"research",positive:true))),"positive feedback at saturation verifies clamped score")
        AgentFixture.agents[0]["satisfaction"] = 0.0;await model.refresh()
        check(await model.perform(try model.review(.feedback(id:"research",positive:false))),"negative feedback at zero verifies clamped score")
        AgentFixture.agents[0]["tasks"] = true;await model.refresh()
        do { _ = try model.review(.feedback(id:"research",positive:true));fatalError("boolean task counter accepted") } catch {}
        check(model.rows("agents")[0].raw["tasks"] as? Bool == true,"malformed counter does not make feedback executable")
        AgentFixture.agents[0]["tasks"] = 3;AgentFixture.agents[0]["satisfaction"] = 0.5;await model.refresh()
        let staleFeedback = try model.review(.feedback(id:"research",positive:false))
        AgentFixture.agents[0]["tasks"] = 5
        let stalePosts = AgentFixture.posts
        check(!(await model.perform(staleFeedback)) && AgentFixture.posts == stalePosts,"concurrent feedback counter change blocks stale write")
        await model.refresh()
        let proposal = try model.review(.proposal("pattern-a"))
        check(proposal.explanation.contains("cloud provider/fallback") && proposal.explanation.contains("Private fixture topic") && proposal.explanation.contains("cannot be inspected"),"proposal review discloses LLM costs, data and immediate generated-prompt persistence")
        check(await model.perform(proposal) && model.rows("agents").contains { $0.id == "specialist_notes" },"pattern-generated specialist exact ID and tool scope verified")
        let proposalBody = try JSONSerialization.jsonObject(with:AgentFixture.requests.last { $0.httpMethod == "POST" }!.httpBody!) as! [String:Any]
        check(proposalBody.count == 1 && proposalBody["pattern_id"] as? String == "pattern-a","pattern path never silently submits persona manifest")
        await model.refresh()
        let changedProposal = try model.review(.proposal("pattern-a"))
        AgentFixture.proposal["sample_prompts"] = ["Changed private text"]
        let proposalPosts = AgentFixture.posts
        check(!(await model.perform(changedProposal)) && AgentFixture.posts == proposalPosts,"sample prompt changes invalidate cloud review")
        AgentFixture.proposal["sample_prompts"] = ["Private fixture topic"]
        let normal = AgentFixture.normal
        AgentFixture.handler = { request in request.url!.path == "/api/agents/personas" ? (200,["personas":[AgentFixture.persona,AgentFixture.persona]]) : normal(request) }
        await model.refresh()
        check(model.errors["personas"] != nil && model.inventories["personas"] == nil && model.rows("agents").count > 0,"duplicate inventory fails independently without replacing valid other sections")
        do { _ = try model.review(.persona("research"));fatalError("duplicate persona accepted") } catch {}
        check(AgentFixture.posts == proposalPosts,"malformed catalogue cannot dispatch registration")
        AgentFixture.handler = normal;await model.refresh()
        let held = try model.review(.feedback(id:"research",positive:false))
        await model.refresh()
        check(!(await model.perform(held)) && AgentFixture.posts == proposalPosts,"refresh invalidates held review")
        let falseSuccess = try model.review(.feedback(id:"research",positive:false))
        AgentFixture.handler = { request in request.httpMethod == "POST" ? (200,["success":true]) : normal(request) }
        check(!(await model.perform(falseSuccess)) && model.actionError?.contains("write may already") == true,"HTTP success alone cannot prove feedback effect")
        let afterFailure = AgentFixture.posts
        check(!(await model.perform(falseSuccess)) && AgentFixture.posts == afterFailure,"uncertain failed receipt never automatically retries or reuses review")
        AgentFixture.handler = normal;await model.refresh()
        let wrongReceipt = try model.review(.custom(["agent_id":"wrong","name":"Wrong receipt","system_prompt":"Help","tool_permissions":[]]))
        AgentFixture.handler = { request in request.httpMethod == "POST" ? (200,["success":true,"source":"persona_manifest","agent":["agent_id":"another"]]) : normal(request) }
        check(!(await model.perform(wrongReceipt)) && model.actionError?.contains("did not identify") == true,"mismatched registration receipt refuses success")
        AgentFixture.handler = normal;await model.refresh()
        let negative = try model.review(.feedback(id:"research",positive:false))
        AgentFixture.handler = { request in request.httpMethod == "POST" ? (200,["success":false,"error":"private-secret-canary"]) : normal(request) }
        check(!(await model.perform(negative)) && model.actionError?.contains("private-secret-canary") == false,"negative service receipt cannot echo backend secrets")
        AgentFixture.handler = normal;await model.refresh()
        let delayedReview = try model.review(.feedback(id:"research",positive:false))
        AgentFixture.delayPath = "/api/agents/list";AgentFixture.delayed = false
        let task = Task { await model.perform(delayedReview) }
        for _ in 0..<1000 { if AgentFixture.delayed { break };try await Task.sleep(nanoseconds:1_000_000) }
        let beforeDisconnect = AgentFixture.posts
        await model.configure(baseURL:nil)
        check(!(await task.value) && AgentFixture.posts == beforeDisconnect && !model.available,"disconnect during preflight prevents POST and stale state publication")
        await model.configure(baseURL:URL(string:"http://example.com")!)
        check(model.inventories.isEmpty && AgentFixture.posts == beforeDisconnect,"non-loopback service refused before network dispatch")
        AgentFixture.handler = normal;await model.configure(baseURL:base)
        let blockedPolicy = NativeSelectedContextPolicy(sessionID:"blocked",connectionID:UUID(),taskReady:false,managed:true)
        model.setContextPolicy(blockedPolicy)
        let beforeBlocked = AgentFixture.posts
        do { _ = try model.review(.proposal("pattern-a"));fatalError("unready proposal permitted") } catch {}
        await model.refresh()
        check(AgentFixture.posts == beforeBlocked && !model.inventories.isEmpty,"blocked context retains passive inventory with no proposal POST")
        check(await model.perform(try model.review(.feedback(id:"research",positive:true))),"global feedback remains available while chat blocked")
        let switched = try model.review(.feedback(id:"research",positive:false))
        AgentFixture.delayPath = "/api/agents/list";AgentFixture.delayed = false
        let policyRace = Task { await model.perform(switched) }
        for _ in 0..<1000 { if AgentFixture.delayed { break };try await Task.sleep(nanoseconds:1_000_000) }
        let beforePolicySwitch = AgentFixture.posts
        model.setContextPolicy(NativeSelectedContextPolicy(sessionID:"other",connectionID:UUID(),taskReady:true,managed:false))
        check(!(await policyRace.value) && AgentFixture.posts == beforePolicySwitch,"context switch after awaited inventory invalidates metadata review before POST")
        AgentFixture.handler = normal;await model.refresh()
        let lateReadback = try model.review(.feedback(id:"research",positive:true))
        let beforeLatePost = AgentFixture.posts
        AgentFixture.delayed = false;AgentFixture.delayMethod = "GET"
        AgentFixture.handler = { request in
            let response = normal(request)
            if request.httpMethod == "POST" { AgentFixture.delayPath = "/api/agents/list" }
            return response
        }
        let lateReadbackTask = Task { await model.perform(lateReadback) }
        for _ in 0..<1000 { if AgentFixture.delayed { break };try await Task.sleep(nanoseconds:1_000_000) }
        check(AgentFixture.delayed && AgentFixture.posts == beforeLatePost + 1 && model.receipt == nil,"specialist fixture holds post-write readback without early receipt")
        model.setContextPolicy(NativeSelectedContextPolicy(sessionID:"after-write",connectionID:UUID(),taskReady:false,managed:true))
        check(!(await lateReadbackTask.value) && model.receipt == nil && model.actionError?.contains("write may already") == true && AgentFixture.posts == beforeLatePost + 1,"late specialist readback after policy switch sends one write and publishes no stale success")
        check(!(await model.perform(lateReadback)) && AgentFixture.posts == beforeLatePost + 1,"late specialist outcome cannot replay consumed review")
        AgentFixture.handler = normal;AgentFixture.delayMethod = nil
        print("Native specialist feature: \(count) assertions passed.")
    }
}
