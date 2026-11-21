import Foundation

// Deterministic wire fixtures: no registry requests, code imports, credential access or installs.
private final class CapabilitiesWire: URLProtocol {
    static var calls: [(String, String, [String: Any])] = []
    static var skill: [String: Any] = ["skill_id": "fixture", "name": "Fixture skill", "endpoints": [["id": "read", "method": "GET", "read_only": true]]]
    static var draft: [String: Any] = ["skill_id": "draft", "description": "Synthetic draft", "permissions": ["network"], "python_impl": "# fixture only"]
    static var tool: [String: Any] = ["tool_id": "tool", "name": "Fixture tool", "preview": "# truncated fixture"]
    static var proposal:[String:Any] = ["sequence_id":"0123456789ab","name":"auto_fixture","tools":["notes__read","notes__list"],"seen_count":3,"description":"Synthetic tracked sequence"]
    static var proposals:[[String:Any]] = [proposal]
    static var generated:[[String:Any]] = []
    static var additionalPending:[[String:Any]] = []
    static var stats:Any = ["sequences_tracked":1,"proposals_ready":1,"tools_generated":0,"total_uses":0]
    static var forgeResponse:[String:Any]?,missingPending = false,wrongPendingPreview = false
    static let code = String(repeating:"# synthetic, never executed\n",count:25)
    static var failPath: String?, missingSignature = false, mismatchedPreview = false, malformedDependencies = false, promotedLive = false, degraded = false
    static var hook: (() -> Void)?
    static var delayPath:String?
    static var delayed = false
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        var bytes = request.httpBody ?? Data()
        if let stream = request.httpBodyStream { stream.open(); defer { stream.close() }; var buffer = [UInt8](repeating: 0, count: 4096); while stream.hasBytesAvailable { let n = stream.read(&buffer, maxLength: buffer.count); if n <= 0 { break }; bytes.append(buffer, count: n) } }
        let body = (try? JSONSerialization.jsonObject(with: bytes)) as? [String: Any] ?? [:]
        let path = request.url!.path, method = request.httpMethod ?? "GET"; Self.calls.append((path, method, body))
        var value: Any
        switch path {
        case "/skills": value = [Self.skill]
        case "/api/skills/pending": value = ["pending": [Self.draft]]
        case "/api/tool-genesis/pending": value = ["proposals": [Self.tool] + (Self.missingPending ? [] : Self.additionalPending)]
        case "/api/tool-genesis/list": value = ["tools": Self.generated]
        case "/api/tool-genesis/proposals": value = ["proposals": Self.proposals]
        case "/api/tool-genesis/stats": value = Self.stats
        case "/api/tool-genesis/propose","/api/tool-genesis/generate":
            let intent = body["intent"] as? String,sequence = body["sequence_id"] as? String
            let id = intent.map(NativeCapabilitiesModel.intentToolID) ?? "genesis_" + (sequence ?? "missing")
            let source = intent != nil ? [] : Self.proposal["tools"] as! [String]
            let draft:[String:Any] = ["tool_id":id,"name":"Synthetic new draft","description":"Fixture generated only","preview":Self.wrongPendingPreview ? "wrong preview" : String(Self.code.prefix(400)),"source_sequence":source]
            Self.additionalPending.append(draft);Self.generated.append(["tool_id":id,"name":draft["name"]!,"description":draft["description"]!,"source":source])
            let result:[String:Any] = ["tool_id":id,"name":draft["name"]!,"description":draft["description"]!,"preview":Self.code]
            value = Self.forgeResponse ?? (intent != nil ? ["success":true,"tool_id":id,"preview":Self.code] : ["success":true,"tool":result])
        case "/api/marketplace/installed": value = ["skills": [["skill_id": "installed", "name": "Installed fixture"]]]
        case "/api/apps": value = ["apps": [["app_id": "app", "description": "Synthetic app", "missing_skill_dependencies": [["skill_id": "absent"]]]]]
        case "/api/marketplace/catalog": value = ["items": [["id": "remote", "name": "Remote metadata"]]]
        case "/api/marketplace/preview": value = ["success": true, "kind": body["kind"]!, "id": Self.mismatchedPreview ? "wrong" : body["id"]!, "permissions": ["network"], "permission_details": [["id": "network", "label": "Network", "description": "Calls remote services"]], "signature": ["verified": !Self.missingSignature, "sha256": String(repeating: "a", count: 64)], "install_token": "opaque-fixture-token", "expires_in": 300]
        case "/api/apps/preview": value = ["success": true, "app": ["app_id": "different-manifest-id"], "source": ["origin": "registry_id", "value": body["registry_id"]!], "permissions": [], "permission_details": [], "signature": ["verified": true, "sha256": String(repeating: "b", count: 64)], "skill_dependencies": Self.malformedDependencies ? ["to_install": []] : ["already_installed": [], "to_install": [["skill_id": "dependency", "permission_details": [["id": "network", "description": "fixture access"]], "signature": ["verified": true]]], "unavailable": [["skill_id": "missing", "reason": "not published"]]], "degraded": true, "install_token": "app-token", "expires_in": 300]
        case "/api/marketplace/install": value = ["success": true, "signature": ["verified": true, "sha256": String(repeating: "a", count: 64)]]
        case "/api/apps/install": value = ["success": true, "app": ["app_id": "different-manifest-id"], "degraded": Self.degraded]
        case "/api/skills/reload": value = ["ok": true, "skill_id": "fixture"]
        case "/api/skills/approve": value = ["ok": true, "skill_id": "draft", "registered": true]
        case "/api/skills/reject": value = ["ok": true, "skill_id": "draft", "rejected": true]
        case "/api/tool-genesis/approve": value = ["success": true, "promoted": true, "skill_id": "tool", "reloaded": Self.promotedLive]
        case "/api/tool-genesis/reject": value = ["success": true, "rejected": "tool"]
        case "/api/apps/app": value = ["success": true]
        case "/api/marketplace/uninstall/installed": value = ["success": true]
        default: value = ["error": "unexpected request"]
        }
        if path == Self.failPath { value = ["ok": false, "error": "fixture refusal"] }
        Self.hook?(); Self.hook = nil
        let bytesOut = try! JSONSerialization.data(withJSONObject:value)
        let deliver = {
            self.client?.urlProtocol(self, didReceive: HTTPURLResponse(url: self.request.url!, statusCode: 200, httpVersion: nil, headerFields: [:])!, cacheStoragePolicy: .notAllowed)
            self.client?.urlProtocol(self, didLoad:bytesOut);self.client?.urlProtocolDidFinishLoading(self)
        }
        if path == Self.delayPath {Self.delayPath = nil;Self.delayed = true;DispatchQueue.global().asyncAfter(deadline:.now()+0.1,execute:deliver)} else {deliver()}
    }
    override func stopLoading() {}
}
@main private struct NativeCapabilitiesFeatureTests {
    @MainActor static func main() async throws {
        var count = 0
        func check(_ condition: Bool, _ message: String) { precondition(condition, message); count += 1 }
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [CapabilitiesWire.self]; let session = URLSession(configuration: config)
        let url = URL(string: "http://127.0.0.1:19999")!
        let model = NativeCapabilitiesModel(baseURL: url, session: session)
        await model.refresh()
        check(CapabilitiesWire.calls.count == 8, "seven supported local inventories plus actual Forge statistics")
        check(!CapabilitiesWire.calls.contains { $0.0 == "/api/marketplace/catalog" || $0.1 != "GET" }, "no automatic remote registry or mutation")
        check(model.rows["skills"]?.first?.key == "fixture", "loaded skill array parsed")
        check(model.rows["skillDrafts"]?.count == 1 && model.rows["toolDrafts"]?.count == 1, "separate draft identities")
        check(model.rows["apps"]?.first?.raw["missing_skill_dependencies"] != nil, "missing app dependencies preserved")
        CapabilitiesWire.failPath = "/skills"; await model.refresh()
        check(model.rows["skills"] == nil && model.errors["skills"] != nil, "failed list is not empty success")
        CapabilitiesWire.failPath = nil; await model.refresh()
        let reload = try model.review(.reload("fixture"))
        check(reload.explanation.contains("immediately") && reload.explanation.contains("cron"),"reload discloses immediate imports and background cron arming")
        CapabilitiesWire.skill["description"] = "Changed after review"
        let before = CapabilitiesWire.calls.count
        check(await model.perform(reload) == false, "changed manifest review refused")
        check(!CapabilitiesWire.calls.dropFirst(before).contains { $0.1 == "POST" }, "no reload after stale preflight")
        let freshReload = try model.review(.reload("fixture"))
        check(await model.perform(freshReload), "reviewed exact reload")
        check(model.receipt?.contains("live reload") == true, "reload receipt is bounded")
        check(await model.perform(freshReload) == false, "review single-use")
        let approve = try model.review(.decision(source: "skillDrafts", id: "draft", approve: true))
        check(approve.explanation.contains("immediately") && approve.explanation.contains("cron") && !approve.explanation.contains("not executed by this button"),"approval discloses actual registration side effects")
        check(await model.perform(approve), "skill-generator approval")
        check(CapabilitiesWire.calls.contains { $0.0 == "/api/skills/approve" && $0.2["skill_id"] as? String == "draft" && $0.2["tool_id"] == nil }, "exact skill source body")
        let reject = try model.review(.decision(source: "toolDrafts", id: "tool", approve: false))
        check(await model.perform(reject), "tool rejection")
        check(CapabilitiesWire.calls.contains { $0.0 == "/api/tool-genesis/reject" && $0.2["tool_id"] as? String == "tool" }, "exact Tool Genesis route")
        let toolApprove = try model.review(.decision(source: "toolDrafts", id: "tool", approve: true))
        check(await model.perform(toolApprove), "persisted tool promotion")
        check(model.receipt?.contains("live registry reload was not confirmed") == true, "promoted is not assumed live")
        let browse = try model.review(.browse(kind: "skill", query: "explicit fixture query"))
        check(browse.explanation.contains("explicit fixture query"), "remote query disclosed before request")
        check(await model.perform(browse), "explicit registry metadata read")
        check(model.rows["catalogue"]?.count == 1 && model.install == nil, "metadata not install consent")
        do { _ = try model.review(.preview(kind: "app", id: "remote")); check(false, "wrong catalogue kind blocked") } catch { check(true, "wrong catalogue kind blocked") }
        CapabilitiesWire.missingSignature = true
        let unsigned = try model.review(.preview(kind: "skill", id: "remote"))
        let unsignedResult = await model.perform(unsigned); check(!unsignedResult && model.install == nil, "unsigned preview blocked")
        CapabilitiesWire.missingSignature = false; CapabilitiesWire.mismatchedPreview = true
        let mismatch = try model.review(.preview(kind: "skill", id: "remote"))
        let mismatchResult = await model.perform(mismatch); check(!mismatchResult && model.install == nil, "preview identity mismatch blocked")
        CapabilitiesWire.mismatchedPreview = false
        let preview = try model.review(.preview(kind: "skill", id: "remote"))
        check(await model.perform(preview), "signed permission preview")
        let consent = try model.review(.install)
        check(consent.explanation.contains("Existing versions may be replaced"), "replacement scope disclosed")
        check(await model.perform(consent), "token-bound verified install")
        check(CapabilitiesWire.calls.contains { $0.0 == "/api/marketplace/install" && $0.2["install_token"] as? String == "opaque-fixture-token" && $0.2["kind"] as? String == "skill" && $0.2["id"] as? String == "remote" && $0.2["source_url"] == nil }, "no unverified install bypass")
        let repeated = await model.perform(consent); check(model.install == nil && !repeated, "consumed token not retried")
        let appBrowse = try model.review(.browse(kind: "app", query: "")); _ = await model.perform(appBrowse)
        CapabilitiesWire.malformedDependencies = true
        let badApp = try model.review(.preview(kind: "app", id: "remote"))
        check(await model.perform(badApp) == false, "incomplete app dependency review blocked")
        CapabilitiesWire.malformedDependencies = false
        let appPreview = try model.review(.preview(kind: "app", id: "remote"))
        check(await model.perform(appPreview), "registry reference may differ from manifest app ID")
        let appConsent = try model.review(.install); CapabilitiesWire.degraded = true
        let appResult = await model.perform(appConsent); check(appResult && model.receipt?.contains("unavailable dependencies") == true, "degraded app install truthful")
        let remove = try model.review(.uninstall(source: "apps", id: "app"))
        check(await model.perform(remove), "reviewed installed app deletion")
        for (path,action) in [("/skills",NativeCapabilityAction.reload("fixture")),("/api/skills/pending",NativeCapabilityAction.decision(source:"skillDrafts",id:"draft",approve:true)),("/api/apps",NativeCapabilityAction.uninstall(source:"apps",id:"app"))] {
            model.configure(baseURL:url);await model.refresh();let held = try model.review(action)
            CapabilitiesWire.delayPath = path;CapabilitiesWire.delayed = false
            let pending = Task {await model.perform(held)}
            for _ in 0..<1000 {if CapabilitiesWire.delayed {break};try await Task.sleep(nanoseconds:1_000_000)}
            check(CapabilitiesWire.delayed,"delayed same-row preflight reached")
            let mutations = CapabilitiesWire.calls.filter {$0.1 != "GET"}.count
            model.configure(baseURL:URL(string:"http://127.0.0.1:20000")!)
            check(!(await pending.value) && CapabilitiesWire.calls.filter {$0.1 != "GET"}.count == mutations && model.rows.isEmpty && model.receipt == nil && !model.busy,"old preflight cannot dispatch or publish after backend switch")
        }
        model.configure(baseURL:url);await model.refresh()
        CapabilitiesWire.delayPath = "/api/marketplace/catalog";CapabilitiesWire.delayed = false
        let delayedBrowse = try model.review(.browse(kind:"skill",query:"fixture")),browseTask = Task {await model.perform(delayedBrowse)}
        for _ in 0..<1000 {if CapabilitiesWire.delayed {break};try await Task.sleep(nanoseconds:1_000_000)}
        model.configure(baseURL:nil)
        check(!(await browseTask.value) && model.rows["catalogue"] == nil && model.receipt == nil,"late catalogue cannot publish into disconnected model")
        model.configure(baseURL:url);await model.refresh();_ = await model.perform(try model.review(.browse(kind:"skill",query:"fixture")))
        let previewHeld = try model.review(.preview(kind:"skill",id:"remote"))
        CapabilitiesWire.delayPath = "/api/marketplace/preview";CapabilitiesWire.delayed = false
        let previewTask = Task {await model.perform(previewHeld)}
        for _ in 0..<1000 {if CapabilitiesWire.delayed {break};try await Task.sleep(nanoseconds:1_000_000)}
        model.configure(baseURL:nil)
        check(!(await previewTask.value) && model.install == nil && model.receipt == nil,"late preview cannot mint an install approval on changed backend")
        model.configure(baseURL:url);await model.refresh()
        let old = try model.review(.reload("fixture")); model.configure(baseURL: nil)
        let untouched = CapabilitiesWire.calls.count
        let oldResult = await model.perform(old); check(!oldResult && CapabilitiesWire.calls.count == untouched, "disconnected generation refuses writes")
        let foreign = NativeCapabilitiesModel(baseURL: URL(string: "http://example.com")!, session: session); await foreign.refresh()
        check(CapabilitiesWire.calls.count == untouched && foreign.errors["skills"] != nil, "nonloopback URL refused before network")
        let credential = NativeCapabilitiesModel(baseURL: URL(string: "http://user:pass@127.0.0.1")!, session: session); await credential.refresh()
        check(CapabilitiesWire.calls.count == untouched, "URL credentials refused")
        try await forgeChecks(session:session,url:url,check:check)
        print("NativeCapabilitiesFeatureTests PASS \(count) assertions")
    }
    @MainActor static func forgeChecks(session:URLSession,url:URL,check:(Bool,String) -> Void) async throws {
        func reset() {
            CapabilitiesWire.calls = [];CapabilitiesWire.generated = [];CapabilitiesWire.additionalPending = [];CapabilitiesWire.proposals = [CapabilitiesWire.proposal];CapabilitiesWire.failPath = nil;CapabilitiesWire.hook = nil;CapabilitiesWire.forgeResponse = nil;CapabilitiesWire.missingPending = false;CapabilitiesWire.wrongPendingPreview = false
            CapabilitiesWire.stats = ["sequences_tracked":1,"proposals_ready":1,"tools_generated":0,"total_uses":0]
        }
        func mutations() -> Int { CapabilitiesWire.calls.filter {$0.1 != "GET"}.count }
        reset()
        var clock = Date(timeIntervalSince1970:1_000)
        let model = NativeCapabilitiesModel(baseURL:url,session:session,now:{clock});await model.refresh()
        check(model.rows["proposals"]?.first?.key == "0123456789ab" && model.forgeStats?["proposals_ready"] == 1,"actual sequence and exact measured stats parsed")
        check(NativeCapabilitiesModel.intentToolID("Read a synthetic note") == "genesis_intent_c06ef59b4d04","intent deterministic ID matches Python source")
        check(NativeCapabilitiesModel.intentToolID(String(repeating:"x",count:199)+"e\u{0301}") == "genesis_intent_868e5743984c","first 200 Python code points, not Swift grapheme clusters")
        for stats:Any in [[:],[],["sequences_tracked":true],["sequences_tracked":1.5]] {
            CapabilitiesWire.stats = stats;await model.refresh()
            check(model.forgeStats?.isEmpty == true || (model.forgeStats == nil && model.errors["stats"] != nil),"uninitialized or malformed statistics never invent measured zeroes")
            model.forgeIntent = "Read a synthetic note"
            do { _ = try model.review(.propose(intent:model.forgeIntent));check(false,"unavailable/malformed stats must block drafting") } catch { check(true,"unavailable/malformed stats block drafting") }
        }
        reset();await model.refresh();model.forgeIntent = "Read a synthetic note"
        let input = try model.review(.propose(intent:model.forgeIntent))
        check(input.explanation.contains(model.forgeIntent) && input.explanation.contains("does not sandbox-run") && input.explanation.contains("atomic"),"exact intent and actual generation limits disclosed")
        model.forgeIntent += " changed";let before = mutations()
        check(!(await model.perform(input)) && mutations() == before,"intent drift refuses before POST")
        model.forgeIntent = "Read a synthetic note"
        let expired = try model.review(.propose(intent:model.forgeIntent));clock = clock.addingTimeInterval(121)
        check(!(await model.perform(expired)) && mutations() == before,"expired review has no draft POST")
        let submitted = try model.review(.propose(intent:model.forgeIntent))
        check(await model.perform(submitted),"reviewed intent drafts and verifies actual pending/inventory")
        check(model.forgeOutcome?.state == "pending" && model.forgeOutcome?.toolID == "genesis_intent_c06ef59b4d04" && model.forgeOutcome?.preview == CapabilitiesWire.code,"exact generated ID and returned code preview retained")
        check(CapabilitiesWire.calls.contains { $0.0 == "/api/tool-genesis/propose" && $0.2.count == 1 && $0.2["intent"] as? String == "Read a synthetic note" },"only exact supported intent body sent")
        check(!CapabilitiesWire.calls.contains { ["/api/tool-genesis/approve","/api/tool-genesis/execute","/api/skills/approve"].contains($0.0) },"generation never approves/promotes/executes")
        check(!(await model.perform(submitted)),"generation review single use")
        do { _ = try model.review(.propose(intent:model.forgeIntent));check(false,"existing deterministic record must block replacement") } catch { check(true,"known pending/generated target blocks replacement") }
        reset();await model.refresh()
        let sequence = try model.review(.generate(sequenceID:"0123456789ab"))
        CapabilitiesWire.proposals[0]["seen_count"] = 4
        check(!(await model.perform(sequence)) && mutations() == 0,"changed proposal terms refuse generation")
        reset();await model.refresh();let missing = try model.review(.generate(sequenceID:"0123456789ab"));CapabilitiesWire.proposals = []
        check(!(await model.perform(missing)) && mutations() == 0,"missing sequence refuses generation")
        do { _ = try model.review(.generate(sequenceID:"foreign"));check(false,"foreign sequence must fail") } catch { check(true,"foreign sequence blocked") }
        reset();await model.refresh()
        let fresh = try model.review(.generate(sequenceID:"0123456789ab"))
        check(await model.perform(fresh),"exact sequence generation and 400/800-character preview readback")
        check(CapabilitiesWire.calls.contains {$0.0 == "/api/tool-genesis/generate" && $0.2.count == 1 && $0.2["sequence_id"] as? String == "0123456789ab"},"generate sends sequence_id, never freeform prompt")
        for malformed:[String:Any] in [["success":true,"tool_id":"foreign","preview":"code"],["success":1,"tool_id":"genesis_intent_c06ef59b4d04","preview":"code"],["success":true,"tool_id":"genesis_intent_c06ef59b4d04"],["success":false,"error":"generation_failed"]] {
            reset();let isolated = NativeCapabilitiesModel(baseURL:url,session:session);await isolated.refresh();isolated.forgeIntent = "Read a synthetic note";CapabilitiesWire.forgeResponse = malformed
            let held = try isolated.review(.propose(intent:isolated.forgeIntent))
            check(!(await isolated.perform(held)) && isolated.forgeOutcome?.state == "outcome_unknown" && mutations() == 1,"malformed/refused POST outcome remains uncertain and never retries")
        }
        for mismatch in ["preview","pending"] {
            reset();let isolated = NativeCapabilitiesModel(baseURL:url,session:session);await isolated.refresh();isolated.forgeIntent = "Read a synthetic note";CapabilitiesWire.wrongPendingPreview = mismatch == "preview";CapabilitiesWire.missingPending = mismatch == "pending"
            check(!(await isolated.perform(try isolated.review(.propose(intent:isolated.forgeIntent)))) && isolated.forgeOutcome?.state == "outcome_unknown" && isolated.receipt == nil,"missing/mismatched pending readback cannot claim generated pending outcome")
        }
        reset();await model.refresh();model.forgeIntent = "Uncertain disposable request";CapabilitiesWire.failPath = "/api/tool-genesis/propose"
        let uncertain = try model.review(.propose(intent:model.forgeIntent))
        check(!(await model.perform(uncertain)) && model.forgeOutcome?.state == "outcome_unknown","lost/refused draft result cannot imply no effect")
        CapabilitiesWire.failPath = nil;CapabilitiesWire.generated = [];CapabilitiesWire.additionalPending = [];await model.refresh()
        do { _ = try model.review(.propose(intent:model.forgeIntent));check(false,"absent inventory cannot release uncertain same-ID request") } catch { check(true,"uncertain same-ID request remains blocked without automatic replay") }
        reset();model.configure(baseURL:nil);model.configure(baseURL:url);await model.refresh();model.forgeIntent = "Late disposable draft"
        let late = try model.review(.propose(intent:model.forgeIntent));CapabilitiesWire.delayPath = "/api/tool-genesis/propose";CapabilitiesWire.delayed = false
        let pending = Task { await model.perform(late) }
        for _ in 0..<1000 {if CapabilitiesWire.delayed {break};try await Task.sleep(nanoseconds:1_000_000)}
        model.configure(baseURL:nil)
        check(!(await pending.value) && model.forgeOutcome == nil && model.receipt == nil,"late draft cannot publish into a disconnected generation")
        CapabilitiesWire.generated = [];CapabilitiesWire.additionalPending = [];model.configure(baseURL:url);await model.refresh();model.forgeIntent = "Late disposable draft"
        do { _ = try model.review(.propose(intent:model.forgeIntent));check(false,"connection drift cannot release uncertain same-ID draft") } catch { check(true,"uncertain post retains same-ID refusal across connection drift") }
        reset();let freshModel = NativeCapabilitiesModel(baseURL:url,session:session,now:{clock});await freshModel.refresh()
        for invalid in [" \n\t",String(repeating:"é",count:4001)] {
            freshModel.forgeIntent = invalid
            do { _ = try freshModel.review(.propose(intent:invalid));check(false,"invalid intent must refuse") } catch { check(true,"empty or over-byte-bound intent refuses without POST") }
        }
        freshModel.forgeIntent = "Distinct fresh preflight fixture"
        let collision = try freshModel.review(.propose(intent:freshModel.forgeIntent)),collisionID = NativeCapabilitiesModel.intentToolID(freshModel.forgeIntent)
        CapabilitiesWire.generated = [["tool_id":collisionID]]
        check(!(await freshModel.perform(collision)) && mutations() == 0,"new deterministic target appearing during preflight blocks POST")
        reset();await freshModel.refresh();freshModel.forgeIntent = "Fresh expired during preflight"
        let expiring = try freshModel.review(.propose(intent:freshModel.forgeIntent));CapabilitiesWire.delayPath = "/api/tool-genesis/list";CapabilitiesWire.delayed = false
        let preflight = Task { await freshModel.perform(expiring) }
        for _ in 0..<1000 {if CapabilitiesWire.delayed {break};try await Task.sleep(nanoseconds:1_000_000)}
        clock = clock.addingTimeInterval(121)
        check(!(await preflight.value) && mutations() == 0,"expiry while awaiting fresh inventory prevents POST")
        reset();await freshModel.refresh();freshModel.forgeIntent = "Fresh input drift fixture"
        let drifting = try freshModel.review(.propose(intent:freshModel.forgeIntent));CapabilitiesWire.delayPath = "/api/tool-genesis/list";CapabilitiesWire.delayed = false
        let driftingTask = Task { await freshModel.perform(drifting) }
        for _ in 0..<1000 {if CapabilitiesWire.delayed {break};try await Task.sleep(nanoseconds:1_000_000)}
        freshModel.forgeIntent += " modified"
        check(!(await driftingTask.value) && mutations() == 0,"intent drift during fresh preflight prevents POST")
        reset();CapabilitiesWire.proposals = [CapabilitiesWire.proposal,CapabilitiesWire.proposal];await freshModel.refresh()
        check(freshModel.rows["proposals"] == nil && freshModel.errors["proposals"] != nil,"duplicated sequence identities are not accepted as valid proposals")
    }
}
