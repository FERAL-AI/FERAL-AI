import Foundation
import SwiftUI
import CoreFoundation

struct NativeSetupFailure:LocalizedError {let message:String;init(_ message:String){self.message=message};var errorDescription:String?{message}}
struct NativeSetupProvider:Identifiable {
    let id:String,name:String,defaultModel:String,defaultEndpoint:String
    let endpointTemplate:String?
    let local:Bool,needsKey:Bool,configured:Bool,chatReady:Bool
    let reachable:Bool?
    let runtimeSupported:Bool?,setupSelectable:Bool?
    var automaticChoice:Bool{runtimeSupported==true && setupSelectable==true}
    let raw:[String:Any]
}
enum NativeSetupOperation {case saveCredential,activate,probe,complete}
struct NativeSetupReview:Identifiable {
    let id=UUID(),generation:UUID,origin:URL
    let createdUptime=ProcessInfo.processInfo.systemUptime
    let operation:NativeSetupOperation,provider:String,model:String,endpoint:String,secret:String
    let config:[String:Any],descriptor:[String:Any],setupComplete:Bool
    var title:String{switch operation{case .saveCredential:return "Save this provider credential?";case .activate:return "Activate this chat provider?";case .probe:return "Probe the saved provider?";case .complete:return "Finish provider setup?"}}
    var explanation:String{
        let common="Local service: \(origin.absoluteString). Provider: \(provider). Model: \(model). Endpoint: \(endpoint.isEmpty ? "provider runtime default" : endpoint)."
        switch operation{
        case .saveCredential:return common+"\nStores the entered secret through the existing encrypted vault/config credential route and updates the provider adapter/environment. Vault readiness is checked again first. Saving is not provider authorization or a successful inference test. The secret is intentionally omitted from this review. An active provider endpoint may be updated; no keychain initialization or reset is requested."
        case .activate:return common+"\nPersists provider/model/endpoint for chat and asks the backend to reconfigure its runtime. This can contact the provider or gateway. Existing fallback provider settings are preserved exactly; no additional provider is enabled by default. Coding and voice configuration remain separate. Save/activation receipts do not verify model availability or a successful chat response."
        case .probe:return common+"\nContacts this provider using its BACKEND-SAVED adapter configuration and credentials. Draft endpoint/model changes are not sent by the probe endpoint. The destination may reflect a provider-scoped override not exposed by the catalogue; no exact model inference or billing outcome is promised. A reported reachable provider is separate from a successful chat response."
        case .complete:return common+"\nMarks setup complete through /api/setup/complete with an empty body. Avatar and name remain managed by the preceding native step. No voice provider, microphone permission, network access mode, pairing, sync, fallback or device consent is configured here. These optional steps are deferred. Completion does not unlock credentials, continue a blocked encrypted-memory bootstrap, or prove working provider inference."
        }
    }
}
enum NativeSetupWire {
    static func bool(_ value:Any?)->Bool?{guard let n=value as? NSNumber,CFGetTypeID(n)==CFBooleanGetTypeID() else{return nil};return n.boolValue}
    static func text(_ value:Any?,limit:Int,empty:Bool=false)->String?{guard let s=value as? String,(empty || !s.isEmpty),s.utf8.count<=limit,!s.unicodeScalars.contains(where:{$0.value<32 || $0.value==127}) else{return nil};return s}
    static func id(_ value:Any?)->String?{guard let id=text(value,limit:80),id.range(of:"^[A-Za-z0-9_.-]+$",options:.regularExpression) != nil else{return nil};return id}
    static func endpoint(_ raw:String)throws->String{let clean=raw.trimmingCharacters(in:.whitespacesAndNewlines);if clean.isEmpty{return ""};guard clean.utf8.count<=2048,!clean.contains("{"),!clean.contains("}"),let url=URLComponents(string:clean),["http","https"].contains(url.scheme ?? ""),url.host != nil,url.user==nil,url.password==nil,url.query==nil,url.fragment==nil else{throw NativeSetupFailure("Enter a clean HTTP(S) provider endpoint without embedded credentials or query parameters.")};return clean}
    static func equal(_ a:[String:Any],_ b:[String:Any])->Bool{guard JSONSerialization.isValidJSONObject(a),JSONSerialization.isValidJSONObject(b),let x=try? JSONSerialization.data(withJSONObject:a,options:.sortedKeys),let y=try? JSONSerialization.data(withJSONObject:b,options:.sortedKeys) else{return false};return x==y}
    static func models(_ raw:[String:Any],provider:String)throws->(ids:[String],source:String){
        guard let rows=raw["models"] as? [Any],rows.count<=2000,let source=raw["source"] as? String,["live","cache","fallback"].contains(source),raw["provider_id"] == nil || raw["provider_id"] as? String==provider,raw["warning"] == nil || (raw["warning"] as? String).map({$0.utf8.count<=4096})==true else{throw NativeSetupFailure("Cached model list is unsupported.")}
        let ids:[String]
        // String IDs are the actual route contract; object/id rows remain a
        // homogeneous legacy format, not an excuse to discard malformed rows.
        if let strings=rows as? [String]{ids=strings}
        else if let objects=rows as? [[String:Any]]{ids=try objects.map{row in guard let id=row["id"] as? String else{throw NativeSetupFailure("Cached model list is unsupported.")};return id}}
        else{throw NativeSetupFailure("Cached model list is unsupported.")}
        guard ids.allSatisfy({!$0.isEmpty && $0.utf8.count<=256 && !$0.unicodeScalars.contains(where:{CharacterSet.whitespacesAndNewlines.union(.controlCharacters).contains($0)})}) else{throw NativeSetupFailure("Cached model IDs are unsupported.")}
        var seen:Set<String>=[];return(ids.filter{seen.insert($0).inserted},source)
    }
    static func probe(_ raw:[String:Any],provider:String)throws->Bool{
        // ProviderStatus is a status receipt, including `error: ""` when reachable.
        // A negative result may contain bounded private diagnostics; never render them.
        guard id(provider)==provider,let reachable=bool(raw["reachable"]),raw["id"] != nil || raw["provider_id"] != nil else{throw NativeSetupFailure("Probe receipt did not identify the reviewed provider.")}
        for key in ["id","provider_id"]{if let value=raw[key]{guard id(value)==provider else{throw NativeSetupFailure("Probe receipt did not identify the reviewed provider.")}}}
        if let value=raw["error"],!(value is NSNull){
            guard let detail=value as? String,detail.utf8.count<=4096,!reachable || detail.isEmpty else{throw NativeSetupFailure("Probe reachability status is unsupported. Private provider details are withheld.")}
        }
        return reachable
    }
    static func providers(_ raw:[String:Any])throws->[NativeSetupProvider]{
        guard let rows=raw["providers"] as? [[String:Any]],rows.count<=100 else{throw NativeSetupFailure("Provider catalogue is unavailable or unsupported.")};var seen:Set<String>=[]
        return try rows.map{row in guard let id=id(row["id"]),seen.insert(id).inserted,let name=text(row["display_name"],limit:160),let local=bool(row["supports_local"]),let key=bool(row["requires_api_key"]),let configured=bool(row["configured"]),let chat=bool(row["chat_ready"]),let model=text(row["default_model"],limit:256,empty:true),let endpoint=text(row["default_base_url"],limit:2048,empty:true) else{throw NativeSetupFailure("Provider catalogue metadata is unsupported.")};if let reachable=row["reachable"],!(reachable is NSNull),bool(reachable)==nil{throw NativeSetupFailure("Provider reachability status is unsupported.")};// The actual Qwen descriptor is informational until its workspace is resolved.
            // `configured` means credential presence, not a resolved adapter endpoint.
            for field in ["runtime_supported","setup_selectable"]{if row[field] != nil && bool(row[field])==nil{throw NativeSetupFailure("Provider runtime selection flags are unsupported.")}}
            let supported=bool(row["runtime_supported"]),selectable=bool(row["setup_selectable"])
            let template=endpoint.contains("{WorkspaceId}") ? endpoint : nil
            if template != nil {_ = try Self.endpoint(endpoint.replacingOccurrences(of:"[{WorkspaceId}]",with:"workspace-example").replacingOccurrences(of:"{WorkspaceId}",with:"workspace-example"))}
            var usableDefault="";if template==nil,supported==true,selectable==true{usableDefault=try Self.endpoint(endpoint)}
            return NativeSetupProvider(id:id,name:name,defaultModel:model,defaultEndpoint:usableDefault,endpointTemplate:template,local:local,needsKey:key,configured:configured,chatReady:chat,reachable:bool(row["reachable"]),runtimeSupported:supported,setupSelectable:selectable,raw:row)}
    }
    static func config(_ raw:[String:Any])throws->[String:Any]{guard id(raw["provider"]) != nil,text(raw["model"],limit:256,empty:true) != nil,let endpoint=text(raw["base_url"],limit:2048,empty:true),let fallbacks=raw["fallback_providers"] as? [String],fallbacks.count<=20,fallbacks.allSatisfy({id($0) != nil}),bool(raw["configured"]) != nil else{throw NativeSetupFailure("Saved provider settings are unsupported.")};_ = try Self.endpoint(endpoint);return raw}
}
private final class NativeSetupRedirectGuard:NSObject,URLSessionTaskDelegate {
    func urlSession(_ session:URLSession,task:URLSessionTask,willPerformHTTPRedirection response:HTTPURLResponse,newRequest request:URLRequest,completionHandler:@escaping(URLRequest?)->Void){completionHandler(nil)}
    static func session()->URLSession{let cfg=URLSessionConfiguration.ephemeral;cfg.timeoutIntervalForResource=180;return URLSession(configuration:cfg,delegate:NativeSetupRedirectGuard(),delegateQueue:nil)}
}
@MainActor final class NativeOnboardingSetupModel:ObservableObject {
    @Published private(set) var providers:[NativeSetupProvider]=[]
    @Published private(set) var models:[String]=[]
    @Published private(set) var selected=""
    @Published private(set) var modelsSource="Not loaded"
    @Published var model="" {didSet{if model != oldValue{clearProbe()}}}
    @Published var endpoint="" {didSet{if endpoint != oldValue{clearProbe()}}}
    @Published private(set) var lastProbeReachable:Bool?
    @Published private(set) var busy=false
    @Published private(set) var vaultReady=false
    @Published private(set) var setupComplete=false
    @Published private(set) var runtimeAvailable:Bool?
    @Published private(set) var error:String?
    @Published private(set) var notice:String?
    private var baseURL:URL?,generation=UUID(),operation:UUID?
    private var saved:[String:Any]=[:],used:Set<UUID>=[]
    private var probeConfig:[String:Any]=[:],probeDescriptor:[String:Any]=[:],probeProvider=""
    var probeStatusText:String{if let verdict=lastProbeReachable{return "Last explicit probe: \(verdict ? "reachable" : "unreachable"). Model inference is not verified by this probe."};return providers.first(where:{$0.id==selected})?.reachable.map{$0 ? "Backend cached status: reachable" : "Backend cached status: unreachable"} ?? "No probe result available in this view."}
    private func clearProbe(){lastProbeReachable=nil;probeConfig=[:];probeDescriptor=[:];probeProvider=""}
    private func sameProbeDescriptor(_ a:[String:Any],_ b:[String:Any])->Bool{let observations:Set<String>=["reachable","last_refresh","default_model","error"];return NativeSetupWire.equal(a.filter{!observations.contains($0.key)},b.filter{!observations.contains($0.key)})}
    private func reconcileProbe(){guard lastProbeReachable != nil else{return};guard selected==probeProvider,NativeSetupWire.equal(saved,probeConfig),let descriptor=providers.first(where:{$0.id==probeProvider})?.raw,sameProbeDescriptor(descriptor,probeDescriptor) else{clearProbe();return}}
    private let session:URLSession
    init(session:URLSession?=nil){self.session=session ?? NativeSetupRedirectGuard.session()}
    func configure(baseURL:URL?){guard self.baseURL != baseURL else{return};clearProbe();generation=UUID();self.baseURL=baseURL;providers=[];models=[];selected="";saved=[:];model="";endpoint="";runtimeAvailable=nil;vaultReady=false;setupComplete=false;error=nil;notice=nil;used=[];busy=operation != nil}
    private func request(_ base:URL,path:String,body:[String:Any]?=nil,query:[URLQueryItem]=[],probeProvider:String?=nil)async throws->[String:Any]{
        guard base.scheme=="http",["127.0.0.1","::1","[::1]"].contains(base.host ?? ""),base.user==nil,base.password==nil,var parts=URLComponents(url:base,resolvingAgainstBaseURL:false) else{throw NativeSetupFailure("Provider setup requires the local service.")};parts.path=path;parts.queryItems=query.isEmpty ? nil : query;parts.fragment=nil;guard let url=parts.url else{throw NativeSetupFailure("Invalid setup URL.")};var req=URLRequest(url:url);req.httpMethod=body==nil ? "GET" : "POST"
        if let body=body{guard JSONSerialization.isValidJSONObject(body) else{throw NativeSetupFailure("Invalid setup request.")};req.httpBody=try JSONSerialization.data(withJSONObject:body);req.setValue("application/json",forHTTPHeaderField:"Content-Type")}
        let (data,response)=try await session.feralLocalData(for:req);guard data.count<=524288,let http=response as? HTTPURLResponse,http.url==url,(200..<300).contains(http.statusCode),let raw=(try? JSONSerialization.jsonObject(with:data)) as? [String:Any] else{throw NativeSetupFailure("Setup request failed or was not acknowledged. Private provider details are withheld.")};if let probeProvider{_ = try NativeSetupWire.probe(raw,provider:probeProvider)}else{guard raw["error"]==nil else{throw NativeSetupFailure("Setup request failed or was not acknowledged. Private provider details are withheld.")}};return raw
    }
    func refresh()async {
        guard !busy,let base=baseURL else{return};let started=generation,op=UUID();operation=op;busy=true;error=nil
        defer{if operation==op{operation=nil;busy=false}}
        do{
            let list=try NativeSetupWire.providers(try await request(base,path:"/api/llm/providers")),cfg=try NativeSetupWire.config(try await request(base,path:"/api/llm/config")),setup=try await request(base,path:"/api/setup/status"),runtime=try await request(base,path:"/api/llm/status")
            guard generation==started,!Task.isCancelled else{return};guard let complete=NativeSetupWire.bool(setup["setup_complete"]) else{throw NativeSetupFailure("Setup completion status is unsupported.")}
            providers=list;saved=cfg;reconcileProbe();setupComplete=complete;runtimeAvailable=NativeSetupWire.bool(runtime["available"])
            if selected.isEmpty,let active=cfg["provider"] as? String,list.contains(where:{$0.id==active}){selected=active;model=cfg["model"] as? String ?? "";endpoint=cfg["base_url"] as? String ?? ""}
            do{let vault=try await request(base,path:"/api/security/vault/status");guard generation==started else{return};vaultReady=vault["state"] as? String=="ready" && NativeSetupWire.bool(vault["credentials_available"])==true && NativeSetupWire.bool(vault["in_flight"])==false}catch{guard generation==started else{return};vaultReady=false}
            if !selected.isEmpty{try await loadModels(base,provider:selected,started:started)}
        }catch{guard generation==started else{return};clearProbe();self.error=(error as? NativeSetupFailure)?.message ?? "Provider setup is unavailable. Encrypted-memory installs may require explicit vault unlock and separately reviewed bootstrap continuation."}
    }
    private func loadModels(_ base:URL,provider:String,started:UUID)async throws{
        // Ambient refresh and selection read existing suggestions only. Provider
        // contact belongs to the separately reviewed connection operation.
        let raw=try await request(base,path:"/api/llm/providers/"+provider+"/models",query:[URLQueryItem(name:"live",value:"false"),URLQueryItem(name:"recommended",value:"true"),URLQueryItem(name:"model_class",value:"chat")]);guard generation==started,selected==provider else{return}
        let parsed=try NativeSetupWire.models(raw,provider:provider);models=parsed.ids
        switch parsed.source{case "live":modelsSource="Previously discovered; no live request";case "cache":modelsSource="Cached; no live request";default:modelsSource="Bundled fallback; no live request"}
    }
    func select(_ id:String)async {guard !busy,let choice=providers.first(where:{$0.id==id}),let base=baseURL else{return};clearProbe();generation=UUID();let started=generation,op=UUID();operation=op;busy=true;selected=id;models=[];modelsSource="Not loaded";model=(saved["provider"] as? String)==id ? saved["model"] as? String ?? "" : choice.defaultModel;endpoint=(saved["provider"] as? String)==id ? saved["base_url"] as? String ?? "" : choice.defaultEndpoint;defer{if operation==op{operation=nil;busy=false}};do{try await loadModels(base,provider:id,started:started)}catch{guard generation==started else{return};self.error="Cached models could not be read. No live discovery was requested; enter a model explicitly."}}
    func review(_ action:NativeSetupOperation,secret:String="")throws->NativeSetupReview {
        guard !busy,let base=baseURL,let choice=providers.first(where:{$0.id==selected}),!saved.isEmpty else{throw NativeSetupFailure("Wait for the saved provider catalogue and settings.")}
        let cleanModel=model.trimmingCharacters(in:.whitespacesAndNewlines),cleanEndpoint=try NativeSetupWire.endpoint(endpoint)
        guard NativeSetupWire.text(cleanModel,limit:256) != nil else{throw NativeSetupFailure("Choose or enter a model name. Cached suggestions do not prove model availability.")}
        if case .saveCredential=action{guard choice.endpointTemplate==nil || !cleanEndpoint.isEmpty else{throw NativeSetupFailure("Enter a valid resolved workspace endpoint before saving this provider credential.")};guard vaultReady,choice.needsKey,!secret.isEmpty,secret.utf8.count<=16384 else{throw NativeSetupFailure("Set up or unlock encrypted credential storage before reviewing a provider key save.")}}
        if case .activate=action{guard choice.endpointTemplate==nil || !cleanEndpoint.isEmpty else{throw NativeSetupFailure("This provider has an unresolved workspace endpoint template. Enter a valid resolved endpoint before activation; configured credentials alone do not prove endpoint readiness.")};guard !choice.needsKey || choice.configured else{throw NativeSetupFailure("Save the required provider key first, or choose a local provider.")};guard choice.automaticChoice || !cleanEndpoint.isEmpty else{throw NativeSetupFailure("This provider lacks a chat adapter. An explicit supported gateway endpoint is required.")}}
        if case .complete=action{guard saved["provider"] as? String==selected,saved["model"] as? String==cleanModel,saved["base_url"] as? String==cleanEndpoint else{throw NativeSetupFailure("Activate and read back this exact provider/model/endpoint before completing setup.")}}
        return NativeSetupReview(generation:generation,origin:base,operation:action,provider:selected,model:cleanModel,endpoint:cleanEndpoint,secret:secret,config:saved,descriptor:choice.raw,setupComplete:setupComplete)
    }
    func canUse(_ item:NativeSetupReview)->Bool{!busy && generation==item.generation && baseURL==item.origin && !used.contains(item.id) && selected==item.provider && model.trimmingCharacters(in:.whitespacesAndNewlines)==item.model && (try? NativeSetupWire.endpoint(endpoint))==item.endpoint && ProcessInfo.processInfo.systemUptime-item.createdUptime<=300}
    @discardableResult func perform(_ item:NativeSetupReview)async->Bool {
        guard canUse(item) else{clearProbe();error="This setup review changed or was already used. No action was sent.";return false};clearProbe();used.insert(item.id);let started=generation,op=UUID();operation=op;busy=true;error=nil;notice=nil;var sent=false;var probeResult:Bool?
        defer{if operation==op{operation=nil;busy=false}}
        do{
            let fresh=try NativeSetupWire.config(try await request(item.origin,path:"/api/llm/config")),list=try NativeSetupWire.providers(try await request(item.origin,path:"/api/llm/providers"))
            guard generation==started,!Task.isCancelled else{return false};guard NativeSetupWire.equal(fresh,item.config),let choice=list.first(where:{$0.id==item.provider}),NativeSetupWire.equal(choice.raw,item.descriptor) else{throw NativeSetupFailure("Saved provider configuration or catalogue changed. Review the current selection again.")}
            let raw:[String:Any]
            switch item.operation{
            case .saveCredential:
                let vault=try await request(item.origin,path:"/api/security/vault/status");guard generation==started,!Task.isCancelled else{return false};guard vault["state"] as? String=="ready",NativeSetupWire.bool(vault["credentials_available"])==true,NativeSetupWire.bool(vault["in_flight"])==false else{throw NativeSetupFailure("Vault readiness changed. No credential write was sent.")}
                sent=true;raw=try await request(item.origin,path:"/api/llm/providers/"+item.provider+"/configure",body:["base_url":item.endpoint,"api_key":item.secret])
                guard NativeSetupWire.bool(raw["success"])==true,let state=raw["status"] as? [String:Any],state["id"] as? String==item.provider || state["provider_id"] as? String==item.provider,let persisted=raw["persisted"] as? [String:Any],NativeSetupWire.bool(persisted["ok"])==true else{throw NativeSetupFailure("Credential persistence was not verified; configuration may already have changed.")};notice="Backend reports provider credential persistence. Authorization, connectivity and model inference remain unverified."
            case .activate:
                sent=true;raw=try await request(item.origin,path:"/api/llm/config",body:["provider":item.provider,"model":item.model,"base_url":item.endpoint,"fallback_providers":item.config["fallback_providers"] ?? []])
                guard NativeSetupWire.bool(raw["success"])==true,raw["provider"] as? String==item.provider,raw["model"] as? String==item.model else{throw NativeSetupFailure("Provider activation receipt did not match the review.")}
                notice="Provider settings were acknowledged. Runtime activation and working model inference require separate verification."
            case .probe:
                sent=true;raw=try await request(item.origin,path:"/api/llm/providers/"+item.provider+"/probe",body:[:],probeProvider:item.provider);let reachable=try NativeSetupWire.probe(raw,provider:item.provider);probeResult=reachable;notice=reachable ? "Backend reports the saved provider reachable. A successful model response is still unverified." : "Backend reports the saved provider unreachable. Setup remains saved; no successful model response is claimed."
            case .complete:
                let setup=try await request(item.origin,path:"/api/setup/status");guard generation==started,!Task.isCancelled else{return false};guard NativeSetupWire.bool(setup["setup_complete"])==item.setupComplete else{throw NativeSetupFailure("Setup completion state changed. Review again.")};sent=true;raw=try await request(item.origin,path:"/api/setup/complete",body:[:]);guard NativeSetupWire.bool(raw["ok"])==true,NativeSetupWire.bool(raw["setup_complete"])==true else{throw NativeSetupFailure("Setup completion was not acknowledged.")};notice="Setup completion was acknowledged. Voice, access, pairing and sync remain deferred; provider inference is unverified."
            }
            guard generation==started else{return false}
            let readback=try NativeSetupWire.config(try await request(item.origin,path:"/api/llm/config"))
            if case .activate=item.operation{guard readback["provider"] as? String==item.provider,readback["model"] as? String==item.model,readback["base_url"] as? String==item.endpoint,NativeSetupWire.equal(["v":readback["fallback_providers"] ?? []],["v":item.config["fallback_providers"] ?? []]) else{throw NativeSetupFailure("Activated provider readback did not match the exact reviewed settings.")}}
            let providerReadback=try NativeSetupWire.providers(try await request(item.origin,path:"/api/llm/providers")),setup=try await request(item.origin,path:"/api/setup/status")
            guard generation==started,!Task.isCancelled else{return false};guard let complete=NativeSetupWire.bool(setup["setup_complete"]) else{throw NativeSetupFailure("Setup completion readback is unsupported.")}
            if case .complete=item.operation{guard complete else{throw NativeSetupFailure("Setup completion readback did not confirm completion.")}}
            if let result=probeResult{guard selected==item.provider,model.trimmingCharacters(in:.whitespacesAndNewlines)==item.model,(try? NativeSetupWire.endpoint(endpoint))==item.endpoint,NativeSetupWire.equal(readback,item.config),let descriptor=providerReadback.first(where:{$0.id==item.provider})?.raw,sameProbeDescriptor(descriptor,item.descriptor) else{throw NativeSetupFailure("Provider settings changed during the probe. Its result cannot certify this selection.")};probeProvider=item.provider;probeConfig=readback;probeDescriptor=descriptor;lastProbeReachable=result}
            saved=readback;providers=providerReadback;setupComplete=complete;return true
        }catch{guard generation==started else{return false};clearProbe();notice=nil;self.error=((error as? NativeSetupFailure)?.message ?? "Setup action could not be verified. Private provider details are withheld.")+(sent ? " The action may already have taken effect; refresh before preparing a new review." : "");return false}
    }
}
struct NativeOnboardingSetupFeatureView:View {
    let baseURL:URL?
    var onCompleted:()->Void={}
    @StateObject private var model=NativeOnboardingSetupModel()
    @State private var secret=""
    @State private var review:NativeSetupReview?
    @State private var localError:String?
    @State private var vaultPresented=false
    var body:some View {
        VStack(alignment:.leading,spacing:14){
            Text("Connect your assistant").font(.title.bold())
            Text("Choose a local or cloud model. You can change this later in AI Providers.").foregroundStyle(.secondary)
            if let error=localError ?? model.error{NativeSelectableText(error).foregroundStyle(.orange)}
            if let notice=model.notice{NativeSelectableText(notice).font(.callout)}
            if model.providers.isEmpty{Text("Waiting for your providers to load. If FERAL needs unlocking, open Security & Cost.").font(.caption).foregroundStyle(.secondary)}
            Picker("Chat provider",selection:Binding(get:{model.selected},set:{id in secret="";Task{await model.select(id)}})){Text("Choose provider").tag("");ForEach(model.providers.filter{$0.automaticChoice || $0.id==model.selected}){provider in Text(provider.name+(provider.local ? " · local" : " · cloud")+(provider.automaticChoice ? "" : " · manual gateway")).tag(provider.id)}}.disabled(model.busy)
            if model.providers.contains(where:{!$0.automaticChoice}){Menu("Configure a custom gateway…"){ForEach(model.providers.filter{!$0.automaticChoice}){provider in Button(provider.name){secret="";Task{await model.select(provider.id)}}}}.disabled(model.busy)}
            if let provider=model.providers.first(where:{$0.id==model.selected}){
                if !provider.automaticChoice{Text("This catalogue entry has no confirmed runtime adapter. Enter your compatible gateway endpoint explicitly; saved credentials and catalogue defaults do not establish support.").font(.caption).foregroundStyle(.orange)}
                Text(model.probeStatusText).font(.caption).foregroundStyle(model.lastProbeReachable==false ? .orange : .secondary)
                TextField("Model name",text:$model.model).textFieldStyle(.roundedBorder)
                if !model.models.isEmpty{Menu("Chat suggestions (\(model.modelsSource))"){ForEach(model.models,id:\.self){id in Button(id){model.model=id}}}}
                TextField("Provider endpoint (empty selects runtime default)",text:$model.endpoint).textFieldStyle(.roundedBorder)
                if let template=provider.endpointTemplate{NativeSelectableText("Workspace endpoint template (not a usable default): "+template).font(.caption);Text("Enter the resolved endpoint explicitly before activation or key storage.").font(.caption).foregroundStyle(.secondary)}
                if provider.needsKey{
                    if !model.vaultReady{NativeVaultSetupFeatureView(baseURL:baseURL,onReady:{Task{await model.refresh()}});Button("Unlock existing credential storage…"){secret="";vaultPresented=true}.disabled(model.busy)}
                    SecureField("Provider API key",text:$secret).textFieldStyle(.roundedBorder).disabled(!model.vaultReady)
                    Text(model.vaultReady ? "Encrypted vault authenticated; saving the key requires a separate review." : "Set up or unlock encrypted credential storage first. Local providers do not require a key.").font(.caption).foregroundStyle(.secondary)
                    Button("Review key storage…"){prepare(.saveCredential)}.disabled(model.busy || !model.vaultReady || secret.isEmpty)
                }
                HStack{Button("Review connection…"){prepare(.activate)};Button("Check saved connection…"){prepare(.probe)}}.disabled(model.busy)
            }
            Text("Successful inference, FERAL tool execution and voice are separate from saved configuration. You can connect voice and glasses later.").font(.caption).foregroundStyle(.secondary)
            HStack{Button("Refresh"){Task{await model.refresh()}}.disabled(model.busy);Spacer();Button("Finish setup…"){prepare(.complete)}.disabled(model.busy || model.selected.isEmpty);if model.busy{ProgressView().controlSize(.small)}}
        }.padding(24).task(id:baseURL){secret="";review=nil;model.configure(baseURL:baseURL);await model.refresh()}
        .sheet(item:$review){item in VStack(alignment:.leading,spacing:16){ScrollView{NativeReviewSummaryView(review:summary(item))};HStack{Button("Cancel"){review=nil;secret=""};Spacer();Button("Confirm reviewed step"){review=nil;secret="";Task{if await model.perform(item),case .complete=item.operation{onCompleted()}}}.disabled(!model.canUse(item))}}.padding(24).frame(width:680,height:520)}
        .sheet(isPresented:$vaultPresented,onDismiss:{Task{await model.refresh()}}){ScrollView{VStack(alignment:.leading,spacing:16){HStack{Text("Credential storage").font(.headline);Spacer();Button("Done"){vaultPresented=false}};NativeVaultFeatureView(baseURL:baseURL)}.padding(24)}.frame(width:760,height:680)}
    }
    private func summary(_ item:NativeSetupReview)->NativeReviewSummary {
        let effect:String,scope:[String]
        switch item.operation {
        case .saveCredential:
            effect="Stores your entered key in the existing encrypted vault and updates the provider configuration."
            scope=["The provider adapter/environment and its endpoint can change. This does not verify provider authorization or a successful model response."]
        case .activate:
            effect="Saves this provider, model and endpoint for chat."
            scope=["Activation can contact the provider or gateway. Existing fallback configuration is preserved; a successful model response remains unverified."]
        case .probe:
            effect="Contacts the provider using its saved configuration and credentials."
            scope=["Draft endpoint/model changes are not sent. A provider-specific saved override may determine the destination; this is not a model inference test."]
        case .complete:
            effect="Marks provider setup complete after verifying the saved selection."
            scope=["Voice, permissions, access mode and device pairing remain deferred. Completion does not prove working inference or finish blocked agent startup."]
        }
        let endpointLabel:String;if case .probe=item.operation{endpointLabel="Draft endpoint (not sent by probe)"}else{endpointLabel="Provider endpoint"}
        return NativeReviewSummary(action:item.title,effect:effect,materialScope:scope,
            targets:[.init(label:"Local service",value:item.origin.absoluteString),.init(label:"Provider / model",value:item.provider+" / "+item.model),.init(label:endpointLabel,value:item.endpoint.isEmpty ? "Provider runtime default" : item.endpoint)],details:item.explanation)
    }
    private func prepare(_ action:NativeSetupOperation){do{localError=nil;review=try model.review(action,secret:secret)}catch{localError=(error as? NativeSetupFailure)?.message ?? "This setup step could not be reviewed."}}
}
