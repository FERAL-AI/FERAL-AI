// Pure wire fixtures; no backend, permission grants, model calls or UI execution.
import Foundation
private struct RichAssertion:Error{let message:String}
private func check(_ condition:@autoclosure()->Bool,_ message:String)throws{if !condition(){throw RichAssertion(message:message)}}
@main struct NativeRichChatFeatureTests {
 static func frame(_ type:String,_ payload:[String:Any],session:String="thread-1")->[String:Any]{["type":type,"session_id":session,"payload":payload]}
 static func permission(_ id:String="request-1",operation:String="write",expiry:Double=Date().timeIntervalSince1970+300)->[String:Any]{frame("permission_request",["request_id":id,"path":"/tmp/reviewed","operation":operation,"reason":"Edit fixture","expires_at":expiry,"scope":"persistent_workspace"])}
 @MainActor static func main() async {
  do {
   let model=NativeRichChatModel(),connection=UUID();model.configure(sessionID:"thread-1",connectionID:connection)
   try check(!model.consume(frame("tool_start",["tool":"coding__run","call_id":"a"],session:"other"),connectionID:connection),"foreign session accepted");try check(!model.consume(permission(),connectionID:UUID()),"old socket accepted");try check(model.tools.isEmpty && model.permissions.isEmpty,"foreign frames mutated state");print("PASS session and socket identity reject stale wire")
   _=model.consume(frame("stream_delta",["kind":"reasoning","delta":"Provider reasoning"]),connectionID:connection);try check(model.reasoning=="Provider reasoning","reasoning not separated");try check(!model.consume(frame("stream_delta",["delta":"Answer"]),connectionID:connection),"ordinary answer delta stolen");print("PASS provider reasoning separated from answer text")
   _=model.consume(frame("tool_start",["tool":"coding__run","call_id":"call-1","args_preview":"{command:test}"]),connectionID:connection);_=model.consume(frame("tool_result",["tool":"coding__run","call_id":"call-1","success":false,"error_code":"policy_denied","error":"Declined","result_preview":"Excerpt","preview_truncated":true]),connectionID:connection);try check(model.tools.count==1 && model.tools[0].status=="policy_denied" && model.tools[0].arguments.contains("command") && model.tools[0].raw.count==2,"start/result merge lost args or refusal");_=model.consume(frame("tool_result",["tool":"coding__run","call_id":"call-2"]),connectionID:connection);try check(model.tools[1].status=="outcome_unknown","missing success treated successful");_=model.consume(frame("tool_start",["tool":"legacy"]),connectionID:connection);_=model.consume(frame("tool_result",["tool":"legacy","success":true]),connectionID:connection);try check(model.tools.count==3 && model.tools.last?.raw.count==2 && model.tools.last?.correlation=="single_pending_no_call_id" && model.tools.last?.status=="reported_success","single missing-ID start remained running");print("PASS exact tool identity and explicit single-pending missing-ID inference")
   _=model.consume(permission(),connectionID:connection);let review=try model.reviewPermission("request-1",allow:true);try check(review.permission.scope.contains("Persistent") && review.permission.scope.contains("not an allow-once"),"grant scope hidden");var sent:NativeRichPermissionResponse?;await model.respond(review){value in sent=value};try check(sent?.requestID=="request-1" && sent?.sessionID=="thread-1" && sent?.connectionID==connection && ((sent?.wireFrame["payload"] as? [String:Any])?["action_id"] as? String)=="perm_grant_request-1","response authority/wire incorrect");try check(model.permissions[0].state=="response_sent_unconfirmed","socket send falsely became grant");var duplicates=0;await model.respond(review){_ in duplicates+=1};try check(duplicates==0,"answered request resent");print("PASS explicit persistent-scope review and exact authority; send is not grant confirmation")
   _=model.consume(frame("permission_decision",["request_id":"request-1","status":"granted","path":"/tmp/other","mode":"readwrite","scope":"persistent_workspace"]),connectionID:connection);try check(model.permissions[0].state=="receipt_mismatch","mismatched scope path accepted");_=model.consume(permission("valid-ack",operation:"readwrite"),connectionID:connection);let valid=try model.reviewPermission("valid-ack",allow:true);await model.respond(valid){_ in _=model.consume(frame("permission_decision",["request_id":"valid-ack","status":"granted","path":valid.permission.canonicalPath!,"mode":"readwrite","scope":"persistent_workspace"]),connectionID:connection)};try check(model.permissions.last?.state=="granted_confirmed","actual ack overwritten by sent-state after await");print("PASS matched structured grant receipt and early acknowledgement race")
   _=model.consume(permission("expired",expiry:Date().timeIntervalSince1970-1),connectionID:connection);var blocked=false;do{_=try model.reviewPermission("expired",allow:true)}catch{blocked=true};try check(blocked,"expired request actionable");_=model.consume(permission("unsupported",operation:"execute"),connectionID:connection);blocked=false;do{_=try model.reviewPermission("unsupported",allow:true)}catch{blocked=true};try check(blocked,"unsupported operation actionable");blocked=false;do{_=try model.reviewPermission("unsupported",allow:false)}catch{blocked=true};try check(blocked,"unsupported deny actionable");print("PASS expired and unsupported requests have no approval authority");await remaining();await canonicalAndAnonymous();try presentation()
  } catch {print("FAIL \(error)");exit(1)}
 }
 @MainActor static func presentation() throws {
  let model=NativeRichChatModel(),socket=UUID();model.configure(sessionID:"thread-1",connectionID:socket)
  for event in ["llm_call","memory_write","tool_exec"] {
   _=model.consume(frame("brain_event",["event":event,"model":"fixture-model","success":false]),connectionID:socket)
  }
  try check(model.diagnosticEvents.count==3 && model.visibleEvents.isEmpty,"known telemetry clutters default conversation")
  try check(model.eventRecords.count==3 && ((model.diagnosticEvents[0].raw["payload"] as? [String:Any])?["model"] as? String)=="fixture-model","diagnostic disclosure lost exact wire metadata")
  for type in ["refusal","error","skill_proposal","sdui","sdui_patch","timeline"] {
   _=model.consume(frame(type,["message":"Fixture review or result"]),connectionID:socket)
  }
  _=model.consume(frame("brain_event",["event":"new_action_requires_review","message":"Review needed"]),connectionID:socket)
  _=model.consume(frame("brain_event",["message":"Unknown event"]),connectionID:socket)
  try check(model.visibleEvents.count==8 && model.visibleEvents.contains{$0.kind=="refusal"} && model.visibleEvents.contains{$0.text=="Review needed"},"unknown, actionable or failure events hidden as telemetry")
  _=model.consume(frame("tool_start",["tool":"fixture","call_id":"visible-tool"]),connectionID:socket)
  _=model.consume(frame("tool_result",["tool":"fixture","call_id":"visible-tool","success":false,"error":"Actual failure"]),connectionID:socket)
  _=model.consume(permission("visible-permission"),connectionID:socket)
  _=model.consume(frame("budget_exceeded",["call_site":"chat","cap_dollars":1]),connectionID:socket)
  try check(model.tools.count==1 && model.tools[0].error=="Actual failure" && model.permissions.count==1 && model.budgets["chat"] != nil,"diagnostics filtering removed actual tool result, approval or cost visibility")
  model.configure(sessionID:"thread-2",connectionID:UUID())
  try check(model.diagnosticEvents.isEmpty && model.visibleEvents.isEmpty,"old session diagnostics retained")
  print("PASS consumer diagnostics preserve exact telemetry; failures, unknown events, tool results, permission requests and budgets stay visible")
 }
 @MainActor static func remaining() async {
  do {
   let m=NativeRichChatModel(),c=UUID();m.configure(sessionID:"thread-1",connectionID:c);_=m.consume(permission(),connectionID:c);let old=try m.reviewPermission("request-1",allow:false);m.configure(sessionID:"thread-2",connectionID:UUID());var count=0;await m.respond(old){_ in count+=1};try check(count==0 && m.permissions.isEmpty,"stale permission callback executed");print("PASS expiry, unsupported operations and disconnected review cannot authorize")
   let live=NativeRichChatModel();live.configure(sessionID:"thread-1",connectionID:c);_=live.consume(["type":"state_push","event":"cost_cap_hit","data":["call_site":"chat","cap_dollars":5,"current_dollars":6]],connectionID:c);try check(live.budgets["chat"]?["current_dollars"] as? Int==6,"broadcast cap dropped");_=live.consume(frame("plan_mode",["session_id":"thread-1","plan_mode":true,"latest_plan":["opaque":true]]),connectionID:c);try check(live.plan?["plan_mode"] as? Bool==true,"plan state lost");_=live.consume(frame("sdui",["root":["type":"button","action_id":"execute-secret-command"]]),connectionID:c);try check(live.events.last?.kind=="sdui" && live.permissions.isEmpty,"opaque UI became an action");print("PASS budget, plan and opaque structured content remain data-only")
   let failing=NativeRichChatModel();failing.configure(sessionID:"thread-1",connectionID:c);_=failing.consume(permission("delivery"),connectionID:c);let denial=try failing.reviewPermission("delivery",allow:false);await failing.respond(denial){_ in throw RichAssertion(message:"fixture transport failure")};try check(failing.permissions[0].state=="delivery_uncertain" && !failing.canRespond(denial),"failed delivery falsely settled or immediately retriable");print("PASS uncertain delivery never reports granted or reuses request authority")
   print("9 original Rich chat fixture groups passed; no production actions or native GUI verification.")
  }catch{print("FAIL \(error)");exit(1)}
 }
 @MainActor static func canonicalAndAnonymous() async {
  do {
   let connection=UUID(),model=NativeRichChatModel(resolvePath:{ path in path.hasPrefix("/tmp/") ? "/private"+path : nil })
   model.configure(sessionID:"thread-1",connectionID:connection)
   _=model.consume(permission("canonical"),connectionID:connection)
   let review=try model.reviewPermission("canonical",allow:true)
   try check(review.permission.path=="/tmp/reviewed" && review.permission.canonicalPath=="/private/tmp/reviewed" && review.permission.scope.contains("/private/tmp/reviewed"),"canonical folder not disclosed before grant")
   await model.respond(review){_ in _=model.consume(frame("permission_decision",["request_id":"canonical","status":"granted","path":"/private/tmp/reviewed","mode":"readwrite","scope":"persistent_workspace"]),connectionID:connection)}
   try check(model.permissions[0].state=="granted_confirmed","canonical backend receipt was rejected")
   print("PASS original and canonical folder reviewed; canonical backend receipt confirms scope")
   var resolved="/private/tmp/first"
   let race=NativeRichChatModel(resolvePath:{_ in resolved});race.configure(sessionID:"thread-1",connectionID:connection)
   _=race.consume(permission("race"),connectionID:connection);let held=try race.reviewPermission("race",allow:true)
   resolved="/private/tmp/second";var sent=0;await race.respond(held){_ in sent+=1}
   try check(sent==0 && !race.canRespond(held),"changed symlink resolution could grant stale scope")
   let denial=try race.reviewPermission("race",allow:false);await race.respond(denial){_ in sent+=1}
   try check(sent==1,"safe denial incorrectly required unchanged symlink scope")
   print("PASS changed path resolution invalidates grants while denial remains available")
   let mismatched=NativeRichChatModel(resolvePath:{_ in "/private/tmp/expected"});mismatched.configure(sessionID:"thread-1",connectionID:connection)
   _=mismatched.consume(permission("mismatch"),connectionID:connection)
   let mismatchReview=try mismatched.reviewPermission("mismatch",allow:true)
   await mismatched.respond(mismatchReview){_ in _=mismatched.consume(frame("permission_decision",["request_id":"mismatch","status":"granted","path":"/private/tmp/unexpected","mode":"readwrite","scope":"persistent_workspace"]),connectionID:connection)}
   try check(mismatched.permissions[0].state=="receipt_mismatch" && mismatched.error?.contains("already have been granted")==true,"unexpected real grant lost uncertainty warning")
   print("PASS unexpected canonical receipt remains uncertain and directs Security inspection")
   let anonymous=NativeRichChatModel();anonymous.configure(sessionID:"thread-1",connectionID:connection)
   _=anonymous.consume(frame("tool_start",["tool":"same","args_preview":"first"]),connectionID:connection)
   _=anonymous.consume(frame("tool_start",["tool":"same","args_preview":"second"]),connectionID:connection)
   _=anonymous.consume(frame("tool_result",["tool":"same","success":true]),connectionID:connection)
   try check(anonymous.tools.count==3 && anonymous.tools.filter{$0.status=="outcome_unresolved"}.count==2 && !anonymous.tools.contains{$0.status=="running"},"ambiguous anonymous calls falsely paired or left indefinitely running")
   try check(anonymous.tools.allSatisfy{$0.correlation=="ambiguous_no_call_id"},"ambiguous identity not explicit")
   print("PASS ambiguous missing-ID results preserve uncertainty instead of guessing a call")
   let exact=NativeRichChatModel();exact.configure(sessionID:"thread-1",connectionID:connection)
   _=exact.consume(frame("tool_start",["tool":"shared","call_id":"identified"]),connectionID:connection)
   _=exact.consume(frame("tool_result",["tool":"shared","success":false]),connectionID:connection)
   try check(exact.tools.count==2 && exact.tools[0].status=="running" && exact.tools[0].correlation=="exact_call_id","missing-ID terminal consumed exact-ID pending call")
   exact.clearTurnPresentation();_=exact.consume(frame("tool_result",["tool":"shared","success":true]),connectionID:connection)
   try check(exact.tools.count==1 && exact.tools[0].correlation=="unmatched_no_call_id","turn reset reused an old anonymous pending call")
   print("PASS exact IDs and turn resets prevent anonymous cross-call correlation")
   let fm=FileManager.default,base=fm.temporaryDirectory.appendingPathComponent("feral-rich-canonical-"+UUID().uuidString)
   try fm.createDirectory(at:base.appendingPathComponent("target/nested"),withIntermediateDirectories:true)
   defer{try? fm.removeItem(at:base)}
   try fm.createSymbolicLink(atPath:base.appendingPathComponent("alias").path,withDestinationPath:base.appendingPathComponent("target/nested").path)
   let expected=NativeRichPath.canonical(base.appendingPathComponent("target/missing-child").path)
   try check(NativeRichPath.canonical(base.path+"/alias/../missing-child")==expected && expected != nil,"symlink plus parent resolution differs from backend Path.resolve")
   try fm.createSymbolicLink(atPath:base.appendingPathComponent("loop").path,withDestinationPath:"loop")
   try check(NativeRichPath.canonical(base.path+"/loop")==nil,"symlink loop was accepted")
   try check(NativeRichPath.canonical("relative/folder")==nil && NativeRichPath.canonical("/tmp/a\0b")==nil,"relative or NUL path granted guessed scope")
   print("PASS real disposable filesystem symlink/nonexistent-child/loop semantics; no folder policy grants")
   print("15 Rich chat fixture groups passed; no production grants or native GUI verification.")
  }catch{print("FAIL \(error)");exit(1)}
 }
}
