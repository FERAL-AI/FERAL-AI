// Standalone mocked-wire regression harness. Not included in the application.
// This verifies Swift model behavior, not model inference or real ACP execution.
import Foundation

private final class WireProtocol: URLProtocol {
    static let lock = NSLock()
    static var captured: [(String, [String: Any])] = []
    static var replies: [String: [String: Any]] = [:]
    static var sequences: [String: [[String: Any]]] = [:]
    static var delayedPath: String?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        var body: [String: Any] = [:]
        if let data = request.httpBody { body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:] }
        else if let stream = request.httpBodyStream {
            stream.open(); defer { stream.close() }
            var bytes = Data(); var buffer = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer, maxLength: buffer.count)
                if count <= 0 { break }; bytes.append(buffer, count: count)
            }
            body = (try? JSONSerialization.jsonObject(with: bytes)) as? [String: Any] ?? [:]
        }
        let path = request.url!.path
        Self.lock.lock()
        Self.captured.append((path, body))
        let response: [String: Any]
        if var queue = Self.sequences[path], !queue.isEmpty { response = queue.removeFirst(); Self.sequences[path] = queue }
        else { response = Self.replies[path] ?? (path == "/api/conversations/save" ? ["id": body["id"] ?? "", "message_count": (body["messages"] as? [Any])?.count ?? 0] : ["ok": true]) }
        let delayed = Self.delayedPath == path; if delayed { Self.delayedPath = nil }
        Self.lock.unlock()
        let deliver = { [self] in
        let data = try! JSONSerialization.data(withJSONObject: response)
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: ["Content-Type": "application/json"])!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
        }
        if delayed { DispatchQueue.global().asyncAfter(deadline: .now() + 0.15, execute: deliver) } else { deliver() }
    }
    override func stopLoading() {}
    static func reset(_ responses: [String: [String: Any]] = [:]) {
        lock.lock(); defer { lock.unlock() }; captured = []; replies = responses; sequences = [:]; delayedPath = nil
    }
    static func bodies(_ path: String) -> [[String: Any]] {
        lock.lock(); defer { lock.unlock() }; return captured.filter { $0.0 == path }.map { $0.1 }
    }
}

private struct AssertionFailure: Error, CustomStringConvertible { let description: String }
private func expect(_ condition: @autoclosure () -> Bool, _ message: String) throws {
    if !condition() { throw AssertionFailure(description: message) }
}

@main struct NativeModelTests {
    @MainActor static func model(preferences injectedPreferences: UserDefaults? = nil, runtimeOwner: (() -> NativeRuntimeOwnership?)? = nil) -> NativeModel {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [WireProtocol.self]
        let name = "theora.native-wire-tests." + UUID().uuidString
        let preferences = injectedPreferences ?? UserDefaults(suiteName: name)!
        if injectedPreferences == nil { preferences.removePersistentDomain(forName: name) }
        let model = NativeModel(session: URLSession(configuration: configuration), preferences: preferences, runtimeOwner: runtimeOwner)
        model.ready = true
        try! model.restoreThread(["id": "fixture-thread", "messages": [[String: Any]]()])
        return model
    }
    static func frame(_ type: String, _ payload: [String: Any]) -> [String: Any] { ["type": type, "payload": payload] }

