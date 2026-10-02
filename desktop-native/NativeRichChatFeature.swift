import Foundation
import SwiftUI
import CoreFoundation

private func richJSON(_ value:Any)->String { if let text = value as? String { return text }; guard JSONSerialization.isValidJSONObject(value),let data = try? JSONSerialization.data(withJSONObject:value,options:[.sortedKeys,.prettyPrinted]) else { return String(describing:value) }; return String(decoding:data,as:UTF8.self) }
private func richResetDate(_ value:Any?)->String { guard let n=value as? NSNumber,n.doubleValue.isFinite else{return "unknown"};return Date(timeIntervalSince1970:n.doubleValue).formatted() }
private func richBool(_ value:Any?)->Bool? { guard let n = value as? NSNumber,CFGetTypeID(n)==CFBooleanGetTypeID() else { return nil }; return n.boolValue }
private func richCorrelation(_ value:String)->String {switch value{case "single_pending_no_call_id":return "Matched the single pending tool without a call ID; identity is inferred.";case "ambiguous_no_call_id":return "Missing call ID and multiple pending calls; identity/outcome pairing is unresolved.";case "unmatched_no_call_id":return "Backend omitted a call ID; no exact correlation is available.";default:return ""}}
enum NativeRichPath {
 /// Mirrors Path.expanduser().resolve(strict=False) without assuming the
 /// backend's working directory or lexically erasing a symlink before '..'.
 static func canonical(_ raw:String) -> String? {
  guard !raw.isEmpty,raw.utf8.count <= 4096,!raw.contains("\0") else{return nil}
  let expanded=(raw as NSString).expandingTildeInPath
  guard expanded.hasPrefix("/") else{return nil}
  var pending=expanded.components(separatedBy:"/"),resolved:[String]=[],links=0
  while !pending.isEmpty {
   let part=pending.removeFirst()
   if part.isEmpty || part=="." {continue}
   if part==".." {if !resolved.isEmpty{resolved.removeLast()};continue}
   let path="/"+(resolved+[part]).joined(separator:"/")
   do {
    let attrs=try FileManager.default.attributesOfItem(atPath:path)
    if attrs[.type] as? FileAttributeType == .typeSymbolicLink {
     links+=1;guard links<=40 else{return nil}
     let target=try FileManager.default.destinationOfSymbolicLink(atPath:path)
     if target.hasPrefix("/"){resolved=[]}
     pending=target.components(separatedBy:"/")+pending
     continue
    }
   }catch{
    let code=(error as NSError).code
    guard code==NSFileReadNoSuchFileError || code==NSFileNoSuchFileError else{return nil}
   }
   resolved.append(part)
  }
  return "/"+resolved.joined(separator:"/")
 }
}
struct NativeRichTool:Identifiable { let id:String, tool:String; var label:String, status:String, arguments:String, result:String, error:String, errorCode:String; var milliseconds:Double?, truncated:Bool; var raw:[[String:Any]];var correlation="exact_call_id" }
struct NativeRichEvent:Identifiable {
 let id:String, kind:String, title:String, text:String; let raw:[String:Any]
 /// Only known transport/implementation telemetry is tucked away. Unknown
 /// events stay visible, so a new approval or outcome cannot disappear here.
 var diagnosticsOnly:Bool {
  if kind == "reasoning" { return true } // Reasoning has its own disclosure.
  guard kind == "brain_event",let payload=raw["payload"] as? [String:Any],let event=payload["event"] as? String else{return false}
  return ["llm_call","memory_write","tool_exec"].contains(event)
 }
}
struct NativeRichPermission:Identifiable {
 let id:String,session:String,path:String,operation:String,reason:String,generation:UUID
 let raw:[String:Any]
 let canonicalPath:String?
 var state:String="pending"
 var supported:Bool { ["read","write","readwrite"].contains(operation) && canonicalPath != nil && id.count <= 128 && !id.isEmpty && id.unicodeScalars.allSatisfy{CharacterSet(charactersIn:"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.").contains($0)} }
 var expired:Bool { guard let expiry=(raw["payload"] as? [String:Any])?["expires_at"] as? NSNumber else{return false};return !expiry.doubleValue.isFinite || expiry.doubleValue <= Date().timeIntervalSince1970 }
 var scope:String { "Persistent folder policy for this brain, including descendants of \(canonicalPath ?? "an unresolved folder"); \(operation != "read" ? "read and write" : "read") access. This is not an allow-once or session-only grant." }
}
struct NativeRichPermissionReview { let permission:NativeRichPermission, allow:Bool, signature:String }
struct NativeRichPermissionResponse { let sessionID:String, requestID:String, granted:Bool, connectionID:UUID; var wireFrame:[String:Any] { ["type":"ui_event","hop":"client","session_id":sessionID,"payload":["screen_id":"chat","action_id":(granted ? "perm_grant_" : "perm_deny_")+requestID,"event":"tap"]] } }
private struct RichFailure:LocalizedError { let message:String; var errorDescription:String? {message} }

