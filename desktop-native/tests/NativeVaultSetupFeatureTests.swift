import Foundation

private final class SetupFixture:URLProtocol {
    static var requests:[URLRequest]=[]
    static let token="12345678-1234-1234-1234-123456789abc"
    static var state=status()
    static var wrongToken=false,foreign=false,deny=false,failReadback=false,delay=false,partial=false,wrongVault=false
    static var initialized=false
    static func status(_ code:String="available",pending:Bool=false)->[String:Any]{["supported":code=="available","code":code,"in_flight":pending,"credential_storage_available":code=="vault_ready","existing_artifacts":code=="vault_ready" || code=="initialization_partial" || code=="vault_artifacts_present","requires_signed_acceptance":code=="release_acceptance_required","local_use_requires_vault":false,"message":"private-status-sentinel"]}
    override class func canInit(with request:URLRequest)->Bool{true}
    override class func canonicalRequest(for request:URLRequest)->URLRequest{request}
    static func body(_ request:URLRequest)->[String:Any]{
        if let data=request.httpBody{return (try? JSONSerialization.jsonObject(with:data)) as? [String:Any] ?? [:]}
        guard let stream=request.httpBodyStream else{return [:]};stream.open();defer{stream.close()};var data=Data(),buffer=[UInt8](repeating:0,count:1024)
        while stream.hasBytesAvailable{let n=stream.read(&buffer,maxLength:buffer.count);if n<=0{break};data.append(buffer,count:n)}
        return (try? JSONSerialization.jsonObject(with:data)) as? [String:Any] ?? [:]
    }
    override func startLoading(){
        var recorded=request;if request.httpMethod=="POST"{recorded.httpBody=try! JSONSerialization.data(withJSONObject:Self.body(request))};Self.requests.append(recorded)
        let path=request.url!.path;var code=200;let raw:[String:Any]
        if path=="/api/security/vault/initialize/status"{if Self.failReadback && Self.initialized{code=500;raw=["detail":"private-status-sentinel"]}else{raw=Self.state}}
        else if path=="/api/security/vault/initialize/review"{raw=["review_token":Self.token,"expires_in_seconds":300,"scope":"Create a fresh encrypted vault; never replace existing keys or files.","previous_status":Self.state]}
        else if path=="/api/security/vault/initialize/cancel"{raw=["ok":true]}
        else if path=="/api/security/vault/initialize"{
            if Self.deny{code=409;raw=["detail":"private-status-sentinel"]}
            else{Self.initialized=true;Self.state=Self.partial ? Self.status("initialization_partial") : Self.status("vault_ready");raw=["ok":!Self.partial,"operation":"initialize","review_token":Self.wrongToken ? UUID().uuidString : Self.token,"status":Self.state,"vault_status":["credentials_available":!Self.partial,"in_flight":false]]}
        }else if path=="/api/security/vault/status"{raw=["state":Self.wrongVault ? "locked" : "ready","credentials_available":!Self.wrongVault,"in_flight":false]}
        else{code=404;raw=["detail":"private-status-sentinel"]}
        let data=try! JSONSerialization.data(withJSONObject:raw),response=HTTPURLResponse(url:Self.foreign ? URL(string:"http://127.0.0.1:9465/")! : request.url!,statusCode:code,httpVersion:nil,headerFields:nil)!
        let deliver={self.client?.urlProtocol(self,didReceive:response,cacheStoragePolicy:.notAllowed);self.client?.urlProtocol(self,didLoad:data);self.client?.urlProtocolDidFinishLoading(self)}
        if Self.delay && path=="/api/security/vault/initialize"{DispatchQueue.global().asyncAfter(deadline:.now()+0.08,execute:deliver)}else{deliver()}
    }
    override func stopLoading(){}
    static func reset(){requests=[];state=status();wrongToken=false;foreign=false;deny=false;failReadback=false;delay=false;partial=false;wrongVault=false;initialized=false}
}
@main struct NativeVaultSetupFeatureTests {
    static var count=0
    @MainActor static func check(_ result:Bool,_ name:String){guard result else{fatalError("FAIL: "+name)};count+=1}
    static var writes:Int{SetupFixture.requests.filter{$0.url?.path=="/api/security/vault/initialize"}.count}
    @MainActor static func main()async throws {
        var clock=1000.0
        let config=URLSessionConfiguration.ephemeral;config.protocolClasses=[SetupFixture.self]
        let model=NativeVaultSetupModel(session:URLSession(configuration:config),uptime:{clock}),base=URL(string:"http://127.0.0.1:9464")!
        model.configure(baseURL:base);await model.refresh()
        check(model.canPrepare && writes==0 && SetupFixture.requests.count==1,"passive state does not initialize")
        let review=await model.prepareReview()!
        check(writes==0 && !review.scope.contains("private-status"),"prepare never dispatches or discloses diagnostics")
        let succeeded=await model.confirm(review)
        check(succeeded && writes==1 && model.status?.credentialsAvailable==true && !model.outcomeUnconfirmed,"matching result plus independent readback establishes storage only")
        check(SetupFixture.requests.contains{$0.url?.path=="/api/security/vault/status"},"separate vault readiness was read")
        check(SetupFixture.body(SetupFixture.requests.first{$0.url?.path=="/api/security/vault/initialize"}!).keys.count==1,"confirm sends only review token")
        let duplicate=await model.confirm(review);check(!duplicate && writes==1,"confirmation cannot replay")
        SetupFixture.reset();await model.refresh();let cancelled=await model.prepareReview()!;await model.cancel(cancelled)
        check(writes==0 && !model.canUse(cancelled),"cancel revokes unused review without key write")
        SetupFixture.reset();await model.refresh();let stale=await model.prepareReview()!;SetupFixture.state=SetupFixture.status("vault_artifacts_present")
        let changed=await model.confirm(stale);check(!changed && writes==0,"appearing artifacts block dispatch")
        SetupFixture.reset();await model.refresh();let expired=await model.prepareReview()!;clock+=301
        let tooLate=await model.confirm(expired);check(!tooLate && writes==0,"expiry blocks dispatch")
        clock=1000;SetupFixture.reset();await model.refresh();let prior=await model.prepareReview()!;model.configure(baseURL:URL(string:"http://127.0.0.1:9466")!)
        let drift=await model.confirm(prior);check(!drift && writes==0,"origin generation drift blocks prior review")
        model.configure(baseURL:base)
        for failure in ["wrongToken","denial","readback","vaultReadback"] {
            SetupFixture.reset();await model.refresh();let item=await model.prepareReview()!
            SetupFixture.wrongToken=failure=="wrongToken";SetupFixture.deny=failure=="denial";SetupFixture.failReadback=failure=="readback";SetupFixture.wrongVault=failure=="vaultReadback"
            let ok=await model.confirm(item)
            check(!ok && writes==1 && model.outcomeUnconfirmed && !model.canPrepare,"unconfirmed "+failure+" cannot claim ready or automatically retry")
            check(model.error?.contains("private-status-sentinel") != true,"private diagnostics withheld for "+failure)
            SetupFixture.failReadback=false;await model.refresh();check(!model.outcomeUnconfirmed,"passive final state permits reconciliation after "+failure)
        }
        SetupFixture.reset();await model.refresh();let partial=await model.prepareReview()!;SetupFixture.partial=true
        let partialOK=await model.confirm(partial);check(!partialOK && model.status?.code=="initialization_partial" && !model.canPrepare,"partial persistence cannot become success or retry")
        for code in ["release_acceptance_required","vault_artifacts_present","key_already_present","platform_unsupported"] {
            SetupFixture.reset();SetupFixture.state=SetupFixture.status(code);await model.refresh();let blocked=await model.prepareReview();check(blocked==nil && writes==0 && !model.canPrepare,"blocked "+code+" remains available only for local use")
        }
        SetupFixture.reset();SetupFixture.state=["supported":false,"code":"initializer_not_configured","can_initialize":false,"configured":false,"storage_inspected":false,"in_flight":false,"credential_storage_available":false,"existing_artifacts":false,"requires_signed_acceptance":true,"local_use_requires_vault":false]
        await model.refresh()
        check(model.status?.code=="initializer_not_configured" && model.error==nil,"optional unavailable service is a readable status")
        let unavailable=await model.prepareReview()
        check(unavailable==nil && !model.canPrepare && writes==0 && SetupFixture.requests.allSatisfy{$0.httpMethod=="GET"},"uninspected unavailable status cannot mint or dispatch initialization review")
        SetupFixture.reset();SetupFixture.foreign=true;await model.refresh();check(model.error != nil && writes==0,"foreign response cannot establish authority")
        SetupFixture.reset();await model.refresh();let old=await model.prepareReview()!;SetupFixture.delay=true
        let task=Task{await model.confirm(old)}
        try await Task.sleep(nanoseconds:20_000_000);model.configure(baseURL:URL(string:"http://127.0.0.1:9467")!)
        let late=await task.value;check(!late && model.receipt==nil && model.status==nil,"late receipt cannot populate replaced connection")
        model.configure(baseURL:base);SetupFixture.reset();await model.refresh();let waitReview=await model.prepareReview()!;SetupFixture.delay=true
        let waiter=Task{await model.confirm(waitReview)}
        try await Task.sleep(nanoseconds:20_000_000);waiter.cancel()
        let cancelledWait=await waiter.value
        check(!cancelledWait && writes==1 && model.outcomeUnconfirmed && !model.canPrepare,"cancelled HTTP wait cannot undo or retry dispatched OS action")
        try await Task.sleep(nanoseconds:100_000_000);await model.refresh()
        check(model.status?.credentialsAvailable==true && !model.outcomeUnconfirmed,"passive refresh reconciles a dispatched operation after cancelled wait")
        print("NativeVaultSetupFeatureTests: \(count) passed")
    }
}
