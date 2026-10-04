import SwiftUI
import Foundation
import CoreFoundation

struct NativeSurfaceFailure: LocalizedError {
    let message:String
    init(_ message:String) { self.message = message }
    var errorDescription:String? { message }
}
struct NativeSurfaceNode: Identifiable {
    let id:String
    let type:String
    let raw:[String:Any]
    let children:[NativeSurfaceNode]
    let supported:Bool
    var actionID:String? { supported && ["Button","TextField","Checkbox","Toggle","Form","FormView"].contains(type) ? raw["action_id"] as? String : nil }
    var actionIDs:Set<String> { children.reduce(actionID.map { Set([$0]) } ?? []) { $0.union($1.actionIDs) } }
}
enum NativeSurfaceWire {
    static let maximumBytes = 262144, maximumNodes = 256, maximumDepth = 16
    static let types:Set<String> = ["VStack","Column","HStack","Row","Card","ScrollView","Text","Markdown","CodeBlock","Badge","Button","TextField","Checkbox","Toggle","Form","FormView","ProgressBar","Divider","Spacer"]
    static func bool(_ value:Any?) -> Bool? { guard let n = value as? NSNumber,CFGetTypeID(n) == CFBooleanGetTypeID() else { return nil };return n.boolValue }
    static func number(_ value:Any?) -> Double? { guard let n = value as? NSNumber,CFGetTypeID(n) != CFBooleanGetTypeID(),n.doubleValue.isFinite else { return nil };return n.doubleValue }
    static func json(_ value:Any) -> String { guard JSONSerialization.isValidJSONObject(value),let data = try? JSONSerialization.data(withJSONObject:value,options:[.sortedKeys,.prettyPrinted]),let text = String(data:data,encoding:.utf8) else { return "Unreadable metadata" };return text }
    static func same(_ a:Any,_ b:Any) -> Bool { JSONSerialization.isValidJSONObject(a) && JSONSerialization.isValidJSONObject(b) && json(a) == json(b) }
    static func segment(_ value:String) -> String { value.addingPercentEncoding(withAllowedCharacters:CharacterSet(charactersIn:"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")) ?? "" }
    static func screen(app:String,surface:String,session:String) -> String { [app,surface,session].map(segment).joined(separator:":") }
    static func identifier(_ value:Any?) -> String? { guard let id = value as? String,!id.isEmpty,id.count <= 256 else { return nil };return id }
    static func rows(_ body:[String:Any],key:String,idKey:String) throws -> [[String:Any]] {
        guard let rows = body[key] as? [[String:Any]],rows.count <= 500 else { throw NativeSurfaceFailure("The app inventory is unreadable.") }
        let ids = rows.compactMap { identifier($0[idKey]) }
        guard ids.count == rows.count,Set(ids).count == ids.count else { throw NativeSurfaceFailure("App identifiers are missing or duplicated.") };return rows
    }
    static func manifest(_ body:[String:Any],app:String) throws -> [String:Any] {
        guard body["app_id"] as? String == app,let value = body["manifest"] as? [String:Any],value["app_id"] as? String == app,
              let version = value["version"] as? String,body["version"] as? String == version,!version.isEmpty else { throw NativeSurfaceFailure("The app manifest does not identify the selected app/version.") }
        let surfaces = try rows(value,key:"surfaces",idKey:"surface_id")
        guard let entry = identifier(value["entry_surface_id"]),surfaces.contains(where:{$0["surface_id"] as? String == entry}) else { throw NativeSurfaceFailure("The app has no declared entry surface.") }
        for surface in surfaces {
            _ = try rows(surface,key:"action_contract",idKey:"action_id")
            guard ["authored","generated","hybrid"].contains(surface["kind"] as? String ?? "") else { throw NativeSurfaceFailure("A surface kind is not supported by the declared contract.") }
        }
        return value
    }
    static func tree(_ raw:[String:Any]) throws -> NativeSurfaceNode {
        guard JSONSerialization.isValidJSONObject(raw),let data = try? JSONSerialization.data(withJSONObject:raw),data.count <= maximumBytes else { throw NativeSurfaceFailure("Surface exceeds the 256 KiB rendering budget.") }
        var nodes = 0,textBytes = 0
        func countText(_ value:Any,depth:Int) throws {
            guard depth <= maximumDepth else { throw NativeSurfaceFailure("Surface metadata exceeds the native depth budget.") }
            if let text = value as? String { textBytes += text.utf8.count }
            else if let object = value as? [String:Any] { for child in object.values { try countText(child,depth:depth+1) } }
            else if let array = value as? [Any] { for child in array { try countText(child,depth:depth+1) } }
            guard textBytes <= 65536 else { throw NativeSurfaceFailure("Surface text exceeds the native rendering budget.") }
        }
        func visit(_ raw:[String:Any],path:String,depth:Int) throws -> NativeSurfaceNode {
            nodes += 1
            guard nodes <= maximumNodes,depth <= maximumDepth else { throw NativeSurfaceFailure("Surface exceeds the node/depth rendering budget.") }
            for (key,value) in raw where key != "children" { try countText(value,depth:0) }
            let type = raw["type"] as? String ?? "Unknown"
            var supported = types.contains(type)
            if type == "TextField",!["text","email","search","number"].contains(raw["input_type"] as? String ?? "text") { supported = false }
            if type == "Form" || type == "FormView" {
                guard let fields = raw["fields"] as? [[String:Any]],fields.count <= 32 else { throw NativeSurfaceFailure("Form fields exceed the native budget or are unreadable.") }
                let names = fields.compactMap { identifier($0["name"]) }
                guard names.count == fields.count,Set(names).count == names.count else { throw NativeSurfaceFailure("Form field names must be unique and declared.") }
                for field in fields {
                    if !["text","number","email","textarea","checkbox","select"].contains(field["type"] as? String ?? "text") { supported = false }
                    if field["type"] as? String == "select" {
                        guard let options = field["options"] as? [String],options.count <= 64,Set(options).count == options.count else { supported = false;continue }
                    }
                }
            }
            var children:[NativeSurfaceNode] = []
            if let values = raw["children"] {
                guard let list = values as? [[String:Any]] else { throw NativeSurfaceFailure("Surface children must be declared node objects.") }
                for (index,child) in list.enumerated() { children.append(try visit(child,path:path + ".\(index)",depth:depth+1)) }
            }
            // Unsupported containers never make hidden descendants interactive.
            return NativeSurfaceNode(id:path,type:type,raw:raw,children:supported ? children : [],supported:supported)
        }
        return try visit(raw,path:"root",depth:0)
    }
    static func schema(_ spec:[String:Any],manifest:[String:Any]) throws -> [String:Any]? {
        if let inline = spec["value_schema"] as? [String:Any] { return inline }
        if let ref = spec["value_schema_ref"] as? String,!ref.isEmpty {
            let id = ref.hasPrefix("#/data_schemas/") ? String(ref.dropFirst("#/data_schemas/".count)) : ref
            let schemas = try rows(manifest,key:"data_schemas",idKey:"schema_id")
            guard let row = schemas.first(where:{$0["schema_id"] as? String == id}),let schema = row["schema"] as? [String:Any] else { throw NativeSurfaceFailure("The action’s declared value schema cannot be resolved.") };return schema
        }
        return nil
    }
    static func validate(_ value:Any,schema:[String:Any],depth:Int = 0) throws {
        guard depth <= 8,schema.count <= 20 else { throw NativeSurfaceFailure("Action schema exceeds the native validation budget.") }
        let allowed:Set<String> = ["type","properties","required","additionalProperties","items","enum","minLength","maxLength","minimum","maximum","minItems","maxItems","description","title","default"]
        guard Set(schema.keys).isSubset(of:allowed),let type = schema["type"] as? String else { throw NativeSurfaceFailure("This schema uses unsupported rules; the action is inspection only.") }
        for key in ["minLength","maxLength","minimum","maximum","minItems","maxItems"] {
            if let raw = schema[key] { guard number(raw) != nil else { throw NativeSurfaceFailure("Malformed numeric schema constraint; inspection only.") } }
        }
        for key in ["minLength","maxLength","minItems","maxItems"] {
            if let n = number(schema[key]),n < 0 || n.rounded(.towardZero) != n { throw NativeSurfaceFailure("Malformed length/count constraint; inspection only.") }
        }
        if let raw = schema["enum"] { guard let choices = raw as? [Any],!choices.isEmpty,choices.count <= 64 else { throw NativeSurfaceFailure("Malformed choice schema; inspection only.") } }
        if let raw = schema["required"] { guard raw is [String] else { throw NativeSurfaceFailure("Malformed required-field schema; inspection only.") } }
        if let raw = schema["properties"] { guard raw is [String:[String:Any]] else { throw NativeSurfaceFailure("Malformed property schema; inspection only.") } }
        if let raw = schema["items"] { guard raw is [String:Any] else { throw NativeSurfaceFailure("Unsupported item schema; inspection only.") } }
        if let raw = schema["additionalProperties"] { guard bool(raw) != nil || raw is [String:Any] else { throw NativeSurfaceFailure("Malformed additional-field schema; inspection only.") } }
        if let choices = schema["enum"] as? [Any],!choices.contains(where:{ same(["v":$0],["v":value]) }) { throw NativeSurfaceFailure("The value is not in the declared choice list.") }
        switch type {
        case "null":guard value is NSNull else { throw NativeSurfaceFailure("This action requires a null value.") }
        case "boolean":guard bool(value) != nil else { throw NativeSurfaceFailure("This action requires a boolean value.") }
        case "string":
            guard let text = value as? String,text.utf8.count <= 16384 else { throw NativeSurfaceFailure("This action requires bounded text.") }
            if let min = number(schema["minLength"]),Double(text.count) < min { throw NativeSurfaceFailure("Text is shorter than the declared minimum.") }
            if let max = number(schema["maxLength"]),Double(text.count) > max { throw NativeSurfaceFailure("Text is longer than the declared maximum.") }
        case "number","integer":
            guard let n = number(value),type != "integer" || n.rounded(.towardZero) == n else { throw NativeSurfaceFailure("This action requires a declared numeric value.") }
            if let min = number(schema["minimum"]),n < min { throw NativeSurfaceFailure("Value is below the declared minimum.") }
            if let max = number(schema["maximum"]),n > max { throw NativeSurfaceFailure("Value exceeds the declared maximum.") }
        case "object":
            guard let object = value as? [String:Any],object.count <= 64 else { throw NativeSurfaceFailure("This action requires a bounded object.") }
            let properties = schema["properties"] as? [String:[String:Any]] ?? [:]
            for key in schema["required"] as? [String] ?? [] { guard object[key] != nil else { throw NativeSurfaceFailure("Required form field is missing.") } }
            for (key,child) in object {
                if let field = properties[key] { try validate(child,schema:field,depth:depth+1) }
                else if bool(schema["additionalProperties"]) == false { throw NativeSurfaceFailure("An undeclared form field was supplied.") }
                else if let extra = schema["additionalProperties"] as? [String:Any] { try validate(child,schema:extra,depth:depth+1) }
            }
        case "array":
            guard let array = value as? [Any],array.count <= 64 else { throw NativeSurfaceFailure("This action requires a bounded array.") }
            if let min = number(schema["minItems"]),Double(array.count) < min { throw NativeSurfaceFailure("Too few values were supplied.") }
            if let max = number(schema["maxItems"]),Double(array.count) > max { throw NativeSurfaceFailure("Too many values were supplied.") }
            if let items = schema["items"] as? [String:Any] { for child in array { try validate(child,schema:items,depth:depth+1) } }
        default:throw NativeSurfaceFailure("Unsupported value type; this action is inspection only.")
        }
    }
    static func contract(_ manifest:[String:Any],surface:String,action:String,value:Any) throws -> [String:Any] {
        guard !["confirm_","reject_","perm_grant_","perm_deny_"].contains(where:{action.hasPrefix($0)}),action.range(of:"^[A-Za-z][A-Za-z0-9_.:-]{0,119}$",options:.regularExpression) != nil,
              let surfaces = manifest["surfaces"] as? [[String:Any]],let row = surfaces.first(where:{$0["surface_id"] as? String == surface}),
              let actions = row["action_contract"] as? [[String:Any]],let spec = actions.first(where:{$0["action_id"] as? String == action}) else { throw NativeSurfaceFailure("This action is not an executable declared surface contract.") }
        let handler = spec["handler"] as? String ?? ""
        guard ["navigate","close","skill_call","app_event"].contains(handler),bool(spec["requires_confirmation"]) != nil else { throw NativeSurfaceFailure("Unsupported or opaque action handler; inspection only.") }
        let target = spec["target"] as? String ?? ""
        if handler == "navigate" { guard surfaces.contains(where:{$0["surface_id"] as? String == target}) else { throw NativeSurfaceFailure("Navigation target is not a declared surface.") } }
        if handler == "skill_call" { guard target.range(of:"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",options:.regularExpression) != nil else { throw NativeSurfaceFailure("The skill endpoint target is not a supported contract.") } }
        if ["close","app_event"].contains(handler),!target.isEmpty { throw NativeSurfaceFailure("Opaque action targets are inspection only.") }
        if let schema = try schema(spec,manifest:manifest) { try validate(value,schema:schema) }
        else if !(value is NSNull) { throw NativeSurfaceFailure("Non-null input requires a declared supported value schema; inspection only.") }
        return spec
    }
}
final class NativeSurfaceRedirectGuard:NSObject,URLSessionTaskDelegate {
    func urlSession(_ session:URLSession,task:URLSessionTask,willPerformHTTPRedirection response:HTTPURLResponse,newRequest request:URLRequest,completionHandler:@escaping(URLRequest?) -> Void) { completionHandler(nil) }
    static func session() -> URLSession { let config = URLSessionConfiguration.ephemeral;config.timeoutIntervalForResource = 180;return URLSession(configuration:config,delegate:NativeSurfaceRedirectGuard(),delegateQueue:nil) }
}
struct NativeSurfaceClient {
    let baseURL:URL
    let session:URLSession
    func request(_ path:String,body:[String:Any]? = nil) async throws -> [String:Any] {
        guard baseURL.scheme == "http",["127.0.0.1","::1","[::1]"].contains(baseURL.host ?? ""),baseURL.user == nil,baseURL.password == nil,var components = URLComponents(url:baseURL,resolvingAgainstBaseURL:false) else { throw NativeSurfaceFailure("App surfaces require the local app service.") }
        components.percentEncodedPath = path;components.query = nil;components.fragment = nil
        guard let url = components.url else { throw NativeSurfaceFailure("Invalid surface request.") }
        var request = URLRequest(url:url);request.httpMethod = body == nil ? "GET" : "POST"
        if let body = body { guard JSONSerialization.isValidJSONObject(body) else{throw NativeSurfaceFailure("The app request body is not valid JSON. No request was sent.")};request.httpBody = try JSONSerialization.data(withJSONObject:body);request.setValue("application/json",forHTTPHeaderField:"Content-Type") }
        let (data,response) = try await session.feralLocalData(for:request)
        guard data.count <= 1048576,let http = response as? HTTPURLResponse,(200..<300).contains(http.statusCode),let value = (try? JSONSerialization.jsonObject(with:data)) as? [String:Any],value["error"] == nil,NativeSurfaceWire.bool(value["success"]) != false else { throw NativeSurfaceFailure("The local app request failed or returned an unreadable receipt. Private service details are withheld.") }
        return value
    }
}
protocol NativeSurfaceSocket:AnyObject {
    func resume()
    func send(_ frame:[String:Any]) async throws
    func receive() async throws -> [String:Any]
    func cancel()
}
final class NativeSurfaceWebSocket:NativeSurfaceSocket {
    private let session:URLSession
    private let task:URLSessionWebSocketTask
    init(url:URL) {
        let created=NativeSurfaceRedirectGuard.session();session=created;task=created.webSocketTask(with:url)
        task.maximumMessageSize=262144
    }
    func resume(){task.resume()}
    func send(_ frame:[String:Any]) async throws {
        guard JSONSerialization.isValidJSONObject(frame) else{throw NativeSurfaceFailure("The app-session response is not valid JSON. No response was sent.")}
        let data=try JSONSerialization.data(withJSONObject:frame)
        try await task.send(.string(String(decoding:data,as:UTF8.self)))
    }
    func receive() async throws -> [String:Any] {
        let message=try await task.receive(),data:Data
        switch message{case .string(let text):data=Data(text.utf8);case .data(let bytes):data=bytes;@unknown default:throw NativeSurfaceFailure("Unsupported app-session message.")}
        guard data.count<=262144,let frame=(try? JSONSerialization.jsonObject(with:data)) as? [String:Any] else{throw NativeSurfaceFailure("App-session message exceeded the wire budget or was unreadable.")};return frame
    }
    func cancel(){task.cancel(with:.goingAway,reason:nil);session.invalidateAndCancel()}
}
struct NativeSurfaceTranscriptRow:Identifiable {let id=UUID();let role:String;var text:String}
enum NativeSurfaceAction { case open(surface:String,regenerate:Bool,data:[String:Any]);case dispatch(action:String,value:Any) }
struct NativeSurfaceReview:Identifiable {
    let id = UUID()
    let generation:UUID
    let origin:String
    let sessionID:String
    let appID:String
    let manifest:[String:Any]
    let surfaceID:String
    let action:NativeSurfaceAction
    let title:String
    let explanation:String
}
private struct NativeSurfaceNavigationReceipt {
    let id:UUID,sourceRevision:UUID,connectionID:UUID
    let origin:URL,sessionID:String,appID:String,target:String
    let manifest:[String:Any],contract:[String:Any]
    let createdUptime=ProcessInfo.processInfo.systemUptime
}
@MainActor final class NativeAppSurfaceModel:ObservableObject {
    @Published private(set) var apps:[[String:Any]]?
    @Published private(set) var manifest:[String:Any]?
    @Published private(set) var appID = ""
    @Published private(set) var surfaceID = ""
    @Published private(set) var root:NativeSurfaceNode?
    @Published private(set) var renderIdentity = UUID()
    @Published private(set) var loading = false
    @Published private(set) var acting = false
    @Published private(set) var error:String?
    @Published private(set) var receipt:String?
    @Published private(set) var sessionStatus="Disconnected"
    @Published private(set) var transcript:[NativeSurfaceTranscriptRow]=[]
    let richChat=NativeRichChatModel()
    let appConfirmations=NativeAppConfirmationModel()
    private let session:URLSession
    private let socketFactory:(URL)->NativeSurfaceSocket
    private var socket:NativeSurfaceSocket?
    private var socketIdentity=UUID()
    private var receiveTask:Task<Void,Never>?
    private var handshakeID:String?
    private var handshakeAcknowledged=false
    private var streamingTranscriptID:UUID?
    private var pendingNavigation:NativeSurfaceNavigationReceipt?
    private var activeOperation:UUID?
    private var surfaceUpdates:NativeSurfaceUpdateReducer?
    private var confirmationCloseMounts:[String:(renderIdentity:UUID,manifest:[String:Any])]=[:]
    private var client:NativeSurfaceClient?
    private var sessionID:String?
    private var generation = UUID(),read = UUID()
    private var used:Set<UUID> = []
    init(session:URLSession? = nil,socketFactory:@escaping(URL)->NativeSurfaceSocket = {NativeSurfaceWebSocket(url:$0)}) { self.session = session ?? NativeSurfaceRedirectGuard.session();self.socketFactory=socketFactory }
    var available:Bool { client != nil }
    var surfaces:[[String:Any]] { manifest?["surfaces"] as? [[String:Any]] ?? [] }
    func configure(baseURL:URL?,sessionID:String?) async {
        closeSession()
        generation = UUID();read = UUID();used = [];client = baseURL.map { NativeSurfaceClient(baseURL:$0,session:session) };self.sessionID = sessionID
        apps = nil;manifest = nil;appID = "";surfaceID = "";root = nil;loading = false;acting = activeOperation != nil;error = nil;receipt = nil;transcript=[]
        if available { await refresh(allowActiveOperation:true) }
    }
    func refresh(allowActiveOperation:Bool=false) async {
        guard let client = client,!acting || allowActiveOperation else { return }
        generation = UUID();read = UUID();let revision = read,connection = generation;loading = true
        pendingNavigation=nil;surfaceUpdates=nil;root = nil;surfaceID = "";manifest = nil;appID = ""
        defer { if revision == read,connection == generation { loading = false } }
        do {
            let rows = try NativeSurfaceWire.rows(try await client.request("/api/apps"),key:"apps",idKey:"app_id")
            guard revision == read,connection == generation else { return };apps = rows;error = nil
        } catch { guard revision == read,connection == generation else { return };apps = nil;self.error = "Installed apps could not be read. No app has been opened." }
    }
    func inspect(_ id:String) async {
        guard let client = client,!acting,apps?.contains(where:{$0["app_id"] as? String == id}) == true else { return }
        generation = UUID();read = UUID();let revision = read,connection = generation;loading = true
        pendingNavigation=nil;surfaceUpdates=nil;root = nil;surfaceID = "";manifest = nil;appID = id;error = nil;receipt = nil
        defer { if revision == read,connection == generation { loading = false } }
        do {
            let body = try await client.request("/api/apps/" + NativeSurfaceWire.segment(id) + "/manifest")
            let value = try NativeSurfaceWire.manifest(body,app:id)
            guard revision == read,connection == generation else { return };manifest = value
        } catch { guard revision == read,connection == generation else { return };self.error = "The app’s declared manifest could not be verified. No surface was opened." }
    }
    func dismissSurface() { guard !acting else { return };generation = UUID();pendingNavigation=nil;surfaceUpdates=nil;root = nil;surfaceID = "";receipt = "Surface dismissed locally. This does not uninstall the app or undo its actions." }
    func closeSession() {
        confirmationCloseMounts=[:];pendingNavigation=nil;surfaceUpdates=nil
        generation=UUID();receiveTask?.cancel();receiveTask=nil;socket?.cancel();socket=nil;socketIdentity=UUID();handshakeID=nil;handshakeAcknowledged=false;streamingTranscriptID=nil
        richChat.configure(sessionID:nil,connectionID:nil);appConfirmations.configure(sessionID:nil,connectionID:nil);sessionStatus="Disconnected";acting=activeOperation != nil
    }
    private func connectSession(connection:UUID,reviewSession:String) async throws {
        guard connection==generation,reviewSession==sessionID,let client=client else{throw NativeSurfaceFailure("The app session changed before connecting.")}
        if socket != nil && handshakeAcknowledged{return}
        receiveTask?.cancel();socket?.cancel();socket=nil
        var components=URLComponents(url:client.baseURL,resolvingAgainstBaseURL:false)!
        components.scheme="ws";components.path="/v1/session";components.queryItems=[URLQueryItem(name:"session_id",value:reviewSession)];components.fragment=nil
        guard let url=components.url else{throw NativeSurfaceFailure("Invalid app-session connection.")}
        let current=socketFactory(url),identity=UUID(),probe=UUID().uuidString
        socket=current;socketIdentity=identity;handshakeID=probe;handshakeAcknowledged=false;sessionStatus="Connecting";current.resume()
        richChat.configure(sessionID:reviewSession,connectionID:identity)
        appConfirmations.configure(sessionID:reviewSession,connectionID:identity)
        confirmationCloseMounts=[:]
        receiveTask=Task{[weak self] in
            while !Task.isCancelled {
                do {
                    let frame=try await current.receive()
                    guard let self,!Task.isCancelled,self.socketIdentity==identity,self.sessionID==reviewSession else{return}
                    await self.consumeSessionFrame(frame,identity:identity,sessionID:reviewSession)
                }catch{
                    guard let self,!Task.isCancelled,self.socketIdentity==identity else{return}
                    self.handshakeAcknowledged=false;self.sessionStatus="Disconnected";self.richChat.configure(sessionID:nil,connectionID:nil)
                    self.appConfirmations.configure(sessionID:nil,connectionID:nil)
                    self.surfaceUpdates=nil;self.pendingNavigation=nil
                    self.error="App session disconnected. Replies and approvals are unavailable; an earlier REST action may still be running. Review before reconnecting."
                    return
                }
            }
        }
        // A transport-open callback alone does not prove state.sessions owns this
        // socket. A successful passive gateway response proves the registered
        // application loop has processed our exact socket request.
        try await current.send(["type":"req","id":probe,"method":"config.get","params":[:]])
        for _ in 0..<100 {
            guard connection==generation,reviewSession==sessionID,socketIdentity==identity else{throw NativeSurfaceFailure("The app session changed while connecting.")}
            if handshakeAcknowledged{return}
            if sessionStatus=="Disconnected"{break}
            try await Task.sleep(nanoseconds:50_000_000)
        }
        if socketIdentity==identity{receiveTask?.cancel();receiveTask=nil;current.cancel();socket=nil;socketIdentity=UUID();handshakeID=nil;handshakeAcknowledged=false;sessionStatus="Disconnected";richChat.configure(sessionID:nil,connectionID:nil);appConfirmations.configure(sessionID:nil,connectionID:nil)}
        throw NativeSurfaceFailure("The dedicated app session did not acknowledge registration. No app action was sent.")
    }
    func consumeSessionFrame(_ frame:[String:Any],identity:UUID,sessionID sid:String) async {
        guard socketIdentity==identity,sessionID==sid,socket != nil else{return}
        if frame["type"] as? String=="res",frame["id"] as? String==handshakeID {
            if NativeSurfaceWire.bool(frame["ok"])==true{handshakeAcknowledged=true;sessionStatus="Connected"}else{
                handshakeAcknowledged=false;sessionStatus="Disconnected"
                richChat.configure(sessionID:nil,connectionID:nil);appConfirmations.configure(sessionID:nil,connectionID:nil)
            }
            return
        }
        let type=frame["type"] as? String ?? "",payload=frame["payload"] as? [String:Any] ?? [:]
        guard frame["session_id"] as? String==sid,(payload["session_id"] as? String).map({$0==sid}) ?? true else{return}
        guard JSONSerialization.isValidJSONObject(frame),let data=try? JSONSerialization.data(withJSONObject:frame),data.count<=65536 else{appendTranscript("notice","An app-session frame exceeded the display budget and was not rendered.");return}
        var safe=frame
        if type=="error"{safe["payload"]=["message":"The app agent reported an error. Private service details are withheld."]}
        if type=="stream_delta",payload["kind"] as? String=="reasoning",richChat.reasoning.utf8.count>=65536{return}
        _=richChat.consume(safe,connectionID:identity)
        // Confirmation authority belongs to the dedicated socket, not to the
        // currently selected manifest or its generated component action IDs.
        let confirmationHandled=appConfirmations.consume(safe,connectionID:identity)
        if confirmationHandled,let requestID=(payload["confirmation"] as? [String:Any])?["request_id"] as? String,
           confirmationCloseMounts[requestID]==nil,let request=appConfirmations.requests.first(where:{$0.id==requestID && $0.state=="pending"}),
           request.metadata["handler"] as? String=="close",request.appID==appID,request.surfaceID==surfaceID,root != nil,let mounted=manifest,
           let contract=try? NativeSurfaceWire.contract(mounted,surface:surfaceID,action:request.actionID,value:request.metadata["value"] ?? NSNull()),
           contract["handler"] as? String=="close",NativeSurfaceWire.bool(contract["requires_confirmation"])==true {
            confirmationCloseMounts[requestID]=(renderIdentity,mounted)
        }
        if type=="confirmation_decision",confirmationHandled,let requestID=payload["request_id"] as? String,
           let mounted=confirmationCloseMounts.removeValue(forKey:requestID),let currentManifest=manifest,
           mounted.renderIdentity==renderIdentity,NativeSurfaceWire.same(mounted.manifest,currentManifest),root != nil,
           let request=appConfirmations.requests.first(where:{$0.id==requestID && $0.state=="accepted_dispatch"}),
           request.metadata["handler"] as? String=="close",request.appID==appID,request.surfaceID==surfaceID {
            // This is the acknowledged close lifecycle, not verified tool work.
            // The owning mount snapshot prevents replay from closing a new root.
            generation=UUID();pendingNavigation=nil;surfaceUpdates=nil;root=nil;surfaceID="";renderIdentity=UUID()
            receipt="The backend accepted this surface’s close dispatch. The local surface was dismissed; tool completion remains unverified."
        }
        if !confirmationHandled,["sdui","sdui_patch"].contains(type){await applySurfaceUpdate(safe,identity:identity,sessionID:sid)}
        if ["text_response","chat_response"].contains(type),let text=payload["text"] as? String,!text.isEmpty {
            if let id=streamingTranscriptID,let index=transcript.firstIndex(where:{$0.id==id}){transcript[index].text=String(text.prefix(16384))}else{appendTranscript("assistant",text)}
            streamingTranscriptID=nil
        }else if type=="stream_delta",payload["kind"] as? String != "reasoning"{
            let delta=payload["delta"] as? String ?? ""
            if let id=streamingTranscriptID,let index=transcript.firstIndex(where:{$0.id==id}){transcript[index].text=String((transcript[index].text+delta).prefix(16384))}
            else if !delta.isEmpty{appendTranscript("assistant",delta);streamingTranscriptID=transcript.last?.id}
            if NativeSurfaceWire.bool(payload["is_final"])==true{streamingTranscriptID=nil}
        }else if type=="error"{appendTranscript("error","The app agent reported an error; private service details are withheld.");streamingTranscriptID=nil}
        else if type=="sdui",confirmationHandled{appendTranscript("notice","A structured app-confirmation frame was received. See its confirmation card or inspection notice below; receiving a prompt does not execute the action.")}
        else if type=="confirmation_decision",confirmationHandled{appendTranscript("notice","An app-action decision receipt was received. Its confirmation card shows the acknowledged status; tool completion remains unverified.")}
        else if type=="sdui" || type=="sdui_patch"{appendTranscript("notice","Structured app-session update received for inspection. Open a declared surface to render it; opaque confirmations and patches are not executed automatically.")}
    }
    private func applySurfaceUpdate(_ frame:[String:Any],identity:UUID,sessionID sid:String) async {
        guard let client=client,var candidate=surfaceUpdates,root != nil,handshakeAcknowledged,
              candidate.connectionID==identity,candidate.sessionID==sid else{return}
        let mountedRevision=candidate.revision
        let navigation=pendingNavigation
        let allowedTarget:String?
        if let navigation=navigation,navigation.sourceRevision==mountedRevision,navigation.connectionID==identity,navigation.sessionID==sid,navigation.origin==client.baseURL,navigation.appID==candidate.appID,
           NativeSurfaceWire.same(navigation.manifest,candidate.manifest),ProcessInfo.processInfo.systemUptime-navigation.createdUptime<=300{allowedTarget=navigation.target}else{allowedTarget=nil}
        do {
            // Validate owner/screen/path/tree before any passive manifest read.
            guard try candidate.consume(frame,origin:client.baseURL,sessionID:sid,connectionID:identity,allowedNavigationTarget:allowedTarget) else{return}
            let fresh=try NativeSurfaceWire.manifest(try await client.request("/api/apps/"+NativeSurfaceWire.segment(candidate.appID)+"/manifest"),app:candidate.appID)
            guard socketIdentity==identity,sessionID==sid,client.baseURL==self.client?.baseURL,appID==candidate.appID,surfaceUpdates?.revision==mountedRevision,root != nil else{return}
            guard NativeSurfaceWire.same(fresh,candidate.manifest) else {
                pendingNavigation=nil;surfaceUpdates=nil;root=nil;surfaceID="";renderIdentity=UUID();generation=UUID();confirmationCloseMounts=[:];manifest=fresh
                error="The app contract changed before its surface update. The old surface was dismissed; review and open the current declared surface again."
                return
            }
            if candidate.surfaceID != surfaceID {
                guard let navigation=navigation,pendingNavigation?.id==navigation.id,allowedTarget==candidate.surfaceID else{return}
            }
            pendingNavigation=nil;surfaceUpdates=candidate;root=candidate.node;surfaceID=candidate.surfaceID;renderIdentity=candidate.revision;generation=UUID();confirmationCloseMounts=[:]
            receipt="An owning app-session surface update was validated and mounted. Earlier action reviews and local drafts were invalidated; tool completion remains unverified."
        } catch {
            guard socketIdentity==identity,sessionID==sid,client.baseURL==self.client?.baseURL,appID==candidate.appID,surfaceUpdates?.revision==mountedRevision else{return}
            self.error="The app-session surface update could not be validated. The previous surface was retained; private service details are withheld."
        }
    }
    private func appendTranscript(_ role:String,_ text:String) {
        transcript.append(NativeSurfaceTranscriptRow(role:role,text:String(text.prefix(16384))))
        while transcript.count>100 || transcript.reduce(0,{$0+$1.text.utf8.count})>262144{transcript.removeFirst()}
    }
    func respondToPermission(_ response:NativeRichPermissionResponse) async throws {
        guard let socket=socket,handshakeAcknowledged,response.sessionID==sessionID,response.connectionID==socketIdentity,
              richChat.permissions.contains(where:{$0.id==response.requestID && $0.session==sessionID && $0.supported && !$0.expired && $0.state=="responding"}) else{throw NativeSurfaceFailure("The app-session permission request or socket changed.")}
        try await socket.send(response.wireFrame)
    }
    func respondToAppConfirmation(_ response:NativeAppConfirmationResponse) async throws {
        guard let socket=socket,handshakeAcknowledged,response.sessionID==sessionID,
              response.connectionID==socketIdentity,appConfirmations.isLiveResponse(response) else{
            throw NativeSurfaceFailure("The owning app-action confirmation or app-session socket changed. No response was sent.")
        }
        if response.confirm,let request=appConfirmations.requests.first(where:{$0.id==response.requestID}),request.metadata["handler"] as? String=="navigate",
           request.appID==appID,request.surfaceID==surfaceID,let updates=surfaceUpdates,let mounted=manifest,
           let spec=try? NativeSurfaceWire.contract(mounted,surface:surfaceID,action:request.actionID,value:request.metadata["value"] ?? NSNull()),
           spec["handler"] as? String=="navigate",NativeSurfaceWire.bool(spec["requires_confirmation"])==true,
           let target=spec["target"] as? String,target==request.metadata["target"] as? String,let client=client {
            pendingNavigation=NativeSurfaceNavigationReceipt(id:UUID(),sourceRevision:updates.revision,connectionID:socketIdentity,origin:client.baseURL,sessionID:response.sessionID,appID:appID,target:target,manifest:mounted,contract:spec)
        }
        do{try await socket.send(response.wireFrame)}catch{pendingNavigation=nil;throw error}
    }
    func review(_ action:NativeSurfaceAction,mountedRevision:UUID?=nil) throws -> NativeSurfaceReview {
        if let mountedRevision=mountedRevision,mountedRevision != renderIdentity{throw NativeSurfaceFailure("The mounted surface changed before this control callback. Review the current native surface again.")}
        guard let client = client,let manifest = manifest,!loading,!acting,let sid = sessionID,!sid.isEmpty,sid.count <= 256 else { throw NativeSurfaceFailure("Wait for a declared app manifest and an active conversation ID.") }
        let selected:String,title:String,explanation:String
        switch action {
        case .open(let surface,let regenerate,let data):
            guard let spec = surfaces.first(where:{$0["surface_id"] as? String == surface}),JSONSerialization.isValidJSONObject(data),let payload = try? JSONSerialization.data(withJSONObject:data),payload.count <= 65536 else { throw NativeSurfaceFailure("Select a declared surface and bounded JSON data.") }
            selected = surface;title = regenerate ? "Regenerate this surface?" : "Open this app surface?"
            explanation = "App \(appID), surface \(surface), kind \(spec["kind"] ?? "unknown"), session \(sid). Opens/renders a surface and may write per-user cache/trace metadata and push it to this session or phone. Generated surfaces on a cache miss, and requested regeneration, can call the configured AI provider/fallback using the app’s prompts and your data below. Existing app permissions: \(NativeSurfaceWire.json(["permissions":manifest["permissions"] ?? []])). No JavaScript, embedded web content, URL fetching or browser code is run by this native renderer. Data: \(NativeSurfaceWire.json(data)). Generation description: \(spec["generation_prompt"] ?? "")."
        case .dispatch(let actionID,let value):
            guard let root = root,!surfaceID.isEmpty,root.actionIDs.contains(actionID),let updates=surfaceUpdates,
                  updates.connectionID==socketIdentity,updates.sessionID==sid,updates.surfaceID==surfaceID,
                  updates.revision==renderIdentity,handshakeAcknowledged else { throw NativeSurfaceFailure("The action must belong to the current rendered native surface and its owning live session. Open the surface again after reconnecting.") }
            let spec = try NativeSurfaceWire.contract(manifest,surface:surfaceID,action:actionID,value:value)
            guard JSONSerialization.isValidJSONObject(["value":value]),let data = try? JSONSerialization.data(withJSONObject:["value":value]),data.count <= 65536 else { throw NativeSurfaceFailure("The action input exceeds 64 KiB.") }
            selected = surfaceID;title = "Send this declared app action?"
            explanation = "Exact app/session/surface: \(appID) / \(sid) / \(surfaceID). Action: \(actionID). Contract: \(NativeSurfaceWire.json(spec)). Submitted value: \(NativeSurfaceWire.json(["value":value])). skill_call can perform the declared endpoint’s writes/network actions under core policy. app_event forwards this value to the AI agent, can spend provider budget and may lead to tools. Navigation may generate the target surface. The REST receipt acknowledges dispatch, not completed execution; requires_confirmation may create a separate backend confirmation instead of executing. Closing a surface does not undo actions."
        }
        let sessionDisclosure="\nThis request connects a dedicated app-session socket. Its direct transcript is separate from Chat, but memory, identity, tools and persistent grants remain shared across the brain. Connection may bind connected glasses/phones. Disconnect may trigger backend learning, summarization or identity maintenance (including configured-provider spend), and clears non-primary session context. Closing does not reliably cancel a REST-dispatched action."
        return NativeSurfaceReview(generation:generation,origin:client.baseURL.absoluteString,sessionID:sid,appID:appID,manifest:manifest,surfaceID:selected,action:action,title:title,explanation:explanation+sessionDisclosure)
    }
    func perform(_ review:NativeSurfaceReview) async -> Bool {
        guard let client = client,review.generation == generation,review.origin == client.baseURL.absoluteString,review.sessionID == sessionID,review.appID == appID,!loading,!acting,activeOperation==nil,!used.contains(review.id) else { error = "The surface review expired or was already used. Review again.";return false }
        used.insert(review.id);let operation=UUID();activeOperation=operation;acting = true;error = nil;receipt = nil;let connection = generation
        defer { if activeOperation==operation { activeOperation=nil;acting=false } }
        var dispatched = false
        do {
            let fresh = try NativeSurfaceWire.manifest(try await client.request("/api/apps/" + NativeSurfaceWire.segment(appID) + "/manifest"),app:appID)
            guard connection == generation,review.sessionID == sessionID else { return false }
            guard NativeSurfaceWire.same(fresh,review.manifest) else { throw NativeSurfaceFailure("App version, permissions or surface contracts changed. No action was sent.") }
            try await connectSession(connection:connection,reviewSession:review.sessionID)
            guard connection==generation,review.sessionID==sessionID,handshakeAcknowledged else{return false}
            let confirmed=try NativeSurfaceWire.manifest(try await client.request("/api/apps/"+NativeSurfaceWire.segment(review.appID)+"/manifest"),app:review.appID)
            guard connection==generation,review.sessionID==sessionID,handshakeAcknowledged else{return false}
            guard NativeSurfaceWire.same(confirmed,review.manifest) else{throw NativeSurfaceFailure("The app contract changed while connecting its session. No app action was sent.")}
            let owningSocket=socketIdentity
            let prefix = "/api/apps/" + NativeSurfaceWire.segment(review.appID)
            switch review.action {
            case .open(let surface,let regenerate,let data):
                dispatched = true
                let result = try await client.request(prefix + "/open",body:["surface_id":surface,"data":data,"session_id":review.sessionID,"user_fingerprint":review.sessionID,"regenerate":regenerate])
                guard NativeSurfaceWire.bool(result["success"]) == true,result["app_id"] as? String == review.appID,result["surface_id"] as? String == surface,result["screen_id"] as? String == NativeSurfaceWire.screen(app:review.appID,surface:surface,session:review.sessionID),let raw = result["root"] as? [String:Any] else { throw NativeSurfaceFailure("The opened surface did not match the reviewed app, surface and session.") }
                let updates=try NativeSurfaceUpdateReducer(origin:client.baseURL,sessionID:review.sessionID,connectionID:owningSocket,manifest:confirmed,surfaceID:surface,root:raw)
                guard connection==generation,review.sessionID==sessionID,socketIdentity==owningSocket else{
                    if review.sessionID==sessionID,review.appID==appID,client.baseURL==self.client?.baseURL{
                        receipt="The earlier reviewed open returned a matching surface receipt after the session or mounted view changed. Its old tree was not mounted; delivery and tool completion remain unverified."
                    }
                    return true
                }
                surfaceUpdates=updates;root = updates.node;surfaceID = surface;renderIdentity = updates.revision
                receipt = NativeSurfaceWire.bool(result["pushed"]) == false ? "Surface returned and mounted locally. The backend reports session push failed; no cross-device delivery is claimed." : "Surface returned and mounted locally. Session/phone delivery and tool execution are not verified."
            case .dispatch(let actionID,let value):
                guard surfaceID == review.surfaceID,root?.actionIDs.contains(actionID) == true else { throw NativeSurfaceFailure("The rendered surface changed after review.") }
                let spec = try NativeSurfaceWire.contract(fresh,surface:review.surfaceID,action:actionID,value:value)
                appendTranscript("action","Reviewed \(review.appID)/\(review.surfaceID): \(actionID). Receipt and app-session replies are shown separately.")
                if spec["handler"] as? String=="navigate",NativeSurfaceWire.bool(spec["requires_confirmation"])==false,let target=spec["target"] as? String,let updates=surfaceUpdates {
                    pendingNavigation=NativeSurfaceNavigationReceipt(id:operation,sourceRevision:updates.revision,connectionID:owningSocket,origin:client.baseURL,sessionID:review.sessionID,appID:review.appID,target:target,manifest:confirmed,contract:spec)
                }
                dispatched = true
                let result = try await client.request(prefix + "/dispatch",body:["surface_id":review.surfaceID,"action_id":actionID,"value":value,"event":"tap","session_id":review.sessionID])
                guard NativeSurfaceWire.bool(result["success"]) == true,result["handler"] as? String == spec["handler"] as? String,result["target"] as? String == spec["target"] as? String,
                      NativeSurfaceWire.bool(result["requires_confirmation"]) == NativeSurfaceWire.bool(spec["requires_confirmation"]),result["screen_id"] as? String == NativeSurfaceWire.screen(app:review.appID,surface:review.surfaceID,session:review.sessionID) else { throw NativeSurfaceFailure("Dispatch acknowledgement does not match the reviewed contract/session.") }
                guard connection==generation,review.sessionID==sessionID,socketIdentity==owningSocket else{
                    if review.sessionID==sessionID,review.appID==appID,client.baseURL==self.client?.baseURL{
                        receipt="The earlier reviewed action received a matching dispatch acknowledgement after the session or mounted view changed. Its outcome remains unverified; no old surface state was restored."
                    }
                    return true
                }
                if spec["handler"] as? String == "close",NativeSurfaceWire.bool(spec["requires_confirmation"]) == false { pendingNavigation=nil;surfaceUpdates=nil;root = nil;surfaceID = "";renderIdentity=UUID() }
                receipt = NativeSurfaceWire.bool(spec["requires_confirmation"]) == true ? "Dispatch acknowledged. Review the owning backend confirmation card when it arrives; execution has not been verified." : "Dispatch acknowledged. This receipt does not verify tool completion, navigation delivery or model outcome."
            }
            generation = UUID();return true
        } catch {
            if pendingNavigation?.id==operation{pendingNavigation=nil}
            guard review.sessionID==sessionID,review.appID==appID,client.baseURL==self.client?.baseURL else{return false}
            self.error = ((error as? NativeSurfaceFailure)?.message ?? "The local surface request failed.") + (dispatched ? " The request may already have taken effect. Refresh before retrying." : "")
            return false
        }
    }
}

