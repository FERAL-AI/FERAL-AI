import SwiftUI
import Foundation
import CoreFoundation

struct NativeAutomationError:LocalizedError {
    let text:String
    init(_ text:String) { self.text = text }
    var errorDescription:String? { text }
}
enum NativeAutomationWire {
    static func bool(_ value:Any?) -> Bool? { guard let n = value as? NSNumber,CFGetTypeID(n) == CFBooleanGetTypeID() else { return nil };return n.boolValue }
    static func id(_ value:String) -> Bool { value.range(of:"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$",options:.regularExpression) != nil }
    static func equal(_ a:[String:Any],_ b:[String:Any]) -> Bool { NSDictionary(dictionary:a).isEqual(to:b) }
    static func segment(_ value:String) -> String { value.addingPercentEncoding(withAllowedCharacters:CharacterSet(charactersIn:"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")) ?? "" }
    static func safe(_ value:[String:Any],_ fields:[String]) -> [String:Any] { value.filter { fields.contains($0.key) } }
}
final class NativeAutomationRedirectGuard:NSObject,URLSessionTaskDelegate {
    func urlSession(_ session:URLSession,task:URLSessionTask,willPerformHTTPRedirection response:HTTPURLResponse,newRequest request:URLRequest,completionHandler:@escaping(URLRequest?) -> Void) { completionHandler(nil) }
}
enum NativeAutomationKind:String,CaseIterable { case geofences,inbound,outgoing
    var path:String { switch self { case .geofences:return "/api/geofences";case .inbound:return "/api/custom-webhooks/list";case .outgoing:return "/api/outgoing-webhooks" } }
    var listKey:String { self == .geofences ? "geofences" : "webhooks" }
}
enum NativeAutomationAction {
    case load(NativeAutomationKind),create(NativeAutomationKind,[String:Any]),delete(NativeAutomationKind,String),testOutgoing(String),runs(String)
}
struct NativeAutomationReview:Identifiable {
    let id = UUID(),generation:UUID
    let contextRevision:UUID
    let action:NativeAutomationAction,title:String,detail:String
    let expected:[String:Any]?
}
struct NativeAutomationRow:Identifiable { let id:String,fields:[String:Any] }
struct NativeRoutineDispatchSnapshot {
    let state:String
    let requiresReconciliation:Bool
    var message:String {
        switch state {
        case "scheduled":return "No unresolved action reported. Scheduled actions still require their normal permissions."
        case "in_progress":return "Action in progress. Its outcome has not been verified."
        case "reconciliation_required":return "Needs review: an earlier action outcome is unresolved. Automatic replay is blocked."
        case "bookkeeping_pending":return "The callback returned; schedule updates are pending. A completed external action is not established."
        default:return "Action status unavailable. Reload before relying on this schedule."
        }
    }
    static func parse(_ value:Any?,routineID:String) throws -> NativeRoutineDispatchSnapshot {
        guard let value else { return NativeRoutineDispatchSnapshot(state:"unavailable",requiresReconciliation:false) }
        guard let raw = value as? [String:Any],let available = NativeAutomationWire.bool(raw["tracking_available"]),let state = raw["dispatch_state"] as? String else { throw NativeAutomationError("Routine action status is unreadable.") }
        if !available {
            guard state == "unavailable",raw["occurrence"] == nil || raw["occurrence"] is NSNull else { throw NativeAutomationError("Unavailable action status contains inconsistent evidence.") }
            return NativeRoutineDispatchSnapshot(state:state,requiresReconciliation:false)
        }
        guard ["scheduled","in_progress","reconciliation_required","bookkeeping_pending"].contains(state),let needsReview = NativeAutomationWire.bool(raw["reconciliation_required"]),needsReview == (state == "reconciliation_required") else { throw NativeAutomationError("Routine action status is inconsistent.") }
        if state == "scheduled" {
            guard raw["occurrence"] is NSNull else { throw NativeAutomationError("Ready schedule contains an unresolved action.") }
        } else {
            guard let occurrence = raw["occurrence"] as? [String:Any],String(describing:occurrence["job_id"] ?? "") == routineID,let id = occurrence["occurrence_id"] as? String,UUID(uuidString:id) != nil,let status = occurrence["status"] as? String else { throw NativeAutomationError("Exact routine action identity was not confirmed.") }
            let allowed = state == "in_progress" ? ["claimed"] : state == "bookkeeping_pending" ? ["callback_returned"] : ["claimed","outcome_unknown"]
            guard allowed.contains(status),occurrence["rearmed_at"] == nil || occurrence["rearmed_at"] is NSNull else { throw NativeAutomationError("Routine action status contradicts its outcome record.") }
        }
        return NativeRoutineDispatchSnapshot(state:state,requiresReconciliation:needsReview)
    }
}
@MainActor final class NativeAutomationModel:ObservableObject {
    @Published private(set) var rows:[NativeAutomationKind:[NativeAutomationRow]] = [:]
    @Published private(set) var errors:[NativeAutomationKind:String] = [:]
    @Published private(set) var receipt:String?
    @Published private(set) var error:String?
    @Published private(set) var busy = false
    @Published private(set) var runRows:[[String:Any]]?
    @Published private(set) var routineID:String?
    @Published private(set) var routineDispatch:NativeRoutineDispatchSnapshot?
    private var baseURL:URL?,generation = UUID(),issued = Set<UUID>()
    private var originals:[NativeAutomationKind:[String:[String:Any]]] = [:]
    private var contextGate = NativeContextActionGate()
    @Published private(set) var contextPolicy = NativeSelectedContextPolicy.legacy
    func setContextPolicy(_ policy:NativeSelectedContextPolicy) {
        if contextGate.update(policy) { contextPolicy = policy }
    }
    private let session:URLSession
    init(session:URLSession? = nil) {
        if let session { self.session = session } else { let config = URLSessionConfiguration.ephemeral;config.timeoutIntervalForResource = 30;self.session = URLSession(configuration:config,delegate:NativeAutomationRedirectGuard(),delegateQueue:nil) }
    }
    func configure(_ url:URL?) { baseURL = url;generation = UUID();issued = [];rows = [:];originals = [:];errors = [:];receipt = nil;error = nil;runRows = nil;routineID = nil;routineDispatch = nil;busy = false }
    private func request(_ path:String,method:String = "GET",body:[String:Any]? = nil) async throws -> [String:Any] {
        guard let baseURL,baseURL.scheme == "http",["127.0.0.1","::1","[::1]"].contains(baseURL.host ?? ""),baseURL.user == nil,baseURL.password == nil,var parts = URLComponents(url:baseURL,resolvingAgainstBaseURL:false) else { throw NativeAutomationError("Automation requires the local app service.") }
        parts.percentEncodedPath = path;parts.query = nil;parts.fragment = nil
        guard let url = parts.url else { throw NativeAutomationError("Invalid automation endpoint.") }
        var req = URLRequest(url:url);req.httpMethod = method
        if let body { req.httpBody = try JSONSerialization.data(withJSONObject:body);req.setValue("application/json",forHTTPHeaderField:"Content-Type") }
        let data:Data,response:URLResponse
        do { (data,response) = try await session.feralLocalData(for:req) } catch { throw NativeAutomationError("Local automation request did not finish.") }
        guard let http = response as? HTTPURLResponse,(200..<300).contains(http.statusCode),let value = (try? JSONSerialization.jsonObject(with:data)) as? [String:Any],value["error"] == nil || value["error"] is NSNull,NativeAutomationWire.bool(value["success"]) != false,NativeAutomationWire.bool(value["ok"]) != false else { throw NativeAutomationError("Automation response did not establish success. Private backend details are withheld.") }
        return value
    }
    private func parse(_ kind:NativeAutomationKind,_ value:[String:Any]) throws -> [String:[String:Any]] {
        guard let list = value[kind.listKey] as? [[String:Any]],list.count <= 500,list.allSatisfy({ NativeAutomationWire.id($0["id"] as? String ?? "") }),Set(list.compactMap { $0["id"] as? String }).count == list.count else { throw NativeAutomationError("Automation inventory is unreadable; empty state is not established.") }
        return Dictionary(uniqueKeysWithValues:list.map { ($0["id"] as! String,$0) })
    }
    private func install(_ kind:NativeAutomationKind,_ records:[String:[String:Any]]) {
        originals[kind] = records;errors[kind] = nil
        rows[kind] = records.keys.sorted().map { id in
            let raw = records[id]!,fields = kind == .geofences ? ["name","lat","lon","radius_m","on_enter","on_exit"] : kind == .inbound ? ["name","action","trigger_count","last_triggered"] : ["name","event_types","enabled","delivery_count","failure_count","last_delivered"]
            var safe = NativeAutomationWire.safe(raw,fields)
            if kind == .outgoing,let target = raw["target_url"] as? String { safe["target_host"] = URL(string:target)?.host ?? "Unavailable" }
            if kind == .inbound { safe["signed"] = !(raw["secret"] as? String ?? "").isEmpty }
            return NativeAutomationRow(id:id,fields:safe)
        }
    }
    func startsWork(_ action:NativeAutomationAction)->Bool {
        switch action { case .create,.testOutgoing:return true;case .load,.delete,.runs:return false }
    }
    private func assertContext(_ approved:NativeAutomationReview) throws {
        guard contextGate.accepts(approved.contextRevision),!startsWork(approved.action) || contextPolicy.taskReady else { throw NativeAutomationError("Selected chat, connection or readiness changed. Review again; no automatic retry is made.") }
    }
    func review(_ action:NativeAutomationAction) throws -> NativeAutomationReview {
        guard !startsWork(action) || contextPolicy.taskReady else { throw NativeAutomationError("Verify the selected chat before arming a trigger or sending a real test. Inspection and trigger deletion remain available.") }
        guard baseURL != nil,!busy else { throw NativeAutomationError("Wait for the local automation service.") }
        let title:String,detail:String;var expected:[String:Any]?
        switch action {
        case .load(let kind):title = "Load \(kind.rawValue) automation inventory?";detail = "Reads local trigger settings and can initialize the backend’s persistent webhook store. Geofence coordinates and automation names are private. An empty response does not prove the underlying engine/store is available. No test event or location sample is sent."
        case .create(let kind,let body):
            guard originals[kind] != nil,errors[kind] == nil,let name = body["name"] as? String,NativeAutomationWire.id(name) else { throw NativeAutomationError("Load the inventory and enter a bounded name using letters, digits, dots, dashes or underscores.") }
            if kind == .geofences {
                guard originals[kind]?[name] == nil,let lat = body["lat"] as? Double,lat.isFinite,(-90...90).contains(lat),let lon = body["lon"] as? Double,lon.isFinite,(-180...180).contains(lon),let radius = body["radius_m"] as? Double,radius.isFinite,(1...100000).contains(radius),let enter = body["on_enter"] as? String,let exit = body["on_exit"] as? String,enter.count <= 2000,exit.count <= 2000 else { throw NativeAutomationError("Use a new geofence name, valid coordinates, radius 1–100000 m and bounded enter/exit actions.") }
                expected = [:];title = "Create location trigger \(name)?";detail = "Stores private coordinates \(lat), \(lon) with radius \(radius) m. Enter action: \(enter). Exit action: \(exit). Events require location updates from a connected source and backend callbacks; saving does not prove GPS, permissions or action execution. Trigger actions can lead to provider/tool use under backend policy. No location sample or OS location permission is requested here."
            } else if kind == .inbound {
                guard body["action"] as? String == "chat",let secret = body["secret"] as? String,!secret.isEmpty,secret.count <= 4096,let params = body["action_params"] as? [String:Any],let prefix = params["prefix"] as? String,prefix.count <= 2000 else { throw NativeAutomationError("Enter a signing secret and bounded chat prefix.") }
                title = "Create signed inbound webhook \(name)?";detail = "Creates an endpoint that verifies SHA-256 HMAC signatures using the hidden new secret. Received payloads can become agent commands in the first active backend session, not necessarily this conversation, and may use tools/provider budget. Prefix: \(prefix). Network exposure depends on server access configuration; endpoint creation does not prove external reachability."
            } else {
                guard let raw = body["target_url"] as? String,let url = URL(string:raw),url.scheme == "https",url.host != nil,url.user == nil,url.password == nil,url.fragment == nil,let events = body["event_types"] as? [String],!events.isEmpty,events.count <= 50,events.allSatisfy({ !$0.isEmpty && $0.count <= 100 }),NativeAutomationWire.bool(body["enabled"]) != nil,let secret = body["secret"] as? String,!secret.isEmpty,secret.count <= 4096 else { throw NativeAutomationError("Use an HTTPS endpoint, signing secret and explicit bounded event filters.") }
                title = "Create outgoing subscription \(name)?";detail = "Sends matching event payloads to \(url.host!) when enabled: \(body["enabled"]!). Filters: \(events.joined(separator:", ")). Payloads may contain private chat, memory or health information. URL query and signing secret are hidden. This arms recurring delivery when enabled and wires the event bus; it does not prove delivery or recipient trust."
            }
        case .delete(_,let id),.testOutgoing(let id):
            let selectedKind:NativeAutomationKind
            if case .delete(let kind,_) = action { selectedKind = kind } else { selectedKind = .outgoing }
            guard NativeAutomationWire.id(id),errors[selectedKind] == nil,let row = originals[selectedKind]?[id] else { throw NativeAutomationError("Reload this exact automation before reviewing it.") }
            expected = row
            if case .testOutgoing = action {
                guard let raw = row["target_url"] as? String,let target = URL(string:raw),target.scheme == "https",target.host != nil,target.user == nil,target.password == nil,target.fragment == nil else { throw NativeAutomationError("Stored test target must be HTTPS without embedded credentials or a fragment. No delivery sent.") }
                title = "Send synthetic test to this outgoing endpoint?";detail = "Makes a real HTTPS request to \(target.host!) using its stored signing secret. Sends only test.ping and a fixed fixture message. No chat, memory or health payload is included. Delivered=false is a failed delivery verdict, not success."
            }
            else { title = "Permanently delete \(selectedKind.rawValue) automation \(id)?";detail = "Deletes this exact stored trigger. Already running actions or outbound requests may continue; their effects are not undone. No provider credential revocation is established." }
        case .runs(let id):guard Int(id).map({$0 > 0}) == true,id.count <= 18 else { throw NativeAutomationError("Enter a positive routine ID.") };title = "Inspect routine \(id) and last 20 runs?";detail = "Reads schedule and execution metadata for this backend routine. This uses the detail endpoint and does not call the routine list that can restart the scheduler. Raw payloads, outputs and error strings are withheld because they may include private credentials or content."
        }
        let result = NativeAutomationReview(generation:generation,contextRevision:contextGate.revision,action:action,title:title,detail:detail + "\nScope: global trigger/subscription. It is not bound to the selected chat or its saved context.",expected:expected);issued.insert(result.id);return result
    }
    func perform(_ approved:NativeAutomationReview) async -> Bool {
        guard approved.generation == generation,contextGate.accepts(approved.contextRevision),(!startsWork(approved.action) || contextPolicy.taskReady),issued.remove(approved.id) != nil,!busy else { error = "Review expired or already used.";return false }
        let version = generation;busy = true;receipt = nil;error = nil;var dispatched = false
        defer { if generation == version { busy = false } }
        do {
            let text:String
            switch approved.action {
            case .load(let kind):
                let records = try parse(kind,try await request(kind.path));guard generation == version else { return false };install(kind,records);text = "Inventory response loaded. Engine/store availability is not proven by an empty list."
            case .runs(let id):
                runRows = nil;routineID = nil;routineDispatch = nil
                let value = try await request("/api/routines/" + id)
                guard let routine = value["routine"] as? [String:Any],String(describing:routine["id"] ?? "") == id,let runs = value["runs"] as? [[String:Any]],runs.count <= 20,runs.allSatisfy({String(describing:$0["job_id"] ?? "") == id}) else { throw NativeAutomationError("Exact routine and bounded run history were not confirmed.") }
                let dispatch = try NativeRoutineDispatchSnapshot.parse(value["dispatch"],routineID:id)
                guard generation == version else { return false }
                routineID = id;routineDispatch = dispatch;runRows = runs.map { NativeAutomationWire.safe($0,["id","job_id","started_at","finished_at","status","duration_ms"]) };text = "Routine \(id) metadata read; \(runs.count) run records returned. Completed external effects are not established by run status alone."
            case .create(let kind,let body):
                let fresh = try parse(kind,try await request(kind.path));guard generation == version else { return false }
                if kind == .geofences,let name = body["name"] as? String,fresh[name] != nil { throw NativeAutomationError("That geofence name now exists. Reload and review a new name.") }
                try assertContext(approved)
                dispatched = true
                let path = kind == .inbound ? "/api/custom-webhooks/create" : kind.path
                let value = try await request(path,method:"POST",body:body)
                let key = kind == .geofences ? "geofence" : "webhook"
                guard NativeAutomationWire.bool(value["success"]) == true,let record = value[key] as? [String:Any],let id = record["id"] as? String,NativeAutomationWire.id(id),record["name"] as? String == body["name"] as? String else { throw NativeAutomationError("Exact automation creation receipt was not confirmed.") }
                guard fresh[id] == nil else { throw NativeAutomationError("Creation receipt reused an existing automation ID.") }
                let after = try parse(kind,try await request(kind.path));guard let stored = after[id] else { throw NativeAutomationError("Created automation is absent from readback.") }
                if kind == .inbound { guard stored["secret"] as? String == body["secret"] as? String else { throw NativeAutomationError("Inbound signing secret persistence was not confirmed.") } }
                if kind == .outgoing {
                    guard let supplied = body["secret"] as? String,record["secret"] as? String == supplied else { throw NativeAutomationError("Creation receipt did not confirm the exact supplied outgoing signing secret.") }
                    guard NativeAutomationWire.bool(stored["has_secret"]) == true || stored["secret"] as? String == supplied else { throw NativeAutomationError("Outgoing signing secret presence was not confirmed by readback.") }
                }
                for field in body.keys where field != "secret" { guard let actual = stored[field],NativeAutomationWire.equal(["v":actual],["v":body[field]!]) else { throw NativeAutomationError("Stored automation fields differ from the review.") } }
                guard generation == version else { return false };install(kind,after);text = "Exact stored \(kind.rawValue) automation \(id) confirmed. No completed trigger or delivery is established."
            case .delete(let kind,let id):
                let fresh = try parse(kind,try await request(kind.path));guard let expected = approved.expected,let current = fresh[id],NativeAutomationWire.equal(expected,current) else { throw NativeAutomationError("This automation changed. Reload and review again.") };guard generation == version else { return false };try assertContext(approved);dispatched = true
                let path = kind == .inbound ? "/api/custom-webhooks/" : kind.path + "/"
                let result = try await request(path + NativeAutomationWire.segment(id),method:"DELETE");guard NativeAutomationWire.bool(result["success"]) == true else { throw NativeAutomationError("Deletion was not acknowledged.") }
                let after = try parse(kind,try await request(kind.path));guard after[id] == nil else { throw NativeAutomationError("Deleted automation still appears in readback.") };guard generation == version else { return false };install(kind,after);text = "Stored automation \(id) deleted. In-flight effects may remain."
            case .testOutgoing(let id):
                let fresh = try parse(.outgoing,try await request(NativeAutomationKind.outgoing.path));guard let expected = approved.expected,let current = fresh[id],NativeAutomationWire.equal(expected,current) else { throw NativeAutomationError("Outgoing target changed. Reload and review again.") };guard generation == version else { return false };try assertContext(approved);dispatched = true
                let result = try await request("/api/outgoing-webhooks/" + NativeAutomationWire.segment(id) + "/test",method:"POST",body:["event_type":"test.ping","payload":["message":"FERAL synthetic connection test"]]);guard result["event_type"] as? String == "test.ping",let delivered = NativeAutomationWire.bool(result["delivered"]) else { throw NativeAutomationError("No exact synthetic delivery verdict returned.") };guard generation == version else { return false };text = delivered ? "Backend reports synthetic test delivered. Future delivery is not guaranteed." : "Backend reports synthetic test delivery failed."
            }
            try assertContext(approved)
            receipt = text + " Scope: global automation store, not selected-chat context."
            return true
        } catch { if generation == version { receipt = nil;self.error = error.localizedDescription + (dispatched ? " Action outcome may be partial or uncertain; reload before another review. No automatic retry." : "");if case .load(let kind) = approved.action { rows[kind] = nil;originals[kind] = nil;errors[kind] = self.error } };return false }
    }
}

