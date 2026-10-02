import Foundation

final class ConversationFixtureProtocol: URLProtocol {
    static var requests: [URLRequest] = []
    static var delayPath: String?
    static var delayed = false
    static var title = "First conversation"
    static var pinned = false
    static var removed = false
    static func summary(_ id: String, _ title: String) -> [String: Any] {
        ["id":id,"title":title,"preview":"Preview", "message_count":3, "pinned":id == "thread-a" ? pinned : false,"updated_at":1000, "title_custom":false,"unknown_metadata":["keep":true]]
    }
    static func body(_ request: URLRequest) -> [String: Any] {
        var data = request.httpBody
        if data == nil, let stream = request.httpBodyStream {
            stream.open(); defer { stream.close() }
            var bytes = Data(), buffer = [UInt8](repeating:0,count:4096)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer,maxLength:buffer.count)
                if count <= 0 { break }
                bytes.append(buffer,count:count)
            }
            data = bytes
        }
        return (data.flatMap { try? JSONSerialization.jsonObject(with:$0) }) as? [String:Any] ?? [:]
    }
    static func normal(_ request: URLRequest) -> (Int, Any) {
        let path = request.url!.path
        if path == "/api/conversations" {
            let query = URLComponents(url:request.url!,resolvingAgainstBaseURL:false)!.queryItems!
            let offset = Int(query.first { $0.name == "offset" }!.value!)!
            let q = query.first { $0.name == "q" }!.value!
            let rows: [[String:Any]] = offset == 0 ? (removed ? [summary("thread-b","Second conversation")] : [summary("thread-a",title),summary("thread-b","Second conversation")]) : [summary("thread-b","Second conversation"),summary("thread-c","Third conversation")]
            return (200,["conversations":rows,"total":3,"limit":25,"offset":offset,"has_more":offset == 0 && !removed,"query":q])
        }
        if request.httpMethod == "DELETE" { removed = true; return (200,["ok":true]) }
        if path.hasSuffix("/rename") {
            title = body(request)["title"] as! String
            return (200,["ok":true,"id":"thread-a","title":title,"title_custom":true])
        }
        if path.hasSuffix("/pin") {
            pinned = body(request)["pinned"] as! Bool
            return (200,["ok":true,"id":"thread-a","pinned":pinned])
        }
        let id = request.url!.lastPathComponent
        return (200,["id":id,"title":id == "thread-a" ? title : "Second conversation", "unknown_top_level":["keep":true],"messages":[
            ["id":"m1","role":"user","content":"Hi", "attachments":[["handle":"attachment-1","name":"photo.png","future_field":["keep":true]]]],
            ["id":"m2","role":"tool","tool_call_id":"call-1", "tool_result":["future_structured_data":[1,2,3]]],
            ["id":"m3","role":"assistant","content":[["type":"text","text":"Hello"],["type":"image","source":"original-image-handle"]],"reasoning":"preserve"]]])
    }
    static var handler: (URLRequest) -> (Int, Any) = normal
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        var captured = request
        if request.httpMethod == "POST" { captured.httpBody = try! JSONSerialization.data(withJSONObject:Self.body(request)) }
        let (status, value) = Self.handler(captured)
        Self.requests.append(captured)
        let response = HTTPURLResponse(url:request.url!,statusCode:status,httpVersion:nil,headerFields:nil)!
        let data = try! JSONSerialization.data(withJSONObject:value)
        let deliver = {
            self.client?.urlProtocol(self,didReceive:response,cacheStoragePolicy:.notAllowed)
            self.client?.urlProtocol(self,didLoad:data)
            self.client?.urlProtocolDidFinishLoading(self)
        }
        if Self.delayPath == request.url!.path {
            Self.delayPath = nil; Self.delayed = true
            DispatchQueue.global().asyncAfter(deadline:.now()+0.15,execute:deliver)
        } else { deliver() }
    }
    override func stopLoading() {}
}