struct NativeAppSurfaceFeatureView:View {
    let baseURL:URL?
    let sessionID:String?
    @StateObject private var model = NativeAppSurfaceModel()
    @State private var review:NativeSurfaceReview?
    @State private var localError:String?
    var body:some View {
        VStack(alignment:.leading,spacing:12) {
            HStack { Text("App surfaces").font(.title2.bold());Spacer();Button("Refresh installed apps") { Task { await model.refresh() } }.disabled(!model.available || model.loading || model.acting) }
            Text("Installed app interfaces rendered with native controls. Install, verified update and uninstall remain in Skills & Store. App actions use a separate agent session; they do not receive this Chat thread’s conversation history, and their agent replies are not mounted in Chat. Dispatch receipts below do not prove completion. Embedded web content and unsupported components stay inactive.").foregroundStyle(.secondary)
            if let error = localError ?? model.error { NativeSelectableText(error).foregroundStyle(.red) }
            if let receipt = model.receipt { NativeSelectableText(receipt).font(.callout) }
            if model.loading || model.acting { ProgressView(model.acting ? "Checking contract and receipt…" : "Reading installed app contracts…") }
            HStack { Text("App session: "+model.sessionStatus).font(.caption);Spacer();Button("Disconnect app session") { model.closeSession() }.disabled(model.sessionStatus=="Disconnected") }
            HSplitView {
                ScrollView { VStack(alignment:.leading,spacing:12) {
                    if let apps = model.apps {
                        if apps.isEmpty { Text("No installed apps returned by the service.") }
                        ForEach(Array(apps.enumerated()),id:\.offset) { _,app in
                            Button(app["app_id"] as? String ?? "Unknown app") { if let id = app["app_id"] as? String { localError = nil;Task { await model.inspect(id) } } }.disabled(model.loading || model.acting)
                        }
                    } else { Text("Installed inventory is unavailable.") }
                    ForEach(Array(model.surfaces.enumerated()),id:\.offset) { _,surface in surfaceCard(surface) }
                    if let manifest = model.manifest { DisclosureGroup("Declared manifest") { NativeSelectableText(NativeSurfaceWire.json(manifest)).font(.system(.caption,design:.monospaced)) } }
                }.frame(maxWidth:.infinity,alignment:.leading).padding(8) }.frame(minWidth:220,idealWidth:280,maxWidth:360)
                ScrollView { VStack(alignment:.leading,spacing:12) {
                    if let node = model.root {
                        let mountedRevision=model.renderIdentity
                        HStack { Text("Native surface: \(model.surfaceID)").font(.headline);Spacer();Button("Dismiss locally") { model.dismissSurface() }.disabled(model.acting) }
                        NativeSurfaceNodeView(node:node,disabled:model.acting || model.loading) { id,value in prepare(.dispatch(action:id,value:value),mountedRevision:mountedRevision) }.id(mountedRevision)
                    } else { Text("Select an installed app, inspect its contract, then review opening a surface.").foregroundStyle(.secondary) }
                    Divider()
                    Text("Dedicated app-session replies").font(.headline)
                    Text("Direct Chat history is separate. Memory, identity, tools and persistent folder permissions remain shared across the brain. Disconnect does not undo or reliably cancel REST actions.").font(.caption).foregroundStyle(.secondary)
                    ForEach(model.transcript){row in VStack(alignment:.leading,spacing:4){Text(row.role.capitalized).font(.caption.bold());NativeSelectableText(verbatim:row.text)}}
                    NativeSurfaceSessionEvents(model:model)
                }.frame(maxWidth:.infinity,alignment:.leading).padding(12) }.frame(minWidth:320)
            }
        }.padding(20)
        .task(id:(baseURL?.absoluteString ?? "") + "|" + (sessionID ?? "")) { review = nil;localError = nil;await model.configure(baseURL:baseURL,sessionID:sessionID) }
        .onDisappear{model.closeSession()}
        .sheet(item:$review) { item in VStack(alignment:.leading,spacing:16) {
            ScrollView { NativeReviewSummaryView(review:summary(item)) }
            HStack { Spacer();Button("Cancel") { review = nil };Button("Confirm reviewed request") { review = nil;Task { _ = await model.perform(item) } }.disabled(model.acting || model.loading) }
        }.padding(24).frame(width:680,height:540) }
    }
    private func summary(_ item:NativeSurfaceReview)->NativeReviewSummary {
        var scope=["Memory, identity, tools and persistent folder grants remain shared across the brain. The app session may bind connected glasses or phones.","Disconnect can trigger provider-backed learning; closing does not undo or reliably cancel a dispatched action."]
        var targets:[NativeReviewSummary.Field]=[.init(label:"Local service",value:item.origin),.init(label:"App / surface",value:item.appID+" / "+item.surfaceID),.init(label:"Owning session",value:item.sessionID)]
        let effect:String
        switch item.action {
        case .open(let surface,let regenerate,let data):
            effect=regenerate ? "Regenerates and opens this app surface." : "Opens this app surface."
            let spec=(item.manifest["surfaces"] as? [[String:Any]])?.first{$0["surface_id"] as? String==surface}
            if regenerate || (spec?["kind"] as? String) != "authored" {scope.append("Generation can send the app prompts and submitted data to the configured AI provider or fallback and spend provider budget.")}
            scope.append("Opening may save app cache/trace data and push the surface to this session or connected phone.")
            targets.append(.init(label:"Submitted data",value:NativeSurfaceWire.json(data)))
            targets.append(.init(label:"Declared app permissions",value:NativeSurfaceWire.json(["permissions":item.manifest["permissions"] ?? []])))
        case .dispatch(let actionID,let value):
            effect="Sends this declared action. Dispatch acknowledgement does not verify completed execution."
            if let contract=try? NativeSurfaceWire.contract(item.manifest,surface:item.surfaceID,action:actionID,value:value) {
                switch contract["handler"] as? String {
                case "app_event":scope.append("Sends the entered value to the agent; it can use shared tools and spend configured provider budget.")
                case "skill_call":scope.append("Calls the declared skill endpoint, which can perform writes or network actions under core policy.")
                case "navigate":scope.append("Opening the declared destination may generate its surface using the configured AI provider.")
                default:break
                }
                targets.append(.init(label:"Declared action contract",value:NativeSurfaceWire.json(contract)))
            }
            targets.append(.init(label:"Action",value:actionID))
            targets.append(.init(label:"Submitted value",value:NativeSurfaceWire.json(["value":value])))
        }
        return NativeReviewSummary(action:item.title,effect:effect,materialScope:scope,targets:targets,details:item.explanation)
    }
    private func surfaceCard(_ surface:[String:Any]) -> some View {
        VStack(alignment:.leading,spacing:8) {
            Text(surface["title"] as? String ?? surface["surface_id"] as? String ?? "Surface").font(.headline)
            Text("Kind: \(surface["kind"] as? String ?? "unknown")").font(.caption)
            Button("Review opening…") { if let id = surface["surface_id"] as? String { prepare(.open(surface:id,regenerate:false,data:[:])) } }
            if surface["kind"] as? String != "authored" { Button("Review regeneration…") { if let id = surface["surface_id"] as? String { prepare(.open(surface:id,regenerate:true,data:[:])) } } }
        }.disabled(model.loading || model.acting || sessionID?.isEmpty != false).padding(10).background(Color.secondary.opacity(0.07),in:RoundedRectangle(cornerRadius:10))
    }
    private func prepare(_ action:NativeSurfaceAction,mountedRevision:UUID?=nil) { do { localError = nil;review = try model.review(action,mountedRevision:mountedRevision) } catch { localError = (error as? NativeSurfaceFailure)?.message ?? "This surface request could not be reviewed." } }
}