    @MainActor static func main() async {
        do {
            let prefsSuite = "feral.preferences-startup-fixture." + UUID().uuidString
            let fixturePrefs = UserDefaults(suiteName: prefsSuite)!
            defer { fixturePrefs.removePersistentDomain(forName: prefsSuite) }
            fixturePrefs.set("Existing fixture", forKey: "displayName")
            fixturePrefs.set(true, forKey: "onboarded")
            var suiteCalls = 0
            let sameDomain = NativeModel.resolvePreferences(suiteName: "fixture.bundle", bundleIdentifier: "fixture.bundle", standard: fixturePrefs) { _ in suiteCalls += 1; return nil }
            try expect(sameDomain === fixturePrefs && suiteCalls == 0 && sameDomain?.string(forKey: "displayName") == "Existing fixture", "bundle-domain startup must use existing standard defaults without recreating suite")
            let independent = NativeModel.resolvePreferences(suiteName: "explicit.fixture", bundleIdentifier: "fixture.bundle", standard: fixturePrefs) { name in suiteCalls += 1; return name == "explicit.fixture" ? fixturePrefs : nil }
            try expect(independent === fixturePrefs && suiteCalls == 1, "explicit independent suite bypassed selected profile")
            let unavailablePreferences = NativeModel.resolvePreferences(suiteName: "unavailable.fixture", bundleIdentifier: "fixture.bundle", standard: fixturePrefs) { _ in nil }
            try expect(unavailablePreferences == nil, "failed explicit suite fell back to wrong profile")
            let fixtureConfiguration = URLSessionConfiguration.ephemeral
            fixtureConfiguration.protocolClasses = [WireProtocol.self]
            let startup = NativeModel(session: URLSession(configuration: fixtureConfiguration), preferencesResolver: { sameDomain })
            try expect(startup.onboarded && startup.displayName == "Existing fixture", "real initializer resolution path lost saved profile")
            WireProtocol.reset()
            let blocked = NativeModel(session: URLSession(configuration: fixtureConfiguration), preferencesResolver: { nil })
            try expect(!blocked.onboarded && !blocked.ready && blocked.error?.contains("not been changed") == true, "nil defaults resolution crashed or silently started fresh profile")
            blocked.displayName = "Must never persist"; blocked.saveProfile(); blocked.completeProfileOnboarding()
            await blocked.saveSettings(); await blocked.start()
            blocked.ready = true
            let blockedSend = await blocked.sendChat("Must not dispatch")
            try expect(!blockedSend && !blocked.ready && blocked.featureBaseURL == nil && blocked.securityBaseURL == nil && !blocked.onboarded && WireProtocol.captured.isEmpty, "unavailable preferences permitted startup, onboarding or network effects")
            try expect(fixturePrefs.string(forKey: "displayName") == "Existing fixture" && fixturePrefs.bool(forKey: "onboarded"), "nil resolver reset existing fixture profile")
            print("PASS app-domain defaults startup preserves settings; nil explicit suites fail closed before profile/runtime/network effects")
            WireProtocol.reset()
            let empty = model(); empty.isSending = true
            await empty.consume(frame("text_response", ["text": ""]))
            try expect(!empty.isSending && empty.error != nil && empty.messages.isEmpty, "empty text terminal must fail without assistant prose")
            let terminal = model(); terminal.isSending = true
            await terminal.consume(frame("stream_delta", ["delta": "", "is_final": true]))
            try expect(!terminal.isSending && terminal.error != nil && terminal.messages.isEmpty, "empty stream terminal must fail")
            print("PASS empty chat terminal handling")

            WireProtocol.reset()
            let partial = model(); partial.isSending = true
            partial.messages = [NativeMessage(id: "user", role: "user", text: "hello")]
            await partial.consume(frame("stream_delta", ["delta": "unfinished", "is_final": false]))
            try expect(partial.messages.count == 2, "nonterminal stream must display actual partial output")
            await partial.consume(frame("error", ["message": "model failed", "code": "llm_provider_error", "recoverable": true]))
            try expect(!partial.isSending && partial.error == "model failed" && partial.messages.count == 1, "failed partial output must not become a completed assistant")
            let rows = WireProtocol.bodies("/api/conversations/save").last?["messages"] as? [[String: Any]] ?? []
            try expect(rows.count == 1 && rows[0]["role"] as? String == "user", "error persistence must exclude incomplete assistant")
            print("PASS error after partial stream and saved conversation")

            WireProtocol.reset()
            let success = model(); success.isSending = true
            await success.consume(frame("stream_delta", ["delta": "real ", "is_final": false]))
            await success.consume(frame("stream_delta", ["delta": "reply", "is_final": true]))
            try expect(!success.isSending && success.messages.count == 1 && success.messages[0].text == "real reply", "successful stream must finish exactly one assistant")
            let saved = WireProtocol.bodies("/api/conversations/save").last?["messages"] as? [[String: Any]] ?? []
            try expect(saved.count == 1 && (saved[0]["content"] as? String ?? saved[0]["text"] as? String) == "real reply", "successful assistant must be saved")
            print("PASS successful streamed reply persistence")

            let coding = model()
            coding.updateCoding(["status": "completed", "session_handle": "old", "tool_calls": [
                ["tool_call_id": "call", "title": "Run check", "status": "in_progress", "action_kind": "execute"],
                ["tool_call_id": "call", "title": "", "status": "completed", "text": "CHECK_PASSED", "action_kind": ""]
            ]])
            try expect(coding.coding.actions.count == 1 && coding.coding.actions[0].status == "completed" && coding.coding.actions[0].title == "Run check", "tool updates must merge by real call ID")
            try expect(coding.coding.actions[0].detail.contains("execute") && coding.coding.actions[0].detail.contains("CHECK_PASSED"), "merged action must retain classification and actual output")
            print("PASS observed action update deduplication")

            coding.updateCoding(["status": "awaiting_permission", "pending_permissions": [["request_id": "p", "title": "Edit files", "details": [
                "kind": "edit", "rawInput": ["diff": "first proposed diff"],
                "content": [["type": "diff", "oldText": "old-first", "newText": "new-first", "path": "first.txt"], ["type": "diff", "oldText": "old-second", "newText": "new-second", "path": "second.txt"]]
            ]]]])
            let permission = coding.coding.permissions[0]
            let review = permission.before + permission.after + permission.diff
            try expect(review.contains("old-first") && review.contains("new-first") && review.contains("old-second") && review.contains("new-second"), "permission review must expose all file changes")
            coding.updateCoding(["status": "awaiting_permission", "pending_permissions": [["request_id": "shell", "details": ["kind": "execute", "rawInput": ["command": "python check.py"]]]]])
            try expect(coding.coding.permissions[0].diff.contains("python check.py"), "permission review must expose actual shell command")
            try expect(coding.coding.permissions[0].reviewable, "exact shell command must remain reviewable")
            coding.updateCoding(["status": "awaiting_permission", "pending_permissions": [["request_id": "unknown-read", "details": ["kind": "read", "rawInput": [:], "locations": []]]]])
            try expect(!coding.coding.permissions[0].reviewable, "empty engine metadata must not enable approval")
            WireProtocol.reset()
            await coding.answerPermission("unknown-read", true)
            try expect(WireProtocol.bodies("/api/coding/permissions/unknown-read").isEmpty, "unreviewable approval must never be dispatched")
            print("PASS actual ACP multi-file diff and shell review")

            WireProtocol.reset(["/api/coding/tasks": ["status": "failed", "session_handle": "failed", "error": "engine unavailable", "tool_calls": [], "pending_permissions": []]])
            let failed = model(); failed.coding.workspace = "/tmp/project"
            await failed.startCoding("do work")
            try expect(failed.coding.status == "failed" && failed.error?.contains("engine unavailable") == true, "structured failed task must set terminal status and show error")
            print("PASS structured failed coding task")

            WireProtocol.reset(["/api/coding/workspaces": ["path": "/tmp/new"], "/api/coding/tasks": ["status": "completed", "session_handle": "new", "tool_calls": [], "pending_permissions": []]])
            let workspace = model(); workspace.coding.workspace = "/tmp/old"
            workspace.updateCoding(["status": "completed", "session_handle": "old", "text": "old project output"])
            await workspace.grantWorkspace("/tmp/new")
            try expect(workspace.coding.text.isEmpty && workspace.coding.status.isEmpty, "new project must clear prior task state")
            await workspace.startCoding("new project request")
            let posted = WireProtocol.bodies("/api/coding/tasks").last ?? [:]
            try expect(posted["workspace_dir"] as? String == "/tmp/new" && posted["session_handle"] == nil, "new workspace task must not reuse old session authorization")
            print("PASS new workspace clears old coding session")
            let records: [[String: Any]] = [
                ["id": "rich", "role": "user", "content": [["type": "image", "handle": "private-fixture"]], "attachments": [["name": "fixture.png"]], "opaque": ["count": 7]],
                ["id": "tool", "role": "tool", "content": ["result": "fixture result"]],
                ["id": "no-text", "role": "user", "attachments": [["name": "fixture.pdf"]]]
            ]
            let restored = model(); try restored.restoreThread(["id": "rich-thread", "messages": records])
            try expect(restored.messages.count == 3 && restored.messages[1].role == "tool", "structured and attachment-only records must not disappear or become assistant messages")
            let savedRich = restored.messages.map(\.savedRecord)
            try expect(JSONSerialization.isValidJSONObject(savedRich), "all restored metadata remains serializable")
            let before = try JSONSerialization.data(withJSONObject: records, options: [.sortedKeys])
            let after = try JSONSerialization.data(withJSONObject: savedRich, options: [.sortedKeys])
            try expect(before == after, "structured content and unknown metadata must survive an untouched round trip")
            WireProtocol.reset()
            await restored.consume(frame("text_response", ["text": "fixture reply"]))
            let save = WireProtocol.bodies("/api/conversations/save").last ?? [:]
            try expect(save["id"] as? String == "rich-thread" && save["title"] == nil, "autosave must bind its thread and must not overwrite a custom title")
            try expect((save["messages"] as? [[String: Any]])?.count == 4, "autosave must retain structured records alongside new reply")
            restored.conversationDeleted("deleted-fixture")
            do {
                try restored.restoreThread(["id": "deleted-fixture", "messages": []]); throw AssertionFailure(description: "deleted thread was accepted")
            } catch is NativeFailure { }
            print("PASS structured-message preservation, title safety and deletion tombstone")
            WireProtocol.reset(["/api/coding": ["agents": [], "provider": ["prepared": true]],
                                "/api/coding/tasks": ["status": "completed", "session_handle": "remembered-session", "tool_calls": [], "pending_permissions": []]])
            let recalled = model()
            let staged = await recalled.stageCodingSession("remembered-session", workspace: "/tmp/remembered-project")
            try expect(staged && recalled.coding.workspace == "/tmp/remembered-project" && recalled.coding.status == "recalled", "recalled session stages its actual workspace without claiming live execution")
            try expect(WireProtocol.bodies("/api/coding/workspaces").isEmpty && WireProtocol.bodies("/api/coding/tasks").isEmpty, "selection must neither grant a folder nor start work")
            await recalled.startCoding("explicit follow-up request")
            let continuation = WireProtocol.bodies("/api/coding/tasks").last ?? [:]
            try expect(continuation["session_handle"] as? String == "remembered-session", "explicit continuation carries the recalled handle")
            recalled.coding.status = "running"
            let refused = await recalled.stageCodingSession("another-session", workspace: "/tmp/another-project")
            try expect(!refused && recalled.coding.workspace == "/tmp/remembered-project", "active coding refuses session replacement")
            print("PASS recalled coding session authorization boundary")
            WireProtocol.reset(["/api/coding": ["provider": ["prepared": true]]])
            let changingCoding = model()
            changingCoding.coding.endpoint = "http://127.0.0.1:11435/v1"
            changingCoding.coding.modelName = "fixture-model"
            changingCoding.coding.status = "running"
            let activeChange = await changingCoding.prepareCoding()
            try expect(!activeChange && WireProtocol.bodies("/api/coding/provider").isEmpty, "active coding must refuse provider replacement before HTTP")
            changingCoding.coding.status = "awaiting_permission"
            let pendingChange = await changingCoding.prepareCoding()
            try expect(!pendingChange && WireProtocol.bodies("/api/coding/provider").isEmpty, "pending approval must retain its coding provider")
            changingCoding.coding.status = "completed"
            let idleChange = await changingCoding.prepareCoding()
            try expect(idleChange && WireProtocol.bodies("/api/coding/provider").count == 1, "explicit idle model connection must return verified prepared status")
            changingCoding.ready = false
            let unavailableChange = await changingCoding.prepareCoding()
            try expect(!unavailableChange && WireProtocol.bodies("/api/coding/provider").count == 1, "unavailable runtime cannot replace coding provider")
            print("PASS coding provider replacement live-turn and readiness boundaries")
            let attached = model()
            attached.pendingAttachments = [NativeAttachmentRef(id: "fixture-upload", filename: "fixture.txt", contentType: "text/plain", sizeBytes: 7, sha256: String(repeating: "a", count: 64))]
            let refusedAttachment = await attached.sendChat("read the file")
            try expect(!refusedAttachment && attached.messages.isEmpty && !attached.isSending && attached.pendingAttachments.count == 1, "unreviewed attachments cannot dispatch, append or clear a message")
            let staleAttachment = await attached.sendChat("read the file", authorizedAttachmentIDs: ["old-upload"])
            try expect(!staleAttachment && attached.attachmentError != nil, "stale attachment review cannot authorize different file contents")
            print("PASS attachment content authorization and stale review boundary")
            let scoped = model()
            await scoped.consume(frame("todo_update", ["session_id": "other-thread", "todos": [["id": "outside"]]]))
            try expect(scoped.observedTodos == nil, "todos from a different conversation cannot populate current tools")
            await scoped.consume(frame("todo_update", ["session_id": "fixture-thread", "todos": [["id": "mine", "future": ["keep": 1]]]]))
            try expect(scoped.observedTodos?.first?["id"] as? String == "mine", "observed todos bind to their actual thread")
            try scoped.restoreThread(["id": "another-thread", "messages": []])
            try expect(scoped.observedTodos == nil, "thread changes cannot carry stale observed todos")
            WireProtocol.reset()
            scoped.isSending = true
            do { try await scoped.applySnapshotHistory("target", history: records); throw AssertionFailure(description: "active chat accepted snapshot") } catch is NativeFailure { }
            try expect(WireProtocol.bodies("/api/conversations/save").isEmpty, "active chat snapshot refusal sends no write")
            scoped.isSending = false
            WireProtocol.reset(["/api/conversations/save": ["id": "wrong-target"]])
            do { try await scoped.applySnapshotHistory("target", history: records); throw AssertionFailure(description: "wrong save receipt accepted") } catch is NativeFailure { }
            try expect(scoped.activeConversationID == "another-thread" && !scoped.switchingConversation, "unconfirmed snapshot save cannot replace visible thread")
            let proposed = WireProtocol.bodies("/api/conversations/save").last ?? [:]
            try expect(proposed["title"] == nil && (proposed["messages"] as? [[String: Any]])?.count == records.count, "explicit snapshot application preserves rows and custom title")
            WireProtocol.reset(["/api/conversations/save": ["id": "target"], "/api/conversations/target": ["id": "target", "messages": []]])
            do { try await scoped.applySnapshotHistory("target", history: records); throw AssertionFailure(description: "mismatched readback accepted") } catch is NativeFailure { }
            try expect(scoped.activeConversationID == "another-thread", "save/readback mismatch retains visible prior thread")
            print("PASS observed todo ownership and snapshot active-turn/receipt/readback boundaries")
            WireProtocol.reset()
            let rich = model()
            try rich.restoreThread(["id": "rich-live", "messages": []])
            await rich.consume(["type": "stream_delta", "session_id": "rich-live", "payload": ["kind": "reasoning", "delta": "fixture analysis"]])
            try expect(rich.messages.isEmpty && rich.richChat.reasoning == "fixture analysis", "reasoning must not become answer text")
            await rich.consume(["type": "stream_delta", "payload": ["session_id": "foreign", "kind": "reasoning", "delta": "foreign"]])
            await rich.consume(["type": "stream_delta", "payload": ["kind": "reasoning", "delta": "unscoped"]])
            try expect(rich.messages.isEmpty && rich.richChat.reasoning == "fixture analysis", "foreign or unscoped reasoning must never fall through into an answer")
            await rich.consume(["type": "tool_result", "session_id": "foreign", "payload": ["tool": "read", "call_id": "foreign", "success": true]])
            try expect(rich.richChat.tools.isEmpty, "foreign structured events must not enter current chat")
            await rich.consume(["type": "tool_result", "session_id": "rich-live", "payload": ["tool": "read", "call_id": "fixture", "success": false, "error_code": "policy_denied"]])
            await rich.consume(["type": "text_response", "session_id": "rich-live", "payload": ["text": "fixture answer", "model": "fixture model", "usage": ["tokens": 7]]])
            try expect(rich.messages.last?.text == "fixture answer", "answer must exclude reasoning")
            let richRow = rich.messages.last?.savedRecord ?? [:]
            try expect(richRow["reasoning"] as? String == "fixture analysis" && (richRow["tools"] as? [[String: Any]])?.first?["status"] as? String == "policy_denied", "saved answer must retain reasoning and truthful tool outcome")
            try expect((richRow["response_payload"] as? [String: Any])?["model"] as? String == "fixture model", "terminal metadata must remain preserved")
            print("PASS rich wire reasoning separation, ownership and metadata preservation")
            rich.isSending = true
            await rich.consume(["type": "refusal", "session_id": "rich-live", "payload": ["reason": "fixture refusal"]])
            try expect(!rich.isSending && rich.chatError == "fixture refusal", "refused turn must terminate without fake answer")
            rich.isSending = true
            await rich.consume(["type": "stream_delta", "session_id": "rich-live", "payload": ["delta": "incomplete fixture"]])
            await rich.consume(["type": "budget_exceeded", "session_id": "rich-live", "payload": ["call_site": "chat", "cap_dollars": 1]])
            try expect(!rich.isSending && rich.messages.last?.metadata["responseIncomplete"] as? Bool == true && rich.messages.last?.metadata["deliveryError"] is String, "budget-capped partial must carry an explicit incomplete marker")
            do {
                try await rich.respondToChatPermission(NativeRichPermissionResponse(sessionID: "foreign", requestID: "fixture", granted: true, connectionID: UUID()))
                throw AssertionFailure(description: "foreign permission response accepted")
            } catch is NativeFailure { }
            print("PASS declined turn completion and stale permission dispatch refusal")
            let cancellation = model()
            cancellation.updateCoding(["session_handle": "cancel-fixture", "status": "running", "pending_permissions": [["request_id": "held", "title": "fixture approval"]]])
            WireProtocol.reset(["/api/coding/sessions/cancel-fixture/cancel": ["handle": "cancel-fixture", "closed": false]])
            await cancellation.cancelCoding()
            try expect(cancellation.coding.status == "running" && !cancellation.coding.permissions.isEmpty && cancellation.error != nil, "unconfirmed cancellation must not become a closed session or erase pending state")
            WireProtocol.reset(["/api/coding/sessions/cancel-fixture/cancel": ["handle": "foreign", "closed": true]])
            await cancellation.cancelCoding()
            try expect(cancellation.coding.status == "running", "foreign cancellation receipt must not close current coding task")
            WireProtocol.reset(["/api/coding/sessions/cancel-fixture/cancel": ["handle": "cancel-fixture", "closed": true]])
            await cancellation.cancelCoding()
            try expect(cancellation.coding.status == "closed" && cancellation.coding.permissions.isEmpty && !cancellation.codingBusy && cancellation.error == nil, "exact closed receipt must stop observed coding session and clear obsolete uncertainty")
            print("PASS coding cancellation exact receipt and uncertain outcome boundary")
            let isolatedApps = model()
            let appScope = isolatedApps.appSurfaceSessionID!
            try expect(appScope != isolatedApps.activeConversationID && appScope == isolatedApps.appSurfaceSessionID, "app scope must be distinct and stable")
            isolatedApps.isSending = true
            await isolatedApps.consume(["type": "text_response", "session_id": appScope, "payload": ["text": "app-only reply", "session_id": appScope]])
            try expect(isolatedApps.isSending && isolatedApps.messages.isEmpty, "late app reply must not complete or populate Chat")
            try isolatedApps.restoreThread(["id": "different-app-owner", "messages": [[String: Any]]()])
            try expect(isolatedApps.appSurfaceSessionID != appScope, "switching saved Chat thread must rotate the associated app session")
            print("PASS isolated app action session cannot complete active Chat turn")
            let primary = "primary-fixture"
            let richSaved: [[String: Any]] = [["id": "rich-saved", "role": "assistant", "content": "Existing reply", "reasoning": "Preserve", "tools": [["id": "kept-tool"]]]]
            func startupReplies(_ selected: String = "primary-fixture") -> [String: [String: Any]] {
                ["/api/sessions/primary": ["session_id": primary],
                 "/api/conversations/" + selected: ["id": selected, "messages": richSaved],
                 "/api/sessions/primary/transcript": ["session_id": primary, "primary_session_id": primary, "messages": [["role": "assistant", "text": "Existing reply", "ts_ms": 1], ["role": "user", "text": "Missed turn", "ts_ms": 2], ["role": "user", "text": "Missed turn", "ts_ms": 3]], "count": 3],
                 "/api/sessions/isolated/transcript": ["session_id": "isolated", "messages": [], "count": 0]]
            }
            WireProtocol.reset(startupReplies())
            let canonical = model()
            try await canonical.resolveStartupConversation()
            try expect(canonical.activeConversationID == primary && canonical.messages.count == 3 && !canonical.switchingConversation, "boot uses canonical primary ID and preserves repeated live turns")
            try expect(canonical.messages.first?.metadata["reasoning"] as? String == "Preserve" && (canonical.messages.first?.metadata["tools"] as? [[String: Any]])?.first?["id"] as? String == "kept-tool", "primary merge retains complete saved assistant metadata")
            try expect(WireProtocol.bodies("/api/conversations/active/thread").isEmpty && WireProtocol.bodies("/api/conversations/new").isEmpty, "existing canonical thread does not use arbitrary recent resolution or create")
            try expect(canonical.recoveryStatus.lowercased().contains("snapshot-restored"), "primary status distinguishes missed live turns from model snapshot context")
            let selectionSuite = "theora.native-selection-tests." + UUID().uuidString
            let selectionPrefs = UserDefaults(suiteName: selectionSuite)!
            defer { selectionPrefs.removePersistentDomain(forName: selectionSuite) }
            let selectionKey = "feral.native.selectedConversation." + primary.utf8.map { String(format: "%02x", $0) }.joined()
            selectionPrefs.set("isolated", forKey: selectionKey)
            WireProtocol.reset(startupReplies("isolated"))
            let remembered = model(preferences: selectionPrefs)
            try await remembered.resolveStartupConversation()
            try expect(remembered.activeConversationID == "isolated" && remembered.messages.count == 1, "remembered isolated selection stays exact and receives no primary transcript")
            try expect(WireProtocol.bodies("/api/sessions/primary/transcript").isEmpty && WireProtocol.bodies("/api/sessions/isolated/transcript").count == 1, "isolated hydration reads only exact session transcript")
            try expect(remembered.recoveryStatus.contains("not injected"), "isolated status discloses no automatic runtime-history restoration")
            var unavailable = startupReplies(); unavailable["/api/conversations/" + primary] = ["error": "Memory not initialized"]
            WireProtocol.reset(unavailable)
            let unknown = model()
            do { try await unknown.resolveStartupConversation(); throw AssertionFailure(description: "unknown memory state was treated as missing") } catch is NativeFailure { }
            try expect(unknown.activeConversationID == "fixture-thread" && WireProtocol.bodies("/api/conversations/new").isEmpty, "unknown saved-thread failure never creates or overwrites canonical records")
            var missing = startupReplies(); missing["/api/conversations/" + primary] = ["error": "Not found"]
            missing["/api/conversations/new"] = ["ok": true, "id": primary, "create_if_missing": true, "created": true, "conversation": ["id": primary, "messages": []]]
            WireProtocol.reset(missing)
            WireProtocol.lock.lock(); WireProtocol.sequences["/api/conversations/" + primary] = [["error": "Not found"], ["id": primary, "messages": []]]; WireProtocol.lock.unlock()
            let createdPrimary = model()
            try await createdPrimary.resolveStartupConversation()
            try expect(createdPrimary.activeConversationID == primary && WireProtocol.bodies("/api/conversations/new").first?["id"] as? String == primary && WireProtocol.bodies("/api/conversations/new").first?["create_if_missing"] as? Bool == true, "explicit missing canonical record creates exact primary ID with readback")
            missing["/api/conversations/new"] = ["ok": true, "id": primary, "create_if_missing": true, "created": false, "conversation": ["id": primary, "messages": richSaved]]
            WireProtocol.reset(missing)
            WireProtocol.lock.lock(); WireProtocol.sequences["/api/conversations/" + primary] = [["error": "Not found"], ["id": primary, "messages": richSaved]]; WireProtocol.lock.unlock()
            let existingWinner = model(); try await existingWinner.resolveStartupConversation()
            try expect(existingWinner.messages.first?.metadata["reasoning"] as? String == "Preserve", "atomic concurrently existing winner keeps full saved records")
            missing["/api/conversations/new"] = ["ok": true, "id": primary, "message_count": 0]
            WireProtocol.reset(missing)
            let oldUpsertContract = model()
            do { try await oldUpsertContract.resolveStartupConversation(); throw AssertionFailure(description: "legacy overwrite contract accepted for canonical creation") } catch is NativeFailure { }
            try expect(oldUpsertContract.activeConversationID == "fixture-thread", "unsupported legacy atomic receipt fails closed")
            missing["/api/conversations/new"] = ["ok": true, "id": "foreign", "create_if_missing": true, "created": true, "conversation": ["id": "foreign", "messages": []]]
            WireProtocol.reset(missing)
            let wrongCreation = model()
            do { try await wrongCreation.resolveStartupConversation(); throw AssertionFailure(description: "foreign creation receipt accepted") } catch is NativeFailure { }
            try expect(wrongCreation.activeConversationID == "fixture-thread", "foreign canonical creation acknowledgement retains previous visible thread")
            var foreignTranscript = startupReplies(); foreignTranscript["/api/sessions/primary/transcript"] = ["session_id": "foreign", "primary_session_id": primary, "messages": [], "count": 0]
            WireProtocol.reset(foreignTranscript)
            let recoveryFailure = model(); try await recoveryFailure.resolveStartupConversation()
            try expect(recoveryFailure.messages.count == 1 && recoveryFailure.recoveryStatus.contains("unavailable"), "foreign transcript fails without clearing saved canonical records")
            WireProtocol.reset(["/api/sessions/primary": ["error": "not ready"]])
            let noPrimary = model()
            do { try await noPrimary.resolveStartupConversation(); throw AssertionFailure(description: "unverified primary accepted") } catch is NativeSessionRecoveryFailure { }
            try expect(noPrimary.activeConversationID == "fixture-thread" && noPrimary.recoveryStatus.contains("unavailable"), "unverified primary keeps previous thread without guessing shared identity")
            WireProtocol.reset(startupReplies())
            WireProtocol.lock.lock(); WireProtocol.delayedPath = "/api/sessions/primary"; WireProtocol.lock.unlock()
            let supersededStartup = model()
            let oldStartup = Task { try await supersededStartup.resolveStartupConversation() }
            while WireProtocol.bodies("/api/sessions/primary").isEmpty { await Task.yield() }
            try await supersededStartup.resolveStartupConversation()
            do { try await oldStartup.value; throw AssertionFailure(description: "stale startup lookup accepted") } catch is NativeSessionRecoveryFailure { }
            try expect(supersededStartup.activeConversationID == primary && supersededStartup.messages.count == 3 && !supersededStartup.switchingConversation, "superseded startup cannot replace newer canonical selection or clear operation lock")
            print("PASS canonical primary startup, remembered isolation, exact creation and recovery failure boundaries")
            WireProtocol.reset()
            let quitPartial = model(); quitPartial.isSending = true
            quitPartial.messages = [NativeMessage(id: "quit-user", role: "user", text: "Request")]
            await quitPartial.consume(frame("stream_delta", ["delta": "Partial actual output", "is_final": false]))
            await quitPartial.flushConversationForShutdown()
            let quitRows = WireProtocol.bodies("/api/conversations/save").last?["messages"] as? [[String: Any]] ?? []
            try expect(quitRows.count == 2 && quitRows.last?["responseIncomplete"] as? Bool == true && quitRows.last?["deliveryError"] as? String == "The app closed before this reply completed.", "shutdown flush keeps actual partial text with explicit incomplete metadata")
            try expect(!quitPartial.isSending && quitPartial.messages.last?.text == "Partial actual output", "shutdown flush does not claim completion or discard partial output")
            WireProtocol.reset()
            WireProtocol.lock.lock(); WireProtocol.delayedPath = "/api/conversations/save"; WireProtocol.lock.unlock()
            let serial = model()
            let olderSave = Task { await serial.consume(frame("text_response", ["text": "First reply"])) }
            while WireProtocol.bodies("/api/conversations/save").isEmpty { await Task.yield() }
            let newerSave = Task { await serial.consume(frame("text_response", ["text": "Second reply"])) }
            for _ in 0..<100 { await Task.yield() }
            try expect(WireProtocol.bodies("/api/conversations/save").count == 1, "new save waits for previous in-flight save")
            await olderSave.value; await newerSave.value
            let ordered = WireProtocol.bodies("/api/conversations/save")
            try expect(ordered.count == 2 && (ordered.last?["messages"] as? [[String: Any]])?.count == 2, "serialized latest save retains both replies instead of older overwrite")
            print("PASS shutdown incomplete reply flush and serialized saves")
            WireProtocol.reset()
            let healthOwner = NativeRuntimeOwnership(generation: UUID(), instanceID: "owned-fixture", baseURL: URL(string: "http://127.0.0.1:9465")!, processIdentity: UUID())
            var currentHealthOwner = healthOwner
            let dying = model(runtimeOwner: { currentHealthOwner })
            dying.isSending = true
            dying.messages = [NativeMessage(id: "pending", role: "user", text: "Do not replay")]
            dying.pendingAttachments = [NativeAttachmentRef(id: "held", filename: "fixture.txt", contentType: "text/plain", sizeBytes: 1, sha256: String(repeating: "b", count: 64))]
            dying.updateCoding(["status": "running", "session_handle": "owned-code", "text": "Observed output"])
            await dying.consume(frame("stream_delta", ["delta": "Actual unfinished output", "is_final": false]))
            func unavailableEvent(_ owner: NativeRuntimeOwnership, phase: NativeRuntimeHealthPhase = .unavailable) -> NativeRuntimeHealthEvent {
                NativeRuntimeHealthEvent(ownership: owner, phase: phase, reason: "Owned runtime exited; previous actions may have taken effect.", requiresExplicitRestart: phase == .unavailable, reconnectVerifiedSession: false, availableForActions: false)
            }
            let staleHealthOwner = NativeRuntimeOwnership(generation: UUID(), instanceID: "old", baseURL: healthOwner.baseURL, processIdentity: UUID())
            dying.observeRuntimeHealth(unavailableEvent(staleHealthOwner))
            try expect(dying.ready && dying.isSending, "foreign runtime event cannot disable current Chat")
            let oldAppScope = dying.appSurfaceSessionID
            dying.observeRuntimeHealth(NativeRuntimeHealthEvent(ownership: healthOwner, phase: .ready, reason: "Transport latency", requiresExplicitRestart: false, reconnectVerifiedSession: false, availableForActions: true, healthWarning: "Health verification is delayed."))
            try expect(dying.ready && dying.isSending && dying.coding.status == "running" && dying.appSurfaceSessionID == oldAppScope && dying.runtimeHealthWarning != nil, "short owned probe delay must preserve the current response and coding authority while disclosing latency")
            dying.observeRuntimeHealth(NativeRuntimeHealthEvent(ownership: healthOwner, phase: .ready, reason: "Verified", requiresExplicitRestart: false, reconnectVerifiedSession: false, availableForActions: true))
            try expect(dying.runtimeHealthWarning == nil && dying.isSending, "verified health clears warning without restarting an active response")
            dying.observeRuntimeHealth(unavailableEvent(healthOwner))
            try expect(!dying.ready && !dying.isSending && dying.featureBaseURL == nil, "current owned exit clears readiness and feature authority")
            try expect(dying.messages.count == 2 && dying.messages.last?.text == "Actual unfinished output" && dying.messages.last?.metadata["responseIncomplete"] as? Bool == true, "backend loss preserves actual partial output as incomplete")
            try expect(dying.pendingAttachments.count == 1 && dying.coding.text == "Observed output" && dying.coding.status == "connection_lost", "runtime failure retains attachment and coding observations while marking live task status unavailable")
            try expect(dying.appSurfaceSessionID != oldAppScope && dying.recoveryStatus.contains("not retried"), "runtime failure invalidates app scope and discloses no replay")
            let writesBefore = WireProtocol.bodies("/api/conversations/save").count
            let attemptedReplay = await dying.sendChat("Do not replay")
            try expect(!attemptedReplay && WireProtocol.bodies("/api/conversations/save").count == writesBefore, "unavailable runtime does not send or save an automatic replay")
            currentHealthOwner = staleHealthOwner
            dying.ready = true
            dying.observeRuntimeHealth(unavailableEvent(healthOwner))
            try expect(dying.ready, "old exit cannot disable a replacement owner")
            dying.observeRuntimeHealth(unavailableEvent(staleHealthOwner, phase: .ready))
            try expect(!dying.ready && dying.messages.count == 2, "sleep/wake verification gates actions while retaining visible history")
            dying.observeRuntimeHealth(NativeRuntimeHealthEvent(ownership: staleHealthOwner, phase: .limited, reason: "Memory unavailable; use Security.", requiresExplicitRestart: false, reconnectVerifiedSession: false, availableForActions: false, serviceReachable: true))
            try expect(!dying.ready && dying.serviceReachable && dying.securityBaseURL != nil && dying.featureBaseURL == nil, "limited memory readiness exposes Security only and never full agent features")
            print("PASS owned runtime events, stale callback rejection, partial preservation and no replay")
            print("NATIVE_MODEL_WIRE_TESTS_PASSED: 20 groups; mocked HTTP/wire only, no engine/model execution")
        } catch {
            fputs("NATIVE_MODEL_WIRE_TESTS_FAILED: \(error)\n", stderr)
            exit(1)
        }
    }
}
