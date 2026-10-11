import Foundation

@main struct NativeSessionRecoveryFeatureTests {
    static var count = 0
    static func check(_ ok: Bool, _ label: String) { guard ok else { fatalError("FAIL: " + label) }; count += 1 }
    static func refuses(_ label: String, _ action: () throws -> Void) { do { try action(); fatalError("Accepted: " + label) } catch { count += 1 } }
    @MainActor static func rejects(_ label: String, _ action: () async throws -> Void) async { do { try await action(); fatalError("Accepted: " + label) } catch { count += 1 } }
    static func row(_ position: Any, _ role: String = "user", _ text: String = "Repeated") -> [String: Any] { ["role":role,"text":text,"ts_ms":position] }
    static func body(_ sid: String, _ rows: [[String: Any]], primary: Bool = false) -> [String: Any] {
        var result: [String:Any] = ["session_id":sid,"messages":rows,"count":rows.count]
        if primary { result["primary_session_id"] = sid }; return result
    }
    @MainActor static func main() async throws {
        let owner = "primary-fixture", url = URL(string:"http://127.0.0.1:9465")!
        check(try NativeSessionRecoveryWire.primary(["session_id":owner]) == owner,"verified primary parsed")
        refuses("empty primary") { _ = try NativeSessionRecoveryWire.primary(["session_id":""]) }
        refuses("primary error is unavailable") { _ = try NativeSessionRecoveryWire.primary(["session_id":owner,"error":"private failure"]) }
        refuses("control characters") { _ = try NativeSessionRecoveryWire.primary(["session_id":"bad\nowner"]) }
        let repeated = try NativeSessionRecoveryWire.transcript(body(owner,[row(1),row(3),row(5,"assistant","Reply")],primary:true),owner:owner,primary:owner)
        let saved: [[String:Any]] = [["id":"local","role":"user","content":"Repeated","attachments":[["id":"keep"]]], ["id":"rich","role":"assistant","content":"Reply","tools":[["id":"tool"]],"reasoning":"Keep reasoning","model":"local"]]
        let merged = try NativeSessionRecoveryWire.merge(existing:saved,transcript:repeated)
        check(merged.count == 3,"one matching existing occurrence absorbs only one repeated row")
        check(try JSONSerialization.data(withJSONObject:Array(merged.prefix(2)),options:.sortedKeys) == JSONSerialization.data(withJSONObject:saved,options:.sortedKeys),"existing metadata preserved byte-for-byte")
        check(merged.last?["content"] as? String == "Repeated","unmatched identical occurrence retained")
        let again = try NativeSessionRecoveryWire.merge(existing:merged,transcript:repeated)
        check(again.count == 3,"repeated recovery is idempotent within same overlapping history")
        let different = try NativeSessionRecoveryWire.merge(existing:saved,transcript:NativeRecoveryTranscript(sessionID:owner,rows:[row(6,"assistant","Different canonical reply")]))
        check(different.count == 3 && different[1]["content"] as? String == "Reply","differing canonical text does not replace local rich row")
        let empty = try NativeSessionRecoveryWire.transcript(body(owner,[],primary:true),owner:owner,primary:owner)
        check(try NativeSessionRecoveryWire.merge(existing:saved,transcript:empty).count == 2,"empty live transcript retains durable UI records")
        refuses("foreign transcript") { _ = try NativeSessionRecoveryWire.transcript(body("foreign",[]),owner:owner,primary:nil) }
        refuses("missing primary proof") { _ = try NativeSessionRecoveryWire.transcript(body(owner,[]),owner:owner,primary:owner) }
        var issue = body(owner,[]); issue["error"] = "orchestrator unavailable"
        refuses("unknown is not empty") { _ = try NativeSessionRecoveryWire.transcript(issue,owner:owner,primary:nil) }
        for bad in ([true,0,-1,1.2,Double.infinity,"1"] as [Any]) {
            refuses("finite strict positive position") { _ = try NativeSessionRecoveryWire.transcript(body(owner,[row(bad)]),owner:owner,primary:nil) }
        }
        refuses("positions strictly increasing") { _ = try NativeSessionRecoveryWire.transcript(body(owner,[row(2),row(2)]),owner:owner,primary:nil) }
        refuses("tool row not public transcript") { _ = try NativeSessionRecoveryWire.transcript(body(owner,[row(1,"tool")]),owner:owner,primary:nil) }
        refuses("empty text") { _ = try NativeSessionRecoveryWire.transcript(body(owner,[row(1,"user"," \n")]),owner:owner,primary:nil) }
        refuses("bounded text") { _ = try NativeSessionRecoveryWire.transcript(body(owner,[row(1,"user",String(repeating:"a",count:65537))]),owner:owner,primary:nil) }
        var badCount = body(owner,[]); badCount["count"] = true
        refuses("strict receipt count") { _ = try NativeSessionRecoveryWire.transcript(badCount,owner:owner,primary:nil) }
        refuses("manually constructed malformed transcript revalidated") { _ = try NativeSessionRecoveryWire.merge(existing:saved,transcript:NativeRecoveryTranscript(sessionID:owner,rows:[row(false)])) }
        refuses("non JSON saved metadata is retained by refusal") { _ = try NativeSessionRecoveryWire.merge(existing:[["date":Date()]],transcript:empty) }
        let suite = "ai.feral.session-recovery.fixture." + UUID().uuidString
        let prefs = UserDefaults(suiteName:suite)!; defer { prefs.removePersistentDomain(forName:suite) }
        var requests: [URLRequest] = [], primaryID = owner, responseCode = 200
        var continuation: CheckedContinuation<Void,Never>?, hold = false
        let model = NativeSessionRecoveryModel(preferences:prefs,transport:{ request in
            requests.append(request)
            if hold { hold = false; await withCheckedContinuation { continuation = $0 } }
            let data: [String:Any] = request.url!.path == "/api/sessions/primary" ? ["session_id":primaryID] : body(request.url!.path == "/api/sessions/primary/transcript" ? primaryID : "isolated/thread",[],primary:request.url!.path == "/api/sessions/primary/transcript")
            return (try JSONSerialization.data(withJSONObject:data),HTTPURLResponse(url:request.url!,statusCode:responseCode,httpVersion:nil,headerFields:nil)!)
        })
        refuses("preferences unavailable without verified installation") { _ = try model.selectedConversationID() }
        model.configure(baseURL:url,connectionID:UUID())
        await rejects("transcript cannot guess primary") { _ = try await model.transcript(runtimeSessionID:owner) }
        _ = try await model.resolvePrimary(); try model.rememberSelection("isolated/thread")
        check(try model.selectedConversationID() == "isolated/thread","exact selected isolated conversation persisted")
        _ = try await model.transcript(runtimeSessionID:owner)
        check(requests.last?.url?.path == "/api/sessions/primary/transcript" && requests.last?.url?.query == "limit=500&since_ms=0","primary recovery passive bounded endpoint")
        _ = try await model.transcript(runtimeSessionID:"isolated/thread")
        check(requests.last?.url?.absoluteString.contains("isolated%2Fthread/transcript") == true,"isolated scope encoded as one path segment")
        check(requests.allSatisfy { $0.httpMethod == "GET" && $0.httpBody == nil },"recovery never sends mutations")
        primaryID = "second-install"; model.configure(baseURL:url,connectionID:UUID()); _ = try await model.resolvePrimary()
        check(try model.selectedConversationID() == nil,"selection never crosses primary installation")
        primaryID = owner; model.configure(baseURL:url,connectionID:UUID()); _ = try await model.resolvePrimary()
        check(try model.selectedConversationID() == "isolated/thread","original installation restores only its selection")
        hold = true
        let pending = Task { try await model.transcript(runtimeSessionID:owner) }
        while continuation == nil { await Task.yield() }
        model.configure(baseURL:URL(string:"http://127.0.0.1:9466")!,connectionID:UUID()); continuation?.resume(); continuation = nil
        await rejects("stale connection recovery rejected") { _ = try await pending.value }
        check(model.primarySessionID == nil,"stale completion cannot restore primary authority")
        model.configure(baseURL:url,connectionID:UUID()); hold = true
        let olderPrimary = Task { try await model.resolvePrimary() }
        while continuation == nil { await Task.yield() }
        _ = try await model.resolvePrimary()
        continuation?.resume(); continuation = nil
        await rejects("older concurrent primary lookup rejected") { _ = try await olderPrimary.value }
        check(model.primarySessionID == owner,"older primary lookup cannot overwrite latest authority")
        model.configure(baseURL:url,connectionID:UUID()); responseCode = 503
        await rejects("unavailable primary fails closed") { _ = try await model.resolvePrimary() }
        check(model.primarySessionID == nil,"unavailable is not a verified empty installation")
        model.configure(baseURL:URL(string:"http://example.com")!,connectionID:UUID())
        let before = requests.count
        await rejects("remote service refused before transport") { _ = try await model.resolvePrimary() }
        check(requests.count == before,"remote recovery sends no request")
        print("NativeSessionRecoveryFeatureTests: \(count) assertions passed")
    }
}
