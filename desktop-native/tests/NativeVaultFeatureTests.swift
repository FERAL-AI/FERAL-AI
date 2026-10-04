import Foundation

private final class VaultFixture:URLProtocol {
    static var requests:[URLRequest]=[]
    static var state:[String:Any]=status()
    static var token="fixture-review-01234567890123456789"
    static var nextUnlock:[String:Any]?
    static var rejectUnlock=false
    static var delayUnlock=false
    static var foreignResponse=false
    static var bootstrap:[String:Any]=bootstrapStatus()
    static var nextBootstrap:[String:Any]?
    static var delayBootstrap=false
    static var rejectBootstrap=false
    static let bootstrapToken=String(repeating:"A",count:43)
    static func bootstrapStatus(_ phase:String="pending",pending:Bool=false,restart:Bool=false,credentials:Bool=true)->[String:Any]{["supported":true,"phase":phase,"in_flight":pending,"restart_required":restart,"credentials_available":credentials,"memory_available":true,"orchestrator_available":phase=="ready","bootstrap_required":phase != "ready","agent_ready":phase=="ready" && !restart && credentials]}
    static func status(_ state:String="locked",pending:Bool=false)->[String:Any]{["state":state,"code":state=="ready" ? "ready" : pending ? "unlock_pending" : "unlock_required","message":"private-system-canary","in_flight":pending,"credentials_available":state=="ready","unlock_supported":true,"initialization_supported":false,"memory_available":false,"bootstrap_required":true,"sync_dormant":true]}
    override class func canInit(with request:URLRequest)->Bool{true}
    override class func canonicalRequest(for request:URLRequest)->URLRequest{request}
    static func body(_ request:URLRequest)->[String:Any]{
        if let data=request.httpBody{return (try? JSONSerialization.jsonObject(with:data)) as? [String:Any] ?? [:]}
        guard let stream=request.httpBodyStream else{return [:]};stream.open();defer{stream.close()};var data=Data(),buffer=[UInt8](repeating:0,count:1024)
        while stream.hasBytesAvailable{let n=stream.read(&buffer,maxLength:buffer.count);if n<=0{break};data.append(buffer,count:n)}
        return (try? JSONSerialization.jsonObject(with:data)) as? [String:Any] ?? [:]
    }
    override func startLoading(){
        var captured=request;if captured.httpMethod=="POST"{captured.httpBody=try! JSONSerialization.data(withJSONObject:Self.body(request))};Self.requests.append(captured)
        let path=request.url!.path;let value:[String:Any];let code:Int
        if path=="/api/security/vault/status"{value=Self.state;code=200}
        else if path=="/api/security/vault/unlock/review"{value=["review_token":Self.token,"expires_in_seconds":300,"scope":"Explicit existing-key OS access and authenticated local credential activation","previous_state":Self.state["state"]!,"status":Self.state];code=200}
        else if path=="/api/security/vault/unlock/cancel"{value=["ok":true];code=200}
        else if path=="/api/security/vault/unlock"{
            code=Self.rejectUnlock ? 409 : 200
            value=Self.rejectUnlock ? ["detail":["code":"private-service-canary","message":"private-system-canary"]] : Self.nextUnlock ?? ["ok":true,"operation":"unlock","review_token":Self.token,"status":Self.status("ready")]
        }else if path=="/api/security/agent-bootstrap/status"{value=Self.bootstrap;code=200}
        else if path=="/api/security/agent-bootstrap/review"{value=["review_token":Self.bootstrapToken,"expires_in_seconds":300,"scope":"Resume configured providers, integrations and background jobs with possible network and local writes; partial failures require restart.","previous_status":Self.bootstrap];code=200}
        else if path=="/api/security/agent-bootstrap/cancel"{value=["ok":true];code=200}
        else if path=="/api/security/agent-bootstrap"{code=Self.rejectBootstrap ? 409 : 200;value=Self.rejectBootstrap ? ["detail":"private-bootstrap-canary"] : Self.nextBootstrap ?? ["ok":true,"operation":"bootstrap","review_token":Self.bootstrapToken,"status":Self.bootstrapStatus("ready")]}
        else{code=404;value=["detail":"private-service-canary"]}
        let data=try! JSONSerialization.data(withJSONObject:value),response=HTTPURLResponse(url:Self.foreignResponse ? URL(string:"http://127.0.0.1:9465/api/security/vault/status")! : request.url!,statusCode:code,httpVersion:nil,headerFields:nil)!
        let deliver={self.client?.urlProtocol(self,didReceive:response,cacheStoragePolicy:.notAllowed);self.client?.urlProtocol(self,didLoad:data);self.client?.urlProtocolDidFinishLoading(self)}
        if (path=="/api/security/vault/unlock" && Self.delayUnlock) || (path=="/api/security/agent-bootstrap" && Self.delayBootstrap){DispatchQueue.global().asyncAfter(deadline:.now()+0.1,execute:deliver)}else{deliver()}
    }
    override func stopLoading(){}
}
@main struct NativeVaultFeatureTests {
    static var count=0
    @MainActor static func check(_ condition:Bool,_ name:String){guard condition else{fatalError("FAIL: "+name)};count+=1}
    @MainActor static func reject(_ action:()throws->Void,_ name:String){do{try action();fatalError("Accepted: "+name)}catch{count+=1}}
    static var unlocks:Int{VaultFixture.requests.filter{$0.url?.path=="/api/security/vault/unlock"}.count}
    static var bootstraps:Int{VaultFixture.requests.filter{$0.url?.path=="/api/security/agent-bootstrap"}.count}
    @MainActor static func main()async throws {
        var clock=1000.0
        let config=URLSessionConfiguration.ephemeral;config.protocolClasses=[VaultFixture.self]
        let model=NativeVaultModel(session:URLSession(configuration:config),uptime:{clock}),base=URL(string:"http://127.0.0.1:9464")!
        model.configure(baseURL:base);await model.refresh()
        check(model.status?.state=="locked" && model.status?.credentialsAvailable==false && model.error==nil,"passive exact status parsed without invented credential readiness")
        check(VaultFixture.requests.count==1 && VaultFixture.requests[0].httpMethod=="GET" && VaultFixture.requests[0].url?.path=="/api/security/vault/status" && unlocks==0,"ambient refresh cannot request OS access")
        VaultFixture.foreignResponse=true;await model.refresh()
        check(model.error != nil && model.status?.credentialsAvailable==false && unlocks==0,"foreign-origin response cannot establish vault authority")
        VaultFixture.foreignResponse=false;await model.refresh()
        let review=await model.prepareReview()!
        check(unlocks==0 && review.explanation.contains("existing Keychain") && review.explanation.contains("may continue") && !review.explanation.contains("private-system-canary"),"server-token scope review is explicit and private messages withheld")
        await model.confirm(review)
        check(unlocks==1 && model.status?.credentialsAvailable==true && model.status?.bootstrapRequired==true && model.receipt?.contains("separate status")==true,"matching review receipt exposes credentials without claiming full agent or provider readiness")
        let unlockBody=VaultFixture.body(VaultFixture.requests.first{$0.url?.path=="/api/security/vault/unlock"}!)
        check(unlockBody.count==1 && unlockBody["review_token"] as? String==VaultFixture.token,"unlock sends only owning server review token")
        await model.confirm(review);check(unlocks==1,"unlock review is one-use")
        VaultFixture.state=VaultFixture.status();await model.refresh();let stale=await model.prepareReview()!
        VaultFixture.state=VaultFixture.status("unlocking",pending:true);await model.confirm(stale)
        check(unlocks==1 && model.status?.inFlight==true && model.error?.contains("No unlock was sent") == true,"fresh preflight sees another in-flight operation and blocks OS retry")
        check(await model.prepareReview()==nil && unlocks==1,"in-flight status cannot prepare another unlock review")
        VaultFixture.state=VaultFixture.status();await model.refresh();let expired=await model.prepareReview()!;clock=1400;await model.confirm(expired)
        check(unlocks==1 && !model.canUse(expired),"local expiry rejects review before HTTP unlock")
        clock=1500;let cancelled=await model.prepareReview()!;await model.cancel(cancelled);await model.confirm(cancelled)
        check(unlocks==1 && VaultFixture.requests.contains{$0.url?.path=="/api/security/vault/unlock/cancel"} && model.receipt?.contains("does not cancel an OS operation")==true,"cancel revokes only an unused review, cannot replay unlock")
        let pending=await model.prepareReview()!
        VaultFixture.nextUnlock=["ok":false,"operation":"unlock","review_token":VaultFixture.token,"status":VaultFixture.status("unlocking",pending:true)]
        await model.confirm(pending)
        check(model.status?.inFlight==true && model.receipt?.contains("original unlock is still running")==true && !model.busy,"bounded backend wait reports continuing operation rather than false failure")
        let countBeforeRefresh=unlocks;VaultFixture.state=VaultFixture.status("unlocking",pending:true);await model.refresh()
        check(unlocks==countBeforeRefresh,"status after bounded wait never retries OS unlock")
        VaultFixture.state=VaultFixture.status();await model.refresh();let mismatch=await model.prepareReview()!
        VaultFixture.nextUnlock=["ok":true,"operation":"unlock","review_token":"wrong-token","status":VaultFixture.status("ready")];await model.confirm(mismatch)
        check(model.status?.credentialsAvailable==false && model.error?.contains("may still be running")==true,"mismatched receipt cannot fabricate readiness or retry review")
        let denied=await model.prepareReview()!;VaultFixture.rejectUnlock=true;await model.confirm(denied)
        check(model.error?.contains("private-service-canary")==false && model.error?.contains("private-system-canary")==false && !model.busy,"stale server rejection remains private and unlock outcome unconfirmed")
        VaultFixture.rejectUnlock=false;VaultFixture.nextUnlock=nil
        let delayed=await model.prepareReview()!;VaultFixture.delayUnlock=true
        let task=Task{await model.confirm(delayed)}
        for _ in 0..<1000{if model.busy && unlocks>countBeforeRefresh+2{break};try await Task.sleep(nanoseconds:1_000_000)}
        model.configure(baseURL:nil);await task.value
        check(model.status==nil && model.receipt==nil && !model.busy,"origin change revokes late unlock receipt without claiming cancellation of OS worker")
        reject({_ = try NativeVaultWire.url(URL(string:"https://attacker.example")!,path:"/api/security/vault/status")},"remote vault endpoint blocked before network")
        var unsupported=VaultFixture.status();unsupported["initialization_supported"]=true
        reject({_ = try NativeVaultWire.status(unsupported)},"unsupported initialization contract cannot expose setup/reset controls")
        unsupported=VaultFixture.status();unsupported["credentials_available"]=1
        reject({_ = try NativeVaultWire.status(unsupported)},"numeric readiness is not a Boolean")
        unsupported=VaultFixture.status();unsupported["credentials_available"]=true
        reject({_ = try NativeVaultWire.status(unsupported)},"contradictory locked credentials fail closed")
        let continuation=NativeAgentBootstrapModel(session:URLSession(configuration:config),uptime:{clock})
        continuation.configure(baseURL:base);await continuation.refresh()
        check(continuation.status?.canReview==true && bootstraps==0,"passive continuation refresh cannot start agent jobs or unlock")
        let startup=await continuation.prepareReview()!
        check(bootstraps==0 && startup.explanation.contains("separate action") && startup.explanation.contains("process restart"),"startup is a separate reviewed effect with partial failure disclosure")
        await continuation.confirm(startup)
        check(bootstraps==1 && continuation.status?.agentReady==true && continuation.status?.bootstrapRequired==false,"matched startup receipt exposes exact backend agent readiness")
        let sentBody=VaultFixture.body(VaultFixture.requests.first{$0.url?.path=="/api/security/agent-bootstrap"}!)
        check(sentBody.count==1 && sentBody["review_token"] as? String==VaultFixture.bootstrapToken,"continuation sends only reviewed server token")
        await continuation.confirm(startup);check(bootstraps==1,"startup review is one-use")
        VaultFixture.bootstrap=VaultFixture.bootstrapStatus();await continuation.refresh()
        let changed=await continuation.prepareReview()!
        VaultFixture.bootstrap=VaultFixture.bootstrapStatus("starting",pending:true)
        await continuation.confirm(changed)
        check(bootstraps==1 && continuation.status?.inFlight==true,"fresh preflight blocks startup after another operation begins")
        check(await continuation.prepareReview()==nil,"pending startup cannot prepare another review")
        VaultFixture.bootstrap=VaultFixture.bootstrapStatus();await continuation.refresh()
        let expiredStartup=await continuation.prepareReview()!;clock+=301
        await continuation.confirm(expiredStartup)
        check(bootstraps==1 && !continuation.canUse(expiredStartup),"expired continuation review never dispatches")
        let cancelledStartup=await continuation.prepareReview()!
        await continuation.cancel(cancelledStartup);await continuation.confirm(cancelledStartup)
        check(bootstraps==1 && continuation.receipt?.contains("does not stop")==true,"cancel revokes only unused startup review")
        let pendingStartup=await continuation.prepareReview()!
        VaultFixture.nextBootstrap=["ok":false,"operation":"bootstrap","review_token":VaultFixture.bootstrapToken,"status":VaultFixture.bootstrapStatus("starting",pending:true)]
        await continuation.confirm(pendingStartup)
        check(continuation.status?.inFlight==true && continuation.receipt?.contains("original continuation is still running")==true && !continuation.busy,"bounded wait reports original startup continuing without retry")
        let dispatchedCount=bootstraps
        VaultFixture.bootstrap=VaultFixture.bootstrapStatus("starting",pending:true);await continuation.refresh()
        check(bootstraps==dispatchedCount,"passive polling never dispatches continuation")
        VaultFixture.bootstrap=VaultFixture.bootstrapStatus();await continuation.refresh()
        let failedStartup=await continuation.prepareReview()!
        VaultFixture.nextBootstrap=["ok":false,"operation":"bootstrap","review_token":VaultFixture.bootstrapToken,"status":VaultFixture.bootstrapStatus("failed",restart:true)]
        await continuation.confirm(failedStartup)
        check(continuation.status?.restartRequired==true && continuation.receipt?.contains("process restart")==true,"partial startup truthfully requires restart")
        check(await continuation.prepareReview()==nil,"restart-required state cannot retry startup")
        VaultFixture.bootstrap=VaultFixture.bootstrapStatus();await continuation.refresh()
        check(await continuation.prepareReview()==nil && continuation.restartRequired && !continuation.canPrepareReview,"restart refusal is sticky even if a later same-origin response regresses")
        continuation.configure(baseURL:nil);continuation.configure(baseURL:base);await continuation.refresh()
        let wrongStartup=await continuation.prepareReview()!
        VaultFixture.nextBootstrap=["ok":true,"operation":"unlock","review_token":VaultFixture.bootstrapToken,"status":VaultFixture.bootstrapStatus("ready")]
        await continuation.confirm(wrongStartup)
        check(continuation.status?.agentReady==false && continuation.error?.contains("Do not repeat")==true,"unlock receipt cannot impersonate startup acknowledgement")
        let wrongReady=await continuation.prepareReview()!
        VaultFixture.nextBootstrap=["ok":false,"operation":"bootstrap","review_token":VaultFixture.bootstrapToken,"status":VaultFixture.bootstrapStatus("ready")]
        await continuation.confirm(wrongReady)
        check(continuation.status?.agentReady==false && continuation.error?.contains("inconsistent")==true,"inconsistent receipt cannot fabricate agent readiness")
        let deniedStartup=await continuation.prepareReview()!;VaultFixture.rejectBootstrap=true
        await continuation.confirm(deniedStartup)
        check(continuation.error?.contains("private-bootstrap-canary")==false && !continuation.busy,"private startup server rejection is withheld")
        VaultFixture.rejectBootstrap=false;VaultFixture.nextBootstrap=nil
        VaultFixture.bootstrap=VaultFixture.bootstrapStatus(credentials:false);await continuation.refresh()
        let noCredentialsCount=bootstraps
        check(await continuation.prepareReview()==nil && bootstraps==noCredentialsCount,"no credentials prevents bootstrap review without auto-unlock")
        VaultFixture.bootstrap=VaultFixture.bootstrapStatus();await continuation.refresh()
        VaultFixture.delayBootstrap=true
        let lateStartup=await continuation.prepareReview()!
        let late=Task{await continuation.confirm(lateStartup)}
        for _ in 0..<1000{if bootstraps>noCredentialsCount{break};try await Task.sleep(nanoseconds:1_000_000)}
        continuation.configure(baseURL:nil);await late.value
        check(continuation.status==nil && continuation.receipt==nil && !continuation.busy,"disconnect revokes late continuation receipt without claiming backend cancellation")
        var malformed=VaultFixture.bootstrapStatus();malformed["agent_ready"]=1
        reject({_ = try NativeVaultWire.bootstrapStatus(malformed)},"numeric bootstrap readiness rejected")
        malformed=VaultFixture.bootstrapStatus();malformed["agent_ready"]=true
        reject({_ = try NativeVaultWire.bootstrapStatus(malformed)},"partial state cannot claim agent ready")
        malformed=VaultFixture.bootstrapStatus("future-state")
        reject({_ = try NativeVaultWire.bootstrapStatus(malformed)},"unknown continuation state rejected")
        malformed=VaultFixture.bootstrapStatus("ready");malformed["restart_required"]=true
        reject({_ = try NativeVaultWire.bootstrapStatus(malformed)},"restart required cannot coexist with ready")
        print("Native vault: \(count) assertions passed.")
    }
}
