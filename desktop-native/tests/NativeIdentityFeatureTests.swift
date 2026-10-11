// Standalone fixture execution; no real provider/vault/keychain operations.
import Foundation

private final class SelfWire: URLProtocol {
    static let lock = NSLock()
    static var responses: [String: (Int, [String: Any])] = [:]
    static var calls: [(URL, String, [String: Any])] = []
    static var echoIdentity = false
    static var heldPaths = Set<String>()
    static var callbacks: [() -> Void] = []
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        var bytes = request.httpBody ?? Data()
        if bytes.isEmpty, let stream = request.httpBodyStream { stream.open(); defer { stream.close() }; var buffer = [UInt8](repeating: 0, count: 4096); while stream.hasBytesAvailable { let count = stream.read(&buffer, maxLength: buffer.count); if count <= 0 { break }; bytes.append(buffer, count: count) } }
        let body = (try? JSONSerialization.jsonObject(with: bytes)) as? [String: Any] ?? [:]
        Self.lock.lock(); let path = request.url!.path; Self.calls.append((request.url!, request.httpMethod ?? "GET", body)); var reply = Self.responses[path] ?? (200, [:]); if Self.echoIdentity && path == "/api/identity" && request.httpMethod == "POST" { Self.responses[path] = (200, body); reply = (200,["ok":true]) }
        let respond = { [self] in client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: reply.0, httpVersion: nil, headerFields: ["Content-Type": "application/json"])!, cacheStoragePolicy: .notAllowed); client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: reply.1)); client?.urlProtocolDidFinishLoading(self) }
        let held = Self.heldPaths.contains(path); if held { Self.callbacks.append(respond) }; Self.lock.unlock(); if !held { respond() }
    }
    override func stopLoading() {}
    static func reset(_ values: [String: (Int, [String: Any])]) { lock.lock(); defer { lock.unlock() }; responses = values; echoIdentity = false; calls = []; heldPaths = []; callbacks = [] }
    static func set(_ path: String, _ value: [String: Any], status: Int = 200) { lock.lock(); defer { lock.unlock() }; responses[path] = (status, value) }
    static func count(_ path: String? = nil) -> Int { lock.lock(); defer { lock.unlock() }; return calls.filter { path == nil || $0.0.path == path }.count }
    static func body(_ path: String) -> [String: Any] { lock.lock(); defer { lock.unlock() }; return calls.last(where: { $0.0.path == path && $0.1 != "GET" })?.2 ?? [:] }
    static func passiveOnly() -> Bool { lock.lock(); defer { lock.unlock() }; return calls.allSatisfy { $0.1 == "GET" && !$0.0.path.contains("probe") && (!($0.0.path.hasSuffix("/models")) || URLComponents(url: $0.0, resolvingAgainstBaseURL: false)?.queryItems?.contains(where: { $0.name == "live" && $0.value == "false" }) == true) } }
    static func hold(_ path: String) { lock.lock(); defer { lock.unlock() }; heldPaths.insert(path) }
    static func release() { lock.lock(); let values = callbacks; callbacks = []; heldPaths = []; lock.unlock(); values.forEach { $0() } }
}
private struct SelfAssertion: Error { let message: String }
private func check(_ condition: @autoclosure () -> Bool, _ message: String) throws { if !condition() { throw SelfAssertion(message: message) } }

