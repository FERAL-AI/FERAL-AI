import Foundation

final class MemoryFixtureProtocol: URLProtocol {
    static var requests: [URLRequest] = []
    static var delayPath: String?
    static var didDelay = false
    static var handler: (URLRequest) -> (Int, Any) = { request in
        switch request.url!.path {
        case "/internal/memory/recent": return (200, [["id":"note/a", "content":"A remembered fact", "tags":["personal"], "created_at":1234, "future_metadata":["keep":true]]])
        case "/api/memory/stats": return (200, ["ok":true, "totals":["notes":1,"episodes":2,"knowledge_triples":3]])
        case "/internal/memory/stats": return (200, ["observability":["embedding_provider":"hash","active_vector_store":"numpy"]])
        case "/api/sync/scopes": return (200, ["ok":true,"grants":[["scope":"family"],["scope":"family"]]])
        case "/internal/memory/save": return (200, ["id":"new-note", "status":"saved"])
        default: return (200, ["deleted":true])
        }
    }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        var captured = request
        if captured.httpBody == nil, let stream = request.httpBodyStream {
            stream.open(); defer { stream.close() }
            var data = Data(); var buffer = [UInt8](repeating:0,count:4096)
            while stream.hasBytesAvailable { let count = stream.read(&buffer,maxLength:buffer.count); if count <= 0 { break }; data.append(buffer,count:count) }
            captured.httpBody = data
        }
        Self.requests.append(captured)
        let (status, value) = Self.handler(request)
        let response = HTTPURLResponse(url:request.url!,statusCode:status,httpVersion:nil,headerFields:["Content-Type":"application/json"])!
        let deliver = {
            self.client?.urlProtocol(self,didReceive:response,cacheStoragePolicy:.notAllowed)
            self.client?.urlProtocol(self,didLoad:try! JSONSerialization.data(withJSONObject:value))
            self.client?.urlProtocolDidFinishLoading(self)
        }
        if Self.delayPath == request.url!.path {
            Self.delayPath = nil; Self.didDelay = true
            DispatchQueue.global().asyncAfter(deadline:.now()+0.15,execute:deliver)
        } else { deliver() }
    }
    override func stopLoading() {}
}