@main struct NativeConversationFeatureTests {
    static var checks = 0
    @MainActor static func check(_ value: Bool, _ name: String) {
        if !value { fatalError("FAIL: \(name)") }
        checks += 1
    }
    @MainActor static func waitForDelay() async throws {
        for _ in 0..<1000 {
            if ConversationFixtureProtocol.delayed { return }
            try await Task.sleep(nanoseconds:1_000_000)
        }
        fatalError("Fixture did not reach delay")
    }
    @MainActor static func main() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ConversationFixtureProtocol.self]
        let session = URLSession(configuration:configuration)
        let base = URL(string:"http://127.0.0.1:9464")!
        let model = NativeConversationFeatureModel(session:session)
        await model.configure(nil)
        check(!model.available && model.threads == nil && ConversationFixtureProtocol.requests.isEmpty,"nil readiness performs no calls")
        await model.configure(base)
        check(model.threads?.count == 2 && model.total == 3 && model.hasMore && model.listError == nil,"real pagination envelope loaded")
        check((model.threads![0].raw["unknown_metadata"] as? [String:Bool])?["keep"] == true,"list retains unknown metadata")
        await model.more()
        check(model.threads?.map(\.id) == ["thread-a","thread-b","thread-c"] && !model.hasMore,"paging deduplicates moved rows")
        let page2 = ConversationFixtureProtocol.requests.last!
        check(URLComponents(url:page2.url!,resolvingAgainstBaseURL:false)!.queryItems!.first { $0.name == "offset" }?.value == "2","paging advances actual server offset")
        model.selection = "thread-a"; await model.inspect()
        check(model.document?.messages.count == 3 && model.document?.messages[1]["role"] as? String == "tool","inspector retains textless tool row")
        let raw = NativeConversationWire.json(model.document!.raw)
        check(raw.contains("attachment-1") && raw.contains("future_field") && raw.contains("original-image-handle") && raw.contains("future_structured_data") && raw.contains("reasoning") && raw.contains("unknown_top_level"),"raw document preserves all attachment/tool/block/top-level metadata")
        check(NativeConversationWire.messageText(model.document!.messages[2]) == "Hello", "blocks preview text without mutating blocks")
        check(ConversationFixtureProtocol.requests.allSatisfy { $0.httpMethod == "GET" },"inspection never saves/re-writes messages")
        model.search = "  café / 20% &?  "; await model.refresh()
        let queryRequest = ConversationFixtureProtocol.requests.last!
        check(URLComponents(url:queryRequest.url!,resolvingAgainstBaseURL:false)!.queryItems!.first { $0.name == "q" }?.value == "café / 20% &?", "search trims and safely encodes literals")
        model.search = "not submitted"; await model.more()
        check(URLComponents(url:ConversationFixtureProtocol.requests.last!.url!,resolvingAgainstBaseURL:false)!.queryItems!.first { $0.name == "q" }?.value == "café / 20% &?", "paging uses completed query not edited search text")
        model.search = ""; await model.refresh()
        let original = model.threads![0]
        check(await model.rename(original,title:"  My chosen title  "),"confirmed exact rename")
        let renameRequest = ConversationFixtureProtocol.requests.last { $0.url!.path.hasSuffix("/rename") }!
        let renameBody = try JSONSerialization.jsonObject(with:renameRequest.httpBody!) as! [String:Any]
        check(renameBody.count == 1 && renameBody["title"] as? String == "My chosen title" && model.threads![0].title == "My chosen title","rename writes title only and refreshes genuine list")
        check(await model.pin(model.threads![0]) && model.threads![0].pinned,"pin exact receipt and list")
        let beforeInvalid = ConversationFixtureProtocol.requests.count
        check(!(await model.rename(original,title:"stale title")) && ConversationFixtureProtocol.requests.count == beforeInvalid,"stale reviewed item cannot mutate")
        var active = true, guardCalls = 0, callbackIDs: [String] = []
        let blocked = await model.deleteConfirmed(model.threads![0],canDelete:{ _ in guardCalls += 1; return !active },onDeleted:{callbackIDs.append($0)})
        check(!blocked && guardCalls == 1 && callbackIDs.isEmpty && ConversationFixtureProtocol.requests.count == beforeInvalid,"live active-thread deletion guard rejects before dispatch")
        active = false
        let deleted = await model.deleteConfirmed(model.threads![0],canDelete:{_ in !active},onDeleted:{ callbackIDs.append($0); check(model.acting,"delete parent callback runs before post-delete refresh") })
        check(deleted && callbackIDs == ["thread-a"] && model.threads?.contains { $0.id == "thread-a" } == false,"confirmed deletion callback once exact ID")
        ConversationFixtureProtocol.removed = false; await model.refresh()
        let normal = ConversationFixtureProtocol.normal
        ConversationFixtureProtocol.handler = { request in request.url!.path.hasSuffix("/rename") ? (200,["ok":true,"id":"wrong-thread","title":"Wrong receipt","title_custom":true]) : normal(request) }
        check(!(await model.rename(model.threads![0],title:"Wrong receipt")) && model.actionError?.contains("exact conversation") == true,"wrong-ID receipt never claims rename")
        ConversationFixtureProtocol.handler = { request in request.httpMethod == "DELETE" ? (200,["ok":false]) : normal(request) }
        let callbacksBefore = callbackIDs.count
        check(!(await model.deleteConfirmed(model.threads![0],onDeleted:{callbackIDs.append($0)})) && callbackIDs.count == callbacksBefore,"unconfirmed delete never calls parent")
        ConversationFixtureProtocol.handler = { request in request.url!.path.hasSuffix("/rename") ? (200,["error":"Not found"]) : normal(request) }
        check(!(await model.rename(model.threads![0],title:"gone")) && model.actionError == "Not found","HTTP200 logical error not success")
        ConversationFixtureProtocol.handler = { request in request.url!.path == "/api/conversations" ? (503,["detail":"store unavailable"]) : normal(request) }
        await model.refresh()
        check(model.threads == nil && model.total == nil && model.listError?.contains("503") == true && !model.loading,"failed list not successful empty")
        ConversationFixtureProtocol.handler = { _ in (200,["conversations":[],"total":true,"limit":25,"offset":0,"has_more":false]) }
        await model.refresh()
        check(model.listError != nil && model.threads == nil,"boolean total rejected")
        ConversationFixtureProtocol.handler = { _ in (200,["conversations":[["id":"broken","title":"x","message_count":1.2,"pinned":false]],"total":1,"limit":25,"offset":0,"has_more":false]) }
        await model.refresh()
        check(model.listError != nil,"fractional message count rejected")
        ConversationFixtureProtocol.handler = { _ in (200,["conversations":[],"total":0,"limit":25,"offset":0,"has_more":true]) }
        await model.refresh()
        check(model.listError != nil,"empty page cannot promise infinite more")
        ConversationFixtureProtocol.handler = { _ in (200,["conversations":[],"total":0,"limit":25,"offset":0,"has_more":false]) }
        await model.refresh()
        check(model.threads?.isEmpty == true && model.total == 0 && model.listError == nil,"genuine successful empty distinguished")
        ConversationFixtureProtocol.handler = normal; await model.refresh()
        ConversationFixtureProtocol.delayPath = "/api/conversations/thread-a"; ConversationFixtureProtocol.delayed = false
        model.selection = "thread-a"
        let firstInspection = Task { await model.inspect() }
        try await waitForDelay()
        model.selection = "thread-b"; await model.inspect(); await firstInspection.value
        check(model.document?.id == "thread-b" && model.detailError == nil && !model.inspecting,"slow previous selection cannot replace current document")
        ConversationFixtureProtocol.handler = { request in request.url!.path == "/api/conversations/thread-a" ? (200,["id":"thread-b","title":"Wrong thread","messages":[]]) : normal(request) }
        model.selection = "thread-a"; await model.inspect()
        check(model.document == nil && model.detailError != nil,"mismatched thread not eligible to open")
        ConversationFixtureProtocol.handler = { request in request.url!.path == "/api/conversations/thread-a" ? (200,["id":"thread-a","title":"Malformed","messages":["lost row"]]) : normal(request) }
        await model.inspect()
        check(model.document == nil && model.detailError != nil,"malformed messages rejected without dropping rows")
        ConversationFixtureProtocol.handler = normal; await model.refresh()
        ConversationFixtureProtocol.delayPath = "/api/conversations/thread-a/rename"; ConversationFixtureProtocol.delayed = false
        let renameRace = Task { await model.rename(model.threads![0],title:"Refresh race") }
        try await waitForDelay(); await model.refresh()
        check(await renameRace.value && !model.acting,"concurrent list refresh cannot strand action busy state")
        ConversationFixtureProtocol.delayPath = "/api/conversations"; ConversationFixtureProtocol.delayed = false
        let reconnectRename = Task { await model.rename(model.threads![0],title:"Old backend") }
        try await waitForDelay(); await model.configure(URL(string:"http://127.0.0.1:9465")!)
        check(!(await reconnectRename.value) && model.receipt == nil && !model.acting,"mutation refresh cannot leak receipt onto changed backend")
        ConversationFixtureProtocol.delayPath = "/api/conversations/thread-a"; ConversationFixtureProtocol.delayed = false
        model.selection = "thread-a"
        let disconnectInspect = Task { await model.inspect() }
        try await waitForDelay(); await model.configure(nil); await disconnectInspect.value
        check(model.document == nil && model.threads == nil && !model.inspecting,"disconnect suppresses late detail and clears records")
        await model.configure(base)
        ConversationFixtureProtocol.delayPath = "/api/conversations/thread-a"; ConversationFixtureProtocol.delayed = false
        let oldDelete = Task { await model.deleteConfirmed(model.threads![0],onDeleted:{callbackIDs.append($0)}) }
        try await waitForDelay(); let oldCallbackCount = callbackIDs.count
        await model.configure(URL(string:"http://127.0.0.1:9466")!)
        check(!(await oldDelete.value) && callbackIDs.count == oldCallbackCount && model.receipt == nil,"old backend deletion response cannot call parent after reconnect")
        let remoteBefore = ConversationFixtureProtocol.requests.count
        do { _ = try await NativeConversationClient(baseURL:URL(string:"https://example.com")!,session:session).request("/api/conversations"); fatalError("remote accepted") } catch {}
        check(ConversationFixtureProtocol.requests.count == remoteBefore,"remote origins rejected before transport")
        check(NativeConversationWire.segment("thread/a?é") == "thread%2Fa%3F%C3%A9","opaque IDs safely percent encoded")
        session.invalidateAndCancel()
        print("PASS: \(checks) native conversation fixture assertions; no real backend/user data")
    }
}
