import Foundation

final class SurfaceSocketFixture:NativeSurfaceSocket {
    static var sockets:[SurfaceSocketFixture]=[]
    static var urls:[URL]=[]
    static var failNextHandshake=false
    private let lock=NSLock()
    private var sentFrames:[[String:Any]]=[]
    private var isCancelled=false
    var sent:[[String:Any]]{lock.lock();defer{lock.unlock()};return sentFrames}
    var cancelled:Bool{lock.lock();defer{lock.unlock()};return isCancelled}
    private var queue:[[String:Any]]=[]
    private var waiting:CheckedContinuation<[String:Any],Error>?
    let sid:String
    let failHandshake:Bool
    init(url:URL){Self.urls.append(url);sid=URLComponents(url:url,resolvingAgainstBaseURL:false)!.queryItems!.first{$0.name=="session_id"}!.value!;failHandshake=Self.failNextHandshake;Self.failNextHandshake=false;Self.sockets.append(self)}
    func resume(){}
    func enqueue(_ frame:[String:Any]){
        lock.lock()
        guard !isCancelled else{lock.unlock();return}
        let receiver=waiting
        if receiver != nil{waiting=nil}else{queue.append(frame)}
        lock.unlock()
        receiver?.resume(returning:frame)
    }
    private func recordSend(_ frame:[String:Any])->Bool{
        lock.lock();defer{lock.unlock()}
        guard !isCancelled else{return false};sentFrames.append(frame);return true
    }
    func send(_ frame:[String:Any]) async throws {
        guard recordSend(frame) else{throw NativeSurfaceFailure("Fixture socket closed")}
        if frame["type"] as? String=="req"{enqueue(["type":"res","id":frame["id"]!,"ok":!failHandshake,"payload":[:],"error":["message":"private-server-canary"]])}
        else if let payload=frame["payload"] as? [String:Any],let id=payload["action_id"] as? String,id.hasPrefix("perm_"){
            let grant=id.hasPrefix("perm_grant_"),requestID=String(id.dropFirst(grant ? "perm_grant_".count : "perm_deny_".count))
            enqueue(["type":"permission_decision","session_id":sid,"payload":["request_id":requestID,"status":grant ? "granted" : "denied","path":"/private/tmp","mode":"readwrite","scope":"persistent_workspace"]])
        }
    }
    func receive() async throws -> [String:Any]{
        try await withCheckedThrowingContinuation{receiver in
            lock.lock()
            if isCancelled{lock.unlock();receiver.resume(throwing:NativeSurfaceFailure("Fixture socket closed"));return}
            if !queue.isEmpty{let next=queue.removeFirst();lock.unlock();receiver.resume(returning:next);return}
            if waiting != nil{lock.unlock();receiver.resume(throwing:NativeSurfaceFailure("Fixture already has a receiver"));return}
            waiting=receiver;lock.unlock()
        }
    }
    func cancel(){
        lock.lock();isCancelled=true;let receiver=waiting;waiting=nil;queue=[];lock.unlock()
        receiver?.resume(throwing:NativeSurfaceFailure("Fixture socket closed"))
    }
}

