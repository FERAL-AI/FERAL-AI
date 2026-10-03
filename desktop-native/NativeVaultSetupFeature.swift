import Foundation
import SwiftUI

struct NativeVaultSetupStatus:Equatable {
    let supported:Bool,code:String,inFlight:Bool,credentialsAvailable:Bool,existingArtifacts:Bool,requiresSignedAcceptance:Bool
    var canPrepare:Bool { supported && code == "available" && !inFlight && !credentialsAvailable && !existingArtifacts && !requiresSignedAcceptance }
    var message:String {
        switch code {
        case "available":return "Create encrypted credential storage before saving a cloud provider key. Local AI does not require it."
        case "vault_ready":return "Encrypted credential storage is authenticated. Provider keys still need separate review."
        case "release_acceptance_required":return "Fresh cloud-key setup requires a signed app with verified Keychain behavior. This build cannot initialize storage; local AI remains available."
        case "vault_artifacts_present", "key_already_present":return "Existing credential storage or a shared key was found. Unlock the existing vault; this setup will not replace or reset it."
        case "initialization_partial":return "A key may have been stored before disk persistence failed. Initialization is incomplete; no reset, deletion or automatic retry will occur."
        case "operation_in_progress":return "The original storage operation is still running. Refresh status; do not start another operation."
        case "platform_unsupported":return "This platform has no verified secure initializer. No plaintext fallback is used."
        default:return "Secure storage cannot be initialized in the current state. Local AI remains available."
        }
    }
}
struct NativeVaultSetupReview:Identifiable {
    let id=UUID()
    let token:String,scope:String,origin:URL,generation:UUID,status:NativeVaultSetupStatus,expiresAtUptime:TimeInterval
}
enum NativeVaultSetupWire {
    static let codes:Set<String>=["available","vault_ready","release_acceptance_required","platform_unsupported","initializer_not_configured","operation_in_progress","storage_unavailable","initialization_partial","vault_artifacts_present","key_already_present"]
    static func status(_ raw:[String:Any])throws->NativeVaultSetupStatus {
        guard let code=raw["code"] as? String,codes.contains(code),let supported=NativeVaultWire.bool(raw["supported"]),let pending=NativeVaultWire.bool(raw["in_flight"]),let credentials=NativeVaultWire.bool(raw["credential_storage_available"]),let artifacts=NativeVaultWire.bool(raw["existing_artifacts"]),let gated=NativeVaultWire.bool(raw["requires_signed_acceptance"]),NativeVaultWire.bool(raw["local_use_requires_vault"])==false,
              supported == (code == "available"),!supported || (!pending && !credentials && !artifacts && !gated),code != "vault_ready" || credentials else {throw NativeVaultFailure("Unsupported credential setup status. No initialization was requested.")}
        return NativeVaultSetupStatus(supported:supported,code:code,inFlight:pending,credentialsAvailable:credentials,existingArtifacts:artifacts,requiresSignedAcceptance:gated)
    }
    static func token(_ raw:Any?)->String? {guard let text=raw as? String,UUID(uuidString:text) != nil,text.count==36 else{return nil};return text}
}
private final class NativeVaultSetupRedirectGuard:NSObject,URLSessionTaskDelegate {
    func urlSession(_ session:URLSession,task:URLSessionTask,willPerformHTTPRedirection response:HTTPURLResponse,newRequest request:URLRequest,completionHandler:@escaping(URLRequest?)->Void){completionHandler(nil)}
    static func session()->URLSession{let config=URLSessionConfiguration.ephemeral;config.timeoutIntervalForRequest=12;config.timeoutIntervalForResource=15;return URLSession(configuration:config,delegate:NativeVaultSetupRedirectGuard(),delegateQueue:nil)}
}
@MainActor final class NativeVaultSetupModel:ObservableObject {
    @Published private(set) var status:NativeVaultSetupStatus?
    @Published private(set) var busy=false
    @Published private(set) var error:String?
    @Published private(set) var receipt:String?
    @Published private(set) var outcomeUnconfirmed=false
    private var baseURL:URL?,generation=UUID(),operation:UUID?
    private var used:Set<UUID>=[]
    private let session:URLSession
    private let uptime:()->TimeInterval
    var canPrepare:Bool{!busy && !outcomeUnconfirmed && status?.canPrepare==true}
    init(session:URLSession?=nil,uptime:@escaping()->TimeInterval={ProcessInfo.processInfo.systemUptime}){self.session=session ?? NativeVaultSetupRedirectGuard.session();self.uptime=uptime}
    func configure(baseURL:URL?){guard self.baseURL != baseURL else{return};self.baseURL=baseURL;generation=UUID();status=nil;error=nil;receipt=nil;used=[];outcomeUnconfirmed=false;busy=operation != nil}
    private func request(_ base:URL,path:String,body:[String:Any]?=nil)async throws->[String:Any] {
        var req=URLRequest(url:try NativeVaultWire.url(base,path:path));req.httpMethod=body==nil ? "GET" : "POST"
        if let body{req.httpBody=try JSONSerialization.data(withJSONObject:body);req.setValue("application/json",forHTTPHeaderField:"Content-Type")}
        let (data,response)=try await session.feralLocalData(for:req)
        guard data.count<=16384,let http=response as? HTTPURLResponse,http.url==req.url,(200..<300).contains(http.statusCode),let raw=(try? JSONSerialization.jsonObject(with:data)) as? [String:Any],raw["error"]==nil else{throw NativeVaultFailure("Credential setup response could not be verified. Private service details are withheld.")};return raw
    }
    func refresh()async {
        guard !busy,let base=baseURL else{return};let started=generation,op=UUID();operation=op;busy=true
        defer{if operation==op{operation=nil;busy=false}}
        do{let current=try NativeVaultSetupWire.status(try await request(base,path:"/api/security/vault/initialize/status"));guard generation==started,!Task.isCancelled else{return};status=current;error=nil;if !current.inFlight{outcomeUnconfirmed=false}}
        catch{guard generation==started else{return};self.error="Credential setup status could not be refreshed. No initialization was requested; existing data remains unchanged by this read."}
    }
    func prepareReview()async->NativeVaultSetupReview? {
        guard !busy,!outcomeUnconfirmed,let base=baseURL else{return nil};let started=generation,op=UUID();operation=op;busy=true;error=nil;receipt=nil
        defer{if operation==op{operation=nil;busy=false}}
        do {
            let current=try NativeVaultSetupWire.status(try await request(base,path:"/api/security/vault/initialize/status"));guard generation==started,!Task.isCancelled else{return nil};status=current
            guard current.canPrepare else{throw NativeVaultFailure(current.message)}
            let raw=try await request(base,path:"/api/security/vault/initialize/review",body:[:]);guard generation==started,!Task.isCancelled else{return nil}
            guard let token=NativeVaultSetupWire.token(raw["review_token"]),let seconds=NativeVaultWire.number(raw["expires_in_seconds"]),seconds>0,seconds<=300,let scope=raw["scope"] as? String,!scope.isEmpty,scope.utf8.count<=4096,!scope.unicodeScalars.contains(where:{($0.value<32 && $0.value != 10) || $0.value==127}),let previous=raw["previous_status"] as? [String:Any],try NativeVaultSetupWire.status(previous)==current else{throw NativeVaultFailure("Initialization review did not match the current storage state. No initialization was sent.")}
            return NativeVaultSetupReview(token:token,scope:scope,origin:base,generation:started,status:current,expiresAtUptime:uptime()+seconds)
        }catch{guard generation==started else{return nil};self.error=(error as? NativeVaultFailure)?.message ?? "Credential setup review is unavailable. No initialization was sent.";return nil}
    }
    func canUse(_ review:NativeVaultSetupReview)->Bool{!busy && !outcomeUnconfirmed && baseURL==review.origin && generation==review.generation && !used.contains(review.id) && uptime()<review.expiresAtUptime && status==review.status && review.status.canPrepare}
    func confirm(_ review:NativeVaultSetupReview)async->Bool {
        guard canUse(review) else{error="This initialization review is stale, expired or already used. No initialization was sent.";return false}
        used.insert(review.id);let started=generation,op=UUID();operation=op;busy=true;error=nil;receipt=nil;var dispatched=false
        defer{if operation==op{operation=nil;busy=false}}
        do {
            let fresh=try NativeVaultSetupWire.status(try await request(review.origin,path:"/api/security/vault/initialize/status"));guard generation==started,!Task.isCancelled else{return false};status=fresh
            guard fresh==review.status,fresh.canPrepare,uptime()<review.expiresAtUptime else{throw NativeVaultFailure("Credential storage changed or review expired. No initialization was sent.")}
            dispatched=true;outcomeUnconfirmed=true
            let raw=try await request(review.origin,path:"/api/security/vault/initialize",body:["review_token":review.token]);guard generation==started,!Task.isCancelled else{return false}
            guard raw["operation"] as? String=="initialize",raw["review_token"] as? String==review.token,let ok=NativeVaultWire.bool(raw["ok"]),let result=raw["status"] as? [String:Any],let vault=raw["vault_status"] as? [String:Any],let credentials=NativeVaultWire.bool(vault["credentials_available"]),let pending=NativeVaultWire.bool(vault["in_flight"]),ok==credentials else{throw NativeVaultFailure("Initialization receipt did not match this review or its reported outcome.")}
            let reported=try NativeVaultSetupWire.status(result)
            guard reported.credentialsAvailable==credentials,reported.inFlight==pending else{throw NativeVaultFailure("Initialization receipt reported inconsistent readiness.")}
            let readback=try NativeVaultSetupWire.status(try await request(review.origin,path:"/api/security/vault/initialize/status"));guard generation==started,!Task.isCancelled else{return false};status=readback
            if readback.inFlight{receipt="The original initialization is still running. Refresh passive status; do not retry this review.";return false}
            if !ok || !readback.credentialsAvailable{outcomeUnconfirmed=false;receipt=readback.message;return false}
            let vaultReadback=try await request(review.origin,path:"/api/security/vault/status");guard generation==started,!Task.isCancelled else{return false}
            guard vaultReadback["state"] as? String=="ready",NativeVaultWire.bool(vaultReadback["credentials_available"])==true,NativeVaultWire.bool(vaultReadback["in_flight"])==false else{throw NativeVaultFailure("Independent vault readiness was not established after initialization.")}
            outcomeUnconfirmed=false;receipt="Encrypted credential storage was initialized and authenticated, with independent status readback. No provider key was saved and no model request was sent.";return true
        }catch{guard generation==started else{return false};self.error=((error as? NativeVaultFailure)?.message ?? "Credential setup response is unconfirmed. Private details are withheld.")+(dispatched ? " An OS operation may still run or have stored a key. Refresh passive status before any new review; no reset or retry occurs." : "");return false}
    }
    func cancel(_ review:NativeVaultSetupReview)async {
        guard canUse(review) else{return};used.insert(review.id);let started=generation,op=UUID();operation=op;busy=true
        defer{if operation==op{operation=nil;busy=false}}
        do{let raw=try await request(review.origin,path:"/api/security/vault/initialize/cancel",body:["review_token":review.token]);guard generation==started else{return};guard NativeVaultWire.bool(raw["ok"])==true else{throw NativeVaultFailure("Review cancellation was not acknowledged.")};receipt="Unused initialization review cancelled. No initialization was sent by this review."}
        catch{guard generation==started else{return};self.error="Review cancellation was not acknowledged. No initialization was sent by this cancelled review; server expiry remains bounded."}
    }
}
struct NativeVaultSetupFeatureView:View {
    let baseURL:URL?
    var onReady:()->Void={}
    @StateObject private var model=NativeVaultSetupModel()
    @State private var review:NativeVaultSetupReview?
    var body:some View {
        VStack(alignment:.leading,spacing:10){
            Text("Set up encrypted credentials").font(.headline)
            Text(model.status?.message ?? "Reading passive credential setup status…").font(.caption).foregroundStyle(.secondary)
            if let error=model.error{NativeSelectableText(error).foregroundStyle(.orange)}
            if let receipt=model.receipt{NativeSelectableText(receipt).font(.callout)}
            HStack{Button("Refresh setup status"){Task{await model.refresh()}}.disabled(model.busy || baseURL==nil);Button("Review secure setup…"){Task{review=await model.prepareReview()}}.disabled(!model.canPrepare);if model.busy{ProgressView().controlSize(.small)}}
        }.task(id:baseURL){review=nil;model.configure(baseURL:baseURL);await model.refresh()}
        .sheet(item:$review){item in
            VStack(alignment:.leading,spacing:16){
                ScrollView{NativeReviewSummaryView(review:NativeReviewSummary(action:"Create encrypted credential storage?",effect:"Adds a random master key to your existing macOS Keychain and writes a fresh empty encrypted vault.",materialScope:["Existing keys and vault files are never replaced or reset. macOS may ask for access.","A disk failure after the key write can leave partial initialization. Cancelling an HTTP wait cannot undo an OS operation.","Saving a cloud provider key, activating a model and starting integrations remain separate actions."],targets:[.init(label:"Local service",value:item.origin.absoluteString),.init(label:"Server-reviewed scope",value:item.scope)],details:"Local AI remains available without this setup."))}
                HStack{Button("Cancel review"){review=nil;Task{await model.cancel(item)}};Spacer();Button("Create reviewed secure storage"){review=nil;Task{if await model.confirm(item){onReady()}}}.disabled(!model.canUse(item))}
            }.padding(24).frame(width:700,height:530).interactiveDismissDisabled()
        }
    }
}