@MainActor final class NativeRichChatModel:ObservableObject {
 @Published private(set) var tools:[NativeRichTool] = []
 @Published private(set) var events:[NativeRichEvent] = []
 @Published private(set) var permissions:[NativeRichPermission] = []
 @Published private(set) var reasoning = ""
 @Published private(set) var plan:[String:Any]?
 @Published private(set) var budgets:[String:[String:Any]] = [:]
 @Published private(set) var error:String?
 private var sessionID:String?
 private var connectionID:UUID?
 private var generation = UUID()
 private var inFlight = Set<String>()
 private let resolvePath:(String)->String?
 init(resolvePath:@escaping(String)->String? = NativeRichPath.canonical){self.resolvePath=resolvePath}
 func configure(sessionID:String?,connectionID:UUID?) { guard self.sessionID != sessionID || self.connectionID != connectionID else {return}; self.sessionID = sessionID; self.connectionID = connectionID; generation = UUID(); tools = []; events = []; permissions = []; reasoning = ""; plan = nil; budgets = [:]; error = nil; inFlight = [] }
 /// Returns true only for structured frames handled here. Main chat retains ownership of terminal text/error and ordinary text deltas.
 @discardableResult func consume(_ frame:[String:Any],connectionID:UUID)->Bool {
  guard self.connectionID==connectionID,let sid = sessionID,!sid.isEmpty else {return false}
  let type = frame["type"] as? String ?? ""; let payload = frame["payload"] as? [String:Any] ?? [:]
  let addressed = frame["session_id"] as? String ?? payload["session_id"] as? String
  let broadcastBudget = type=="state_push" && frame["event"] as? String=="cost_cap_hit"
  guard broadcastBudget || addressed==sid else {return false}
  if type=="stream_delta",payload["kind"] as? String=="reasoning" { reasoning += payload["delta"] as? String ?? ""; return true }
  if type=="reasoning" { reasoning += payload["text"] as? String ?? payload["delta"] as? String ?? ""; addEvent(type,title:"Model reasoning",text:"Provider-supplied reasoning, not proof of action.",raw:frame); return true }
  if ["text_response","chat_response"].contains(type) { if let value = payload["reasoning"] as? String,!value.isEmpty,reasoning.isEmpty {reasoning=value}; return false }
  if ["tool_start","tool_call","skill_start","tool_result","skill_result"].contains(type) {
   let tool = payload["tool"] as? String ?? payload["name"] as? String ?? "unknown"; let call = payload["call_id"] as? String ?? ""; let terminal = ["tool_result","skill_result"].contains(type)
   var key = call.isEmpty ? UUID().uuidString : call + ":" + tool
   var old = call.isEmpty ? nil : tools.firstIndex{$0.id==key}
   var correlation=call.isEmpty ? "unmatched_no_call_id" : "exact_call_id"
   if call.isEmpty && terminal {
    let starts:Set<String>=type=="skill_result" ? ["skill_start"] : ["tool_start","tool_call"]
    let candidates=tools.indices.filter {index in
     let candidate=tools[index]
     guard candidate.tool==tool,candidate.status=="running",let first=candidate.raw.first,
           starts.contains(first["type"] as? String ?? "") else{return false}
     return ((first["payload"] as? [String:Any])?["call_id"] as? String ?? "").isEmpty
    }
    if candidates.count==1 {old=candidates[0];key=tools[candidates[0]].id;correlation="single_pending_no_call_id"}
    else if candidates.count>1 {
     correlation="ambiguous_no_call_id"
     for index in candidates {tools[index].status="outcome_unresolved";tools[index].correlation="ambiguous_no_call_id"}
    }
   }
   var record = old.map{tools[$0]} ?? NativeRichTool(id:key,tool:tool,label:payload["display_name"] as? String ?? tool,status:"running",arguments:"",result:"",error:"",errorCode:"",milliseconds:nil,truncated:false,raw:[])
   record.correlation=correlation
   record.raw.append(frame)
   if terminal { let success = richBool(payload["success"]); record.errorCode = payload["error_code"] as? String ?? ""; record.status = ["policy_denied","plan_mode_blocked","pending_approval"].contains(record.errorCode) ? record.errorCode : success==true ? "reported_success" : success==false ? "reported_failure" : "outcome_unknown"; record.error = payload["error"] as? String ?? ""; if let result = payload["result_preview"] ?? payload["result"] ?? payload["output"] {record.result=richJSON(result)}; let latency = (payload["latency_ms"] as? NSNumber)?.doubleValue; record.milliseconds=latency.flatMap{$0.isFinite && $0>=0 ? $0:nil}; record.truncated = richBool(payload["result_preview_truncated"])==true || richBool(payload["preview_truncated"])==true }
   else { if let args = payload["args_preview"] ?? payload["args"] ?? payload["arguments"] ?? payload["params"] {record.arguments=richJSON(args)} }
   if let old {tools[old]=record} else {tools.append(record)}; if tools.count>100 {tools.removeFirst(tools.count-100)}; return true
  }
  if type=="permission_request" { guard let id = payload["request_id"] as? String,!id.isEmpty,let path = payload["path"] as? String,let operation = payload["operation"] as? String else { addEvent("unsupported_permission",title:"Incomplete permission request",text:"No approval controls available; required backend identifiers or scope are missing.",raw:frame); return true }; let p = NativeRichPermission(id:id,session:sid,path:path,operation:operation,reason:payload["reason"] as? String ?? "",generation:generation,raw:frame,canonicalPath:resolvePath(path)); if let index = permissions.firstIndex(where:{$0.id==id}) { if permissions[index].state=="pending" {permissions[index]=p} } else {permissions.append(p)}; return true }
  if type=="permission_decision" {
   guard let id=payload["request_id"] as? String,let index=permissions.firstIndex(where:{$0.id==id && $0.session==sid && $0.generation==generation}),let status=payload["status"] as? String else{return false}
   if status=="granted" {let p=permissions[index];let mode=p.operation=="read" ? "read" : "readwrite";guard let canonical=p.canonicalPath,payload["path"] as? String==canonical,payload["mode"] as? String==mode,payload["scope"] as? String=="persistent_workspace" else{permissions[index].state="receipt_mismatch";error="Grant receipt scope, canonical path or mode did not match the review. Persistent access may already have been granted; inspect Security before retrying.";return true};permissions[index].state="granted_confirmed"}
   else if ["denied","expired","error"].contains(status){permissions[index].state=status}
   else{return false};return true
  }
  if type=="plan_mode" {guard payload["session_id"] as? String==sid,richBool(payload["plan_mode"]) != nil else{return false};plan=payload;return true}
  if type=="budget_exceeded" || broadcastBudget {let data = broadcastBudget ? frame["data"] as? [String:Any] ?? [:] : payload; let site = data["call_site"] as? String ?? "unknown"; budgets[site]=data;return true}
  if type=="budget_reset" {if let site = payload["call_site"] as? String ?? frame["call_site"] as? String {budgets[site]=nil};return true}
  if ["refusal","error","brain_event","skill_proposal","timeline","sdui","sdui_patch"].contains(type) { let title = type=="refusal" ? "Request declined" : type=="error" ? "Agent error" : type=="sdui" || type=="sdui_patch" ? "Structured content — inspection only" : type=="timeline" ? "Timeline" : type=="skill_proposal" ? "Skill proposal — not installed" : ((payload["event"] as? String ?? "Agent event").replacingOccurrences(of:"_",with:" ")); addEvent(type,title:title,text:payload["reason"] as? String ?? payload["message"] as? String ?? payload["text"] as? String ?? payload["summary"] as? String ?? payload["status"] as? String ?? "",raw:frame); return type != "error" }
  return false
 }
 private func addEvent(_ kind:String,title:String,text:String,raw:[String:Any]) {events.append(NativeRichEvent(id:UUID().uuidString,kind:kind,title:title,text:text,raw:raw));if events.count>100 {events.removeFirst(events.count-100)}}
 var visibleEvents:[NativeRichEvent] {events.filter{!$0.diagnosticsOnly}}
 var diagnosticEvents:[NativeRichEvent] {events.filter{$0.diagnosticsOnly}}
 var eventRecords:[[String:Any]] {events.map{["type":$0.kind,"title":$0.title,"text":$0.text,"raw_frame":$0.raw]}}
 func clearTurnPresentation() { tools=[]; reasoning="" } // Call when a new user turn starts, after archiving the prior turn metadata.
 var turnMetadata:[String:Any] { ["reasoning":reasoning,"tools":tools.map{["tool":$0.tool,"status":$0.status,"correlation":$0.correlation,"args_preview":$0.arguments,"result_preview":$0.result,"error":$0.error,"error_code":$0.errorCode,"raw_frames":$0.raw] as [String:Any]}] }
 func reviewPermission(_ id:String,allow:Bool) throws -> NativeRichPermissionReview { guard let p = permissions.first(where:{$0.id==id}),p.generation==generation,p.session==sessionID,p.supported,!p.expired,p.state=="pending",!inFlight.contains(id),connectionID != nil else {throw RichFailure(message:"This permission is stale, unsupported or already answered.")};if allow,resolvePath(p.path) != p.canonicalPath{throw RichFailure(message:"This folder now resolves to a different path. Ask for a fresh scope review before granting access.")};return NativeRichPermissionReview(permission:p,allow:allow,signature:richJSON(p.raw)) }
 func canRespond(_ review:NativeRichPermissionReview)->Bool { guard let p=permissions.first(where:{$0.id==review.permission.id}) else{return false};return p.generation==generation && p.session==sessionID && p.supported && !p.expired && p.state=="pending" && !inFlight.contains(p.id) && richJSON(p.raw)==review.signature && p.canonicalPath==review.permission.canonicalPath && (!review.allow || resolvePath(p.path)==review.permission.canonicalPath) && connectionID != nil }
 func respond(_ review:NativeRichPermissionReview,send:@escaping(NativeRichPermissionResponse) async throws -> Void) async {
  guard canRespond(review),let connectionID else {error="Permission review changed. Review the current request again.";return};let id=review.permission.id;let started=generation;inFlight.insert(id);if let index=permissions.firstIndex(where:{$0.id==id}){permissions[index].state="responding"};defer{if started==generation{inFlight.remove(id)}}
  do {try await send(NativeRichPermissionResponse(sessionID:review.permission.session,requestID:id,granted:review.allow,connectionID:connectionID));guard started==generation else{return};if let index=permissions.firstIndex(where:{$0.id==id}){if permissions[index].state=="responding" {permissions[index].state="response_sent_unconfirmed"};if permissions[index].state != "receipt_mismatch"{error=nil}}}
  catch {if started==generation{if let index=permissions.firstIndex(where:{$0.id==id}),permissions[index].state=="responding" {permissions[index].state="delivery_uncertain"};self.error="Permission delivery is not confirmed. Inspect current backend state before retrying."}}
 }
 func report(_ message:String){error=message}
}

