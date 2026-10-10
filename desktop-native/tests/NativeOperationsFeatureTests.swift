// Standalone fixture execution; no real provider/vault/keychain operations.
import Foundation

private final class OperationsWire: URLProtocol {
    static let lock = NSLock()
    static var responses: [String: (Int, [String: Any])] = [:]
    static var calls: [(URL, String, [String: Any])] = []
    static var heldPaths = Set<String>()
    static var callbacks: [() -> Void] = []
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        var bytes = request.httpBody ?? Data()
        if bytes.isEmpty, let stream = request.httpBodyStream { stream.open(); defer { stream.close() }; var buffer = [UInt8](repeating: 0, count: 4096); while stream.hasBytesAvailable { let count = stream.read(&buffer, maxLength: buffer.count); if count <= 0 { break }; bytes.append(buffer, count: count) } }
        let body = (try? JSONSerialization.jsonObject(with: bytes)) as? [String: Any] ?? [:]
        Self.lock.lock(); let path = request.url!.path; Self.calls.append((request.url!, request.httpMethod ?? "GET", body)); let reply = Self.responses[path] ?? (200, [:])
        let respond = { [self] in client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: reply.0, httpVersion: nil, headerFields: ["Content-Type": "application/json"])!, cacheStoragePolicy: .notAllowed); client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: reply.1)); client?.urlProtocolDidFinishLoading(self) }
        let held = Self.heldPaths.contains(path); if held { Self.callbacks.append(respond) }; Self.lock.unlock(); if !held { respond() }
    }
    override func stopLoading() {}
    static func reset(_ values: [String: (Int, [String: Any])]) { lock.lock(); defer { lock.unlock() }; responses = values; calls = []; heldPaths = []; callbacks = [] }
    static func set(_ path: String, _ value: [String: Any], status: Int = 200) { lock.lock(); defer { lock.unlock() }; responses[path] = (status, value) }
    static func count(_ path: String? = nil) -> Int { lock.lock(); defer { lock.unlock() }; return calls.filter { path == nil || $0.0.path == path }.count }
    static func body(_ path: String) -> [String: Any] { lock.lock(); defer { lock.unlock() }; return calls.last(where: { $0.0.path == path && $0.1 != "GET" })?.2 ?? [:] }
    static func passiveOnly() -> Bool { lock.lock(); defer { lock.unlock() }; return calls.allSatisfy { $0.1 == "GET" && !$0.0.path.contains("probe") && (!($0.0.path.hasSuffix("/models")) || URLComponents(url: $0.0, resolvingAgainstBaseURL: false)?.queryItems?.contains(where: { $0.name == "live" && $0.value == "false" }) == true) } }
    static func hold(_ path: String) { lock.lock(); defer { lock.unlock() }; heldPaths.insert(path) }
    static func release() { lock.lock(); let values = callbacks; callbacks = []; heldPaths = []; lock.unlock(); values.forEach { $0() } }
}
private struct OperationsAssertion: Error { let message: String }
private func check(_ condition: @autoclosure () -> Bool, _ message: String) throws { if !condition() { throw OperationsAssertion(message: message) } }