private struct NativeSurfaceSessionEvents:View {
    @ObservedObject var model:NativeAppSurfaceModel
    var body:some View {
        VStack(alignment:.leading,spacing:12) {
            NativeAppConfirmationFeatureView(model:model.appConfirmations,onResponse:{ response in try await model.respondToAppConfirmation(response) })
            NativeRichChatEventsView(model:model.richChat,onPermissionResponse:{ response in try await model.respondToPermission(response) })
        }
    }
}

private struct NativeSurfaceNodeView:View {
    let node:NativeSurfaceNode
    let disabled:Bool
    let onAction:(String,Any) -> Void
    @State private var text = ""
    @State private var checked = false
    @State private var values:[String:Any] = [:]
    @State private var inputError:String?
    var body:some View {
        content.disabled(disabled).onAppear {
            text = node.raw["value"] as? String ?? "";checked = NativeSurfaceWire.bool(node.raw["value"]) ?? false
            for field in node.raw["fields"] as? [[String:Any]] ?? [] {
                if let name = field["name"] as? String {
                    values[name] = field["value"] ?? (field["type"] as? String == "checkbox" ? false : (field["options"] as? [String])?.first ?? "")
                }
            }
        }
    }
    @ViewBuilder private var content:some View {
        if !node.supported { Text("Unsupported \(node.type) component — inspection only. No content was fetched or executed.").foregroundStyle(.secondary) }
        else if ["VStack","Column","Card","ScrollView"].contains(node.type) {
            VStack(alignment:.leading,spacing:10) { children }.padding(node.type == "Card" ? 12 : 0)
        } else if ["HStack","Row"].contains(node.type) { HStack(alignment:.top,spacing:10) { children } }
        else if ["Text","Markdown","CodeBlock","Badge"].contains(node.type) {
            NativeSelectableText(verbatim:node.raw["content"] as? String ?? display(node.raw["value"])).font(node.raw["style"] as? String == "headline" ? .headline : .body)
        } else if node.type == "Button" {
            Button(node.raw["label"] as? String ?? "Review action…") { emit(node.raw["value"] ?? NSNull()) }.disabled(node.actionID == nil || NativeSurfaceWire.bool(node.raw["disabled"]) == true)
        } else if node.type == "TextField" {
            VStack(alignment:.leading) {
                TextField(node.raw["label"] as? String ?? "Text",text:$text)
                if let inputError = inputError { Text(inputError).foregroundStyle(.red) }
                Button("Review submitted value…") {
                    if node.raw["input_type"] as? String == "number" {
                        guard let n = Double(text),n.isFinite else { inputError = "Enter a finite number.";return };inputError = nil;emit(n)
                    } else { inputError = nil;emit(text) }
                }.disabled(node.actionID == nil)
            }
        } else if ["Checkbox","Toggle"].contains(node.type) {
            VStack(alignment:.leading) { Toggle(node.raw["label"] as? String ?? "Choice",isOn:$checked);Button("Review submitted choice…") { emit(checked) }.disabled(node.actionID == nil) }
        } else if ["Form","FormView"].contains(node.type) { form }
        else if node.type == "ProgressBar" { ProgressView(value:max(0,min(1,NativeSurfaceWire.number(node.raw["value"]) ?? 0))) { Text(node.raw["label"] as? String ?? "Progress") } }
        else if node.type == "Divider" { Divider() }
        else if node.type == "Spacer" { Spacer(minLength:8) }
    }
    private var children:some View { AnyView(ForEach(node.children) { child in NativeSurfaceNodeView(node:child,disabled:disabled,onAction:onAction) }) }
    private var form:some View {
        VStack(alignment:.leading,spacing:10) {
            ForEach(Array((node.raw["fields"] as? [[String:Any]] ?? []).enumerated()),id:\.offset) { _,field in formField(field) }
            if let inputError = inputError { Text(inputError).foregroundStyle(.red) }
            Button(node.raw["submit_label"] as? String ?? "Review form submission…") {
                var submitted = values
                for field in node.raw["fields"] as? [[String:Any]] ?? [] where field["type"] as? String == "number" {
                    if let name = field["name"] as? String {
                        guard let n = Double(display(values[name])),n.isFinite else { inputError = "Enter a finite number for \(name).";return };submitted[name] = n
                    }
                }
                inputError = nil;emit(["values":submitted])
            }.disabled(node.actionID == nil)
            Text("Editing stays local. Submission requires a separate contract and data review.").font(.caption).foregroundStyle(.secondary)
        }
    }
    @ViewBuilder private func formField(_ field:[String:Any]) -> some View {
        let name = field["name"] as? String ?? ""
        let label = field["label"] as? String ?? name
        if field["type"] as? String == "checkbox" {
            Toggle(label,isOn:Binding(get:{ NativeSurfaceWire.bool(values[name]) ?? false },set:{ values[name] = $0 }))
        } else if field["type"] as? String == "select" {
            Picker(label,selection:Binding(get:{ values[name] as? String ?? "" },set:{ values[name] = $0 })) { ForEach(field["options"] as? [String] ?? [],id:\.self) { option in Text(option).tag(option) } }
        } else if field["type"] as? String == "textarea" {
            NativePlainTextEditor(text:Binding(get:{ display(values[name]) },set:{ values[name] = $0 }),label:label).frame(minHeight:100)
        } else { TextField(label,text:Binding(get:{ display(values[name]) },set:{ values[name] = $0 })) }
    }
    private func emit(_ value:Any) { if let id = node.actionID { onAction(id,value) } }
    private func display(_ value:Any?) -> String { if let text = value as? String { return text };if let number = value as? NSNumber { return number.stringValue };return "" }
}