struct NativeAutomationFeatureView:View {
    let baseURL:URL?,sessionID:String?
    let contextPolicy:NativeSelectedContextPolicy
    init(baseURL:URL?,sessionID:String? = nil,contextPolicy:NativeSelectedContextPolicy = .legacy) { self.baseURL = baseURL;self.sessionID = sessionID;self.contextPolicy = contextPolicy }
    @StateObject private var model = NativeAutomationModel()
    @State private var kind = NativeAutomationKind.geofences
    @State private var review:NativeAutomationReview?
    @State private var localError:String?
    @State private var name = ""
    @State private var latitude = ""
    @State private var longitude = ""
    @State private var radius = "200"
    @State private var enter = ""
    @State private var exit = ""
    @State private var secret = ""
    @State private var target = ""
    @State private var filters = ""
    @State private var prefix = "Webhook received: "
    @State private var routine = ""
    @State private var enabled = false
    var body:some View {
        VStack(alignment:.leading,spacing:14) {
            Text("Automation triggers").font(.title.bold())
            Text(contextPolicy.message).font(.caption).foregroundStyle(.secondary)
            Text("Location triggers, signed webhooks and routine run metadata.").foregroundStyle(.secondary)
            Picker("Trigger type",selection:$kind) { ForEach(NativeAutomationKind.allCases,id:\.self) { Text($0.rawValue.capitalized).tag($0) } }.pickerStyle(.segmented)
            Button("Review loading \(kind.rawValue)…") { prepare(.load(kind)) }.disabled(model.busy || baseURL == nil)
            if let error = localError ?? model.error { Text(error).foregroundStyle(.orange) }
            if let receipt = model.receipt { Text(receipt).foregroundStyle(.secondary) }
            ScrollView {
                VStack(alignment:.leading,spacing:14) {
                    if let rows = model.rows[kind] {
                        if rows.isEmpty { Text("Inventory returned no records. Backend engine/store availability remains unverified.") }
                        ForEach(rows) { row in VStack(alignment:.leading,spacing:6) {
                            Text(row.fields["name"] as? String ?? row.id).font(.headline)
                            if kind == .outgoing { Text("Recipient: \(row.fields["target_host"] as? String ?? "Unavailable") · Enabled: \(NativeAutomationWire.bool(row.fields["enabled"]).map { $0 ? "Yes" : "No" } ?? "Unknown")");Button("Review synthetic delivery test…") { prepare(.testOutgoing(row.id)) }.disabled(!contextPolicy.taskReady) }
                            if kind == .inbound { Text("Signed: \(NativeAutomationWire.bool(row.fields["signed"]) == true ? "Yes" : "No — unsigned endpoint") · Action: \(row.fields["action"] as? String ?? "Unknown")");Text("Receive path: /api/custom-webhooks/\(row.id)/receive").font(.system(.caption,design:.monospaced)) }
                            if kind == .geofences { Text("Coordinates: \(String(describing:row.fields["lat"] ?? "Unknown")), \(String(describing:row.fields["lon"] ?? "Unknown")) · Radius: \(String(describing:row.fields["radius_m"] ?? "Unknown")) m") }
                            Button("Review delete…",role:.destructive) { prepare(.delete(kind,row.id)) }
                        }.padding().frame(maxWidth:.infinity,alignment:.leading).background(.quaternary,in:RoundedRectangle(cornerRadius:12)) }
                        draft
                    } else { Text(model.errors[kind] == nil ? "Not loaded. Review the local read first." : "Unavailable; no empty inventory is assumed.") }
                    Divider();Text("Routine run inspection").font(.headline);TextField("Existing routine ID",text:$routine);Button("Review reading last 20 runs…") { prepare(.runs(routine)) }
                    if let dispatch = model.routineDispatch { Text(dispatch.message).foregroundStyle(dispatch.requiresReconciliation ? Color.orange : Color.secondary) }
                    if let runs = model.runRows { Text("\(runs.count) run records for routine \(model.routineID ?? ""). Raw payload/output/error content withheld.");ForEach(Array(runs.enumerated()),id:\.offset) { _,run in Text("Status: \(safeRunStatus(run["status"])) · Started: \(String(describing:run["started_at"] ?? "Unknown"))").font(.caption) } }
                }.disabled(model.busy)
            }
        }.padding(24).task(id:baseURL?.absoluteString ?? "") { review = nil;secret = "";model.setContextPolicy(contextPolicy);model.configure(baseURL) }.onChange(of:kind) { _ in secret = "";review = nil }
        .onChange(of:contextPolicy) { policy in review = nil;secret = "";model.setContextPolicy(policy) }
        .alert(review?.title ?? "Review automation",isPresented:Binding(get:{review != nil},set:{if !$0 {review = nil}})) {
            Button("Cancel",role:.cancel) {review = nil}
            Button("Confirm") { if let approved = review {review = nil;secret = "";Task { model.setContextPolicy(contextPolicy);_ = await model.perform(approved) }} }
        } message:{ Text(review?.detail ?? "") }
    }
    private func safeRunStatus(_ value:Any?) -> String { guard let text = value as? String,["success","failed","running","completed","pending","cancelled"].contains(text) else { return "Unrecognized / unavailable" };return text }
    private func prepare(_ action:NativeAutomationAction) { do { model.setContextPolicy(contextPolicy);review = try model.review(action);localError = nil } catch { localError = error.localizedDescription } }
    @ViewBuilder private var draft:some View {
        Text("Create a new trigger").font(.headline);TextField("Name (letters, digits, dash, dot, underscore)",text:$name)
        if kind == .geofences { TextField("Latitude",text:$latitude);TextField("Longitude",text:$longitude);TextField("Radius in metres",text:$radius);TextField("Enter action",text:$enter);TextField("Exit action",text:$exit) }
        else { SecureField("New signing secret",text:$secret);if kind == .inbound { TextField("Agent command prefix",text:$prefix) } else { TextField("HTTPS recipient URL",text:$target);TextField("Explicit event filters, comma separated",text:$filters);Toggle("Enable recurring event delivery",isOn:$enabled) } }
        Button("Review creating trigger…") {
            var body:[String:Any] = ["name":name]
            if kind == .geofences { body["lat"] = Double(latitude);body["lon"] = Double(longitude);body["radius_m"] = Double(radius);body["on_enter"] = enter;body["on_exit"] = exit }
            else if kind == .inbound { body["secret"] = secret;body["action"] = "chat";body["action_params"] = ["prefix":prefix] }
            else { body["secret"] = secret;body["target_url"] = target;body["event_types"] = filters.split(separator:",").map { $0.trimmingCharacters(in:.whitespacesAndNewlines) };body["enabled"] = enabled }
            prepare(.create(kind,body))
        }.disabled(!contextPolicy.taskReady)
    }
}
