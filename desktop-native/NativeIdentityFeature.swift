import Foundation
import SwiftUI
import CoreFoundation

enum NativeSelfTab: String, CaseIterable { case identity = "Identity", soul = "Soul", facts = "About Me", memory = "Memory", personas = "Personas" }
struct NativeSelfFact: Identifiable { let id: String, kind: String, text: String, source: String, tags: [String]; let confidence: Double; let raw: [String: Any]; var inferred: Bool { source != "user_stated" && confidence < 1 } }
struct NativeSelfPersona: Identifiable { let id: String, name: String, description: String, prompt: String, permissions: [String], tags: [String]; let raw: String }
struct NativeSelfReview { let generation: UUID, tab: NativeSelfTab, operation: String, id: String, title: String, summary: String, snapshot: String, body: [String: Any] }
private struct SelfFailure: LocalizedError { let message: String; var errorDescription: String? { message } }
private func selfJSON(_ object: Any) -> String { guard let data = try? JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys]), let value = String(data:data,encoding:.utf8) else { return "" }; return value }
private func selfTrue(_ value: Any?) -> Bool { guard let n = value as? NSNumber, CFGetTypeID(n) == CFBooleanGetTypeID() else { return false }; return n.boolValue }
final class NativeSelfRedirectGuard: NSObject, URLSessionTaskDelegate {
 func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
  guard let a = task.originalRequest?.url, let b = request.url, a.scheme == b.scheme, a.host == b.host, a.port == b.port, b.user == nil, b.password == nil else { completionHandler(nil); return }; completionHandler(request)
 }
}
@MainActor final class NativeIdentityModel: ObservableObject {
 @Published var tab: NativeSelfTab = .identity
 @Published var name = ""
@Published var personality = ""
@Published var greeting = ""
@Published var rules = ""
@Published var voice = ""
@Published var rawIdentity = ""
@Published var soul = ""
 @Published var advanced = false
 @Published var factKind = "preference"
 @Published var factText = ""
 @Published var factTags = ""
 @Published var factFilter = "all"
 @Published private(set) var facts: [NativeSelfFact] = []
 @Published private(set) var personas: [NativeSelfPersona] = []
 @Published private(set) var kinds: [String] = []
 @Published private(set) var memory = ""
 @Published private(set) var error: String?
 @Published private(set) var notice: String?
 @Published private(set) var busy = false
 @Published private(set) var fresh = Set<NativeSelfTab>()
 private var baseURL: URL?, generation = UUID(), identity: [String: Any] = [:], draftIdentity: [String: Any] = [:], originalSoul = ""
 private let session: URLSession
 init(baseURL: URL?, session: URLSession? = nil) { self.baseURL = baseURL; self.session = session ?? URLSession(configuration:.ephemeral,delegate:NativeSelfRedirectGuard(),delegateQueue:nil) }
 func configure(baseURL: URL?) { guard self.baseURL != baseURL else { return }; self.baseURL = baseURL; generation = UUID(); identity = [:]; draftIdentity = [:]; name = ""; personality = ""; greeting = ""; rules = ""; voice = ""; rawIdentity = ""; soul = ""; originalSoul = ""; memory = ""; facts = []; personas = []; kinds = []; factText = ""; factTags = ""; error = nil; notice = nil; fresh = []; busy = false }
 private func request(_ path: String, method: String = "GET", body: [String: Any]? = nil) async throws -> [String: Any] {
  let started = generation; try Task.checkCancellation()
  guard let baseURL, ["127.0.0.1","::1","[::1]"].contains(baseURL.host ?? ""), ["http","https"].contains(baseURL.scheme ?? ""), baseURL.user == nil, baseURL.password == nil, var c = URLComponents(url:baseURL,resolvingAgainstBaseURL:false) else { throw SelfFailure(message:"Connect to the app’s local agent to manage identity.") }
  c.path = path; c.query = nil; c.fragment = nil; guard let url = c.url else { throw SelfFailure(message:"Invalid identity request.") }
  var req = URLRequest(url:url); req.httpMethod = method; req.timeoutInterval = 60
  if let body { req.httpBody = try JSONSerialization.data(withJSONObject:body); req.setValue("application/json",forHTTPHeaderField:"Content-Type") }
  let (data,response) = try await session.feralLocalData(for:req); try Task.checkCancellation(); guard generation == started else { throw SelfFailure(message:"Agent connection changed. Refresh and review again.") }
  guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode), let object = (try? JSONSerialization.jsonObject(with:data)) as? [String:Any] else { throw SelfFailure(message:"The agent did not confirm this request. Refresh before retrying.") }
  if let message = object["error"] as? String, !message.isEmpty { throw SelfFailure(message:"The agent reported an identity operation failure.") }; return object
 }
 private func decodeFact(_ row: [String: Any]) throws -> NativeSelfFact { guard let id = row["id"] as? String, !id.isEmpty, let kind = row["kind"] as? String, let text = row["text"] as? String, let source = row["source"] as? String, let confidence = row["confidence"] as? NSNumber, confidence.doubleValue.isFinite, (0...1).contains(confidence.doubleValue) else { throw SelfFailure(message:"Incomplete fact response.") }; return NativeSelfFact(id:id,kind:kind,text:text,source:source,tags:row["tags"] as? [String] ?? [],confidence:confidence.doubleValue,raw:row) }
 private func bindIdentity(_ row:[String:Any], baseline: Bool = true) { if baseline { identity = row }; draftIdentity = row; name = row["name"] as? String ?? ""; personality = row["personality"] as? String ?? ""; greeting = row["greeting_style"] as? String ?? ""; rules = (row["rules"] as? [String] ?? []).joined(separator:"\n"); voice = (row["voice"] as? [String:Any])?["tts_voice"] as? String ?? ""; rawIdentity = selfJSON(row) }
 func refresh() async {
  guard !busy else { return }; let started = generation, selected = tab; busy = true; defer { if generation == started { busy = false } }
  do {
   switch selected {
   case .identity: bindIdentity(try await request("/api/identity"))
   case .soul: let row = try await request("/api/identity/soul"); guard let text = row["soul"] as? String else { throw SelfFailure(message:"Soul response is incomplete.") }; soul = text; originalSoul = text
   case .memory: let row = try await request("/api/identity/memory_md"); guard let text = row["memory"] as? String else { throw SelfFailure(message:"Memory response is incomplete.") }; memory = text
   case .facts: let row = try await request("/api/about-me"); guard let list = row["facts"] as? [[String:Any]], let supported = row["kinds_supported"] as? [String] else { throw SelfFailure(message:"Fact listing is incomplete.") }; facts = try list.map(decodeFact); guard Set(facts.map(\.id)).count == facts.count else { throw SelfFailure(message:"Fact identifiers are duplicated.") }; kinds = supported
   case .personas: let row = try await request("/api/agents/personas"); guard let list = row["personas"] as? [[String:Any]] else { throw SelfFailure(message:"Persona catalog is incomplete.") }; personas = try list.map { item in guard let id = item["agent_id"] as? String else { throw SelfFailure(message:"Persona identifier missing.") }; return NativeSelfPersona(id:id,name:item["name"] as? String ?? id,description:item["description"] as? String ?? "",prompt:item["system_prompt"] as? String ?? "",permissions:item["tool_permissions"] as? [String] ?? [],tags:item["tags"] as? [String] ?? [],raw:selfJSON(item)) }
   }; fresh.insert(selected); error = nil
  } catch { if started == generation { fresh.remove(selected); self.error = error.localizedDescription } }
 }
 func reviewIdentity() throws -> NativeSelfReview {
  guard fresh.contains(.identity) else { throw SelfFailure(message:"Refresh identity before editing.") }
  var body = draftIdentity
  if advanced { guard let data = rawIdentity.data(using:.utf8), let parsed = (try? JSONSerialization.jsonObject(with:data)) as? [String:Any] else { throw SelfFailure(message:"Advanced identity must be a JSON object.") }; body = parsed }
  else { body["name"] = name; body["personality"] = personality; body["greeting_style"] = greeting; body["rules"] = rules.components(separatedBy:"\n").map { $0.trimmingCharacters(in:.whitespaces) }.filter { !$0.isEmpty }; var v = body["voice"] as? [String:Any] ?? [:]; v["tts_voice"] = voice; body["voice"] = v }
  return NativeSelfReview(generation:generation,tab:.identity,operation:"identity",id:"",title:"Save agent identity?",summary:"Replaces IDENTITY.yaml with the reviewed document. Runtime adoption is not confirmed by this endpoint.\n\n" + selfJSON(body),snapshot:selfJSON(identity),body:body)
 }
 func switchAdvanced(_ enabled: Bool) {
  do {
   if enabled { rawIdentity = selfJSON(try reviewIdentity().body) }
   else { guard let data = rawIdentity.data(using:.utf8), let row = (try? JSONSerialization.jsonObject(with:data)) as? [String:Any] else { throw SelfFailure(message:"Fix advanced JSON before switching to the form.") }; bindIdentity(row,baseline:false) }
   advanced = enabled; error = nil
  } catch { self.error = error.localizedDescription }
 }
 func reviewSoul() throws -> NativeSelfReview { guard fresh.contains(.soul), !soul.isEmpty else { throw SelfFailure(message:"This backend cannot clear Soul with empty content. Enter content or cancel.") }; return NativeSelfReview(generation:generation,tab:.soul,operation:"soul",id:"",title:"Replace Soul?",summary:"Replaces SOUL.md. Runtime adoption is not confirmed by this endpoint.\n\n" + soul,snapshot:originalSoul,body:["content":soul]) }
 func reviewFact(_ operation: String, fact: NativeSelfFact? = nil) throws -> NativeSelfReview {
  guard fresh.contains(.facts), ["add","confirm","reject","delete"].contains(operation) else { throw SelfFailure(message:"Refresh facts and choose a supported action.") }
  if operation == "add" { let text = factText.trimmingCharacters(in:.whitespacesAndNewlines); guard !text.isEmpty,kinds.contains(factKind) else { throw SelfFailure(message:"Choose a supported kind and enter a fact.") }; let body:[String:Any] = ["kind":factKind,"text":text,"tags":factTags.split(separator:",").map { $0.trimmingCharacters(in:.whitespaces) }.filter { !$0.isEmpty },"source":"user_stated","confidence":1.0]; return NativeSelfReview(generation:generation,tab:.facts,operation:operation,id:"",title:"Add personal fact?",summary:"Store as user-stated with confidence 1.0.\n\n" + selfJSON(body),snapshot:"",body:body) }
  guard let fact, facts.contains(where: { $0.id == fact.id }), !fact.id.isEmpty, fact.id.unicodeScalars.allSatisfy({ CharacterSet(charactersIn:"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.").contains($0) }) else { throw SelfFailure(message:"Fact is no longer available or its identifier is unsupported.") }
  return NativeSelfReview(generation:generation,tab:.facts,operation:operation,id:fact.id,title:operation == "reject" ? "Convert fact into a taboo?" : operation == "delete" ? "Permanently delete fact?" : "Confirm fact?",summary:fact.text + "\n\n" + (operation == "reject" ? "Keeps this ID and changes its text to ‘Never assume: …’, kind taboo, source user_stated, confidence 1.0. This is not deletion." : operation == "delete" ? "Deletes this stored fact. This does not remove other memories or previously replicated copies." : "Sets confidence to 1.0 and source to user_stated."),snapshot:selfJSON(fact.raw),body:[:])
 }
 func report(_ message: String) { error = message }
 func execute(_ reviewed: NativeSelfReview) async {
  guard !busy, reviewed.generation == generation, fresh.contains(reviewed.tab) else { error = "Review is stale. Refresh and review again."; return }; let started = generation; busy = true; notice = nil; defer { if started == generation { busy = false } }
  do {
   switch reviewed.operation {
   case "identity": let current = try await request("/api/identity"); guard selfJSON(current) == reviewed.snapshot else { throw SelfFailure(message:"Identity changed on the agent. Refresh and review again.") }; let result = try await request("/api/identity",method:"POST",body:reviewed.body); guard selfTrue(result["ok"]) else { throw SelfFailure(message:"Identity save was not confirmed.") }; let readback = try await request("/api/identity"); guard selfJSON(readback) == selfJSON(reviewed.body) else { throw SelfFailure(message:"Saved identity readback differs. Refresh to inspect the actual document.") }; bindIdentity(readback); notice = "Identity document saved and read back. Runtime adoption is not confirmed."
   case "soul": let current = try await request("/api/identity/soul"); guard current["soul"] as? String == reviewed.snapshot else { throw SelfFailure(message:"Soul changed on the agent. Refresh and review again.") }; let result = try await request("/api/identity/soul",method:"POST",body:reviewed.body); guard selfTrue(result["ok"]) else { throw SelfFailure(message:"Soul save was not confirmed.") }; let readback = try await request("/api/identity/soul"); guard let text = readback["soul"] as? String,text == reviewed.body["content"] as? String else { throw SelfFailure(message:"Soul readback differs; save is not confirmed.") }; soul = text; originalSoul = text; notice = "Soul document saved and read back. Runtime adoption is not confirmed."
   case "add": let result = try await request("/api/about-me",method:"POST",body:reviewed.body); guard selfTrue(result["success"]), let row = result["fact"] as? [String:Any] else { throw SelfFailure(message:"Fact creation was not confirmed.") }; let fact = try decodeFact(row); guard fact.text == reviewed.body["text"] as? String,fact.kind == reviewed.body["kind"] as? String else { throw SelfFailure(message:"Returned fact differs from the reviewed fact.") }; facts.removeAll { $0.id == fact.id }; facts.append(fact); factText = ""; factTags = ""; notice = "Personal fact stored."
   case "confirm","reject","delete": let current = try await request("/api/about-me"); guard let rows = current["facts"] as? [[String:Any]], let row = rows.first(where: { $0["id"] as? String == reviewed.id }), selfJSON(row) == reviewed.snapshot else { throw SelfFailure(message:"Fact changed or was removed. Refresh and review again.") }; let result = try await request("/api/about-me/" + reviewed.id + (reviewed.operation == "delete" ? "" : "/" + reviewed.operation),method:reviewed.operation == "delete" ? "DELETE" : "POST",body:reviewed.operation == "delete" ? nil : [:]); guard selfTrue(result["success"]) else { throw SelfFailure(message:"Fact mutation was not confirmed.") }; if reviewed.operation == "delete" { facts.removeAll { $0.id == reviewed.id }; notice = "Fact deleted from this store. Other memories and replicated copies are unaffected." } else { guard let returned = result["fact"] as? [String:Any] else { throw SelfFailure(message:"Fact mutation has no returned fact.") }; let updated = try decodeFact(returned); guard updated.id == reviewed.id, updated.source == "user_stated", updated.confidence == 1, reviewed.operation != "reject" || updated.kind == "taboo" else { throw SelfFailure(message:"Fact mutation outcome did not match its reviewed action.") }; facts.removeAll { $0.id == updated.id }; facts.append(updated); notice = reviewed.operation == "reject" ? "Fact converted into a taboo; it was not deleted." : "Fact confirmed." }
   default: throw SelfFailure(message:"Unsupported self operation.")
   }; error = nil
  } catch { if generation == started { fresh.remove(reviewed.tab); self.error = "Change not confirmed. " + error.localizedDescription } }
 }
 var filteredFacts: [NativeSelfFact] { facts.filter { factFilter == "all" || (factFilter == "inferred" ? $0.inferred : $0.kind == factFilter) } }
}

