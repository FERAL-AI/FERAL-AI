import Foundation

final class ConnectionsFixture: URLProtocol {
    static var requests: [URLRequest] = []
    static var delayPath: String?
    static var delayed = false
    static var delayMethod:String?
    static func body(_ request:URLRequest) -> Data {
        if let data = request.httpBody { return data }
        guard let stream = request.httpBodyStream else { return Data() }
        stream.open(); defer { stream.close() }
        var data = Data(), buffer = [UInt8](repeating:0,count:4096)
        while stream.hasBytesAvailable { let n = stream.read(&buffer,maxLength:buffer.count); if n <= 0 { break }; data.append(buffer,count:n) }
        return data
    }
    static var bundle: [String:Any] {
        ["node_id":"owner-brain","vector_clock":["owner-brain":"1:0:owner-brain"],"operations":[
            ["op_id":"o1","table":"notes","op_type":"delete","row_id":"n1","data":["id":"n1"],"hlc":"1:0:owner-brain","origin_node":"owner-brain","timestamp":1000,"scope":"private","future_metadata":["preserve":true]]],"extra_bundle_metadata":["preserve":true]]
    }
    static func normal(_ request:URLRequest) -> (Int,Any) {
        let path = request.url!.path
        let input = (try? JSONSerialization.jsonObject(with:request.httpBody ?? Data())) as? [String:Any] ?? [:]
        switch path {
        case "/api/devices/connected": return (200,["devices":[["node_id":"phone-a","type":"phone","status":"connected","platform":"iOS","subdevices":[]]],"offline":[["node_id":"glasses-a","status":"disconnected","subdevices":[]]]])
        case "/api/devices/paired":
            var rows: [[String:Any]] = [["device_id":"claimed-1","node_id":"phone-a","name":"Phone","claimed_at":1000,"is_device":true]]
            if URLComponents(url:request.url!,resolvingAgainstBaseURL:false)?.queryItems?.contains(where:{ $0.name == "include_unclaimed" && $0.value == "true" }) == true { rows.append(["device_id":"pending-1","name":"Unclaimed code","claimed_at":NSNull(),"is_device":false]) }
            return (200,["devices":rows])
        case "/api/hardware/mesh": return (200,["nodes":[["node_id":"phone-a"]],"announced_devices":[["device_id":"wearable-a","name":"Glasses","device_kind":"glasses","scanner_node_id":"phone-a","rssi_dbm":-40]]])
        case "/api/access/status": return (200,["pairing_mode":"localhost","remote_url":"","tailscale":["installed":false,"running":false,"logged_in":false,"error":"tailscale_not_installed"],"funnel":["active":false]])
        case "/api/sync/status": return (200,["enabled":true,"running":true,"node_id":"local-brain","peers":["owner-brain"],"peer_count":1,"wal_entries":5,"identity_mode":"mixed"])
        case "/api/handoff/devices": return (200,["devices":[["session_id":"thread-a","node_type":"desktop","node_id":"mac-a"],["session_id":"phone-session","node_type":"phone","node_id":"phone-a"]]])
        case "/api/devices/phone-a/capabilities":
            if request.httpMethod == "POST" { return (200,["ok":true,"node_id":"phone-a","changed":[input["capability"]!],"granted":input["granted"]!,"notified":false,"capabilities":[]]) }
            return (200,["node_id":"phone-a","connected":true,"capabilities":[["capability":"camera","tier":"camera","granted":true,"explicit":false]]])
        case "/api/access/mode": return (200,["ok":true,"mode":input["mode"]!,"restart_required":true,"bind_host":"0.0.0.0"])
        case "/api/access/remote-up": return (503,["detail":["code":"tailscale_not_installed","message":"Fixture has no account"]])
        case "/api/access/remote-down": return (200,["ok":true,"pairing_mode":"localhost"])
        case "/api/devices/pair/url": return (200,["v":1,"schema":2,"url":"http://192.0.2.1/pair?t=disposable","device_id":"pending-1","pin_required":true,"pin":"0123","expires":4_000_000_000])
        case "/api/devices/pair/prune": return (200,["success":true,"revoked":1])
        case "/api/devices/pending-1", "/api/devices/claimed-1": return (200,["ok":true])
        case "/api/sync/export": return (200,bundle)
        case "/api/sync/import": return (200,["applied":0])
        case "/api/handoff": return (200,["ok":true,"success":true,"pending":false,"from_session_id":input["from_session"]!,"to_node_type":input["to_node_type"]!,"to_session_id":"phone-session","messages_transferred":4])
        default: return (404,["detail":"unknown fixture path \(path)"])
        }
    }
    static var handler:(URLRequest) -> (Int,Any) = normal
    override class func canInit(with request:URLRequest) -> Bool { true }
    override class func canonicalRequest(for request:URLRequest) -> URLRequest { request }
    override func startLoading() {
        var captured = request
        if request.httpMethod == "POST" { captured.httpBody = Self.body(request) }
        Self.requests.append(captured)
        let (status,value) = Self.handler(captured)
        let response = HTTPURLResponse(url:request.url!,statusCode:status,httpVersion:nil,headerFields:nil)!
        let data = try! JSONSerialization.data(withJSONObject:value)
        let deliver = { self.client?.urlProtocol(self,didReceive:response,cacheStoragePolicy:.notAllowed); self.client?.urlProtocol(self,didLoad:data); self.client?.urlProtocolDidFinishLoading(self) }
        if Self.delayPath == request.url!.path && (Self.delayMethod == nil || Self.delayMethod == request.httpMethod) { Self.delayPath = nil; Self.delayed = true; DispatchQueue.global().asyncAfter(deadline:.now()+0.1,execute:deliver) } else { deliver() }
    }
    override func stopLoading() {}
}