struct NativeRichMessageView:View {
 let text:String
 let role:String
 let metadata:[String:Any]
 init(text:String,role:String,metadata:[String:Any]=[:]) {self.text=text;self.role=role;self.metadata=metadata}
 var body:some View {
  VStack(alignment:.leading,spacing:10) {
   if let reasoning=metadata["reasoning"] as? String,!reasoning.isEmpty {DisclosureGroup("Model reasoning") {Text(reasoning).font(.callout).foregroundStyle(.secondary).textSelection(.enabled)}}
   if let tools=metadata["tools"] as? [[String:Any]] {ForEach(Array(tools.enumerated()),id:\.offset){_,tool in NativeRichEventCard(title:tool["label"] as? String ?? tool["tool"] as? String ?? "Tool",subtitle:tool["status"] as? String ?? "Recorded outcome",text:(tool["result_preview"] as? String ?? "")+"\n"+richCorrelation(tool["correlation"] as? String ?? ""),raw:tool)}}
   if role=="assistant" {NativeRichText(text:text)} else if !text.isEmpty {Text(text).textSelection(.enabled)}
   if metadata["model"] != nil || metadata["usage"] != nil || metadata["sdui"] != nil || metadata["content"] is [Any] {
    DisclosureGroup("Response details") {
     VStack(alignment:.leading,spacing:8) {
      if let model=metadata["model"] as? String,!model.isEmpty {Text("Model: "+model)}
      if let usage=metadata["usage"] as? [String:Any],!usage.isEmpty {Text("Reported usage").fontWeight(.medium);Text(richJSON(usage)).font(.system(.caption,design:.monospaced))}
      if metadata["sdui"] != nil || metadata["content"] is [Any] {Text("Structured content · inspection only").fontWeight(.medium);Text(richJSON(metadata)).font(.system(.caption,design:.monospaced))}
     }.textSelection(.enabled)
    }.font(.caption).foregroundStyle(.secondary)
   }
  }.frame(maxWidth:.infinity,alignment:.leading)
 }
}
struct NativeRichEventCard:View {
 let title:String,subtitle:String,text:String
 let raw:[String:Any]
 var body:some View {VStack(alignment:.leading,spacing:8){Text(title).font(.headline);if !subtitle.isEmpty{Text(subtitle).font(.caption).foregroundStyle(.secondary)};if !text.isEmpty{Text(text).textSelection(.enabled)};DisclosureGroup("Inspect exact metadata"){Text(richJSON(raw)).font(.system(.caption,design:.monospaced)).textSelection(.enabled)}}.padding(14).frame(maxWidth:.infinity,alignment:.leading).background(RoundedRectangle(cornerRadius:10).fill(Color.secondary.opacity(0.06)))}
}
struct NativeRichChatEventsView:View {
 @ObservedObject var model:NativeRichChatModel
 let onPermissionResponse:((NativeRichPermissionResponse) async throws -> Void)?
 @State private var review:NativeRichPermissionReview?
 init(model:NativeRichChatModel,onPermissionResponse:((NativeRichPermissionResponse) async throws -> Void)?=nil){self.model=model;self.onPermissionResponse=onPermissionResponse}
 var body:some View {
  VStack(alignment:.leading,spacing:12){
   if let error=model.error{Text(error).foregroundStyle(.red).textSelection(.enabled)}
   if !model.reasoning.isEmpty{DisclosureGroup("Model reasoning (live)"){Text(model.reasoning).font(.callout).textSelection(.enabled)}}
   ForEach(model.tools){tool in NativeRichEventCard(title:tool.label,subtitle:tool.status.replacingOccurrences(of:"_",with:" ")+" · backend-reported status, not independent verification",text:(tool.error.isEmpty ? "" : tool.error+"\n")+(tool.result.isEmpty ? "No result preview offered." : tool.result)+(tool.truncated ? "\nResult preview is truncated." : "")+"\n"+richCorrelation(tool.correlation),raw:["arguments":tool.arguments,"error":tool.error,"error_code":tool.errorCode,"preview_truncated":tool.truncated,"correlation":tool.correlation,"frames":tool.raw])}
   ForEach(model.permissions){p in VStack(alignment:.leading,spacing:8){Text("Folder access request").font(.headline);Text("Requested folder: "+p.path).textSelection(.enabled);Text("Canonical folder: "+(p.canonicalPath ?? "Unresolved — inspection only")).textSelection(.enabled);Text("Operation: "+p.operation+" · "+p.state).font(.caption);Text(p.reason).textSelection(.enabled);Text(p.scope).font(.callout);if p.supported,!p.expired,p.state=="pending",onPermissionResponse != nil{HStack{Button("Review persistent grant…"){prepare(p.id,allow:true)};Button("Review deny…"){prepare(p.id,allow:false)}}}else{Text("No live approval controls. Sent responses remain unconfirmed until the backend reports the actual policy state.").font(.caption).foregroundStyle(.secondary)};DisclosureGroup("Exact request"){Text(richJSON(p.raw)).font(.system(.caption,design:.monospaced)).textSelection(.enabled)}}.padding(14).background(RoundedRectangle(cornerRadius:10).fill(Color.orange.opacity(0.06)))}
   ForEach(model.budgets.keys.sorted(),id:\.self){site in let b=model.budgets[site] ?? [:];NativeRichEventCard(title:"Budget limit hit",subtitle:site,text:"Current $\(richJSON(b["current_dollars"] ?? "unknown")) / cap $\(richJSON(b["cap_dollars"] ?? "unknown")); reset at \(richResetDate(b["reset_at"])). A reset is not a billing repair or permission to retry automatically.",raw:b)}
   if let plan=model.plan{NativeRichEventCard(title:richBool(plan["plan_mode"])==true ? "Plan mode on" : "Plan mode off",subtitle:"Plan approval never grants tool permissions",text:plan["reason"] as? String ?? "",raw:plan)}
   ForEach(model.visibleEvents){event in NativeRichEventCard(title:event.title,subtitle:event.kind,text:event.text,raw:event.raw)}
   if !model.diagnosticEvents.isEmpty {
    DisclosureGroup("Conversation diagnostics (\(model.diagnosticEvents.count))") {
     VStack(alignment:.leading,spacing:8) {
      ForEach(model.diagnosticEvents){event in NativeRichEventCard(title:event.title,subtitle:event.kind,text:event.text,raw:event.raw)}
     }.padding(.top,8)
    }.font(.caption).foregroundStyle(.secondary).accessibilityIdentifier("native-chat-diagnostics")
   }
  }.frame(maxWidth:.infinity,alignment:.leading)
  .sheet(item:Binding(get:{review.map(RichPermissionSheet.init)},set:{if $0==nil{review=nil}})){item in VStack(alignment:.leading,spacing:16){Text(item.review.allow ? "Grant persistent folder access?" : "Deny this request?").font(.title2);Text("Requested folder: "+item.review.permission.path).textSelection(.enabled);Text("Canonical folder reviewed: "+(item.review.permission.canonicalPath ?? "Unresolved")).textSelection(.enabled);Text(item.review.permission.scope);Text("The canonical path is rechecked before sending. The backend has no atomic path-revision guard; an unexpected receipt requires inspecting Security before retrying.").font(.caption);Text("Request: \(item.review.permission.id) · Session: \(item.review.permission.session)").font(.caption).textSelection(.enabled);HStack{Button("Cancel"){review=nil};Spacer();Button(item.review.allow ? "Send persistent grant response" : "Send denial"){review=nil;Task{if let callback=onPermissionResponse{await model.respond(item.review,send:callback)}}}.disabled(!model.canRespond(item.review))}}.padding(24).frame(width:600,height:450)}
 }
 private func prepare(_ id:String,allow:Bool){do{review=try model.reviewPermission(id,allow:allow)}catch{model.report(error.localizedDescription)}}
}
private struct RichPermissionSheet:Identifiable{let review:NativeRichPermissionReview;var id:String{review.permission.generation.uuidString+review.permission.id};init(_ review:NativeRichPermissionReview){self.review=review}}