struct NativeIdentityFeatureView: View {
 let baseURL: URL?
 @StateObject private var model: NativeIdentityModel
 @State private var review: NativeSelfReview?
 init(baseURL: URL?) { self.baseURL = baseURL; _model = StateObject(wrappedValue:NativeIdentityModel(baseURL:baseURL)) }
 var body: some View {
  VStack(alignment:.leading,spacing:12) {
   HStack { Text("Self & Identity").font(.largeTitle.weight(.semibold)); Spacer(); Button("Reload selected view") { Task { await model.refresh() } }.disabled(model.busy); if model.busy { ProgressView().controlSize(.small) } }.padding(.horizontal,24).padding(.top,24)
   Picker("Section",selection:$model.tab) { ForEach(NativeSelfTab.allCases,id:\.self) { Text($0.rawValue).tag($0) } }.pickerStyle(.segmented).padding(.horizontal,24).disabled(model.busy)
   ScrollView(.vertical) { LazyVStack(alignment:.leading,spacing:18) {
    if let error = model.error { NativeSelectableText(error).foregroundStyle(.red) }; if let notice = model.notice { NativeSelectableText(notice) }
    if !model.fresh.contains(model.tab) { Text("State is not confirmed. Reload before saving.").foregroundStyle(.secondary) }
    switch model.tab {
    case .identity:
     Text("Agent personality, greeting, rules, and voice. Save reviews the whole document and preserves additional fields in form mode.").foregroundStyle(.secondary)
     Toggle("Advanced whole-document JSON editor",isOn:Binding(get:{ model.advanced },set:{ model.switchAdvanced($0) }))
     if model.advanced { editor("Identity JSON",text:$model.rawIdentity,height:320) } else { TextField("Agent name",text:$model.name); editor("Personality",text:$model.personality,height:120); editor("Greeting style",text:$model.greeting,height:80); editor("Rules — one per line",text:$model.rules,height:120); TextField("TTS voice identifier",text:$model.voice) }
     Button("Review identity save…") { prepare { try model.reviewIdentity() } }.disabled(model.busy || !model.fresh.contains(.identity))
    case .soul: Text("SOUL.md — editable personality prose. Empty clearing is unsupported by the current backend.").foregroundStyle(.secondary); editor("Soul",text:$model.soul,height:340); Button("Review Soul replacement…") { prepare { try model.reviewSoul() } }.disabled(model.busy || !model.fresh.contains(.soul))
    case .memory: Text("MEMORY.md is the agent’s curated summary. Read-only here; an empty response does not prove the workspace is initialized.").foregroundStyle(.secondary); NativeSelectableText(model.memory.isEmpty ? "No memory document content returned." : model.memory).frame(maxWidth:.infinity,alignment:.leading)
    case .facts:
     Text("Personal facts used by the agent. Reject creates a taboo; Delete removes the fact from this store.").foregroundStyle(.secondary)
     Picker("Show",selection:$model.factFilter) { Text("All").tag("all"); Text("Inferred, unconfirmed").tag("inferred"); ForEach(model.kinds,id:\.self) { Text($0).tag($0) } }
     Picker("New fact kind",selection:$model.factKind) { ForEach(model.kinds,id:\.self) { Text($0).tag($0) } }; TextField("Personal fact",text:$model.factText); TextField("Tags, comma separated",text:$model.factTags); Button("Review new fact…") { prepare { try model.reviewFact("add") } }.disabled(model.busy || !model.fresh.contains(.facts))
     ForEach(model.filteredFacts) { fact in card { NativeSelectableText(fact.text).font(.headline); Text("\(fact.kind) · \(fact.source) · confidence \(Int(fact.confidence * 100))%").font(.caption); if !fact.tags.isEmpty { Text(fact.tags.joined(separator:", ")).font(.caption) }; HStack { if fact.inferred { Button("Confirm…") { prepare { try model.reviewFact("confirm",fact:fact) } }; Button("Reject → taboo…") { prepare { try model.reviewFact("reject",fact:fact) } } }; Button("Delete…") { prepare { try model.reviewFact("delete",fact:fact) } } }.disabled(model.busy || !model.fresh.contains(.facts)); DisclosureGroup("Stored metadata") { NativeSelectableText(selfJSON(fact.raw)).font(.system(.caption,design:.monospaced)) } } }
    case .personas:
     Text("First-party persona catalog, not active specialists. Inspecting a persona does not grant its tool permissions or launch an agent. Catalog editing/applying is not supported by these routes.").foregroundStyle(.secondary)
     ForEach(model.personas) { persona in card { Text(persona.name).font(.headline); Text(persona.description); Text("Requested tools: " + persona.permissions.joined(separator:", ")).font(.caption); Text("Tags: " + persona.tags.joined(separator:", ")).font(.caption); DisclosureGroup("System prompt") { NativeSelectableText(persona.prompt) }; DisclosureGroup("Manifest details") { NativeSelectableText(persona.raw).font(.system(.caption,design:.monospaced)) } } }
    }
   }.padding(24).frame(maxWidth:.infinity,alignment:.leading) }.frame(maxWidth:.infinity,maxHeight:.infinity)
  }.frame(maxWidth:.infinity,maxHeight:.infinity,alignment:.topLeading)
  .task(id:baseURL) { review = nil; model.configure(baseURL:baseURL); await model.refresh() }
  .onChange(of:model.tab) { _ in Task { await model.refresh() } }
  .sheet(item:Binding(get:{ review.map(SelfReviewSheet.init) },set:{ if $0 == nil { review = nil } })) { item in VStack(alignment:.leading,spacing:16) { Text(item.review.title).font(.title2); ScrollView { NativeSelectableText(item.review.summary).frame(maxWidth:.infinity,alignment:.leading) }; HStack { Button("Cancel") { review = nil }; Spacer(); Button("Confirm change") { review = nil; Task { await model.execute(item.review) } }.disabled(model.busy) } }.padding(24).frame(width:620,height:520) }
 }
 private func prepare(_ value: () throws -> NativeSelfReview) { do { review = try value() } catch { model.report(error.localizedDescription) } }
 private func editor(_ label: String,text:Binding<String>,height:CGFloat) -> some View { VStack(alignment:.leading) { Text(label).font(.headline); TextEditor(text:text).frame(height:height).border(Color.secondary.opacity(0.25)) } }
 private func card<Content:View>(@ViewBuilder _ content:()->Content)->some View { VStack(alignment:.leading,spacing:10,content:content).padding(16).frame(maxWidth:.infinity,alignment:.leading).background(RoundedRectangle(cornerRadius:12).fill(Color.secondary.opacity(0.06))) }
}
private struct SelfReviewSheet: Identifiable { let review:NativeSelfReview; var id:String { review.generation.uuidString + review.operation + review.id }; init(_ review:NativeSelfReview) { self.review = review } }