@main struct MemoryFeatureTests {
    static var checks = 0
    @MainActor static func expect(_ value: Bool, _ name: String) {
        if !value { fatalError("FAIL: \(name)") }
        checks += 1
    }
    @MainActor static func main() async throws {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [MemoryFixtureProtocol.self]
        let session = URLSession(configuration:config)
        let base = URL(string:"http://127.0.0.1:9464")!
        let store = NativeMemoryFeatureStore(session:session)
        await store.connect(nil)
        expect(MemoryFixtureProtocol.requests.isEmpty && !store.available && store.records == nil, "nil readiness makes no requests")
        await store.connect(base)
        expect(store.records?.count == 1 && store.statsError == nil, "loads notes and real counts")
        expect(store.scopes == ["family"], "only actual unique granted scopes")
        expect(store.observability?["embedding_provider"] as? String == "hash", "actual index/provider metadata")
        let snapshot = try JSONSerialization.jsonObject(with:store.snapshot()) as! [String:Any]
        let savedRows = snapshot["records"] as! [[String:Any]]
        expect((savedRows[0]["future_metadata"] as? [String:Bool])?["keep"] == true && snapshot["complete_store_backup"] as? Bool == false, "export preserves unknown metadata and bounded contract")
        expect(await store.save(content:"  new memory  ",tags:" idea, personal, ",scope:""), "confirmed private save")
        let privateRequest = MemoryFixtureProtocol.requests.last { $0.httpMethod == "POST" }!
        let privateBody = try JSONSerialization.jsonObject(with:privateRequest.httpBody!) as! [String:Any]
        expect(privateBody["scope"] == nil && privateBody["content"] as? String == "new memory" && privateBody["tags"] as? [String] == ["idea","personal"], "private omits scope, trims content/tags")
        let count = MemoryFixtureProtocol.requests.count
        expect(!(await store.save(content:"shared",tags:"",scope:"invented")) && MemoryFixtureProtocol.requests.count == count, "rejects ungranted scope without request")
        expect(await store.save(content:"shared",tags:"",scope:"family"), "confirmed granted save")
        let shared = MemoryFixtureProtocol.requests.last { $0.httpMethod == "POST" }!
        let sharedBody = try JSONSerialization.jsonObject(with:shared.httpBody!) as! [String:Any]
        expect(sharedBody["scope"] as? String == "family", "actual scope on wire")
        await store.deleteConfirmed(store.records![0])
        let deletion = MemoryFixtureProtocol.requests.last { $0.httpMethod == "DELETE" }!
        expect(deletion.url!.absoluteString.contains("note%2Fa"), "delete ID encoded as one path segment")
        let defaultHandler = MemoryFixtureProtocol.handler
        MemoryFixtureProtocol.handler = { request in
            if request.url!.path == "/api/memory/search" {
                return (200,["results":[["id":"1","tier":"note","content":"weak fact","score":0.2,"new_field":["source":"preserved"]],["id":"1","tier":"episode","summary":"experience","score":0.9]],"degradations":[["tier":"knowledge","error":"offline"]]])
            }
            return defaultHandler(request)
        }
        store.section = .search; store.query = "a/b & café?"
        await store.reload()
        expect(store.records?.count == 2 && Set(store.records!.map(\.id)).count == 2 && store.degradations.count == 1, "cross-tier search distinct identity and degradations")
        let search = MemoryFixtureProtocol.requests.last { $0.url!.path == "/api/memory/search" }!
        expect(URLComponents(url:search.url!,resolvingAgainstBaseURL:false)!.queryItems!.first { $0.name == "q" }?.value == "a/b & café?", "query survives URL encoding")
        store.tier = "episode"
        expect(store.filtered.count == 1 && store.filtered[0].tier == "episode", "tier filter actual results")
        store.query = "not yet searched"
        let searchSnapshot = try JSONSerialization.jsonObject(with:store.snapshot()) as! [String:Any]
        expect(searchSnapshot["query"] as? String == "a/b & café?", "snapshot names completed query not edited text")
        await store.reload(); await store.reload()
        expect(MemoryFixtureProtocol.requests.filter { $0.url!.path == "/api/memory/search" }.count == 3, "repeat query requests again")
        MemoryFixtureProtocol.handler = { request in
            if request.url!.path == "/api/memory/search" { return (503,["detail":"memory unavailable"]) }
            return defaultHandler(request)
        }
        await store.reload()
        expect(store.error?.contains("503") == true && store.records == nil, "HTTP failure is not empty results")
        do { _ = try store.snapshot(); fatalError("export should fail") } catch {}
        MemoryFixtureProtocol.handler = { request in
            if request.url!.path == "/internal/memory/save" { return (200,["error":"content rejected"]) }
            return defaultHandler(request)
        }
        expect(!(await store.save(content:"x",tags:"",scope:"")) && store.error == "content rejected", "HTTP200 logical failure not saved")
        MemoryFixtureProtocol.handler = { request in
            if request.url!.path == "/api/sync/scopes" { return (503,["detail":"grants unavailable"]) }
            if request.url!.path == "/api/memory/stats" { return (200,["ok":false,"reason":"stats_timeout","totals":["notes":0]]) }
            if request.url!.path == "/internal/memory/note/a" || request.httpMethod == "DELETE" { return (200,["deleted":false]) }
            return defaultHandler(request)
        }
        store.section = .recent; await store.reload()
        expect(store.scopes.isEmpty && store.scopeError != nil && store.statsError?.contains("stats_timeout") == true, "grants/stats failures not zero or no-peer claims")
        await store.deleteConfirmed(store.records![0])
        expect(store.error?.contains("did not confirm deletion") == true && store.records?.count == 1, "false deletion retains record and error")
        MemoryFixtureProtocol.handler = { request in
            if request.url!.path == "/api/knowledge/entities" { return (200,["entities":[["name":"Alex / café?","type":"person"]]]) }
            if request.url!.absoluteString.contains("/internal/knowledge/about/") { return (200,["facts":[["source":"unknown metadata","object":"coffee"]]]) }
            return defaultHandler(request)
        }
        store.section = .knowledge; await store.reload(); store.selection = store.records![0].id; await store.inspect()
        expect((store.detail as? [String:Any])?["facts"] != nil, "entity detail fetched")
        let detail = MemoryFixtureProtocol.requests.last { $0.url!.absoluteString.contains("/internal/knowledge/about/") }!
        expect(detail.url!.absoluteString.contains("Alex%20%2F%20caf%C3%A9%3F"), "entity path encoded safely")
        MemoryFixtureProtocol.handler = defaultHandler
        store.section = .recent; await store.reload()
        MemoryFixtureProtocol.delayPath = "/internal/memory/save"; MemoryFixtureProtocol.didDelay = false
        let saveRace = Task { await store.save(content:"race",tags:"",scope:"") }
        while !MemoryFixtureProtocol.didDelay { try await Task.sleep(nanoseconds:1_000_000) }
        await store.reload()
        expect(await saveRace.value && !store.busy, "concurrent list refresh does not strand mutation busy flag")
        MemoryFixtureProtocol.delayPath = "/internal/memory/recent"; MemoryFixtureProtocol.didDelay = false
        let saveReconnect = Task { await store.save(content:"old backend",tags:"",scope:"") }
        while !MemoryFixtureProtocol.didDelay { try await Task.sleep(nanoseconds:1_000_000) }
        await store.connect(URL(string:"http://127.0.0.1:9465")!)
        expect(!(await saveReconnect.value) && store.notice == nil && !store.busy, "save post-write refresh cannot leak receipt to reconnected backend")
        MemoryFixtureProtocol.delayPath = "/internal/memory/recent"; MemoryFixtureProtocol.didDelay = false
        let oldNote = store.records![0]
        let deleteReconnect = Task { await store.deleteConfirmed(oldNote) }
        while !MemoryFixtureProtocol.didDelay { try await Task.sleep(nanoseconds:1_000_000) }
        await store.connect(URL(string:"http://127.0.0.1:9466")!)
        await deleteReconnect.value
        expect(store.notice == nil && !store.busy, "delete post-write refresh cannot leak receipt to reconnected backend")
        let prior = MemoryFixtureProtocol.requests.count
        do {
            _ = try await NativeMemoryClient(baseURL:URL(string:"https://example.com")!,session:session).request("/internal/memory/recent")
            fatalError("non-local base accepted")
        } catch {}
        expect(MemoryFixtureProtocol.requests.count == prior, "non-owned remote origin rejected before URLSession")
        await store.connect(nil)
        expect(store.records == nil && store.detail == nil && store.scopes.isEmpty && !store.busy, "disconnect clears prior backend data")
        session.invalidateAndCancel()
        print("PASS: \(checks) native memory fixture assertions; no real backend/user data")
    }
}
