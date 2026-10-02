import Foundation
import SwiftUI
import CoreFoundation

struct NativeVaultFailure:LocalizedError {
    let message:String
    init(_ message:String){self.message=message}
    var errorDescription:String?{message}
}
struct NativeVaultStatus:Equatable {
    let state:String,code:String
    let inFlight:Bool,credentialsAvailable:Bool,unlockSupported:Bool,memoryAvailable:Bool,bootstrapRequired:Bool,syncDormant:Bool
    var canReview:Bool{unlockSupported && !inFlight && !credentialsAvailable && ["locked","unavailable"].contains(state)}
}
struct NativeVaultReview:Identifiable {
    let id=UUID()
    let token:String,scope:String,origin:URL,generation:UUID
    let status:NativeVaultStatus
    let expiresAtUptime:TimeInterval
    var explanation:String{
        "Service: \(origin.absoluteString)\n\nServer-reviewed scope:\n\(scope)\n\nCurrent state: \(status.state), code \(status.code). Credentials available: \(status.credentialsAvailable). Encrypted-memory working data available: \(status.memoryAvailable). Full agent bootstrap still required: \(status.bootstrapRequired). Sync dormant: \(status.syncDormant).\n\nThis explicit request may ask the operating system for access to the existing Keychain key. It can activate authenticated local credentials and restore an encrypted-memory plaintext working database. It does not reset, create or recover a vault, start federation, or complete a blocked full agent bootstrap. If the HTTP wait ends, is cancelled or times out, an already started OS operation may continue. Another unlock must not be inferred necessary from an unconfirmed response; refresh passive status first."
    }
}
enum NativeVaultWire {
    static func bool(_ value:Any?)->Bool?{guard let n=value as? NSNumber,CFGetTypeID(n)==CFBooleanGetTypeID() else{return nil};return n.boolValue}
    static func number(_ value:Any?)->Double?{guard let n=value as? NSNumber,CFGetTypeID(n) != CFBooleanGetTypeID(),n.doubleValue.isFinite else{return nil};return n.doubleValue}
    static func status(_ raw:[String:Any])throws->NativeVaultStatus {
        guard let state=raw["state"] as? String,["locked","unlocking","unavailable","ready"].contains(state),let code=raw["code"] as? String,code.range(of:"^[a-z][a-z0-9_]{0,63}$",options:.regularExpression) != nil,
              let pending=bool(raw["in_flight"]),let credentials=bool(raw["credentials_available"]),let unlock=bool(raw["unlock_supported"]),bool(raw["initialization_supported"])==false,
              let memory=bool(raw["memory_available"]),let bootstrap=bool(raw["bootstrap_required"]),let dormant=bool(raw["sync_dormant"]),credentials==(state=="ready") else{throw NativeVaultFailure("The service returned unsupported vault status. No unlock was requested.")}
        return NativeVaultStatus(state:state,code:code,inFlight:pending,credentialsAvailable:credentials,unlockSupported:unlock,memoryAvailable:memory,bootstrapRequired:bootstrap,syncDormant:dormant)
    }
    static func url(_ base:URL,path:String)throws->URL {
        guard base.scheme=="http",["127.0.0.1","::1","[::1]"].contains(base.host ?? ""),base.user==nil,base.password==nil,var parts=URLComponents(url:base,resolvingAgainstBaseURL:false) else{throw NativeVaultFailure("Vault controls require the local service.")}
        parts.path=path;parts.query=nil;parts.fragment=nil;guard let url=parts.url else{throw NativeVaultFailure("Invalid vault request URL.")};return url
    }
}
private final class NativeVaultRedirectGuard:NSObject,URLSessionTaskDelegate {
    func urlSession(_ session:URLSession,task:URLSessionTask,willPerformHTTPRedirection response:HTTPURLResponse,newRequest request:URLRequest,completionHandler:@escaping(URLRequest?)->Void){completionHandler(nil)}
    static func session()->URLSession{let config=URLSessionConfiguration.ephemeral;config.timeoutIntervalForRequest=12;config.timeoutIntervalForResource=15;return URLSession(configuration:config,delegate:NativeVaultRedirectGuard(),delegateQueue:nil)}
}
@MainActor final class NativeVaultModel:ObservableObject {
    @Published private(set) var status:NativeVaultStatus?
    @Published private(set) var fetchedAt:Date?
    @Published private(set) var busy=false
    @Published private(set) var error:String?
    @Published private(set) var receipt:String?
    private var baseURL:URL?,generation=UUID(),operation:UUID?
    private var used:Set<UUID>=[]
    private let session:URLSession
    private let uptime:()->TimeInterval
    init(session:URLSession?=nil,uptime:@escaping()->TimeInterval={ProcessInfo.processInfo.systemUptime}){self.session=session ?? NativeVaultRedirectGuard.session();self.uptime=uptime}
    func configure(baseURL:URL?){guard self.baseURL != baseURL else{return};generation=UUID();self.baseURL=baseURL;status=nil;fetchedAt=nil;error=nil;receipt=nil;used=[];busy=operation != nil}
    private func request(_ base:URL,path:String,body:[String:Any]?=nil)async throws->[String:Any] {
        var request=URLRequest(url:try NativeVaultWire.url(base,path:path));request.httpMethod=body==nil ? "GET" : "POST"
        if let body=body{guard JSONSerialization.isValidJSONObject(body) else{throw NativeVaultFailure("Invalid vault request body.")};request.httpBody=try JSONSerialization.data(withJSONObject:body);request.setValue("application/json",forHTTPHeaderField:"Content-Type")}
        let (data,response)=try await session.data(for:request)
        guard data.count<=16384,let http=response as? HTTPURLResponse,http.url==request.url,(200..<300).contains(http.statusCode),let raw=(try? JSONSerialization.jsonObject(with:data)) as? [String:Any],raw["error"]==nil else{throw NativeVaultFailure("The vault request was not acknowledged. Private service details are withheld.")};return raw
    }
    func refresh()async {
        guard !busy else{return};guard let base=baseURL else{error="The local service is not ready.";return}
        let started=generation,op=UUID();operation=op;busy=true
        defer{if operation==op{operation=nil;busy=false}}
        do{let current=try NativeVaultWire.status(try await request(base,path:"/api/security/vault/status"));guard generation==started,!Task.isCancelled else{return};status=current;fetchedAt=Date();error=nil}
        catch{guard generation==started else{return};self.error="Passive vault status could not be refreshed. Any displayed status is from the previous successful read; no OS access was requested."}
    }
    func prepareReview()async->NativeVaultReview? {
        guard !busy,let base=baseURL else{return nil}
        let started=generation,op=UUID();operation=op;busy=true;error=nil;receipt=nil
        defer{if operation==op{operation=nil;busy=false}}
        do{
            let current=try NativeVaultWire.status(try await request(base,path:"/api/security/vault/status"))
            guard generation==started,!Task.isCancelled else{return nil};status=current;fetchedAt=Date()
            guard current.canReview else{throw NativeVaultFailure("Unlock cannot be reviewed in this state. No OS operation was requested; refresh passive status.")}
            let raw=try await request(base,path:"/api/security/vault/unlock/review",body:[:])
            guard generation==started,!Task.isCancelled else{return nil}
            guard let token=raw["review_token"] as? String,token.range(of:"^[A-Za-z0-9_-]{16,256}$",options:.regularExpression) != nil,
                  let seconds=NativeVaultWire.number(raw["expires_in_seconds"]),seconds>0,seconds<=300,let scope=raw["scope"] as? String,!scope.isEmpty,scope.utf8.count<=4096,
                  !scope.unicodeScalars.contains(where:{$0.value<32 && $0.value != 10}),let previous=raw["previous_state"] as? String,let statusRaw=raw["status"] as? [String:Any] else{throw NativeVaultFailure("The unlock review was unsupported. No unlock was sent.")}
            let reviewed=try NativeVaultWire.status(statusRaw)
            guard reviewed==current,previous==reviewed.state,reviewed.canReview else{throw NativeVaultFailure("Vault status changed while preparing the review. Refresh and prepare a new review.")}
            return NativeVaultReview(token:token,scope:scope,origin:base,generation:started,status:reviewed,expiresAtUptime:uptime()+seconds)
        }catch{guard generation==started else{return nil};self.error=(error as? NativeVaultFailure)?.message ?? "Unlock review could not be prepared. Private service details are withheld; no unlock was sent.";return nil}
    }
    func canUse(_ review:NativeVaultReview)->Bool{!busy && baseURL==review.origin && generation==review.generation && !used.contains(review.id) && uptime()<review.expiresAtUptime && status==review.status}
    func confirm(_ review:NativeVaultReview)async {
        guard canUse(review) else{error="This unlock review is stale, expired or already used. No unlock was sent.";return}
        used.insert(review.id);let started=generation,op=UUID();operation=op;busy=true;error=nil;receipt=nil;var dispatched=false
        defer{if operation==op{operation=nil;busy=false}}
        do{
            let fresh=try NativeVaultWire.status(try await request(review.origin,path:"/api/security/vault/status"))
            guard generation==started,!Task.isCancelled else{return};status=fresh;fetchedAt=Date()
            guard fresh==review.status,fresh.canReview,uptime()<review.expiresAtUptime else{throw NativeVaultFailure("Vault status or review expiry changed during preflight. No unlock was sent.")}
            dispatched=true
            let raw=try await request(review.origin,path:"/api/security/vault/unlock",body:["review_token":review.token])
            guard generation==started else{return}
            guard raw["operation"] as? String=="unlock",raw["review_token"] as? String==review.token,let statusRaw=raw["status"] as? [String:Any],let ok=NativeVaultWire.bool(raw["ok"]) else{throw NativeVaultFailure("Unlock receipt did not match this review. The OS operation may still be running; refresh passive status.")}
            let current=try NativeVaultWire.status(statusRaw);guard ok==current.credentialsAvailable else{throw NativeVaultFailure("Unlock receipt readiness was inconsistent. Refresh passive status before another attempt.")}
            status=current;fetchedAt=Date()
            receipt=current.inFlight ? "The backend reports the original unlock is still running. Its bounded wait ended; refresh passive status. No second OS operation was requested." : current.credentialsAvailable ? "The backend reports authenticated credentials available. Full agent bootstrap, sync and provider connectivity are separate status; they are not inferred from unlock." : "The backend completed this unlock attempt without making credentials available. No reset, initialization or recovery was requested."
        }catch{guard generation==started else{return};self.error=((error as? NativeVaultFailure)?.message ?? "Unlock response is unconfirmed. Private system details are withheld.")+(dispatched ? " An already started OS operation may continue. Do not repeat the old review; refresh passive status first." : "")}
    }
    func cancel(_ review:NativeVaultReview)async {
        guard canUse(review) else{return};used.insert(review.id)
        let started=generation,op=UUID();operation=op;busy=true
        defer{if operation==op{operation=nil;busy=false}}
        do{let raw=try await request(review.origin,path:"/api/security/vault/unlock/cancel",body:["review_token":review.token]);guard generation==started else{return};guard NativeVaultWire.bool(raw["ok"])==true else{throw NativeVaultFailure("Review cancellation was not acknowledged.")};receipt="The backend acknowledged review cancellation. This only revokes an unused review; it does not cancel an OS operation."}
        catch{guard generation==started else{return};self.error="Review cancellation could not be acknowledged. No unlock was sent by this cancelled client review; server expiry remains bounded."}
    }
}
struct NativeAgentBootstrapStatus:Equatable {
    let supported:Bool,phase:String,inFlight:Bool,restartRequired:Bool,credentialsAvailable:Bool,memoryAvailable:Bool,orchestratorAvailable:Bool,bootstrapRequired:Bool,agentReady:Bool
    var canReview:Bool{supported && phase=="pending" && !inFlight && !restartRequired && credentialsAvailable && memoryAvailable && !orchestratorAvailable && bootstrapRequired && !agentReady}
}
struct NativeAgentBootstrapReview:Identifiable {
    let id=UUID()
    let token:String,scope:String,origin:URL,generation:UUID,status:NativeAgentBootstrapStatus,expiresAtUptime:TimeInterval
    var explanation:String{"Service: \(origin.absoluteString)\n\nServer-reviewed scope:\n\(scope)\n\nPrevious phase: \(status.phase). Memory available: \(status.memoryAvailable). Credentials available: \(status.credentialsAvailable). Bootstrap required: \(status.bootstrapRequired).\n\nThis separate action resumes full agent startup. Providers and integrations may connect, scheduled jobs and background processing may start, and operational data may be written under existing settings. Partial startup and already-started external effects cannot be rolled back reliably. A timeout or cancelled HTTP wait may leave the original operation running. Refresh passive status; do not repeat confirmation. A partial failure or changed identity requires process restart."}
}
extension NativeVaultWire {
    static func bootstrapStatus(_ raw:[String:Any])throws->NativeAgentBootstrapStatus {
        guard let supported=bool(raw["supported"]),let phase=raw["phase"] as? String,["pending","starting","ready","failed","cleanup_failed"].contains(phase),
              let pending=bool(raw["in_flight"]),let restart=bool(raw["restart_required"]),let credentials=bool(raw["credentials_available"]),let memory=bool(raw["memory_available"]),
              let orchestrator=bool(raw["orchestrator_available"]),let required=bool(raw["bootstrap_required"]),let ready=bool(raw["agent_ready"]),
              !ready || (supported && phase=="ready" && !pending && !restart && credentials && memory && orchestrator && !required),
              !restart || !ready else{throw NativeVaultFailure("Unsupported agent continuation status. No startup was requested.")}
        return NativeAgentBootstrapStatus(supported:supported,phase:phase,inFlight:pending,restartRequired:restart,credentialsAvailable:credentials,memoryAvailable:memory,orchestratorAvailable:orchestrator,bootstrapRequired:required,agentReady:ready)
    }
}
@MainActor final class NativeAgentBootstrapModel:ObservableObject {
    @Published private(set) var status:NativeAgentBootstrapStatus?
    @Published private(set) var fetchedAt:Date?
    @Published private(set) var busy=false
    @Published private(set) var error:String?
    @Published private(set) var receipt:String?
    private var baseURL:URL?,generation=UUID(),operation:UUID?
    private var used:Set<UUID>=[]
    private var restartObserved=false
    var restartRequired:Bool{restartObserved || status?.restartRequired==true}
    var canPrepareReview:Bool{!busy && !restartObserved && status?.canReview==true}
    private func adopt(_ current:NativeAgentBootstrapStatus){restartObserved = restartObserved || current.restartRequired;status=current;fetchedAt=Date()}
    private let session:URLSession
    private let uptime:()->TimeInterval
    init(session:URLSession?=nil,uptime:@escaping()->TimeInterval={ProcessInfo.processInfo.systemUptime}){self.session=session ?? NativeVaultRedirectGuard.session();self.uptime=uptime}
    func configure(baseURL:URL?){guard self.baseURL != baseURL else{return};generation=UUID();self.baseURL=baseURL;status=nil;fetchedAt=nil;error=nil;receipt=nil;used=[];restartObserved=false;busy=operation != nil}
    private func request(_ base:URL,path:String,body:[String:Any]?=nil)async throws->[String:Any]{
        var req=URLRequest(url:try NativeVaultWire.url(base,path:path));req.httpMethod=body==nil ? "GET" : "POST"
        if let body=body{req.httpBody=try JSONSerialization.data(withJSONObject:body);req.setValue("application/json",forHTTPHeaderField:"Content-Type")}
        let (data,response)=try await session.data(for:req)
        guard data.count<=16384,let http=response as? HTTPURLResponse,http.url==req.url,let raw=(try? JSONSerialization.jsonObject(with:data)) as? [String:Any] else{throw NativeVaultFailure("Agent continuation response is unconfirmed. Private service details are withheld.")}
        if !(200..<300).contains(http.statusCode){
            if let detail=raw["detail"] as? [String:Any],detail["code"] as? String=="bootstrap_config_requires_update"{throw NativeVaultFailure("Current settings require automatic model or phone-bridge changes, or contain pending account authorization records. Resolve that configuration before reviewing continuation. No startup was sent.")}
            throw NativeVaultFailure("Agent continuation response is unconfirmed. Private service details are withheld.")
        }
        guard raw["error"]==nil else{throw NativeVaultFailure("Agent continuation response is unconfirmed. Private service details are withheld.")};return raw
    }
    func refresh()async {
        guard !busy else{return};guard let base=baseURL else{error="The local service is not ready.";return}
        let started=generation,op=UUID();operation=op;busy=true
        defer{if operation==op{operation=nil;busy=false}}
        do{let current=try NativeVaultWire.bootstrapStatus(try await request(base,path:"/api/security/agent-bootstrap/status"));guard generation==started,!Task.isCancelled else{return};adopt(current);error=nil}
        catch{guard generation==started else{return};self.error="Passive continuation status could not be refreshed. Displayed status remains from the last successful read. No startup was requested."}
    }
    func prepareReview()async->NativeAgentBootstrapReview? {
        guard !busy,let base=baseURL else{return nil}
        guard !restartObserved else{error="A previous partial startup requires process restart. No startup was sent.";return nil}
        let started=generation,op=UUID();operation=op;busy=true;error=nil;receipt=nil
        defer{if operation==op{operation=nil;busy=false}}
        do{
            let current=try NativeVaultWire.bootstrapStatus(try await request(base,path:"/api/security/agent-bootstrap/status"))
            guard generation==started,!Task.isCancelled else{return nil};adopt(current)
            guard current.canReview else{throw NativeVaultFailure("Agent continuation cannot be reviewed in this state. Unlock and restore memory first; a partial failure requires process restart.")}
            let raw=try await request(base,path:"/api/security/agent-bootstrap/review",body:[:])
            guard generation==started,!Task.isCancelled else{return nil}
            guard let token=raw["review_token"] as? String,token.range(of:"^[A-Za-z0-9_-]{43}$",options:.regularExpression) != nil,
                  let seconds=NativeVaultWire.number(raw["expires_in_seconds"]),seconds>0,seconds<=300,let scope=raw["scope"] as? String,!scope.isEmpty,scope.utf8.count<=4096,
                  !scope.unicodeScalars.contains(where:{($0.value<32 && $0.value != 10) || $0.value==127}),let previous=raw["previous_status"] as? [String:Any] else{throw NativeVaultFailure("Unsupported continuation review. No startup was sent.")}
            let reviewed=try NativeVaultWire.bootstrapStatus(previous)
            guard reviewed==current,reviewed.canReview else{throw NativeVaultFailure("Agent state changed while preparing review. Refresh and prepare a new review.")}
            return NativeAgentBootstrapReview(token:token,scope:scope,origin:base,generation:started,status:reviewed,expiresAtUptime:uptime()+seconds)
        }catch{guard generation==started else{return nil};self.error=(error as? NativeVaultFailure)?.message ?? "Continuation review could not be prepared. Private details are withheld; no startup was sent.";return nil}
    }
    func canUse(_ review:NativeAgentBootstrapReview)->Bool{!busy && !restartObserved && baseURL==review.origin && generation==review.generation && !used.contains(review.id) && uptime()<review.expiresAtUptime && status==review.status}
    func confirm(_ review:NativeAgentBootstrapReview)async {
        guard canUse(review) else{error="This continuation review is stale, expired or already used. No startup was sent.";return}
        used.insert(review.id);let started=generation,op=UUID();operation=op;busy=true;error=nil;receipt=nil;var dispatched=false
        defer{if operation==op{operation=nil;busy=false}}
        do{
            let fresh=try NativeVaultWire.bootstrapStatus(try await request(review.origin,path:"/api/security/agent-bootstrap/status"))
            guard generation==started,!Task.isCancelled else{return};adopt(fresh)
            guard fresh==review.status,fresh.canReview,uptime()<review.expiresAtUptime else{throw NativeVaultFailure("Continuation state or expiry changed during preflight. No startup was sent.")}
            dispatched=true
            let raw=try await request(review.origin,path:"/api/security/agent-bootstrap",body:["review_token":review.token])
            guard generation==started else{return}
            guard raw["operation"] as? String=="bootstrap",raw["review_token"] as? String==review.token,let statusRaw=raw["status"] as? [String:Any],let ok=NativeVaultWire.bool(raw["ok"]) else{throw NativeVaultFailure("Continuation receipt did not match this review.")}
            let current=try NativeVaultWire.bootstrapStatus(statusRaw)
            guard ok==current.agentReady else{throw NativeVaultFailure("Continuation receipt readiness was inconsistent.")}
            adopt(current)
            receipt=current.inFlight ? "The original continuation is still running. Refresh passive status; no second startup was requested." : current.restartRequired ? "The backend requires process restart after partial or interrupted startup. This panel does not retry or reset your data." : current.agentReady ? "The backend reports memory, orchestrator and startup hooks ready. Provider connectivity and individual optional services still have their own status." : "Agent readiness was not established. Refresh passive status before another action."
        }catch{guard generation==started else{return};self.error=((error as? NativeVaultFailure)?.message ?? "Continuation response is unconfirmed. Private details are withheld.")+(dispatched ? " An already started continuation may still be running or have produced external effects. Do not repeat the old review; refresh passive status." : "")}
    }
    func cancel(_ review:NativeAgentBootstrapReview)async {
        guard canUse(review) else{return};used.insert(review.id);let started=generation,op=UUID();operation=op;busy=true
        defer{if operation==op{operation=nil;busy=false}}
        do{let raw=try await request(review.origin,path:"/api/security/agent-bootstrap/cancel",body:["review_token":review.token]);guard generation==started else{return};guard NativeVaultWire.bool(raw["ok"])==true else{throw NativeVaultFailure("Review cancellation was not acknowledged.")};receipt="The backend revoked this unused review. Cancellation does not stop an already started continuation."}
        catch{guard generation==started else{return};self.error="Continuation review cancellation is unconfirmed. No startup was sent by this cancelled review; server expiry remains bounded."}
    }
}