final class SurfaceFixture:URLProtocol {
    static var requests:[URLRequest] = []
    static var delayed = false
    static var delayInterval:TimeInterval=0.1
    static var delayByPath:[String:TimeInterval]=[:]
    static var delayPath:String?
    static var handler:(URLRequest) -> (Int,Any) = normal
    static let nullAction:[String:Any] = ["action_id":"go","handler":"navigate","target":"details","description":"Open declared details","requires_confirmation":false,"value_schema":["type":"null"]]
    static let formAction:[String:Any] = ["action_id":"submit","handler":"app_event","target":"","description":"Send note to agent","requires_confirmation":false,"value_schema_ref":"#/data_schemas/form-value"]
    static let closeAction:[String:Any] = ["action_id":"close","handler":"close","target":"","requires_confirmation":false]
    static var tree:[String:Any] = ["type":"VStack","children":[["type":"Text","value":"Literal <script>never execute</script> [link](https://private.example)"],["type":"Button","action_id":"go","label":"Go"],["type":"FormView","action_id":"submit","fields":[["name":"note","label":"Note","type":"text","value":"Local draft"]]],["type":"Button","action_id":"close","label":"Close"],["type":"WebView","url":"https://private.example"]]]
    static var manifest:[String:Any] = ["app_id":"fixture-app","version":"1.0","entry_surface_id":"home","permissions":["memory.read"],"surfaces":[["surface_id":"home","title":"Home","kind":"authored","schema_version":1,"template_root":tree,"action_contract":[nullAction,formAction,closeAction]],["surface_id":"details","title":"Details","kind":"generated","schema_version":1,"generation_prompt":"Private publisher fixture prompt","action_contract":[]]],"data_schemas":[["schema_id":"form-value","schema":["type":"object","required":["values"],"additionalProperties":false,"properties":["values":["type":"object","required":["note"],"additionalProperties":false,"properties":["note":["type":"string","maxLength":100]]]]]]],"future_manifest":["preserve":true]]
    static func normal(_ request:URLRequest) -> (Int,Any) {
        let body = (try? JSONSerialization.jsonObject(with:request.httpBody ?? Data())) as? [String:Any] ?? [:]
        switch request.url!.path {
        case "/api/apps": return (200,["apps":[["app_id":"fixture-app","version":"1.0","description":"Fixture installed app"]]])
        case "/api/apps/fixture-app/manifest": return (200,["app_id":"fixture-app","version":manifest["version"]!,"manifest":manifest,"install_dir":"/private/fixture"])
        case "/api/apps/fixture-app/open":
            let surface = body["surface_id"] as! String,session = body["session_id"] as! String
            return (200,["success":true,"app_id":"fixture-app","surface_id":surface,"screen_id":NativeSurfaceWire.screen(app:"fixture-app",surface:surface,session:session),"root":tree,"pushed":false,"push_error":"private-server-canary"])
        case "/api/apps/fixture-app/dispatch":
            let surface = body["surface_id"] as! String,session = body["session_id"] as! String,action = body["action_id"] as! String
            let surfaces = manifest["surfaces"] as! [[String:Any]],specs = surfaces.first { $0["surface_id"] as? String == surface }!["action_contract"] as! [[String:Any]],spec = specs.first { $0["action_id"] as? String == action }!
            return (200,["success":true,"handler":spec["handler"]!,"target":spec["target"]!,"requires_confirmation":spec["requires_confirmation"]!,"screen_id":NativeSurfaceWire.screen(app:"fixture-app",surface:surface,session:session)])
        default:return (404,["error":"private-server-canary"])
        }
    }
    static func body(_ request:URLRequest) -> Data {
        if let data = request.httpBody { return data };guard let stream = request.httpBodyStream else { return Data() }
        stream.open();defer { stream.close() };var data = Data(),buffer = [UInt8](repeating:0,count:4096)
        while stream.hasBytesAvailable { let n = stream.read(&buffer,maxLength:buffer.count);if n <= 0 { break };data.append(buffer,count:n) };return data
    }
    override class func canInit(with request:URLRequest) -> Bool { true }
    override class func canonicalRequest(for request:URLRequest) -> URLRequest { request }
    override func startLoading() {
        var captured = request;if captured.httpMethod == "POST" { captured.httpBody = Self.body(request) };Self.requests.append(captured)
        let (status,value) = Self.handler(captured),response = HTTPURLResponse(url:request.url!,statusCode:status,httpVersion:nil,headerFields:nil)!,data = try! JSONSerialization.data(withJSONObject:value)
        let deliver = { self.client?.urlProtocol(self,didReceive:response,cacheStoragePolicy:.notAllowed);self.client?.urlProtocol(self,didLoad:data);self.client?.urlProtocolDidFinishLoading(self) }
        if let delay=Self.delayByPath[request.url!.path]{DispatchQueue.global().asyncAfter(deadline:.now()+delay,execute:deliver)}else if Self.delayPath == request.url!.path { Self.delayPath = nil;Self.delayed = true;DispatchQueue.global().asyncAfter(deadline:.now()+Self.delayInterval,execute:deliver) } else { deliver() }
    }
    override func stopLoading() {}
    static var posts:Int { requests.filter { $0.httpMethod == "POST" }.count }
}
@main struct NativeAppSurfaceFeatureTests {
    static var count = 0
    @MainActor static func check(_ value:Bool,_ name:String) { guard value else { fatalError("FAIL: \(name)") };count += 1 }
    @MainActor static func rejected(_ action:() throws -> Void,_ name:String) { do { try action();fatalError("Accepted: \(name)") } catch { count += 1 } }
    @MainActor static func main() async throws {
        let config = URLSessionConfiguration.ephemeral;config.protocolClasses = [SurfaceFixture.self]
        let session = URLSession(configuration:config),model = NativeAppSurfaceModel(session:session,socketFactory:{SurfaceSocketFixture(url:$0)}),base = URL(string:"http://127.0.0.1:9464")!,sid = "thread /colon:test"
        await model.configure(baseURL:nil,sessionID:sid)
        check(!model.available && SurfaceFixture.requests.isEmpty,"nil readiness no requests")
        await model.configure(baseURL:base,sessionID:sid)
        check(SurfaceFixture.requests.count == 1 && model.apps?.count == 1,"catalogue is single passive inventory read")
        await model.inspect("fixture-app")
        check(model.manifest?["future_manifest"] != nil && SurfaceFixture.posts == 0,"passive manifest preserves metadata and does not render/generate")
        rejected({_ = try model.review(.open(surface:"home",regenerate:false,data:["nested":["amount":Double.infinity]]))},"nonfinite open data is refused before serialization or network")
        rejected({_ = try model.review(.open(surface:"home",regenerate:false,data:["date":Date()]))},"non-JSON open data is refused before serialization or network")
        rejected({_ = try NativeSurfaceWire.tree(["type":"Text","value":"safe","extra":Double.nan])},"nonfinite component metadata never reaches JSON writer")
        check(NativeSurfaceWire.json(["bad":Date()])=="Unreadable metadata" && !NativeSurfaceWire.same(["bad":Date()],["bad":Date()]),"unreadable metadata cannot compare equal as a contract")
        let invalidBodyBefore=SurfaceFixture.requests.count
        do{_ = try await NativeSurfaceClient(baseURL:base,session:session).request("/api/apps/fixture-app/open",body:["bad":Double.infinity]);fatalError("Accepted invalid HTTP body")}catch{count+=1}
        check(SurfaceFixture.requests.count==invalidBodyBefore,"invalid HTTP body never enters URLSession")
        let initialRequests = SurfaceFixture.requests.count
        let opening = try model.review(.open(surface:"home",regenerate:false,data:[:]))
        check(SurfaceFixture.requests.count == initialRequests && opening.explanation.contains("provider/fallback") && opening.explanation.contains(sid),"inert exact-session open review discloses model cache and phone effects")
        let opened=await model.perform(opening)
        if !opened{print("AppSurface fixture open diagnostic: error=\(model.error ?? "none"), session=\(model.sessionStatus), socketFrames=\(SurfaceSocketFixture.sockets.last?.sent.map{$0["type"] as? String ?? "unknown"} ?? []), requests=\(SurfaceFixture.requests.map{$0.url!.path})")}
        check(opened && model.root != nil && model.surfaceID == "home","open verifies app surface session receipt and mounts bounded native tree; \(model.error ?? "no error") / \(model.sessionStatus)")
        check(model.sessionStatus=="Connected" && SurfaceSocketFixture.urls.count==1 && SurfaceSocketFixture.urls[0].scheme=="ws" && SurfaceSocketFixture.urls[0].path=="/v1/session" && SurfaceSocketFixture.sockets[0].sid==sid,"dedicated exact-session WebSocket registered before REST action")
        check(SurfaceSocketFixture.sockets[0].sent[0]["method"] as? String=="config.get" && opening.explanation.contains("learning") && opening.explanation.contains("remain shared"),"passive gateway registration and shared-memory/disconnect effects disclosed")
        check(model.receipt?.contains("push failed") == true && model.receipt?.contains("private-server-canary") == false,"push failure accurate without private error echo")
        let openReq = SurfaceFixture.requests.last { $0.httpMethod == "POST" }!,openBody = try JSONSerialization.jsonObject(with:openReq.httpBody!) as! [String:Any]
        check(openBody["session_id"] as? String == sid && openBody["user_fingerprint"] as? String == sid && openBody["regenerate"] as? Bool == false,"canonical session scoped cache fingerprint and explicit regeneration flag")
        check(model.root!.children.last?.supported == false && SurfaceFixture.requests.allSatisfy { $0.url!.host == "127.0.0.1" },"WebView inspection only with zero resource URL fetches")
        check(NativeSurfaceWire.screen(app:"fixture-app",surface:"home",session:sid) == "fixture-app:home:thread%20%2Fcolon%3Atest","canonical screen ID matches backend percent encoding")
        let replayPosts = SurfaceFixture.posts
        check(!(await model.perform(opening)) && SurfaceFixture.posts == replayPosts,"successful open confirmation cannot replay")
        rejected({ _ = try model.review(.dispatch(action:"unknown",value:NSNull())) },"unknown action is refused locally")
        rejected({ _ = try model.review(.open(surface:"undeclared",regenerate:false,data:[:])) },"undeclared surface is refused locally")
        let form = try model.review(.dispatch(action:"submit",value:["values":["note":"Reviewed form value"]]))
        check(form.explanation.contains("Submitted value") && form.explanation.contains("Reviewed form value") && form.explanation.contains("not completed execution"),"form explicit data and acknowledgement semantics")
        check(await model.perform(form) && model.receipt?.contains("does not verify tool completion") == true,"form action follows declared schema and contract")
        let formBody = try JSONSerialization.jsonObject(with:SurfaceFixture.requests.last { $0.httpMethod == "POST" }!.httpBody!) as! [String:Any]
        check(formBody["surface_id"] as? String == "home" && formBody["action_id"] as? String == "submit" && formBody["session_id"] as? String == sid && (formBody["value"] as? [String:Any])?["values"] != nil,"exact action/surface/session and actual values wrapper")
        rejected({ _ = try model.review(.dispatch(action:"submit",value:["values":["note":"ok","extra":"unreviewed"]])) },"additional fields blocked by resolved schema")
        rejected({ _ = try model.review(.dispatch(action:"submit",value:["values":["note":true]])) },"form value type enforced")
        let change = try model.review(.dispatch(action:"go",value:NSNull()))
        SurfaceFixture.manifest["permissions"] = ["network.any"]
        let beforeChange = SurfaceFixture.posts
        check(!(await model.perform(change)) && SurfaceFixture.posts == beforeChange && model.error?.contains("permissions") == true,"fresh permission drift blocks reviewed action before dispatch")
        SurfaceFixture.manifest["permissions"] = ["memory.read"]
        await model.inspect("fixture-app")
        let regenerate = try model.review(.open(surface:"details",regenerate:true,data:["private":"fixture data"]))
        check(regenerate.title.contains("Regenerate") && regenerate.explanation.contains("Private publisher fixture prompt") && regenerate.explanation.contains("fixture data"),"regeneration reviews exact generation prompt and data")
        check(await model.perform(regenerate),"declared generated surface open endpoint supported")
        let regenBody = try JSONSerialization.jsonObject(with:SurfaceFixture.requests.last { $0.httpMethod == "POST" }!.httpBody!) as! [String:Any]
        check(regenBody["regenerate"] as? Bool == true,"explicit regeneration flag only after confirmation")
        await model.inspect("fixture-app")
        let normal = SurfaceFixture.normal
        let mismatched = try model.review(.open(surface:"home",regenerate:false,data:[:]))
        SurfaceFixture.handler = { request in
            let (code,raw) = normal(request);var value = raw as! [String:Any]
            if request.url!.path.hasSuffix("/open") { value["screen_id"] = NativeSurfaceWire.screen(app:"fixture-app",surface:"home",session:"another-thread") };return (code,value)
        }
        check(!(await model.perform(mismatched)) && model.root == nil && model.error?.contains("may already") == true,"cross-session surface receipt never mounts and uncertain effects explicit")
        SurfaceFixture.handler = normal
        await model.inspect("fixture-app");check(await model.perform(try model.review(.open(surface:"home",regenerate:false,data:[:]))),"fixture reopens valid surface")
        let invalidReceipt = try model.review(.dispatch(action:"go",value:NSNull()))
        SurfaceFixture.handler = { request in let (code,raw) = normal(request);var value = raw as! [String:Any];if request.url!.path.hasSuffix("/dispatch") { value["target"] = "https://attacker.example" };return (code,value) }
        check(!(await model.perform(invalidReceipt)) && model.error?.contains("does not match") == true,"wrong dispatch target cannot become navigation or success")
        SurfaceFixture.handler = normal
        let close = try model.review(.dispatch(action:"close",value:NSNull()))
        check(await model.perform(close) && model.root == nil && model.surfaceID.isEmpty,"declared close acknowledgement clears local surface without uninstall")
        check(SurfaceFixture.requests.allSatisfy { !$0.url!.path.contains("install") && $0.httpMethod != "DELETE" },"surface lifecycle leaves verified install/uninstall owner intact")
        var deep:[String:Any] = ["type":"Text","value":"leaf"]
        for _ in 0...NativeSurfaceWire.maximumDepth { deep = ["type":"VStack","children":[deep]] }
        rejected({ _ = try NativeSurfaceWire.tree(deep) },"depth budget rejects entire overdeep tree")
        rejected({ _ = try NativeSurfaceWire.tree(["type":"VStack","children":Array(repeating:["type":"Text","value":"node"],count:NativeSurfaceWire.maximumNodes)]) },"node budget enforced globally")
        rejected({ _ = try NativeSurfaceWire.tree(["type":"Text","value":String(repeating:"中",count:22000)]) },"UTF-8 text budget enforced")
        rejected({ _ = try NativeSurfaceWire.tree(["type":"Text","value":String(repeating:"a",count:NativeSurfaceWire.maximumBytes)]) },"JSON byte budget enforced")
        rejected({ _ = try NativeSurfaceWire.tree(["type":"VStack","children":["not-a-node"]]) },"malformed children rejected")
        let unsupported = try NativeSurfaceWire.tree(["type":"WebView","children":[["type":"Button","action_id":"go"]],"url":"javascript:alert(1)"])
        check(!unsupported.supported && unsupported.actionIDs.isEmpty,"unsupported container cannot smuggle interactive descendants")
        let password = try NativeSurfaceWire.tree(["type":"TextField","input_type":"password","action_id":"submit"])
        check(!password.supported && password.actionIDs.isEmpty,"unsupported secret-input components stay inspection only")
        rejected({ _ = try NativeSurfaceWire.tree(["type":"Form","fields":[["name":"same"],["name":"same"]]]) },"duplicate form names refused")
        var reserved = SurfaceFixture.manifest
        reserved["surfaces"] = [["surface_id":"home","action_contract":[["action_id":"confirm_fake","handler":"app_event","target":"","requires_confirmation":false]]]]
        rejected({ _ = try NativeSurfaceWire.contract(reserved,surface:"home",action:"confirm_fake",value:NSNull()) },"reserved confirmation prefix never reaches backend special dispatcher")
        rejected({ try NativeSurfaceWire.validate("x",schema:["type":"string","pattern":".*"]) },"unsupported schema constraints fail closed")
        rejected({ try NativeSurfaceWire.validate("x",schema:["type":"string","maxLength":"bad"]) },"malformed constraints fail closed")
        rejected({ try NativeSurfaceWire.validate(true,schema:["type":"integer"]) },"boolean not treated as numeric schema value")
        await model.inspect("fixture-app")
        let stale = try model.review(.open(surface:"home",regenerate:false,data:[:]))
        SurfaceFixture.delayPath = "/api/apps/fixture-app/manifest";SurfaceFixture.delayed = false
        let task = Task { await model.perform(stale) }
        for _ in 0..<1000 { if SurfaceFixture.delayed { break };try await Task.sleep(nanoseconds:1_000_000) }
        let beforeSessionChange = SurfaceFixture.posts
        await model.configure(baseURL:base,sessionID:"new-thread")
        check(!(await task.value) && SurfaceFixture.posts == beforeSessionChange && model.root == nil,"session change during preflight prevents stale POST")
        await model.inspect("fixture-app");let privateFailure = try model.review(.open(surface:"home",regenerate:false,data:[:]))
        SurfaceFixture.handler = { request in request.httpMethod == "POST" ? (400,["detail":"private-server-canary"]) : normal(request) }
        check(!(await model.perform(privateFailure)) && model.error?.contains("private-server-canary") == false,"private backend errors never echoed")
        let lastPosts = SurfaceFixture.posts
        check(!(await model.perform(privateFailure)) && SurfaceFixture.posts == lastPosts,"uncertain failed open review cannot replay")
        SurfaceFixture.handler = normal
        await socketChecks(model:model,base:base)
        try await confirmedCloseChecks(model:model,base:base)
        try await surfaceUpdateChecks(model:model,base:base)
        try await pendingDispatchUpdateChecks(model:model,base:base)
        try await navigationReadbackOrderingChecks(model:model,base:base)
        await model.configure(baseURL:base,sessionID:nil);await model.inspect("fixture-app")
        rejected({ _ = try model.review(.open(surface:"home",regenerate:false,data:[:])) },"no implicit default/rest-dispatch session")
        let requests = SurfaceFixture.requests.count
        await model.configure(baseURL:URL(string:"http://example.com")!,sessionID:sid)
        check(SurfaceFixture.requests.count == requests && model.apps == nil,"non-loopback origin rejected before network")
        print("Native app surfaces: \(count) assertions passed.")
    }
    @MainActor static func navigationReadbackOrderingChecks(model:NativeAppSurfaceModel,base:URL) async throws {
        defer{SurfaceFixture.handler=SurfaceFixture.normal;SurfaceFixture.delayByPath=[:];model.closeSession()}
        let sid="navigation-readback-order"
        await model.configure(baseURL:base,sessionID:sid);await model.inspect("fixture-app")
        check(await model.perform(try model.review(.open(surface:"home",regenerate:false,data:[:]))),"navigation ACK/readback fixture mounts home")
        let socket=SurfaceSocketFixture.sockets.last!,mounted=model.renderIdentity
        let target:[String:Any]=["type":"sdui","session_id":sid,"payload":["screen_id":NativeSurfaceWire.screen(app:"fixture-app",surface:"details",session:sid),"confirmation":NSNull(),"root":["type":"VStack","children":[["type":"Text","content":"Exact declared navigation result"]]]]]
        SurfaceFixture.handler={request in
            if request.url?.path=="/api/apps/fixture-app/dispatch"{
                SurfaceFixture.delayByPath["/api/apps/fixture-app/dispatch"]=0.05
                SurfaceFixture.delayByPath["/api/apps/fixture-app/manifest"]=0.3
                socket.enqueue(target)
            }
            return SurfaceFixture.normal(request)
        }
        let navigating=Task{await model.perform(try! model.review(.dispatch(action:"go",value:NSNull())))}
        check(await navigating.value,"dispatch receipt can arrive before target manifest readback")
        check(model.surfaceID=="home" && model.renderIdentity==mounted && !model.acting,"HTTP ACK alone does not mount unvalidated target")
        for _ in 0..<1000{if model.surfaceID=="details"{break};try await Task.sleep(nanoseconds:1_000_000)}
        check(model.surfaceID=="details" && model.root?.children.first?.raw["content"] as? String=="Exact declared navigation result" && model.renderIdentity != mounted,"target update survives dispatch-generation rotation while fresh readback still pending")
        SurfaceFixture.delayByPath=[:];let current=model.renderIdentity
        socket.enqueue(["type":"sdui","session_id":sid,"payload":["screen_id":NativeSurfaceWire.screen(app:"fixture-app",surface:"home",session:sid),"confirmation":NSNull(),"root":SurfaceFixture.tree]])
        try await Task.sleep(nanoseconds:20_000_000)
        check(model.surfaceID=="details" && model.renderIdentity==current,"consumed navigation authorization cannot mount different unsolicited target")
    }
    @MainActor static func pendingDispatchUpdateChecks(model:NativeAppSurfaceModel,base:URL) async throws {
        defer{SurfaceFixture.handler=SurfaceFixture.normal;SurfaceFixture.delayPath=nil;SurfaceFixture.delayInterval=0.1;model.closeSession()}
        SurfaceFixture.delayInterval=1
        let sid="pending-dispatch-update"
        await model.configure(baseURL:base,sessionID:sid);await model.inspect("fixture-app")
        check(await model.perform(try model.review(.open(surface:"home",regenerate:false,data:[:]))),"pending-dispatch fixture mounts reviewed surface")
        let socket=SurfaceSocketFixture.sockets.last!
        func waitForPost()async throws{for _ in 0..<1000{if SurfaceFixture.delayed{return};try await Task.sleep(nanoseconds:1_000_000)};fatalError("Dispatch POST not started")}
        func patch(_ value:String)->[String:Any]{["type":"sdui_patch","session_id":sid,"payload":["screen_id":NativeSurfaceWire.screen(app:"fixture-app",surface:"home",session:sid),"patches":[["op":"replace","path":"children/0/value","value":value]]]]}
        func waitForUpdate(_ revision:UUID)async throws{for _ in 0..<1000{if model.renderIdentity != revision{return};try await Task.sleep(nanoseconds:1_000_000)};fatalError("Pending dispatch patch not mounted")}
        let review=try model.review(.dispatch(action:"submit",value:["values":["note":"Explicit delayed side effect"]])),before=model.renderIdentity
        SurfaceFixture.delayPath="/api/apps/fixture-app/dispatch";SurfaceFixture.delayed=false
        let dispatch=Task{await model.perform(review)};try await waitForPost();socket.enqueue(patch("Updated before dispatch receipt"));try await waitForUpdate(before)
        check(model.acting && model.root?.children.first?.raw["value"] as? String=="Updated before dispatch receipt","live update revokes view revision without unlocking pending HTTP operation")
        rejected({_ = try model.review(.open(surface:"home",regenerate:false,data:[:]))},"new app operation cannot overlap pending side effect after push")
        check(await dispatch.value,"matching late dispatch acknowledgement remains accepted despite replaced view")
        check(!model.acting && model.receipt?.contains("earlier reviewed action") == true && model.receipt?.contains("outcome remains unverified") == true,"busy released only after matching HTTP completion and outcome is truthful")
        let failure=try model.review(.dispatch(action:"submit",value:["values":["note":"Explicit failure fixture"]])),failureRevision=model.renderIdentity
        SurfaceFixture.handler={request in request.url?.path=="/api/apps/fixture-app/dispatch" ? (400,["error":"private-service-canary"]) : SurfaceFixture.normal(request)}
        SurfaceFixture.delayPath="/api/apps/fixture-app/dispatch";SurfaceFixture.delayed=false
        let failing=Task{await model.perform(failure)};try await waitForPost();socket.enqueue(patch("Updated before uncertain failure"));try await waitForUpdate(failureRevision)
        check(model.acting,"push does not unlock operation awaiting an error receipt")
        check(!(await failing.value) && !model.acting && model.error?.contains("may already have taken effect") == true && model.error?.contains("private-service-canary") == false,"stale-view HTTP error releases busy and preserves possible effects without leaking details")
        SurfaceFixture.handler=SurfaceFixture.normal
        let closing=try model.review(.dispatch(action:"submit",value:["values":["note":"Explicit disconnect fixture"]]))
        SurfaceFixture.delayPath="/api/apps/fixture-app/dispatch";SurfaceFixture.delayed=false
        let disconnected=Task{await model.perform(closing)};try await waitForPost();model.closeSession()
        check(model.acting && model.sessionStatus=="Disconnected","disconnect revokes approval transport but does not pretend pending REST dispatch was cancelled")
        check(await disconnected.value,"matching REST receipt after disconnect still acknowledges original reviewed dispatch")
        check(!model.acting && model.receipt?.contains("outcome remains unverified") == true,"owning HTTP completion releases operation lock after disconnect")
    }
    @MainActor static func surfaceUpdateChecks(model:NativeAppSurfaceModel,base:URL) async throws {
        let previousManifest=SurfaceFixture.manifest
        defer{SurfaceFixture.manifest=previousManifest;model.closeSession()}
        let sid="live-surface-updates"
        await model.configure(baseURL:base,sessionID:sid);await model.inspect("fixture-app")
        check(await model.perform(try model.review(.open(surface:"home",regenerate:false,data:[:]))),"live-update fixture opens reviewed REST tree")
        let socket=SurfaceSocketFixture.sockets.last!,held=try model.review(.dispatch(action:"go",value:NSNull())),initial=model.renderIdentity
        func patch(_ path:String,value:Any,owner:String?=nil,surface:String="home")->[String:Any]{let exactOwner=owner ?? sid;return ["type":"sdui_patch","session_id":exactOwner,"payload":["screen_id":NativeSurfaceWire.screen(app:"fixture-app",surface:surface,session:exactOwner),"patches":[["op":"replace","path":path,"value":value]]]]}
        func replacement()->[String:Any]{["type":"sdui","session_id":sid,"payload":["screen_id":NativeSurfaceWire.screen(app:"fixture-app",surface:"details",session:sid),"confirmation":NSNull(),"root":["type":"VStack","children":[["type":"Text","value":"Actual declared navigation"],["type":"WebView","url":"javascript:alert(1)"]]]]]}
        func waitForRevision(_ old:UUID)async throws{for _ in 0..<1000{if model.renderIdentity != old{return};try await Task.sleep(nanoseconds:1_000_000)};fatalError("Live surface update timed out")}
        socket.enqueue(patch("/children/0/value",value:"Actual owning patch"));try await waitForRevision(initial)
        check(model.root?.children.first?.raw["value"] as? String=="Actual owning patch" && model.sessionStatus=="Connected","bounded owning patch updates native controls without disconnect")
        rejected({_ = try model.review(.dispatch(action:"go",value:NSNull()),mountedRevision:initial)},"queued old control callback cannot create new review from replaced revision")
        let posts=SurfaceFixture.posts
        check(!(await model.perform(held)) && SurfaceFixture.posts==posts,"live patch invalidates held action review before dispatch")
        let stable=model.renderIdentity,beforeForeign=SurfaceFixture.requests.count
        socket.enqueue(patch("children/0/value",value:"Foreign",owner:"foreign"));try await Task.sleep(nanoseconds:20_000_000)
        check(model.renderIdentity==stable && SurfaceFixture.requests.count==beforeForeign,"foreign update cannot mount or initiate preflight")
        socket.enqueue(patch("children.0.value",value:"Bad dotted path"))
        for _ in 0..<1000{if model.error?.contains("could not be validated")==true{break};try await Task.sleep(nanoseconds:1_000_000)}
        check(model.renderIdentity==stable && model.root?.children.first?.raw["value"] as? String=="Actual owning patch","malformed patch retains tree revision and exact previous content")
        socket.enqueue(replacement());try await Task.sleep(nanoseconds:20_000_000)
        check(model.surfaceID=="home" && model.renderIdentity==stable,"unsolicited declared target cannot navigate without reviewed dispatch")
        check(await model.perform(try model.review(.dispatch(action:"go",value:NSNull()))),"immutable declared navigate contract dispatch acknowledged")
        socket.enqueue(replacement());try await waitForRevision(stable)
        check(model.surfaceID=="details" && model.root?.children.first?.raw["value"] as? String=="Actual declared navigation" && model.root?.children.last?.supported==false,"ordinary confirmation-null SDUI navigates declared surface with unknown component inactive")
        let navigated=model.renderIdentity
        var drift=SurfaceFixture.manifest;drift["permissions"]=["network.new-scope"];SurfaceFixture.manifest=drift
        socket.enqueue(patch("children/0/value",value:"Drift",surface:"details"));try await waitForRevision(navigated)
        check(model.root==nil && model.surfaceID.isEmpty && model.error?.contains("contract changed")==true && model.sessionStatus=="Connected","fresh manifest drift revokes old mount instead of guessing updated contract")
        socket.enqueue(replacement());try await Task.sleep(nanoseconds:20_000_000)
        check(model.root==nil,"unmounted update cannot remount after close or contract revocation")
    }
    @MainActor static func confirmedCloseChecks(model:NativeAppSurfaceModel,base:URL) async throws {
        let previousManifest=SurfaceFixture.manifest
        defer{SurfaceFixture.manifest=previousManifest;model.closeSession()}
        var surfaces=previousManifest["surfaces"] as! [[String:Any]],actions=surfaces[0]["action_contract"] as! [[String:Any]]
        let index=actions.firstIndex{$0["action_id"] as? String=="close"}!;actions[index]["requires_confirmation"]=true;surfaces[0]["action_contract"]=actions;SurfaceFixture.manifest["surfaces"]=surfaces
        let sid="confirmed-close-session"
        await model.configure(baseURL:base,sessionID:sid);await model.inspect("fixture-app")
        check(await model.perform(try model.review(.open(surface:"home",regenerate:false,data:[:]))),"confirmed-close fixture mounts real declared surface")
        let socket=SurfaceSocketFixture.sockets.last!
        func request(_ id:String,surface:String="home")->[String:Any]{
            let screen=NativeSurfaceWire.screen(app:"fixture-app",surface:surface,session:sid),now=Date().timeIntervalSince1970
            let metadata:[String:Any]=["contract_version":1,"request_id":id,"session_id":sid,"app_id":"fixture-app","surface_id":surface,"action_id":"close","screen_id":screen,"scope":"app_action","created_at":now,"expires_at":now+300,"handler":"close","target":"","event":"tap","value":NSNull(),"requires_confirmation":true]
            return ["type":"sdui","session_id":sid,"payload":["screen_id":screen,"confirmation":metadata,"root":["type":"VStack","children":[["type":"Text","value":"Confirm closing exact fixture surface"],["type":"Button","action_id":"confirm_"+id,"label":"Confirm"],["type":"Button","action_id":"reject_"+id,"label":"Reject"]]]]]
        }
        func decision(_ id:String,status:String="accepted",surface:String="home",action:String="close")->[String:Any]{["type":"confirmation_decision","session_id":sid,"payload":["request_id":id,"status":status,"app_id":"fixture-app","surface_id":surface,"action_id":action,"screen_id":NativeSurfaceWire.screen(app:"fixture-app",surface:surface,session:sid),"scope":"app_action","dispatch_accepted":status=="accepted","tool_outcome_verified":false]]}
        func waitFor(_ id:String,state:String)async throws{for _ in 0..<1000{if model.appConfirmations.requests.contains(where:{$0.id==id && $0.state==state}){return};try await Task.sleep(nanoseconds:1_000_000)};fatalError("Timed out confirmation \(id) \(state)")}
        let rejectedID=UUID().uuidString;socket.enqueue(request(rejectedID));try await waitFor(rejectedID,state:"pending")
        await model.appConfirmations.respond(try model.appConfirmations.review(rejectedID,confirm:false)){try await model.respondToAppConfirmation($0)}
        socket.enqueue(decision(rejectedID,status:"rejected"));try await waitFor(rejectedID,state:"rejected_confirmed")
        check(model.root != nil && model.surfaceID=="home","reject receipt preserves exact mounted surface")
        let wrongSurfaceID=UUID().uuidString;socket.enqueue(request(wrongSurfaceID,surface:"details"));try await waitFor(wrongSurfaceID,state:"pending")
        await model.appConfirmations.respond(try model.appConfirmations.review(wrongSurfaceID,confirm:true)){try await model.respondToAppConfirmation($0)}
        socket.enqueue(decision(wrongSurfaceID,surface:"details"));try await waitFor(wrongSurfaceID,state:"accepted_dispatch")
        check(model.root != nil && model.surfaceID=="home","accepted close for different declared surface cannot dismiss mounted home")
        let mismatchID=UUID().uuidString;socket.enqueue(request(mismatchID));try await waitFor(mismatchID,state:"pending")
        await model.appConfirmations.respond(try model.appConfirmations.review(mismatchID,confirm:true)){try await model.respondToAppConfirmation($0)}
        socket.enqueue(decision(mismatchID,action:"wrong-action"));try await waitFor(mismatchID,state:"receipt_mismatch")
        check(model.root != nil,"mismatched close receipt never dismisses mounted root")
        let held=try model.review(.dispatch(action:"go",value:NSNull())),acceptedID=UUID().uuidString
        socket.enqueue(request(acceptedID));try await waitFor(acceptedID,state:"pending")
        await model.appConfirmations.respond(try model.appConfirmations.review(acceptedID,confirm:true)){try await model.respondToAppConfirmation($0)}
        socket.enqueue(decision(acceptedID));try await waitFor(acceptedID,state:"accepted_dispatch")
        check(model.root==nil && model.surfaceID.isEmpty && model.sessionStatus=="Connected" && model.receipt?.contains("tool completion remains unverified")==true,"matching accepted confirmed close dismisses exact local mount while preserving owning socket and truthful acknowledgement")
        let posts=SurfaceFixture.posts
        check(!(await model.perform(held)) && SurfaceFixture.posts==posts,"confirmed close invalidates held action review")
        check(await model.perform(try model.review(.open(surface:"home",regenerate:false,data:[:]))),"closed surface can be opened as a new mount")
        socket.enqueue(decision(acceptedID));try await Task.sleep(nanoseconds:20_000_000)
        check(model.root != nil && model.surfaceID=="home","duplicate accepted receipt cannot dismiss new mount")
    }
    @MainActor static func socketChecks(model:NativeAppSurfaceModel,base:URL) async {
        do{
            await model.configure(baseURL:base,sessionID:"app-scope");await model.inspect("fixture-app")
            check(await model.perform(try model.review(.open(surface:"home",regenerate:false,data:[:]))),"app session connected for scoped outcome checks")
            let socket=SurfaceSocketFixture.sockets.last!
            socket.enqueue(["type":"text_response","session_id":"foreign","payload":["text":"Foreign reply"]])
            socket.enqueue(["type":"text_response","session_id":"app-scope","payload":["text":"Real app-session reply"]])
            socket.enqueue(["type":"tool_start","session_id":"app-scope","payload":["tool":"fixture_tool","call_id":"call-app"]])
            socket.enqueue(["type":"tool_result","session_id":"app-scope","payload":["tool":"fixture_tool","call_id":"call-app","success":true]])
            socket.enqueue(["type":"stream_delta","session_id":"app-scope","payload":["kind":"reasoning","delta":"App provider reasoning"]])
            for _ in 0..<1000{if model.richChat.reasoning=="App provider reasoning"{break};try await Task.sleep(nanoseconds:1_000_000)}
            check(model.transcript.contains{$0.text=="Real app-session reply"} && !model.transcript.contains{$0.text=="Foreign reply"},"only owning app session populates separate transcript")
            check(model.richChat.tools.count==1 && model.richChat.tools[0].status=="reported_success" && model.richChat.reasoning=="App provider reasoning","scoped tool and reasoning frames reach shared native presentation")
            socket.enqueue(["type":"permission_request","session_id":"app-scope","payload":["request_id":"req-app","path":"/private/tmp","operation":"write","expires_at":Date().timeIntervalSince1970+300,"scope":"persistent_workspace"]])
            for _ in 0..<1000{if model.richChat.permissions.count==1{break};try await Task.sleep(nanoseconds:1_000_000)}
            let permission=try model.richChat.reviewPermission("req-app",allow:true)
            check(permission.permission.scope.contains("Persistent") && permission.permission.canonicalPath=="/private/tmp","app permission review exposes brain-wide canonical grant")
            await model.richChat.respond(permission){response in try await model.respondToPermission(response)}
            for _ in 0..<1000{if model.richChat.permissions[0].state=="granted_confirmed"{break};try await Task.sleep(nanoseconds:1_000_000)}
            check(model.richChat.permissions[0].state=="granted_confirmed" && (socket.sent.last?["payload"] as? [String:Any])?["action_id"] as? String=="perm_grant_req-app","exact live socket permission response and canonical receipt")
            let sent=socket.sent.count
            await model.richChat.respond(permission){response in try await model.respondToPermission(response)}
            check(socket.sent.count==sent,"dedicated permission response cannot replay")
            socket.enqueue(["type":"error","session_id":"app-scope","payload":["message":"private-server-canary"]])
            socket.enqueue(["type":"sdui","session_id":"app-scope","payload":["root":["type":"Button","action_id":"confirm_fake"]]])
            for _ in 0..<1000{if model.richChat.events.contains(where:{$0.kind=="sdui"}){break};try await Task.sleep(nanoseconds:1_000_000)}
            check(!model.transcript.contains{$0.text.contains("private-server-canary")} && !model.richChat.events.contains{NativeSurfaceWire.json($0.raw).contains("private-server-canary")},"private app-session service errors withheld from transcript and structured events")
            check(model.richChat.events.contains{$0.kind=="sdui"} && !(model.root?.actionIDs.contains("confirm_fake") ?? true),"opaque backend app confirmation remains inspection-only")
            socket.enqueue(["type":"permission_request","session_id":"app-scope","payload":["request_id":"stale-app","path":"/private/tmp","operation":"read","expires_at":Date().timeIntervalSince1970+300]])
            for _ in 0..<1000{if model.richChat.permissions.count==2{break};try await Task.sleep(nanoseconds:1_000_000)}
            let stale=try model.richChat.reviewPermission("stale-app",allow:true)
            model.closeSession()
            await model.richChat.respond(stale){response in try await model.respondToPermission(response)}
            check(socket.cancelled && model.sessionStatus=="Disconnected" && socket.sent.count==sent && model.richChat.permissions.isEmpty,"disconnect cancels socket and removes stale permission authority")
            await model.configure(baseURL:base,sessionID:"next-app-scope");await model.inspect("fixture-app")
            SurfaceSocketFixture.failNextHandshake=true;let before=SurfaceFixture.posts
            check(!(await model.perform(try model.review(.open(surface:"home",regenerate:false,data:[:])))) && SurfaceFixture.posts==before,"transport alone is insufficient; failed gateway registration blocks REST action")
            check(model.error?.contains("private-server-canary")==false,"handshake service error details withheld")
        }catch{fatalError("Socket fixture failure: \(error)")}
    }
}
