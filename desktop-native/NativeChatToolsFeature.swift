import Foundation
import SwiftUI
import CoreFoundation

enum NativeChatToolsTab: String, CaseIterable { case plan = "Plan & Todos", thoughts = "Resumable state", snapshots = "Saved points" }
struct NativeChatEntity: Identifiable { let id: String, kind: String, status: String, summary: String, owner: String; let raw: [String:Any] }
struct NativeChatSnapshot: Identifiable { let id: String, session: String, label: String, branch: String; let date: Date }
struct NativeChatToolReview { let generation:UUID, actionRevision:UUID, operation:String, id:String, summary:String, body:[String:Any], signature:String }
struct NativeChatThreadResult { let session:String; let history:[[String:Any]]; let generation:UUID; let actionRevision:UUID; let origin:URL; let sourceSession:String }
private struct ChatToolsFailure: LocalizedError { let message:String; var errorDescription:String? { message } }
private func chatToolsJSON(_ object:Any)->String { guard let data = try? JSONSerialization.data(withJSONObject:object,options:[.sortedKeys,.prettyPrinted]) else { return "" }; return String(decoding:data,as:UTF8.self) }
private func chatToolsBool(_ value:Any?)->Bool? { guard let n = value as? NSNumber, CFGetTypeID(n)==CFBooleanGetTypeID() else { return nil }; return n.boolValue }
final class NativeChatToolsRedirectGuard:NSObject,URLSessionTaskDelegate {
 func urlSession(_ session:URLSession,task:URLSessionTask,willPerformHTTPRedirection response:HTTPURLResponse,newRequest request:URLRequest,completionHandler:@escaping(URLRequest?)->Void) { guard let a = task.originalRequest?.url,let b = request.url,a.scheme==b.scheme,a.host==b.host,a.port==b.port,b.user==nil,b.password==nil else { completionHandler(nil); return }; completionHandler(request) }
}
@MainActor final class NativeChatToolsModel:ObservableObject {
 @Published var tab:NativeChatToolsTab = .plan
 @Published var reason = ""
 @Published var label = ""
 @Published var branchName = ""
 @Published private(set) var plan:[String:Any] = [:]
 @Published private(set) var entities:[NativeChatEntity] = []
 @Published private(set) var snapshots:[NativeChatSnapshot] = []
 @Published private(set) var inspected:[String:Any]?
 @Published private(set) var busy = false
 @Published private(set) var isChatBusy = false
 @Published private(set) var applyingHistory = false
 @Published private(set) var error:String?
 @Published private(set) var notice:String?
 @Published private(set) var fresh = Set<NativeChatToolsTab>()
 private var baseURL:URL?
 private var sessionID:String?
 private var generation = UUID()
 private var actionRevision = UUID()
 private let session:URLSession
 init(baseURL:URL?,sessionID:String?,isChatBusy:Bool = false,session:URLSession? = nil) { self.baseURL = baseURL; self.sessionID = sessionID; self.isChatBusy = isChatBusy; self.session = session ?? URLSession(configuration:.ephemeral,delegate:NativeChatToolsRedirectGuard(),delegateQueue:nil) }
 func configure(baseURL:URL?,sessionID:String?) { guard self.baseURL != baseURL || self.sessionID != sessionID else { return }; self.baseURL = baseURL; self.sessionID = sessionID; generation = UUID(); actionRevision = UUID(); plan = [:]; entities = []; snapshots = []; inspected = nil; fresh = []; error = nil; notice = nil; busy = false; applyingHistory = false; reason = ""; label = ""; branchName = "" }
 func setChatBusy(_ value:Bool) { guard isChatBusy != value else { return }; isChatBusy = value; actionRevision = UUID() }
 func beginApply(_ thread:NativeChatThreadResult)->Bool { guard !busy,!applyingHistory,canApply(thread) else { return false }; applyingHistory = true; return true }
 func endApply(_ thread:NativeChatThreadResult) { guard thread.generation==generation else { return }; applyingHistory = false }
 private func mutate(_ path:String,reviewed:NativeChatToolReview) async throws -> [String:Any] {
  guard !isChatBusy,!applyingHistory,reviewed.generation==generation,reviewed.actionRevision==actionRevision else { throw ChatToolsFailure(message:"The active chat changed or is busy, or saved history is being applied. Stop or wait, then review this change again.") }
  let result = try await request(path,method:"POST",body:reviewed.body)
  guard !isChatBusy,reviewed.actionRevision==actionRevision else { throw ChatToolsFailure(message:"Chat activity changed during the request. Its result is not confirmed here; reload actual state before retrying.") }; return result
 }
 private func identifier(_ value:String?) throws -> String { guard let value,!value.isEmpty,value.unicodeScalars.allSatisfy({ CharacterSet(charactersIn:"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:").contains($0) }) else { throw ChatToolsFailure(message:"A valid active session or item identifier is required.") }; return value }
 private func request(_ path:String,method:String = "GET",body:[String:Any]? = nil,query:[URLQueryItem] = []) async throws -> [String:Any] {
  let started = generation; try Task.checkCancellation(); _ = try identifier(sessionID)
  guard let baseURL,["127.0.0.1","::1","[::1]"].contains(baseURL.host ?? ""),["http","https"].contains(baseURL.scheme ?? ""),baseURL.user==nil,baseURL.password==nil,var c = URLComponents(url:baseURL,resolvingAgainstBaseURL:false) else { throw ChatToolsFailure(message:"Connect to the local agent to use conversation tools.") }; c.path = path; c.queryItems = query.isEmpty ? nil : query; c.fragment = nil; guard let url = c.url else { throw ChatToolsFailure(message:"Invalid conversation request.") }; var req = URLRequest(url:url); req.httpMethod = method; req.timeoutInterval = 60
  if let body { req.httpBody = try JSONSerialization.data(withJSONObject:body); req.setValue("application/json",forHTTPHeaderField:"Content-Type") }
  let (data,response) = try await session.data(for:req); try Task.checkCancellation(); guard started==generation else { throw ChatToolsFailure(message:"Conversation or agent changed. Review again.") }; guard let http = response as? HTTPURLResponse,(200..<300).contains(http.statusCode),let row = (try? JSONSerialization.jsonObject(with:data)) as? [String:Any] else { throw ChatToolsFailure(message:"The agent did not confirm this request.") }; if let e = row["error"] as? String,!e.isEmpty { throw ChatToolsFailure(message:"The agent reported a conversation operation failure.") }; return row
 }
 func refresh() async {
  guard !busy else { return }; let started = generation, selected = tab; busy = true; defer { if started==generation { busy = false } }
  do {
   let sid = try identifier(sessionID)
   switch selected {
   case .plan: let row = try await request("/api/sessions/\(sid)/plan_mode"); guard row["session_id"] as? String==sid,chatToolsBool(row["plan_mode"]) != nil else { throw ChatToolsFailure(message:"Plan state is incomplete or belongs to another session.") }; plan = row
   case .thoughts: let row = try await request("/api/consciousness/state",query:[URLQueryItem(name:"owner_session_id",value:sid)]); guard let list = row["entities"] as? [[String:Any]] else { throw ChatToolsFailure(message:"Resumable state listing is incomplete.") }; entities = try list.map { r in guard let id = r["id"] as? String,let owner = r["owner_session_id"] as? String,owner==sid,let kind = r["kind"] as? String,let status = r["status"] as? String else { throw ChatToolsFailure(message:"An entity is not owned by this session.") }; _ = try identifier(id); return NativeChatEntity(id:id,kind:kind,status:status,summary:r["summary"] as? String ?? "",owner:owner,raw:r) }; guard Set(entities.map(\.id)).count==entities.count else { throw ChatToolsFailure(message:"Duplicate entity identifiers.") }
   case .snapshots: let row = try await request("/api/session/snapshots",query:[URLQueryItem(name:"session_id",value:sid),URLQueryItem(name:"limit",value:"50")]); guard let list = row["snapshots"] as? [[String:Any]] else { throw ChatToolsFailure(message:"Snapshot listing is incomplete.") }; snapshots = try list.map { r in guard let id = r["snapshot_id"] as? String,r["session_id"] as? String==sid else { throw ChatToolsFailure(message:"Snapshot belongs to another session.") }; _ = try identifier(id); return NativeChatSnapshot(id:id,session:sid,label:r["label"] as? String ?? "",branch:r["branch_name"] as? String ?? "",date:Date(timeIntervalSince1970:(r["created_at"] as? NSNumber)?.doubleValue ?? 0)) }
   }; fresh.insert(selected); error = nil
  } catch { if generation==started { fresh.remove(selected); self.error = error.localizedDescription } }
 }
 func inspect(_ id:String) async {
  guard !busy,fresh.contains(.snapshots),snapshots.contains(where:{$0.id==id}) else { return }; let started = generation; busy = true; inspected = nil; defer { if started==generation { busy = false } }
  do { let row = try await request("/api/session/snapshots/\(try identifier(id))"); guard row["snapshot_id"] as? String==id,row["session_id"] as? String==sessionID,row["history"] is [[String:Any]],row["working"] is [[String:Any]] else { throw ChatToolsFailure(message:"Snapshot details are incomplete or name another session.") }; inspected = row; error = nil } catch { if generation==started { self.error = error.localizedDescription } }
 }
 func review(_ operation:String,id:String = "") throws -> NativeChatToolReview {
  guard !isChatBusy,!applyingHistory else { throw ChatToolsFailure(message:"Wait for the active chat, conversation switch, upload or saved-history application before reviewing a change.") }; let sid = try identifier(sessionID); let selected:NativeChatToolsTab; var body:[String:Any] = [:],summary = "",signature = ""
  switch operation {
  case "plan_enter","plan_leave","plan_approve": selected = .plan; body = ["enabled":operation=="plan_enter","approved":operation=="plan_approve","reason":reason]; signature = chatToolsJSON(plan); summary = operation=="plan_enter" ? "Enter research-only plan mode for this session.\nReason: \(reason)" : "Leave plan mode for this session. Approving a plan records approval only; individual steps still require their normal permissions. This does not execute a plan automatically."
  case "snapshot": selected = .snapshots; body = ["session_id":sid,"label":label,"branch_name":"main"]; summary = "Save this session’s current runtime working memory and up to 200 model-history rows. This includes model conversation context and may contain private document data. It is not a file backup or a complete conversation export."
  case "branch","restore","restore_new": selected = .snapshots; guard let row = inspected,row["snapshot_id"] as? String==id else { throw ChatToolsFailure(message:"Inspect the exact saved point before branch or restore.") }; signature = chatToolsJSON(row); body = ["snapshot_id":id,"session_id":sid]; if operation=="branch" { let branch = branchName.trimmingCharacters(in:.whitespacesAndNewlines); guard !branch.isEmpty,branch.unicodeScalars.allSatisfy({ CharacterSet(charactersIn:"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.").contains($0) }) else { throw ChatToolsFailure(message:"Use letters, digits, dots, underscores or hyphens for the branch name.") }; body["branch_name"] = branch } else { body["as_new_session"] = operation=="restore_new" }; summary = "Saved point: \(id)\nSource session: \(sid)\nHistory rows: \((row["history"] as? [Any] ?? []).count) · working rows: \((row["working"] as? [Any] ?? []).count)\n\(operation=="restore" ? "Replaces this session’s runtime model history and working memory." : "Creates a separate runtime session from this saved point.") Does not undo file edits, external actions, durable episodes, personal facts, or approvals. Opening/persisting the resulting UI thread is a separate operation.\n\n" + chatToolsJSON(row)
  case "resume","pause","abandon": selected = .thoughts; guard let entity = entities.first(where:{$0.id==id}),["thought","flow","intent","turn","device_stream"].contains(entity.kind),operation != "resume" || entity.status=="paused" else { throw ChatToolsFailure(message:"This entity cannot perform the reviewed action.") }; body = ["id":id]; signature = chatToolsJSON(entity.raw); summary = "\(operation.capitalized) \(entity.kind): \(entity.summary)\nSession: \(entity.owner)\n" + (operation=="resume" ? "May re-enter flow/intent execution; thought text is queued for the next turn if the matching runtime is available. Status-only response is not proof of resumed execution." : "Changes the consciousness status. It does not cancel underlying work or undo file edits.") + "\n\n" + chatToolsJSON(entity.raw)
  default: throw ChatToolsFailure(message:"Unsupported conversation action.")
  }
  guard fresh.contains(selected) else { throw ChatToolsFailure(message:"Reload current state before reviewing a change.") }; return NativeChatToolReview(generation:generation,actionRevision:actionRevision,operation:operation,id:id,summary:summary,body:body,signature:signature)
 }
 func canApply(_ thread:NativeChatThreadResult)->Bool { !isChatBusy && thread.generation==generation && thread.actionRevision==actionRevision && thread.origin==baseURL && thread.sourceSession==sessionID }
 func report(_ message:String) { error = message }
 func execute(_ reviewed:NativeChatToolReview) async -> NativeChatThreadResult? {
  guard !busy,!isChatBusy,!applyingHistory,reviewed.generation==generation,reviewed.actionRevision==actionRevision else { error = "Review is stale. Reload and review again."; return nil }; let started = generation; busy = true; notice = nil; defer { if started==generation { busy = false } }
  do {
   let sid = try identifier(sessionID)
   switch reviewed.operation {
   case "plan_enter","plan_leave","plan_approve": guard fresh.contains(.plan) else { throw ChatToolsFailure(message:"Plan state is stale.") }; let current = try await request("/api/sessions/\(sid)/plan_mode"); guard chatToolsJSON(current)==reviewed.signature else { throw ChatToolsFailure(message:"Plan changed. Reload and review again.") }; let row = try await mutate("/api/sessions/\(sid)/plan_mode",reviewed:reviewed); guard row["session_id"] as? String==sid,chatToolsBool(row["plan_mode"])==chatToolsBool(reviewed.body["enabled"]) else { throw ChatToolsFailure(message:"Plan change was not confirmed.") }; plan = row; notice = "Plan posture updated; no task permissions granted."
   case "snapshot": guard fresh.contains(.snapshots) else { throw ChatToolsFailure(message:"Saved-point state is stale.") }; let row = try await mutate("/api/session/snapshot",reviewed:reviewed); guard let id = row["snapshot_id"] as? String,!id.isEmpty,row["session_id"] as? String==sid else { throw ChatToolsFailure(message:"Snapshot creation was not confirmed.") }; fresh.remove(.snapshots); notice = "Saved point created: \(id). Reload to inspect it."
   case "branch","restore","restore_new": guard fresh.contains(.snapshots),let source = inspected,chatToolsJSON(source)==reviewed.signature else { throw ChatToolsFailure(message:"Saved-point review is stale.") }; let current = try await request("/api/session/snapshots/\(try identifier(reviewed.id))"); guard chatToolsJSON(current)==reviewed.signature else { throw ChatToolsFailure(message:"Saved point changed. Inspect again.") }; let route = reviewed.operation=="branch" ? "branch" : "restore"; let row = try await mutate("/api/session/\(route)",reviewed:reviewed); guard row["status"] as? String==(route=="branch" ? "branched" : "restored"),let target = row["target_session_id"] as? String,!target.isEmpty,(row[route=="branch" ? "source_snapshot_id" : "restored_from_snapshot_id"] as? String)==reviewed.id,(reviewed.operation != "restore" || target==sid),(reviewed.operation=="restore" || target != sid) else { throw ChatToolsFailure(message:"Resulting session was not confirmed. Reload before retrying.") }; _ = try identifier(target); notice = "Runtime \(route) confirmed. Files, durable episodes and approvals were not reverted."; error = nil; return NativeChatThreadResult(session:target,history:source["history"] as? [[String:Any]] ?? [],generation:started,actionRevision:reviewed.actionRevision,origin:baseURL!,sourceSession:sid)
   case "resume","pause","abandon": guard fresh.contains(.thoughts) else { throw ChatToolsFailure(message:"Resumable state is stale.") }; let listing = try await request("/api/consciousness/state",query:[URLQueryItem(name:"owner_session_id",value:sid)]); guard let list = listing["entities"] as? [[String:Any]],let current = list.first(where:{$0["id"] as? String==reviewed.id}),chatToolsJSON(current)==reviewed.signature else { throw ChatToolsFailure(message:"Entity changed or belongs to another session. Reload and review again.") }; let row = try await mutate("/api/consciousness/\(reviewed.operation)",reviewed:reviewed); guard chatToolsBool(row["ok"])==true else { throw ChatToolsFailure(message:"Entity status update was not confirmed.") }; fresh.remove(.thoughts); let method = (row["rehydrated"] as? [String:Any])?["method"] as? String ?? "status_only"; notice = "Entity status update acknowledged. Rehydration method: \(method). This is not proof of running or completed work."
   default: throw ChatToolsFailure(message:"Unsupported conversation change.")
   }; error = nil; return nil
  } catch { if generation==started { fresh.remove(tab); self.error = "Change not confirmed. " + error.localizedDescription }; return nil }
 }
}