struct NativeVaultFeatureView:View {
    let baseURL:URL?
    @StateObject private var model=NativeVaultModel()
    @StateObject private var bootstrap=NativeAgentBootstrapModel()
    @State private var bootstrapReview:NativeAgentBootstrapReview?
    @State private var review:NativeVaultReview?
    var body:some View {
        VStack(alignment:.leading,spacing:12) {
                Text("Encrypted credentials").font(.title3.bold())
                Text("Status refresh reads the backend’s in-memory state and does not request OS Keychain access. Initialization and recovery are unavailable here; this panel does not reset your keychain.").font(.caption).foregroundStyle(.secondary)
                if let status=model.status {
                    Text("Vault: \(status.state) · \(status.code)").font(.headline)
                    Text("Credentials: \(status.credentialsAvailable ? "available" : "unavailable") · Memory working data: \(status.memoryAvailable ? "available" : "unavailable") · Full bootstrap: \(status.bootstrapRequired ? "still required" : "not reported required") · Sync: \(status.syncDormant ? "dormant" : "not reported dormant")").font(.caption)
                    if status.inFlight{Text("The original unlock is still running. Refresh status without starting another OS operation.").foregroundStyle(.orange)}
                    if !status.unlockSupported{Text("Explicit unlock is unsupported for this service. No fallback, initialization or reset is attempted.").font(.caption).foregroundStyle(.secondary)}
                }else{Text("Vault state unavailable. No credential readiness is inferred.").foregroundStyle(.secondary)}
                if let fetched=model.fetchedAt{Text("Status fetched \(fetched.formatted(date:.abbreviated,time:.standard)).").font(.caption).foregroundStyle(.secondary)}
                if let error=model.error{Text(error).foregroundStyle(.orange).textSelection(.enabled)}
                if let receipt=model.receipt{Text(receipt).font(.callout).textSelection(.enabled)}
                HStack{Button("Refresh passive status"){Task{await model.refresh()}}.disabled(model.busy || bootstrap.busy || baseURL==nil);Button("Prepare unlock review…"){Task{review=await model.prepareReview()}}.disabled(model.busy || bootstrap.busy || model.status?.canReview != true);if model.busy{ProgressView().controlSize(.small)}}
                Divider()
                Text("Resume agent startup").font(.title3.bold())
                Text("Restoring encrypted memory does not start the full agent. Review this separate action to connect enabled integrations and start background services under current settings.").font(.caption).foregroundStyle(.secondary)
                if let state=bootstrap.status {
                    Text("Continuation: \(state.phase)").font(.headline)
                    if state.agentReady{Text("Reviewed continuation reports the agent ready.").font(.caption)}
                    else if !state.bootstrapRequired && state.phase=="pending"{Text("No blocked continuation is reported. Current agent readiness comes from connection health.").font(.caption)}
                    if bootstrap.restartRequired{Text("Process restart required. Partial startup is not retried here.").foregroundStyle(.orange)}
                    else if state.inFlight{Text("The original startup may still be running. Refresh passive status; do not start another operation.").foregroundStyle(.orange)}
                    else if state.bootstrapRequired && !state.credentialsAvailable{Text("Unlock existing credentials and restore encrypted memory before preparing continuation.").font(.caption)}
                    if !state.supported{Text("Reviewed continuation is unavailable for this service.").font(.caption)}
                }else{Text("Continuation status unavailable. Unlock alone does not establish agent readiness.").foregroundStyle(.secondary)}
                if let fetched=bootstrap.fetchedAt{Text("Continuation status fetched \(fetched.formatted(date:.abbreviated,time:.standard)).").font(.caption).foregroundStyle(.secondary)}
                if let error=bootstrap.error{Text(error).foregroundStyle(.orange).textSelection(.enabled)}
                if let receipt=bootstrap.receipt{Text(receipt).font(.callout).textSelection(.enabled)}
                HStack{
                    Button("Refresh continuation status"){Task{await bootstrap.refresh()}}.disabled(model.busy || bootstrap.busy || baseURL==nil)
                    Button("Review agent startup…"){Task{bootstrapReview=await bootstrap.prepareReview()}}.disabled(model.busy || bootstrap.busy || !bootstrap.canPrepareReview)
                    if bootstrap.busy{ProgressView().controlSize(.small)}
                }
        }.frame(maxWidth:.infinity,alignment:.leading)
        .task(id:baseURL){review=nil;bootstrapReview=nil;model.configure(baseURL:baseURL);bootstrap.configure(baseURL:baseURL);await model.refresh();await bootstrap.refresh()}
        .sheet(item:$review){item in
            VStack(alignment:.leading,spacing:16){
                ScrollView{NativeReviewSummaryView(review:NativeReviewSummary(
                    action:"Unlock your existing vault?",
                    effect:"Makes saved credentials available and may restore local memory working data.",
                    materialScope:["macOS may request access to the existing Keychain key.","An OS operation already started may continue after a timeout. Agent startup and sync remain separate."],
                    targets:[.init(label:"Local service",value:item.origin.absoluteString),.init(label:"Server-reviewed scope",value:item.scope)],
                    details:item.explanation))}
                HStack{
                    Button("Cancel review"){review=nil;Task{await model.cancel(item)}}
                    Spacer()
                    Button("Send reviewed unlock"){review=nil;Task{await model.confirm(item);await bootstrap.refresh()}}.disabled(!model.canUse(item))
                }
            }.padding(24).frame(width:700,height:530).interactiveDismissDisabled()
        }
        .sheet(item:$bootstrapReview){item in
            VStack(alignment:.leading,spacing:16){
                ScrollView{NativeReviewSummaryView(review:NativeReviewSummary(
                    action:"Resume the full agent?",
                    effect:"Starts the previously blocked agent and enabled background services.",
                    materialScope:["Providers, integrations, devices and schedulers may perform network or local actions under existing settings.","Already-started effects may continue after timeout. Partial failure requires restart; no automatic retry or reset."],
                    targets:[.init(label:"Local service",value:item.origin.absoluteString),.init(label:"Server-reviewed scope",value:item.scope)],
                    details:item.explanation))}
                HStack{
                    Button("Cancel startup review"){bootstrapReview=nil;Task{await bootstrap.cancel(item)}}
                    Spacer()
                    Button("Send reviewed startup"){bootstrapReview=nil;Task{await bootstrap.confirm(item);await model.refresh()}}.disabled(!bootstrap.canUse(item))
                }
            }.padding(24).frame(width:700,height:530).interactiveDismissDisabled()
        }
    }
}