@main struct NativeOperationsFeatureTests {
 static let base = URL(string: "http://127.0.0.1:9465")!
 @MainActor static func make(_ url: URL? = base) -> NativeOperationsModel { let c = URLSessionConfiguration.ephemeral; c.protocolClasses = [OperationsWire.self]; return NativeOperationsModel(baseURL: url, session: URLSession(configuration: c)) }
 static var fixture: [String:(Int,[String:Any])] { [
 "/api/jobs":(200,["items":[["id":"flow-1","kind":"taskflow","name":"Check","status":"running","cancellable_via":"POST /api/taskflows/flow-1/cancel"],["id":"bad","kind":"background_bash","status":"running","cancellable_via":"POST /api/taskflows/bad/cancel"]],"degraded":["daemon":"unavailable"]]),
 "/api/checkpoints/turns":(200,["turns":[["turn_id":"turn-1","session_id":"session","files":1,"actions":1,"ended_at":1234]],"note":"Shell changes are not covered."]),
 "/api/coding/activity":(200,["sessions":[["summary":"Edited fixture","agent_id":"opencode","at":1234]],"live_sessions":[["handle":"ext-1","agent_id":"opencode","cwd":"/tmp/fixture","alive":true,"turns":1]],"resumable_sessions":[]]),
 "/api/timeline":(200,["entries":[["type":"memory","title":"Fixture memory","content":"Details","timestamp":1234]]]) ] }
 static func preview(_ status: String = "restorable") -> [String:Any] { ["turn_id":"turn-1","dry_run":true,"files":[["kind":"file","path":"/tmp/fixture/a.txt","action":"restore","status":status]],"actions":[["kind":"action","target":"reminder-1","path":"","action":"compensate","status":"reversible"]]] }
 @MainActor static func main() async {
 do {
 OperationsWire.reset(fixture); let nilModel = make(nil); await nilModel.refresh(); try check(OperationsWire.count()==0 && !nilModel.fresh.contains(.jobs),"nil connection dispatched"); let remote = make(URL(string:"https://example.com")!); await remote.refresh(); try check(OperationsWire.count()==0,"remote connection dispatched"); let credential = make(URL(string:"http://user:password@127.0.0.1:9465")!); await credential.refresh(); let dns = make(URL(string:"http://localhost:9465")!); await dns.refresh(); try check(OperationsWire.count()==0,"credential or DNS base dispatched"); print("PASS unavailable, remote, DNS, and credential connections make zero requests")
 OperationsWire.reset(fixture); let m = make(); await m.refresh(); try check(m.jobs.count==2 && m.jobs[0].stopRoute != nil && m.jobs[1].stopRoute == nil && m.degraded.contains("daemon"),"allowlist/degraded parsing"); await m.stop(m.jobs[1]); try check(OperationsWire.count("/api/taskflows/bad/cancel")==0,"unsupported route executed"); OperationsWire.set("/api/taskflows/flow-1/cancel",["id":"flow-1","status":"cancelled"]); await m.stop(m.jobs[0]); try check(m.receipt?.contains("marked the TaskFlow cancelled")==true,"cancel actual contract"); print("PASS kind/ID/route allowlist and confirmed taskflow cancellation")
 OperationsWire.reset(fixture); let fail = make(); await fail.refresh(); OperationsWire.set("/api/taskflows/flow-1/cancel",["id":"different","status":"cancelled"]); await fail.stop(fail.jobs[0]); try check(fail.receipt==nil && !fail.fresh.contains(.jobs),"wrong identity reported success"); print("PASS mutation outcome identity failure invalidates action state")
 OperationsWire.reset(fixture); let cp = make(); cp.tab = .checkpoints; await cp.refresh(); OperationsWire.set("/api/checkpoints/revert",preview()); await cp.inspect("turn-1"); let reviewed = cp.preview!; try check(reviewed.entries.map(\.target)==["/tmp/fixture/a.txt","reminder-1"],"exact file and external targets missing"); OperationsWire.set("/api/checkpoints/revert",preview("drifted")); await cp.revert(reviewed,force:false); try check(OperationsWire.body("/api/checkpoints/revert")["dry_run"] as? Bool == true && cp.errors[.checkpoints]?.contains("changed")==true,"changed plan mutated"); print("PASS exact checkpoint targets and fresh plan drift blocks mutation")
 OperationsWire.reset(fixture); let coding = make(); coding.tab = .coding; await coding.refresh(); let live = coding.coding[0]; try check(coding.canOpen(live) && live.live && coding.activity[0].title=="Edited fixture","coding activity contract"); OperationsWire.set("/api/coding/sessions/ext-1/cancel",["handle":"ext-1","closed":true,"was_live":true]); await coding.closeCoding(live); try check(coding.coding.isEmpty && coding.receipt?.contains("confirmed")==true,"close real contract"); print("PASS coding live/indexed parsing, open boundary, and actual close receipt")
 OperationsWire.reset(fixture); let stale = make(); await stale.refresh(); let old = stale.jobs[0]; stale.configure(baseURL:nil); await stale.stop(old); try check(OperationsWire.count("/api/taskflows/flow-1/cancel")==0,"old review dispatched after disconnect"); print("PASS stale reviewed mutation never dispatches")
 OperationsWire.reset(fixture); OperationsWire.hold("/api/jobs"); let delayed = make(); let task = Task { await delayed.refresh() }; for _ in 0..<100 { if OperationsWire.count("/api/jobs")>0 {break}; try await Task.sleep(nanoseconds:10_000_000) }; delayed.configure(baseURL:nil); OperationsWire.release(); await task.value; try check(delayed.jobs.isEmpty && delayed.fresh.isEmpty && delayed.errors.isEmpty && !delayed.busy,"old response overwrote cleared state"); print("PASS delayed response cannot repopulate disconnected backend")
 OperationsWire.reset(fixture); let timeline = make(); timeline.tab = .timeline; await timeline.refresh(); try check(timeline.timeline[0].title=="Fixture memory","timeline typed contract"); OperationsWire.lock.lock(); let query = OperationsWire.calls.last!.0.query ?? ""; OperationsWire.lock.unlock(); try check(query.contains("type=memories"),"default loaded calendar"); print("PASS timeline defaults to memory-only bounded request")
 let redirectSession = URLSession(configuration:.ephemeral); let redirectTask = redirectSession.dataTask(with:base.appendingPathComponent("api/jobs")); let delegate = OperationsRedirectGuard(); let response = HTTPURLResponse(url:base,statusCode:307,httpVersion:nil,headerFields:nil)!; var accepted:URLRequest?; delegate.urlSession(redirectSession,task:redirectTask,willPerformHTTPRedirection:response,newRequest:URLRequest(url:URL(string:"https://example.com/api/jobs")!)) { accepted = $0 }; try check(accepted==nil,"cross-origin redirect accepted"); delegate.urlSession(redirectSession,task:redirectTask,willPerformHTTPRedirection:response,newRequest:URLRequest(url:base.appendingPathComponent("api/jobs"))) { accepted = $0 }; try check(accepted != nil,"same origin redirect rejected"); redirectSession.invalidateAndCancel(); print("PASS redirect delegate rejects off-origin replay and preserves same origin")
 print("9 Operations fixture groups passed; mocked HTTP only, no production actions or GUI verification.")
 } catch { print("FAIL \(error)"); exit(1) }
 }
}