struct NativeChatToolsFeatureView:View {
 let baseURL:URL?
 let sessionID:String?
 let isChatBusy:Bool
 let todos:[[String:Any]]?
 let onOpenThread:((String,[[String:Any]]) async throws -> Void)?
 @StateObject private var model:NativeChatToolsModel
 @State private var review:NativeChatToolReview?
 @State private var pendingThread:NativeChatThreadResult?
 @State private var applyingThread = false
 @State private var applyReview = false
 init(baseURL:URL?,sessionID:String?,isChatBusy:Bool = false,todos:[[String:Any]]? = nil,onOpenThread:((String,[[String:Any]]) async throws -> Void)? = nil) { self.baseURL = baseURL; self.sessionID = sessionID; self.isChatBusy = isChatBusy; self.todos = todos; self.onOpenThread = onOpenThread; _model = StateObject(wrappedValue:NativeChatToolsModel(baseURL:baseURL,sessionID:sessionID,isChatBusy:isChatBusy)) }
 var body:some View {
  VStack(alignment:.leading,spacing:12) {
   HStack { Text("Conversation tools").font(.title.weight(.semibold)); Spacer(); Button("Reload") { Task { await model.refresh() } }.disabled(model.busy || applyingThread); if model.busy { ProgressView().controlSize(.small) } }; NativeSelectableText("Session: " + (sessionID ?? "unavailable")).font(.caption)
   Picker("Tools",selection:$model.tab) { ForEach(NativeChatToolsTab.allCases,id:\.self) { Text($0.rawValue).tag($0) } }.pickerStyle(.segmented).disabled(model.busy || applyingThread)
   ScrollView(.vertical) { LazyVStack(alignment:.leading,spacing:16) {
    if isChatBusy { Text("Chat is active, switching or uploading. Conversation changes and applying saved history are disabled; passive inspection remains available.").foregroundStyle(.secondary) }; if let error = model.error { NativeSelectableText(error).foregroundStyle(.red) }; if let notice = model.notice { NativeSelectableText(notice) }; if let pendingThread { NativeSelectableText("Runtime session ready: " + pendingThread.session); Text("Saved Chat UI history has not been applied. Applying it separately stores the inspected history and opens that thread; it can fail independently of the runtime change.").font(.caption); if onOpenThread != nil { Button("Review apply and open saved Chat thread…") { applyReview = true }.disabled(isChatBusy || applyingThread || model.busy) } }; if !model.fresh.contains(model.tab) { Text("Current state is not confirmed. Reload before changing it.").foregroundStyle(.secondary) }
    switch model.tab {
    case .plan:
     Text(chatToolsBool(model.plan["plan_mode"])==true ? "Research-only plan mode is on" : model.fresh.contains(.plan) ? "Plan mode is off" : "Plan mode unavailable").font(.headline); TextField("Reason for entering plan mode",text:$model.reason); HStack { Button("Enter plan mode…") { prepare("plan_enter") }; Button("Leave without approval…") { prepare("plan_leave") }; Button("Approve plan and leave…") { prepare("plan_approve") } }.disabled(isChatBusy || applyingThread || model.applyingHistory || model.busy || !model.fresh.contains(.plan)); Text("Plan approval records intent; it never approves tools or starts execution automatically.").font(.caption).foregroundStyle(.secondary)
     if let latest = model.plan["latest_plan"], !(latest is NSNull) { DisclosureGroup("Latest plan") { NativeSelectableText(chatToolsJSON(latest)) } }
     Text("Agent task list").font(.title2); if let todos { if todos.isEmpty { Text("Latest observed task list is empty.").foregroundStyle(.secondary) }; ForEach(Array(todos.enumerated()),id:\.offset) { _,todo in HStack { Image(systemName:todo["status"] as? String=="completed" ? "checkmark.circle" : todo["status"] as? String=="in_progress" ? "circle.inset.filled" : "circle"); NativeSelectableText(todo["content"] as? String ?? "Task"); Spacer(); Text(todo["status"] as? String ?? "unknown").font(.caption) } }; Text("Last observed todo_update frame for this session. Agent-owned scratch list; completed status is not proof of verified execution.").font(.caption).foregroundStyle(.secondary) } else { Text("No task-list update observed for this session. The backend has no REST hydration endpoint; this does not mean there are no tasks.").foregroundStyle(.secondary) }
    case .thoughts:
     Text("Session-owned operational state. Pause and abandon change tracking status; they do not interrupt underlying tasks.").foregroundStyle(.secondary)
     ForEach(model.entities) { entity in card { Text(entity.summary.isEmpty ? entity.id : entity.summary).font(.headline); Text("\(entity.kind) · \(entity.status)").font(.caption); DisclosureGroup("Stored context") { NativeSelectableText(chatToolsJSON(entity.raw)) }; HStack { if entity.status=="paused" { Button("Review resume…") { prepare("resume",id:entity.id) } }; if ["active","waiting_user","waiting_tool"].contains(entity.status) { Button("Pause tracking…") { prepare("pause",id:entity.id) } }; Button("Abandon tracking…") { prepare("abandon",id:entity.id) } }.disabled(isChatBusy || applyingThread || model.applyingHistory || model.busy || !model.fresh.contains(.thoughts)) } }
    case .snapshots:
     Text("Saved points capture runtime model history and working memory. Restore is not file undo and does not delete durable memories or alter approvals.").foregroundStyle(.secondary); TextField("Saved point label",text:$model.label); Button("Review save current session…") { prepare("snapshot") }.disabled(isChatBusy || applyingThread || model.applyingHistory || model.busy || !model.fresh.contains(.snapshots))
     ForEach(model.snapshots) { snapshot in card { Text(snapshot.label.isEmpty ? snapshot.id : snapshot.label).font(.headline); Text("\(snapshot.branch) · \(snapshot.date.formatted())").font(.caption); Button("Inspect saved point") { Task { await model.inspect(snapshot.id) } }.disabled(model.busy) } }
     if let inspected = model.inspected,let id = inspected["snapshot_id"] as? String { Text("Inspect \(id)").font(.headline); snapshotMessages(inspected); DisclosureGroup("Working memory and complete metadata") { NativeSelectableText(chatToolsJSON(inspected)).font(.system(.caption,design:.monospaced)) }; TextField("New branch name",text:$model.branchName); HStack { Button("Branch to new session…") { prepare("branch",id:id) }; Button("Restore this session…") { prepare("restore",id:id) }; Button("Restore into new session…") { prepare("restore_new",id:id) } }.disabled(isChatBusy || applyingThread || model.applyingHistory || model.busy || !model.fresh.contains(.snapshots)) }
    }
   }.padding(.vertical,12).frame(maxWidth:.infinity,alignment:.leading) }.frame(maxWidth:.infinity,maxHeight:.infinity)
  }.padding(24).frame(minWidth:680,minHeight:540).disabled(applyingThread)
  .task(id:(baseURL?.absoluteString ?? "") + "|" + (sessionID ?? "")) { review = nil; pendingThread = nil; applyReview = false; model.configure(baseURL:baseURL,sessionID:sessionID); model.setChatBusy(isChatBusy); await model.refresh() }
  .onChange(of:isChatBusy) { value in review = nil; pendingThread = nil; applyReview = false; model.setChatBusy(value) }
  .onChange(of:model.tab) { _ in Task { await model.refresh() } }
  .sheet(item:Binding(get:{review.map(ChatToolsReviewSheet.init)},set:{if $0==nil {review=nil}})) { item in VStack(alignment:.leading,spacing:16) { Text("Review conversation change").font(.title2); ScrollView { NativeSelectableText(item.review.summary) }; HStack { Button("Cancel") { review = nil }; Spacer(); Button("Confirm change") { review = nil; Task { model.setChatBusy(isChatBusy); guard !isChatBusy else { return }; if let result = await model.execute(item.review), model.canApply(result), result.origin==baseURL, result.sourceSession==sessionID { pendingThread = result } } }.disabled(isChatBusy || applyingThread || model.applyingHistory || model.busy) } }.padding(24).frame(width:640,height:520) }
  .alert("Apply saved Chat thread?",isPresented:$applyReview) {
   Button("Cancel",role:.cancel) {}
   Button("Apply and open") { guard !isChatBusy,let thread = pendingThread,thread.origin==baseURL,thread.sourceSession==sessionID,let callback = onOpenThread,model.beginApply(thread) else { return }; applyingThread = true; Task { defer { applyingThread = false; model.endApply(thread) }; guard model.canApply(thread) else { return }; do { try await callback(thread.session,thread.history); if model.canApply(thread) { pendingThread = nil } } catch { if model.canApply(thread) { model.report("Runtime change succeeded, but saved Chat thread was not confirmed. " + error.localizedDescription) } } } }
  } message: { Text(pendingThread.map { "Store \($0.history.count) inspected history rows into Chat session \($0.session) and open it. This is separate from runtime branch/restore. It does not undo files or remove durable memories." } ?? "No reviewed thread available.") }
 }
 private func prepare(_ operation:String,id:String = "") { do { model.setChatBusy(isChatBusy); review = try model.review(operation,id:id) } catch { model.report(error.localizedDescription) } }
 private func snapshotMessages(_ row:[String:Any])->some View { ForEach(Array((row["history"] as? [[String:Any]] ?? []).enumerated()),id:\.offset) { _,message in card { Text(message["role"] as? String ?? "unknown").font(.caption); if let text = message["content"] as? String ?? message["text"] as? String { NativeSelectableText(text) } else { NativeSelectableText(chatToolsJSON(message)).font(.system(.caption,design:.monospaced)) } } } }
 private func card<Content:View>(@ViewBuilder _ content:()->Content)->some View { VStack(alignment:.leading,spacing:8,content:content).padding(14).frame(maxWidth:.infinity,alignment:.leading).background(RoundedRectangle(cornerRadius:10).fill(Color.secondary.opacity(0.06))) }
}
private struct ChatToolsReviewSheet:Identifiable { let review:NativeChatToolReview; var id:String {review.generation.uuidString + review.operation + review.id}; init(_ review:NativeChatToolReview) {self.review=review} }