@main struct NativeIdentityFeatureTests {
 static let base = URL(string:"http://127.0.0.1:9465")!
 static let identity:[String:Any] = ["name":"FERAL","personality":"Original","rules":["Be precise"],"greeting_style":"Short","voice":["tts_voice":"nova","opaque_voice":["retain":true]],"opaque":["version":7]]
 static var fact:[String:Any] { ["id":"fact-1","kind":"preference","text":"I like tea","source":"inferred","confidence":0.4,"tags":["drink"],"updated_at":1] }
 static var fixtures:[String:(Int,[String:Any])] { ["/api/identity":(200,identity),"/api/identity/soul":(200,["soul":"Original soul"]),"/api/identity/memory_md":(200,["memory":"Actual curated summary"]),"/api/about-me":(200,["facts":[fact],"kinds_supported":["preference","taboo"]]),"/api/agents/personas":(200,["personas":[["agent_id":"coding_assistant","name":"Coding Assistant","description":"Curated","system_prompt":"Prompt","tool_permissions":["coding_tools"],"tags":["coding"],"opaque":17]]])] }
 @MainActor static func make(_ url:URL? = base)->NativeIdentityModel { let c = URLSessionConfiguration.ephemeral; c.protocolClasses = [SelfWire.self]; return NativeIdentityModel(baseURL:url,session:URLSession(configuration:c)) }
 @MainActor static func main() async {
 do {
 SelfWire.reset(fixtures); for url in [nil,URL(string:"https://example.com"),URL(string:"http://user:password@127.0.0.1:9465"),URL(string:"http://localhost:9465")] { let m = make(url); await m.refresh() }; try check(SelfWire.count()==0,"unsafe/unavailable connection dispatch"); print("PASS nil, remote, credential and DNS endpoint rejection")
 SelfWire.reset(fixtures); let model = make(); await model.refresh(); model.name = "Theora"; let reviewed = try model.reviewIdentity(); try check((reviewed.body["opaque"] as? [String:Any])?["version"] as? Int == 7 && ((reviewed.body["voice"] as? [String:Any])?["opaque_voice"] as? [String:Any])?["retain"] as? Bool == true,"form lost opaque fields"); model.switchAdvanced(true); try check(model.rawIdentity.contains("Theora"),"advanced lost draft"); model.switchAdvanced(false); SelfWire.echoIdentity = true; await model.execute(reviewed); try check(model.notice?.contains("read back")==true && SelfWire.body("/api/identity")["name"] as? String == "Theora","save/readback contract"); print("PASS opaque nested identity preservation, editor switch, reviewed save/readback")
 SelfWire.reset(fixtures); let conflict = make(); await conflict.refresh(); let old = try conflict.reviewIdentity(); SelfWire.set("/api/identity",["name":"Changed remotely"]); await conflict.execute(old); try check(conflict.notice==nil && SelfWire.body("/api/identity").isEmpty,"concurrent identity overwritten"); print("PASS concurrent document change blocks replacement")
 SelfWire.reset(fixtures); let soul = make(); soul.tab = .soul; await soul.refresh(); soul.soul = ""; var blocked = false; do { _ = try soul.reviewSoul() } catch { blocked = true }; try check(blocked && SelfWire.count("/api/identity/soul")==1,"empty Soul falsely saved"); soul.soul = "New soul"; let replacement = try soul.reviewSoul(); SelfWire.set("/api/identity/soul",["ok":true,"soul":"Original soul"]); await soul.execute(replacement); try check(soul.notice==nil && soul.error?.contains("readback") == true,"ok without matching Soul readback treated as saved"); print("PASS unsupported empty Soul and mismatched readback never claim save")
 SelfWire.reset(fixtures); let about = make(); about.tab = .facts; await about.refresh(); let reject = try about.reviewFact("reject",fact:about.facts[0]); try check(reject.summary.contains("not deletion"),"reject semantics hidden"); var taboo = fact; taboo["kind"] = "taboo"; taboo["text"] = "Never assume: I like tea"; taboo["source"] = "user_stated"; taboo["confidence"] = 1.0; SelfWire.set("/api/about-me/fact-1/reject",["success":true,"fact":taboo]); await about.execute(reject); try check(about.facts[0].kind=="taboo" && about.notice?.contains("not deleted")==true,"reject not represented as taboo"); print("PASS reject preserves ID and explicitly creates taboo")
 SelfWire.reset(fixtures); let add = make(); add.tab = .facts; await add.refresh(); add.factText = "I like tea"; add.factTags = "drink, quiet"; let creation = try add.reviewFact("add"); var stated = fact; stated["source"] = "user_stated"; stated["confidence"] = 1.0; SelfWire.set("/api/about-me",["success":true,"fact":stated]); await add.execute(creation); try check(add.notice=="Personal fact stored." && SelfWire.body("/api/about-me")["source"] as? String == "user_stated","fact creation contract"); SelfWire.set("/api/about-me",["facts":[stated],"kinds_supported":["preference","taboo"]]); let deletion = try add.reviewFact("delete",fact:add.facts[0]); SelfWire.set("/api/about-me/fact-1",["success":true]); await add.execute(deletion); try check(add.facts.isEmpty && add.notice?.contains("replicated copies") == true,"delete scope/receipt"); print("PASS reviewed fact creation and deletion with bounded scope")
 SelfWire.reset(fixtures); let mismatch = make(); mismatch.tab = .facts; await mismatch.refresh(); let confirm = try mismatch.reviewFact("confirm",fact:mismatch.facts[0]); var wrong = fact; wrong["id"] = "different"; wrong["source"] = "user_stated"; wrong["confidence"] = 1.0; SelfWire.set("/api/about-me/fact-1/confirm",["success":true,"fact":wrong]); await mismatch.execute(confirm); try check(mismatch.notice==nil && !mismatch.fresh.contains(.facts),"wrong fact identity accepted"); print("PASS fact mutation mismatched identity invalidates action state")
 SelfWire.reset(fixtures); let read = make(); read.tab = .memory; await read.refresh(); try check(read.memory=="Actual curated summary","wrong memory response key"); read.tab = .personas; await read.refresh(); try check(read.personas[0].permissions==["coding_tools"] && read.personas[0].raw.contains("opaque"),"persona metadata lost"); try check(SelfWire.calls.allSatisfy { $0.1 == "GET" },"read-only persona/memory triggered mutation"); print("PASS actual memory key and read-only persona catalog")
 SelfWire.reset(fixtures); let stale = make(); await stale.refresh(); let review = try stale.reviewIdentity(); stale.configure(baseURL:nil); await stale.execute(review); try check(SelfWire.body("/api/identity").isEmpty && stale.name.isEmpty,"stale review dispatched"); print("PASS disconnected reviews cannot mutate")
 SelfWire.reset(fixtures); SelfWire.hold("/api/identity"); let delayed = make(); let task = Task { await delayed.refresh() }; for _ in 0..<100 { if SelfWire.count("/api/identity")>0 { break }; try await Task.sleep(nanoseconds:10_000_000) }; delayed.configure(baseURL:nil); SelfWire.release(); await task.value; try check(delayed.name.isEmpty && delayed.fresh.isEmpty && delayed.error==nil,"old read repopulated state"); print("PASS delayed old backend responses discarded")
 print("10 Identity fixture groups passed; mocked HTTP only; no production documents, provider calls, or GUI verification.")
 } catch { print("FAIL \(error)"); exit(1) }
 }
}