@main struct NativeConnectionsFeatureTests {
    static var count = 0
    @MainActor static func check(_ condition:Bool,_ name:String) { if !condition { fatalError("FAIL: \(name)") }; count += 1 }
    @MainActor static func awaitDelay() async throws {
        for _ in 0..<1000 { if ConnectionsFixture.delayed { return }; try await Task.sleep(nanoseconds:1_000_000) }
        fatalError("Fixture delay not reached")
    }
    @MainActor static func main() async throws {
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [ConnectionsFixture.self]
        let session = URLSession(configuration:config), base = URL(string:"http://127.0.0.1:9464")!
        let model = NativeConnectionsModel(session:session)
        await model.configure(baseURL:nil,sessionID:nil)
        check(ConnectionsFixture.requests.isEmpty && !model.available,"nil readiness makes no requests")
        await model.configure(baseURL:base,sessionID:"thread-a")
        check(model.errors.isEmpty && model.rows("connected").count == 1 && model.rows("connected",key:"offline").count == 1,"actual connected/offline resource shapes")
        check(model.unclaimed.count == 1 && model.unclaimed[0].raw["device_id"] as? String == "pending-1","unclaimed codes are not attached devices")
        check(!ConnectionsFixture.requests.contains { $0.url!.path == "/api/devices/pair/url" } && ConnectionsFixture.requests.allSatisfy { $0.httpMethod == "GET" },"passive refresh never mints pairing token or changes access")
        let pairing = try model.review(.pair("Phone & glasses"))
        check(!ConnectionsFixture.requests.contains { $0.url!.path == "/api/devices/pair/url" } && pairing.explanation.contains("credential"),"review is read-free explicit token consent")
        check(await model.perform(pairing) && model.pairLink?["pin"] as? String == "0123","confirmed unexpired PIN pairing payload")
        let pairRequest = ConnectionsFixture.requests.first { $0.url!.path == "/api/devices/pair/url" }!
        let query = URLComponents(url:pairRequest.url!,resolvingAgainstBaseURL:false)!.queryItems!
        check(query.first { $0.name == "pin" }?.value == "true" && query.first { $0.name == "name" }?.value == "Phone & glasses","explicit PIN request and safe name encoding")
        check(await model.perform(try model.review(.revoke("pending-1"))) && model.pairLink == nil,"revoked code credential cleared")
        check(await model.perform(try model.review(.accessMode("local"))) && model.receipt?.contains("Restart is required") == true,"saved mode not falsely described as live network")
        let mode = ConnectionsFixture.requests.last { $0.url!.path == "/api/access/mode" }!
        check((try JSONSerialization.jsonObject(with:mode.httpBody!) as! [String:Any])["mode"] as? String == "local","access mode canonical action wire")
        check(!(await model.perform(try model.review(.remote(true)))) && model.actionError?.contains("503") == true && model.receipt == nil,"Tailscale fixture failure not remote success")
        model.selectedNode = "phone-a"; await model.loadCapabilities()
        check(model.capabilities?.first?["explicit"] as? Bool == false,"default permission distinct from explicit decision")
        check(await model.perform(try model.review(.capability(node:"phone-a",name:"camera",granted:false))) && model.receipt?.contains("not confirmed") == true,"correct node/capability decision and notification limitation")
        let cap = ConnectionsFixture.requests.last { $0.httpMethod == "POST" && $0.url!.path.hasSuffix("capabilities") }!
        let capBody = try JSONSerialization.jsonObject(with:cap.httpBody!) as! [String:Any]
        check(capBody.count == 2 && capBody["capability"] as? String == "camera" && capBody["granted"] as? Bool == false,"capability write only exact permission fields")
        let imported = try model.review(.importBundle(ConnectionsFixture.bundle))
        check(imported.explanation.contains("1 deletions") && imported.explanation.contains("notes") && imported.explanation.contains("no one-click undo"),"import review exposes deletion/table scope")
        check(await model.perform(imported) && model.receipt?.contains("applied 0") == true,"zero applied receipt honest duplicate/refusal semantics")
        let importRequest = ConnectionsFixture.requests.last { $0.url!.path == "/api/sync/import" }!
        let importedBody = try JSONSerialization.jsonObject(with:importRequest.httpBody!) as! [String:Any]
        let operation = (importedBody["operations"] as! [[String:Any]])[0]
        check(operation["scope"] as? String == "private" && (operation["future_metadata"] as? [String:Bool])?["preserve"] == true && importedBody["extra_bundle_metadata"] != nil,"import preserves scope and unknown metadata unchanged")
        check(await model.perform(try model.review(.exportBundle)) && model.exportData != nil && model.receipt?.contains("save dialog") == true,"export fetched not falsely saved to disk")
        let exported = try JSONSerialization.jsonObject(with:model.exportData!) as! [String:Any]
        check((exported["operations"] as! [[String:Any]])[0]["scope"] as? String == "private","export contains actual private operations")
        let handoff = try model.review(.handoff(session:"thread-a",type:"phone",depth:20))
        check(handoff.explanation.contains("first connected") && handoff.explanation.contains("pending tools"),"handoff class selection and transfer limitations disclosed")
        check(await model.perform(handoff) && model.receipt?.contains("4 working-memory") == true,"exact handoff confirmed copied receipt")
        let handoffRequest = ConnectionsFixture.requests.last { $0.url!.path == "/api/handoff" }!
        let handoffBody = try JSONSerialization.jsonObject(with:handoffRequest.httpBody!) as! [String:Any]
        check(handoffBody.count == 3 && handoffBody["from_session"] as? String == "thread-a" && handoffBody["to_node_type"] as? String == "phone" && handoffBody["history_depth"] as? Int == 20 && handoffBody["target"] == nil,"handoff uses real server contract, never stale web target field")
        let normal = ConnectionsFixture.normal
        ConnectionsFixture.handler = { request in request.url!.path == "/api/handoff" ? (200,["ok":true,"success":true,"pending":true,"from_session_id":"thread-a","to_node_type":"phone","messages_transferred":0]) : normal(request) }
        check(await model.perform(try model.review(.handoff(session:"thread-a",type:"phone",depth:20))) && model.receipt?.contains("queued") == true,"queued handoff not completed transfer")
        ConnectionsFixture.handler = { request in request.url!.path == "/api/handoff" ? (200,["ok":true,"success":true,"pending":false,"from_session_id":"wrong","to_node_type":"phone","to_session_id":"phone-session","messages_transferred":4]) : normal(request) }
        check(!(await model.perform(try model.review(.handoff(session:"thread-a",type:"phone",depth:20)))) && model.actionError != nil,"wrong source receipt cannot confirm handoff")
        ConnectionsFixture.handler = normal
        let stale = try model.review(.accessMode("local"))
        await model.configure(baseURL:base,sessionID:"thread-b")
        let staleCount = ConnectionsFixture.requests.count
        check(!(await model.perform(stale)) && ConnectionsFixture.requests.count == staleCount,"session change invalidates reviewed action before dispatch")
        do { _ = try model.review(.handoff(session:"thread-a",type:"phone",depth:20)); fatalError("old source accepted") } catch {}
        check(ConnectionsFixture.requests.count == staleCount,"wrong current thread rejected before action")
        await model.configure(baseURL:base,sessionID:"thread-a")
        ConnectionsFixture.delayPath = "/api/access/mode"; ConnectionsFixture.delayed = false
        let delayedReview = try model.review(.accessMode("local"))
        let late = Task { await model.perform(delayedReview) }
        try await awaitDelay(); await model.configure(baseURL:URL(string:"http://127.0.0.1:9465")!,sessionID:"thread-a")
        check(!(await late.value) && model.receipt == nil && !model.acting,"late old-backend receipt discarded")
        ConnectionsFixture.handler = { request in request.url!.path == "/api/devices/connected" ? (200,["devices":[]]) : normal(request) }
        await model.refresh()
        check(model.payloads["connected"] == nil && model.errors["connected"] != nil && model.payloads["sync"] != nil,"malformed list unavailable with independent other sections")
        ConnectionsFixture.handler = { request in request.url!.path == "/api/devices/pair/url" ? (200,["url":"http://192.0.2.1/pair","device_id":"pending-1","pin_required":true,"pin":"abcd","expires":4_000_000_000]) : normal(request) }
        check(!(await model.perform(try model.review(.pair("Disposable")))) && model.pairLink == nil,"malformed PIN not published as usable pairing")
        do { _ = try NativeConnectionsWire.bundle(Data("{\"node_id\":\"x\",\"operations\":[1],\"vector_clock\":{}}".utf8)); fatalError("malformed import accepted") } catch {}
        check(ConnectionsFixture.requests.allSatisfy { $0.url!.host == "127.0.0.1" },"all fixture traffic remains literal loopback")
        let remoteCount = ConnectionsFixture.requests.count
        for url in ["https://example.com", "http://localhost:9464", "http://secret@127.0.0.1:9464"] {
            do { _ = try await NativeConnectionsClient(baseURL:URL(string:url)!,session:session).request("/api/access/status"); fatalError("unsafe origin accepted") } catch {}
        }
        check(ConnectionsFixture.requests.count == remoteCount,"DNS/remote/credential URLs rejected before transport")
        let delegate = NativeConnectionsRedirectGuard(), source = URL(string:"http://127.0.0.1:9464/api/sync/import")!
        let task = session.dataTask(with:URLRequest(url:source)), response = HTTPURLResponse(url:source,statusCode:307,httpVersion:nil,headerFields:nil)!
        var forwarded = true
        delegate.urlSession(session,task:task,willPerformHTTPRedirection:response,newRequest:URLRequest(url:URL(string:"https://example.com/upload")!)) { forwarded = $0 != nil }
        check(!forwarded,"cross-origin redirect refused without request")
        delegate.urlSession(session,task:task,willPerformHTTPRedirection:response,newRequest:URLRequest(url:URL(string:"http://127.0.0.1:9464/api/sync/import/")!)) { forwarded = $0 != nil }
        check(forwarded,"same-origin redirect remains valid")
        await model.configure(baseURL:nil,sessionID:nil)
        check(model.pairLink == nil && model.exportData == nil && model.payloads.isEmpty,"disconnect removes credential/export/user records")
        ConnectionsFixture.handler = normal;await model.configure(baseURL:base,sessionID:"thread-a")
        model.setContextPolicy(NativeSelectedContextPolicy(sessionID:"thread-a",connectionID:UUID(),taskReady:false,managed:true))
        let beforeBlocked = ConnectionsFixture.requests.count
        do { _ = try model.review(.handoff(session:"thread-a",type:"phone",depth:20));fatalError("managed handoff permitted") } catch {}
        model.setContextPolicy(NativeSelectedContextPolicy(sessionID:"thread-a",connectionID:UUID(),taskReady:true,managed:true))
        do { _ = try model.review(.handoff(session:"thread-a",type:"phone",depth:20));fatalError("ready managed handoff permitted") } catch {}
        model.setContextPolicy(NativeSelectedContextPolicy(sessionID:"thread-a",connectionID:UUID(),taskReady:false,managed:false))
        do { _ = try model.review(.handoff(session:"thread-a",type:"phone",depth:20));fatalError("unready standard handoff permitted") } catch {}
        await model.refresh()
        check(ConnectionsFixture.requests.dropFirst(beforeBlocked).allSatisfy { $0.httpMethod == "GET" } && model.payloads["handoff"] != nil,"blocked managed chat keeps passive connection/handoff inventory with no POST")
        check(await model.perform(try model.review(.accessMode("localhost"))),"global access settings remain available while chat blocked")
        let oldPolicyReview = try model.review(.accessMode("local"))
        model.setContextPolicy(NativeSelectedContextPolicy(sessionID:"other",connectionID:UUID(),taskReady:true,managed:false))
        let beforeStale = ConnectionsFixture.requests.count
        check(!(await model.perform(oldPolicyReview)) && ConnectionsFixture.requests.count == beforeStale,"exact context epoch invalidates held global settings review before dispatch")
        for action in [NativeConnectionAction.accessMode("localhost"),.capability(node:"phone-a",name:"camera",granted:false)] {
            model.setContextPolicy(NativeSelectedContextPolicy(sessionID:"thread-a",connectionID:UUID(),taskReady:true,managed:false))
            model.selectedNode = "phone-a";await model.loadCapabilities()
            let lateReadback = try model.review(action)
            let beforeLatePosts = ConnectionsFixture.requests.filter { $0.httpMethod == "POST" }.count
            ConnectionsFixture.delayed = false;ConnectionsFixture.delayMethod = "GET"
            ConnectionsFixture.handler = { request in
                let response = normal(request)
                if request.httpMethod == "POST" {
                    ConnectionsFixture.delayPath = request.url!.path.hasSuffix("capabilities") ? "/api/devices/phone-a/capabilities" : "/api/access/status"
                }
                return response
            }
            let lateReadbackTask = Task { await model.perform(lateReadback) };try await awaitDelay()
            check(model.receipt == nil && ConnectionsFixture.requests.filter { $0.httpMethod == "POST" }.count == beforeLatePosts + 1,"connections fixture holds final refresh/capability read after one effect")
            model.setContextPolicy(NativeSelectedContextPolicy(sessionID:"after-write",connectionID:UUID(),taskReady:false,managed:true))
            check(!(await lateReadbackTask.value) && model.receipt == nil && model.actionError != nil && !model.acting && ConnectionsFixture.requests.filter { $0.httpMethod == "POST" }.count == beforeLatePosts + 1,"policy switch during final connection readback publishes no stale success or duplicate effect")
            ConnectionsFixture.handler = normal;ConnectionsFixture.delayMethod = nil
        }
        task.cancel(); session.invalidateAndCancel()
        print("PASS: \(count) native Connections fixture assertions; no Tailscale, remote exposure, physical device or user-data actions")
    }
}
